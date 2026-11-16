import asyncio
from decimal import Decimal

import pytest
import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from config import settings
from db.models import EntryApproval, ExecutionIntent, Trade, TradeEvent
from paper import partial_stop
from paper.durable_effects import PaperEffectOperation, PaperEffectPosition, PaperProtectionAttempt
from tasks import trading_tasks
from tasks.trading_tasks import _monitor_paper_candles, _resume_durable_paper_stop
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_initial_paper_protection import setup
from tests.integration.test_paper_monitor import frame, monitor_env
from tests.integration.test_manual_paper_close import expire_close_claim
from trading.order_executor import OrderExecutor, OrderExecutionError


async def expire_partial_claim(sessions):
    async with sessions() as db:
        await db.execute(text("UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second' WHERE operation_id LIKE 'partial-stop:%'"))
        await db.commit()


async def partial_trade(sessions, side="buy"):
    connector, intent_id, request = await setup(sessions, partial=True)
    if side == "sell":
        from dataclasses import replace
        from paper.durable_effects import DurablePaperEffects
        from trading.connectors.memory_transport import MemoryTransport
        from trading.connectors.simulated import SimulatedConnector
        from trading.connectors.types import OrderSide
        provider = connector._test_evidence_provider
        context = replace(connector.context, position_model="hedging")
        transport = MemoryTransport(context, durable_effects=DurablePaperEffects(
            sessions, context, evidence_clock=provider.clock,
        ))
        transport.prices[context.instrument.symbol] = Decimal("100")
        transport.scenarios[intent_id] = Decimal("0.5")
        connector = SimulatedConnector(context, transport=transport)
        connector._test_evidence_provider = provider
        request.update(side=OrderSide.SELL, stop_loss=Decimal("105"), take_profit=Decimal("90"))
        async with sessions() as db:
            intent = (await db.execute(select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id))).scalar_one()
            intent.side = "sell"
            await db.commit()
    async with sessions() as db:
        with pytest.raises(OrderExecutionError, match="partially executed"):
            await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        data = frame()
        start = pd.Timestamp(trade.open_at).floor("h") - pd.Timedelta(hours=75)
        data["timestamp"] = pd.date_range(start, periods=len(data), freq="h")
        trade.paper_monitor_candle_at = data.loc[75, "timestamp"].to_pydatetime()
        trade.paper_monitor_timeframe = "1h"
        trade_id = trade.id
        await db.commit()
    return connector, intent_id, trade_id, data


async def assert_closed(sessions, intent_id, trade_id, data, reason, quantity="0.5"):
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id))).scalar_one()
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        receipts = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "cancel-entry-stop"
        ))).scalars().all()
        close_receipts = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id.like("manual-close:%")
        ))).scalars().all()
        events = (await db.execute(select(TradeEvent).where(TradeEvent.event_type == reason))).scalars().all()
        assert trade.status == "closed" and trade.close_reason == reason and position.quantity == 0
        assert trade.quantity == intent.filled_quantity == Decimal(quantity)
        assert intent.order_terminal is True and intent.status == "partial"
        assert intent.risk_amount == 20 and intent.capital_amount == 500
        assert trade.paper_monitor_candle_at == data.loc[76, "timestamp"]
        assert trade.pnl_net is not None and trade.commission > 0
        assert len(receipts) == len(close_receipts) == len(events) == 1
        assert receipts[0].result["terminal"] is True
        assert (await db.execute(select(EntryApproval))).scalar_one().status != "executed"
        assert close_receipts[0].payload["paper_costs"] == receipts[0].payload["paper_costs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,column,value", [("SL_HIT", "low", 94), ("TP_HIT", "high", 111)])
