import asyncio
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from config import settings
from db.models import ExecutionIntent, PaperSafeBreakDecision, ProtectionAuthority, Trade, TradeEvent
from paper.durable_effects import DurablePaperEffects, PaperEffectOperation, PaperEffectPosition, PaperProtectionAttempt
from tasks.trading_tasks import _monitor_paper_candles, _resume_durable_safe_break
from tests.integration.paper_protection_helpers import PostFillSyntheticProvider
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_initial_paper_protection import setup
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_paper_monitor import frame, monitor_env
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.types import OrderSide
from trading.order_executor import OrderExecutionError, OrderExecutor


async def setup_monitor(sessions, side="buy", *, partial=False):
    connector, intent_id, request = await setup(sessions, partial=partial)
    if side == "sell":
        old_connector = connector
        context = replace(connector.context, position_model="hedging")
        effects = DurablePaperEffects(
            sessions, context, evidence_clock=connector._test_evidence_provider.clock,
        )
        transport = MemoryTransport(context, durable_effects=effects)
        transport.prices[context.instrument.symbol] = Decimal("100")
        connector = SimulatedConnector(context, transport=transport)
        connector._test_evidence_provider = old_connector._test_evidence_provider
        async with sessions() as db:
            intent = (await db.execute(select(ExecutionIntent).where(
                ExecutionIntent.intent_id == intent_id
            ))).scalar_one()
            intent.side = "sell"
            await db.commit()
        request.update(side=OrderSide.SELL, stop_loss=Decimal("105"), take_profit=Decimal("90"))
        connector._test_evidence_provider.reference = "94"
        connector._test_evidence_provider.history_low = "92"
    async with sessions() as db:
        executor = OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider)
        if partial:
            with pytest.raises(OrderExecutionError, match="partially executed"):
                await executor.open_position(**request)
            trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        else:
            trade = await executor.open_position(**request)
        trade_id = trade.id
    data = frame()
    fill = (await connector.query_order(intent_id=intent_id)).executions[0]
    start = pd.Timestamp(fill.executed_at).floor("h") - pd.Timedelta(hours=75)
    data["timestamp"] = pd.date_range(start, periods=len(data), freq="h")
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.open_at = fill.executed_at
        trade.paper_monitor_candle_at = data.loc[75, "timestamp"].to_pydatetime()
        trade.paper_monitor_timeframe = "1h"
        await db.commit()
    provider = PostFillSyntheticProvider(
        reference="106" if side == "buy" else "94",
        history_low="104" if side == "buy" else "92",
    ).bind_clock(connector._test_evidence_provider.clock)
    if side == "buy":
        data.loc[76, ["high", "close"]] = [106, 106]
        data.loc[77, ["open", "high", "low", "close"]] = [105, 106, 100, 102]
    else:
        data.loc[76, ["low", "close"]] = [94, 94]
        data.loc[77, ["open", "high", "low", "close"]] = [95, 100, 94, 98]
    return connector, intent_id, trade_id, data, provider


async def monitor(sessions, trade_id, data, env, provider):
    await _monitor_paper_candles(
        sessions, trade_id, data, settings, env,
        evidence_provider=provider, evidence_clock=provider.clock,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("side,stop", [("buy", "101"), ("sell", "99")])
async def test_safe_break_is_durable_and_stop_applies_next_candle(effect_sessions, monitor_env, side, stop):
    _, intent_id, trade_id, data, provider = await setup_monitor(effect_sessions, side)
    await monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, provider)
    await monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, provider)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        authority = await db.get(ProtectionAuthority, intent_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        decision = (await db.execute(select(PaperSafeBreakDecision))).scalar_one()
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert trade.status == "open" and position.quantity == Decimal("1")
        assert trade.stop_loss == authority.stop_loss == position.stop_loss == Decimal(stop)
        assert authority.version == trade.protection_levels_version == decision.levels_version == 2
        assert decision.entry_revision == 1 and decision.exposure_quantity == Decimal("1")
        assert decision.entry_price == trade.entry_price
        assert authority.levels_effective_at == data.loc[77, "timestamp"].to_pydatetime()
        assert authority.levels_recorded_at is not None
        assert authority.levels_recorded_at < authority.levels_effective_at
        assert decision.effective_at == authority.levels_effective_at
        assert intent.status == "confirmed"
        assert trade.paper_monitor_candle_at == data.loc[76, "timestamp"]
        events = (await db.execute(select(TradeEvent).where(
            TradeEvent.event_type == "SAFE_BREAK_ARMED"
        ))).scalars().all()
        assert len(events) == 1
        assert len((await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.payload["kind"].astext == "protect",
        ))).scalars().all()) == 2
    await monitor(effect_sessions, trade_id, data.loc[:77], monitor_env, provider)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        authority = await db.get(ProtectionAuthority, intent_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert trade.status == "closed" and trade.close_reason == "SL_HIT"
        assert trade.paper_monitor_candle_at == data.loc[77, "timestamp"]
        assert position.quantity == 0 and authority.stop_loss == Decimal(stop)


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_stop_on_arming_candle_precedes_safe_break(effect_sessions, monitor_env, side):
    _, intent_id, trade_id, data, provider = await setup_monitor(effect_sessions, side)
    data.loc[76, "low" if side == "buy" else "high"] = 94 if side == "buy" else 106
    await monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, provider)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        authority = await db.get(ProtectionAuthority, intent_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert trade.status == "closed" and trade.close_reason == "SL_HIT"
        assert position.quantity == 0 and authority.version == 1
        assert not (await db.execute(select(PaperSafeBreakDecision))).scalars().all()


@pytest.mark.asyncio
async def test_two_workers_and_repetition_use_one_decision(effect_sessions, monitor_env):
    _, intent_id, trade_id, data, provider = await setup_monitor(effect_sessions)
    await asyncio.gather(*[
        monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, provider)
        for _ in range(2)
    ])
    await monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, provider)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        authority = await db.get(ProtectionAuthority, intent_id)
        decisions = (await db.execute(select(PaperSafeBreakDecision))).scalars().all()
        receipts = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect",
        ))).scalars().all()
        events = (await db.execute(select(TradeEvent).where(
            TradeEvent.event_type == "SAFE_BREAK_ARMED"
        ))).scalars().all()
        assert len(decisions) == len(events) == 1 and len(receipts) == 2
        assert trade.stop_loss == position.stop_loss == authority.stop_loss == Decimal("101")
        assert trade.paper_monitor_candle_at == data.loc[76, "timestamp"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "accounting"])
