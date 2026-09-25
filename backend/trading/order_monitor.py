"""Monitor de ordens: detecta execuções via WebSocket e verifica órfãos no startup."""
import logging
from decimal import Decimal

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import Trade, TradeEvent
from trading.connectors.base import BaseConnector
from trading.reconciliation import ReconciliationReport, reconcile_positions

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

    async def check_orphan_positions(self) -> ReconciliationReport:
        """Sinaliza divergências entre sistema e conector sem executar mutações."""
        result = await self._db.execute(
            select(Trade).where(Trade.status == "open")
        )
        open_trades = result.scalars().all()
        report = await reconcile_positions(self._conn, open_trades)
        for issue in report.issues:
            logger.warning("[monitor] Reconciliation issue: %s", issue)
        return report

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
