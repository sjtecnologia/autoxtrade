import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import make_url, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import settings
from database import Base
from db.models import EntryApproval, ExecutionIntent, PaperSafeBreakDecision, ProtectionAuthority, Trade, TradeEvent
from paper.durable_effects import PaperProtectionEvidenceRecord
from tests.integration.paper_protection_helpers import ControlledEvidenceClock, PostFillSyntheticProvider
from trading.approval_service import EntryApprovalService
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.contracts import Capability, ExecutionContext, ExecutionMode, Instrument
from trading.contracts import ExecutionResult, ResultState
from trading.order_executor import OrderExecutor, OrderExecutionError
from trading.safety import AccountNature
from trading.connectors.types import OrderSide


def _context(symbol: str) -> ExecutionContext:
    return ExecutionContext(
        venue="approval-test-venue",
        broker="approval-test-broker",
        account_id=f"approval-test-{uuid4().hex}",
        nature=AccountNature.SIMULATED,
        verified=True,
        mode=ExecutionMode.PAPER,
        market="CRIPTO",
        account_currency="USDT",
        position_model="spot",
        instrument=Instrument(
            symbol=symbol,
            asset_class="crypto",
            base_currency="BTC",
            quote_currency="USDT",
            contract_size=Decimal("1"),
            tick_size=Decimal("1"),
            tick_value=Decimal("1"),
            tick_currency="USDT",
            min_volume=Decimal("0.001"),
            max_volume=Decimal("100"),
            volume_step=Decimal("0.001"),
            price_precision=0,
            volume_precision=3,
            margin_model="cash",
            margin_rate=Decimal("1"),
            min_notional=Decimal("1"),
        ),
        capabilities=frozenset(Capability),
    )


async def _seed(
    sessions,
    *,
    status: str = "approved",
    intent_status: str = "reserved",
    expires_at: datetime | None = None,
    lease_until: datetime | None = None,
    symbol: str = "BTC/USDT",
) -> tuple[str, str]:
    approval_id = str(uuid4())
    intent_id = uuid4().hex
    now = datetime.now(timezone.utc)
    expiry = expires_at or now + timedelta(minutes=10)
    async with sessions() as db:
        db.add(
            ExecutionIntent(
                intent_id=intent_id,
                market="CRIPTO",
                mode="paper",
                symbol=symbol,
                side="buy",
                quantity=Decimal("1"),
                status=intent_status,
                expires_at=expiry,
            )
        )
        await db.flush()
        db.add(
            EntryApproval(
                approval_id=approval_id,
                intent_id=intent_id,
                market="CRIPTO",
                symbol=symbol,
                side="buy",
                mode="paper",
                status=status,
                dedupe_key=uuid4().hex,
                payload={
                    "id": approval_id,
                    "intent_id": intent_id,
                    "market": "CRIPTO",
                    "symbol": symbol,
                    "side": "buy",
                    "mode": "paper",
                    "entry_price": "100",
                    "stop_loss": "95",
                    "take_profit": "110",
                    "quantity": "1",
                },
                created_at=now,
                updated_at=now,
                expires_at=expiry,
                lease_until=lease_until,
            )
        )
        await db.commit()
    return approval_id, intent_id