async def test_safe_break_recovers_same_receipt_after_failure(
    effect_sessions, monitor_env, monkeypatch, failure,
):
    _, intent_id, trade_id, data, provider = await setup_monitor(effect_sessions)
    if failure == "response":
        original = SimulatedConnector.modify_position_protection

        async def fail(self, *args, **kwargs):
            await original(self, *args, **kwargs)
            raise TimeoutError("response lost after protection")

        monkeypatch.setattr(SimulatedConnector, "modify_position_protection", fail)
    else:
        original = DurablePaperEffects.release_initial_protection_claim

        async def fail(*args, **kwargs):
            raise RuntimeError("accounting failed after protection")

        monkeypatch.setattr(DurablePaperEffects, "release_initial_protection_claim", fail)
    await monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, provider)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        authority = await db.get(ProtectionAuthority, intent_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert trade.stop_loss == position.stop_loss == authority.stop_loss == Decimal("101")
        assert trade.paper_monitor_candle_at == data.loc[75, "timestamp"]
        assert intent.status == "protection_pending"
        assert intent.risk_amount == Decimal("20") and intent.capital_amount == Decimal("500")
        assert len((await db.execute(select(PaperSafeBreakDecision))).scalars().all()) == 1
        assert len((await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()) == 2
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second' "
            "WHERE owner IS NOT NULL"
        ))
        await db.commit()
    if failure == "response":
        monkeypatch.setattr(SimulatedConnector, "modify_position_protection", original)
    else:
        monkeypatch.setattr(DurablePaperEffects, "release_initial_protection_claim", original)
    unavailable = AsyncMock()
    unavailable.evidence_for = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    resumed, success = await _resume_durable_safe_break(
        effect_sessions, trade_id, evidence_provider=unavailable, evidence_clock=provider.clock,
    )
    assert resumed and success
    unavailable.evidence_for.assert_not_awaited()
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert trade.paper_monitor_candle_at == data.loc[76, "timestamp"]
        assert intent.status == "confirmed"
        assert len((await db.execute(select(PaperSafeBreakDecision))).scalars().all()) == 1
        assert len((await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["crossed", "incomplete"])
async def test_insufficient_evidence_preserves_previous_stop_and_reservation(
    effect_sessions, monitor_env, kind,
):
    from notifications.telegram import notifier

    _, intent_id, trade_id, data, provider = await setup_monitor(effect_sessions)
    class IncompleteProvider(PostFillSyntheticProvider):
        async def evidence_for(self, request):
            evidence = await super().evidence_for(request)
            evidence["history"] = evidence["history"][1:]
            return evidence

    unavailable = (
        PostFillSyntheticProvider(reference="100", history_low="99")
        if kind == "crossed" else IncompleteProvider(reference="106", history_low="104")
    ).bind_clock(provider.clock)
    await monitor(effect_sessions, trade_id, data.loc[:77], monitor_env, unavailable)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        authority = await db.get(ProtectionAuthority, intent_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert trade.stop_loss == authority.stop_loss == Decimal("101")
        assert position.stop_loss == Decimal("95") and position.quantity == Decimal("1")
        assert trade.status == "open" and trade.paper_monitor_candle_at == data.loc[75, "timestamp"]
        assert intent.status == "protection_pending"
        assert intent.risk_amount == Decimal("20") and intent.capital_amount == Decimal("500")
        assert len((await db.execute(select(PaperSafeBreakDecision))).scalars().all()) == 1
    notifier.send_emergency_alert.assert_awaited()


@pytest.mark.asyncio
async def test_pending_desired_stop_cannot_reduce_calculated_risk(effect_sessions, monitor_env):
    from risk.manager import RiskManager

    _, intent_id, trade_id, data, provider = await setup_monitor(effect_sessions)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.risk_amount = None
        trade.capital_amount = None
        await db.commit()
        previous_risk, previous_capital = RiskManager._trade_budget(trade)
    crossed = PostFillSyntheticProvider(reference="100", history_low="99").bind_clock(provider.clock)
    await monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, crossed)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        risk, capital = RiskManager._trade_budget(trade)
        assert risk >= previous_risk and risk >= intent.risk_amount
        assert capital >= previous_capital and capital >= intent.capital_amount
        assert intent.status == "protection_pending"
        assert trade.stop_loss == Decimal("101") and position.stop_loss == Decimal("95")


@pytest.mark.asyncio
async def test_missing_durable_reservation_blocks_safe_break(effect_sessions, monitor_env):
    from notifications.telegram import notifier

    _, intent_id, trade_id, data, provider = await setup_monitor(effect_sessions)
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        intent.risk_amount = None
        await db.commit()
    await monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, provider)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert trade.stop_loss == position.stop_loss == Decimal("95")
        assert trade.paper_monitor_candle_at == data.loc[75, "timestamp"]
        assert not (await db.execute(select(PaperSafeBreakDecision))).scalars().all()
    notifier.send_emergency_alert.assert_awaited()


