import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pandas as pd
from sqlalchemy import select, text

from config import settings
from db.models import ExecutionIntent, Trade, TradeEvent
from paper.durable_effects import PaperEffectOperation, PaperEffectPosition, PaperProtectionAttempt
from tasks.trading_tasks import _monitor_paper_candles, _resume_durable_paper_stop
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_manual_paper_close import opened
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_paper_monitor import frame, monitor_env
from trading.connectors.simulated import SimulatedConnector
from trading.contracts import ResultState
from trading.order_executor import OrderExecutor


async def setup_trade(sessions, data):
    connector, intent_id, trade_id = await opened(sessions)
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        start = pd.Timestamp(trade.open_at).floor("h") - pd.Timedelta(hours=75)
        data["timestamp"] = pd.date_range(start, periods=len(data), freq="h")
        trade.paper_monitor_candle_at = data.loc[75, "timestamp"].to_pydatetime()
        trade.paper_monitor_timeframe = "1h"
        await db.commit()
    return connector, intent_id, trade_id


async def verify(sessions, intent_id, trade_id, candle, reason, raw_price):
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        decisions = (await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.operation_id.like("manual-close:%")
        ))).scalars().all()
        receipts = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id.like("manual-close:%")
        ))).scalars().all()
        events = (await db.execute(select(TradeEvent).where(
            TradeEvent.trade_id == trade_id, TradeEvent.event_type == reason
        ))).scalars().all()
        price = Decimal(raw_price)
        simulated_exit = price - Decimal("0.001")
        gross = simulated_exit - Decimal("100.001")
        commission = (Decimal("100.001") + simulated_exit) * Decimal("0.001")
        assert position.quantity == 0
        assert trade.status == "closed" and trade.close_reason == reason
        assert trade.paper_monitor_candle_at == candle.to_pydatetime()
        assert trade.paper_monitor_timeframe == "1h"
        assert trade.exit_price == simulated_exit
        assert trade.pnl_gross == gross.quantize(Decimal("0.01"))
        assert trade.commission == commission.quantize(Decimal("0.01"))
        assert trade.pnl_net == (gross - commission).quantize(Decimal("0.01"))
        assert intent.status == "confirmed" and intent.order_terminal is True
        assert intent.risk_amount == Decimal("20") and intent.capital_amount == Decimal("500")
        assert len(decisions) == len(receipts) == len(events) == 1
        assert decisions[0].payload == receipts[0].payload
        assert decisions[0].payload["candle"] == candle.to_pydatetime().isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("bar,reason,price", [
    ({"low": 94}, "SL_HIT", "95"),
    ({"high": 111}, "TP_HIT", "110"),
    ({"low": 94, "high": 111}, "SL_HIT", "95"),
    ({"open": 90, "low": 89, "high": 100, "close": 91}, "SL_HIT", "90"),
])
async def test_monitor_durable_stop_target(effect_sessions, monitor_env, bar, reason, price):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    for column, value in bar.items():
        data.loc[76, column] = value
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], reason, price)


@pytest.mark.asyncio
async def test_monitor_accumulated_candles_and_gap(effect_sessions, monitor_env):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    data.loc[78, "low"] = 94
    missing = data.drop(index=77)
    await _monitor_paper_candles(effect_sessions, trade_id, missing, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "open" and trade.paper_monitor_candle_at == data.loc[76, "timestamp"]
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await verify(effect_sessions, intent_id, trade_id, data.loc[78, "timestamp"], "SL_HIT", "95")


@pytest.mark.asyncio
async def test_two_monitor_workers(effect_sessions, monitor_env):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    data.loc[76, "low"] = 94
    await asyncio.gather(*[
        _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
        for _ in range(2)
    ])
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], "SL_HIT", "95")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "accounting"])
async def test_monitor_recovers_effect_after_interruption(effect_sessions, monitor_env, monkeypatch, failure):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    data.loc[76, "low"] = 94
    if failure == "response":
        original = SimulatedConnector.reduce_position

        async def lost(self, *args, **kwargs):
            await original(self, *args, **kwargs)
            raise TimeoutError("response lost after effect")
        monkeypatch.setattr(SimulatedConnector, "reduce_position", lost)
    else:
        original = OrderExecutor._finalize_trade

        def fail(*args, **kwargs):
            raise RuntimeError("accounting failed after effect")
        monkeypatch.setattr(OrderExecutor, "_finalize_trade", fail)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert trade.status == "open" and trade.pnl_net is None
        assert trade.paper_monitor_candle_at == data.loc[75, "timestamp"]
        assert position.quantity == 0
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second' "
            "WHERE operation_id LIKE 'manual-close:%'"
        ))
        await db.commit()
    if failure == "response":
        monkeypatch.setattr(SimulatedConnector, "reduce_position", original)
    else:
        monkeypatch.setattr(OrderExecutor, "_finalize_trade", original)
    data.loc[76, "low"] = 99
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], "SL_HIT", "95")