@pytest.fixture
async def approval_database():
    database_url = make_url(settings.database_url)
    endpoint = f"{database_url.host}:{database_url.port or 5432}"
    if endpoint not in os.environ.get("AUTOXTRADE_TEST_ENDPOINTS", "").split(","):
        pytest.skip("isolated PostgreSQL endpoint is not explicitly enabled")

    schema = f"approval_test_{uuid4().hex}"
    admin_engine = create_async_engine(str(database_url))
    tenant_engine = None
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (OSError, DBAPIError):
        await admin_engine.dispose()
        pytest.skip("isolated PostgreSQL endpoint is unavailable")

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        tenant_engine = create_async_engine(
            str(database_url),
            connect_args={"server_settings": {"search_path": schema}},
        )
        tables = [
            Trade.__table__,
            TradeEvent.__table__,
            ExecutionIntent.__table__,
            ProtectionAuthority.__table__,
            EntryApproval.__table__,
            PaperSafeBreakDecision.__table__,
        ]
        async with tenant_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=tables)
            from pathlib import Path
            from runpy import run_path
            migration = run_path(str(Path(__file__).parents[2] / "alembic/versions/011_protection_authority.py"))
            await connection.execute(text(migration["AUTHORITY_TRIGGER"]))
            await connection.execute(text("""
                CREATE TRIGGER autoxtrade_guard_protection_authority
                BEFORE INSERT OR UPDATE OR DELETE ON protection_authorities FOR EACH ROW
                EXECUTE FUNCTION autoxtrade_guard_protection_authority()
            """))
            current_migration = run_path(str(Path(__file__).parents[2] / "alembic/versions/016_immutable_protection_receipts.py"))
            await connection.execute(text(current_migration["AUTHORITY_TRIGGER"]))
            safe_break_migration = run_path(str(Path(__file__).parents[2] / "alembic/versions/017_paper_safe_break_decisions.py"))
            await connection.execute(text(safe_break_migration["DECISION_TRIGGER"]))
            await connection.execute(text("""
                CREATE TRIGGER autoxtrade_guard_paper_safe_break_decision
                BEFORE INSERT OR UPDATE OR DELETE ON paper_safe_break_decisions FOR EACH ROW
                EXECUTE FUNCTION autoxtrade_guard_paper_safe_break_decision()
            """))
            await connection.execute(text(safe_break_migration["AUTHORITY_TRIGGER"]))
            await connection.execute(text(migration["TRADE_TRIGGER"]))
            await connection.execute(text("""
                CREATE TRIGGER autoxtrade_version_protection_levels
                BEFORE INSERT OR UPDATE ON trades FOR EACH ROW
                EXECUTE FUNCTION autoxtrade_version_protection_levels()
            """))
        yield async_sessionmaker(tenant_engine, expire_on_commit=False)
    finally:
        if tenant_engine is not None:
            await tenant_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.asyncio
async def test_two_workers_claim_one_approval_and_expiry_preserves_execution(
    approval_database,
):
    sessions = approval_database
    approval_id, intent_id = await _seed(sessions)
    workers = [
        EntryApprovalService(sessions, redis_client=None),
        EntryApprovalService(sessions, redis_client=None),
    ]
    try:
        claims = await asyncio.gather(
            workers[0].pop_approved_entries(limit=1),
            workers[1].pop_approved_entries(limit=1),
        )
        claimed = [entry for batch in claims for entry in batch]
        assert len(claimed) == 1
        assert claimed[0]["id"] == approval_id

        expired_approval, expired_intent = await _seed(
            sessions,
            status="approved",
            intent_status="reserved",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            symbol="EXPIRED/USDT",
        )
        recovered_approval, recovered_intent = await _seed(
            sessions,
            status="executing",
            intent_status="planned",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            lease_until=datetime.now(timezone.utc) - timedelta(seconds=1),
            symbol="RECOVER/USDT",
        )

        recovered = await workers[0].pop_approved_entries(limit=10)
        assert any(
            row["id"] == recovered_approval and row["_recovered_executing"]
            for row in recovered
        )
        assert all(row["id"] != expired_approval for row in recovered)
        async with sessions() as db:
            expired = await db.get(EntryApproval, expired_approval)
            expired_execution = (
                await db.execute(
                    select(ExecutionIntent).where(
                        ExecutionIntent.intent_id == expired_intent
                    )
                )
            ).scalar_one()
            in_progress = await db.get(EntryApproval, recovered_approval)
            in_progress_execution = (
                await db.execute(
                    select(ExecutionIntent).where(
                        ExecutionIntent.intent_id == recovered_intent
                    )
                )
            ).scalar_one()
            assert expired.status == "expired"
            assert expired_execution.status == "failed"
            assert in_progress.status == "executing"
            assert in_progress_execution.status == "planned"
    finally:
        for worker in workers:
            await worker.close()


