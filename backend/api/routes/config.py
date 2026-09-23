from datetime import datetime, timezone
import json
import re
from pathlib import Path
import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import verify_token
from api.schemas.config import BotConfigPatch, BotConfigResponse
from database import get_db
from db.models import BotConfig, ConfigAuditLog

router = APIRouter(prefix="/config", tags=["config"])

VALID_MARKETS = {"CRIPTO", "B3", "FOREX"}
MARKET_WATCH_SYMBOLS_FILE_CANDIDATES = (
    "MarketWatchSymbols.txt",
    "market_watch_symbols.txt",
    "all_symbols.txt",
)
MARKET_WATCH_AUTOGEN_FILE = "MarketWatchSymbols.txt"


def _dwx_paths() -> tuple[Path | None, Path | None]:
    from config import settings

    if not settings.mt5_files_dir:
        return None, None
    dwx_dir = Path(settings.mt5_files_dir) / "DWX"
    return dwx_dir, dwx_dir / "DWX_Market_Data.txt"


def _read_dwx_market_symbols() -> list[str]:
    dwx_dir, market_file = _dwx_paths()
    if dwx_dir is None or market_file is None:
        return []
    if not market_file.exists():
        return []
    try:
        with open(market_file) as f:
            data = json.load(f)
        return sorted([s for s in data.keys() if isinstance(s, str) and s.strip()])
    except Exception:
        return []


def _read_market_watch_symbols_file() -> list[str]:
    dwx_dir, _ = _dwx_paths()
    if dwx_dir is None:
        return []

    for name in MARKET_WATCH_SYMBOLS_FILE_CANDIDATES:
        fpath = dwx_dir / name
        if not fpath.exists():
            continue
        try:
            raw = fpath.read_text(encoding="utf-8", errors="ignore")
            tokens = re.split(r"[\n,;\t ]+", raw)
            symbols = sorted({t.strip() for t in tokens if t.strip()})
            if symbols:
                return symbols
        except Exception:
            continue
    return []


def _sanitize_symbols(raw_symbols: list[str] | set[str]) -> list[str]:
    clean: set[str] = set()
    for sym in raw_symbols:
        s = (sym or "").strip()
        if not s:
            continue
        if s.upper() in {"ALL", "(NULL)", "NULL"}:
            continue
        if re.match(r"^[A-Za-z0-9._-]+$", s):
            clean.add(s)
    return sorted(clean)


def _collect_symbols_from_sources() -> list[str]:
    symbols: set[str] = set()

    # 1) Feed live atual
    symbols.update(_read_dwx_market_symbols())

    # 2) Arquivo já existente (se houver)
    symbols.update(_read_market_watch_symbols_file())

    # 3) Mensagens DWX
    dwx_dir, _ = _dwx_paths()
    if dwx_dir is not None:
        msg_file = dwx_dir / "DWX_Messages.txt"
        if msg_file.exists():
            try:
                with open(msg_file) as f:
                    msgs = json.load(f)
                for row in msgs.values():
                    text = " ".join(str(row.get(k, "")) for k in ("message", "description"))
                    if not text:
                        continue

                    m = re.search(r"subscribed to:\s*(.+)$", text, flags=re.IGNORECASE)
                    if m:
                        for part in m.group(1).split(","):
                            symbols.add(part.strip())

                    m = re.search(r"Could not subscribe to symbols:\s*(.+)$", text, flags=re.IGNORECASE)
                    if m:
                        for part in m.group(1).split(","):
                            symbols.add(part.strip())

                    for rgx in (
                        r"historic data for\s+([A-Za-z0-9._-]+)_",
                        r"select symbol\s+([A-Za-z0-9._-]+)\s+in market watch",
                    ):
                        for found in re.finditer(rgx, text, flags=re.IGNORECASE):
                            symbols.add(found.group(1).strip())
            except Exception:
                pass

    # 4) Estrutura de dados local (parquet)
    ml_dir = Path(__file__).parent.parent.parent / "ml" / "data"
    if ml_dir.exists():
        for market_dir in ml_dir.iterdir():
            if not market_dir.is_dir():
                continue
            for sym_dir in market_dir.iterdir():
                if sym_dir.is_dir():
                    symbols.add(sym_dir.name)

    return _sanitize_symbols(symbols)


