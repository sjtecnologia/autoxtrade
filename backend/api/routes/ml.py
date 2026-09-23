"""Endpoints da API para ML (predição e gerenciamento de modelos)."""
from __future__ import annotations

from datetime import datetime, timezone
import csv
import io
import threading
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import verify_token
from database import get_db

router = APIRouter(prefix="/ml", tags=["ml"])

SUPPORTED_TRAIN_TIMEFRAMES = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "1w",
}

_TRAIN_JOBS: dict[str, dict] = {}
_TRAIN_JOBS_LOCK = threading.Lock()
_MAX_TRAIN_JOBS = 20


def _map_mt5_crypto_to_binance(symbol: str) -> str:
    s = symbol.strip().upper()
    if s.endswith(".LV"):
        base = s[:-3]
        if base.endswith("USD") and len(base) > 3:
            return f"{base[:-3]}/USDT"
    return symbol


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_didi_profile(profile: Optional[str]) -> bool:
    return (profile or "didi").strip().lower() == "didi"


def _new_job(job_id: str, market: Optional[str], symbol: Optional[str], timeframe: str) -> None:
    with _TRAIN_JOBS_LOCK:
        _TRAIN_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "market": market,
            "symbol": symbol,
            "timeframe": timeframe,
            "created_at": _utcnow_iso(),
            "started_at": None,
            "finished_at": None,
            "total_symbols": 0,
            "completed_symbols": 0,
            "failed_symbols": 0,
            "rejected_symbols": 0,
            "approved_symbols": 0,
            "symbols": {},
        }


def _trim_jobs() -> None:
    with _TRAIN_JOBS_LOCK:
        if len(_TRAIN_JOBS) <= _MAX_TRAIN_JOBS:
            return
        keys = sorted(_TRAIN_JOBS.keys(), key=lambda k: _TRAIN_JOBS[k]["created_at"])
        for k in keys[:-_MAX_TRAIN_JOBS]:
            _TRAIN_JOBS.pop(k, None)


def _mark_job_started(job_id: str, symbols: list[tuple[str, str]]) -> None:
    with _TRAIN_JOBS_LOCK:
        job = _TRAIN_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "running"
        job["started_at"] = _utcnow_iso()
        job["total_symbols"] = len(symbols)
        for market, sym in symbols:
            key = f"{market}/{sym}"
            job["symbols"][key] = {
                "market": market,
                "symbol": sym,
                "status": "pending",
                "message": "Aguardando execução",
                "started_at": None,
                "finished_at": None,
            }


def _update_symbol(job_id: str, market: str, sym: str, status_value: str, message: str) -> None:
    with _TRAIN_JOBS_LOCK:
        job = _TRAIN_JOBS.get(job_id)
        if not job:
            return

        key = f"{market}/{sym}"
        row = job["symbols"].setdefault(
            key,
            {
                "market": market,
                "symbol": sym,
                "status": "pending",
                "message": "Aguardando execução",
                "started_at": None,
                "finished_at": None,
            },
        )
        old_status = row["status"]
        row["status"] = status_value
        row["message"] = message
        if status_value == "running" and row["started_at"] is None:
            row["started_at"] = _utcnow_iso()
        if status_value in {"completed", "failed", "rejected"}:
            row["finished_at"] = _utcnow_iso()

        if old_status == status_value:
            return
        if status_value == "completed":
            job["completed_symbols"] += 1
            job["approved_symbols"] += 1
        elif status_value == "failed":
            job["failed_symbols"] += 1
        elif status_value == "rejected":
            job["completed_symbols"] += 1
            job["rejected_symbols"] += 1


def _finish_job(job_id: str, failed: bool = False, message: str = "") -> None:
    with _TRAIN_JOBS_LOCK:
        job = _TRAIN_JOBS.get(job_id)
        if not job:
            return
        job["status"] = "failed" if failed else "completed"
        job["finished_at"] = _utcnow_iso()
        if message:
            job["message"] = message


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PredictRequest(BaseModel):
    symbol: str


class PredictResponse(BaseModel):
    symbol: str
    signal: str
    confidence: float
    model_version: str


class RollbackRequest(BaseModel):
    symbol: str


class RollbackResponse(BaseModel):
    symbol: str
    activated_version: str
    retired_version: str


