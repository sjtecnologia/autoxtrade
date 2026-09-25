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

    # Resolve preço de entrada
    if entry_price is None:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
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
        capital = await sizer.get_available_capital("USDT")
        if capital <= Decimal("0"):
            # Demo: usa 10.000 USDT quando não há saldo (sem connector)
            capital = Decimal("10000")

        try:
            result = sizer.calculate(
                capital=capital,
                risk_pct=risk_pct,
                entry_price=entry_price,
                stop_loss_price=stop_loss_price,
                step_size=step_size,
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
