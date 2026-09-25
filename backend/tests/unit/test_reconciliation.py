from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from trading.contracts import (Capability, ExecutionContext, ExecutionMode,
                               Instrument, Position)
from trading.connectors.types import OrderSide
from trading.safety import AccountNature, ExposureBlocked


def _context():
    return ExecutionContext(
        venue="mt5", broker="simulator", account_id="reconcile-test",
        nature=AccountNature.SIMULATED, verified=True, mode=ExecutionMode.PAPER,
        market="FOREX", account_currency="USD", position_model="hedging",
        instrument=Instrument(
            symbol="EURUSD", asset_class="forex", base_currency="EUR", quote_currency="USD",
            contract_size=Decimal("1"), tick_size=Decimal("0.00001"), tick_value=Decimal("0.00001"),
            tick_currency="USD", min_volume=Decimal("0.01"), max_volume=Decimal("100"),
            volume_step=Decimal("0.01"), price_precision=5, volume_precision=2,
            margin_model="forex", margin_rate=Decimal("1"), min_notional=Decimal("0.01"),
        ), capabilities=frozenset(Capability),
    )


def _trade(position_id, status="open"):
    return SimpleNamespace(
        id=1, symbol="EURUSD", status=status, position_id=position_id,
        open_order_id="entry-order", quantity=Decimal("0.10"), side="buy",
    )


def _position(position_id):
    context = _context()
    return Position(position_id, context, OrderSide.BUY, Decimal("0.10"), Decimal("1.10000"))


@pytest.mark.parametrize("mutator", ["place_order", "reduce_position", "cancel_order"])
async def test_reconciliation_detects_connector_orphan_without_real_order(mutator):
    from trading.reconciliation import reconcile_positions
    connector = SimpleNamespace(
        list_positions=AsyncMock(return_value=[_position("broker-only")]),
        list_pending_orders=AsyncMock(return_value=[]),
        place_order=AsyncMock(side_effect=AssertionError("must not place orders")),
        reduce_position=AsyncMock(side_effect=AssertionError("must not reduce positions")),
        cancel_order=AsyncMock(side_effect=AssertionError("must not cancel orders")),
    )

    report = await reconcile_positions(connector, [])

    assert [issue.kind for issue in report.issues] == ["orphan_connector_position"]
    assert report.issues[0].position_id == "broker-only"
    getattr(connector, mutator).assert_not_called()


async def test_reconciliation_detects_system_position_missing_from_connector():
    from trading.reconciliation import reconcile_positions
    connector = SimpleNamespace(
        list_positions=AsyncMock(return_value=[]),
        list_pending_orders=AsyncMock(return_value=[]),
    )

    report = await reconcile_positions(connector, [_trade("system-only")])

    missing_position = next(issue for issue in report.issues if issue.kind == "missing_connector_position")
    assert missing_position.trade_id == 1
    assert missing_position.position_id == "system-only"


async def test_reconciliation_blocked_environment_is_signaled_without_mutation():
    from trading.reconciliation import reconcile_positions
    connector = SimpleNamespace(
        list_positions=AsyncMock(side_effect=ExposureBlocked("blocked query")),
        list_pending_orders=AsyncMock(return_value=[]),
        place_order=AsyncMock(side_effect=AssertionError("must not place orders")),
        reduce_position=AsyncMock(side_effect=AssertionError("must not reduce positions")),
        cancel_order=AsyncMock(side_effect=AssertionError("must not cancel orders")),
    )

    report = await reconcile_positions(connector, [_trade("system-only")])

    assert [issue.kind for issue in report.issues] == ["connector_unavailable"]
    connector.place_order.assert_not_called()
    connector.reduce_position.assert_not_called()
    connector.cancel_order.assert_not_called()


async def test_reconciliation_exposes_missing_positions_source():
    from trading.reconciliation import SourceHealth, reconcile_positions

    connector = SimpleNamespace(
        positions_source_health=lambda: SourceHealth("positions", "missing", "DWX_Positions_* ausente"),
        list_positions=AsyncMock(return_value=[]),
        list_pending_orders=AsyncMock(return_value=[]),
    )

    report = await reconcile_positions(connector, [])

    assert report.positions_health.availability == "missing"
    assert report.issues == ()