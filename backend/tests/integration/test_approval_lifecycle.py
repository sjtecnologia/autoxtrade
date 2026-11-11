import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import make_url, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import settings
from database import Base
from db.models import EntryApproval, ExecutionIntent, Trade, TradeEvent
from trading.approval_service import EntryApprovalService
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.contracts import Capability, ExecutionContext, ExecutionMode, Instrument
from trading.order_executor import OrderExecutor
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
            EntryApproval.__table__,
        ]
        async with tenant_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=tables)
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