@pytest.mark.asyncio
async def test_approval_is_durable_and_reuses_unique_intent(
    approval_database, monkeypatch
):
    from trading import approval_service

    monkeypatch.setattr(approval_service, "require_paper_new_exposure", lambda _: None)
    sessions = approval_database
    intent_id = uuid4().hex
    async with sessions() as db:
        db.add(
            ExecutionIntent(
                intent_id=intent_id,
                market="CRIPTO",
                mode="paper",
                symbol="BTC/USDT",
                side="buy",
                quantity=Decimal("1"),
                status="reserved",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
        )
        await db.commit()
    service = EntryApprovalService(sessions, redis_client=None)
    approval_args = {
        "intent_id": intent_id,
        "market": "CRIPTO",
        "symbol": "BTC/USDT",
        "side": "buy",
        "mode": "paper",
        "entry_price": Decimal("100"),
        "stop_loss": Decimal("95"),
        "take_profit": Decimal("110"),
        "quantity": Decimal("1"),
        "criteria": {},
        "details": {},
        "ttl_seconds": 300,
    }
    try:
        created = await service.create_pending_entry(**approval_args)
        replayed = await service.create_pending_entry(**approval_args)
        assert created is not None
        assert replayed is not None
        assert replayed["id"] == created["id"]
        assert replayed["intent_id"] == intent_id

        other_intent = uuid4().hex
        async with sessions() as db:
            db.add(
                ExecutionIntent(
                    intent_id=other_intent,
                    market="CRIPTO",
                    mode="paper",
                    symbol="BTC/USDT",
                    side="buy",
                    quantity=Decimal("1"),
                    status="reserved",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                )
            )
            await db.commit()
        assert await service.create_pending_entry(
            **{**approval_args, "intent_id": other_intent}
        ) is None
        stored = await service.get_entry(created["id"])
        assert stored is not None
        assert stored["intent_id"] == intent_id
        assert stored["status"] == "pending"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_recovery_after_order_send_queries_and_does_not_resend(approval_database):
    sessions = approval_database
    approval_id, intent_id = await _seed(sessions)
    service = EntryApprovalService(sessions, redis_client=None)
    try:
        first_claim = (await service.pop_approved_entries(limit=1))[0]
        context = _context("BTC/USDT")
        transport = MemoryTransport(context)
        transport.prices[context.instrument.symbol] = Decimal("100")
        connector = SimulatedConnector(context, transport=transport)

        await connector.connect()
        async with sessions() as db:
            first_trade = await OrderExecutor(connector, db).open_position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                entry_price=Decimal("100"),
                stop_loss=Decimal("95"),
                take_profit=Decimal("110"),
                market="CRIPTO",
                intent_id=intent_id,
            )
        assert first_trade.intent_id == intent_id
        assert len(transport.intents) == 1

        # Simulate process death after the exchange and trade commit but before
        # the approval's terminal transition.
        async with sessions() as db:
            row = await db.get(EntryApproval, approval_id)
            row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()

        recovered = (await service.pop_approved_entries(limit=1))[0]
        assert recovered["_recovered_executing"] is True
        observed = await connector.query_order(intent_id=intent_id)
        assert observed.state.value == "confirmed"
        persisted_status = await service.record_observed_execution(
            approval_id,
            recovered["_claim_token"],
            state=observed.state.value,
            order_id=observed.order_id,
            position_id=observed.position_id,
            filled_quantity=observed.filled,
        )
        assert persisted_status == "confirmed"
        async with sessions() as db:
            second_trade = await OrderExecutor(connector, db).open_position(
                symbol="BTC/USDT",
                side=OrderSide.BUY,
                quantity=Decimal("1"),
                entry_price=Decimal("100"),
                stop_loss=Decimal("95"),
                take_profit=Decimal("110"),
                market="CRIPTO",
                intent_id=intent_id,
            )
        assert second_trade.id == first_trade.id
        assert len(transport.intents) == 1
        assert await service.mark_entry_executed(
            approval_id, recovered["_claim_token"]
        )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_partial_fill_recovery_updates_existing_trade(approval_database):
    sessions = approval_database
    from paper.durable_effects import DurablePaperEffects, PaperEffectOperation, PaperEffectPosition, PaperEntryFill, PaperProtectionAttempt
    async with sessions.kw["bind"].begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[PaperEffectPosition.__table__, PaperEffectOperation.__table__, PaperEntryFill.__table__,
                    PaperProtectionAttempt.__table__, PaperProtectionEvidenceRecord.__table__],
        )
    _, intent_id = await _seed(sessions)
    async with sessions() as db:
        intent = (await db.execute(
            select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id)
        )).scalar_one()
        intent.quantity = Decimal("0.01")
        intent.risk_amount = Decimal("20")
        intent.capital_amount = Decimal("500")
        await db.commit()
    context = _context("BTC/USDT")
    evidence_clock = ControlledEvidenceClock()
    evidence_provider = PostFillSyntheticProvider(
        reference="51000", history_low="50999"
    ).bind_clock(evidence_clock)
    transport = MemoryTransport(context, durable_effects=DurablePaperEffects(
        sessions, context, evidence_clock=evidence_clock,
    ))
    transport.prices[context.instrument.symbol] = Decimal("50000")
    transport.scenarios[intent_id] = Decimal("0.005")
    connector = SimulatedConnector(context, transport=transport)
    connector.create_order = AsyncMock(wraps=connector.create_order)
    request = {
        "symbol": "BTC/USDT", "side": OrderSide.BUY,
        "quantity": Decimal("0.01"), "entry_price": Decimal("50000"),
        "stop_loss": Decimal("48000"), "take_profit": Decimal("55000"),
        "market": "CRIPTO", "intent_id": intent_id,
    }
    async with sessions() as db:
        with pytest.raises(OrderExecutionError, match="partially executed"):
            await OrderExecutor(connector, db, evidence_provider=evidence_provider).open_position(**request)
    prior = await connector.query_order(intent_id=intent_id)
    async with sessions() as db:
        with pytest.raises(OrderExecutionError, match="outcome unavailable"):
            connector.query_order = AsyncMock(return_value=ExecutionResult(
                intent_id, ResultState.UNAVAILABLE, reason="test unavailable"))
            await OrderExecutor(connector, db, evidence_provider=evidence_provider).open_position(**request)
    intent, trade = await _partial_state(sessions, intent_id)
    assert intent.order_terminal is None
    assert intent.filled_quantity == Decimal("0.005")
    assert intent.risk_amount == Decimal("20")
    assert intent.capital_amount == Decimal("500")
    connector.query_order = SimulatedConnector.query_order.__get__(connector)
    await connector.observe_paper_fill(intent_id, filled=Decimal("0.005"), average=Decimal("51000"),
                                       terminal=False, revision=2)
    async with sessions() as db:
        corrected = await OrderExecutor(connector, db, evidence_provider=evidence_provider).open_position(**request)
        assert corrected.entry_price == Decimal("51000.001")
    current_query = connector.query_order
    connector.query_order = AsyncMock(return_value=prior)
    async with sessions() as db:
        stale = await OrderExecutor(connector, db, evidence_provider=evidence_provider).open_position(**request)
        assert stale.entry_price == Decimal("51000.001")
    connector.query_order = current_query
    await connector.observe_paper_fill(intent_id, filled=Decimal("0.01"), average=Decimal("50000"),
                                       terminal=True, revision=3)
    async with sessions() as db:
        trade = await OrderExecutor(connector, db, evidence_provider=evidence_provider).open_position(**request)
        assert trade.quantity == Decimal("0.01")
        trades = (await db.execute(
            select(Trade).where(Trade.intent_id == intent_id)
        )).scalars().all()
        assert len(trades) == 1
        assert len(transport.intents) == 1
        assert trade.position_id == prior.position_id
        assert connector.create_order.await_count == 1
        assert (await connector.list_positions())[0].quantity == Decimal("0.01")


