import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from db.models import ExecutionIntent, Trade
from paper.durable_effects import PaperEffectOperation
from paper.durable_effects import PaperProtectionEvidenceRecord
from tests.integration.paper_protection_helpers import (
    ControlledEvidenceClock, PostFillSyntheticProvider, register_fixed_provider,
)
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_initial_paper_protection import setup
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_protection_effect_authority import apply, pending_attempt
from tests.integration.test_protection_claim import expire
from trading.order_executor import OrderExecutionError, OrderExecutor
from trading.contracts import ExecutionResult, ResultState


@pytest.fixture
async def run_task(effect_sessions, monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine
    import sqlalchemy.ext.asyncio
    from tasks.trading_tasks import recover_paper_protection
    import paper.protection_recovery as recovery
    original_recover = recovery.recover_pending_paper_protection
    url = str(effect_sessions.kw["bind"].url)
    async with effect_sessions() as db:
        schema_name = (await db.execute(text("SELECT current_schema()"))).scalar_one()

    def isolated_engine(_url):
        return create_async_engine(url, connect_args={"server_settings": {"search_path": schema_name}})

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", isolated_engine)
    async def run(provider=None, evidence_clock=None):
        if provider is not None:
            register_fixed_provider(monkeypatch, provider)
            async def recover_with_test_clock(sessions, **kwargs):
                kwargs["evidence_clock"] = evidence_clock
                return await original_recover(sessions, **kwargs)

            monkeypatch.setattr(recovery, "recover_pending_paper_protection", recover_with_test_clock)
        return await asyncio.to_thread(recover_paper_protection.run)
    return run


@pytest.mark.asyncio
async def test_real_task_reprotects_additional_fill_and_is_idempotent(effect_sessions, run_task):
    connector, intent_id, request = await setup(effect_sessions, partial=True)
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="protection pending"):
            await OrderExecutor(connector, db).open_position(**request)
    await connector.observe_paper_fill(
        intent_id, filled=Decimal("1"), average=Decimal("100"), terminal=True, revision=2,
    )
    await expire(effect_sessions)
    evidence_clock = ControlledEvidenceClock()
    provider = PostFillSyntheticProvider().bind_clock(evidence_clock)
    first = await run_task(provider, evidence_clock)
    assert first == {"selected": 1, "confirmed": 1, "deferred": 0, "failed": 0}
    position = (await connector.list_positions())[0]
    assert position.quantity == Decimal("1") and position.stop_loss == Decimal("95")
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status == "confirmed" and intent.observation_revision == 2
        evidence = (await db.execute(select(PaperProtectionEvidenceRecord))).scalar_one()
        assert evidence.assessment["state"] == "valid"
        assert evidence.payload["evidence"]["reference"]["source"] == "synthetic-test"
        effects = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()
        assert len(effects) == 1
        assert effects[0].payload["revision"] == intent.observation_revision == 2
    second = await run_task(provider, evidence_clock)
    assert second["selected"] == 0


@pytest.mark.asyncio
async def test_task_defers_busy_claim_then_takeover_fences_old_worker(effect_sessions, run_task):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    first = await run_task(provider, provider.clock)
    assert first["deferred"] == 1 and first["failed"] == 0
    assert (await connector.list_positions())[0].stop_loss is None
    await expire(effect_sessions)
    second = await run_task(provider, provider.clock)
    assert second["confirmed"] == 1
    with pytest.raises(ValueError, match="Stale fencing"):
        await apply(connector, data, provider)
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status == "confirmed"


@pytest.mark.asyncio
async def test_task_after_lost_reply_uses_receipt_without_effect(effect_sessions, run_task, monkeypatch):
    connector, _, data, provider = await pending_attempt(effect_sessions)
    await apply(connector, data, provider)
    await expire(effect_sessions)
    from trading.connectors.simulated import SimulatedConnector
    effect = AsyncMock(side_effect=AssertionError("receipt should avoid effect"))
    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", effect)
    result = await run_task(provider, provider.clock)
    assert result["confirmed"] == 1
    effect.assert_not_awaited()
    async with effect_sessions() as db:
        effects = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()
        assert len(effects) == 1


@pytest.mark.asyncio
async def test_two_task_workers_do_not_duplicate_effect(effect_sessions, run_task):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    await expire(effect_sessions)
    results = await asyncio.gather(run_task(provider, provider.clock), run_task(provider, provider.clock))
    assert sum(result["confirmed"] for result in results) >= 1
    assert all(result["failed"] == 0 for result in results)
    async with effect_sessions() as db:
        effects = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()
        assert len(effects) == 1
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status == "confirmed"
        assert intent.status != "protection_pending"
    position = (await connector.list_positions())[0]
    assert position.quantity == Decimal("1")
    assert position.stop_loss == Decimal("95")
    assert position.take_profit == Decimal("110")
    assert (await run_task())["selected"] == 0


