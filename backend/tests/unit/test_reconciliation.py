from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from trading.contracts import (
    Capability,
    ExecutionContext,
    ExecutionMode,
    ExecutionResult,
    Instrument,
    Position,
    ResultState,
)
from trading.connectors.types import OrderSide
from trading.safety import AccountNature, ExposureBlocked


def _context(symbol="EURUSD", account_id="reconcile-test"):
    return ExecutionContext(
        venue="mt5",
        broker="simulator",
        account_id=account_id,
        nature=AccountNature.SIMULATED,
        verified=True,
        mode=ExecutionMode.PAPER,
        market="FOREX",
        account_currency="USD",
        position_model="hedging",
        instrument=Instrument(
            symbol=symbol,
            asset_class="forex",
            base_currency="EUR",
            quote_currency="USD",
            contract_size=Decimal("1"),
            tick_size=Decimal("0.00001"),
            tick_value=Decimal("0.00001"),
            tick_currency="USD",
            min_volume=Decimal("0.01"),
            max_volume=Decimal("100"),
            volume_step=Decimal("0.01"),
            price_precision=5,
            volume_precision=2,
            margin_model="forex",
            margin_rate=Decimal("1"),
            min_notional=Decimal("0.01"),
        ),
        capabilities=frozenset(Capability),
    )


