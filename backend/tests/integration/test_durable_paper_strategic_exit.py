import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import ExecutionIntent, PaperSafeBreakDecision, ProtectionAuthority, Trade
from paper.durable_effects import PaperEffectOperation, PaperEffectPosition, PaperProtectionAttempt
from tasks import trading_tasks
from tasks.trading_tasks import _monitor_paper_candles, _resume_durable_paper_stop
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_durable_paper_monitor_stops import setup_trade, verify
from tests.integration.test_durable_paper_safe_break import setup_monitor
from tests.integration.test_manual_paper_close import expire_close_claim
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_paper_monitor import frame, monitor_env
from trading.connectors.simulated import SimulatedConnector
from trading.contracts import ResultState
from trading.order_executor import OrderExecutor


@pytest.fixture
def strategic_signal(monitor_env, monkeypatch):
    from trading import didi_strategy

    def enable():
        monkeypatch.setattr(settings, "didi_exit_auto_close", True)
        monkeypatch.setattr(didi_strategy, "evaluate_exit", lambda *args: SimpleNamespace(
            should_exit=True, criteria={"didi_reverse_with_trend": True}, details={"adx_now": "25.00"},
        ))
    return enable


async def pending_state(sessions, intent_id, trade_id, candle, quantity):
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        attempt = (await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.operation_id.like("manual-close:%")
        ))).scalar_one()
        assert trade.status == "open" and trade.pnl_net is None
        assert trade.paper_monitor_candle_at == candle.to_pydatetime()
        assert intent.capital_amount == Decimal("500") and intent.risk_amount == Decimal("20")
        assert position.quantity == Decimal(quantity)
        assert attempt.payload["reason"] == "DIDI_EXIT"
        assert attempt.payload["revision"] == 1
        assert attempt.payload["levels_version"] == 1
        assert attempt.payload["paper_costs"] == trade.execution_context["paper_costs"]
        return dict(attempt.payload)


@pytest.mark.asyncio
async def test_strategic_exit_closes_durable_paper_position(effect_sessions, monitor_env, strategic_signal):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    strategic_signal()
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], "DIDI_EXIT", "100")


@pytest.mark.asyncio
async def test_two_workers_choose_one_strategic_exit(effect_sessions, monitor_env, strategic_signal):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    strategic_signal()
    await asyncio.gather(*[
        _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
        for _ in range(2)
    ])
    await _resume_durable_paper_stop(effect_sessions, trade_id)
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], "DIDI_EXIT", "100")


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_stop_precedes_strategy_and_strategy_precedes_safe_break(
    effect_sessions, monitor_env, strategic_signal, stop,
):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    strategic_signal()
    data.loc[76, ["high", "close"]] = [106, 106]
    if stop:
        data.loc[76, "low"] = 94
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await verify(
        effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"],
        "SL_HIT" if stop else "DIDI_EXIT", "95" if stop else "106",
    )
    async with effect_sessions() as db:
        assert (await db.get(ProtectionAuthority, intent_id)).version == 1
        assert not (await db.execute(select(PaperSafeBreakDecision))).scalars().all()


@pytest.mark.asyncio
async def test_committed_decision_survives_crash_before_effect(
    effect_sessions, monitor_env, strategic_signal, monkeypatch,
):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    strategic_signal()
    original = trading_tasks._execute_durable_paper_stop

    async def crash(*args, **kwargs):
        raise RuntimeError("interrupted after decision commit")

    monkeypatch.setattr(trading_tasks, "_execute_durable_paper_stop", crash)
    with pytest.raises(RuntimeError, match="after decision commit"):
        await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    payload = await pending_state(effect_sessions, intent_id, trade_id, data.loc[75, "timestamp"], "1")
    assert payload["candle"] == data.loc[76, "timestamp"].to_pydatetime().isoformat()
    assert payload["cursor"] == data.loc[75, "timestamp"].to_pydatetime().isoformat()
    assert payload["decision_context"] == {
        "candle": {"open": "100.0", "high": "101.0", "low": "99.0", "close": "100.0", "volume": "1.0"},
        "criteria": {"didi_reverse_with_trend": True},
        "details": {"adx_now": "25.00"},
    }
    async with effect_sessions() as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(text(
                "UPDATE paper_protection_attempts SET payload = payload || "
                "'{\"reason\":\"changed\"}'::jsonb WHERE operation_id LIKE 'manual-close:%'"
            ))
        await db.rollback()
    monkeypatch.setattr(trading_tasks, "_execute_durable_paper_stop", original)
    assert await _resume_durable_paper_stop(effect_sessions, trade_id)
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], "DIDI_EXIT", "100")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "accounting", "commit", "partial", "uncertain"])
async def test_strategic_effect_is_recovered_without_reapplication(
    effect_sessions, monitor_env, strategic_signal, monkeypatch, failure,
):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    strategic_signal()
    original_reduce = SimulatedConnector.reduce_position
    original_finalize = OrderExecutor._finalize_trade
    original_commit = AsyncSession.commit

    async def interrupted(self, *args, **kwargs):
        result = await original_reduce(self, *args, **kwargs)
        if failure == "response":
            raise TimeoutError("response lost after durable effect")
        if failure == "partial":
            return replace(result, state=ResultState.PARTIAL, filled=Decimal("0.5"))
        return replace(result, state=ResultState.UNKNOWN)

    def failed_accounting(self, *args, **kwargs):
        original_finalize(self, *args, **kwargs)
        raise RuntimeError("accounting failed after effect")

    async def failed_commit(self):
        if any(isinstance(item, Trade) and item.status == "closed" for item in self.identity_map.values()):
            await self.flush()
            raise RuntimeError("accounting transaction failed before commit")
        await original_commit(self)

    if failure == "accounting":
        monkeypatch.setattr(OrderExecutor, "_finalize_trade", failed_accounting)
    elif failure == "commit":
        monkeypatch.setattr(AsyncSession, "commit", failed_commit)
    else:
        monkeypatch.setattr(SimulatedConnector, "reduce_position", interrupted)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    await pending_state(effect_sessions, intent_id, trade_id, data.loc[75, "timestamp"], "0")
    async with effect_sessions() as db:
        assert len((await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id.like("manual-close:%")
        ))).scalars().all()) == 1
    monkeypatch.setattr(OrderExecutor, "_finalize_trade", original_finalize)
    monkeypatch.setattr(AsyncSession, "commit", original_commit)
    await expire_close_claim(effect_sessions)

    async def no_reapply(*args, **kwargs):
        raise AssertionError("existing receipt must be consulted before reduction")

    monkeypatch.setattr(SimulatedConnector, "reduce_position", no_reapply)
    assert await _resume_durable_paper_stop(effect_sessions, trade_id)
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], "DIDI_EXIT", "100")


