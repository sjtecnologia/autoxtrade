"""Paper trader: simula execução de ordens sem enviar para a exchange real."""
import logging
from decimal import Decimal

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Trade, TradeEvent
from trading.connectors.types import OrderSide
from trading.contracts import ExecutionContext, ExecutionMode
from trading.safety import ExposureBlocked

logger = logging.getLogger(__name__)

# Spread simulado (0.1%)
SIMULATED_SPREAD = Decimal("0.001")


class PaperTrader:
    """Gerencia trades simulados monitorando preços via Redis."""

    def __init__(self, db: AsyncSession, market: str = "CRIPTO", *, context: ExecutionContext | None = None) -> None:
        self._db = db
        self._market = market
        self._redis: aioredis.Redis | None = None
        self._context = context

    async def startup(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def shutdown(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def open_position(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal | None,
        model_version: str | None = None,
        ml_confidence: Decimal | None = None,
    ) -> Trade:
        """Cria um trade simulado no banco."""
        context = self._context
        if type(context) is not ExecutionContext:
            raise ExposureBlocked("Paper opening requires server-side execution identity")
        context.validate()
        if context.mode != ExecutionMode.PAPER or context.market != self._market or context.instrument.symbol != symbol:
            raise ExposureBlocked("Paper request conflicts with bound identity")
        context.instrument.validate(quantity, entry_price)
        spread = entry_price * SIMULATED_SPREAD
        simulated_entry = (
            entry_price + spread if side == OrderSide.BUY else entry_price - spread
        )
        exchange = context.venue
        trade = Trade(
            market=self._market,
            exchange=exchange,
            symbol=symbol,
            side=side.value,
            status="open",
            mode="paper",
            account_id=context.account_id,
            account_nature=context.nature.value,
            instrument_class=context.instrument.asset_class,
            execution_context=context.snapshot(),
            identity_status="bound",
            entry_price=simulated_entry,
            quantity=quantity,
            entry_value=(simulated_entry * quantity).quantize(Decimal("0.01")),
            stop_loss=stop_loss,
            take_profit=take_profit,
            open_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            model_version=model_version,
            ml_confidence=ml_confidence,
        )
        self._db.add(trade)
        self._db.add(
            TradeEvent(
                trade=trade,
                event_type="ORDER_SENT",
                payload={"mode": "paper", "symbol": symbol, "side": side.value},
            )
        )
        await self._db.commit()
        await self._db.refresh(trade)
        logger.info("[paper] Posição aberta: %s %s @ %s", side.value.upper(), symbol, simulated_entry)
        return trade

    async def check_stops(self, trade: Trade) -> None:
        """Verifica se TP ou SL foram atingidos para um trade paper aberto."""
        if not self._redis:
            return
        cached = await self._redis.get(f"price:{trade.symbol}")
        if not cached:
            return
        current_price = Decimal(cached)
        side = trade.side
        sl = trade.stop_loss
        tp = trade.take_profit

        hit_sl = (side == "buy" and current_price <= sl) or (side == "sell" and current_price >= sl)
        hit_tp = tp and (
            (side == "buy" and current_price >= tp) or (side == "sell" and current_price <= tp)
        )

        if hit_tp or hit_sl:
            reason = "TP_HIT" if hit_tp else "SL_HIT"
            exit_price = (tp if hit_tp else sl) or current_price
            await self._close_position(trade, exit_price, reason)

    async def _close_position(self, trade: Trade, exit_price: Decimal, reason: str) -> None:
        import datetime

        qty = trade.quantity
        entry = trade.entry_price
        side_mult = Decimal("1") if trade.side == "buy" else Decimal("-1")
        spread = exit_price * SIMULATED_SPREAD
        simulated_exit = exit_price - spread if trade.side == "buy" else exit_price + spread

        pnl_gross = (simulated_exit - entry) * qty * side_mult
        commission = (entry * qty + simulated_exit * qty) * Decimal("0.001")
        pnl_net = pnl_gross - commission
        pnl_pct = pnl_net / trade.entry_value * 100

        trade.status = "closed"
        trade.exit_price = simulated_exit
        trade.exit_value = (simulated_exit * qty).quantize(Decimal("0.01"))
        trade.close_at = datetime.datetime.now(datetime.timezone.utc)
        trade.close_reason = reason
        trade.pnl_gross = pnl_gross.quantize(Decimal("0.01"))
        trade.commission = commission.quantize(Decimal("0.01"))
        trade.pnl_net = pnl_net.quantize(Decimal("0.01"))
        trade.pnl_pct = pnl_pct.quantize(Decimal("0.0001"))
        trade.duration_sec = int(
            (trade.close_at - trade.open_at).total_seconds()
        )
        self._db.add(
            TradeEvent(
                trade=trade,
                event_type=reason,
                payload={"exit_price": str(simulated_exit), "pnl_net": str(pnl_net)},
            )
        )
        await self._db.commit()
        logger.info("[paper] Posição fechada: %s via %s @ %s | PnL: %s", trade.symbol, reason, simulated_exit, pnl_net)