def _trade(position_id, status="open", **overrides):
    values = {
        "id": 1,
        "symbol": "EURUSD",
        "status": status,
        "position_id": position_id,
        "open_order_id": "entry-order",
        "quantity": Decimal("0.10"),
        "side": "buy",
        "stop_loss": Decimal("1.09000"),
        "take_profit": Decimal("1.12000"),
        "account_id": "reconcile-test",
        "execution_context": _context().snapshot(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _position(
    position_id,
    *,
    quantity=Decimal("0.10"),
    side=OrderSide.BUY,
    context=None,
    stop_loss=Decimal("1.09000"),
    take_profit=Decimal("1.12000"),
):
    return Position(
        position_id,
        context or _context(),
        side,
        quantity,
        Decimal("1.10000"),
        stop_loss,
        take_profit,
    )


def _confirmed_entry(
    order_id="entry-order",
    *,
    position_id="system-position",
    filled=Decimal("0.10"),
):
    return ExecutionResult(
        intent_id="entry-intent",
        state=ResultState.CONFIRMED,
        order_id=order_id,
        position_id=position_id,
        filled=filled,
        average=Decimal("1.10000"),
    )


def _connector(*, positions=(), query_result=None, **overrides):
    connector = SimpleNamespace(
        context=_context(),
        list_positions=AsyncMock(return_value=list(positions)),
        query_order=AsyncMock(return_value=query_result or _confirmed_entry()),
        list_pending_orders=AsyncMock(
            side_effect=AssertionError("entry reconciliation must query by order ID")
        ),
        place_order=AsyncMock(side_effect=AssertionError("must not place orders")),
        reduce_position=AsyncMock(side_effect=AssertionError("must not reduce positions")),
        cancel_order=AsyncMock(side_effect=AssertionError("must not cancel orders")),
    )
    for key, value in overrides.items():
        setattr(connector, key, value)
    return connector


@pytest.mark.parametrize("mutator", ["place_order", "reduce_position", "cancel_order"])
async def test_reconciliation_detects_connector_orphan_without_mutation(mutator):
    from trading.reconciliation import reconcile_positions

    connector = _connector(positions=[_position("broker-only")])
    report = await reconcile_positions(connector, [])

    assert [issue.kind for issue in report.issues] == ["orphan_connector_position"]
    assert report.issues[0].position_id == "broker-only"
    connector.query_order.assert_not_called()
    connector.list_pending_orders.assert_not_called()
    getattr(connector, mutator).assert_not_called()


async def test_reconciliation_detects_system_position_missing_from_connector():
    from trading.reconciliation import reconcile_positions

    connector = _connector(positions=[])
    report = await reconcile_positions(connector, [_trade("system-only")])

    missing_position = next(
        issue for issue in report.issues if issue.kind == "missing_connector_position"
    )
    assert missing_position.trade_id == 1
    assert missing_position.position_id == "system-only"
    connector.query_order.assert_awaited_once_with(order_id="entry-order")


async def test_filled_entry_is_queried_when_not_pending():
    from trading.reconciliation import reconcile_positions

    trade = _trade("system-position")
    connector = _connector(
        positions=[_position("system-position")],
        query_result=_confirmed_entry(position_id="system-position"),
    )

    report = await reconcile_positions(connector, [trade])

    assert not any(issue.kind == "missing_connector_order" for issue in report.issues)
    assert not any(issue.kind == "entry_order_quantity_mismatch" for issue in report.issues)
    connector.query_order.assert_awaited_once_with(order_id="entry-order")
    connector.list_pending_orders.assert_not_called()


async def test_reconciliation_detects_quantity_direction_and_account_mismatches():
    from trading.reconciliation import reconcile_positions

    trade = _trade("system-position")
    connector_position = _position(
        "system-position",
        quantity=Decimal("0.20"),
        side=OrderSide.SELL,
        context=_context(account_id="different-account"),
    )
    connector = _connector(
        positions=[connector_position],
        query_result=_confirmed_entry(
            position_id="system-position",
            filled=Decimal("0.20"),
        ),
    )

    report = await reconcile_positions(connector, [trade])
    kinds = {issue.kind for issue in report.issues}

    assert "position_quantity_mismatch" in kinds
    assert "position_direction_mismatch" in kinds
    assert "position_identity_mismatch" in kinds
    assert "entry_order_quantity_mismatch" in kinds


async def test_reconciliation_distinguishes_confirmed_order_absence_from_unknown():
    from trading.reconciliation import reconcile_positions

    trade = _trade("system-position")
    missing = _connector(
        positions=[_position("system-position")],
        query_result=ExecutionResult(
            intent_id="",
            state=ResultState.UNKNOWN,
            reason="Order not found",
        ),
    )
    missing_report = await reconcile_positions(missing, [trade])
    assert any(issue.kind == "missing_connector_order" for issue in missing_report.issues)
    assert missing_report.entry_orders_health.availability == "ok"

    uncertain = _connector(
        positions=[_position("system-position")],
        query_result=ExecutionResult(
            intent_id="",
            state=ResultState.UNKNOWN,
            reason="Transport timeout",
        ),
    )
    uncertain_report = await reconcile_positions(uncertain, [trade])
    assert any(issue.kind == "entry_order_unknown" for issue in uncertain_report.issues)
    assert not any(
        issue.kind == "missing_connector_order" for issue in uncertain_report.issues
    )
    assert uncertain_report.entry_orders_health.availability == "unknown"


async def test_reconciliation_reports_source_unavailability_without_false_absence():
    from trading.reconciliation import reconcile_positions

    connector = _connector(
        positions=[],
        list_positions=AsyncMock(side_effect=ExposureBlocked("positions query blocked")),
        query_order=AsyncMock(side_effect=ExposureBlocked("order query unavailable")),
    )

    report = await reconcile_positions(connector, [_trade("system-only")])

    assert report.positions_health.availability == "unavailable"
    assert report.entry_orders_health.availability == "unavailable"
    assert "positions_unavailable" in {issue.kind for issue in report.issues}
    assert "entry_order_unavailable" in {issue.kind for issue in report.issues}
    assert not any(issue.kind == "missing_connector_position" for issue in report.issues)
    connector.list_pending_orders.assert_not_called()
    connector.place_order.assert_not_called()
    connector.reduce_position.assert_not_called()
    connector.cancel_order.assert_not_called()


async def test_protection_is_reconciled_separately_from_entry_orders():
    from trading.reconciliation import reconcile_positions

    trade = _trade("system-position")
    connector = _connector(
        positions=[
            _position(
                "system-position",
                stop_loss=Decimal("1.08000"),
                take_profit=None,
            )
        ],
    )

    report = await reconcile_positions(connector, [trade])

    protection_issue = next(
        issue for issue in report.issues if issue.kind == "protection_mismatch"
    )
    assert "stop_loss" in protection_issue.detail
    assert "take_profit" in protection_issue.detail
    assert report.protection_health.availability == "ok"
    connector.list_pending_orders.assert_not_called()


async def test_reconciliation_exposes_missing_positions_source_without_false_missing():
    from trading.reconciliation import SourceHealth, reconcile_positions

    connector = _connector(
        positions=[],
        positions_source_health=lambda: SourceHealth(
            "positions", "missing", "DWX_Positions_* ausente"
        ),
    )

    report = await reconcile_positions(connector, [_trade("system-only")])

    assert report.positions_health.availability == "missing"
    assert any(issue.kind == "positions_source_missing" for issue in report.issues)
    assert not any(issue.kind == "missing_connector_position" for issue in report.issues)