@pytest.mark.asyncio
async def test_durable_terminal_partial_releases_only_unfilled_reservation(approval_database):
    from paper.durable_effects import DurablePaperEffects, PaperEffectOperation, PaperEffectPosition, PaperEntryFill, PaperProtectionAttempt

    sessions = approval_database
    async with sessions.kw["bind"].begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[PaperEffectPosition.__table__, PaperEffectOperation.__table__, PaperEntryFill.__table__,
                    PaperProtectionAttempt.__table__, PaperProtectionEvidenceRecord.__table__],
        )
    _, intent_id = await _seed(sessions)
    async with sessions() as db:
        intent = (await db.execute(
            select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id)
        )).scalar_one()
        intent.quantity, intent.risk_amount, intent.capital_amount = (
            Decimal("0.01"), Decimal("20"), Decimal("500")
        )
        await db.commit()
    context = _context("BTC/USDT")
    evidence_clock = ControlledEvidenceClock()
    evidence_provider = PostFillSyntheticProvider(
        reference="51000", history_low="50999"
    ).bind_clock(evidence_clock)
    transport = MemoryTransport(context, durable_effects=DurablePaperEffects(
        sessions, context, evidence_clock=evidence_clock,
    ))
    transport.prices[context.instrument.symbol] = Decimal("50000")
    transport.scenarios[intent_id] = Decimal("0.005")
    connector = SimulatedConnector(context, transport=transport)
    connector.create_order = AsyncMock(wraps=connector.create_order)
    request = {
        "symbol": "BTC/USDT", "side": OrderSide.BUY, "quantity": Decimal("0.01"),
        "entry_price": Decimal("50000"), "stop_loss": Decimal("48000"),
        "take_profit": Decimal("55000"), "market": "CRIPTO", "intent_id": intent_id,
    }
    async with sessions() as db:
        with pytest.raises(OrderExecutionError, match="partially executed"):
            await OrderExecutor(connector, db, evidence_provider=evidence_provider).open_position(**request)
    await connector.observe_paper_fill(intent_id, filled=Decimal("0.005"),
                                       average=Decimal("50000"), terminal=True, revision=2)
    async with sessions() as db:
        trade = await OrderExecutor(connector, db, evidence_provider=evidence_provider).open_position(**request)
        assert trade.quantity == Decimal("0.005")
        assert trade.risk_amount == Decimal("10")
        assert trade.capital_amount == Decimal("250")
    intent, _ = await _partial_state(sessions, intent_id)
    assert intent.order_terminal is True
    assert intent.status == "protection_pending"
    assert connector.create_order.await_count == 1


