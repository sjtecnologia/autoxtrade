"""Testes unitários do OrderExecutor com conector simulado vinculado."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.types import OrderSide
from trading.contracts import (Capability, ExecutionContext, ExecutionResult,
                               ExecutionMode, Instrument, ResultState)
from trading.order_executor import OrderExecutor, OrderExecutionError
from trading.safety import AccountNature


def _context() -> ExecutionContext:
    return ExecutionContext(
        venue="binance_spot", broker="simulator", account_id="test-account",
        nature=AccountNature.SIMULATED, verified=True, mode=ExecutionMode.PAPER,
        market="CRIPTO", account_currency="USDT", position_model="hedging",
        instrument=Instrument(
            symbol="BTC/USDT", asset_class="crypto", base_currency="BTC",
            quote_currency="USDT", contract_size=Decimal("1"), tick_size=Decimal("1"),
            tick_value=Decimal("1"), tick_currency="USDT", min_volume=Decimal("0.001"),
            max_volume=Decimal("100"), volume_step=Decimal("0.001"), price_precision=0,
            volume_precision=3, margin_model="cash", margin_rate=Decimal("1"),
            min_notional=Decimal("1"),
        ),
        capabilities=frozenset(Capability),
    )


@pytest.fixture
def mock_connector():
    context = _context()
    transport = MemoryTransport(context)
    transport.prices[context.instrument.symbol] = Decimal("50000")
    return SimulatedConnector(context, transport=transport)


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_open_position_success(mock_connector, mock_db):
    executor = OrderExecutor(mock_connector, mock_db)

    async def fake_refresh(obj):
        obj.__dict__["id"] = 1

    mock_db.refresh.side_effect = fake_refresh

    trade = await executor.open_position(
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        entry_price=Decimal("50000"),
        stop_loss=Decimal("48000"),
        take_profit=Decimal("55000"),
    )

    assert trade.status == "open"
    assert trade.position_id is not None
    assert mock_connector._transport.positions[trade.position_id].stop_loss == Decimal("48000")


@pytest.mark.asyncio
async def test_open_position_invalid_sl(mock_connector, mock_db):
    executor = OrderExecutor(mock_connector, mock_db)
    with pytest.raises(OrderExecutionError, match="MENOR"):
        await executor.open_position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.01"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("52000"),  # inválido: SL acima da entrada em LONG
        )


@pytest.mark.asyncio
async def test_oco_fallback_to_stop_market(mock_connector, mock_db):
    """Proteção rejeitada mantém a exposição aberta para reconciliação."""
    mock_connector.modify_position_protection = AsyncMock(return_value=ExecutionResult(
        "protect", ResultState.REJECTED, reason="simulated rejection"))

    async def fake_refresh(obj):
        obj.__dict__["id"] = 2

    mock_db.refresh.side_effect = fake_refresh

    executor = OrderExecutor(mock_connector, mock_db)
    with pytest.raises(OrderExecutionError, match="Protection not confirmed"):
        await executor.open_position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.01"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("48000"),
            take_profit=Decimal("55000"),
        )

    persisted_trade = next(call.args[0] for call in mock_db.add.call_args_list
                           if getattr(call.args[0], "status", None) == "open")
    assert persisted_trade.status == "open"


@pytest.mark.asyncio
async def test_close_position(mock_connector, mock_db):
    executor = OrderExecutor(mock_connector, mock_db)
    trade = await executor.open_position(
        symbol="BTC/USDT", side=OrderSide.BUY, quantity=Decimal("0.01"),
        entry_price=Decimal("50000"), stop_loss=Decimal("48000"),
        take_profit=Decimal("55000"),
    )
    result = await executor.close_position(trade, reason="manual")
    assert result.status == "closed"
    assert result.close_reason == "manual"
