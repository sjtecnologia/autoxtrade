from decimal import Decimal
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import settings
from trading.safety import (
    AccountNature, ExposureBlocked, require_entry_mode, require_new_exposure,
    require_position_management,
)
from trading.connectors.factory import get_connector, _create
from trading.connectors.fake_transports import TemporaryDWXTransport
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.dwx import DWXConnector
from trading.connectors.types import OrderSide, OrderType
from trading.contracts import (Capability, ExecutionContext, ExecutionMode,
                               Instrument, OrderIntent, ResultState)
from trading.order_executor import OrderExecutor


def _context(market="FOREX"):
    symbol = "BTC/USDT" if market == "CRIPTO" else "EURUSD"
    return ExecutionContext(
        venue="binance_spot" if market == "CRIPTO" else "mt5",
        broker="simulator", account_id=f"{market.lower()}-test", nature=AccountNature.SIMULATED,
        verified=True, mode=ExecutionMode.PAPER, market=market, account_currency="USDT",
        position_model="hedging",
        instrument=Instrument(
            symbol=symbol, asset_class="crypto" if market == "CRIPTO" else "forex",
            base_currency=symbol[:3], quote_currency="USDT" if market == "CRIPTO" else "USD",
            contract_size=Decimal("1"), tick_size=Decimal("1"), tick_value=Decimal("1"),
            tick_currency="USDT" if market == "CRIPTO" else "USD", min_volume=Decimal("0.001"),
            max_volume=Decimal("100"), volume_step=Decimal("0.001"), price_precision=0,
            volume_precision=3, margin_model="cash", margin_rate=Decimal("1"),
            min_notional=Decimal("1"),
        ), capabilities=frozenset(Capability),
    )

@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("nature", [AccountNature.UNKNOWN, AccountNature.REAL, AccountNature.DEMO])
def test_no_unvalidated_account_can_open(enabled, nature, monkeypatch):
    monkeypatch.setattr(settings, "real_new_exposure_enabled", enabled)
    with pytest.raises(ExposureBlocked):
        require_new_exposure(SimpleNamespace(account_nature=nature))


def test_default_disabled():
    from config import Settings
    assert Settings.model_fields["real_new_exposure_enabled"].default is False


@pytest.mark.parametrize("mode", ["live", "demo", "PAPER", "", None])
def test_invalid_or_real_mode_blocked(mode):
    with pytest.raises(ExposureBlocked):
        require_entry_mode(mode)


@pytest.mark.parametrize("market", ["CRIPTO", "B3", "FOREX"])
def test_factory_paper_is_always_simulated(market, monkeypatch):
    monkeypatch.setattr(settings, "binance_paper_mode", False)
    context = _context(market)
    assert isinstance(get_connector(context, force_new=True), SimulatedConnector)
    assert isinstance(_create(context), SimulatedConnector)
    live_context = ExecutionContext(**{**context.__dict__, "nature": AccountNature.REAL,
                                       "mode": ExecutionMode.LIVE})
    with pytest.raises(ExposureBlocked):
        get_connector(live_context, force_new=True)
    with pytest.raises(ExposureBlocked):
        _create(live_context)


async def test_direct_executor_blocks_before_send_or_persistence():
    conn, db = AsyncMock(), AsyncMock()
    conn.account_nature = AccountNature.UNKNOWN
    with pytest.raises(ExposureBlocked):
        await OrderExecutor(conn, db).open_position(
            "EURUSD", OrderSide.BUY, Decimal("1"), Decimal("100"), Decimal("95"))
    conn.place_order.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.parametrize("command", ["OPEN_ORDER", "CLOSE_ORDER", "CLOSE_ALL_ORDERS", "MODIFY_ORDER", "CUSTOM"])
def test_dwx_low_level_mutations_blocked(tmp_path, command):
    conn = DWXConnector(str(tmp_path))
    with pytest.raises(ExposureBlocked):
        conn._write_command(command, "arbitrary")
    assert list(tmp_path.iterdir()) == []
    assert conn.account_nature is AccountNature.SIMULATED


