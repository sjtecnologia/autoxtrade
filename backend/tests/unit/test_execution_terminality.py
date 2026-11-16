from decimal import Decimal

import pytest

from trading.connectors.binance import BinanceConnector
from trading.connectors.fake_transports import FakeCCXTClient
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.types import OrderSide, OrderType
from trading.contracts import ExecutionResult, OrderIntent, ResultState
from tests.unit.test_order_executor import _context


def _intent(context, identifier):
    return OrderIntent(
        identifier, context, OrderSide.BUY, OrderType.MARKET, Decimal("0.01")
    )


def test_memory_partial_grows_and_full_order_is_terminal():
    context = _context()
    transport = MemoryTransport(context)
    transport.prices[context.instrument.symbol] = Decimal("50000")
    transport.scenarios["partial"] = Decimal("0.005")
    first = transport.create(_intent(context, "partial"))
    assert first.state == ResultState.PARTIAL
    assert first.filled == Decimal("0.005")
    assert first.terminal is False
    final = transport.settle("partial", Decimal("0.01"))
    assert final.filled == Decimal("0.01")
    assert final.terminal is True
    assert transport.query(intent_id="partial") == final


def test_memory_cancelled_partial_is_terminal_without_losing_fill():
    context = _context()
    transport = MemoryTransport(context)
    transport.prices[context.instrument.symbol] = Decimal("50000")
    transport.scenarios["partial"] = Decimal("0.005")
    first = transport.create(_intent(context, "partial"))
    transport.cancel(first.order_id, "cancel")
    result = transport.query(intent_id="partial")
    assert result.state == ResultState.PARTIAL
    assert result.filled == Decimal("0.005")
    assert result.terminal is True
    assert not transport.pending()


@pytest.mark.parametrize("status,expected", [
    ("open", False), ("canceled", True), ("unknown", None),
])
def test_binance_terminality_is_explicit_from_status(status, expected):
    context = _context()
    connector = BinanceConnector(context=context, client=FakeCCXTClient(context))
    result = connector._parse_result({
        "id": "order", "clientOrderId": "intent", "symbol": context.instrument.symbol,
        "status": status, "filled": "0.005", "amount": "0.01", "average": "50000",
    }, "intent")
    assert result.terminal is expected
    assert result.state == (ResultState.UNKNOWN if status == "unknown" else ResultState.PARTIAL)


def test_unknown_terminality_is_the_default():
    assert ExecutionResult("intent", ResultState.UNKNOWN).terminal is None
