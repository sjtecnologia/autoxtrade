import json
import logging
from decimal import Decimal

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import verify_token
from api.schemas.trade import BalanceAsset, PositionSchema
from config import settings
from database import get_db
from db.models import Trade

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("/balance", response_model=list[BalanceAsset])
async def get_balance(
    _: str = Depends(verify_token),
) -> list[BalanceAsset]:
    """Retorna saldo da exchange (do cache Redis ou placeholder)."""
    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        cached = await r.get("balance:binance")
        if cached:
            data = json.loads(cached)
            return [
                BalanceAsset(
                    asset=k,
                    free=Decimal(str(v["free"])),
                    locked=Decimal(str(v["locked"])),
                    total=Decimal(str(v["total"])),
                )
                for k, v in data.items()
            ]
    finally:
        await r.aclose()

    # Sem cache: retorna vazio (balance é populado pelo connector em runtime)
    return []


@router.get("", response_model=list[PositionSchema])
async def get_positions(
    db: AsyncSession = Depends(get_db),
    _: str = Depends(verify_token),
) -> list[PositionSchema]:
    """Retorna posições abertas enriquecidas com preço atual do Redis."""
    result = await db.execute(
        select(Trade).where(Trade.status == "open").order_by(Trade.open_at.desc())
    )
    trades = result.scalars().all()

    r = aioredis.from_url(settings.redis_url, decode_responses=True)
    positions: list[PositionSchema] = []
    try:
        for trade in trades:
            cached_price = await r.get(f"price:{trade.symbol}")
            current_price = Decimal(cached_price) if cached_price else None

            unrealized_pnl = None
            unrealized_pnl_pct = None
            if current_price:
                side_mult = Decimal("1") if trade.side == "buy" else Decimal("-1")
                unrealized_pnl = (current_price - trade.entry_price) * trade.quantity * side_mult
                unrealized_pnl_pct = (unrealized_pnl / trade.entry_value * 100).quantize(
                    Decimal("0.01")
                )

            positions.append(
                PositionSchema(
                    id=trade.id,
                    symbol=trade.symbol,
                    side=trade.side,
                    mode=trade.mode,
                    entry_price=trade.entry_price,
                    quantity=trade.quantity,
                    stop_loss=trade.stop_loss,
                    take_profit=trade.take_profit,
                    current_price=current_price,
                    unrealized_pnl=unrealized_pnl,
                    unrealized_pnl_pct=unrealized_pnl_pct,
                    open_at=trade.open_at.isoformat(),
                    model_version=trade.model_version,
                    ml_confidence=trade.ml_confidence,
                )
            )
    finally:
        await r.aclose()

    return positions