class MLModelInfo(BaseModel):
    id: int
    version: str
    symbol: Optional[str]
    market: str
    status: str
    bt_profit_factor: Optional[float]
    bt_win_rate: Optional[float]
    bt_total_trades: Optional[int]
    bt_sharpe_ratio: Optional[float]
    bt_max_drawdown: Optional[float]
    bt_total_return: Optional[float]
    ml_accuracy: Optional[float]
    train_samples: int
    is_approved: bool
    trained_at: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/predict", response_model=PredictResponse)
async def predict(
    body: PredictRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Retorna sinal ML (LONG/SHORT/NEUTRO) com confiança para o símbolo."""
    from ml.predictor import ModelNotFoundError, predictor
    from trading.connectors.binance import BinanceConnector

    symbol = body.symbol

    # Buscar últimos 250 candles (1h) para calcular features
    try:
        connector = BinanceConnector()
        await connector.connect()
        df = await connector.get_ohlcv(symbol, "1h", limit=250)
        await connector.close()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Erro ao buscar OHLCV: {exc}",
        )

    # Carregar modelo se necessário
    try:
        result = predictor.predict(symbol, df)
    except ModelNotFoundError:
        # Tenta carregar modelo ativo
        try:
            await predictor.load_model(symbol, db)
            result = predictor.predict(symbol, df)
        except ModelNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            )

    return PredictResponse(
        symbol=symbol,
        signal=result.signal,
        confidence=result.confidence,
        model_version=result.model_version,
    )


@router.post("/rollback", response_model=RollbackResponse)
async def rollback(
    body: RollbackRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Reverte para o modelo anterior, inativando o atual."""
    from datetime import datetime, timezone

    from db.models import MLModel
    from ml.predictor import predictor

    symbol = body.symbol

    # Modelo atual ativo
    result_active = await db.execute(
        select(MLModel)
        .where(MLModel.symbol == symbol, MLModel.status == "active")
        .limit(1)
    )
    current: Optional[MLModel] = result_active.scalar_one_or_none()
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Nenhum modelo ativo para {symbol}",
        )

    # Modelo anterior (mais recente inativo)
    result_prev = await db.execute(
        select(MLModel)
        .where(MLModel.symbol == symbol, MLModel.status == "inactive")
        .order_by(MLModel.trained_at.desc())
        .limit(1)
    )
    previous: Optional[MLModel] = result_prev.scalar_one_or_none()
    if previous is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Nenhum modelo anterior disponível para {symbol}",
        )

    # Troca de status
    retired_version = current.version
    current.status = "retired"
    current.deactivated_at = datetime.now(timezone.utc)

    previous.status = "active"
    previous.activated_at = datetime.now(timezone.utc)

    await db.commit()

    # Recarregar em memória
    await predictor.reload(symbol, db)

    return RollbackResponse(
        symbol=symbol,
        activated_version=previous.version,
        retired_version=retired_version,
    )


@router.get("/models", response_model=list[MLModelInfo])
async def list_models(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_token),
):
    """Lista todos os modelos ML registrados no banco."""
    from db.models import MLModel

    result = await db.execute(
        select(MLModel).order_by(MLModel.trained_at.desc()).limit(50)
    )
    models = result.scalars().all()

    return [
        MLModelInfo(
            id=m.id,
            version=m.version,
            symbol=m.symbol,
            market=m.market,
            status=m.status,
            bt_profit_factor=float(m.bt_profit_factor) if m.bt_profit_factor else None,
            bt_win_rate=float(m.bt_win_rate) if m.bt_win_rate else None,
            bt_total_trades=m.bt_total_trades,
            bt_sharpe_ratio=float(m.bt_sharpe_ratio) if m.bt_sharpe_ratio else None,
            bt_max_drawdown=float(m.bt_max_drawdown) if m.bt_max_drawdown else None,
            bt_total_return=float(m.bt_total_return) if m.bt_total_return else None,
            ml_accuracy=float(m.ml_accuracy) if m.ml_accuracy else None,
            train_samples=m.train_samples,
            is_approved=m.is_approved,
            trained_at=m.trained_at.isoformat(),
        )
        for m in models
    ]


