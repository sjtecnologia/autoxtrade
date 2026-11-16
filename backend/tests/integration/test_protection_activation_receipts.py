from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from db.models import ExecutionIntent, ProtectionAuthority, Trade
from paper.durable_effects import PaperEffectOperation, PaperProtectionEvidenceRecord
from paper.protection_evidence_store import collect_protection_evidence
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_initial_paper_protection import expire_claim, setup
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_protection_effect_authority import apply, pending_attempt
from tests.integration.paper_protection_helpers import PostFillSyntheticProvider
from trading.order_executor import OrderExecutionError, OrderExecutor


class DelayedProvider(PostFillSyntheticProvider):
    async def evidence_for(self, request):
        data = await super().evidence_for(request)
        fill = request.executions[0]
        delayed = fill.executed_at.replace(second=0, microsecond=0) + timedelta(minutes=2)
        candle_start = fill.executed_at.replace(second=0, microsecond=0)
        for candle in data["history"]:
            if candle["opened_at"] == candle_start.isoformat():
                candle["low"] = "94"
        data["stop_effective_at"] = delayed.isoformat()
        data["intervals"] = []
        return data


class CoveredCrossingProvider(DelayedProvider):
    async def evidence_for(self, request):
        data = await super().evidence_for(request)
        fill = request.executions[0]
        boundary = fill.executed_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
        data["intervals"] = [{
            "execution_id": fill.execution_id,
            "started_at": fill.executed_at.isoformat(),
            "ended_at": boundary.isoformat(),
            "low": "94", "high": "102", "source": "synthetic-test",
        }]
        return data


class NoActivationProvider(PostFillSyntheticProvider):
    async def evidence_for(self, request):
        data = await super().evidence_for(request)
        data.pop("stop_effective_at")
        return data


@pytest.mark.asyncio
async def test_provider_cannot_postpone_fill_activation_and_hide_crossing(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    provider = DelayedProvider().bind_clock(connector._test_evidence_provider.clock)
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="protection pending"):
            await OrderExecutor(connector, db, evidence_provider=provider).open_position(**request)
    assert (await connector.list_positions())[0].stop_loss is None
    async with effect_sessions() as db:
        record = (await db.execute(select(PaperProtectionEvidenceRecord))).scalar_one()
        assert record.assessment["state"] in {"stop_crossed", "incomplete_or_ambiguous"}
        assert not (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()


@pytest.mark.asyncio
async def test_crossing_after_fill_and_before_suggested_activation_is_detected(effect_sessions):
    connector, _, request = await setup(effect_sessions)
    provider = CoveredCrossingProvider().bind_clock(connector._test_evidence_provider.clock)
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="protection pending"):
            await OrderExecutor(connector, db, evidence_provider=provider).open_position(**request)
    async with effect_sessions() as db:
        evidence = (await db.execute(select(PaperProtectionEvidenceRecord))).scalar_one()
        assert evidence.assessment["state"] == "stop_crossed"
        assert len(evidence.assessment["crossed_execution_ids"]) == 1
    assert (await connector.list_positions())[0].stop_loss is None


