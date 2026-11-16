import asyncio
import logging
import os
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import make_url, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import settings
from database import Base
from db.models import ExecutionIntent, ProtectionAuthority, Trade, TradeEvent
from tasks.trading_tasks import _monitor_paper_candles


@pytest_asyncio.fixture
async def paper_sessions():
    url = make_url(settings.database_url)
    endpoint = f"{url.host}:{url.port or 5432}"
    if endpoint not in os.environ.get("AUTOXTRADE_TEST_ENDPOINTS", "").split(","):
        pytest.skip("isolated PostgreSQL endpoint is not explicitly enabled")
    admin = create_async_engine(str(url))
    tenant = None
    schema = f"paper_monitor_test_{uuid4().hex}"
    try:
        try:
            async with admin.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (OSError, DBAPIError):
            pytest.skip("isolated PostgreSQL endpoint is unavailable")
        async with admin.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        tenant = create_async_engine(
            str(url), connect_args={"server_settings": {"search_path": schema}}
        )
        async with tenant.begin() as connection:
            await connection.run_sync(
                Base.metadata.create_all,
                tables=[ExecutionIntent.__table__, ProtectionAuthority.__table__, Trade.__table__, TradeEvent.__table__]
            )
        yield async_sessionmaker(tenant, expire_on_commit=False)
    finally:
        if tenant is not None:
            await tenant.dispose()
            async with admin.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


@pytest.fixture
def monitor_env(monkeypatch):
    from notifications.telegram import notifier
    from trading import didi_strategy

    monkeypatch.setattr(settings, "didi_timeframe", "1h")
    monkeypatch.setattr(settings, "didi_safe_break_rr", 1)
    monkeypatch.setattr(settings, "didi_exit_auto_close", False)
    monkeypatch.setattr(
        didi_strategy, "evaluate_exit",
        lambda *args: SimpleNamespace(should_exit=False, criteria={}, details={}),
    )
    monkeypatch.setattr(notifier, "send_message", AsyncMock())
    monkeypatch.setattr(notifier, "send_emergency_alert", AsyncMock())
    return AsyncMock()


def frame():
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=80, freq="h", tz="UTC"),
        "open": [100.0] * 80,
        "high": [101.0] * 80,
        "low": [99.0] * 80,
        "close": [100.0] * 80,
        "volume": [1.0] * 80,
    })


async def seed(sessions, opened):
    async with sessions() as db:
        trade = Trade(
            market="FOREX", exchange="test", symbol="TEST", side="buy", status="open",
            mode="paper", entry_price=Decimal("100"), quantity=Decimal("1"),
            entry_value=Decimal("100"), stop_loss=Decimal("95"),
            take_profit=Decimal("110"), open_at=opened,
        )
        db.add(trade)
        await db.commit()
        return trade.id


async def state(sessions, trade_id):
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        events = (await db.execute(
            select(TradeEvent).where(TradeEvent.trade_id == trade_id).order_by(TradeEvent.id)
        )).scalars().all()
        return trade, events


async def test_legacy_cursor_decision_is_idempotent_and_preserves_block(
    paper_sessions, monitor_env,
):
    from trading.paper_cursor_reconciliation import (
        list_blocked_paper_cursors, record_paper_cursor_decision,
    )

    data = frame()
    trade_id = await seed(paper_sessions, data.loc[75, "timestamp"].to_pydatetime())
    async with paper_sessions() as db:
        db.add(TradeEvent(
            trade_id=trade_id, event_type="SAFE_BREAK_ARMED",
            payload={"new_stop_loss": "100"},
        ))
        trade = await db.get(Trade, trade_id)
        trade.stop_loss = Decimal("100")
        await db.commit()
    blocked = await list_blocked_paper_cursors(paper_sessions)
    assert [item.trade_id for item in blocked] == [trade_id]
    decision_id = str(uuid4())
    decision = dict(
        decision_id=decision_id, operator="reviewer", reason="histórico insuficiente",
        evidence="registro do safe break sem candle ou stop anterior",
    )
    assert await record_paper_cursor_decision(paper_sessions, trade_id, **decision)
    assert not await record_paper_cursor_decision(paper_sessions, trade_id, **decision)
    with pytest.raises(ValueError, match="payload diferente"):
        await record_paper_cursor_decision(
            paper_sessions, trade_id, **{**decision, "reason": "mudança"},
        )
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.paper_monitor_candle_at is None
    assert trade.stop_loss == Decimal("100")
    assert [event.event_type for event in events] == [
        "SAFE_BREAK_ARMED", "PAPER_CURSOR_DECISION",
    ]
    assert events[-1].payload["decision"] == "keep_blocked"
    assert [item.trade_id for item in await list_blocked_paper_cursors(paper_sessions)] == [trade_id]


