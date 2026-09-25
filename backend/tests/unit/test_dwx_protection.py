from decimal import Decimal
from unittest.mock import AsyncMock

from trading.connectors.dwx import DWXConnector
from trading.connectors.fake_transports import TemporaryDWXTransport
from trading.connectors.types import OrderSide, OrderType
from trading.contracts import (Capability, ExecutionContext, ExecutionMode,
                               Instrument, OrderIntent, ResultState)
from trading.safety import AccountNature


def _context():
    return ExecutionContext(
        venue="mt5", broker="simulator", account_id="mt5-paper-test",
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


async def test_dwx_protection_modifies_existing_position_without_opposite_order(tmp_path):
    context = _context()
    transport = TemporaryDWXTransport(context)
    transport.prices[context.instrument.symbol] = Decimal("1.10000")
    conn = DWXConnector(str(tmp_path), context=context, transport=transport)
    conn.place_order = AsyncMock(side_effect=AssertionError("opposite order fallback is forbidden"))

    entry = await conn.create_order(OrderIntent(
        "entry", context, OrderSide.BUY, OrderType.MARKET, Decimal("0.10")))
    result = await conn.modify_position_protection(
        entry.position_id, stop_loss=Decimal("1.09000"), take_profit=Decimal("1.12000"),
        intent_id="protect")

    assert result.state is ResultState.CONFIRMED
    assert result.position_id == entry.position_id
    conn.place_order.assert_not_awaited()
    assert list(transport.positions) == [entry.position_id]
    commands = [path.read_text() for path in transport.directory.glob("DWX_Commands_*.txt")]
    assert any("|MODIFY_ORDER|" in text for text in commands)
    assert not any("sell" in text.lower() and "|OPEN_ORDER|" in text for text in commands)