import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import select

from db.models import BotConfig, ExecutionIntent, Trade
from paper.durable_effects import DurablePaperEffects, PaperEffectOperation, PaperEffectPosition
from paper.protection_recovery import recover_pending_paper_protection
from risk.correlation_checker import CorrelationChecker
from risk.drawdown_monitor import DrawdownMonitor
from risk.manager import RiskManager
from risk.position_sizer import PositionSizer
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_durable_approval_entry import approval_flow
from tests.integration.test_paper_durable_effects import effect_sessions
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.types import OrderSide, OrderType
from trading.contracts import ExecutionResult, OrderIntent, ResultState
from trading.order_executor import OrderExecutionError, OrderExecutor


def connector_for(flow):
    effects = DurablePaperEffects(flow.sessions, flow.context, evidence_clock=flow.clock)
    transport = MemoryTransport(flow.context, durable_effects=effects)
    transport.prices[flow.context.instrument.symbol] = Decimal("100")
    return SimulatedConnector(flow.context, transport=transport)


def request_for(flow):
    return {
        "symbol": "BTC/USDT", "side": OrderSide.BUY, "quantity": Decimal("1"),
        "entry_price": Decimal("100"), "stop_loss": Decimal("95"),
        "take_profit": Decimal("110"), "market": "CRIPTO",
        "intent_id": flow.intent_id, "model_version": "didi-manual",
    }


async def assert_reservation_blocks_new_entry(flow):
    async with flow.sessions() as db:
        config = (await db.execute(select(BotConfig).where(BotConfig.market == "CRIPTO"))).scalar_one()
        config.max_open_trades = 1
        await db.commit()
        result = await RiskManager(
            db, PositionSizer(), DrawdownMonitor(db), CorrelationChecker()
        ).can_open_position(
            "CRIPTO", "BTC/USDT", Decimal("100"), Decimal("95"),
            side="buy", requested_quantity=Decimal("1"),
        )
        assert not result.approved
        assert "reservadas" in result.reason


async def assert_intent_and_position(flow, *, intent_status, trade_expected, protected):
    async with flow.sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == flow.intent_id
        ))).scalar_one()
        positions = (await db.execute(select(PaperEffectPosition))).scalars().all()
        entries = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id == "entry:" + flow.intent_id
        ))).scalars().all()
        trades = (await db.execute(select(Trade).where(Trade.intent_id == flow.intent_id))).scalars().all()
        assert intent.status == intent_status
        assert intent.risk_amount == flow.risk_amount and intent.capital_amount == flow.capital_amount
        assert len(positions) == len(entries) == 1
        position = positions[0]
        assert position.position_id == "paper_" + flow.intent_id
        assert position.quantity == Decimal("1")
        assert position.stop_loss == (Decimal("95") if protected else None)
        assert position.take_profit == (Decimal("110") if protected else None)
        assert len(trades) == int(trade_expected)
        if trade_expected:
            trade = trades[0]
            assert trade.status == "open" and trade.quantity == Decimal("1")
            assert trade.position_id == position.position_id == intent.position_id
            assert intent.order_id == trade.open_order_id == position.position_id
            assert intent.filled_quantity == Decimal("1")
            assert intent.observation_revision == 1 and intent.order_terminal is True
            assert trade.risk_amount == flow.risk_amount and trade.capital_amount == flow.capital_amount
            assert trade.execution_context["paper_costs"] == entries[0].payload["paper_costs"]
        protections = (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.payload["kind"].astext == "protect"
        ))).scalars().all()
        assert len(protections) == int(protected)


async def recover(flow):
    return await recover_pending_paper_protection(
        flow.sessions, evidence_provider=flow.provider, evidence_clock=flow.clock,
    )


@pytest.mark.asyncio
async def test_absence_during_send_preserves_reservation_and_recovers_after_crash(approval_flow):
    flow = approval_flow
    connector = connector_for(flow)
    send_started = asyncio.Event()
    resume_send = asyncio.Event()
    original = connector.create_order

    async def paused_send(order):
        send_started.set()
        await resume_send.wait()
        await original(order)
        raise asyncio.CancelledError()

    connector.create_order = paused_send

    async def send():
        async with flow.sessions() as db:
            return await OrderExecutor(connector, db, evidence_provider=flow.provider).open_position(
                **request_for(flow)
            )

    sender = asyncio.create_task(send())
    try:
        await asyncio.wait_for(send_started.wait(), timeout=10)
        async with flow.sessions() as db:
            with pytest.raises(OrderExecutionError, match="unknown"):
                await OrderExecutor(connector_for(flow), db, evidence_provider=flow.provider).open_position(
                    **request_for(flow)
                )
        async with flow.sessions() as db:
            intent = (await db.execute(select(ExecutionIntent).where(
                ExecutionIntent.intent_id == flow.intent_id
            ))).scalar_one()
            assert intent.status in {"planned", "unknown"}
            assert intent.risk_amount == flow.risk_amount and intent.capital_amount == flow.capital_amount
            assert not (await db.execute(select(PaperEffectPosition))).scalars().all()
        await assert_reservation_blocks_new_entry(flow)
        assert (await recover(flow))["selected"] == 0
        resume_send.set()
        with pytest.raises(asyncio.CancelledError):
            await sender
        await assert_intent_and_position(flow, intent_status="unknown", trade_expected=False, protected=False)
        assert await recover(flow) == {"selected": 1, "confirmed": 1, "deferred": 0, "failed": 0}
        await assert_intent_and_position(flow, intent_status="confirmed", trade_expected=True, protected=True)
        assert (await recover(flow))["selected"] == 0
        await assert_reservation_blocks_new_entry(flow)
    finally:
        if not sender.done():
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)


