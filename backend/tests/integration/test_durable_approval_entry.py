import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from config import settings
from database import Base
from db.models import (
    BotConfig, EntryApproval, EquitySnapshot, ExecutionIntent, MLModel,
    ProtectionAuthority, RiskAccountLock, Trade,
)
from paper.durable_effects import (
    DurablePaperEffects, PaperEffectOperation, PaperEffectPosition, PaperEntryFill,
)
from risk.correlation_checker import CorrelationChecker
from risk.drawdown_monitor import DrawdownMonitor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer
from tests.integration.paper_protection_helpers import (
    ControlledEvidenceClock, PostFillSyntheticProvider, register_fixed_provider,
)
from tests.integration.test_approval_lifecycle import _context, approval_database
from tests.integration.test_initial_paper_protection import setup
from tests.integration.test_paper_durable_effects import effect_sessions
from trading.approval_service import EntryApprovalService
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.order_executor import OrderExecutionError, OrderExecutor


@pytest_asyncio.fixture
async def approval_flow(effect_sessions, monkeypatch):
    import sqlalchemy.ext.asyncio as sqlalchemy_async
    import trading.approval_service as approvals
    from notifications.telegram import notifier
    from tasks import trading_tasks

    sessions = effect_sessions
    async with sessions.kw["bind"].begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=[
            MLModel.__table__, BotConfig.__table__, EquitySnapshot.__table__, RiskAccountLock.__table__,
        ])
    context = _context("BTC/USDT")
    monkeypatch.setattr(settings, "paper_initial_capital", Decimal("10000"))
    async with sessions() as db:
        db.add(BotConfig(
            market="CRIPTO", is_active=True, mode="paper", exchange=context.venue,
            enabled_symbols=["BTC/USDT"], execution_context={"BTC/USDT": context.snapshot()},
            risk_per_trade_pct=Decimal("1"), max_open_trades=20,
            max_drawdown_pct=Decimal("15"), drawdown_alert_pct=Decimal("10"),
            max_corr_threshold=Decimal("0.7"), drawdown_status="OK",
        ))
        await db.commit()
        risk = RiskManager(db, PositionSizer(), DrawdownMonitor(db), CorrelationChecker())
        reservation = await risk.can_open_position(
            "CRIPTO", "BTC/USDT", Decimal("100"), Decimal("95"),
            side="buy", requested_quantity=Decimal("1"),
        )
        assert reservation.approved and reservation.intent_id
    service = EntryApprovalService(sessions, redis_client=None)
    entry = await service.create_pending_entry(
        intent_id=reservation.intent_id, market="CRIPTO", symbol="BTC/USDT",
        side="buy", mode="paper", entry_price=Decimal("100"), stop_loss=Decimal("95"),
        take_profit=Decimal("110"), quantity=Decimal("1"), criteria={}, details={}, ttl_seconds=600,
    )
    assert entry is not None
    assert await service.update_entry_status(entry["id"], "approved", expected_status="pending")
    async with sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == reservation.intent_id
        ))).scalar_one()
        risk_amount, capital_amount = intent.risk_amount, intent.capital_amount
    clock = ControlledEvidenceClock()
    provider = PostFillSyntheticProvider().bind_clock(clock)
    register_fixed_provider(monkeypatch, provider)
    init_effects = DurablePaperEffects.__init__

    def init_with_test_clock(self, sessions, context, **kwargs):
        if kwargs.get("evidence_clock") is None:
            kwargs["evidence_clock"] = clock
        init_effects(self, sessions, context, **kwargs)

    monkeypatch.setattr(DurablePaperEffects, "__init__", init_with_test_clock)
    monkeypatch.setattr(approvals, "EntryApprovalService", lambda: service)
    monkeypatch.setattr(sqlalchemy_async, "create_async_engine", lambda *args, **kwargs: SimpleNamespace(
        dispose=AsyncMock()
    ))
    monkeypatch.setattr(sqlalchemy_async, "async_sessionmaker", lambda *args, **kwargs: sessions)
    monkeypatch.setattr(trading_tasks, "_current_price_for_approval", AsyncMock(return_value=Decimal("100")))
    monkeypatch.setattr(notifier, "send_message", AsyncMock())
    return SimpleNamespace(
        sessions=sessions, service=service, approval_id=entry["id"], intent_id=reservation.intent_id,
        context=context, provider=provider, clock=clock, risk_amount=risk_amount,
        capital_amount=capital_amount, task=trading_tasks._process_entry_approvals_async,
    )


