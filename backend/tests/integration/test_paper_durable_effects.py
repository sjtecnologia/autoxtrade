import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from database import Base
from paper.durable_effects import (DurablePaperEffects, PaperEffectOperation, PaperEffectPosition,
                                   PaperEntryFill, PaperProtectionAttempt, PaperProtectionEvidenceRecord,
                                   ProtectionEvidenceRequired)
from tests.integration.test_approval_lifecycle import approval_database, _context
from trading.connectors.types import OrderSide
from trading.contracts import ExecutionMode
from trading.safety import AccountNature, ExposureBlocked


@pytest_asyncio.fixture
async def effect_sessions(approval_database):
    engine = approval_database.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[PaperEffectPosition.__table__, PaperEffectOperation.__table__, PaperEntryFill.__table__,
                    PaperProtectionAttempt.__table__, PaperProtectionEvidenceRecord.__table__],
        )
        from pathlib import Path
        from runpy import run_path
        migration = run_path(str(Path(__file__).parents[2] / "alembic/versions/013_immutable_paper_attempts.py"))
        await conn.execute(text(migration["ATTEMPT_TRIGGER"]))
        await conn.execute(text("""
            CREATE TRIGGER autoxtrade_immutable_paper_attempt
            BEFORE UPDATE OR DELETE ON paper_protection_attempts FOR EACH ROW
            EXECUTE FUNCTION autoxtrade_immutable_paper_attempt()
        """))
        await conn.execute(text(migration["RECEIPT_TRIGGER"]))
        await conn.execute(text("""
            CREATE TRIGGER autoxtrade_immutable_paper_close_receipt
            BEFORE UPDATE OR DELETE ON paper_effect_operations FOR EACH ROW
            EXECUTE FUNCTION autoxtrade_immutable_paper_close_receipt()
        """))
        migration = run_path(str(Path(__file__).parents[2] / "alembic/versions/016_immutable_protection_receipts.py"))
        await conn.execute(text(migration["RECEIPT_TRIGGER"]))
        migration = run_path(str(Path(__file__).parents[2] / "alembic/versions/018_immutable_paper_entry_request.py"))
        await conn.execute(text(migration["RECEIPT_TRIGGER"]))
        migration = run_path(str(Path(__file__).parents[2] / "alembic/versions/019_partial_stop_cancellation.py"))
        await conn.execute(text(migration["RECEIPT_TRIGGER"]))
        migration = run_path(str(Path(__file__).parents[2] / "alembic/versions/015_paper_entry_fills.py"))
        await conn.execute(text(migration["IMMUTABLE_FUNCTION"]))
        await conn.execute(text("""
            CREATE TRIGGER autoxtrade_immutable_paper_entry_fill
            BEFORE UPDATE OR DELETE ON paper_entry_fills FOR EACH ROW
            EXECUTE FUNCTION autoxtrade_immutable_paper_entry_fill()
        """))
        migration = run_path(str(Path(__file__).parents[2] / "alembic/versions/014_protection_evidence.py"))
        await conn.execute(text(migration["IMMUTABLE_FUNCTION"]))
        await conn.execute(text("""
            CREATE TRIGGER autoxtrade_immutable_protection_evidence
            BEFORE UPDATE OR DELETE ON paper_protection_evidence FOR EACH ROW
            EXECUTE FUNCTION autoxtrade_immutable_protection_evidence()
        """))
    return approval_database


