import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import select, text

from config import settings
from db.models import EntryApproval, ExecutionIntent, Trade, TradeEvent
from paper.durable_effects import DurablePaperEffects, PaperEffectOperation, PaperEffectPosition
from tasks.trading_tasks import _monitor_paper_candles
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_durable_approval_entry import approval_flow, expire_approval_and_protection
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_paper_monitor import frame, monitor_env
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.order_executor import OrderExecutor


async def awaiting_ack(flow, monkeypatch, *, partial=False):
    original_ack = flow.service.mark_entry_executed
    original_create = MemoryTransport.durable_create
    if partial:
        async def create_partial(self, intent):
            self.scenarios[intent.id] = Decimal("0.5")
            return await original_create(self, intent)
        monkeypatch.setattr(MemoryTransport, "durable_create", create_partial)
    monkeypatch.setattr(flow.service, "mark_entry_executed", AsyncMock(return_value=None))
    await flow.task()
    monkeypatch.setattr(flow.service, "mark_entry_executed", original_ack)
    monkeypatch.setattr(MemoryTransport, "durable_create", original_create)
    async with flow.sessions() as db:
        approval = await db.get(EntryApproval, flow.approval_id)
        trade = (await db.execute(select(Trade).where(Trade.intent_id == flow.intent_id))).scalar_one()
        assert approval.status == "executing" and trade.status == "open"
        return trade.id


async def close_before_ack(flow, monkeypatch, redis, *, partial=False, manual=False):
    trade_id = await awaiting_ack(flow, monkeypatch, partial=partial)
    if manual:
        connector = SimulatedConnector(flow.context, transport=MemoryTransport(
            flow.context, durable_effects=DurablePaperEffects(flow.sessions, flow.context),
        ))
        async with flow.sessions() as db:
            trade = await db.get(Trade, trade_id)
            await OrderExecutor(connector, db).close_position(trade, execution_price=Decimal("105"))
    else:
        async with flow.sessions() as db:
            trade = await db.get(Trade, trade_id)
            data = frame()
            start = pd.Timestamp(trade.open_at).floor("h") - pd.Timedelta(hours=75)
            data["timestamp"] = pd.date_range(start, periods=len(data), freq="h")
            trade.paper_monitor_candle_at = data.loc[75, "timestamp"].to_pydatetime()
            trade.paper_monitor_timeframe = "1h"
            await db.commit()
        data.loc[76, "low"] = 94
        await _monitor_paper_candles(flow.sessions, trade_id, data, settings, redis)
    async with flow.sessions() as db:
        assert (await db.get(Trade, trade_id)).status == "closed"
    return trade_id


async def assert_resolution(flow, trade_id, status):
    async with flow.sessions() as db:
        approval = await db.get(EntryApproval, flow.approval_id)
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent).where(ExecutionIntent.intent_id == flow.intent_id))).scalar_one()
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert approval.status == status and approval.dedupe_key is None
        assert approval.claim_token is None and approval.lease_until is None
        assert position.quantity == 0 and trade.status == "closed"
        assert intent.capital_amount == flow.capital_amount and intent.risk_amount == flow.risk_amount
        assert intent.order_terminal is True
        assert intent.status == ("partial" if status == "closed_partial" else "confirmed")
        assert Decimal(approval.payload["quantity"]) == intent.quantity == 1
        resolution = approval.payload["resolution"]
        assert Decimal(resolution["filled_quantity"]) == trade.quantity == intent.filled_quantity
        assert Decimal(resolution["unfilled_quantity"]) == intent.quantity - trade.quantity
        assert resolution["close_operation_id"] == trade.close_order_id
        events = (await db.execute(select(TradeEvent).where(TradeEvent.event_type == "APPROVAL_RESOLVED"))).scalars().all()
        assert len(events) == 1 and events[0].payload["status"] == status
        assert len((await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id.like("manual-close:%")
        ))).scalars().all()) == 1
    assert await flow.service.pop_approved_entries() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("manual,workers", [(False, 1), (False, 2), (True, 1)])
