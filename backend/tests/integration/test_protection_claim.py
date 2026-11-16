import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from db.models import ExecutionIntent, Trade
from paper.durable_effects import PaperEffectOperation, PaperProtectionAttempt
from paper.protection_evidence_store import provide_and_record_protection_evidence
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_protection_effect_authority import apply, pending_attempt


async def prepare(connector, data, owner):
    position_id, _, _, payload, _ = data
    return await connector._transport.durable_effects.prepare_initial_protection(
        position_id, quantity=Decimal(payload["quantity"]), revision=payload["revision"],
        stop_loss=Decimal(payload["stop_loss"]),
        take_profit=Decimal(payload["take_profit"]) if payload["take_profit"] else None,
        owner=owner, lease_seconds=60,
    )


async def expire(sessions):
    async with sessions() as db:
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second'"
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_claim_repetition_occupied_and_takeover_fence_worker(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    position_id, operation_id, token, payload, owner = data
    assert await prepare(connector, data, owner) == (operation_id, token, payload)
    with pytest.raises(ValueError, match="occupied"):
        await prepare(connector, data, "another-worker")
    await expire(effect_sessions)
    with pytest.raises(ValueError, match="expired protection claim"):
        await apply(connector, data, provider)
    new_operation, new_token, new_payload = await prepare(connector, data, "another-worker")
    assert new_operation == operation_id and new_payload == payload and new_token > token
    with pytest.raises(ValueError, match="Stale fencing"):
        await apply(connector, data, provider)
    async with effect_sessions() as db:
        await db.execute(select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id).with_for_update())
        await db.execute(select(Trade).where(Trade.intent_id == intent_id).with_for_update())
        assert not await connector._transport.durable_effects.confirm_initial_protection(
            db, position_id, operation_id, token, payload, owner)
    new_data = (position_id, new_operation, new_token, new_payload, "another-worker")
    await apply(connector, new_data, provider)
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")
    async with effect_sessions() as db:
        attempts = (await db.execute(select(PaperProtectionAttempt))).scalars().all()
        effects = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()
        assert len(attempts) == len(effects) == 1


@pytest.mark.asyncio
async def test_takeover_after_effect_recovers_receipt_without_duplicate(effect_sessions):
    connector, _, data, provider = await pending_attempt(effect_sessions)
    first = await apply(connector, data, provider)
    await expire(effect_sessions)
    operation_id, token, payload = await prepare(connector, data, "restart")
    evidence_id, _ = await provide_and_record_protection_evidence(
        connector._transport.durable_effects, data[0], operation_id, provider=provider,
    )
    receipt = await connector._transport.durable_effects.query_initial_protection_receipt(
        data[0], operation_id, token, payload, "restart", evidence_id=evidence_id,
    )
    assert receipt == first
    with pytest.raises(ValueError, match="Stale fencing"):
        await apply(connector, data, provider)
    assert await apply(connector, (data[0], operation_id, token, payload, "restart"), provider) == first
    async with effect_sessions() as db:
        effects = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()
        assert len(effects) == 1
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")


@pytest.mark.asyncio
async def test_two_workers_compete_for_expired_claim(effect_sessions):
    connector, _, data, provider = await pending_attempt(effect_sessions)
    await expire(effect_sessions)
    results = await asyncio.gather(
        prepare(connector, data, "worker-a"), prepare(connector, data, "worker-b"),
        return_exceptions=True,
    )
    winners = [result for result in results if isinstance(result, tuple)]
    losers = [result for result in results if isinstance(result, ValueError)]
    assert len(winners) == len(losers) == 1
    assert "occupied" in str(losers[0])
    assert winners[0][1] == data[2] + 1


@pytest.mark.asyncio
async def test_old_receipt_cannot_confirm_changed_levels_after_takeover(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    await apply(connector, data, provider)
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        trade.stop_loss = Decimal("94")
        await db.commit()
    await expire(effect_sessions)
    with pytest.raises(ValueError, match="levels or exposure changed"):
        await prepare(connector, data, "restart")
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status == "protection_pending"
        assert len((await db.execute(select(PaperProtectionAttempt))).scalars().all()) == 1
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")