async def expire_approval_and_protection(flow):
    async with flow.sessions() as db:
        await db.execute(text(
            "UPDATE entry_approvals SET lease_until = clock_timestamp() - interval '1 second' "
            "WHERE approval_id = :approval_id"
        ), {"approval_id": flow.approval_id})
        await db.execute(text(
            "UPDATE paper_protection_attempts SET lease_until = clock_timestamp() - interval '1 second' "
            "WHERE owner IS NOT NULL"
        ))
        await db.commit()


async def verify_flow(flow, *, status, quantity, trade_exists=True, protection_count=1):
    async with flow.sessions() as db:
        approval = await db.get(EntryApproval, flow.approval_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == flow.intent_id
        ))).scalar_one()
        trades = (await db.execute(select(Trade).where(Trade.intent_id == flow.intent_id))).scalars().all()
        positions = (await db.execute(select(PaperEffectPosition))).scalars().all()
        entries = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id == "entry:" + flow.intent_id
        ))).scalars().all()
        assert approval.status == status
        assert len(trades) == int(trade_exists) and len(positions) == len(entries) == 1
        assert positions[0].position_id == "paper_" + flow.intent_id
        assert positions[0].quantity == Decimal(quantity)
        assert intent.risk_amount == flow.risk_amount and intent.capital_amount == flow.capital_amount
        fills = (await db.execute(select(PaperEntryFill).where(
            PaperEntryFill.position_id == positions[0].position_id
        ))).scalars().all()
        assert sum((fill.quantity for fill in fills), Decimal("0")) == Decimal(quantity)
        if trades:
            trade = trades[0]
            assert trade.intent_id == flow.intent_id and trade.quantity == Decimal(quantity)
            assert trade.model_version == "didi-manual"
            assert trade.entry_price == Decimal("100.001")
            assert trade.execution_context["paper_costs"] == entries[0].payload["paper_costs"]
        if status == "executed":
            assert intent.status == "confirmed" and intent.order_terminal is True
            assert positions[0].stop_loss == Decimal("95") and positions[0].take_profit == Decimal("110")
            assert len((await db.execute(select(PaperEffectOperation).where(
                PaperEffectOperation.payload["kind"].astext == "protect"
            ))).scalars().all()) == protection_count


@pytest.mark.asyncio
@pytest.mark.parametrize("workers", [1, 2])
async def test_real_approval_opens_one_protected_durable_entry(approval_flow, workers):
    flow = approval_flow
    await asyncio.gather(*(flow.task() for _ in range(workers)))
    await flow.task()
    assert await flow.service.update_entry_status(
        flow.approval_id, "approved", expected_status="pending"
    ) is None
    await verify_flow(flow, status="executed", quantity="1")


@pytest.mark.asyncio
async def test_real_approval_partial_remains_pending(approval_flow, monkeypatch):
    flow = approval_flow
    original = MemoryTransport.durable_create

    async def partial(self, intent):
        self.scenarios[intent.id] = Decimal("0.5")
        return await original(self, intent)

    monkeypatch.setattr(MemoryTransport, "durable_create", partial)
    await flow.task()
    await verify_flow(flow, status="executing", quantity="0.5")
    await expire_approval_and_protection(flow)
    await flow.task()
    await verify_flow(flow, status="executing", quantity="0.5")


