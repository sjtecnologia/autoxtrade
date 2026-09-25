"""Read-only reconciliation between system state and connector state."""
from dataclasses import dataclass, field
from typing import Iterable

from trading.safety import ExposureBlocked


@dataclass(frozen=True)
class SourceHealth:
    source: str
    availability: str
    detail: str | None = None


@dataclass(frozen=True)
class ReconciliationIssue:
    kind: str
    position_id: str | None = None
    order_id: str | None = None
    trade_id: int | None = None
    symbol: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ReconciliationReport:
    issues: tuple[ReconciliationIssue, ...]
    positions_health: SourceHealth = field(
        default_factory=lambda: SourceHealth("positions", "ok")
    )

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)


def _open_trades(trades: Iterable[object]) -> list[object]:
    return [trade for trade in trades if getattr(trade, "status", "open") == "open"]


async def reconcile_positions(connector, trades: Iterable[object]) -> ReconciliationReport:
    """Compare known open trades with connector-reported positions/orders.

    This routine deliberately never sends, cancels, reduces, or closes orders.
    It only queries connector state and returns issues for the caller to surface.
    """
    issues: list[ReconciliationIssue] = []
    system_trades = _open_trades(trades)
    health_factory = getattr(connector, "positions_source_health", None)
    positions_health = health_factory() if callable(health_factory) else SourceHealth("positions", "ok")
    try:
        connector_positions = await connector.list_positions()
        connector_orders = await connector.list_pending_orders()
    except ExposureBlocked as exc:
        return ReconciliationReport(
            (ReconciliationIssue("connector_unavailable", detail=str(exc)),),
            positions_health,
        )

    trades_by_position = {
        str(trade.position_id): trade for trade in system_trades if getattr(trade, "position_id", None)
    }
    connector_by_position = {str(position.id): position for position in connector_positions}

    for trade in system_trades:
        position_id = getattr(trade, "position_id", None)
        if not position_id:
            issues.append(ReconciliationIssue(
                "system_position_unresolved", trade_id=getattr(trade, "id", None),
                symbol=getattr(trade, "symbol", None), detail="Open trade has no position_id"))
        elif str(position_id) not in connector_by_position:
            issues.append(ReconciliationIssue(
                "missing_connector_position", position_id=str(position_id),
                trade_id=getattr(trade, "id", None), symbol=getattr(trade, "symbol", None)))

    for position_id, position in connector_by_position.items():
        if position_id not in trades_by_position:
            issues.append(ReconciliationIssue(
                "orphan_connector_position", position_id=position_id,
                symbol=position.context.instrument.symbol))

    system_order_ids = {
        str(trade.open_order_id) for trade in system_trades if getattr(trade, "open_order_id", None)
    }
    connector_order_ids = {str(order.id) for order in connector_orders if getattr(order, "id", None)}

    for order_id in sorted(system_order_ids - connector_order_ids):
        trade = next((trade for trade in system_trades if str(getattr(trade, "open_order_id", "")) == order_id), None)
        issues.append(ReconciliationIssue(
            "missing_connector_order", order_id=order_id,
            trade_id=getattr(trade, "id", None), symbol=getattr(trade, "symbol", None)))
    for order in connector_orders:
        order_id = str(order.id)
        if order_id not in system_order_ids:
            issues.append(ReconciliationIssue(
                "orphan_connector_order", order_id=order_id, symbol=getattr(order, "symbol", None)))

    return ReconciliationReport(tuple(issues), positions_health)