class TrainRequest(BaseModel):
    market: Optional[str] = None   # None = todos os mercados
    symbol: Optional[str] = None   # None = todos os símbolos do mercado
    timeframe: str = "1h"
    profile: str = "didi"  # didi | robust


class OptimizeTimeframesRequest(BaseModel):
    market: Optional[str] = None
    symbol: Optional[str] = None
    timeframes: list[str] = ["15m", "30m", "1h", "4h"]
    profile: str = "didi"


class TrainResponse(BaseModel):
    status: str
    message: str
    job_id: str
    profile: Optional[str] = None


class TrainSymbolStatus(BaseModel):
    market: str
    symbol: str
    status: str
    message: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class TrainJobStatusResponse(BaseModel):
    job_id: str
    status: str
    market: Optional[str] = None
    symbol: Optional[str] = None
    timeframe: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    total_symbols: int
    completed_symbols: int
    failed_symbols: int
    rejected_symbols: int
    approved_symbols: int
    progress_pct: float
    symbols: list[TrainSymbolStatus]


def _score_timeframe_candidate(candidate: dict) -> float:
    """Score simples para ranquear timeframe por símbolo.

    Priorização: profit factor > win rate > total trades.
    """
    if not candidate.get("approved"):
        return -1.0
    pf = float(candidate.get("profit_factor", 0.0) or 0.0)
    wr = float(candidate.get("win_rate", 0.0) or 0.0)
    trades = int(candidate.get("total_trades", 0) or 0)
    trades_bonus = min(trades, 200) / 200.0
    return (pf * 0.6) + ((wr / 100.0) * 0.3) + (trades_bonus * 0.1)


@router.post("/train", response_model=TrainResponse)
async def trigger_train(
    body: TrainRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_token),
):
    """Dispara coleta de dados + treinamento em background (sem negociação)."""

    timeframe = body.timeframe.lower().strip()
    if timeframe not in SUPPORTED_TRAIN_TIMEFRAMES:
        allowed = ", ".join(sorted(SUPPORTED_TRAIN_TIMEFRAMES))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"timeframe inválido: {body.timeframe}. Use um de: {allowed}",
        )

    job_id = str(uuid4())
    _new_job(job_id, body.market, body.symbol, timeframe)

    def _run() -> None:
        import asyncio
        asyncio.run(_train_async(job_id, body.market, body.symbol, timeframe, body.profile))

    background_tasks.add_task(_run)

    target = f"{body.market}/{body.symbol}" if body.symbol else (body.market or "todos os mercados")
    return TrainResponse(
        status="started",
        message=f"Treinamento iniciado para {target} (timeframe={timeframe}, profile={body.profile})",
        profile=body.profile,
        job_id=job_id,
    )


@router.post("/train/optimize-timeframes", response_model=TrainResponse)
async def trigger_train_optimize_timeframes(
    body: OptimizeTimeframesRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(verify_token),
):
    """Testa múltiplos timeframes por ativo e escolhe automaticamente o melhor."""

    tfs = [tf.lower().strip() for tf in body.timeframes if tf and tf.strip()]
    if not tfs:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="timeframes vazio",
        )

    invalid = [tf for tf in tfs if tf not in SUPPORTED_TRAIN_TIMEFRAMES]
    if invalid:
        allowed = ", ".join(sorted(SUPPORTED_TRAIN_TIMEFRAMES))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"timeframe(s) inválido(s): {', '.join(invalid)}. Use: {allowed}",
        )

    job_id = str(uuid4())
    _new_job(job_id, body.market, body.symbol, "multi")

    def _run() -> None:
        import asyncio
        asyncio.run(
            _train_optimize_timeframes_async(
                job_id,
                body.market,
                body.symbol,
                tfs,
                body.profile,
            )
        )

    background_tasks.add_task(_run)
    target = f"{body.market}/{body.symbol}" if body.symbol else (body.market or "todos os mercados")
    return TrainResponse(
        status="started",
        message=(
            f"Otimização de timeframes iniciada para {target}: "
            f"{', '.join(tfs)} (profile={body.profile})"
        ),
        profile=body.profile,
        job_id=job_id,
    )


