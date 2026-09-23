"""Monitor de ordens: detecta execuções via WebSocket e verifica órfãos no startup."""
import logging
from decimal import Decimal

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Trade, TradeEvent
from trading.connectors.base import BaseConnector
from trading.connectors.types import OrderSide, OrderStatus, OrderType
from uuid import uuid4

logger = logging.getLogger(__name__)


class OrderMonitor:
    def __init__(self, connector: BaseConnector, db: AsyncSession) -> None:
        self._conn = connector
        self._db = db
        self._redis: aioredis.Redis | None = None

    async def startup(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        await self.check_orphan_positions()

    async def shutdown(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def check_orphan_positions(self) -> None:
        """Verifica trades "open" sem OCO correspondente na exchange e resolve."""
        result = await self._db.execute(
            select(Trade).where(Trade.status == "open", Trade.mode == "live")
        )
        open_trades = result.scalars().all()
        if not open_trades:
            return

        logger.info("[monitor] Verificando %d posições abertas por órfãos...", len(open_trades))
        for trade in open_trades:
            try:
                open_orders = await self._conn.get_open_orders(trade.symbol)
                has_stop = any(o.type in (OrderType.STOP_MARKET, OrderType.OCO) for o in open_orders)
                if not has_stop:
                    logger.warning("[monitor] Trade #%d (%s) sem OCO/stop — tentando recriar", trade.id, trade.symbol)
                    await self._recreate_stop(trade)
            except Exception as exc:
                logger.error("[monitor] Erro ao verificar órfão trade #%d: %s", trade.id, exc)

    async def _recreate_stop(self, trade: Trade) -> None:
        close_side = OrderSide.SELL if trade.side == "buy" else OrderSide.BUY
        try:
            await self._conn.place_order(
                symbol=trade.symbol,
                side=close_side,
                order_type=OrderType.STOP_MARKET,
                amount=trade.quantity,
                price=trade.stop_loss,
            )
            self._db.add(TradeEvent(
                trade=trade,
                event_type="OCO_SET",
                payload={"note": "stop recriado no startup (órfão)"},
            ))
            await self._db.commit()
            logger.info("[monitor] Stop recriado para trade #%d", trade.id)
        except Exception as exc:
            logger.critical("[monitor] Não conseguiu recriar stop para #%d — fechando a mercado: %s", trade.id, exc)
            await self._close_at_market(trade, "orphan_no_stop")

    async def _close_at_market(self, trade: Trade, reason: str) -> None:
        close_side = OrderSide.SELL if trade.side == "buy" else OrderSide.BUY
        intent_id = uuid4().hex
        try:
            order = await self._conn.place_order(
                symbol=trade.symbol,
                side=close_side,
                order_type=OrderType.MARKET,
                amount=trade.quantity,
                params={"intent_id": intent_id},
            )
            confirmed = (order.raw.get("intent_id") == intent_id
                         and order.status == OrderStatus.CLOSED
                         and order.filled == trade.quantity
                         and order.average is not None)
            if not confirmed:
                state = "UNKNOWN" if order.status == OrderStatus.UNKNOWN else "REJECTED_OR_PARTIAL"
                self._db.add(TradeEvent(
                    trade=trade, event_type="CLOSE_FAILED" if state != "UNKNOWN" else "CLOSE_PENDING",
                    payload={"reason": reason, "intent_id": intent_id, "state": state,
                             "order_id": order.id, "filled": str(order.filled)},
                ))
            else:
                self._db.add(TradeEvent(
                    trade=trade, event_type="CLOSE_CONFIRMED",
                    payload={"reason": reason, "intent_id": intent_id, "order_id": order.id},
                ))
                from trading.order_executor import OrderExecutor
                OrderExecutor(self._conn, self._db)._finalize_trade(trade, order.average, reason, order.id)
        except Exception as exc:
            self._db.add(TradeEvent(
                trade=trade, event_type="CLOSE_FAILED",
                payload={"reason": reason, "intent_id": intent_id, "error": str(exc)},
            ))
        await self._db.commit()

    async def handle_order_update(self, event: dict) -> None:
        """Processa update de ordem recebido via WebSocket e atualiza trade."""
        order_id = str(event.get("id", ""))
        status = event.get("status", "")
        symbol = event.get("symbol", "")

        if status not in ("closed", "canceled"):
            return

        result = await self._db.execute(
            select(Trade).where(Trade.open_order_id == order_id, Trade.status == "open")
        )
        trade = result.scalar_one_or_none()
        if not trade:
            return

        exit_price = Decimal(str(event.get("average") or event.get("price") or 0))
        close_reason = "TP_HIT" if event.get("reduceOnly") else "ORDER_CLOSED"

        import datetime

        side_mult = Decimal("1") if trade.side == "buy" else Decimal("-1")
        pnl_net = (exit_price - trade.entry_price) * trade.quantity * side_mult

        trade.status = "closed"
        trade.exit_price = exit_price
        trade.close_at = datetime.datetime.now(datetime.timezone.utc)
        trade.close_reason = close_reason
        trade.pnl_net = pnl_net.quantize(Decimal("0.01"))

        self._db.add(TradeEvent(
            trade=trade,
            event_type=close_reason,
            payload={"order_event": event},
        ))
        await self._db.commit()

        # Broadcast via Redis pub/sub para o WebSocket
        if self._redis:
            import json
            await self._redis.publish(
                "ws:broadcast",
                json.dumps({
                    "type": "POSITION_UPDATE",
                    "payload": {"trade_id": trade.id, "status": "closed", "pnl_net": str(pnl_net)},
                }),
            )
