from datetime import timedelta
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from freezegun import freeze_time
from sqlalchemy import select

from config import settings
from db.models import Trade, TradeEvent
from paper.entry_time import reconcile_entry_time
from tasks.trading_tasks import _monitor_paper_candles
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_initial_paper_protection import setup, expire_claim
from tests.integration.test_manual_paper_close import opened
from tests.integration.test_paper_monitor import frame, monitor_env
from trading.order_executor import OrderExecutor, OrderExecutionError


@pytest.mark.asyncio
async def test_late_trade_recovery_keeps_fill_time_and_evaluates_prior_stop(effect_sessions, monitor_env, monkeypatch):
    connector, intent_id, request = await setup(effect_sessions)
    connector.create_order = AsyncMock(wraps=connector.create_order)
    async with effect_sessions() as db:
        executor = OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider)
        executor._persist_confirmed_entry = AsyncMock(side_effect=RuntimeError("Trade persistence unavailable"))
        with pytest.raises(OrderExecutionError):
            await executor.open_position(**request)
    receipt = await connector.query_order(intent_id=intent_id)
    filled_at = receipt.executions[0].executed_at
    await expire_claim(effect_sessions)
    with freeze_time(filled_at + timedelta(hours=2), real_asyncio=True):
        async with effect_sessions() as db:
            trade = await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
            trade_id = trade.id
            assert trade.open_at == filled_at
        data = frame().iloc[:77].copy()
        start = pd.Timestamp(filled_at).floor("h") - pd.Timedelta(hours=75)
        data["timestamp"] = pd.date_range(start, periods=len(data), freq="h")
        data.loc[76, "low"] = 94
        await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "closed" and trade.close_reason == "SL_HIT"
        assert trade.open_at == filled_at and trade.paper_monitor_candle_at == data.loc[76, "timestamp"]
    assert connector.create_order.await_count == 1


@pytest.mark.asyncio
async def test_legacy_time_without_cursor_can_be_repaired_idempotently(effect_sessions):
    connector, intent_id, trade_id = await opened(effect_sessions)
    filled_at = (await connector.query_order(intent_id=intent_id)).executions[0].executed_at
    async with effect_sessions() as db:
        (await db.get(Trade, trade_id)).open_at = filled_at + timedelta(hours=2)
        await db.commit()
    assert (await reconcile_entry_time(effect_sessions, intent_id))["status"] == "repair_available"
    assert (await reconcile_entry_time(effect_sessions, intent_id, apply=True))["status"] == "repaired"
    assert (await reconcile_entry_time(effect_sessions, intent_id, apply=True))["status"] == "already_correct"
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.open_at == filled_at and trade.paper_monitor_candle_at is None
        assert len((await db.execute(select(TradeEvent).where(
            TradeEvent.event_type == "ENTRY_TIME_RECONCILED"
        ))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_legacy_time_with_cursor_blocks_instead_of_skipping_history(effect_sessions, monitor_env):
    connector, intent_id, trade_id = await opened(effect_sessions)
    filled_at = (await connector.query_order(intent_id=intent_id)).executions[0].executed_at
    cursor = filled_at.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.open_at = filled_at + timedelta(hours=2)
        trade.paper_monitor_candle_at = cursor
        trade.paper_monitor_timeframe = "1h"
        await db.commit()
    assert (await reconcile_entry_time(effect_sessions, intent_id, apply=True))["status"] == "history_reconciliation_required"
    data = frame()
    start = pd.Timestamp(filled_at).floor("h") - pd.Timedelta(hours=75)
    data["timestamp"] = pd.date_range(start, periods=len(data), freq="h")
    data.loc[78, "low"] = 94
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "open" and trade.paper_monitor_candle_at == cursor
    from notifications.telegram import notifier
    notifier.send_emergency_alert.assert_awaited()


@pytest.mark.asyncio
async def test_time_repair_does_not_reinterpret_a_pending_safe_break(effect_sessions):
    from decimal import Decimal
    from paper.safe_break import stage_safe_break
    connector, intent_id, trade_id = await opened(effect_sessions)
    fill = (await connector.query_order(intent_id=intent_id)).executions[0]
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        snapshot = dict(trade.execution_context)
        arguments = dict(
            position_id=trade.position_id, candle_at=fill.executed_at.replace(minute=0, second=0, microsecond=0),
            timeframe="1h", interval_seconds=3600, previous_cursor=None, previous_timeframe=None,
            old_stop=trade.stop_loss, new_stop=Decimal("101"), take_profit=trade.take_profit,
            entry_price=trade.entry_price, quantity=trade.quantity, levels_version=1, entry_revision=1,
            candle={"open":"100","high":"106","low":"99","close":"106","trigger_rr":"1"},
        )
    assert await stage_safe_break(effect_sessions, trade_id, snapshot, **arguments)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.open_at = fill.executed_at + timedelta(hours=2)
        await db.commit()
    result = await reconcile_entry_time(effect_sessions, intent_id, apply=True)
    assert result["status"] == "history_reconciliation_required"
    async with effect_sessions() as db:
        assert (await db.get(Trade, trade_id)).open_at == fill.executed_at + timedelta(hours=2)
        assert not (await db.execute(select(TradeEvent).where(TradeEvent.event_type == "ENTRY_TIME_RECONCILED"))).scalars().all()