@pytest.mark.asyncio
async def test_strategic_takeover_fences_stale_worker(effect_sessions, monitor_env, strategic_signal, monkeypatch):
    data = frame()
    connector, intent_id, trade_id = await setup_trade(effect_sessions, data)
    strategic_signal()
    original = trading_tasks._execute_durable_paper_stop

    async def crash(*args, **kwargs):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(trading_tasks, "_execute_durable_paper_stop", crash)
    with pytest.raises(RuntimeError, match="interrupted"):
        await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    effects = connector._transport.durable_effects
    position_id = "paper_" + intent_id
    kwargs = dict(
        trade_id=trade_id, quantity=Decimal("1"), reason="DIDI_EXIT",
        execution_price=Decimal("100"), candle=data.loc[76, "timestamp"].to_pydatetime(),
        timeframe="1h", previous_cursor=data.loc[75, "timestamp"].to_pydatetime(),
        previous_timeframe="1h",
    )
    operation_id, old_fence, _ = await effects.prepare_manual_close(position_id, owner="old", **kwargs)
    await expire_close_claim(effect_sessions)
    _, new_fence, _ = await effects.prepare_manual_close(position_id, owner="new", **kwargs)
    assert new_fence > old_fence
    with pytest.raises(ValueError, match="Stale fencing"):
        await connector.reduce_position(
            position_id, Decimal("1"), intent_id=operation_id, fence=old_fence,
            execution_price=Decimal("100"), owner="old",
        )
    await pending_state(effect_sessions, intent_id, trade_id, data.loc[75, "timestamp"], "1")
    await expire_close_claim(effect_sessions)
    monkeypatch.setattr(trading_tasks, "_execute_durable_paper_stop", original)
    assert await _resume_durable_paper_stop(effect_sessions, trade_id)
    await verify(effect_sessions, intent_id, trade_id, data.loc[76, "timestamp"], "DIDI_EXIT", "100")


@pytest.mark.asyncio
async def test_stale_exposure_revision_does_not_finalize_strategic_receipt(
    effect_sessions, monitor_env, strategic_signal, monkeypatch,
):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    strategic_signal()
    original = SimulatedConnector.reduce_position

    async def lost(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise TimeoutError("lost response")

    monkeypatch.setattr(SimulatedConnector, "reduce_position", lost)
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        intent.observation_revision += 1
        await db.commit()
    await expire_close_claim(effect_sessions)
    monkeypatch.setattr(SimulatedConnector, "reduce_position", original)
    assert await _resume_durable_paper_stop(effect_sessions, trade_id)
    await pending_state(effect_sessions, intent_id, trade_id, data.loc[75, "timestamp"], "0")


@pytest.mark.asyncio
async def test_sell_strategic_exit_uses_candle_close_and_costs(effect_sessions, monitor_env, strategic_signal):
    _, intent_id, trade_id, data, provider = await setup_monitor(effect_sessions, "sell")
    strategic_signal()
    await _monitor_paper_candles(
        effect_sessions, trade_id, data, settings, monitor_env,
        evidence_provider=provider, evidence_clock=provider.clock,
    )
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        decision = (await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.operation_id.like("manual-close:%")
        ))).scalar_one()
        receipt = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id == decision.operation_id
        ))).scalar_one()
        assert position.quantity == 0
        assert trade.status == "closed" and trade.close_reason == "DIDI_EXIT"
        assert trade.paper_monitor_candle_at == data.loc[76, "timestamp"]
        assert trade.exit_price == Decimal("94.001")
        assert trade.pnl_gross == Decimal("6.00")
        assert trade.commission == Decimal("0.19") and trade.pnl_net == Decimal("5.80")
        assert intent.capital_amount == Decimal("500") and intent.risk_amount == Decimal("20")
        assert receipt.payload == decision.payload and Decimal(decision.payload["execution_price"]) == 94
        assert not (await db.execute(select(PaperSafeBreakDecision))).scalars().all()


@pytest.mark.asyncio
async def test_invalid_strategic_price_does_not_advance_cursor(effect_sessions, monitor_env, strategic_signal):
    data = frame()
    _, intent_id, trade_id = await setup_trade(effect_sessions, data)
    strategic_signal()
    data.loc[76, "close"] = 100.5
    await _monitor_paper_candles(effect_sessions, trade_id, data, settings, monitor_env)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert trade.status == "open" and trade.paper_monitor_candle_at == data.loc[75, "timestamp"]
        assert (await db.execute(select(PaperEffectPosition))).scalar_one().quantity == 1
        assert intent.capital_amount == Decimal("500") and intent.risk_amount == Decimal("20")
        assert not (await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.operation_id.like("manual-close:%")
        ))).scalars().all()
