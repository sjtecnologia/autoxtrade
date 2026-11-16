import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from db.models import ExecutionIntent, Trade
from paper.durable_effects import PaperEffectOperation, PaperProtectionAttempt
from tests.integration.paper_protection_helpers import (
    ControlledEvidenceClock, PostFillSyntheticProvider, apply_prepared_protection,
)
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_initial_paper_protection import setup
from tests.integration.test_paper_durable_effects import effect_sessions
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.types import OrderSide, OrderType
from trading.contracts import OrderIntent
from trading.order_executor import OrderExecutionError, OrderExecutor


async def pending_attempt(sessions, *, partial=False, no_trade=False):
    connector, intent_id, request = await setup(sessions, partial=partial)
    connector.modify_position_protection = AsyncMock(side_effect=TimeoutError("before effect"))
    async with sessions() as db:
        if no_trade:
            original = db.commit
            commits = 0

            async def fail_trade():
                nonlocal commits
                commits += 1
                if commits == 2:
                    raise RuntimeError("trade commit failed")
                await original()

            db.commit = AsyncMock(side_effect=fail_trade)
        with pytest.raises(OrderExecutionError, match="protection pending"):
            await OrderExecutor(
                connector, db, evidence_provider=connector._test_evidence_provider
            ).open_position(**request)
    async with sessions() as db:
        attempt = (await db.execute(select(PaperProtectionAttempt))).scalar_one()
        assert attempt.payload["intent_id"] == intent_id
        assert attempt.payload["identity"]["account_id"] == connector.context.account_id
        assert attempt.payload["levels_version"] == 1
        assert attempt.payload["revision"] == 1
        assert attempt.payload["payload_version"] == 2
        assert "mark_price" not in attempt.payload
        assert Decimal(attempt.payload["quantity"]) == (Decimal("0.5") if partial else Decimal("1"))
        data = (attempt.position_id, attempt.operation_id, attempt.fence, attempt.payload, attempt.owner)
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one_or_none()
        assert (trade is None) == no_trade
    restarted = SimulatedConnector(connector.context, transport=MemoryTransport(
        connector.context, durable_effects=connector._transport.durable_effects
    ))
    evidence_clock = ControlledEvidenceClock()
    restarted._transport.durable_effects._evidence_clock = evidence_clock
    provider = PostFillSyntheticProvider().bind_clock(evidence_clock)
    return restarted, intent_id, data, provider


async def apply(connector, data, provider):
    position_id, operation_id, token, payload, owner = data
    return await apply_prepared_protection(
        connector, (position_id, operation_id, token, payload), owner=owner, provider=provider,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("no_trade", [False, True])
async def test_valid_attempt_with_or_without_trade(effect_sessions, no_trade):
    connector, _, data, provider = await pending_attempt(effect_sessions, no_trade=no_trade)
    await apply(connector, data, provider)
    position = (await connector.list_positions())[0]
    assert (position.stop_loss, position.take_profit) == (Decimal("95"), Decimal("110"))


@pytest.mark.asyncio
async def test_legacy_entry_without_authority_is_blocked(effect_sessions):
    connector, intent_id, _ = await setup(effect_sessions)
    entry = await connector.create_order(OrderIntent(
        intent_id, connector.context, OrderSide.BUY, OrderType.MARKET, Decimal("1")
    ))
    with pytest.raises(ValueError, match="authority missing"):
        await connector._transport.durable_effects.prepare_initial_protection(
            entry.position_id, quantity=entry.filled, revision=entry.revision,
            stop_loss=Decimal("95"), take_profit=Decimal("110"), owner="worker",
        )
    position = (await connector.list_positions())[0]
    assert position.stop_loss is None and position.take_profit is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [
    ("stop_loss", Decimal("94")), ("take_profit", Decimal("111")),
])
async def test_changed_levels_reject_before_effect(effect_sessions, field, value):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        setattr(trade, field, value)
        await db.commit()
    with pytest.raises(ValueError, match="levels or exposure is stale"):
        await apply(connector, data, provider)
    position = (await connector.list_positions())[0]
    assert position.stop_loss is None and position.take_profit is None


@pytest.mark.asyncio
async def test_levels_changed_and_restored_still_reject_old_version(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    for stop in (Decimal("94"), Decimal("95")):
        async with effect_sessions() as db:
            trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
            trade.stop_loss = stop
            await db.commit()
    with pytest.raises(ValueError, match="levels or exposure is stale"):
        await apply(connector, data, provider)
    assert (await connector.list_positions())[0].stop_loss is None


@pytest.mark.asyncio
async def test_fill_after_preparation_rejects_effect(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions, partial=True)
    await connector.observe_paper_fill(intent_id, filled=Decimal("0.8"), average=Decimal("100"),
                                       terminal=False, revision=2)
    with pytest.raises(ValueError, match="Evidence entry revision is missing or stale"):
        await apply(connector, data, provider)
    position = (await connector.list_positions())[0]
    assert position.quantity == Decimal("0.8")
    assert position.stop_loss is None and position.take_profit is None


@pytest.mark.asyncio
async def test_conflicting_trade_exposure_rejects_effect(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions, partial=True)
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        trade.quantity = Decimal("0.4")
        await db.commit()
    with pytest.raises(ValueError, match="exposure is stale"):
        await apply(connector, data, provider)
    position = (await connector.list_positions())[0]
    assert position.quantity == Decimal("0.5") and position.stop_loss is None


@pytest.mark.asyncio
async def test_conflicting_trade_position_identity_rejects_effect(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        trade.position_id = "paper_other"
        await db.commit()
    with pytest.raises(ValueError, match="identity, levels or exposure is stale"):
        await apply(connector, data, provider)
    assert (await connector.list_positions())[0].stop_loss is None


@pytest.mark.asyncio
async def test_concurrent_level_update_blocks_then_rejects(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(
            Trade.intent_id == intent_id
        ).with_for_update())).scalar_one()
        trade.stop_loss = Decimal("94")
        await db.flush()
        worker = asyncio.create_task(apply(connector, data, provider))
        for _ in range(100):
            async with effect_sessions() as observer:
                waiting = (await observer.execute(text("""
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database() AND wait_event_type = 'Lock'
                      AND query ILIKE '%from trades%' AND query ILIKE '%for update%'
                """))).scalar_one()
            if waiting:
                break
            assert not worker.done()
            await asyncio.sleep(0.05)
        assert waiting == 1
        await db.commit()
        with pytest.raises(ValueError, match="levels or exposure is stale"):
            await asyncio.wait_for(worker, timeout=5)
    position = (await connector.list_positions())[0]
    assert position.stop_loss is None and position.take_profit is None


@pytest.mark.asyncio
async def test_lost_reply_replay_does_not_confirm_stale_levels(effect_sessions):
    connector, intent_id, data, provider = await pending_attempt(effect_sessions)
    first = await apply(connector, data, provider)
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        trade.stop_loss = Decimal("94")
        await db.commit()
    position_id, operation_id, token, payload, owner = data
    receipt = await connector._transport.durable_effects.query_initial_protection_receipt(
        position_id, operation_id, token, payload, owner,
    )
    assert receipt == first
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ).with_for_update())).scalar_one()
        await db.execute(select(Trade).where(Trade.intent_id == intent_id).with_for_update())
        valid = await connector._transport.durable_effects.confirm_initial_protection(
            db, position_id, operation_id, token, payload, owner
        )
        assert not valid and intent.status == "protection_pending"
        receipts = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()
        assert len(receipts) == 1
    position = (await connector.list_positions())[0]
    assert position.stop_loss == Decimal("95") and position.take_profit == Decimal("110")