def _write_market_watch_symbols_file(symbols: list[str]) -> Path | None:
    dwx_dir, _ = _dwx_paths()
    if dwx_dir is None:
        return None
    try:
        dwx_dir.mkdir(parents=True, exist_ok=True)
        path = dwx_dir / MARKET_WATCH_AUTOGEN_FILE
        path.write_text("\n".join(symbols) + "\n", encoding="utf-8")
        return path
    except Exception:
        return None


def _write_subscribe_symbols(symbols: list[str]) -> bool:
    dwx_dir, _ = _dwx_paths()
    if dwx_dir is None:
        return False

    symbols = sorted({s.strip() for s in symbols if s and s.strip()})
    if not symbols:
        return False

    cmd = f"<:{int(time.time()) % 99000}|SUBSCRIBE_SYMBOLS|{','.join(symbols)}:>"

    # Tenta usar um slot de comando livre.
    for i in range(50):
        cmd_path = dwx_dir / f"DWX_Commands_{i}.txt"
        if cmd_path.exists():
            continue
        try:
            with open(cmd_path, "w") as f:
                f.write(cmd)
            return True
        except Exception:
            continue
    return False


def _wait_market_data_refresh(timeout_s: int = 8) -> list[str]:
    _, market_file = _dwx_paths()
    if market_file is None:
        return []

    start = time.time()
    while time.time() - start < timeout_s:
        syms = _read_dwx_market_symbols()
        if syms:
            return syms
        time.sleep(0.4)
    return _read_dwx_market_symbols()


CRYPTO_BASE_ASSETS = (
    "BTC", "ETH", "LTC", "BCH", "XRP", "ADA", "SOL", "DOT", "DOGE",
    "AVAX", "LINK", "MATIC", "BNB", "TRX", "UNI", "ATOM", "XLM", "ETC",
)


def _classify_market(symbol: str) -> str | None:
    sym = symbol.strip()
    if not sym:
        return None

    # O sufixo varia por conta/servidor (.lv na Live, .x na Demo), então a
    # classificacao de cripto usa o ativo base do par.
    base = re.sub(r"\.(lv|pr|x|raw|ecn|m|c|pro)$", "", sym, flags=re.IGNORECASE).upper()
    if base.startswith(CRYPTO_BASE_ASSETS):
        return "CRIPTO"

    if sym.endswith(".lv"):
        return "CRIPTO"
    if sym.endswith(".pr"):
        return "FOREX"
    # Fallback simples para ativos B3 (ex.: PETR4, VALE3, WINQ26)
    if re.match(r"^[A-Z]{4}\d+[A-Z0-9]*$", sym):
        return "B3"
    # Muitos CFDs de ações/índices vêm sem sufixo no Equiti; por ora
    # agrupamos no mercado FOREX para manter esses ativos importáveis.
    if re.match(r"^[A-Za-z][A-Za-z0-9._-]{1,}$", sym):
        return "FOREX"
    return None


def _discover_symbols_for_market(market: str) -> list[str]:
    from config import settings

    collected: set[str] = set()

    # 1) Símbolos já presentes em parquets locais
    ml_market_dir = Path(__file__).parent.parent.parent / "ml" / "data" / market
    if ml_market_dir.exists():
        for d in ml_market_dir.iterdir():
            if d.is_dir() and any(d.glob("*.parquet")):
                collected.add(d.name)

    if settings.mt5_files_dir:
        dwx_dir = Path(settings.mt5_files_dir) / "DWX"

        # 2) Feed live atual
        market_file = dwx_dir / "DWX_Market_Data.txt"
        if market_file.exists():
            try:
                with open(market_file) as f:
                    data = json.load(f)
                for sym in data.keys():
                    if _classify_market(sym) == market:
                        collected.add(sym)
            except Exception:
                pass

        # 3) Histórico de mensagens DWX (subscribed/historic/select symbol)
        msg_file = dwx_dir / "DWX_Messages.txt"
        if msg_file.exists():
            try:
                with open(msg_file) as f:
                    msgs = json.load(f)

                for row in msgs.values():
                    text = " ".join(
                        str(row.get(k, "")) for k in ("message", "description")
                    )
                    if not text:
                        continue

                    # Caso "Successfully subscribed to: ..."
                    sub_match = re.search(r"subscribed to:\s*(.+)$", text, flags=re.IGNORECASE)
                    if sub_match:
                        for part in sub_match.group(1).split(","):
                            sym = part.strip()
                            if _classify_market(sym) == market:
                                collected.add(sym)

                    # Casos "historic data for SYMBOL_TF" e "select symbol SYMBOL"
                    for rgx in (
                        r"historic data for\s+([A-Za-z0-9._-]+)_",
                        r"select symbol\s+([A-Za-z0-9._-]+)\s+in market watch",
                    ):
                        for m in re.finditer(rgx, text, flags=re.IGNORECASE):
                            sym = m.group(1).strip()
                            if _classify_market(sym) == market:
                                collected.add(sym)
            except Exception:
                pass

    return sorted(collected)