async def test_legacy_cursor_decisions_are_serialized(paper_sessions):
    from trading.paper_cursor_reconciliation import record_paper_cursor_decision

    data = frame()
    trade_id = await seed(paper_sessions, data.loc[75, "timestamp"].to_pydatetime())
    async with paper_sessions() as db:
        db.add(TradeEvent(trade_id=trade_id, event_type="SAFE_BREAK_ARMED", payload={}))
        await db.commit()
    decision = dict(
        decision_id=str(uuid4()), operator="reviewer", reason="dados incompletos",
        evidence="evento sem contexto",
    )
    results = await asyncio.gather(*[
        record_paper_cursor_decision(paper_sessions, trade_id, **decision) for _ in range(2)
    ])
    assert sorted(results) == [False, True]
    _, events = await state(paper_sessions, trade_id)
    assert sum(event.event_type == "PAPER_CURSOR_DECISION" for event in events) == 1


async def test_legacy_cursor_alert_lists_trade_and_caches_only_delivery(paper_sessions, caplog):
    from tasks.trading_tasks import _alert_legacy_paper_cursors

    data = frame()
    trade_id = await seed(paper_sessions, data.loc[75, "timestamp"].to_pydatetime())
    async with paper_sessions() as db:
        db.add(TradeEvent(trade_id=trade_id, event_type="SAFE_BREAK_ARMED", payload={}))
        await db.commit()
    redis = AsyncMock()
    redis.get.return_value = None
    notifier = SimpleNamespace(send_message=AsyncMock(return_value=False))
    await _alert_legacy_paper_cursors(paper_sessions, redis, notifier)
    assert str(trade_id) in notifier.send_message.await_args.args[0]
    assert "paper_cursor_reconciliation list" in notifier.send_message.await_args.args[0]
    redis.setex.assert_not_awaited()
    assert "Alerta não entregue" in caplog.text
    notifier.send_message.return_value = True
    await _alert_legacy_paper_cursors(paper_sessions, redis, notifier)
    redis.setex.assert_awaited_once()


async def test_identified_paper_position_blocks_stop_close(paper_sessions, monitor_env):
    data = frame()
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime())
    async with paper_sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.position_id = "paper_test"
        await db.commit()
    data.loc[76, "close"] = 94
    data.loc[76, "low"] = 94
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.status == "open"
    assert trade.paper_monitor_candle_at is None
    assert not events


async def test_identified_paper_position_blocks_safe_break(paper_sessions, monitor_env):
    data = frame()
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime())
    async with paper_sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.position_id = "paper_test"
        await db.commit()
    data.loc[76, "close"] = 106
    data.loc[76, "high"] = 106
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.stop_loss == Decimal("95")
    assert trade.paper_monitor_candle_at is None
    assert not events


async def test_identified_paper_position_blocks_direct_and_strategic_close(
    paper_sessions, monitor_env, monkeypatch,
):
    from trading import didi_strategy
    from trading.paper_trader import PaperTrader
    from trading.safety import ExposureBlocked

    data = frame()
    trade_id = await seed(paper_sessions, data.loc[75, "timestamp"].to_pydatetime())
    async with paper_sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.position_id = "paper_test"
        trade.paper_monitor_candle_at = data.loc[75, "timestamp"].to_pydatetime()
        trade.paper_monitor_timeframe = "1h"
        await db.commit()
        with pytest.raises(ExposureBlocked, match="durable close requires reconciliation"):
            await PaperTrader(db)._close_position(trade, Decimal("100"), "manual")
    monkeypatch.setattr(settings, "didi_exit_auto_close", True)
    monkeypatch.setattr(
        didi_strategy, "evaluate_exit",
        lambda *args: SimpleNamespace(should_exit=True, criteria={}, details={}),
    )
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.status == "open"
    assert trade.paper_monitor_candle_at == data.loc[75, "timestamp"].to_pydatetime()
    assert not events