@pytest.mark.asyncio
async def test_durable_effects_deduplication_takeover_and_restart(effect_sessions):
    context = _context("BTC/USDT")
    first = DurablePaperEffects(effect_sessions, context)
    second = DurablePaperEffects(effect_sessions, context)
    await first.create_position("position-1", OrderSide.BUY, Decimal("1"), Decimal("100"))
    assert await first.acquire_fence("position-1") == 1
    with pytest.raises(ProtectionEvidenceRequired, match="prepared attempt"):
        await first.protect(
            "position-1", "protect-1", 1, stop_loss=Decimal("95"), take_profit=Decimal("110")
        )
    with pytest.raises(ProtectionEvidenceRequired, match="prepared attempt"):
        await second.protect(
            "position-1", "protect-1", 1, stop_loss=Decimal("94"), take_profit=Decimal("110")
        )
    async with effect_sessions() as db:
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert position.stop_loss is None
        assert not (await db.execute(select(PaperEffectOperation))).scalars().all()
    assert await second.acquire_fence("position-1") == 2
    with pytest.raises(ValueError, match="Stale fencing"):
        await first.protect("position-1", "stale", 1, stop_loss=Decimal("94"))
    with pytest.raises(ValueError, match="Stale fencing"):
        await first.reduce(
            "position-1", "stale-reduce", 1, quantity=Decimal("0.5"), execution_price=Decimal("100")
        )
    async def lose_response_after_commit():
        await second.reduce(
            "position-1", "reduce-1", 2, quantity=Decimal("0.5"), execution_price=Decimal("100")
        )
        raise TimeoutError("response lost after commit")

    with pytest.raises(TimeoutError, match="after commit"):
        await lose_response_after_commit()
    restarted = DurablePaperEffects(effect_sessions, context)
    receipt = await restarted.reduce(
        "position-1", "reduce-1", 2, quantity=Decimal("0.5"), execution_price=Decimal("100")
    )
    assert receipt.filled == Decimal("0.5")
    async with effect_sessions() as db:
        positions = (await db.execute(select(PaperEffectPosition))).scalars().all()
        operations = (await db.execute(select(PaperEffectOperation))).scalars().all()
        assert len(positions) == 1
        assert positions[0].quantity == Decimal("0.5")
        assert positions[0].stop_loss is None
        assert positions[0].fence == 2
        assert len(operations) == 1
        assert operations[0].operation_id == "reduce-1"


@pytest.mark.asyncio
async def test_concurrent_reductions_commit_only_once(effect_sessions):
    context = _context("BTC/USDT")
    workers = [DurablePaperEffects(effect_sessions, context) for _ in range(2)]
    await workers[0].create_position("position-2", OrderSide.BUY, Decimal("1"), Decimal("100"))
    fence = await workers[0].acquire_fence("position-2")
    results = await asyncio.gather(*[
        worker.reduce("position-2", "same-reduction", fence,
                      quantity=Decimal("0.5"), execution_price=Decimal("100"))
        for worker in workers
    ])
    assert results[0] == results[1]
    async with effect_sessions() as db:
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        operations = (await db.execute(select(PaperEffectOperation))).scalars().all()
        assert position.quantity == Decimal("0.5")
        assert len(operations) == 1


@pytest.mark.asyncio
async def test_position_identity_is_scoped_to_account(effect_sessions):
        context = _context("BTC/USDT")
        other_context = replace(context, account_id="another-paper-account")
        first = DurablePaperEffects(effect_sessions, context)
        other = DurablePaperEffects(effect_sessions, other_context)
        await first.create_position("same-id", OrderSide.BUY, Decimal("1"), Decimal("100"))
        await other.create_position("same-id", OrderSide.BUY, Decimal("1"), Decimal("100"))
        fence = await first.acquire_fence("same-id")
        await first.reduce("same-id", "reduce", fence, quantity=Decimal("0.5"), execution_price=Decimal("100"))
        async with effect_sessions() as db:
            positions = (await db.execute(select(PaperEffectPosition))).scalars().all()
            assert sorted(position.quantity for position in positions) == [Decimal("0.5"), Decimal("1")]


@pytest.mark.asyncio
async def test_external_modes_remain_blocked(effect_sessions):
    context = replace(_context("BTC/USDT"), mode=ExecutionMode.LIVE, nature=AccountNature.REAL)
    with pytest.raises(ExposureBlocked):
        DurablePaperEffects(effect_sessions, context)


@pytest.mark.asyncio
async def test_unprepared_protection_is_blocked_without_market_evidence(effect_sessions):
    effects = DurablePaperEffects(effect_sessions, _context("BTC/USDT"))
    await effects.create_position("safe-break", OrderSide.BUY, Decimal("1"), Decimal("100"))
    fence = await effects.acquire_fence("safe-break")
    with pytest.raises(ProtectionEvidenceRequired, match="prepared attempt"):
        await effects.protect("safe-break", "safe-break-op", fence,
                              stop_loss=Decimal("100"), take_profit=Decimal("110"))
    async with effect_sessions() as db:
        position = (await db.execute(select(PaperEffectPosition))).scalar_one()
        assert position.stop_loss is None and position.take_profit is None