async def test_partial_stop_cancels_remainder_and_closes(effect_sessions, monitor_env, reason, column, value):
    connector, intent_id, trade_id, data = await partial_trade(effect_sessions)
    data.loc[76, column] = value
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await assert_closed(effect_sessions, intent_id, trade_id, data, reason)
    with pytest.raises(ValueError, match="cancelled"):
        await connector.observe_paper_fill(intent_id, filled=Decimal("1"), average=Decimal("100"), terminal=True, revision=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("filled", ["0.75", "1"])
async def test_fill_before_cancel_is_included_and_fill_after_is_rejected(effect_sessions, monitor_env, monkeypatch, filled):
    connector, intent_id, trade_id, data = await partial_trade(effect_sessions)
    data.loc[76, "low"] = 94
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = partial_stop.cancel_partial_entry
    async def paused(*args, **kwargs):
        entered.set()
        await proceed.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(partial_stop, "cancel_partial_entry", paused)
    worker = asyncio.create_task(_monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env))
    await asyncio.wait_for(entered.wait(), 10)
    await connector.observe_paper_fill(intent_id, filled=Decimal(filled), average=Decimal("100"), terminal=filled == "1", revision=2)
    proceed.set()
    await asyncio.wait_for(worker, 10)
    await assert_closed(effect_sessions, intent_id, trade_id, data, "SL_HIT", filled)
    with pytest.raises(ValueError, match="cancelled"):
        await connector.observe_paper_fill(intent_id, filled=Decimal("1"), average=Decimal("100"), terminal=True, revision=4)


@pytest.mark.asyncio
async def test_two_workers_cancel_and_close_once(effect_sessions, monitor_env):
    _, intent_id, trade_id, data = await partial_trade(effect_sessions)
    data.loc[76, "low"] = 94
    await asyncio.gather(*[_monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env) for _ in range(2)])
    await _resume_durable_paper_stop(effect_sessions, trade_id)
    await assert_closed(effect_sessions, intent_id, trade_id, data, "SL_HIT")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel-response", "before-close", "close-accounting"])
async def test_partial_stop_recovers_between_effects(effect_sessions, monitor_env, monkeypatch, failure):
    _, intent_id, trade_id, data = await partial_trade(effect_sessions)
    data.loc[76, "low"] = 94
    original_cancel = partial_stop.cancel_partial_entry
    original_stage = trading_tasks._stage_durable_paper_close
    original_finalize = OrderExecutor._finalize_trade
    async def lost(*args, **kwargs):
        await original_cancel(*args, **kwargs)
        raise TimeoutError("cancellation response lost")
    async def crash(*args, **kwargs):
        raise RuntimeError("crash before close decision")
    def accounting(*args, **kwargs):
        raise RuntimeError("accounting interrupted")
    if failure == "cancel-response":
        monkeypatch.setattr(partial_stop, "cancel_partial_entry", lost)
    elif failure == "before-close":
        monkeypatch.setattr(trading_tasks, "_stage_durable_paper_close", crash)
    else:
        monkeypatch.setattr(OrderExecutor, "_finalize_trade", accounting)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "open" and trade.paper_monitor_candle_at == data.loc[75, "timestamp"]
        intent = (await db.execute(select(ExecutionIntent))).scalar_one()
        assert intent.order_terminal is True and intent.risk_amount == 20
    monkeypatch.setattr(partial_stop, "cancel_partial_entry", original_cancel)
    monkeypatch.setattr(trading_tasks, "_stage_durable_paper_close", original_stage)
    monkeypatch.setattr(OrderExecutor, "_finalize_trade", original_finalize)
    await expire_close_claim(effect_sessions)
    assert await _resume_durable_paper_stop(effect_sessions, trade_id)
    await assert_closed(effect_sessions, intent_id, trade_id, data, "SL_HIT")