async def test_replay_ignores_pre_entry_extremes_and_prioritizes_sl(
    paper_sessions, monitor_env,
):
    data = frame()
    data.loc[75:76, ["high", "low"]] = [120, 90]
    data.loc[77, ["high", "low"]] = [115, 94]
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime() + timedelta(minutes=30))

    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.status == "closed"
    assert trade.close_reason == "SL_HIT"
    assert trade.exit_price == Decimal("95")
    assert trade.paper_monitor_candle_at == data.loc[77, "timestamp"]
    assert [event.event_type for event in events] == ["SL_HIT"]


async def test_overlapping_candle_close_is_only_observable_price(paper_sessions, monitor_env):
    data = frame()
    data.loc[76, ["open", "high", "low", "close"]] = [80, 120, 75, 90]
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime() + timedelta(minutes=30))
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, _ = await state(paper_sessions, trade_id)
    assert trade.close_reason == "SL_HIT"
    assert trade.exit_price == Decimal("90")
    assert trade.paper_monitor_candle_at == data.loc[76, "timestamp"]


async def test_overlapping_candle_does_not_evaluate_exit_indicators(
    paper_sessions, monitor_env, monkeypatch,
):
    from trading import didi_strategy

    data = frame()
    data.loc[76, ["high", "low"]] = [120, 90]
    trade_id = await seed(
        paper_sessions, data.loc[76, "timestamp"].to_pydatetime() + timedelta(minutes=30)
    )
    evaluated = []

    def exit_signal(window, side):
        evaluated.append(window["timestamp"].iloc[-1])
        return SimpleNamespace(should_exit=False, criteria={}, details={})

    monkeypatch.setattr(didi_strategy, "evaluate_exit", exit_signal)
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.status == "open"
    assert evaluated == data.loc[77:79, "timestamp"].tolist()
    assert not events


async def test_resume_after_interruption_and_replay_is_idempotent(
    paper_sessions, monitor_env, monkeypatch,
):
    from trading import didi_strategy

    data = frame()
    data.loc[78, "low"] = 94
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime())
    calls = 0

    def interrupt_on_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("worker interrupted")
        return SimpleNamespace(should_exit=False, criteria={}, details={})

    monkeypatch.setattr(didi_strategy, "evaluate_exit", interrupt_on_second)
    with pytest.raises(RuntimeError, match="worker interrupted"):
        await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.status == "open"
    assert trade.paper_monitor_candle_at == data.loc[76, "timestamp"]
    assert not events

    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.close_reason == "SL_HIT"
    assert trade.paper_monitor_candle_at == data.loc[78, "timestamp"]
    assert [event.event_type for event in events] == ["SL_HIT"]


async def test_two_workers_close_once(paper_sessions, monitor_env):
    data = frame()
    data.loc[77, ["open", "low"]] = [90, 89]
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime())
    async with paper_sessions() as lock_session:
        await lock_session.execute(select(Trade).where(Trade.id == trade_id).with_for_update())
        workers = [
            asyncio.create_task(
                _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
            )
            for _ in range(2)
        ]
        await asyncio.sleep(0.1)
        assert all(not worker.done() for worker in workers)
        await lock_session.commit()
        await asyncio.gather(*workers)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.status == "closed"
    assert trade.close_reason == "SL_HIT"
    assert trade.exit_price == Decimal("90")
    assert trade.paper_monitor_candle_at == data.loc[77, "timestamp"]
    assert [event.event_type for event in events] == ["SL_HIT"]


async def test_crash_after_close_commit_does_not_duplicate_event(
    paper_sessions, monitor_env, monkeypatch,
):
    from trading.paper_trader import PaperTrader

    data = frame()
    data.loc[77, "low"] = 94
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime())
    original = PaperTrader._close_position

    async def crash_after_commit(self, trade, exit_price, reason):
        await original(self, trade, exit_price, reason)
        raise RuntimeError("worker died after commit")

    monkeypatch.setattr(PaperTrader, "_close_position", crash_after_commit)
    with pytest.raises(RuntimeError, match="worker died after commit"):
        await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.status == "closed"
    assert trade.paper_monitor_candle_at == data.loc[77, "timestamp"]
    assert [event.event_type for event in events] == ["SL_HIT"]


