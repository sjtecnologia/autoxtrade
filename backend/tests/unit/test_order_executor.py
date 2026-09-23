"""Testes unitários do OrderExecutor com mocks."""
import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trading.connectors.types import Order, OrderSide, OrderStatus, OrderType
from trading.order_executor import OrderExecutor, OrderExecutionError


def _make_order(**kw) -> Order:
    defaults = dict(
        id="ord1",
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        status=OrderStatus.CLOSED,
        amount=Decimal("0.01"),
        price=None,
        average=Decimal("50000"),
        filled=Decimal("0.01"),
        remaining=Decimal("0"),
        timestamp=None,
        raw={},
    )
    defaults.update(kw)
    return Order(**defaults)


@pytest.fixture
def mock_connector():
    conn = AsyncMock()
    from trading.safety import AccountNature
    conn.account_nature = AccountNature.SIMULATED
    conn.place_order = AsyncMock(return_value=_make_order())
    conn.place_oco_order = AsyncMock(return_value=_make_order(id="oco1"))
    conn.get_open_orders = AsyncMock(return_value=[])
    conn.cancel_order = AsyncMock(return_value=True)
    conn.get_current_price = AsyncMock(return_value=Decimal("50000"))
    return conn


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

    assert mock_connector.place_order.called
    assert mock_connector.place_oco_order.called
    assert trade is not None


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
    """Se OCO falha, deve tentar stop-market."""
    mock_connector.place_oco_order.side_effect = Exception("OCO not supported")
    mock_connector.place_order.side_effect = [
        _make_order(),             # entrada
        _make_order(id="stop1"),   # stop-market fallback
    ]

    async def fake_refresh(obj):
        obj.__dict__["id"] = 2

    mock_db.refresh.side_effect = fake_refresh

    executor = OrderExecutor(mock_connector, mock_db)
    trade = await executor.open_position(
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        entry_price=Decimal("50000"),
        stop_loss=Decimal("48000"),
        take_profit=Decimal("55000"),
    )

    assert trade is not None
    # Segundo place_order foi stop-market fallback
    calls = mock_connector.place_order.call_args_list
    assert len(calls) == 2
    assert calls[1].kwargs.get("order_type") == OrderType.STOP_MARKET or \
           calls[1].args[2] == OrderType.STOP_MARKET or \
           any(str(OrderType.STOP_MARKET) in str(c) for c in calls[1:])


@pytest.mark.asyncio
async def test_close_position(mock_connector, mock_db):
    import datetime
    import types

    trade = types.SimpleNamespace(
        id=1,
        market="CRIPTO",
        exchange="binance",
        symbol="BTC/USDT",
        side="buy",
        status="open",
        mode="live",
        entry_price=Decimal("50000"),
        quantity=Decimal("0.01"),
        entry_value=Decimal("500"),
        stop_loss=Decimal("48000"),
        open_at=datetime.datetime.now(datetime.timezone.utc),
        close_at=None,
        exit_price=None,
        exit_value=None,
        close_reason=None,
        close_order_id=None,
        pnl_gross=None,
        commission=None,
        pnl_net=None,
        pnl_pct=None,
        duration_sec=None,
    )

    executor = OrderExecutor(mock_connector, mock_db)
    result = await executor.close_position(trade, reason="manual")
    assert result.status == "closed"
    assert result.close_reason == "manual"
