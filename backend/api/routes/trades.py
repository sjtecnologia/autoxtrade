"""Endpoints de histórico de trades e métricas de performance."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import verify_token
from database import get_db
from db.models import Trade

router = APIRouter(prefix="/trades", tags=["trades"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TradeSummary(BaseModel):
    id: int
    market: str
    symbol: str
    side: str
    mode: str
    entry_price: Decimal
    exit_price: Optional[Decimal]
    quantity: Decimal
    pnl_gross: Optional[Decimal]
    pnl_net: Optional[Decimal]
    pnl_pct: Optional[Decimal]
    open_at: str
    close_at: Optional[str]
    close_reason: Optional[str]
    duration_sec: Optional[int]
    ml_signal: Optional[str]
    ml_confidence: Optional[Decimal]


class TradeHistoryResponse(BaseModel):
    trades: list[TradeSummary]
    total: int
    page: int
    pages: int


class DailyMetrics(BaseModel):
    day_pnl: Decimal
    trades_count: int
    win_rate: Decimal


class PerformanceMetrics(BaseModel):
    total_pnl: Decimal
    trades_count: int
    win_rate: Decimal
    profit_factor: Decimal
    avg_pnl: Decimal
    avg_duration_sec: Optional[float]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/history", response_model=TradeHistoryResponse)
async def get_trade_history(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    market: Optional[str] = Query(None),
    result: Optional[str] = Query(None, description="profit | loss"),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    _: str = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
) -> TradeHistoryResponse:
    """Histórico paginado de operações encerradas."""
    filters = [Trade.status == "closed"]

    if market:
        filters.append(Trade.market == market.upper())
    if start_date:
        filters.append(Trade.close_at >= start_date)
    if end_date:
        filters.append(Trade.close_at <= end_date)
    if result == "profit":
        filters.append(Trade.pnl_net > 0)
    elif result == "loss":
        filters.append(Trade.pnl_net <= 0)

    # Total
    count_q = await db.execute(select(func.count()).select_from(Trade).where(and_(*filters)))
    total = count_q.scalar_one()

    # Página
    offset = (page - 1) * per_page
    q = (
        select(Trade)
        .where(and_(*filters))
        .order_by(Trade.close_at.desc())
        .offset(offset)
        .limit(per_page)
    )
    result_rows = await db.execute(q)
    trades = result_rows.scalars().all()

    def _trade_summary(t: Trade) -> TradeSummary:
        return TradeSummary(
            id=t.id,
            market=t.market,
            symbol=t.symbol,
            side=t.side,
            mode=t.mode,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            quantity=t.quantity,
            pnl_gross=t.pnl_gross,
            pnl_net=t.pnl_net,
            pnl_pct=t.pnl_pct,
            open_at=t.open_at.isoformat(),
            close_at=t.close_at.isoformat() if t.close_at else None,
            close_reason=t.close_reason,
            duration_sec=t.duration_sec,
            ml_signal=t.ml_signal,
            ml_confidence=t.ml_confidence,
        )

    return TradeHistoryResponse(
        trades=[_trade_summary(t) for t in trades],
        total=total,
        page=page,
        pages=max(1, math.ceil(total / per_page)),
    )


@router.get("/stats/daily", response_model=DailyMetrics)
async def get_daily_stats(
    market: Optional[str] = Query(None),
    _: str = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
) -> DailyMetrics:
    """P&L, contagem e win rate do dia corrente."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    filters = [Trade.status == "closed", Trade.close_at >= today_start]
    if market:
        filters.append(Trade.market == market.upper())

    q = await db.execute(select(Trade).where(and_(*filters)))
    trades = q.scalars().all()

    day_pnl = sum((t.pnl_net or Decimal("0")) for t in trades)
    count = len(trades)
    winners = sum(1 for t in trades if (t.pnl_net or Decimal("0")) > 0)
    win_rate = Decimal(str(round(winners / count * 100, 2))) if count > 0 else Decimal("0")

    return DailyMetrics(
        day_pnl=Decimal(str(day_pnl)),
        trades_count=count,
        win_rate=win_rate,
    )


@router.get("/stats/performance", response_model=PerformanceMetrics)
async def get_performance_metrics(
    market: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    _: str = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
) -> PerformanceMetrics:
    """Métricas de performance consolidadas."""
    filters = [Trade.status == "closed"]
    if market:
        filters.append(Trade.market == market.upper())
    if start_date:
        filters.append(Trade.close_at >= start_date)
    if end_date:
        filters.append(Trade.close_at <= end_date)

    q = await db.execute(select(Trade).where(and_(*filters)))
    trades = q.scalars().all()

    if not trades:
        return PerformanceMetrics(
            total_pnl=Decimal("0"),
            trades_count=0,
            win_rate=Decimal("0"),
            profit_factor=Decimal("0"),
            avg_pnl=Decimal("0"),
            avg_duration_sec=None,
        )

    pnls = [(t.pnl_net or Decimal("0")) for t in trades]
    total_pnl = sum(pnls)
    count = len(pnls)
    winners = sum(1 for p in pnls if p > 0)
    win_rate = Decimal(str(round(winners / count * 100, 2)))

    gains = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    if losses == 0:
        pf = Decimal("999.99")  # só vencedores
    else:
        pf = Decimal(str(round(float(gains / losses), 4)))

    avg_pnl = Decimal(str(round(float(total_pnl) / count, 2)))

    durations = [t.duration_sec for t in trades if t.duration_sec is not None]
    avg_dur = round(sum(durations) / len(durations), 1) if durations else None

    return PerformanceMetrics(
        total_pnl=Decimal(str(total_pnl)),
        trades_count=count,
        win_rate=win_rate,
        profit_factor=pf,
        avg_pnl=avg_pnl,
        avg_duration_sec=avg_dur,
    )
