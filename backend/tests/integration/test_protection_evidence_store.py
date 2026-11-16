import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from paper.durable_effects import PaperProtectionAttempt, PaperProtectionEvidenceRecord
from paper.protection_evidence import Candle, FillActivation, MarketReference, ProtectionEvidence
from paper.protection_evidence_store import record_protection_evidence
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_protection_effect_authority import pending_attempt
from trading.connectors.types import OrderSide
from db.models import Trade


@pytest.mark.asyncio
async def test_records_immutable_separate_evidence_under_attempt(effect_sessions):
    connector, intent_id, data, _ = await pending_attempt(effect_sessions)
    position_id, operation_id, _, original_payload, _ = data
    async with effect_sessions() as db:
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one()
        now = (await db.execute(select(text("clock_timestamp()")))).scalar_one()
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
        decision = Candle(start, Decimal("100"), Decimal("101"), Decimal("90"), Decimal("100"))
        next_candle = Candle(start + timedelta(hours=1), Decimal("100"), Decimal("102"),
                             Decimal("99"), Decimal("101"))
        evidence = ProtectionEvidence(
            OrderSide.BUY, Decimal("100"), trade.entry_price, Decimal("95"),
            MarketReference(Decimal("101"), start + timedelta(hours=2), "synthetic"),
            decision, start + timedelta(hours=1), (next_candle,), "1h", "24/7", "synthetic",
        )
    effects = connector._transport.durable_effects

    async def record():
        return await record_protection_evidence(effects, position_id, operation_id, "sample-1",
                                                evidence, max_age=timedelta(hours=1))

    results = await asyncio.gather(record(), record())
    assert results[0].assessment["state"] == "incomplete_or_ambiguous"
    assert results[0].assessed_at == results[1].assessed_at
    with pytest.raises(ValueError, match="different payload"):
        await record_protection_evidence(
            effects, position_id, operation_id, "sample-1",
            replace(evidence, history_source="other"), max_age=timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="prices or identity"):
        await record_protection_evidence(
            effects, position_id, operation_id, "wrong-price",
            replace(evidence, cost_adjusted_entry=Decimal("100")), max_age=timedelta(hours=1),
        )
    with pytest.raises(ValueError, match="stop conflicts"):
        await record_protection_evidence(
            effects, position_id, operation_id, "wrong-stop",
            replace(evidence, desired_stop=Decimal("94")), max_age=timedelta(hours=1),
        )
    missing = await record_protection_evidence(
        effects, position_id, operation_id, "missing-reference",
        replace(evidence, reference=None), max_age=timedelta(hours=1),
    )
    assert missing.assessment["state"] == "missing"
    receipt = await connector.query_order(intent_id=intent_id)
    fill = receipt.executions[0]
    supplied = replace(
        evidence, stop_effective_at=fill.executed_at,
        executions=(FillActivation(fill, fill.executed_at),),
        exposure_quantity=receipt.filled, execution_history_complete=True,
    )
    saved = await record_protection_evidence(
        effects, position_id, operation_id, "durable-fill", supplied, max_age=timedelta(hours=1),
    )
    assert saved.payload["evidence"]["executions"][0]["execution"]["executed_at"] == fill.executed_at.isoformat()
    forged = replace(fill, executed_at=fill.executed_at - timedelta(seconds=1))
    with pytest.raises(ValueError, match="conflicts with durable fills"):
        await record_protection_evidence(
            effects, position_id, operation_id, "forged-fill",
            replace(supplied, executions=(FillActivation(forged, fill.executed_at),)),
            max_age=timedelta(hours=1),
        )
    async with effect_sessions() as db:
        attempts = (await db.execute(select(PaperProtectionAttempt))).scalars().all()
        records = (await db.execute(select(PaperProtectionEvidenceRecord))).scalars().all()
        assert len(attempts) == 1 and len(records) == 4
        assert {"sample-1", "missing-reference", "durable-fill"} <= {
            record.evidence_id for record in records
        }
        assert attempts[0].payload == original_payload
        valid_record = next(record for record in records if record.evidence_id == "sample-1")
        assert valid_record.payload["evidence"]["reference"]["source"] == "synthetic"
        assert valid_record.operation_id == operation_id
        with pytest.raises(DBAPIError, match="immutable"):
            await db.execute(text("UPDATE paper_protection_evidence SET assessment = '{}'::jsonb"))
            await db.commit()