@pytest.mark.asyncio
async def test_initial_activation_comes_from_fill_when_provider_omits_it(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    provider = NoActivationProvider().bind_clock(connector._test_evidence_provider.clock)
    async with effect_sessions() as db:
        await OrderExecutor(connector, db, evidence_provider=provider).open_position(**request)
        evidence = (await db.execute(select(PaperProtectionEvidenceRecord))).scalar_one()
    fill = (await connector.query_order(intent_id=intent_id)).executions[0]
    assert evidence.payload["evidence"]["stop_effective_at"] == fill.executed_at.isoformat()
    assert evidence.assessment["state"] == "valid"


@pytest.mark.asyncio
async def test_changed_authority_without_versioned_effective_time_is_blocked(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        trade.stop_loss = Decimal("94")
        await db.commit()
        authority = await db.get(ProtectionAuthority, intent_id)
        assert authority.version == 2 and authority.levels_effective_at is not None
        await db.execute(text("ALTER TABLE protection_authorities DISABLE TRIGGER autoxtrade_guard_protection_authority"))
        await db.execute(text(
            "UPDATE protection_authorities SET levels_effective_at = NULL WHERE intent_id = :intent_id"
        ), {"intent_id": intent_id})
        await db.execute(text("ALTER TABLE protection_authorities ENABLE TRIGGER autoxtrade_guard_protection_authority"))
        await db.commit()
    await expire_claim(effect_sessions)
    effects = connector._transport.durable_effects
    with pytest.raises(ValueError, match="effective|vigência"):
        await effects.prepare_initial_protection(
            data[0], quantity=Decimal("1"), revision=1,
            stop_loss=Decimal("94"), take_profit=Decimal("110"), owner="new-worker",
        )
    assert (await connector.list_positions())[0].stop_loss is None


@pytest.mark.asyncio
async def test_changed_level_activation_comes_from_versioned_authority(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        trade.stop_loss = Decimal("94")
        await db.commit()
        authority = await db.get(ProtectionAuthority, intent_id)
        effective = authority.levels_effective_at
        assert authority.version == 2 and effective is not None
    await expire_claim(effect_sessions)
    effects = connector._transport.durable_effects
    operation_id, _, payload = await effects.prepare_initial_protection(
        data[0], quantity=Decimal("1"), revision=1,
        stop_loss=Decimal("94"), take_profit=Decimal("110"), owner="new-worker",
    )
    evidence, _, _ = await collect_protection_evidence(
        effects, data[0], operation_id, provider=NoActivationProvider().bind_clock(provider.clock),
    )
    fill = (await connector.query_order(intent_id=intent_id)).executions[0]
    assert payload["levels_version"] == 2
    assert payload["levels_effective_at"] == effective.isoformat()
    assert evidence.stop_effective_at == max(fill.executed_at, effective)
    assert evidence.executions[0].effective_at == max(fill.executed_at, effective)


@pytest.mark.asyncio
async def test_recovery_does_not_treat_missing_legacy_activation_as_current(effect_sessions):
    from paper.protection_recovery import recover_pending_paper_protection

    connector, intent_id, request = await setup(effect_sessions)
    provider = connector._test_evidence_provider
    async with effect_sessions() as db:
        await OrderExecutor(connector, db, evidence_provider=provider).open_position(**request)
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        trade.stop_loss = Decimal("94")
        await db.commit()
    result = await recover_pending_paper_protection(
        effect_sessions, evidence_provider=provider, evidence_clock=provider.clock,
    )
    assert result["confirmed"] == 1
    async with effect_sessions() as db:
        await db.execute(text("ALTER TABLE protection_authorities DISABLE TRIGGER autoxtrade_guard_protection_authority"))
        await db.execute(text(
            "UPDATE protection_authorities SET levels_effective_at = NULL WHERE intent_id = :intent_id"
        ), {"intent_id": intent_id})
        await db.execute(text("ALTER TABLE protection_authorities ENABLE TRIGGER autoxtrade_guard_protection_authority"))
        await db.commit()
    result = await recover_pending_paper_protection(effect_sessions)
    assert result == {"selected": 1, "confirmed": 0, "deferred": 0, "failed": 1}
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status == "protection_pending"


@pytest.mark.asyncio
async def test_lost_response_recovers_with_provider_unavailable(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    original = connector.modify_position_protection

    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise TimeoutError("lost after commit")

    connector.modify_position_protection = AsyncMock(side_effect=lost)
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="protection pending"):
            await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
    await expire_claim(effect_sessions)
    connector._test_evidence_provider.clock.current += timedelta(days=1)
    provider = AsyncMock()
    provider.evidence_for = AsyncMock(side_effect=RuntimeError("feed unavailable"))
    connector.modify_position_protection = AsyncMock(side_effect=AssertionError("effect repeated"))
    async with effect_sessions() as db:
        trade = await OrderExecutor(connector, db, evidence_provider=provider).open_position(**request)
        assert trade.intent_id == intent_id
    provider.evidence_for.assert_not_awaited()
    connector.modify_position_protection.assert_not_awaited()
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        receipts = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()
        assert intent.status == "confirmed"
        assert len(receipts) == 1


@pytest.mark.asyncio
async def test_historical_receipt_does_not_confirm_new_entry_revision(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions, partial=True)
    original = await apply(connector, data, provider)
    await connector.observe_paper_fill(
        intent_id, filled=Decimal("1"), average=Decimal("100"), terminal=True, revision=2,
    )
    position_id, operation_id, token, payload, owner = data
    effects = connector._transport.durable_effects
    receipt = await effects.query_initial_protection_receipt(
        position_id, operation_id, token, payload, owner,
    )
    assert receipt == original
    async with effect_sessions() as db:
        assert not await effects.confirm_initial_protection(
            db, position_id, operation_id, token, payload, owner,
        )
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status != "confirmed"


@pytest.mark.asyncio
async def test_protection_receipt_is_immutable(effect_sessions):
    connector, _, data, provider = await pending_attempt(effect_sessions)
    await apply(connector, data, provider)
    async with effect_sessions() as db:
        receipt = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id == data[1]
        ))).scalar_one()
        receipt.result = {"filled": "1", "average": None}
        with pytest.raises(DBAPIError, match="immutable"):
            await db.commit()
