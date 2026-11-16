import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from db.models import ExecutionIntent, Trade, TradeEvent
from paper.durable_effects import PaperEffectOperation, PaperEffectPosition, PaperProtectionAttempt
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_initial_paper_protection import setup
from tests.integration.test_paper_durable_effects import effect_sessions
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.contracts import ResultState
from trading.order_executor import OrderExecutionError, OrderExecutor


async def opened(sessions):
    connector, intent_id, request = await setup(sessions)
    async with sessions() as db:
        trade = await OrderExecutor(
            connector, db, evidence_provider=connector._test_evidence_provider
        ).open_position(**request)
        trade_id = trade.id
    return connector, intent_id, trade_id


async def close(sessions, connector, trade_id, *, reason="manual", price=Decimal("105")):
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        return await OrderExecutor(connector, db).close_position(
            trade, reason=reason, execution_price=price
        )


async def expire_close_claim(sessions):
    async with sessions() as db:
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second' "
            "WHERE operation_id LIKE 'manual-close:%'"
        ))
        await db.commit()


async def assert_closed(sessions, intent_id, trade_id):
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        events = (await db.execute(select(TradeEvent).where(
            TradeEvent.trade_id == trade_id, TradeEvent.event_type == "MANUAL_CLOSE"
        ))).scalars().all()
        receipts = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id.like("manual-close:%")
        ))).scalars().all()
        assert position.quantity == 0
        assert trade.status == "closed"
        assert trade.close_order_id == f"manual-close:{trade_id}"
        assert trade.exit_price == Decimal("104.999")
        assert trade.pnl_gross == Decimal("5.00")
        assert trade.commission == Decimal("0.20")
        assert trade.pnl_net == Decimal("4.79")
        assert intent.status == "confirmed" and intent.order_terminal is True
        assert intent.risk_amount == Decimal("20") and intent.capital_amount == Decimal("500")
        assert len(events) == len(receipts) == 1


@pytest.mark.asyncio
async def test_manual_close_repetition_and_payload_conflict(effect_sessions):
    connector, intent_id, trade_id = await opened(effect_sessions)
    await close(effect_sessions, connector, trade_id)
    await close(effect_sessions, connector, trade_id)
    with pytest.raises(OrderExecutionError, match="pending"):
        await close(effect_sessions, connector, trade_id, price=Decimal("106"))
    await assert_closed(effect_sessions, intent_id, trade_id)


@pytest.mark.asyncio
async def test_two_manual_close_workers(effect_sessions):
    connector, intent_id, trade_id = await opened(effect_sessions)
    results = await asyncio.gather(
        close(effect_sessions, connector, trade_id),
        close(effect_sessions, connector, trade_id),
        return_exceptions=True,
    )
    assert any(isinstance(result, Trade) for result in results)
    assert all(isinstance(result, (Trade, OrderExecutionError)) for result in results)
    await assert_closed(effect_sessions, intent_id, trade_id)


@pytest.mark.asyncio
async def test_lost_response_reuses_receipt_after_takeover(effect_sessions):
    connector, intent_id, trade_id = await opened(effect_sessions)
    reduce = connector.reduce_position

    async def lost_response(*args, **kwargs):
        await reduce(*args, **kwargs)
        raise TimeoutError("response lost after durable effect")

    connector.reduce_position = lost_response
    with pytest.raises(OrderExecutionError, match="pending"):
        await close(effect_sessions, connector, trade_id)
    async with effect_sessions() as db:
        assert (await db.get(Trade, trade_id)).status == "open"
        assert (await db.execute(select(PaperEffectPosition))).scalar_one().quantity == 0
        assert (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one().status == "confirmed"
    await expire_close_claim(effect_sessions)
    restarted = SimulatedConnector(connector.context, transport=MemoryTransport(
        connector.context, durable_effects=connector._transport.durable_effects
    ))
    await close(effect_sessions, restarted, trade_id)
    await assert_closed(effect_sessions, intent_id, trade_id)


@pytest.mark.asyncio
async def test_stale_worker_cannot_reduce_after_takeover(effect_sessions):
    connector, intent_id, trade_id = await opened(effect_sessions)
    effects = connector._transport.durable_effects
    position_id = "paper_" + intent_id
    operation_id, fence, _ = await effects.prepare_manual_close(
        position_id, trade_id=trade_id, quantity=Decimal("1"), reason="manual",
        execution_price=Decimal("105"), owner="old",
    )
    await expire_close_claim(effect_sessions)
    _, new_fence, _ = await effects.prepare_manual_close(
        position_id, trade_id=trade_id, quantity=Decimal("1"), reason="manual",
        execution_price=Decimal("105"), owner="new",
    )
    assert new_fence > fence
    with pytest.raises(ValueError, match="Stale fencing"):
        await connector.reduce_position(
            position_id, Decimal("1"), intent_id=operation_id, fence=fence,
            execution_price=Decimal("105"), owner="old",
        )
    async with effect_sessions() as db:
        assert (await db.execute(select(PaperEffectPosition))).scalar_one().quantity == Decimal("1")
    await expire_close_claim(effect_sessions)
    await close(effect_sessions, connector, trade_id)
    await assert_closed(effect_sessions, intent_id, trade_id)


@pytest.mark.asyncio
async def test_commit_failure_after_effect_is_recovered(effect_sessions):
    connector, intent_id, trade_id = await opened(effect_sessions)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        executor = OrderExecutor(connector, db)
        original_commit = db.commit
        commits = 0

        async def fail_final_commit():
            nonlocal commits
            commits += 1
            if commits == 3:
                raise RuntimeError("accounting commit lost")
            return await original_commit()

        db.commit = fail_final_commit
        with pytest.raises(OrderExecutionError, match="pending"):
            await executor.close_position(trade, execution_price=Decimal("105"))
    async with effect_sessions() as db:
        assert (await db.get(Trade, trade_id)).status == "open"
        assert (await db.execute(select(PaperEffectPosition))).scalar_one().quantity == 0
        assert len((await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id.like("manual-close:%")
        ))).scalars().all()) == 1
    await expire_close_claim(effect_sessions)
    await close(effect_sessions, connector, trade_id)
    await assert_closed(effect_sessions, intent_id, trade_id)