@pytest.mark.asyncio
async def test_old_safe_break_receipt_cannot_confirm_new_levels(effect_sessions, monitor_env, monkeypatch):
    _, intent_id, trade_id, data, provider = await setup_monitor(effect_sessions)
    original = SimulatedConnector.modify_position_protection

    async def lost(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise TimeoutError("response lost after protection")

    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", lost)
    await monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, provider)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.stop_loss = Decimal("102")
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        intent.status = "protection_pending"
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second' "
            "WHERE owner IS NOT NULL"
        ))
        await db.commit()
    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", original)
    resumed, success = await _resume_durable_safe_break(
        effect_sessions, trade_id, evidence_provider=provider, evidence_clock=provider.clock,
    )
    assert resumed and not success
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        authority = await db.get(ProtectionAuthority, intent_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert authority.version == 3 and trade.protection_levels_version == 3
        assert trade.stop_loss == authority.stop_loss == Decimal("102")
        assert position.stop_loss == Decimal("101")
        assert intent.status == "protection_pending"
        assert trade.paper_monitor_candle_at == data.loc[75, "timestamp"]


@pytest.mark.asyncio
async def test_old_receipt_cannot_confirm_new_fill_revision(effect_sessions, monitor_env, monkeypatch):
    from paper.protection_recovery import recover_pending_paper_protection

    connector, intent_id, trade_id, data, provider = await setup_monitor(effect_sessions, partial=True)
    original = SimulatedConnector.modify_position_protection

    async def lost(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise TimeoutError("response lost after protection")

    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", lost)
    await monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, provider)
    await connector.observe_paper_fill(
        intent_id, filled=Decimal("1"), average=Decimal("100.001"), terminal=True, revision=2,
    )
    async with effect_sessions() as db:
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second' "
            "WHERE owner IS NOT NULL"
        ))
        await db.commit()
    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", original)
    resumed, success = await _resume_durable_safe_break(
        effect_sessions, trade_id, evidence_provider=provider, evidence_clock=provider.clock,
    )
    assert resumed and not success
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert position.quantity == Decimal("1") and trade.quantity == Decimal("0.5")
        assert position.stop_loss == Decimal("101")
        assert intent.status == "protection_pending"
        assert trade.paper_monitor_candle_at == data.loc[75, "timestamp"]
    recovered = await recover_pending_paper_protection(
        effect_sessions, evidence_provider=provider, evidence_clock=provider.clock,
    )
    assert recovered["confirmed"] == 1
    resumed, success = await _resume_durable_safe_break(
        effect_sessions, trade_id, evidence_provider=provider, evidence_clock=provider.clock,
    )
    assert resumed and success
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert trade.quantity == Decimal("1") and intent.observation_revision == 2
        assert intent.status == "confirmed"
        assert trade.paper_monitor_candle_at == data.loc[76, "timestamp"]
        assert len((await db.execute(select(PaperSafeBreakDecision))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_safe_break_decision_is_immutable(effect_sessions, monitor_env):
    _, _, trade_id, data, provider = await setup_monitor(effect_sessions)
    await monitor(effect_sessions, trade_id, data.loc[:76], monitor_env, provider)
    async with effect_sessions() as db:
        decision = (await db.execute(select(PaperSafeBreakDecision))).scalar_one()
        decision.new_stop = Decimal("102")
        with pytest.raises(DBAPIError, match="immutable"):
            await db.commit()
