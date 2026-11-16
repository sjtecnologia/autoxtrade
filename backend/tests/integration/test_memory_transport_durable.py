import asyncio
import multiprocessing
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import text

from config import settings
from paper.durable_effects import DurablePaperEffects, ProtectionEvidenceRequired
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_approval_lifecycle import _context, approval_database
from tests.integration.paper_spawn_worker import run_effect
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.types import OrderSide, OrderType
from trading.contracts import Capability, CapabilityUnavailable, OrderIntent, ResultState


def _connector(sessions, context):
    transport = MemoryTransport(context, durable_effects=DurablePaperEffects(sessions, context))
    transport.prices[context.instrument.symbol] = Decimal("100")
    return SimulatedConnector(context, transport=transport)


@pytest.mark.asyncio
async def test_connector_shares_positions_and_fenced_effect_receipts(effect_sessions):
    context = _context("BTC/USDT")
    first, second = _connector(effect_sessions, context), _connector(effect_sessions, context)
    intent = OrderIntent("entry-1", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    entry = await first.create_order(intent)
    assert entry.terminal is True
    assert await second.query_order(intent_id=intent.id) == entry
    assert await second.create_order(intent) == entry
    assert (await second.list_positions())[0].quantity == Decimal("1")
    assert not hasattr(first._transport, "positions")

    fence = await first.acquire_position_fence(entry.position_id)
    with pytest.raises(ProtectionEvidenceRequired, match="prepared attempt"):
        await first.modify_position_protection(
            entry.position_id, stop_loss=Decimal("95"), take_profit=Decimal("110"),
            intent_id="protect-1", fence=fence, evidence_id="unprepared",
        )
    assert (await first.list_positions())[0].stop_loss is None
    with pytest.raises(ProtectionEvidenceRequired, match="prepared attempt"):
        await second.modify_position_protection(
            entry.position_id, stop_loss=Decimal("94"), take_profit=Decimal("110"),
            intent_id="protect-1", fence=fence, evidence_id="unprepared",
        )
    reduced = await first.reduce_position(
        entry.position_id, Decimal("0.4"), intent_id="reduce-1",
        fence=fence, execution_price=Decimal("100"),
    )
    assert await second.reduce_position(
        entry.position_id, Decimal("0.4"), intent_id="reduce-1",
        fence=fence, execution_price=Decimal("100"),
    ) == reduced
    with pytest.raises(ValueError, match="different payload"):
        await second.reduce_position(entry.position_id, Decimal("0.3"), intent_id="reduce-1",
                                     fence=fence, execution_price=Decimal("100"))
    next_fence = await second.acquire_position_fence(entry.position_id)
    assert next_fence > fence
    with pytest.raises(ValueError, match="Stale fencing"):
        await first.modify_position_protection(
            entry.position_id, stop_loss=Decimal("94"), intent_id="stale-protect",
            fence=fence, evidence_id="unprepared",
        )
    with pytest.raises(ValueError, match="Stale fencing"):
        await first.reduce_position(entry.position_id, Decimal("0.1"), intent_id="stale-reduce",
                                    fence=fence, execution_price=Decimal("100"))
    assert (await second.list_positions())[0].quantity == Decimal("0.6")
    assert await second.create_order(intent) == entry
    with pytest.raises(ValueError, match="different payload"):
        await second.create_order(replace(intent, quantity=Decimal("0.9")))


@pytest.mark.asyncio
async def test_result_recovered_after_commit_in_distinct_process(effect_sessions):
    context = _context("BTC/USDT")
    connector = _connector(effect_sessions, context)
    entry = await connector.create_order(
        OrderIntent("entry-process", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    )
    fence = await connector.acquire_position_fence(entry.position_id)
    async with effect_sessions() as session:
        schema = (await session.execute(text("SELECT current_schema()"))).scalar_one()

    def run_process():
        process = multiprocessing.get_context("spawn").Process(
            target=run_effect, args=(context, settings.database_url, schema, "reduce", entry.position_id, fence)
        )
        process.start()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join()
            raise AssertionError("child process did not finish")
        assert process.exitcode == 0

    await asyncio.to_thread(run_process)
    restarted = _connector(effect_sessions, context)
    assert (await restarted.list_positions())[0].quantity == Decimal("0.6")
    receipt = await restarted.reduce_position(
        entry.position_id, Decimal("0.4"), intent_id="lost-response",
        fence=fence, execution_price=Decimal("100"),
    )
    assert receipt.state is ResultState.CONFIRMED
    assert receipt.filled == Decimal("0.4")
    assert (await restarted.list_positions())[0].quantity == Decimal("0.6")


@pytest.mark.asyncio
async def test_fenced_path_fails_closed_and_durable_mode_rejects_unsupported_operations(effect_sessions):
    context = _context("BTC/USDT")
    plain = SimulatedConnector(context, transport=MemoryTransport(context))
    with pytest.raises(CapabilityUnavailable, match="Durable paper effects required"):
        await plain.acquire_position_fence("position")
    with pytest.raises(CapabilityUnavailable, match="Durable paper effects required"):
        await plain.reduce_position("position", Decimal("1"), intent_id="op",
                                    fence=1, execution_price=Decimal("100"))
    with pytest.raises(CapabilityUnavailable, match="Durable paper effects required"):
        await plain.modify_position_protection("position", stop_loss=Decimal("95"),
                                               intent_id="op", fence=1)
    connector = _connector(effect_sessions, context)
    entry = await connector.create_order(
        OrderIntent("entry-block", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    )
    with pytest.raises(CapabilityUnavailable, match="Fencing token"):
        await connector.reduce_position(entry.position_id, Decimal("1"), intent_id="no-fence")
    with pytest.raises(CapabilityUnavailable, match="immediate market"):
        await connector.create_order(
            OrderIntent("limit-block", context, OrderSide.BUY, OrderType.LIMIT,
                        Decimal("1"), Decimal("100"))
        )
    with pytest.raises(CapabilityUnavailable, match="fill listing"):
        await connector.get_fills()
    with pytest.raises(CapabilityUnavailable, match="In-memory position path disabled"):
        connector._transport.settle("entry-block", Decimal("1"))


@pytest.mark.asyncio
async def test_concurrent_entry_and_closed_position_receipts_remain_idempotent(effect_sessions):
    context = _context("BTC/USDT")
    first, second = _connector(effect_sessions, context), _connector(effect_sessions, context)
    intent = OrderIntent("concurrent", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    entries = await asyncio.gather(first.create_order(intent), second.create_order(intent))
    assert entries[0] == entries[1]
    assert len(await first.list_positions()) == 1
    fence = await first.acquire_position_fence(entries[0].position_id)
    result = await first.reduce_position(entries[0].position_id, Decimal("1"), intent_id="close",
                                         fence=fence, execution_price=Decimal("100"))
    assert await second.list_positions() == []
    assert await second.reduce_position(entries[0].position_id, Decimal("1"), intent_id="close",
                                        fence=fence, execution_price=Decimal("100")) == result
    assert await second.create_order(intent) == entries[0]
    assert await second.list_positions() == []


@pytest.mark.asyncio
async def test_position_list_rejects_mismatched_context_instead_of_hiding_position(effect_sessions):
    context = _context("BTC/USDT")
    connector = _connector(effect_sessions, context)
    await connector.create_order(
        OrderIntent("entry-context", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    )
    changed = replace(context, capabilities=context.capabilities - {Capability.REDUCE})
    with pytest.raises(ValueError, match="context does not match"):
        await _connector(effect_sessions, changed).list_positions()


@pytest.mark.asyncio
async def test_partial_fill_spawn_restart_revisions_and_terminal(effect_sessions):
    context = _context("BTC/USDT")
    first = _connector(effect_sessions, context)
    first._transport.scenarios["partial"] = Decimal("0.5")
    intent = OrderIntent("partial", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    initial = await first.create_order(intent)
    assert initial.filled == Decimal("0.5")
    assert initial.terminal is False and initial.revision == 1
    assert await first.list_pending_orders() == [initial]
    async with effect_sessions() as session:
        schema = (await session.execute(text("SELECT current_schema()"))).scalar_one()

    def run_process():
        process = multiprocessing.get_context("spawn").Process(
            target=run_effect, args=(context, settings.database_url, schema, "fill", intent.id)
        )
        process.start()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join()
            raise AssertionError("child process did not finish")
        assert process.exitcode == 0

    await asyncio.to_thread(run_process)
    restarted = _connector(effect_sessions, context)
    observed = await restarted.query_order(intent_id=intent.id)
    assert observed.filled == Decimal("0.8")
    assert observed.average == Decimal("101.5") and observed.revision == 2
    assert (await restarted.list_positions())[0].quantity == Decimal("0.8")
    assert await restarted.observe_paper_fill(intent.id, filled=Decimal("0.8"),
        average=Decimal("101.5"), terminal=False, revision=2) == observed
    with pytest.raises(ValueError, match="different payload"):
        await restarted.observe_paper_fill(intent.id, filled=Decimal("0.8"),
            average=Decimal("102"), terminal=False, revision=2)
    with pytest.raises(ValueError, match="Stale"):
        await restarted.observe_paper_fill(intent.id, filled=Decimal("0.5"),
            average=Decimal("100"), terminal=False, revision=1)
    with pytest.raises(ValueError, match="Stale"):
        await restarted.observe_paper_fill(intent.id, filled=Decimal("0.5"),
            average=Decimal("100"), terminal=False, revision=3)
    corrected = await restarted.observe_paper_fill(intent.id, filled=Decimal("0.8"),
        average=Decimal("102.25"), terminal=False, revision=3)
    assert corrected.average == Decimal("102.25")
    assert (await restarted.list_positions())[0].quantity == Decimal("0.8")
    assert (await restarted.list_positions())[0].entry_price == Decimal("102.25")
    terminal = await restarted.observe_paper_fill(intent.id, filled=Decimal("0.8"),
        average=Decimal("102.25"), terminal=True, revision=4)
    assert terminal.state is ResultState.PARTIAL and terminal.terminal is True
    assert await restarted.list_pending_orders() == []
    with pytest.raises(ValueError, match="terminal"):
        await restarted.observe_paper_fill(intent.id, filled=Decimal("1"),
            average=Decimal("102"), terminal=True, revision=5)


@pytest.mark.asyncio
async def test_concurrent_repeated_fill_is_applied_once(effect_sessions):
    context = _context("BTC/USDT")
    first, second = _connector(effect_sessions, context), _connector(effect_sessions, context)
    first._transport.scenarios["parallel"] = Decimal("0.5")
    await first.create_order(OrderIntent("parallel", context, OrderSide.BUY, OrderType.MARKET, Decimal("1")))
    async def observe(connector):
        return await connector.observe_paper_fill(
            "parallel", filled=Decimal("0.8"), average=Decimal("100"),
            terminal=False, revision=2,
        )
    results = await asyncio.gather(observe(first), observe(second))
    assert results[0] == results[1]
    assert (await second.list_positions())[0].quantity == Decimal("0.8")
    final = await second.observe_paper_fill(
        "parallel", filled=Decimal("1"), average=Decimal("100"), terminal=True, revision=3,
    )
    assert final.state is ResultState.CONFIRMED and final.terminal is True
    assert (await first.list_positions())[0].quantity == Decimal("1")
