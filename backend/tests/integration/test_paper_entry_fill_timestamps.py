import asyncio
import multiprocessing
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from config import settings
from paper.durable_effects import PaperEffectOperation, PaperEffectPosition, PaperEntryFill
from paper.protection_evidence import (
    Candle, CoveredInterval, EvidenceState, FillActivation, MarketReference, ProtectionEvidence,
    assess_protection_evidence,
)
from tests.integration.paper_spawn_worker import run_effect
from tests.integration.test_approval_lifecycle import _context, approval_database
from tests.integration.test_memory_transport_durable import _connector
from tests.integration.test_paper_durable_effects import effect_sessions
from trading.connectors.types import OrderSide, OrderType
from trading.contracts import OrderIntent


@pytest.mark.asyncio
async def test_full_fill_timestamp_is_database_time_and_stable(effect_sessions):
    context = _context("BTC/USDT")
    connector = _connector(effect_sessions, context)
    intent = OrderIntent("full-timestamp", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    async with effect_sessions() as db:
        before = (await db.execute(select(text("clock_timestamp()")))).scalar_one()
    result = await connector.create_order(intent)
    async with effect_sessions() as db:
        after = (await db.execute(select(text("clock_timestamp()")))).scalar_one()
        row = (await db.execute(select(PaperEntryFill))).scalar_one()
        assert row.executed_at == result.executions[0].executed_at
        assert before <= row.executed_at <= after
        assert row.quantity == row.cumulative_quantity == Decimal("1")
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(text("UPDATE paper_entry_fills SET executed_at = clock_timestamp()"))
            await db.commit()
    assert result.execution_history_complete is True
    assert result.executions[0].execution_id == "entry:full-timestamp:1"
    assert isinstance(result.executions[0].executed_at, datetime)
    assert await connector.create_order(intent) == result
    assert await _connector(effect_sessions, context).query_order(intent_id=intent.id) == result


@pytest.mark.asyncio
async def test_intrabar_evaluator_uses_exact_persisted_fill_timestamp(effect_sessions):
    context = _context("BTC/USDT")
    connector = _connector(effect_sessions, context)
    result = await connector.create_order(
        OrderIntent("evidence-fill", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    )
    fill = result.executions[0]
    current_bar = fill.executed_at.replace(minute=0, second=0, microsecond=0)
    decision_at = current_bar - timedelta(hours=1)
    close_at = current_bar + timedelta(hours=1)
    boundary = fill.executed_at == current_bar
    bar = Candle(current_bar, Decimal("100"), Decimal("105"),
                 Decimal("99") if boundary else Decimal("90"), Decimal("103"))
    evidence = ProtectionEvidence(
        side=OrderSide.BUY, executed_average=Decimal("100"), cost_adjusted_entry=Decimal("100.001"),
        desired_stop=Decimal("95"), reference=MarketReference(Decimal("103"), close_at, "synthetic"),
        decision_candle=Candle(decision_at, Decimal("100"), Decimal("105"), Decimal("90"), Decimal("100")),
        stop_effective_at=fill.executed_at, history=(bar,), timeframe="1h", calendar="24/7",
        history_source="synthetic", executions=(FillActivation(fill, fill.executed_at),),
        execution_history_complete=result.execution_history_complete, exposure_quantity=result.filled,
        intervals=() if boundary else (CoveredInterval(
            fill.execution_id, fill.executed_at, close_at, Decimal("99"), Decimal("105"), "synthetic"),),
    )
    assessment = assess_protection_evidence(evidence, now=close_at, max_age=timedelta(minutes=1))
    assert assessment.state is EvidenceState.VALID
    async with effect_sessions() as db:
        stored = (await db.execute(select(PaperEntryFill))).scalar_one()
        assert stored.executed_at == evidence.executions[0].effective_at


@pytest.mark.asyncio
async def test_partial_increments_retry_and_lost_response(effect_sessions):
    context = _context("BTC/USDT")
    connector = _connector(effect_sessions, context)
    connector._transport.scenarios["increments"] = Decimal("0.3")
    intent = OrderIntent("increments", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    first = await connector.create_order(intent)
    async def lost_response():
        await connector.observe_paper_fill(intent.id, filled=Decimal("0.7"),
                                           average=Decimal("100"), terminal=False, revision=2)
        raise TimeoutError("response lost after commit")
    with pytest.raises(TimeoutError):
        await lost_response()
    restarted = _connector(effect_sessions, context)
    second = await restarted.query_order(intent_id=intent.id)
    assert second.execution_history_complete is True
    assert second.executions[:1] == first.executions
    assert [item.quantity for item in second.executions] == [Decimal("0.3"), Decimal("0.4")]
    assert [item.cumulative_quantity for item in second.executions] == [Decimal("0.3"), Decimal("0.7")]
    assert [item.revision for item in second.executions] == [1, 2]
    assert first.executions[0].executed_at <= second.executions[1].executed_at
    assert await restarted.observe_paper_fill(intent.id, filled=Decimal("0.7"),
                                              average=Decimal("100"), terminal=False, revision=2) == second
    correction = await restarted.observe_paper_fill(
        intent.id, filled=Decimal("0.7"), average=Decimal("101"), terminal=False, revision=3,
    )
    assert correction.executions == second.executions
    final = await restarted.observe_paper_fill(intent.id, filled=Decimal("1"),
                                               average=Decimal("101"), terminal=True, revision=4)
    assert final.executions[:2] == second.executions
    assert final.executions[2].quantity == Decimal("0.3")
    assert final.executions[2].revision == 4
    assert final.execution_history_complete is True
    assert (await restarted.list_positions())[0].quantity == Decimal("1")
    async with effect_sessions() as db:
        assert len((await db.execute(select(PaperEntryFill))).scalars().all()) == 3


@pytest.mark.asyncio
async def test_receipt_query_from_separate_process(effect_sessions):
    context = _context("BTC/USDT")
    connector = _connector(effect_sessions, context)
    intent = OrderIntent("read-child", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    expected = await connector.create_order(intent)
    async with effect_sessions() as db:
        schema = (await db.execute(text("SELECT current_schema()"))).scalar_one()

    def query_in_process():
        ctx = multiprocessing.get_context("spawn")
        receiver, sender = ctx.Pipe(duplex=False)
        process = ctx.Process(target=run_effect, args=(
            context, settings.database_url, schema, "query-entry", intent.id, None, sender,
        ))
        process.start()
        sender.close()
        try:
            if not receiver.poll(20):
                raise AssertionError("child did not return receipt")
            result = receiver.recv()
            process.join(20)
            assert process.exitcode == 0
            return result
        finally:
            receiver.close()
            if process.is_alive():
                process.terminate()
                process.join()

    assert await asyncio.to_thread(query_in_process) == expected


@pytest.mark.asyncio
async def test_failed_timestamp_insert_rolls_back_position_and_receipt(effect_sessions):
    context = _context("BTC/USDT")
    connector = _connector(effect_sessions, context)
    connector._transport.scenarios["rollback"] = Decimal("0.4")
    intent = OrderIntent("rollback", context, OrderSide.BUY, OrderType.MARKET, Decimal("1"))
    initial = await connector.create_order(intent)
    async with effect_sessions.kw["bind"].begin() as conn:
        await conn.execute(text("""
            CREATE FUNCTION reject_test_fill() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'test fill insert failure'; END $$
        """))
        await conn.execute(text("""
            CREATE TRIGGER reject_test_fill BEFORE INSERT ON paper_entry_fills
            FOR EACH ROW EXECUTE FUNCTION reject_test_fill()
        """))
    with pytest.raises(DBAPIError, match="test fill insert failure"):
        await connector.observe_paper_fill(
            intent.id, filled=Decimal("0.8"), average=Decimal("100"), terminal=False, revision=2,
        )
    assert await connector.query_order(intent_id=intent.id) == initial
    assert (await connector.list_positions())[0].quantity == Decimal("0.4")


@pytest.mark.asyncio
async def test_legacy_fill_remains_unknown_after_new_increment(effect_sessions):
    context = _context("BTC/USDT")
    connector = _connector(effect_sessions, context)
    effects = connector._transport.durable_effects
    position_id = "paper_legacy-timestamp"
    async with effect_sessions() as db:
        db.add(PaperEffectPosition(account_key=effects._account_key, position_id=position_id,
                                   context=effects._snapshot(), side="buy", quantity=Decimal("0.4"),
                                   entry_price=Decimal("100")))
        await db.flush()
        db.add(PaperEffectOperation(
            account_key=effects._account_key, position_id=position_id, operation_id="entry:legacy-timestamp",
            fence=0, payload={"kind": "entry", "side": "buy", "order_type": "market",
                              "quantity": "1", "price": "100", "limit_price": None},
            result={"filled": "0.4", "average": "100", "order_id": position_id,
                    "terminal": False, "revision": 1},
        ))
        await db.commit()
    legacy = await connector.query_order(intent_id="legacy-timestamp")
    assert legacy.executions == () and legacy.execution_history_complete is False
    next_result = await connector.observe_paper_fill(
        "legacy-timestamp", filled=Decimal("0.8"), average=Decimal("100"), terminal=False, revision=2,
    )
    assert next_result.execution_history_complete is False
    assert len(next_result.executions) == 1
    assert next_result.executions[0].quantity == Decimal("0.4")
    assert next_result.executions[0].cumulative_quantity == Decimal("0.8")
    assert await connector.query_order(intent_id="legacy-timestamp") == next_result