async def _partial_setup(sessions):
    _, intent_id = await _seed(sessions)
    async with sessions() as db:
        intent = (await db.execute(
            select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id)
        )).scalar_one()
        intent.quantity = Decimal("0.01")
        intent.risk_amount = Decimal("20")
        intent.capital_amount = Decimal("500")
        await db.commit()
    context = _context("BTC/USDT")
    transport = MemoryTransport(context)
    transport.prices[context.instrument.symbol] = Decimal("50000")
    transport.scenarios[intent_id] = Decimal("0.005")
    connector = SimulatedConnector(context, transport=transport)
    request = {
        "symbol": "BTC/USDT", "side": OrderSide.BUY,
        "quantity": Decimal("0.01"), "entry_price": Decimal("50000"),
        "stop_loss": Decimal("48000"), "take_profit": Decimal("55000"),
        "market": "CRIPTO", "intent_id": intent_id,
    }
    async with sessions() as db:
        with pytest.raises(OrderExecutionError, match="partially executed"):
            await OrderExecutor(connector, db).open_position(**request)
    connector.modify_position_protection = AsyncMock(
        side_effect=AssertionError("recovery must not mutate protection")
    )
    connector.reduce_position = AsyncMock(
        side_effect=AssertionError("recovery must not reduce exposure")
    )
    return intent_id, transport, connector, request


async def _partial_state(sessions, intent_id):
    async with sessions() as db:
        intent = (await db.execute(
            select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id)
        )).scalar_one()
        trades = (await db.execute(
            select(Trade).where(Trade.intent_id == intent_id)
        )).scalars().all()
        assert len(trades) == 1
        return intent, trades[0]