async def test_gap_blocks_cursor_until_missing_candle_is_restored(
    paper_sessions, monitor_env, caplog,
):
    data = frame()
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime())
    missing = data.drop(index=77).reset_index(drop=True)
    with caplog.at_level(logging.ERROR):
        await _monitor_paper_candles(paper_sessions, trade_id, missing, settings, monitor_env)
    trade, _ = await state(paper_sessions, trade_id)
    assert trade.paper_monitor_candle_at == data.loc[76, "timestamp"]
    assert "lacuna de candles" in caplog.text
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, _ = await state(paper_sessions, trade_id)
    assert trade.paper_monitor_candle_at == data.loc[79, "timestamp"]


async def test_missing_entry_candle_does_not_initialize_cursor(
    paper_sessions, monitor_env, caplog,
):
    data = frame()
    trade_id = await seed(
        paper_sessions, data.loc[76, "timestamp"].to_pydatetime() + timedelta(minutes=30)
    )
    with caplog.at_level(logging.ERROR):
        await _monitor_paper_candles(
            paper_sessions, trade_id, data.drop(index=76).reset_index(drop=True),
            settings, monitor_env,
        )
    trade, _ = await state(paper_sessions, trade_id)
    assert trade.paper_monitor_candle_at is None
    assert "lacuna de candles" in caplog.text


async def test_changed_timeframe_blocks_cursor(paper_sessions, monitor_env, monkeypatch, caplog):
    data = frame()
    trade_id = await seed(paper_sessions, data.loc[78, "timestamp"].to_pydatetime())
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    monkeypatch.setattr(settings, "didi_timeframe", "2h")
    with caplog.at_level(logging.ERROR):
        await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, _ = await state(paper_sessions, trade_id)
    assert trade.paper_monitor_candle_at == data.loc[79, "timestamp"]
    assert trade.paper_monitor_timeframe == "1h"
    assert "timeframe do cursor mudou" in caplog.text


async def test_safe_break_applies_only_to_next_candle(paper_sessions, monitor_env):
    data = frame()
    data.loc[76, ["high", "low", "close"]] = [106, 99, 105]
    data.loc[77, ["high", "low"]] = [101, 99]
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime())
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.stop_loss == Decimal("100")
    assert trade.close_reason == "SL_HIT"
    assert trade.paper_monitor_candle_at == data.loc[77, "timestamp"]
    assert [event.event_type for event in events] == ["SAFE_BREAK_ARMED", "SL_HIT"]


async def test_legacy_safe_break_does_not_apply_to_prior_candles(
    paper_sessions, monitor_env, caplog,
):
    data = frame()
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime())
    async with paper_sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.stop_loss = Decimal("100")
        db.add(TradeEvent(
            trade=trade,
            event_type="SAFE_BREAK_ARMED",
            payload={"new_stop_loss": "100"},
        ))
        await db.commit()
    with caplog.at_level(logging.ERROR):
        await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, events = await state(paper_sessions, trade_id)
    assert trade.status == "open"
    assert trade.paper_monitor_candle_at is None
    assert [event.event_type for event in events] == ["SAFE_BREAK_ARMED"]
    assert "reconciliação" in caplog.text


@pytest.mark.parametrize(
    "bar,expected_price",
    [
        ({"low": 97}, Decimal("97.6")),
        ({"high": 105}, Decimal("104.8")),
        ({"low": 97, "high": 105}, Decimal("97.6")),
        ({"open": 95, "low": 94}, Decimal("95")),
    ],
)
async def test_ml_replay_matches_monitor_stop_target(
    paper_sessions, monitor_env, bar, expected_price,
):
    from paper.backtest import replay_signals

    data = frame()
    data["atr"] = 2.0
    for column, value in bar.items():
        data.loc[77, column] = value
    trade_id = await seed(paper_sessions, data.loc[76, "timestamp"].to_pydatetime())
    async with paper_sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.stop_loss = Decimal("97.6")
        trade.take_profit = Decimal("104.8")
        await db.commit()
    await _monitor_paper_candles(paper_sessions, trade_id, data, settings, monitor_env)
    trade, _ = await state(paper_sessions, trade_id)
    signals = ["NEUTRO"] * len(data)
    signals[75] = "LONG"
    replay = replay_signals(
        data, signals, forward_candles=5, spread_price=Decimal("0"),
        commission_rate=Decimal("0"), slippage_price=Decimal("0"),
        initial_capital=Decimal("10000"),
    )
    assert trade.exit_price == expected_price
    assert replay["trades"][0]["exit"] == float(expected_price)
    assert replay["trades"][0]["reason"] == trade.close_reason
    assert replay["trades"][0]["exit_time"] == str(data.loc[77, "timestamp"])
