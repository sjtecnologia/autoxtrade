from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from database import Base
from db.models import ProtectionAuthority, Trade
from paper.durable_effects import (
    DurablePaperEffects, PaperEffectOperation, PaperProtectionAttempt, PaperProtectionEvidenceRecord,
    ProtectionEvidenceRequired,
)
from paper.protection_evidence_store import (
    collect_protection_evidence, record_protection_evidence,
)
from tests.integration.paper_protection_helpers import ControlledEvidenceClock, PostFillSyntheticProvider
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_initial_paper_protection import expire_claim, setup
from tests.integration.test_paper_durable_effects import effect_sessions
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.types import OrderSide, OrderType
from trading.contracts import OrderIntent
from trading.order_executor import OrderExecutionError, OrderExecutor


@pytest.fixture
async def evidence_sessions(effect_sessions):
    # effect_sessions creates this table and its Alembic-equivalent trigger.
    return effect_sessions


class EmptyProvider:
    async def evidence_for(self, request):
        return None


async def create_durable_entry(sessions):
    """Create a durable entry with its authority but no linked trade."""
    connector, intent_id, request = await setup(sessions)
    async with sessions() as db:
        snapshot = connector.context.snapshot()
        snapshot["capabilities"] = sorted(snapshot["capabilities"])
        db.add(ProtectionAuthority(
            intent_id=intent_id, execution_context=snapshot, side="buy",
            quantity=Decimal("1"), entry_price=Decimal("100"),
            initial_stop_loss=Decimal("95"), initial_take_profit=Decimal("110"),
            stop_loss=Decimal("95"), take_profit=Decimal("110"),
        ))
        await db.commit()
    entry = await connector.create_order(OrderIntent(
        intent_id, connector.context, OrderSide.BUY, OrderType.MARKET, Decimal("1")
    ))
    return connector, intent_id, entry, request


@pytest.mark.asyncio
async def test_evidence_attempt_identity_excludes_quote(evidence_sessions):
    connector, intent_id, entry, _ = await create_durable_entry(evidence_sessions)
    effects = connector._transport.durable_effects
    clock = ControlledEvidenceClock()
    effects._evidence_clock = clock
    first = await effects.prepare_initial_protection(
        entry.position_id, quantity=entry.filled, revision=entry.revision,
        stop_loss=Decimal("95"), take_profit=Decimal("110"), owner="worker-1",
    )
    second = await effects.prepare_initial_protection(
        entry.position_id, quantity=entry.filled, revision=entry.revision,
        stop_loss=Decimal("95"), take_profit=Decimal("110"), owner="worker-1",
    )
    assert first[0] == second[0]
    assert first[2] == second[2]
    assert first[2]["payload_version"] == 2
    assert "mark_price" not in first[2]

    # Market quotes are evidence attached to the immutable decision, not part
    # of its identity. Persist two distinct references for the same attempt.
    evidence_ids = []
    for reference in ("101", "123"):
        evidence, evidence_id, max_age = await collect_protection_evidence(
            effects, entry.position_id, first[0],
            provider=PostFillSyntheticProvider(reference=reference).bind_clock(clock),
        )
        assert evidence.reference.price == Decimal(reference)
        await record_protection_evidence(
            effects, entry.position_id, first[0], evidence_id, evidence, max_age=max_age,
        )
        evidence_ids.append(evidence_id)
    assert evidence_ids[0] != evidence_ids[1]

    result = await connector.modify_position_protection(
        entry.position_id, stop_loss=Decimal("95"), take_profit=Decimal("110"),
        intent_id=first[0], fence=first[1], revision=first[2]["revision"],
        quantity=entry.filled, owner="worker-1", evidence_id=evidence_ids[0],
    )
    replay = await connector.modify_position_protection(
        entry.position_id, stop_loss=Decimal("95"), take_profit=Decimal("110"),
        intent_id=first[0], fence=first[1], revision=first[2]["revision"],
        quantity=entry.filled, owner="worker-1", evidence_id=evidence_ids[1],
    )
    assert replay == result
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")
    async with evidence_sessions() as db:
        receipts = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id == first[0]
        ))).scalars().all()
        assert len(receipts) == 1