async def test_closed_entry_is_acknowledged_without_reopening(approval_flow, monitor_env, monkeypatch, manual, workers):
    flow = approval_flow
    trade_id = await close_before_ack(flow, monkeypatch, monitor_env, manual=manual)
    await expire_approval_and_protection(flow)
    monkeypatch.setattr(settings, "protection_evidence_provider", "")
    async def no_effect(*args, **kwargs):
        raise AssertionError("Closed execution must be acknowledged without any new effect")
    monkeypatch.setattr(SimulatedConnector, "create_order", no_effect)
    monkeypatch.setattr(SimulatedConnector, "modify_position_protection", no_effect)
    monkeypatch.setattr(SimulatedConnector, "reduce_position", no_effect)
    await asyncio.gather(*[flow.task() for _ in range(workers)])
    await flow.task()
    await assert_resolution(flow, trade_id, "executed")


@pytest.mark.asyncio
async def test_closed_partial_is_terminal_without_claiming_full_execution(approval_flow, monitor_env, monkeypatch):
    flow = approval_flow
    trade_id = await close_before_ack(flow, monkeypatch, monitor_env, partial=True)
    await expire_approval_and_protection(flow)
    await flow.task()
    await assert_resolution(flow, trade_id, "closed_partial")
    from api.routes.approvals import _normalize
    result = _normalize(await flow.service.get_entry(flow.approval_id))
    assert result.status == "closed_partial"
    assert Decimal(result.resolution["filled_quantity"]) == Decimal("0.5")
    # A terminal, accounted closure releases capacity and the approval dedupe key.
    from db.models import BotConfig
    from risk.manager import RiskManager
    from risk.position_sizer import PositionSizer
    from risk.drawdown_monitor import DrawdownMonitor
    from risk.correlation_checker import CorrelationChecker
    async with flow.sessions() as db:
        config = (await db.execute(select(BotConfig))).scalar_one()
        config.max_open_trades = 1
        await db.commit()
        risk = RiskManager(db, PositionSizer(), DrawdownMonitor(db), CorrelationChecker())
        reserved = await risk.can_open_position(
            "CRIPTO", "BTC/USDT", Decimal("100"), Decimal("95"),
            side="buy", requested_quantity=Decimal("1"),
        )
        assert reserved.approved
    new = await flow.service.create_pending_entry(
        intent_id=reserved.intent_id, market="CRIPTO", symbol="BTC/USDT", side="buy", mode="paper",
        entry_price=Decimal("100"), stop_loss=Decimal("95"), take_profit=Decimal("110"),
        quantity=Decimal("1"), criteria={}, details={}, ttl_seconds=600,
    )
    assert new is not None and new["id"] != flow.approval_id and new["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["commission", "pnl_net", "pnl_pct", "exit_value", "entry_value", "revision", "position", "event", "costs"])
async def test_closed_flag_without_consistent_durable_proof_cannot_acknowledge(
    approval_flow, monitor_env, monkeypatch, field,
):
    flow = approval_flow
    trade_id = await close_before_ack(flow, monkeypatch, monitor_env)
    async with flow.sessions() as db:
        trade = await db.get(Trade, trade_id)
        if field in {"commission", "pnl_net", "pnl_pct", "exit_value", "entry_value"}:
            setattr(trade, field, getattr(trade, field) + 1)
        elif field == "revision":
            intent = (await db.execute(select(ExecutionIntent))).scalar_one()
            intent.observation_revision += 1
        elif field == "position":
            (await db.execute(select(PaperEffectPosition))).scalar_one().quantity = Decimal("0.5")
        elif field == "event":
            event = (await db.execute(select(TradeEvent).where(TradeEvent.event_type == "SL_HIT"))).scalar_one()
            event.payload = {**event.payload, "operation_id": "wrong-close"}
        else:
            trade.execution_context = {**trade.execution_context, "paper_costs": {
                **trade.execution_context["paper_costs"], "commission_rate": "0.5",
            }}
        await db.commit()
    await expire_approval_and_protection(flow)
    await flow.task()
    async with flow.sessions() as db:
        approval = await db.get(EntryApproval, flow.approval_id)
        assert approval.status == "executing" and approval.dedupe_key is not None
        assert "resolution" not in approval.payload
        assert not (await db.execute(select(TradeEvent).where(TradeEvent.event_type == "APPROVAL_RESOLVED"))).scalars().all()


@pytest.mark.asyncio
async def test_lost_acknowledgement_response_does_not_duplicate_resolution(approval_flow, monitor_env, monkeypatch):
    flow = approval_flow
    trade_id = await close_before_ack(flow, monkeypatch, monitor_env)
    await expire_approval_and_protection(flow)
    original = flow.service.mark_entry_executed
    async def lost(*args, **kwargs):
        result = await original(*args, **kwargs)
        if result is not None:
            raise TimeoutError("acknowledgement response lost after commit")
        return result
    monkeypatch.setattr(flow.service, "mark_entry_executed", lost)
    await flow.task()
    monkeypatch.setattr(flow.service, "mark_entry_executed", original)
    await flow.task()
    await assert_resolution(flow, trade_id, "executed")


@pytest.mark.asyncio
async def test_old_approval_claim_cannot_acknowledge_after_takeover(approval_flow, monitor_env, monkeypatch):
    flow = approval_flow
    trade_id = await close_before_ack(flow, monkeypatch, monitor_env)
    async with flow.sessions() as db:
        old = (await db.get(EntryApproval, flow.approval_id)).claim_token
    await expire_approval_and_protection(flow)
    claimed = await flow.service.pop_approved_entries()
    new = claimed[0]["_claim_token"]
    assert new != old
    assert await flow.service.mark_entry_executed(flow.approval_id, old) is None
    assert await flow.service.mark_entry_executed(flow.approval_id, new) is not None
    await assert_resolution(flow, trade_id, "executed")


@pytest.mark.asyncio
@pytest.mark.parametrize("zero", [False, True])
async def test_closed_flag_without_close_receipt_is_not_execution_proof(approval_flow, monkeypatch, zero):
    flow = approval_flow
    trade_id = await awaiting_ack(flow, monkeypatch)
    async with flow.sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.status = "closed"
        if zero:
            (await db.execute(select(PaperEffectPosition))).scalar_one().quantity = 0
        await db.commit()
    await expire_approval_and_protection(flow)
    await flow.task()
    async with flow.sessions() as db:
        assert (await db.get(EntryApproval, flow.approval_id)).status == "executing"


@pytest.mark.asyncio
async def test_approval_lease_is_rechecked_after_waiting_for_execution_locks(approval_flow, monitor_env, monkeypatch):
    from trading import approval_service
    flow = approval_flow
    trade_id = await close_before_ack(flow, monkeypatch, monitor_env)
    async with flow.sessions() as db:
        claim = (await db.get(EntryApproval, flow.approval_id)).claim_token
    async with flow.sessions() as blocker:
        await blocker.execute(select(ExecutionIntent).where(ExecutionIntent.intent_id == flow.intent_id).with_for_update())
        pid = (await blocker.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        worker = asyncio.create_task(flow.service.mark_entry_executed(flow.approval_id, claim))
        try:
            async def blocked():
                while True:
                    async with flow.sessions() as observer:
                        count = (await observer.execute(text(
                            "SELECT count(*) FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid))"
                        ), {"pid": pid})).scalar_one()
                    if count:
                        return
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(blocked(), 5)
            class AfterLease(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) + timedelta(minutes=10)
            monkeypatch.setattr(approval_service, "datetime", AfterLease)
            await blocker.commit()
            assert await asyncio.wait_for(worker, 5) is None
        finally:
            await blocker.rollback()
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
    monkeypatch.setattr(approval_service, "datetime", datetime)
    async with flow.sessions() as db:
        row = await db.get(EntryApproval, flow.approval_id)
        assert row.status == "executing" and "resolution" not in row.payload
        assert not (await db.execute(select(TradeEvent).where(TradeEvent.event_type == "APPROVAL_RESOLVED"))).scalars().all()
    assert await flow.service.mark_entry_executed(flow.approval_id, claim) is not None
    await assert_resolution(flow, trade_id, "executed")