@pytest.mark.asyncio
async def test_real_approval_without_provider_remains_pending_and_recovers(approval_flow, monkeypatch):
    flow = approval_flow
    monkeypatch.setattr(settings, "protection_evidence_provider", "")
    await flow.task()
    await verify_flow(flow, status="executing", quantity="1")
    async with flow.sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == flow.intent_id
        ))).scalar_one()
        approval = await db.get(EntryApproval, flow.approval_id)
        claim = approval.claim_token
        intent.status = "confirmed"
        await db.commit()
    assert await flow.service.mark_entry_executed(flow.approval_id, claim) is None
    register_fixed_provider(monkeypatch, flow.provider)
    await expire_approval_and_protection(flow)
    await flow.task()
    await verify_flow(flow, status="executed", quantity="1")


@pytest.mark.asyncio
async def test_partial_fill_growth_recovers_same_approval(approval_flow, monkeypatch):
    flow = approval_flow
    original = MemoryTransport.durable_create

    async def partial(self, intent):
        self.scenarios[intent.id] = Decimal("0.5")
        return await original(self, intent)

    monkeypatch.setattr(MemoryTransport, "durable_create", partial)
    await flow.task()
    await verify_flow(flow, status="executing", quantity="0.5")
    effects = DurablePaperEffects(flow.sessions, flow.context, evidence_clock=flow.clock)
    await effects.observe_entry(
        flow.intent_id, filled=Decimal("1"), average=Decimal("100"), terminal=True, revision=2,
    )
    costs = json.loads(settings.paper_costs_json)
    costs["BTC/USDT"]["slippage_price"] = "0.5"
    monkeypatch.setattr(settings, "paper_costs_json", json.dumps(costs))
    await expire_approval_and_protection(flow)
    await flow.task()
    await verify_flow(flow, status="executed", quantity="1", protection_count=2)


@pytest.mark.asyncio
async def test_lost_protection_response_recovers_receipt_without_provider(approval_flow, monkeypatch):
    flow = approval_flow
    original = SimulatedConnector.modify_position_protection

    async def lost(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise TimeoutError("protection response lost")

    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", lost)
    await flow.task()
    await verify_flow(flow, status="executing", quantity="1")
    await expire_approval_and_protection(flow)
    monkeypatch.setattr(settings, "protection_evidence_provider", "")

    async def no_reapply(*args, **kwargs):
        raise AssertionError("existing protection receipt must not be reapplied")

    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", no_reapply)
    await flow.task()
    await verify_flow(flow, status="executed", quantity="1")


@pytest.mark.asyncio
async def test_unknown_entry_without_receipt_never_resends_or_releases_reservation(approval_flow, monkeypatch):
    flow = approval_flow
    sends = 0

    async def unavailable(self, intent):
        nonlocal sends
        sends += 1
        raise TimeoutError("entry outcome unknown before receipt")

    monkeypatch.setattr(SimulatedConnector, "create_order", unavailable)
    await flow.task()
    await expire_approval_and_protection(flow)
    await flow.task()
    async with flow.sessions() as db:
        approval = await db.get(EntryApproval, flow.approval_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == flow.intent_id
        ))).scalar_one()
        assert approval.status == "executing" and intent.status == "unknown"
        assert intent.risk_amount == flow.risk_amount and intent.capital_amount == flow.capital_amount
        assert not (await db.execute(select(Trade))).scalars().all()
        assert not (await db.execute(select(PaperEffectPosition))).scalars().all()
        assert not (await db.execute(select(PaperEffectOperation))).scalars().all()
    assert sends == 1