@pytest.mark.asyncio
async def test_partial_stop_takeover_rejects_old_worker_and_receipt_mutation(effect_sessions, monitor_env, monkeypatch):
    connector, intent_id, trade_id, data = await partial_trade(effect_sessions)
    data.loc[76, "low"] = 94
    original = partial_stop.cancel_partial_entry
    async def paused(*args, **kwargs):
        raise RuntimeError("pause before cancellation")
    monkeypatch.setattr(partial_stop, "cancel_partial_entry", paused)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    effects = connector._transport.durable_effects
    position_id = "paper_" + intent_id
    await expire_partial_claim(effect_sessions)
    _, old = await partial_stop.claim_partial_stop(effects, position_id, trade_id, "old")
    await expire_partial_claim(effect_sessions)
    _, new = await partial_stop.claim_partial_stop(effects, position_id, trade_id, "new")
    assert new > old
    with pytest.raises(ValueError, match="Stale"):
        await original(effects, position_id, trade_id, "old", old)
    await original(effects, position_id, trade_id, "new", new)
    async with effect_sessions() as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(text("UPDATE paper_effect_operations SET result = '{}'::jsonb WHERE operation_id LIKE 'partial-stop:%'"))
        await db.rollback()
    monkeypatch.setattr(partial_stop, "cancel_partial_entry", original)
    assert await _resume_durable_paper_stop(effect_sessions, trade_id)
    await assert_closed(effect_sessions, intent_id, trade_id, data, "SL_HIT")


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,column,value", [("SL_HIT", "high", 106), ("TP_HIT", "low", 89)])
async def test_sell_partial_stop(effect_sessions, monitor_env, reason, column, value):
    _, intent_id, trade_id, data = await partial_trade(effect_sessions, "sell")
    data.loc[76, column] = value
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await assert_closed(effect_sessions, intent_id, trade_id, data, reason)


@pytest.mark.asyncio
async def test_cancel_waits_for_the_fill_position_lock(effect_sessions, monitor_env, monkeypatch):
    connector, intent_id, trade_id, data = await partial_trade(effect_sessions)
    data.loc[76, "low"] = 94
    original = partial_stop.cancel_partial_entry
    async def pause(*args, **kwargs):
        raise RuntimeError("pause before effect")
    monkeypatch.setattr(partial_stop, "cancel_partial_entry", pause)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await expire_partial_claim(effect_sessions)
    effects = connector._transport.durable_effects
    position_id = "paper_" + intent_id
    _, fence = await partial_stop.claim_partial_stop(effects, position_id, trade_id, "lock-test")
    async with effect_sessions() as db:
        await effects._locked_position(db, position_id)
        blocker = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        worker = asyncio.create_task(original(effects, position_id, trade_id, "lock-test", fence))
        try:
            async def wait_until_blocked():
                while True:
                    async with effect_sessions() as observer:
                        waiting = (await observer.execute(text(
                            "SELECT count(*) FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))"
                        ), {"pid": blocker})).scalar_one()
                    if waiting:
                        return
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_until_blocked(), 5)
            assert not worker.done()
            await db.commit()
            await asyncio.wait_for(worker, 5)
        finally:
            await db.rollback()
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
    monkeypatch.setattr(partial_stop, "cancel_partial_entry", original)
    assert await _resume_durable_paper_stop(effect_sessions, trade_id)
    await assert_closed(effect_sessions, intent_id, trade_id, data, "SL_HIT")


@pytest.mark.asyncio
async def test_cancelled_stop_cannot_be_replaced_by_manual_close(effect_sessions, monitor_env, monkeypatch):
    connector, intent_id, trade_id, data = await partial_trade(effect_sessions)
    data.loc[76, "low"] = 94
    original = trading_tasks._stage_durable_paper_close
    async def crash(*args, **kwargs):
        raise RuntimeError("pause after cancellation")
    monkeypatch.setattr(trading_tasks, "_stage_durable_paper_close", crash)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        with pytest.raises(OrderExecutionError, match="pending"):
            await OrderExecutor(connector, db).close_position(trade, execution_price=Decimal("100"))
    monkeypatch.setattr(trading_tasks, "_stage_durable_paper_close", original)
    assert await _resume_durable_paper_stop(effect_sessions, trade_id)
    await assert_closed(effect_sessions, intent_id, trade_id, data, "SL_HIT")