@router.get("/train/status/{job_id}", response_model=TrainJobStatusResponse)
async def get_train_status(
    job_id: str,
    _: None = Depends(verify_token),
):
    with _TRAIN_JOBS_LOCK:
        job = _TRAIN_JOBS.get(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job não encontrado: {job_id}",
            )

        total = int(job["total_symbols"])
        # completed_symbols já inclui aprovados + rejeitados.
        done = int(job["completed_symbols"]) + int(job["failed_symbols"])
        progress = 0.0 if total == 0 else round((done / total) * 100, 1)
        symbols = [
            TrainSymbolStatus(
                market=s["market"],
                symbol=s["symbol"],
                status=s["status"],
                message=s["message"],
                started_at=s.get("started_at"),
                finished_at=s.get("finished_at"),
            )
            for _, s in sorted(job["symbols"].items())
        ]

        return TrainJobStatusResponse(
            job_id=job["job_id"],
            status=job["status"],
            market=job.get("market"),
            symbol=job.get("symbol"),
            timeframe=job["timeframe"],
            created_at=job["created_at"],
            started_at=job.get("started_at"),
            finished_at=job.get("finished_at"),
            total_symbols=total,
            completed_symbols=int(job["completed_symbols"]),
            failed_symbols=int(job["failed_symbols"]),
            rejected_symbols=int(job["rejected_symbols"]),
            approved_symbols=int(job["approved_symbols"]),
            progress_pct=progress,
            symbols=symbols,
        )


@router.get("/train/report/{job_id}.csv", response_class=PlainTextResponse)
async def get_train_report_csv(
    job_id: str,
    _: None = Depends(verify_token),
):
    with _TRAIN_JOBS_LOCK:
        job = _TRAIN_JOBS.get(job_id)
        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Job não encontrado: {job_id}",
            )

        symbols = [s for _, s in sorted(job["symbols"].items())]

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "job_id",
        "job_status",
        "timeframe",
        "market",
        "symbol",
        "status",
        "message",
        "started_at",
        "finished_at",
    ])

    for s in symbols:
        writer.writerow([
            job_id,
            job.get("status", ""),
            job.get("timeframe", ""),
            s.get("market", ""),
            s.get("symbol", ""),
            s.get("status", ""),
            s.get("message", ""),
            s.get("started_at", ""),
            s.get("finished_at", ""),
        ])

    csv_text = out.getvalue()
    headers = {
        "Content-Disposition": f'attachment; filename="train-report-{job_id}.csv"'
    }
    return PlainTextResponse(content=csv_text, media_type="text/csv", headers=headers)