@pytest.mark.asyncio
async def test_partial_recovery_growth_repetition_and_stale_observation(approval_database):
    sessions = approval_database
    intent_id, transport, connector, request = await _partial_setup(sessions)
    stale = transport.query(intent_id=intent_id)
    transport.settle(intent_id, Decimal("0.008"), Decimal("51000"))
    async with sessions() as db:
        trade = await OrderExecutor(connector, db).open_position(**request)
        assert trade.quantity == Decimal("0.008")
        assert trade.entry_price == Decimal("50375.001")
        assert trade.entry_value == Decimal("403.00")
        assert trade.risk_amount == Decimal("16")
        assert trade.capital_amount == Decimal("400")
    intent, trade = await _partial_state(sessions, intent_id)
    assert intent.status == "protection_pending"
    assert intent.order_terminal is None
    assert intent.filled_quantity == Decimal("0.008")
    connector.query_order = AsyncMock(return_value=stale)
    async with sessions() as db:
        replay = await OrderExecutor(connector, db).open_position(**request)
        assert replay.quantity == Decimal("0.008")
        assert replay.entry_price == Decimal("50375.001")
    connector.query_order.return_value = replace(
        stale, filled=Decimal("0.008"), average=Decimal("49000")
    )
    async with sessions() as db:
        replay = await OrderExecutor(connector, db).open_position(**request)
        assert replay.entry_price == Decimal("50375.001")
    transport.settle(intent_id, Decimal("0.01"), Decimal("51000"))
    connector.query_order.return_value = transport.query(intent_id=intent_id)
    async with sessions() as db:
        final = await OrderExecutor(connector, db).open_position(**request)
        assert final.quantity == Decimal("0.01")
        assert final.entry_price == Decimal("50500.001")
    async with sessions() as db:
        repeated = await OrderExecutor(connector, db).open_position(**request)
        assert repeated.id == final.id
        assert repeated.quantity == final.quantity
    intent, trade = await _partial_state(sessions, intent_id)
    assert intent.order_terminal is True
    assert intent.status == "protection_pending"
    assert len(transport.intents) == 1
    connector.modify_position_protection.assert_not_awaited()
    connector.reduce_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_partial_releases_only_unfilled_reservation(approval_database):
    sessions = approval_database
    intent_id, transport, connector, request = await _partial_setup(sessions)
    order = transport.query(intent_id=intent_id)
    transport.cancel(order.order_id, "cancel-test")
    async with sessions() as db:
        await OrderExecutor(connector, db).open_position(**request)
    intent, trade = await _partial_state(sessions, intent_id)
    assert intent.order_terminal is True
    assert intent.status == "protection_pending"
    assert intent.filled_quantity == Decimal("0.005")
    assert trade.risk_amount == Decimal("10")
    assert trade.capital_amount == Decimal("250")
    assert transport.query(intent_id=intent_id).terminal is True
    async with sessions() as db:
        approval = (await db.execute(
            select(EntryApproval).where(EntryApproval.intent_id == intent_id)
        )).scalar_one()
        approval.status = "executing"
        approval.claim_token = "test-claim"
        approval_id = approval.approval_id
        await db.commit()
    service = EntryApprovalService(sessions, redis_client=None)
    try:
        assert await service.mark_entry_executed(approval_id, "test-claim") is None
    finally:
        await service.close()
    connector.modify_position_protection.assert_not_awaited()
    connector.reduce_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_recovery_queries_without_transaction_and_stays_pending(approval_database):
    sessions = approval_database
    intent_id, transport, connector, request = await _partial_setup(sessions)
    original_query = connector.query_order
    async with sessions() as db:
        async def query(*, intent_id):
            assert not db.in_transaction()
            return await original_query(intent_id=intent_id)

        connector.query_order = query
        await OrderExecutor(connector, db).open_position(**request)
    intent, trade = await _partial_state(sessions, intent_id)
    assert intent.status == "protection_pending"
    assert intent.order_terminal is None
    assert trade.quantity == Decimal("0.005")
    connector.modify_position_protection.assert_not_awaited()
    connector.reduce_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_recovery_rejects_inconsistent_stored_quantities(approval_database):
    sessions = approval_database
    intent_id, transport, connector, request = await _partial_setup(sessions)
    async with sessions() as db:
        trade = (await db.execute(
            select(Trade).where(Trade.intent_id == intent_id)
        )).scalar_one()
        trade.quantity = Decimal("0.008")
        await db.commit()
    transport.settle(intent_id, Decimal("0.006"))
    async with sessions() as db:
        with pytest.raises(OrderExecutionError, match="quantities disagree"):
            await OrderExecutor(connector, db).open_position(**request)
    intent, trade = await _partial_state(sessions, intent_id)
    assert trade.quantity == Decimal("0.008")
    assert intent.filled_quantity == Decimal("0.005")
    assert intent.status == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [ResultState.UNKNOWN, ResultState.UNAVAILABLE])