@pytest.mark.asyncio
async def test_old_protection_receipt_cannot_complete_new_revision(approval_flow, monkeypatch):
    flow = approval_flow
    original = flow.service.mark_entry_executed
    monkeypatch.setattr(flow.service, "mark_entry_executed", AsyncMock(return_value=None))
    await flow.task()
    monkeypatch.setattr(flow.service, "mark_entry_executed", original)
    async with flow.sessions() as db:
        approval = await db.get(EntryApproval, flow.approval_id)
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == flow.intent_id
        ))).scalar_one()
        claim = approval.claim_token
        intent.observation_revision += 1
        await db.commit()
    assert await flow.service.mark_entry_executed(flow.approval_id, claim) is None
    await verify_flow(flow, status="executing", quantity="1")


@pytest.mark.asyncio
async def test_position_price_conflicting_with_entry_receipt_cannot_complete(approval_flow, monkeypatch):
    flow = approval_flow
    original = flow.service.mark_entry_executed
    monkeypatch.setattr(flow.service, "mark_entry_executed", AsyncMock(return_value=None))
    await flow.task()
    monkeypatch.setattr(flow.service, "mark_entry_executed", original)
    async with flow.sessions() as db:
        approval = await db.get(EntryApproval, flow.approval_id)
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        claim = approval.claim_token
        position.entry_price = Decimal("101")
        await db.commit()
    assert await flow.service.mark_entry_executed(flow.approval_id, claim) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["response", "persistence"])
async def test_real_approval_recovers_after_entry_effect(approval_flow, monkeypatch, failure):
    flow = approval_flow
    original_create = SimulatedConnector.create_order
    original_persist = OrderExecutor._persist_confirmed_entry
    sends = 0

    async def lost_response(self, intent):
        nonlocal sends
        sends += 1
        result = await original_create(self, intent)
        if failure == "response":
            raise TimeoutError("entry response lost")
        return result

    async def failed_persistence(*args, **kwargs):
        raise RuntimeError("trade persistence failed after entry")

    monkeypatch.setattr(SimulatedConnector, "create_order", lost_response)
    if failure == "persistence":
        monkeypatch.setattr(OrderExecutor, "_persist_confirmed_entry", failed_persistence)
    await flow.task()
    await verify_flow(flow, status="executing", quantity="1", trade_exists=False)
    async with flow.sessions() as db:
        with pytest.raises(DBAPIError, match="Paper entry request is immutable"):
            await db.execute(text(
                "UPDATE paper_effect_operations SET payload = payload || "
                "'{\"paper_costs\":{}}'::jsonb WHERE operation_id = :operation_id"
            ), {"operation_id": "entry:" + flow.intent_id})
        await db.rollback()
    monkeypatch.setattr(OrderExecutor, "_persist_confirmed_entry", original_persist)
    costs = json.loads(settings.paper_costs_json)
    costs["BTC/USDT"]["slippage_price"] = "0.5"
    monkeypatch.setattr(settings, "paper_costs_json", json.dumps(costs))
    await expire_approval_and_protection(flow)
    await flow.task()
    await verify_flow(flow, status="executed", quantity="1")
    assert sends == 1


@pytest.mark.asyncio
async def test_partial_protected_entry_does_not_complete_approval(effect_sessions):
    connector, intent_id, request = await setup(effect_sessions, partial=True)
    service = EntryApprovalService(effect_sessions, redis_client=None)
    async with effect_sessions() as db:
        approval = (await db.execute(select(EntryApproval).where(
            EntryApproval.intent_id == intent_id
        ))).scalar_one()
        approval.status = "executing"
        approval.claim_token = "test-claim"
        approval.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        approval_id = approval.approval_id
        await db.commit()
    async with effect_sessions() as db:
        with pytest.raises(OrderExecutionError, match="partially executed"):
            await OrderExecutor(
                connector, db, evidence_provider=connector._test_evidence_provider
            ).open_position(**request)
    assert await service.mark_entry_executed(approval_id, "test-claim") is None
    async with effect_sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent_id
        ))).scalar_one()
        approval = await db.get(EntryApproval, approval_id)
        assert intent.status == "partial" and intent.filled_quantity == Decimal("0.5")
        assert approval.status == "executing"
