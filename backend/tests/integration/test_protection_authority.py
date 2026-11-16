import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from db.models import ExecutionIntent, ProtectionAuthority, Trade
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_initial_paper_protection import setup
from tests.integration.test_paper_durable_effects import effect_sessions
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.order_executor import OrderExecutionError, OrderExecutor


@pytest.mark.asyncio
async def test_authority_exists_before_send_and_repetition_is_idempotent(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    original = connector.create_order

    async def inspect_before_send(order):
        async with effect_sessions() as db:
            authority = await db.get(ProtectionAuthority, intent_id)
            assert authority is not None
            assert authority.execution_context["account_id"] == connector.context.account_id
            assert authority.execution_context["instrument"]["symbol"] == request["symbol"]
            assert authority.stop_loss == Decimal("95")
            assert authority.take_profit == Decimal("110")
            assert authority.version == 1
        return await original(order)

    connector.create_order = AsyncMock(side_effect=inspect_before_send)
    async with effect_sessions() as db:
        first = await OrderExecutor(
            connector, db, evidence_provider=connector._test_evidence_provider
        ).open_position(**request)
    async with effect_sessions() as db:
        second = await OrderExecutor(
            connector, db, evidence_provider=connector._test_evidence_provider
        ).open_position(**request)
        authorities = (await db.execute(select(ProtectionAuthority))).scalars().all()
        assert len(authorities) == 1
        assert first.id == second.id
        assert second.protection_authority_id == intent_id
    connector.create_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_intent_with_different_account_is_rejected(effect_sessions):
    connector, _, request = await setup(effect_sessions)
    async with effect_sessions() as db:
        await OrderExecutor(
            connector, db, evidence_provider=connector._test_evidence_provider
        ).open_position(**request)
    other_context = replace(connector.context, account_id="another-account")
    other = SimulatedConnector(other_context, transport=MemoryTransport(other_context))
    other.create_order = AsyncMock(wraps=other.create_order)
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="different request"):
            await OrderExecutor(other, db).open_position(**request)
    other.create_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_authority_commit_prevents_send(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    connector.create_order = AsyncMock(wraps=connector.create_order)
    async with effect_sessions() as db:
        db.commit = AsyncMock(side_effect=RuntimeError("authority commit failed"))
        with pytest.raises(OrderExecutionError, match="no order sent"):
            await OrderExecutor(
                connector, db, evidence_provider=connector._test_evidence_provider
            ).open_position(**request)
    connector.create_order.assert_not_awaited()
    async with effect_sessions() as db:
        assert await db.get(ProtectionAuthority, intent_id) is None
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        assert intent.status == "reserved"


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"stop_loss": Decimal("94")},
    {"take_profit": Decimal("111")},
    {"entry_price": Decimal("101")},
])
async def test_conflicting_request_is_rejected(effect_sessions, changed):
    connector, _, request = await setup(effect_sessions)
    connector.create_order = AsyncMock(wraps=connector.create_order)
    async with effect_sessions() as db:
        await OrderExecutor(
            connector, db, evidence_provider=connector._test_evidence_provider
        ).open_position(**request)
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="different request"):
            await OrderExecutor(
                connector, db, evidence_provider=connector._test_evidence_provider
            ).open_position(**(request | changed))
    connector.create_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_trade_commit_failure_keeps_authority_and_protects(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    async with effect_sessions() as db:
        original = db.commit
        commits = 0

        async def fail_trade():
            nonlocal commits
            commits += 1
            if commits == 2:
                raise RuntimeError("trade commit failed")
            await original()

        db.commit = AsyncMock(side_effect=fail_trade)
        with pytest.raises(OrderExecutionError, match="state changed"):
            await OrderExecutor(
                connector, db, evidence_provider=connector._test_evidence_provider
            ).open_position(**request)
    assert (await connector.list_positions())[0].stop_loss == Decimal("95")
    async with effect_sessions() as db:
        assert await db.get(ProtectionAuthority, intent_id) is not None
        assert (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one_or_none() is None
        db.add(Trade(
            market="CRIPTO", exchange=connector.context.venue, symbol=request["symbol"],
            side="buy", status="open", mode="paper", account_id=connector.context.account_id,
            account_nature="simulated", instrument_class="crypto", identity_status="bound",
            intent_id=intent_id, entry_price=Decimal("100"), quantity=Decimal("1"),
            entry_value=Decimal("100"), stop_loss=Decimal("95"), take_profit=Decimal("110"),
            open_at=datetime.now(timezone.utc), execution_context=connector.context.snapshot(),
        ))
        with pytest.raises(DBAPIError, match="must bind"):
            await db.commit()
        await db.rollback()


@pytest.mark.asyncio
async def test_trade_level_changes_are_atomic_and_versioned(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions)
    async with effect_sessions() as db:
        await OrderExecutor(
            connector, db, evidence_provider=connector._test_evidence_provider
        ).open_position(**request)
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        trade.stop_loss = Decimal("94")
        await db.flush()
        authority = await db.get(ProtectionAuthority, intent_id)
        assert authority.stop_loss == Decimal("94") and authority.version == 2
        assert authority.levels_effective_at is not None
        await db.rollback()
    async with effect_sessions() as db:
        authority = await db.get(ProtectionAuthority, intent_id)
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        assert (authority.stop_loss, authority.version) == (Decimal("95"), 1)
        assert authority.levels_effective_at is None
        assert (trade.stop_loss, trade.protection_levels_version) == (Decimal("95"), 1)
        authority.stop_loss = Decimal("94")
        with pytest.raises(DBAPIError, match="must be updated through"):
            await db.commit()
        await db.rollback()

    async def change(column, value):
        async with effect_sessions() as db:
            trade = (await db.execute(
                select(Trade).where(Trade.intent_id == intent_id).with_for_update()
            )).scalar_one()
            setattr(trade, column, value)
            await asyncio.sleep(0.01)
            await db.commit()

    await asyncio.gather(change("stop_loss", Decimal("94")),
                         change("take_profit", Decimal("111")))
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        authority = await db.get(ProtectionAuthority, intent_id)
        assert (trade.stop_loss, trade.take_profit, trade.protection_levels_version) == (
            Decimal("94"), Decimal("111"), 3)
        assert (authority.stop_loss, authority.take_profit, authority.version) == (
            trade.stop_loss, trade.take_profit, trade.protection_levels_version)
        assert authority.levels_effective_at is not None
        trade.account_id = "another-account"
        with pytest.raises(DBAPIError, match="identity conflict"):
            await db.commit()
        await db.rollback()