def _get_market_or_404(market: str) -> str:
    market_upper = market.upper()
    if market_upper not in VALID_MARKETS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Mercado '{market}' não encontrado. Válidos: {sorted(VALID_MARKETS)}",
        )
    return market_upper


class RemoveSymbolRequest(BaseModel):
    market: str
    symbol: str
    from_all_markets: bool = False


@router.get("/{market}", response_model=BotConfigResponse)
async def get_config(
    market: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> BotConfig:
    market = _get_market_or_404(market)
    result = await db.execute(select(BotConfig).where(BotConfig.market == market))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config não encontrada.")
    return cfg


@router.patch("/{market}", response_model=BotConfigResponse)
async def patch_config(
    market: str,
    payload: BotConfigPatch,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> BotConfig:
    market = _get_market_or_404(market)
    result = await db.execute(select(BotConfig).where(BotConfig.market == market))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config não encontrada.")

    changes = payload.model_dump(exclude_none=True)
    for field, new_val in changes.items():
        old_val = getattr(cfg, field)
        setattr(cfg, field, new_val)
        db.add(
            ConfigAuditLog(
                market=market,
                field_name=field,
                old_value=str(old_val),
                new_value=str(new_val),
            )
        )

    await db.commit()
    await db.refresh(cfg)
    return cfg


@router.post("/{market}/pause", response_model=dict)
async def pause_bot(
    market: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> dict:
    market = _get_market_or_404(market)
    result = await db.execute(select(BotConfig).where(BotConfig.market == market))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config não encontrada.")

    cfg.is_active = False
    cfg.paused_reason = "manual"
    cfg.paused_at = datetime.now(timezone.utc)
    db.add(ConfigAuditLog(market=market, field_name="is_active", old_value="True", new_value="False"))
    await db.commit()
    # Notifica Telegram
    try:
        from notifications.telegram import notifier
        from api.websocket import connection_manager
        import json
        await notifier.send_message(f"⏸ <b>Bot {market} pausado manualmente</b>")
        await connection_manager.broadcast(json.dumps({"type": "BOT_STATUS_CHANGED", "payload": {"market": market, "status": "PAUSADO"}}))
    except Exception:
        pass
    return {"status": "paused"}


@router.get("/audit-log", response_model=list[dict])
async def get_audit_log(
    market: str = "CRIPTO",
    limit: int = 10,
    _: str = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Últimas N entradas do log de auditoria de configuração."""
    from sqlalchemy import select
    result = await db.execute(
        select(ConfigAuditLog)
        .where(ConfigAuditLog.market == market.upper())
        .order_by(ConfigAuditLog.changed_at.desc())
        .limit(limit)
    )
    logs = result.scalars().all()
    return [
        {
            "id": log.id,
            "market": log.market,
            "field": log.field_name,
            "old_value": log.old_value,
            "new_value": log.new_value,
            "changed_at": log.changed_at.isoformat(),
        }
        for log in logs
    ]


@router.post("/{market}/resume", response_model=dict)
async def resume_bot(
    market: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> dict:
    market = _get_market_or_404(market)
    result = await db.execute(select(BotConfig).where(BotConfig.market == market))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config não encontrada.")

    cfg.is_active = True
    cfg.paused_reason = None
    cfg.paused_at = None
    db.add(ConfigAuditLog(market=market, field_name="is_active", old_value="False", new_value="True"))
    await db.commit()
    # Notifica Telegram
    try:
        from notifications.telegram import notifier
        from api.websocket import connection_manager
        import json
        await notifier.send_message(f"▶️ <b>Bot {market} retomado manualmente</b>")
        await connection_manager.broadcast(json.dumps({"type": "BOT_STATUS_CHANGED", "payload": {"market": market, "status": "ATIVO"}}))
    except Exception:
        pass
    return {"status": "active"}


@router.post("/symbols/remove", response_model=dict)
async def remove_symbol_from_system(
    body: RemoveSymbolRequest,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> dict:
    symbol = (body.symbol or "").strip()
    if not symbol:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Símbolo inválido.",
        )

    target_market = _get_market_or_404(body.market)

    result = await db.execute(select(BotConfig))
    configs = result.scalars().all()
    if not configs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nenhuma config encontrada.")

    removed_from: list[str] = []
    skipped: list[str] = []

    for cfg in configs:
        if not body.from_all_markets and cfg.market != target_market:
            continue

        current = list(cfg.enabled_symbols or [])
        if symbol not in current:
            skipped.append(cfg.market)
            continue

        updated = [s for s in current if s != symbol]
        cfg.enabled_symbols = updated
        removed_from.append(cfg.market)
        db.add(
            ConfigAuditLog(
                market=cfg.market,
                field_name="enabled_symbols",
                old_value=",".join(current),
                new_value=",".join(updated),
            )
        )

    await db.commit()

    return {
        "status": "ok",
        "symbol": symbol,
        "requested_market": target_market,
        "from_all_markets": body.from_all_markets,
        "removed_from": removed_from,
        "skipped": skipped,
    }


@router.post("/{market}/import-symbols", response_model=dict)
async def import_symbols(
    market: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> dict:
    market = _get_market_or_404(market)
    result = await db.execute(select(BotConfig).where(BotConfig.market == market))
    cfg = result.scalar_one_or_none()
    if cfg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Config não encontrada.")

    discovered = _discover_symbols_for_market(market)
    if not discovered:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Nenhum símbolo encontrado para importar. "
                "Verifique feed MT5/DWX ativo ou dados locais em ml/data."
            ),
        )

    old_symbols = list(cfg.enabled_symbols or [])
    cfg.enabled_symbols = discovered
    db.add(
        ConfigAuditLog(
            market=market,
            field_name="enabled_symbols",
            old_value=",".join(old_symbols),
            new_value=",".join(discovered),
        )
    )
    await db.commit()

    return {
        "status": "ok",
        "market": market,
        "count": len(discovered),
        "symbols": discovered,
    }


@router.post("/import-symbols/all", response_model=dict)
async def import_symbols_all_markets(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> dict:
    result = await db.execute(select(BotConfig))
    configs = result.scalars().all()
    if not configs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nenhuma config encontrada.")

    imported: dict[str, dict] = {}
    for cfg in configs:
        discovered = _discover_symbols_for_market(cfg.market)
        if not discovered:
            imported[cfg.market] = {"count": 0, "symbols": []}
            continue
        old_symbols = list(cfg.enabled_symbols or [])
        cfg.enabled_symbols = discovered
        db.add(
            ConfigAuditLog(
                market=cfg.market,
                field_name="enabled_symbols",
                old_value=",".join(old_symbols),
                new_value=",".join(discovered),
            )
        )
        imported[cfg.market] = {"count": len(discovered), "symbols": discovered}

    await db.commit()
    return {"status": "ok", "imported": imported}


@router.post("/import-symbols/market-watch", response_model=dict)
async def import_symbols_from_market_watch(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> dict:
    """Sincroniza símbolos visíveis do feed DWX e importa para todos os mercados."""
    result = await db.execute(select(BotConfig))
    configs = result.scalars().all()
    if not configs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nenhuma config encontrada.")

    # Base de símbolos candidata: feed atual + configs já salvas.
    base_symbols = set(_read_dwx_market_symbols())
    file_symbols = _read_market_watch_symbols_file()
    base_symbols.update(file_symbols)
    for cfg in configs:
        base_symbols.update(cfg.enabled_symbols or [])

    # Força o DWX a publicar os símbolos candidatos e aguarda refresh.
    subscribe_sent = _write_subscribe_symbols(sorted(base_symbols)) if base_symbols else False
    refreshed_symbols = _wait_market_data_refresh(timeout_s=8)
    preview_limit = 120
    feed_symbols_preview = refreshed_symbols[:preview_limit]
    omitted = max(len(refreshed_symbols) - preview_limit, 0)

    imported: dict[str, dict] = {}
    for cfg in configs:
        discovered = _discover_symbols_for_market(cfg.market)
        if not discovered:
            imported[cfg.market] = {"count": 0, "symbols": []}
            continue
        old_symbols = list(cfg.enabled_symbols or [])
        cfg.enabled_symbols = discovered
        db.add(
            ConfigAuditLog(
                market=cfg.market,
                field_name="enabled_symbols",
                old_value=",".join(old_symbols),
                new_value=",".join(discovered),
            )
        )
        imported[cfg.market] = {"count": len(discovered), "symbols": discovered}

    await db.commit()
    return {
        "status": "ok",
        "subscribe_sent": subscribe_sent,
        "file_symbols_count": len(file_symbols),
        "feed_symbols_count": len(refreshed_symbols),
        "feed_symbols": feed_symbols_preview,
        "feed_symbols_omitted": omitted,
        "imported": imported,
    }


@router.post("/import-symbols/from-file", response_model=dict)
async def import_symbols_from_file(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> dict:
    """Importa símbolos usando arquivo de apoio no share DWX.

    Arquivos aceitos em /DWX:
    - MarketWatchSymbols.txt
    - market_watch_symbols.txt
    - all_symbols.txt
    """
    symbols = _read_market_watch_symbols_file()
    if not symbols:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Arquivo de símbolos não encontrado/vazio em DWX. "
                "Use um dos nomes: MarketWatchSymbols.txt, market_watch_symbols.txt, all_symbols.txt"
            ),
        )

    _write_subscribe_symbols(symbols)
    _wait_market_data_refresh(timeout_s=8)

    result = await db.execute(select(BotConfig))
    configs = result.scalars().all()
    if not configs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nenhuma config encontrada.")

    imported: dict[str, dict] = {}
    for cfg in configs:
        discovered = _discover_symbols_for_market(cfg.market)
        if not discovered:
            imported[cfg.market] = {"count": 0, "symbols": []}
            continue
        old_symbols = list(cfg.enabled_symbols or [])
        cfg.enabled_symbols = discovered
        db.add(
            ConfigAuditLog(
                market=cfg.market,
                field_name="enabled_symbols",
                old_value=",".join(old_symbols),
                new_value=",".join(discovered),
            )
        )
        imported[cfg.market] = {"count": len(discovered), "symbols": discovered}

    await db.commit()
    return {
        "status": "ok",
        "file_symbols_count": len(symbols),
        "imported": imported,
    }


@router.post("/import-symbols/autogen-file-and-import", response_model=dict)
async def autogen_file_and_import_symbols(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> dict:
    """Gera/regenera arquivo de símbolos automaticamente e importa em 1 passo."""
    result = await db.execute(select(BotConfig))
    configs = result.scalars().all()
    if not configs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nenhuma config encontrada.")

    symbols = set(_collect_symbols_from_sources())
    for cfg in configs:
        symbols.update(cfg.enabled_symbols or [])
    final_symbols = _sanitize_symbols(symbols)

    if not final_symbols:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Nenhum símbolo descoberto automaticamente para gerar o arquivo.",
        )

    generated_file = _write_market_watch_symbols_file(final_symbols)
    if generated_file is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Falha ao escrever arquivo de símbolos no diretório DWX.",
        )

    subscribe_sent = _write_subscribe_symbols(final_symbols)
    refreshed_symbols = _wait_market_data_refresh(timeout_s=8)

    imported: dict[str, dict] = {}
    for cfg in configs:
        discovered = _discover_symbols_for_market(cfg.market)
        if not discovered:
            imported[cfg.market] = {"count": 0, "symbols": []}
            continue

        old_symbols = list(cfg.enabled_symbols or [])
        cfg.enabled_symbols = discovered
        db.add(
            ConfigAuditLog(
                market=cfg.market,
                field_name="enabled_symbols",
                old_value=",".join(old_symbols),
                new_value=",".join(discovered),
            )
        )
        imported[cfg.market] = {"count": len(discovered), "symbols": discovered}

    await db.commit()
    return {
        "status": "ok",
        "generated_file": str(generated_file),
        "generated_symbols_count": len(final_symbols),
        "subscribe_sent": subscribe_sent,
        "feed_symbols_count": len(refreshed_symbols),
        "imported": imported,
    }
