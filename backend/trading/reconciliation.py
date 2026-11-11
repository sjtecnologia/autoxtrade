"""Read-only reconciliation between persisted trades and connector state."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from trading.contracts import ResultState


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
    entry_orders_health: SourceHealth = field(
        default_factory=lambda: SourceHealth("entry_orders", "ok")
    )
    protection_health: SourceHealth = field(
        default_factory=lambda: SourceHealth("protection", "ok")
    )

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)


def _open_trades(trades: Iterable[object]) -> list[object]:
    return [trade for trade in trades if getattr(trade, "status", "open") == "open"]


def _decimal_equal(left: object, right: object) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (ArithmeticError, TypeError, ValueError):
        return False


def _entry_missing(result: object) -> bool:
    reason = str(getattr(result, "reason", "") or "").strip().lower()
    return (
        getattr(result, "state", None) == ResultState.UNKNOWN
        and reason in {"order not found", "intent not found"}
        and not getattr(result, "order_id", None)
        and not getattr(result, "position_id", None)
        and Decimal(str(getattr(result, "filled", 0) or 0)) == 0
    )


def _identity_mismatches(connector_context: object, trade: object, position: object) -> list[str]:
    mismatches: list[str] = []
    position_context = getattr(position, "context", None)
    if position_context is None:
        return ["connector position has no execution context"]

    expected_context = getattr(trade, "execution_context", None)
    expected_account = getattr(trade, "account_id", None)
    if expected_context:
        expected_account = expected_context.get("account_id") or expected_account
        expected_market = expected_context.get("market")
        expected_mode = expected_context.get("mode")
        expected_venue = expected_context.get("venue")
        expected_broker = expected_context.get("broker")
        if expected_market and getattr(position_context, "market", None) != expected_market:
            mismatches.append("market mismatch")
        if expected_mode and getattr(getattr(position_context, "mode", None), "value", None) != expected_mode:
            mismatches.append("mode mismatch")
        if expected_venue and getattr(position_context, "venue", None) != expected_venue:
            mismatches.append("venue mismatch")
        if expected_broker and getattr(position_context, "broker", None) != expected_broker:
            mismatches.append("broker mismatch")

    for label, expected, actual in (
        ("account", expected_account, getattr(position_context, "account_id", None)),
        (
            "connector account",
            getattr(connector_context, "account_id", None),
            getattr(position_context, "account_id", None),
        ),
        (
            "connector market",
            getattr(connector_context, "market", None),
            getattr(position_context, "market", None),
        ),
        (
            "connector mode",
            getattr(getattr(connector_context, "mode", None), "value", None),
            getattr(getattr(position_context, "mode", None), "value", None),
        ),
        (
            "connector venue",
            getattr(connector_context, "venue", None),
            getattr(position_context, "venue", None),
        ),
        (
            "connector broker",
            getattr(connector_context, "broker", None),
            getattr(position_context, "broker", None),
        ),
        (
            "instrument",
            getattr(trade, "symbol", None),
            getattr(getattr(position_context, "instrument", None), "symbol", None),
        ),
        (
            "connector instrument",
            getattr(getattr(connector_context, "instrument", None), "symbol", None),
            getattr(getattr(position_context, "instrument", None), "symbol", None),
        ),
    ):
        if expected is not None and actual != expected:
            mismatches.append(f"{label} mismatch")
    return mismatches


async def reconcile_positions(connector, trades: Iterable[object]) -> ReconciliationReport:
    """Reconcile entry orders by ID, positions, and protection as separate sources.

    A missing pending-order-list entry is not evidence that an executed entry
    is absent. This function only calls query/list APIs and never mutates state.
    """
    issues: list[ReconciliationIssue] = []
    system_trades = _open_trades(trades)
    health_factory = getattr(connector, "positions_source_health", None)
    try:
        positions_health = (
            health_factory()
            if callable(health_factory)
            else SourceHealth("positions", "ok")
        )
    except Exception as exc:
        positions_health = SourceHealth(
            "positions", "unavailable", str(exc) or exc.__class__.__name__
        )
    connector_context = getattr(connector, "context", None)
    if callable(connector_context):
        connector_context = connector_context()

    try:
        connector_positions = await connector.list_positions()
    except Exception as exc:
        detail = str(exc) or exc.__class__.__name__
        health = SourceHealth("positions", "unavailable", detail)
        issues.append(ReconciliationIssue("positions_unavailable", detail=detail))
        connector_positions = []
    else:
        health = positions_health

    if health.availability != "ok":
        issues.append(
            ReconciliationIssue(
                f"positions_source_{health.availability}",
                detail=health.detail,
            )
        )

    connector_by_position = {
        str(position.id): position
        for position in connector_positions
        if getattr(position, "id", None) is not None
    }
    trades_by_position = {
        str(trade.position_id): trade
        for trade in system_trades
        if getattr(trade, "position_id", None)
    }

    for trade in system_trades:
        position_id = getattr(trade, "position_id", None)
        if not position_id:
            issues.append(
                ReconciliationIssue(
                    "system_position_unresolved",
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail="Open trade has no position_id",
                )
            )
        elif health.availability == "ok" and str(position_id) not in connector_by_position:
            issues.append(
                ReconciliationIssue(
                    "missing_connector_position",
                    position_id=str(position_id),
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                )
            )

    for position_id, position in connector_by_position.items():
        if health.availability != "ok":
            break
        trade = trades_by_position.get(position_id)
        if trade is None:
            issues.append(
                ReconciliationIssue(
                    "orphan_connector_position",
                    position_id=position_id,
                    symbol=getattr(
                        getattr(position, "context", None).instrument
                        if getattr(position, "context", None)
                        else None,
                        "symbol",
                        None,
                    ),
                )
            )
            continue

        identity_mismatches = _identity_mismatches(
            connector_context, trade, position
        )
        if identity_mismatches:
            issues.append(
                ReconciliationIssue(
                    "position_identity_mismatch",
                    position_id=position_id,
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail=", ".join(identity_mismatches),
                )
            )
        if not _decimal_equal(
            getattr(position, "quantity", None), getattr(trade, "quantity", None)
        ):
            issues.append(
                ReconciliationIssue(
                    "position_quantity_mismatch",
                    position_id=position_id,
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail=(
                        f"system={getattr(trade, 'quantity', None)}, "
                        f"connector={getattr(position, 'quantity', None)}"
                    ),
                )
            )

        position_side = getattr(getattr(position, "side", None), "value", getattr(position, "side", None))
        trade_side = getattr(getattr(trade, "side", None), "value", getattr(trade, "side", None))
        if position_side != trade_side:
            issues.append(
                ReconciliationIssue(
                    "position_direction_mismatch",
                    position_id=position_id,
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail=f"system={trade_side}, connector={position_side}",
                )
            )

    # Query each stored entry order by its stable broker ID. Executed orders
    # are normally absent from pending-order listings and must still reconcile.
    order_health = "ok"
    order_details: list[str] = []
    for trade in system_trades:
        order_id = getattr(trade, "open_order_id", None)
        if not order_id:
            issues.append(
                ReconciliationIssue(
                    "system_entry_order_unresolved",
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail="Open trade has no open_order_id",
                )
            )
            continue
        try:
            result = await connector.query_order(order_id=str(order_id))
        except Exception as exc:
            detail = str(exc) or exc.__class__.__name__
            order_health = "unavailable"
            order_details.append(detail)
            issues.append(
                ReconciliationIssue(
                    "entry_order_unavailable",
                    order_id=str(order_id),
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail=detail,
                )
            )
            continue

        if _entry_missing(result):
            issues.append(
                ReconciliationIssue(
                    "missing_connector_order",
                    order_id=str(order_id),
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail="Order absence confirmed by query_order",
                )
            )
            continue
        result_state = getattr(result, "state", None)
        if result_state in {ResultState.UNKNOWN, ResultState.UNAVAILABLE}:
            state = "unavailable" if result_state == ResultState.UNAVAILABLE else "unknown"
            if order_health == "ok" or state == "unavailable":
                order_health = state
            detail = getattr(result, "reason", None) or result_state.value
            order_details.append(str(detail))
            issues.append(
                ReconciliationIssue(
                    f"entry_order_{state}",
                    order_id=str(order_id),
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail=str(detail),
                )
            )
            continue
        observed_order_id = getattr(result, "order_id", None)
        if observed_order_id is not None and str(observed_order_id) != str(order_id):
            issues.append(
                ReconciliationIssue(
                    "entry_order_id_mismatch",
                    order_id=str(order_id),
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail=f"query returned order_id={observed_order_id}",
                )
            )
        observed_position_id = getattr(result, "position_id", None)
        expected_position_id = getattr(trade, "position_id", None)
        if (
            observed_position_id is not None
            and expected_position_id is not None
            and str(observed_position_id) != str(expected_position_id)
        ):
            issues.append(
                ReconciliationIssue(
                    "entry_order_position_mismatch",
                    position_id=str(expected_position_id),
                    order_id=str(order_id),
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail=f"query returned position_id={observed_position_id}",
                )
            )
        if result_state == ResultState.REJECTED:
            issues.append(
                ReconciliationIssue(
                    "entry_order_rejected",
                    order_id=str(order_id),
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail=getattr(result, "reason", None),
                )
            )
        filled = getattr(result, "filled", None)
        if filled is None or not _decimal_equal(filled, getattr(trade, "quantity", None)):
            issues.append(
                ReconciliationIssue(
                    "entry_order_quantity_mismatch",
                    order_id=str(order_id),
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail=f"system={getattr(trade, 'quantity', None)}, connector_filled={filled}",
                )
            )

    entry_orders_health = SourceHealth(
        "entry_orders",
        order_health,
        "; ".join(dict.fromkeys(order_details)) or None,
    )

    protection_health = health.availability if health.availability != "ok" else "ok"
    protection_details: list[str] = (
        [health.detail] if health.availability != "ok" and health.detail else []
    )
    for trade in system_trades:
        position_id = getattr(trade, "position_id", None)
        if health.availability != "ok":
            break
        position = connector_by_position.get(str(position_id)) if position_id else None
        if position is None:
            continue
        missing_fields = [
            field_name
            for field_name in ("stop_loss", "take_profit")
            if not hasattr(position, field_name)
        ]
        if missing_fields:
            protection_health = "unknown"
            detail = f"Position omits protection fields: {', '.join(missing_fields)}"
            protection_details.append(detail)
            issues.append(
                ReconciliationIssue(
                    "protection_unknown",
                    position_id=str(position_id),
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail=detail,
                )
            )
            continue

        differences = []
        for field_name in ("stop_loss", "take_profit"):
            expected = getattr(trade, field_name, None)
            actual = getattr(position, field_name, None)
            if expected is None:
                if actual is not None:
                    differences.append(f"{field_name} system=None connector={actual}")
            elif actual is None:
                differences.append(f"{field_name} missing on connector")
            elif not _decimal_equal(expected, actual):
                differences.append(f"{field_name} system={expected} connector={actual}")
        if differences:
            protection_health = "ok"
            issues.append(
                ReconciliationIssue(
                    "protection_mismatch",
                    position_id=str(position_id),
                    trade_id=getattr(trade, "id", None),
                    symbol=getattr(trade, "symbol", None),
                    detail="; ".join(differences),
                )
            )

    return ReconciliationReport(
        tuple(issues),
        health,
        entry_orders_health,
        SourceHealth(
            "protection",
            protection_health,
            "; ".join(dict.fromkeys(protection_details)) or None,
        ),
    )