async def test_uncertain_partial_query_preserves_existing_data(approval_database, state):
    sessions = approval_database
    intent_id, transport, connector, request = await _partial_setup(sessions)
    connector.query_order = AsyncMock(return_value=ExecutionResult(intent_id, state))
    async with sessions() as db:
        with pytest.raises(OrderExecutionError, match="reservation preserved"):
            await OrderExecutor(connector, db).open_position(**request)
    intent, trade = await _partial_state(sessions, intent_id)
    assert intent.status == "partial"
    assert intent.order_terminal is None
    assert intent.filled_quantity == Decimal("0.005")
    assert trade.quantity == Decimal("0.005")
    assert trade.risk_amount == Decimal("10")
    assert trade.capital_amount == Decimal("250")
    connector.modify_position_protection.assert_not_awaited()
    connector.reduce_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_partial_recovery_ignores_late_snapshot(approval_database):
    sessions = approval_database
    intent_id, transport, connector, request = await _partial_setup(sessions)
    stale = transport.query(intent_id=intent_id)
    waiting = asyncio.Event()
    release = asyncio.Event()
    original_query = connector.query_order
    calls = 0

    async def query(*, intent_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            waiting.set()
            await release.wait()
            return stale
        return await original_query(intent_id=intent_id)

    connector.query_order = query

    async def recover():
        async with sessions() as db:
            return await OrderExecutor(connector, db).open_position(**request)

    old_worker = asyncio.create_task(recover())
    await asyncio.wait_for(waiting.wait(), timeout=5)
    transport.settle(intent_id, Decimal("0.01"), Decimal("51000"))
    try:
        new_trade = await asyncio.wait_for(recover(), timeout=5)
        assert new_trade.quantity == Decimal("0.01")
    finally:
        release.set()
    old_trade = await asyncio.wait_for(old_worker, timeout=5)
    assert old_trade.quantity == Decimal("0.01")
    intent, trade = await _partial_state(sessions, intent_id)
    assert intent.filled_quantity == Decimal("0.01")
    assert intent.order_terminal is True
    assert intent.status == "protection_pending"
    assert trade.entry_price == Decimal("50500.001")
    assert trade.risk_amount == Decimal("20")
    assert trade.capital_amount == Decimal("500")
    assert len(transport.intents) == 1
    connector.modify_position_protection.assert_not_awaited()
    connector.reduce_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["protected", "closed", "unknown"])
async def test_refresh_failure_protects_confirmed_exposure_in_real_session(
    approval_database, outcome,
):
    sessions = approval_database
    _, intent_id = await _seed(sessions)
    context = _context("BTC/USDT")
    transport = MemoryTransport(context)
    transport.prices[context.instrument.symbol] = Decimal("100")
    connector = SimulatedConnector(context, transport=transport)
    original_protection = connector.modify_position_protection
    if outcome == "protected":
        connector.modify_position_protection = AsyncMock(wraps=original_protection)
    else:
        connector.modify_position_protection = AsyncMock(return_value=ExecutionResult(
            "protection", ResultState.REJECTED, reason="unavailable",
        ))
    original_reduce = connector.reduce_position
    if outcome == "unknown":
        connector.reduce_position = AsyncMock(return_value=ExecutionResult(
            "reduction", ResultState.UNKNOWN, reason="response lost",
        ))
    else:
        connector.reduce_position = AsyncMock(wraps=original_reduce)

    async with sessions() as db:
        async def fail_refresh(obj):
            db.expire(obj)
            raise RuntimeError("refresh failed after commit")

        db.refresh = fail_refresh
        with pytest.raises(OrderExecutionError):
            await OrderExecutor(connector, db).open_position(
                symbol="BTC/USDT", side=OrderSide.BUY, quantity=Decimal("1"),
                entry_price=Decimal("100"), stop_loss=Decimal("95"),
                take_profit=Decimal("110"), market="CRIPTO", intent_id=intent_id,
            )
    connector.modify_position_protection.assert_awaited_once()
    async with sessions() as db:
        trade = (await db.execute(
            select(Trade).where(Trade.intent_id == intent_id)
        )).scalar_one()
        events = (await db.execute(
            select(TradeEvent).where(TradeEvent.trade_id == trade.id)
        )).scalars().all()
        event_types = [event.event_type for event in events]
        if outcome == "protected":
            assert trade.status == "open"
            assert transport.positions[trade.position_id].stop_loss == Decimal("95")
            assert "PROTECTION_CONFIRMED" in event_types
            connector.reduce_position.assert_not_awaited()
        else:
            assert connector.reduce_position.await_args.args[1] == Decimal("1")
            assert "PROTECTION_FAILED" in event_types
            if outcome == "closed":
                assert trade.status == "closed"
                assert "PROTECTION_FAILED_CLOSED" in event_types
            else:
                assert trade.status == "open"
                unresolved = next(event for event in events if event.event_type == "UNPROTECTED_POSITION")
                assert unresolved.payload["close_state"] == "unknown"
    assert len(transport.intents) == 1