async def _train_async(
    job_id: str,
    market: Optional[str],
    symbol: Optional[str],
    timeframe: str,
    profile: str,
) -> None:
    import logging

    from config import settings
    from ml.trainer import ModelTrainer
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from db.models import BotConfig

    log = logging.getLogger("api.ml.train")
    trainer = ModelTrainer()

    if symbol and market:
        _mark_job_started(job_id, [(market, symbol)])
        # Treina símbolo específico
        log.info("[train] Iniciando %s/%s tf=%s", market, symbol, timeframe)
        _update_symbol(job_id, market, symbol, "running", "Coletando e treinando")
        try:
            engine = create_async_engine(settings.database_url)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            async with Session() as db_sym:
                trainer_sym = ModelTrainer(db=db_sym)
                train_result = await trainer_sym.train(
                    symbol=symbol,
                    timeframe=timeframe,
                    market=market,
                    profile=profile,
                )
            await engine.dispose()
            if train_result.approved:
                _update_symbol(job_id, market, symbol, "completed", "Modelo aprovado")
            else:
                status_value = "failed" if _is_didi_profile(profile) else "rejected"
                message = (
                    train_result.rejection_reason
                    or ("Treino não concluído no perfil didi" if status_value == "failed" else "Modelo rejeitado pelos critérios")
                )
                _update_symbol(
                    job_id,
                    market,
                    symbol,
                    status_value,
                    message,
                )
            log.info("[train] Concluído %s/%s", market, symbol)
        except Exception as exc:
            _update_symbol(job_id, market, symbol, "failed", str(exc))
            log.error("[train] Erro %s/%s: %s", market, symbol, exc, exc_info=True)
            _finish_job(job_id, failed=True, message="Falha durante o treinamento")
            _trim_jobs()
            return
        _finish_job(job_id)
        _trim_jobs()
        return

    # Treina todos os símbolos dos mercados ativos (ou do mercado especificado)
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        query = select(BotConfig)
        query = query.where(BotConfig.is_active == True)
        if market:
            query = query.where(BotConfig.market == market)
        result = await db.execute(query)
        configs = result.scalars().all()

    symbol_pairs: list[tuple[str, str]] = []
    for cfg in configs:
        for sym in (cfg.enabled_symbols or []):
            symbol_pairs.append((cfg.market, sym))
    _mark_job_started(job_id, symbol_pairs)

    # Usa uma nova sessão por símbolo para persistência
    for cfg in configs:
        for sym in (cfg.enabled_symbols or []):
            log.info("[train] Iniciando %s/%s tf=%s", cfg.market, sym, timeframe)
            _update_symbol(job_id, cfg.market, sym, "running", "Coletando e treinando")
            try:
                # Primeiro coleta dados
                if cfg.market in ("B3", "FOREX") and settings.mt5_files_dir:
                    from ml.mt5_data_collector import MT5DataCollector
                    collector = MT5DataCollector(mt5_files_dir=settings.mt5_files_dir)
                    await collector.update_incremental_async(sym, timeframe, market=cfg.market, n_bars=1500)
                elif cfg.market == "CRIPTO":
                    if sym.upper().endswith(".LV") and settings.mt5_files_dir:
                        from ml.mt5_data_collector import MT5DataCollector
                        collector = MT5DataCollector(mt5_files_dir=settings.mt5_files_dir)
                        await collector.update_incremental_async(sym, timeframe, market=cfg.market, n_bars=1500)
                    else:
                        from ml.data_collector import DataCollector
                        dc = DataCollector()
                        symbol_for_data = _map_mt5_crypto_to_binance(sym)
                        dc.update_incremental(symbol_for_data, timeframe)
                async with Session() as db_sym:
                    trainer_sym = ModelTrainer(db=db_sym)
                    train_result = await trainer_sym.train(
                        symbol=sym,
                        timeframe=timeframe,
                        market=cfg.market,
                        profile=profile,
                    )
                if train_result.approved:
                    _update_symbol(job_id, cfg.market, sym, "completed", "Modelo aprovado")
                else:
                    status_value = "failed" if _is_didi_profile(profile) else "rejected"
                    message = (
                        train_result.rejection_reason
                        or ("Treino não concluído no perfil didi" if status_value == "failed" else "Modelo rejeitado pelos critérios")
                    )
                    _update_symbol(
                        job_id,
                        cfg.market,
                        sym,
                        status_value,
                        message,
                    )
                log.info("[train] Concluído %s/%s", cfg.market, sym)
            except Exception as exc:
                _update_symbol(job_id, cfg.market, sym, "failed", str(exc))
                log.error("[train] Erro %s/%s: %s", cfg.market, sym, exc, exc_info=True)
    await engine.dispose()
    _finish_job(job_id)
    _trim_jobs()