@pytest.mark.asyncio
async def test_task_recovers_entry_after_trade_commit_failed(effect_sessions, run_task):
    connector, intent_id, _, provider = await pending_attempt(effect_sessions, no_trade=True)
    await expire(effect_sessions)
    result = await run_task(provider, provider.clock)
    assert result["confirmed"] == 1
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert trade.position_id == "paper_" + intent_id and intent.status == "confirmed"
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")


@pytest.mark.asyncio
async def test_task_recovers_missing_trade_after_protection_was_applied(effect_sessions, run_task, monkeypatch):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions, no_trade=True)
    await apply(connector, data, provider)
    await expire(effect_sessions)
    from trading.connectors.simulated import SimulatedConnector
    effect = AsyncMock(side_effect=AssertionError("persisted receipt should be reused"))
    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", effect)
    result = await run_task(provider, provider.clock)
    assert result["confirmed"] == 1
    effect.assert_not_awaited()
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        assert trade.position_id == "paper_" + intent_id
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")


@pytest.mark.asyncio
async def test_levels_changed_during_effect_stay_pending_until_takeover(effect_sessions, run_task, monkeypatch):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    await expire(effect_sessions)
    from trading.connectors.simulated import SimulatedConnector
    original = SimulatedConnector.modify_position_protection
    changed = False

    async def change_before_effect(self, *args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            async with self._transport.durable_effects._sessions() as db:
                trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
                trade.stop_loss = Decimal("94")
                await db.commit()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", change_before_effect)
    result = await run_task(provider, provider.clock)
    assert result["confirmed"] == 0
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status == "protection_pending"
    assert (await connector.list_positions())[0].stop_loss is None
    await expire(effect_sessions)
    result = await run_task(provider, provider.clock)
    assert result["confirmed"] == 1
    assert (await connector.list_positions())[0].stop_loss == Decimal("94")


@pytest.mark.asyncio
async def test_one_uncertain_position_does_not_block_another(effect_sessions, run_task, monkeypatch):
    first, first_id, first_request = await setup(effect_sessions, partial=True)
    second, second_id, second_request = await setup(effect_sessions, partial=True)
    for connector, request in ((first, first_request), (second, second_request)):
        async with effect_sessions() as db:
            with pytest.raises(OrderExecutionError, match="protection pending"):
                await OrderExecutor(connector, db).open_position(**request)
    for connector, intent_id in ((first, first_id), (second, second_id)):
        await connector.observe_paper_fill(
            intent_id, filled=Decimal("1"), average=Decimal("100"), terminal=True, revision=2,
        )
    await expire(effect_sessions)
    from trading.connectors.simulated import SimulatedConnector
    original = SimulatedConnector.query_order

    async def unavailable_one(self, *, intent_id=None, order_id=None):
        if intent_id == first_id:
            return ExecutionResult(first_id, ResultState.UNAVAILABLE, reason="test unavailable")
        return await original(self, intent_id=intent_id, order_id=order_id)

    monkeypatch.setattr(SimulatedConnector, "query_order", unavailable_one)
    evidence_clock = ControlledEvidenceClock()
    provider = PostFillSyntheticProvider().bind_clock(evidence_clock)
    result = await run_task(provider, evidence_clock)
    assert result["failed"] == result["confirmed"] == 1
    async with effect_sessions() as db:
        rows = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id.in_((first_id, second_id))
        ))).scalars().all()
        statuses = {row.intent_id: row.status for row in rows}
        assert statuses == {first_id: "protection_pending", second_id: "confirmed"}


@pytest.mark.asyncio
async def test_new_fill_during_effect_stays_pending_until_current_revision(effect_sessions, run_task, monkeypatch):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions, partial=True)
    await expire(effect_sessions)
    from trading.connectors.simulated import SimulatedConnector
    original = SimulatedConnector.modify_position_protection
    changed = False

    async def fill_before_effect(self, *args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            await self.observe_paper_fill(
                intent_id, filled=Decimal("1"), average=Decimal("100"), terminal=True, revision=2,
            )
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", fill_before_effect)
    result = await run_task(provider, provider.clock)
    assert result["confirmed"] == 0
    position = (await connector.list_positions())[0]
    assert position.quantity == Decimal("1") and position.stop_loss is None
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status == "protection_pending"
    await expire(effect_sessions)
    result = await run_task(provider, provider.clock)
    assert result["confirmed"] == 1
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")


@pytest.mark.asyncio
async def test_task_finds_changed_levels_on_confirmed_trade(effect_sessions, run_task):
    connector, intent_id, request = await setup(effect_sessions)
    provider = connector._test_evidence_provider
    async with effect_sessions() as db:
        await OrderExecutor(connector, db, evidence_provider=provider).open_position(**request)
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        trade.stop_loss = Decimal("94")
        await db.commit()
    result = await run_task(provider, connector._transport.durable_effects._evidence_clock)
    assert result["confirmed"] == 1
    assert (await connector.list_positions())[0].stop_loss == Decimal("94")