@pytest.mark.asyncio
async def test_partial_response_preserves_cursor_and_recovers_without_market_data(
    effect_sessions, monitor_env, monkeypatch,
):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    data.loc[76, "high"] = 111
    original = SimulatedConnector.reduce_position

    async def partial(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        return replace(result, state=ResultState.PARTIAL, filled=Decimal("0.5"))

    monkeypatch.setattr(SimulatedConnector, "reduce_position", partial)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert trade.status == "open" and trade.paper_monitor_candle_at == data.loc[75, "timestamp"]
        assert trade.pnl_net is None
        assert intent.capital_amount == Decimal("500") and intent.risk_amount == Decimal("20")
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second' "
            "WHERE operation_id LIKE 'manual-close:%'"
        ))
        await db.commit()
    monkeypatch.setattr(SimulatedConnector, "reduce_position", original)
    assert await _resume_durable_paper_stop(effect_sessions, trade_id)
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], "TP_HIT", "110")


@pytest.mark.asyncio
async def test_active_monitor_task_recovers_before_loading_candles(effect_sessions, monitor_env, monkeypatch):
    import redis.asyncio as redis
    import sqlalchemy.ext.asyncio as sqlalchemy_async
    from tasks import trading_tasks

    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    data.loc[76, "low"] = 94
    original = SimulatedConnector.reduce_position

    async def lost(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise TimeoutError("lost response")

    monkeypatch.setattr(SimulatedConnector, "reduce_position", lost)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second' "
            "WHERE operation_id LIKE 'manual-close:%'"
        ))
        await db.commit()
    monkeypatch.setattr(SimulatedConnector, "reduce_position", original)
    monkeypatch.setattr(sqlalchemy_async, "create_async_engine", lambda *args, **kwargs: effect_sessions.kw["bind"])
    monkeypatch.setattr(redis, "from_url", lambda *args, **kwargs: monitor_env)

    def unavailable(*args, **kwargs):
        raise AssertionError("recovery must not require market data")

    monkeypatch.setattr(trading_tasks, "_load_fresh_ohlcv", unavailable)
    await trading_tasks._monitor_open_positions_async()
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], "SL_HIT", "95")


@pytest.mark.asyncio
async def test_pending_decision_is_reused_after_failure_before_effect(effect_sessions, monitor_env, monkeypatch):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    data.loc[76, "low"] = 94
    original = SimulatedConnector.reduce_position

    async def unavailable(*args, **kwargs):
        raise TimeoutError("before effect")

    monkeypatch.setattr(SimulatedConnector, "reduce_position", unavailable)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    data.loc[76, "low"] = 99
    data.loc[76, "high"] = 111
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert trade.status == "open" and trade.paper_monitor_candle_at == data.loc[75, "timestamp"]
        assert position.quantity == 1
        assert len((await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.operation_id.like("manual-close:%")
        ))).scalars().all()) == 1
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second' "
            "WHERE operation_id LIKE 'manual-close:%'"
        ))
        await db.commit()
    monkeypatch.setattr(SimulatedConnector, "reduce_position", original)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], "SL_HIT", "95")


@pytest.mark.asyncio
async def test_real_durable_safe_break_without_provider_stays_pending(effect_sessions, monitor_env):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    data.loc[76, ["high", "close"]] = [106, 106]
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert trade.status == "open" and trade.stop_loss == Decimal("101")
        assert trade.paper_monitor_candle_at == data.loc[75, "timestamp"]
        assert position.quantity == 1 and position.stop_loss == Decimal("95")
        assert intent.status == "protection_pending"
        assert not (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id.like("manual-close:%")
        ))).scalars().all()


@pytest.mark.asyncio
async def test_real_durable_strategic_close_remains_disabled_by_default(effect_sessions, monitor_env, monkeypatch):
    from trading import didi_strategy

    data = frame()
    _, _, trade_id = await setup_trade(effect_sessions, data)
    monkeypatch.setattr(didi_strategy, "evaluate_exit", lambda *args: SimpleNamespace(
        should_exit=True, criteria={}, details={}
    ))
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert trade.status == "open" and trade.paper_monitor_candle_at == data.loc[79, "timestamp"]
        assert position.quantity == 1
        assert not (await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.operation_id.like("manual-close:%")
        ))).scalars().all()