async def _train_optimize_timeframes_async(
    job_id: str,
    market: Optional[str],
    symbol: Optional[str],
    timeframes: list[str],
    profile: str,
) -> None:
    import logging

    from config import settings
    from db.models import BotConfig
    from ml.data_collector import DataCollector
    from ml.mt5_data_collector import MT5DataCollector
    from ml.trainer import ModelTrainer
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    log = logging.getLogger("api.ml.optimize")
    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        # Resolve universo de símbolos
        if symbol and market:
            symbol_pairs = [(market, symbol)]
        else:
            async with Session() as db:
                query = select(BotConfig).where(BotConfig.is_active == True)
                if market:
                    query = query.where(BotConfig.market == market)
                result = await db.execute(query)
                configs = result.scalars().all()

            symbol_pairs: list[tuple[str, str]] = []
            for cfg in configs:
                for sym in (cfg.enabled_symbols or []):
                    symbol_pairs.append((cfg.market, sym))

        _mark_job_started(job_id, symbol_pairs)

        # Roda otimização por ativo
        for mkt, sym in symbol_pairs:
            candidates: list[dict] = []
            for idx, tf in enumerate(timeframes, start=1):
                _update_symbol(
                    job_id,
                    mkt,
                    sym,
                    "running",
                    f"Testando {tf} ({idx}/{len(timeframes)})",
                )
                log.info("[optimize] Testando %s/%s tf=%s", mkt, sym, tf)

                try:
                    # Preparação de dados (best effort) antes de treinar
                    if mkt in ("B3", "FOREX") and settings.mt5_files_dir:
                        collector = MT5DataCollector(mt5_files_dir=settings.mt5_files_dir)
                        await collector.update_incremental_async(sym, tf, market=mkt, n_bars=1500)
                    elif mkt == "CRIPTO":
                        if sym.upper().endswith(".LV") and settings.mt5_files_dir:
                            collector = MT5DataCollector(mt5_files_dir=settings.mt5_files_dir)
                            await collector.update_incremental_async(sym, tf, market=mkt, n_bars=1500)
                        else:
                            dc = DataCollector()
                            dc.update_incremental(_map_mt5_crypto_to_binance(sym), tf)

                    # Avalia sem persistir modelo intermediário
                    trainer_eval = ModelTrainer(db=None)
                    eval_res = await trainer_eval.train(
                        symbol=sym,
                        timeframe=tf,
                        market=mkt,
                        profile=profile,
                    )
                    candidates.append(
                        {
                            "timeframe": tf,
                            "approved": bool(eval_res.approved),
                            "profit_factor": float(eval_res.avg_profit_factor or 0.0),
                            "win_rate": float(eval_res.avg_win_rate or 0.0),
                            "total_trades": int(eval_res.total_trades or 0),
                            "rejection_reason": eval_res.rejection_reason,
                        }
                    )
                except Exception as exc:
                    candidates.append(
                        {
                            "timeframe": tf,
                            "approved": False,
                            "profit_factor": 0.0,
                            "win_rate": 0.0,
                            "total_trades": 0,
                            "rejection_reason": str(exc),
                        }
                    )

            candidates.sort(key=_score_timeframe_candidate, reverse=True)
            best = candidates[0] if candidates else None

            ranking_preview = " | ".join(
                [
                    (
                        f"{c['timeframe']}"
                        f"(PF={float(c.get('profit_factor', 0.0) or 0.0):.2f},"
                        f"WR={float(c.get('win_rate', 0.0) or 0.0):.1f}%,"
                        f"T={int(c.get('total_trades', 0) or 0)})"
                    )
                    for c in candidates[:3]
                ]
            )

            if not best or not best.get("approved"):
                top_reasons = "; ".join(
                    [f"{c['timeframe']}: {c.get('rejection_reason') or 'rejeitado'}" for c in candidates[:3]]
                )
                status_value = "failed" if _is_didi_profile(profile) else "rejected"
                _update_symbol(
                    job_id,
                    mkt,
                    sym,
                    status_value,
                    f"Sem timeframe aprovado. {top_reasons}",
                )
                continue

            # Treina e persiste apenas o melhor timeframe
            try:
                async with Session() as db_sym:
                    trainer_final = ModelTrainer(db=db_sym)
                    final = await trainer_final.train(
                        symbol=sym,
                        timeframe=best["timeframe"],
                        market=mkt,
                        profile=profile,
                    )
                if final.approved:
                    _update_symbol(
                        job_id,
                        mkt,
                        sym,
                        "completed",
                        (
                            f"Melhor TF={best['timeframe']} "
                            f"PF={best['profit_factor']:.2f} WR={best['win_rate']:.1f}% "
                            f"trades={best['total_trades']}"
                            f" | ranking: {ranking_preview}"
                        ),
                    )
                else:
                    status_value = "failed" if _is_didi_profile(profile) else "rejected"
                    _update_symbol(
                        job_id,
                        mkt,
                        sym,
                        status_value,
                        f"Melhor TF={best['timeframe']} reprovou no treino final: {final.rejection_reason}",
                    )
            except Exception as exc:
                _update_symbol(job_id, mkt, sym, "failed", str(exc))

    except Exception as exc:
        _finish_job(job_id, failed=True, message=f"Erro na otimização: {exc}")
        _trim_jobs()
        await engine.dispose()
        return

    await engine.dispose()
    _finish_job(job_id)
    _trim_jobs()