@pytest.mark.asyncio
async def test_evidence_without_trade_uses_persisted_paper_costs(evidence_sessions):
    connector, intent_id, entry, _ = await create_durable_entry(evidence_sessions)
    async with evidence_sessions() as db:
        assert (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one_or_none() is None
    effects = connector._transport.durable_effects
    clock = ControlledEvidenceClock()
    effects._evidence_clock = clock
    operation_id, fence, payload = await effects.prepare_initial_protection(
        entry.position_id, quantity=entry.filled, revision=entry.revision,
        stop_loss=Decimal("95"), take_profit=Decimal("110"), owner="worker-1",
    )
    evidence, evidence_id, max_age = await collect_protection_evidence(
        effects, entry.position_id, operation_id,
        provider=PostFillSyntheticProvider().bind_clock(clock),
    )
    assert evidence.executed_average == entry.average
    assert evidence.cost_adjusted_entry == entry.average + Decimal("0.001")
    await record_protection_evidence(
        effects, entry.position_id, operation_id, evidence_id, evidence, max_age=max_age,
    )
    result = await connector.modify_position_protection(
        entry.position_id, stop_loss=Decimal("95"), take_profit=Decimal("110"),
        intent_id=operation_id, fence=fence, revision=payload["revision"],
        quantity=Decimal(payload["quantity"]), owner="worker-1", evidence_id=evidence_id,
    )
    assert result.state.value == "confirmed"
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")


@pytest.mark.asyncio
@pytest.mark.parametrize(("low", "max_age"), [("94", timedelta(minutes=5)), ("99", timedelta(seconds=1))])
async def test_invalid_evidence_never_applies_protection(evidence_sessions, low, max_age):
    connector, intent_id, entry, _ = await create_durable_entry(evidence_sessions)
    effects = connector._transport.durable_effects
    clock = ControlledEvidenceClock()
    effects._evidence_clock = clock
    operation_id, fence, payload = await effects.prepare_initial_protection(
        entry.position_id, quantity=entry.filled, revision=entry.revision,
        stop_loss=Decimal("95"), take_profit=Decimal("110"), owner="worker-1",
    )
    evidence, evidence_id, actual_max_age = await collect_protection_evidence(
        effects, entry.position_id, operation_id,
        provider=PostFillSyntheticProvider(history_low=low).bind_clock(clock),
        max_age=max_age,
    )
    await record_protection_evidence(
        effects, entry.position_id, operation_id, evidence_id, evidence,
        max_age=actual_max_age,
    )
    with pytest.raises(ProtectionEvidenceRequired):
        await connector.modify_position_protection(
            entry.position_id, stop_loss=Decimal("95"), take_profit=Decimal("110"),
            intent_id=operation_id, fence=fence, revision=payload["revision"],
            quantity=Decimal(payload["quantity"]), owner="worker-1",
            evidence_id=evidence_id,
        )
    assert (await connector.list_positions())[0].stop_loss is None


@pytest.mark.asyncio
async def test_recovery_task_accepts_injected_evidence_provider(evidence_sessions):
    from paper.protection_recovery import recover_pending_paper_protection

    connector, intent_id, request = await setup(evidence_sessions)
    clock = ControlledEvidenceClock()
    provider = PostFillSyntheticProvider().bind_clock(clock)
    async with evidence_sessions() as db:
        with pytest.raises(OrderExecutionError, match="protection pending"):
            await OrderExecutor(connector, db, evidence_provider=EmptyProvider()).open_position(**request)
    await expire_claim(evidence_sessions)
    counts = await recover_pending_paper_protection(
        evidence_sessions, evidence_provider=provider, evidence_clock=clock,
    )
    assert counts == {"selected": 1, "confirmed": 1, "deferred": 0, "failed": 0}
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")
    async with evidence_sessions() as db:
        records = (await db.execute(select(PaperProtectionEvidenceRecord))).scalars().all()
        assert len(records) == 1


@pytest.mark.asyncio
async def test_executor_provider_keeps_pending_and_recovers_with_evidence(evidence_sessions):
    connector, intent_id, request = await setup(evidence_sessions)
    clock = ControlledEvidenceClock()
    provider = PostFillSyntheticProvider().bind_clock(clock)
    async with evidence_sessions() as db:
        with pytest.raises(OrderExecutionError, match="protection pending"):
            await OrderExecutor(connector, db, evidence_provider=EmptyProvider()).open_position(**request)
    async with evidence_sessions() as db:
        attempt = (await db.execute(select(PaperProtectionAttempt))).scalar_one()
        assert attempt.payload["payload_version"] == 2
        assert "mark_price" not in attempt.payload
        assert (await db.execute(select(PaperProtectionEvidenceRecord))).scalars().all() == []
    assert (await connector.list_positions())[0].stop_loss is None

    await expire_claim(evidence_sessions)
    restarted = SimulatedConnector(connector.context, transport=MemoryTransport(
        connector.context, durable_effects=DurablePaperEffects(
            evidence_sessions, connector.context, evidence_clock=clock,
        )
    ))
    async with evidence_sessions() as db:
        await OrderExecutor(
            restarted, db, evidence_provider=provider
        ).open_position(**request)
    assert (await restarted.list_positions())[0].stop_loss == Decimal("95")
    async with evidence_sessions() as db:
        records = (await db.execute(select(PaperProtectionEvidenceRecord))).scalars().all()
        attempts = (await db.execute(select(PaperProtectionAttempt))).scalars().all()
        assert len(records) == len(attempts) == 1
        assert records[0].operation_id == attempts[0].operation_id
        assert records[0].payload["evidence"]["reference"]["price"] == "101"
