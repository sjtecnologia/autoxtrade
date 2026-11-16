from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from db.models import ExecutionIntent, Trade
from paper.durable_effects import DurablePaperEffects, PaperEffectOperation, PaperProtectionAttempt
from paper.durable_effects import PaperProtectionEvidenceRecord
from tests.integration.paper_protection_helpers import ControlledEvidenceClock, PostFillSyntheticProvider
from tests.integration.test_approval_lifecycle import _context, _seed, approval_database
from tests.integration.test_paper_durable_effects import effect_sessions
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.types import OrderSide
from trading.order_executor import OrderExecutionError, OrderExecutor


async def setup(sessions, *, partial=False):
    _, intent_id = await _seed(sessions)
    async with sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        intent.quantity = Decimal("1")
        intent.risk_amount = Decimal("20")
        intent.capital_amount = Decimal("500")
        await db.commit()
    context = _context("BTC/USDT")
    evidence_clock = ControlledEvidenceClock()
    evidence_provider = PostFillSyntheticProvider().bind_clock(evidence_clock)
    transport = MemoryTransport(context, durable_effects=DurablePaperEffects(
        sessions, context, evidence_clock=evidence_clock,
    ))
    transport.prices[context.instrument.symbol] = Decimal("100")
    if partial:
        transport.scenarios[intent_id] = Decimal("0.5")
    connector = SimulatedConnector(context, transport=transport)
    connector._test_evidence_provider = evidence_provider
    return connector, intent_id, {
        "symbol": "BTC/USDT", "side": OrderSide.BUY, "quantity": Decimal("1"),
        "entry_price": Decimal("100"), "stop_loss": Decimal("95"),
        "take_profit": Decimal("110"), "market": "CRIPTO", "intent_id": intent_id,
    }


async def expire_claim(sessions):
    async with sessions() as db:
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second'"
        ))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_initial_protection_applies_to_confirmed_exposure(effect_sessions, partial):
    connector, intent_id, request = await setup(effect_sessions, partial=partial)
    async with effect_sessions() as db:
        if partial:
            with pytest.raises(OrderExecutionError, match="partially executed"):
                await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
        else:
            await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
    position = (await connector.list_positions())[0]
    assert position.quantity == (Decimal("0.5") if partial else Decimal("1"))
    assert position.stop_loss == Decimal("95") and position.take_profit == Decimal("110")
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id))).scalar_one()
        attempts = (await db.execute(select(PaperProtectionAttempt))).scalars().all()
        operations = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()
        assert intent.status == ("partial" if partial else "confirmed")
        assert len(attempts) == len(operations) == 1
        assert attempts[0].payload == operations[0].payload
        evidence = (await db.execute(select(PaperProtectionEvidenceRecord))).scalar_one()
        assert evidence.assessment["state"] == "valid"


@pytest.mark.asyncio
async def test_lost_response_restart_reuses_operation(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    original = connector.modify_position_protection
    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise TimeoutError("lost after commit")
    connector.modify_position_protection = AsyncMock(side_effect=lost)
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="protection pending"):
            await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")
    await expire_claim(effect_sessions)
    restarted = SimulatedConnector(connector.context, transport=MemoryTransport(
        connector.context, durable_effects=DurablePaperEffects(
            effect_sessions, connector.context,
            evidence_clock=connector._transport.durable_effects._evidence_clock,
        )
    ))
    async with effect_sessions() as db:
        await OrderExecutor(restarted, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id))).scalar_one()
        assert intent.status == "confirmed"
        assert len((await db.execute(select(PaperProtectionAttempt))).scalars().all()) == 1
        assert len((await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_refresh_failure_still_applies_initial_protection(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    async with effect_sessions() as db:
        db.refresh = AsyncMock(side_effect=RuntimeError("refresh failed"))
        with pytest.raises(OrderExecutionError, match="persistence/read failed"):
            await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")
    async with effect_sessions() as db:
        trade = await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
        assert trade.intent_id == intent_id
        assert len((await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_trade_persistence_failure_still_protects_and_recovers(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    connector.create_order = AsyncMock(wraps=connector.create_order)
    async with effect_sessions() as db:
        executor = OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider)
        original_commit = db.commit
        commits = 0
        async def fail_trade_commit():
            nonlocal commits
            commits += 1
            if commits == 2:
                raise RuntimeError("commit failed")
            return await original_commit()
        db.commit = AsyncMock(side_effect=fail_trade_commit)
        with pytest.raises(OrderExecutionError, match="state changed"):
            await executor.open_position(**request)
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")
    async with effect_sessions() as db:
        assert (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one_or_none() is None
    await expire_claim(effect_sessions)
    async with effect_sessions() as db:
        trade = await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
        assert trade.position_id == "paper_" + intent_id
    assert connector.create_order.await_count == 1
    async with effect_sessions() as db:
        assert len((await db.execute(select(PaperProtectionAttempt))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_stale_token_cannot_apply_prepared_protection(effect_sessions):
    connector, _, request = await setup(effect_sessions)
    connector.modify_position_protection = AsyncMock(side_effect=TimeoutError("before effect"))
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="protection pending"):
            await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
    async with effect_sessions() as db:
        attempt = (await db.execute(select(PaperProtectionAttempt))).scalar_one()
        position_id, operation_id, token, payload, owner = (
            attempt.position_id, attempt.operation_id, attempt.fence, attempt.payload, attempt.owner
        )
        evidence_record = (await db.execute(select(PaperProtectionEvidenceRecord))).scalar_one()
    await connector.acquire_position_fence(position_id)
    restarted = SimulatedConnector(connector.context, transport=MemoryTransport(
        connector.context, durable_effects=DurablePaperEffects(
            effect_sessions, connector.context,
            evidence_clock=connector._transport.durable_effects._evidence_clock,
        )
    ))
    with pytest.raises(ValueError, match="Stale fencing"):
        await restarted.modify_position_protection(
            position_id, stop_loss=Decimal(payload["stop_loss"]),
            take_profit=Decimal(payload["take_profit"]), intent_id=operation_id,
            fence=token, evidence_id=evidence_record.evidence_id,
            revision=payload["revision"], quantity=Decimal(payload["quantity"]), owner=owner,
        )
    assert (await restarted.list_positions())[0].stop_loss is None


@pytest.mark.asyncio
async def test_new_fill_before_confirmation_keeps_protection_pending(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions, partial=True)
    original = connector.modify_position_protection
    async def fill_after_effect(*args, **kwargs):
        result = await original(*args, **kwargs)
        await connector.observe_paper_fill(
            intent_id, filled=Decimal("0.8"), average=Decimal("100"),
            terminal=False, revision=2,
        )
        return result
    connector.modify_position_protection = AsyncMock(side_effect=fill_after_effect)
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="state changed"):
            await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status == "protection_pending"
    position = (await connector.list_positions())[0]
    assert position.quantity == Decimal("0.8")


@pytest.mark.asyncio
async def test_changed_stop_before_confirmation_preserves_pending(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    original = connector.modify_position_protection
    async def change_after_effect(*args, **kwargs):
        result = await original(*args, **kwargs)
        async with effect_sessions() as db:
            trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
            trade.stop_loss = Decimal("94")
            await db.commit()
        return result
    connector.modify_position_protection = AsyncMock(side_effect=change_after_effect)
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="state changed"):
            await OrderExecutor(connector, db, evidence_provider=connector._test_evidence_provider).open_position(**request)
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status == "protection_pending"
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")