@pytest.mark.asyncio
async def test_partial_response_does_not_finalize_trade(effect_sessions):
    connector, intent_id, trade_id = await opened(effect_sessions)
    original = connector.reduce_position

    async def partial_response(*args, **kwargs):
        result = await original(*args, **kwargs)
        return replace(result, state=ResultState.PARTIAL, filled=Decimal("0.5"))

    connector.reduce_position = partial_response
    with pytest.raises(OrderExecutionError, match="pending"):
        await close(effect_sessions, connector, trade_id)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert trade.status == "open" and trade.pnl_net is None
        assert intent.risk_amount == Decimal("20") and intent.capital_amount == Decimal("500")
    await expire_close_claim(effect_sessions)
    connector.reduce_position = original
    await close(effect_sessions, connector, trade_id)
    await assert_closed(effect_sessions, intent_id, trade_id)


@pytest.mark.asyncio
async def test_close_decision_payload_is_immutable_in_postgresql(effect_sessions):
    connector, intent_id, trade_id = await opened(effect_sessions)
    await connector._transport.durable_effects.prepare_manual_close(
        "paper_" + intent_id, trade_id=trade_id, quantity=Decimal("1"),
        reason="manual", execution_price=Decimal("105"), owner="owner",
    )
    async with effect_sessions() as db:
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(text(
                "UPDATE paper_protection_attempts SET payload = payload || "
                "'{\"reason\":\"changed\"}'::jsonb WHERE operation_id LIKE 'manual-close:%'"
            ))
        await db.rollback()


@pytest.mark.asyncio
async def test_cost_change_after_decision_rejects_effect(effect_sessions):
    connector, intent_id, trade_id = await opened(effect_sessions)
    position_id = "paper_" + intent_id
    operation_id, fence, _ = await connector._transport.durable_effects.prepare_manual_close(
        position_id, trade_id=trade_id, quantity=Decimal("1"),
        reason="manual", execution_price=Decimal("105"), owner="owner",
    )
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        snapshot = dict(trade.execution_context)
        snapshot["paper_costs"] = {**snapshot["paper_costs"], "commission_rate": "0.5"}
        trade.execution_context = snapshot
        await db.commit()
    with pytest.raises(ValueError, match="stale"):
        await connector.reduce_position(
            position_id, Decimal("1"), intent_id=operation_id, fence=fence,
            execution_price=Decimal("105"), owner="owner",
        )
    async with effect_sessions() as db:
        assert (await db.get(Trade, trade_id)).status == "open"
        assert (await db.execute(select(PaperEffectPosition))).scalar_one().quantity == 1


@pytest.mark.asyncio
async def test_invalid_execution_price_does_not_create_decision(effect_sessions):
    connector, intent_id, trade_id = await opened(effect_sessions)
    with pytest.raises(OrderExecutionError, match="pending"):
        await close(effect_sessions, connector, trade_id, price=Decimal("105.5"))
    async with effect_sessions() as db:
        assert (await db.get(Trade, trade_id)).status == "open"
        assert not (await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.operation_id.like("manual-close:%")
        ))).scalars().all()


@pytest.mark.asyncio
async def test_nonterminal_entry_cannot_be_closed(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions, partial=True)
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="partially executed"):
            await OrderExecutor(
                connector, db, evidence_provider=connector._test_evidence_provider
            ).open_position(**request)
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        trade_id = trade.id
    with pytest.raises(OrderExecutionError, match="pending"):
        await close(effect_sessions, connector, trade_id)
    async with effect_sessions() as db:
        assert (await db.get(Trade, trade_id)).status == "open"
        assert (await db.execute(select(PaperEffectPosition))).scalar_one().quantity == Decimal("0.5")
        assert not (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id.like("manual-close:%")
        ))).scalars().all()
