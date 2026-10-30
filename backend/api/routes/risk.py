"""Endpoints de risco: position sizing e drawdown."""
import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import verify_token
from database import get_db
from risk.drawdown_monitor import DrawdownMonitor, DrawdownResult
from risk.position_sizer import PositionSizer, PositionSizingError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/risk", tags=["risk"])


class SizingResponse(BaseModel):
    symbol: str
    quantity: Decimal
    risk_amount: Decimal
    risk_pct: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    stop_distance_pct: Decimal
    position_value: Decimal


class DrawdownResponse(BaseModel):
    market: str
    dd_1d: Decimal
    dd_30d: Decimal
    peak_capital: Decimal
    current_equity: Decimal
    status: str


@router.get("/sizing", response_model=SizingResponse)
async def get_position_sizing(
    symbol: str = Query(..., description="Par de trading, ex: BTC/USDT"),
    stop_pct: Decimal = Query(..., description="% do stop loss em relação à entrada"),
    entry_price: Decimal | None = Query(None, description="Preço de entrada (usa Redis se omitido)"),
    risk_pct: Decimal = Query(Decimal("1.0"), description="% do capital em risco (padrão 1%)"),
    _: str = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
) -> SizingResponse:
    """Simula tamanho de posição para um símbolo e stop percentual dados."""
    import redis.asyncio as aioredis

    from config import settings
    from db.models import BotConfig
    from trading.contracts import context_from_dict

    # Resolve o instrumento vinculado para não dimensionar FX/B3 como spot.
    contract_size = Decimal("1")
    quote_asset = "USDT"
    matched_market: str | None = None
    matched_mode: str | None = None
    configs = (await db.execute(select(BotConfig))).scalars().all()
    for config in configs:
        bindings = getattr(config, "execution_context", None) or {}
        if not isinstance(bindings, dict):
            continue
        raw = bindings.get(symbol)
        if raw is None and bindings.get("instrument", {}).get("symbol") == symbol:
            raw = bindings
        if raw is None:
            continue
        try:
            context = context_from_dict(raw)
        except Exception:
            continue
        contract_size = context.instrument.contract_size or Decimal("1")
        quote_asset = context.instrument.quote_currency or quote_asset
        matched_market = config.market
        matched_mode = config.mode
        break

    # Resolve preço de entrada
    if entry_price is None:
        from trading.market_feed import MarketDataUnavailable, latest_mark_price

        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            if matched_market:
                try:
                    entry_price = await latest_mark_price(
                        r, symbol, matched_market, settings
                    )
                except MarketDataUnavailable:
                    entry_price = None
            else:
                cached = await r.get(f"price:{symbol}")
                entry_price = Decimal(cached) if cached else None
        finally:
            await r.aclose()

    if entry_price is None or entry_price <= 0:
        raise HTTPException(status_code=422, detail="Preço de entrada não disponível. Informe entry_price.")

    stop_loss_price = entry_price * (1 - stop_pct / 100)

    sizer = PositionSizer()
    await sizer.startup()
    try:
        step_size = await sizer.get_step_size(symbol)
        if matched_mode == "paper":
            from trading.equity import paper_available_capital

            capital = await paper_available_capital(db, matched_market or market)
        else:
            capital = await sizer.get_available_capital(quote_asset)
        if capital <= Decimal("0"):
            raise HTTPException(
                status_code=503,
                detail=f"Capital indisponível para {quote_asset}; sizing bloqueado.",
            )

        try:
            result = sizer.calculate(
                capital=capital,
                risk_pct=risk_pct,
                entry_price=entry_price,
                stop_loss_price=stop_loss_price,
                step_size=step_size,
                contract_size=contract_size,
            )
        except PositionSizingError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    finally:
        await sizer.shutdown()

    return SizingResponse(
        symbol=symbol,
        quantity=result.quantity,
        risk_amount=result.risk_amount,
        risk_pct=result.risk_pct,
        entry_price=result.entry_price,
        stop_loss_price=result.stop_loss_price,
        stop_distance_pct=result.stop_distance_pct,
        position_value=result.position_value,
    )


@router.get("/drawdown", response_model=DrawdownResponse)
async def get_drawdown(
    market: str = Query("CRIPTO", description="Mercado: CRIPTO ou B3"),
    _: str = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
) -> DrawdownResponse:
    """Retorna drawdown atual (1d e 30d) e status de risco do mercado."""
    monitor = DrawdownMonitor(db)
    await monitor.startup()
    try:
        result = await monitor.calculate_drawdown(market)
    finally:
        await monitor.shutdown()

    return DrawdownResponse(
        market=market,
        dd_1d=result.dd_1d,
        dd_30d=result.dd_30d,
        peak_capital=result.peak_capital,
        current_equity=result.current_equity,
        status=result.status,
    )


class EquityPoint(BaseModel):
    timestamp: str
    equity: Decimal
    drawdown_pct: Decimal


@router.get("/equity-history", response_model=list[EquityPoint])
async def get_equity_history(
    market: str = Query("CRIPTO"),
    hours: int = Query(24, ge=1, le=720),
    _: str = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
) -> list[EquityPoint]:
    """Retorna histórico de equity (snapshots) para o período."""
    from datetime import timedelta

    from sqlalchemy import and_

    from db.models import EquitySnapshot

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = await db.execute(
        select(EquitySnapshot)
        .where(and_(EquitySnapshot.market == market.upper(), EquitySnapshot.snapshot_at >= since))
        .order_by(EquitySnapshot.snapshot_at.asc())
    )
    snaps = q.scalars().all()
    return [
        EquityPoint(
            timestamp=s.snapshot_at.isoformat(),
            equity=s.equity,
            drawdown_pct=s.drawdown_30d or Decimal("0"),
        )
        for s in snaps
    ]