@pytest.mark.asyncio
async def test_late_timeout_cannot_overwrite_confirmed_recovery(approval_flow):
    flow = approval_flow
    connector = connector_for(flow)
    effect_committed = asyncio.Event()
    release_response = asyncio.Event()
    original = connector.create_order

    async def lost_response(order):
        await original(order)
        effect_committed.set()
        await release_response.wait()
        raise TimeoutError("response lost after entry commit")

    connector.create_order = lost_response

    async def send():
        async with flow.sessions() as db:
            return await OrderExecutor(connector, db, evidence_provider=flow.provider).open_position(
                **request_for(flow)
            )

    sender = asyncio.create_task(send())
    try:
        await asyncio.wait_for(effect_committed.wait(), timeout=10)
        await assert_intent_and_position(flow, intent_status="planned", trade_expected=False, protected=False)
        async with flow.sessions() as db:
            await OrderExecutor(connector_for(flow), db)._record_intent_outcome(
                flow.intent_id, None, status="failed",
            )
        await assert_intent_and_position(flow, intent_status="planned", trade_expected=False, protected=False)
        await assert_reservation_blocks_new_entry(flow)
        assert await recover(flow) == {"selected": 1, "confirmed": 1, "deferred": 0, "failed": 0}
        await assert_intent_and_position(flow, intent_status="confirmed", trade_expected=True, protected=True)
        release_response.set()
        with pytest.raises(OrderExecutionError, match="outcome unknown"):
            await sender
        await assert_intent_and_position(flow, intent_status="confirmed", trade_expected=True, protected=True)
        assert (await recover(flow))["selected"] == 0
        await assert_reservation_blocks_new_entry(flow)
    finally:
        if not sender.done():
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)


@pytest.mark.asyncio
async def test_entry_effect_serializes_with_terminal_intent_transition(approval_flow):
    flow = approval_flow
    connector = connector_for(flow)
    async with flow.sessions() as db:
        _, _, is_new = await OrderExecutor(connector, db)._resolve_intent(
            flow.intent_id, flow.context, "BTC/USDT", OrderSide.BUY, Decimal("1"),
            Decimal("100"), Decimal("95"), Decimal("110"),
        )
        assert is_new
    order = OrderIntent(flow.intent_id, flow.context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    entered = asyncio.Event()

    async def send():
        entered.set()
        return await connector.create_order(order)

    async with flow.sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == flow.intent_id
        ).with_for_update())).scalar_one()
        sender = asyncio.create_task(send())
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(sender), timeout=0.25)
            intent.status = "failed"
            await db.commit()
            with pytest.raises(ValueError, match="no longer permits"):
                await sender
        finally:
            if not sender.done():
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
    async with flow.sessions() as db:
        assert not (await db.execute(select(PaperEffectPosition))).scalars().all()
        assert not (await db.execute(select(PaperEffectOperation))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["timeout", "old_rejection", "conflicting_identity"])
async def test_stale_outcome_preserves_persisted_observation(approval_flow, outcome):
    flow = approval_flow
    connector = connector_for(flow)
    async with flow.sessions() as stale_session:
        stale = (await stale_session.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == flow.intent_id
        ))).scalar_one()
        assert stale.status == "reserved"
        await stale_session.commit()
        async with flow.sessions() as fresh_session:
            current = (await fresh_session.execute(select(ExecutionIntent).where(
                ExecutionIntent.intent_id == flow.intent_id
            ))).scalar_one()
            current.status = "unknown"
            current.order_id = "current-order"
            current.position_id = "current-position"
            current.filled_quantity = Decimal("0.5")
            current.observation_revision = 2
            current.result_state = "partial"
            await fresh_session.commit()
        result = None
        status = "unknown"
        if outcome != "timeout":
            result = ExecutionResult(
                flow.intent_id, ResultState.REJECTED if outcome == "old_rejection" else ResultState.UNKNOWN,
                order_id="old-order", position_id="old-position", revision=1,
            )
            status = "failed" if outcome == "old_rejection" else "unknown"
        await OrderExecutor(connector, stale_session)._record_intent_outcome(flow.intent_id, result, status=status)
    async with flow.sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == flow.intent_id
        ))).scalar_one()
        assert (intent.status, intent.order_id, intent.position_id, intent.result_state) == (
            "unknown", "current-order", "current-position", "partial",
        )
        assert intent.filled_quantity == Decimal("0.5") and intent.observation_revision == 2
        assert intent.risk_amount == flow.risk_amount and intent.capital_amount == flow.capital_amount


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["conflicting_identity", "rejection_without_identity"])
async def test_unresolved_identity_is_not_replaced_or_failed(approval_flow, outcome):
    flow = approval_flow
    async with flow.sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == flow.intent_id
        ))).scalar_one()
        intent.status = "unknown"
        intent.order_id = "current-order"
        intent.position_id = "current-position"
        intent.result_state = "unknown"
        await db.commit()
        result = ExecutionResult(
            flow.intent_id,
            ResultState.UNKNOWN if outcome == "conflicting_identity" else ResultState.REJECTED,
            order_id="old-order" if outcome == "conflicting_identity" else None,
            position_id="old-position" if outcome == "conflicting_identity" else None,
        )
        await OrderExecutor(connector_for(flow), db)._record_intent_outcome(
            flow.intent_id, result, status="unknown" if outcome == "conflicting_identity" else "failed",
        )
    async with flow.sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == flow.intent_id
        ))).scalar_one()
        assert (intent.status, intent.order_id, intent.position_id, intent.result_state) == (
            "unknown", "current-order", "current-position", "unknown",
        )
        assert intent.risk_amount == flow.risk_amount and intent.capital_amount == flow.capital_amount