async def test_dwx_direct_methods_blocked(tmp_path):
    conn = DWXConnector(str(tmp_path))
    with pytest.raises(ExposureBlocked):
        await conn.place_order("EURUSD", OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    with pytest.raises(ExposureBlocked):
        await conn.place_oco_order("EURUSD", OrderSide.SELL, *[Decimal("1")] * 4)
    with pytest.raises(ExposureBlocked):
        await conn.cancel_order("1", "EURUSD")
    assert list(tmp_path.iterdir()) == []


async def test_binance_direct_methods_blocked():
    from trading.connectors.binance import BinanceConnector
    conn = object.__new__(BinanceConnector)
    conn._exchange = AsyncMock()
    with pytest.raises(ExposureBlocked):
        await conn.place_order("BTC/USDT", OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    with pytest.raises(ExposureBlocked):
        await conn.place_oco_order("BTC/USDT", OrderSide.SELL, *[Decimal("1")] * 4)
    conn._exchange.create_order.assert_not_called()


async def test_approval_service_blocks_live_before_status_write():
    from trading.approval_service import EntryApprovalService
    svc = object.__new__(EntryApprovalService)
    svc._redis = AsyncMock()
    svc._redis.ttl.return_value = 60
    svc.get_entry = AsyncMock(return_value={"mode": "live", "status": "pending"})
    for status in ("approved", "executing"):
        with pytest.raises(ExposureBlocked):
            await svc.update_entry_status("id", status)
    svc._redis.setex.assert_not_called()


async def test_manual_approval_returns_conflict(monkeypatch):
    from fastapi import HTTPException
    from api.routes import approvals
    svc = AsyncMock()
    svc.update_entry_status.side_effect = ExposureBlocked("disabled")
    monkeypatch.setattr(approvals, "EntryApprovalService", lambda: svc)
    with pytest.raises(HTTPException) as error:
        await approvals.approve_entry("id", approvals.EntryApprovalRequest(), "test")
    assert error.value.status_code == 409
    svc.close.assert_awaited_once()


@pytest.mark.parametrize("active,dd,mode", [(False,"OK","paper"), (True,"PAUSED","paper"), (True,"OK","live")])
async def test_queued_entry_rechecks_pause_and_mode(active, dd, mode):
    from tasks.trading_tasks import _require_approved_entry
    db = AsyncMock()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(
        is_active=active, drawdown_status=dd, mode=mode)
    with pytest.raises(ExposureBlocked):
        await _require_approved_entry(db, "FOREX", "paper")


async def test_simulated_management_independent_of_entry_block():
    context = _context()
    transport = MemoryTransport(context)
    transport.prices[context.instrument.symbol] = Decimal("100")
    conn = SimulatedConnector(context, transport=transport)
    require_position_management(conn)
    entry = transport.create(OrderIntent("existing-position", context, OrderSide.BUY,
                                         OrderType.MARKET, Decimal("1")))
    protection = await conn.modify_position_protection(
        entry.position_id, stop_loss=Decimal("95"), intent_id="protect-existing")
    closed = await conn.reduce_position(entry.position_id, Decimal("1"), intent_id="close-existing")
    assert protection.state is ResultState.CONFIRMED
    assert closed.state is ResultState.CONFIRMED
    with pytest.raises(ExposureBlocked):
        require_position_management(SimpleNamespace(account_nature=AccountNature.REAL))


async def test_dwx_files_only_in_temporary_directory(tmp_path):
    context = _context()
    transport = TemporaryDWXTransport(context)
    transport.prices[context.instrument.symbol] = Decimal("100")
    conn = DWXConnector(str(tmp_path), context=context, transport=transport)
    await conn.create_order(OrderIntent("temporary-dwx", context, OrderSide.BUY,
                                        OrderType.MARKET, Decimal("1")))
    assert list(tmp_path.iterdir()) == []
    assert list(transport.directory.glob("DWX_Commands_*.txt"))
    shutil.rmtree(transport.directory, ignore_errors=True)


async def test_monitor_runs_without_active_scanner_and_does_not_close_real(monkeypatch):
    import pandas as pd
    import redis.asyncio as redis
    import sqlalchemy.ext.asyncio as sa
    from tasks import trading_tasks
    from trading import didi_strategy
    from trading.paper_trader import PaperTrader
    from notifications.telegram import notifier
    from trading.connectors import factory
    trades = [SimpleNamespace(id=i, mode=mode, market="FOREX", symbol="TEST", side="buy",
                              entry_price=Decimal("100"), stop_loss=Decimal("95"))
              for i, mode in [(1, "live"), (2, "paper")]]
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = trades
    db.execute.return_value = result
    session = MagicMock()
    session.return_value.__aenter__.return_value = db
    engine = AsyncMock()
    monkeypatch.setattr(sa, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(sa, "async_sessionmaker", lambda *a, **kw: session)
    cache = AsyncMock()
    cache.get.return_value = None
    monkeypatch.setattr(redis, "from_url", lambda *a, **kw: cache)
    monkeypatch.setattr(trading_tasks, "_load_ohlcv", lambda *a: pd.DataFrame({"close": [100] * 80}))
    monkeypatch.setattr(didi_strategy, "evaluate_safe_break", lambda **kw: SimpleNamespace(should_move=False))
    monkeypatch.setattr(didi_strategy, "evaluate_exit", lambda *a: SimpleNamespace(should_exit=True, criteria={}, details={}))
    monkeypatch.setattr(notifier, "send_message", AsyncMock())
    import notifications.templates as templates
    monkeypatch.setattr(templates, "format_exit_signal_alert", lambda *a: "fake exit")
    closed = AsyncMock()
    monkeypatch.setattr(PaperTrader, "_close_position", closed)
    broker = MagicMock(side_effect=AssertionError("Financial adapter must not be used"))
    monkeypatch.setattr(factory, "get_connector", broker)
    monkeypatch.setattr(settings, "didi_exit_auto_close", True)
    await trading_tasks._monitor_open_positions_async()
    closed.assert_awaited_once_with(trades[1], Decimal("100"), "DIDI_EXIT")
    broker.assert_not_called()
    query = str(db.execute.call_args.args[0])
    assert "bot_config" not in query  # open positions, independent of scanner pause
    assert "trades.status" in query
