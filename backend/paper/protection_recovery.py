"""Recover pending protection on existing durable paper positions only."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import DateTime, and_, cast, exists, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import EntryApproval, ExecutionIntent, ProtectionAuthority, Trade
from paper.durable_effects import (
    DurablePaperEffects, PaperEffectOperation, PaperEffectPosition, PaperProtectionAttempt,
    ProtectionClaimOccupied,
)
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.types import OrderSide
from trading.contracts import context_from_dict
from trading.order_executor import OrderExecutionError, OrderExecutor

logger = logging.getLogger(__name__)


async def recover_pending_paper_protection(
    sessions: async_sessionmaker[AsyncSession], *, batch_size: int = 50,
    evidence_provider=None, provider=None, evidence_clock=None,
) -> dict[str, int]:
    if not 1 <= batch_size <= 200:
        raise ValueError("Invalid protection recovery batch size")
    entry = PaperEffectOperation.__table__.alias("recovery_entry")
    protected = exists(
        select(1).select_from(PaperProtectionAttempt).join(
            PaperEffectOperation,
            and_(PaperEffectOperation.account_key == PaperProtectionAttempt.account_key,
                 PaperEffectOperation.position_id == PaperProtectionAttempt.position_id,
                 PaperEffectOperation.operation_id == PaperProtectionAttempt.operation_id),
        ).where(
            PaperProtectionAttempt.account_key == PaperEffectPosition.account_key,
            PaperProtectionAttempt.position_id == PaperEffectPosition.position_id,
            PaperProtectionAttempt.revision == entry.c.result["revision"].as_integer(),
            PaperProtectionAttempt.payload["levels_version"].as_integer() == ProtectionAuthority.version,
            or_(
                and_(ProtectionAuthority.version == 1, ProtectionAuthority.levels_effective_at.is_(None)),
                and_(
                    ProtectionAuthority.version > 1,
                    ProtectionAuthority.levels_effective_at.is_not(None),
                    cast(PaperProtectionAttempt.payload["levels_effective_at"].astext,
                         DateTime(timezone=True)) == ProtectionAuthority.levels_effective_at,
                ),
            ),
            PaperEffectOperation.payload == PaperProtectionAttempt.payload,
            PaperEffectOperation.result == {"filled": "0", "average": None},
        )
    )
    async with sessions() as db:
        rows = (await db.execute(
            select(ExecutionIntent.intent_id, ProtectionAuthority.execution_context)
            .join(ProtectionAuthority, ProtectionAuthority.intent_id == ExecutionIntent.intent_id)
            .join(PaperEffectPosition, PaperEffectPosition.position_id == func.concat("paper_", ExecutionIntent.intent_id))
            .join(entry, and_(
                entry.c.account_key == PaperEffectPosition.account_key,
                entry.c.position_id == PaperEffectPosition.position_id,
                entry.c.operation_id == func.concat("entry:", ExecutionIntent.intent_id),
            ))
            .outerjoin(Trade, Trade.intent_id == ExecutionIntent.intent_id)
            .where(
                ExecutionIntent.mode == "paper",
                PaperEffectPosition.context == ProtectionAuthority.execution_context,
                ExecutionIntent.status.in_(("planned", "unknown", "partial", "confirmed", "protection_pending")),
                PaperEffectPosition.quantity > 0,
                or_(Trade.status == "open", and_(
                    Trade.id.is_(None),
                    ExecutionIntent.status.in_(("planned", "unknown", "protection_pending")),
                )),
                or_(Trade.id.is_(None), ExecutionIntent.status == "protection_pending", ~protected,
                    PaperEffectPosition.stop_loss.is_distinct_from(ProtectionAuthority.stop_loss),
                    PaperEffectPosition.take_profit.is_distinct_from(ProtectionAuthority.take_profit)),
            )
            .order_by(ExecutionIntent.updated_at, ExecutionIntent.id)
            .limit(batch_size)
        )).all()

    evidence_provider = evidence_provider if evidence_provider is not None else provider
    counts = {"selected": len(rows), "confirmed": 0, "deferred": 0, "failed": 0}
    for intent_id, snapshot in rows:
        for retry in range(3):
            try:
                context = context_from_dict(snapshot)
                durable = DurablePaperEffects(sessions, context,
                                              evidence_provider=evidence_provider,
                                              evidence_clock=evidence_clock)
                connector = SimulatedConnector(context, transport=MemoryTransport(context, durable_effects=durable))
                async with sessions() as db:
                    await _recover_one(db, connector, intent_id, evidence_provider)
                counts["confirmed"] += 1
                break
            except Exception as exc:
                cause = exc
                while cause.__cause__ is not None:
                    cause = cause.__cause__
                if isinstance(cause, ProtectionClaimOccupied):
                    logger.info("[paper-recovery] intent %s claim ocupado; adiada", intent_id)
                    await _defer(sessions, intent_id)
                    counts["deferred"] += 1
                    break
                if retry < 2:
                    logger.warning("[paper-recovery] intent %s tentativa %d falhou: %s", intent_id, retry + 1, exc)
                    await asyncio.sleep(0.1 * (2 ** retry))
                else:
                    logger.error("[paper-recovery] intent %s permanece pendente após retries: %s", intent_id, exc)
                    await _defer(sessions, intent_id)
                    counts["failed"] += 1
    return counts


async def _defer(sessions: async_sessionmaker[AsyncSession], intent_id: str) -> None:
    try:
        async with sessions() as db:
            await db.execute(update(ExecutionIntent).where(
                ExecutionIntent.intent_id == intent_id
            ).values(updated_at=func.now()))
            await db.commit()
    except (SQLAlchemyError, OSError):
        logger.exception("[paper-recovery] não foi possível adiar intent %s", intent_id)


async def _recover_one(db: AsyncSession, connector: SimulatedConnector, intent_id: str,
                       evidence_provider=None) -> None:
    intent = (await db.execute(
        select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id).with_for_update()
    )).scalar_one()
    trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id).with_for_update())).scalar_one_or_none()
    authority = (await db.execute(
        select(ProtectionAuthority).where(ProtectionAuthority.intent_id == intent_id)
    )).scalar_one()
    if intent.mode != "paper" or intent.status not in {
        "planned", "unknown", "partial", "confirmed", "protection_pending"
    }:
        raise OrderExecutionError("Intent no longer eligible for paper protection recovery")
    if trade is None:
        if intent.status not in {"planned", "unknown", "protection_pending"}:
            raise OrderExecutionError("Missing trade requires manual reconciliation")
        approval = (await db.execute(
            select(EntryApproval.approval_id).where(EntryApproval.intent_id == intent_id)
        )).scalar_one_or_none()
        request = dict(
            symbol=intent.symbol, side=OrderSide(intent.side), quantity=intent.quantity,
            entry_price=authority.entry_price, stop_loss=authority.initial_stop_loss,
            take_profit=authority.initial_take_profit, market=intent.market, intent_id=intent_id,
            model_version="didi-manual" if approval is not None else None,
        )
        intent.updated_at = func.now()
        await db.commit()
        await OrderExecutor(connector, db, evidence_provider=evidence_provider).open_position(**request)
        return
    if trade.status != "open" or trade.position_id != "paper_" + intent_id:
        raise OrderExecutionError("Paper trade identity or status requires manual reconciliation")
    intent.status = "protection_pending"
    intent.updated_at = func.now()
    quantity = intent.quantity
    await db.commit()
    executor = OrderExecutor(connector, db, evidence_provider=evidence_provider)
    _, trade, _ = await executor._reconcile_partial_trade(intent_id, connector.context, quantity)
    stop_loss, take_profit = trade.stop_loss, trade.take_profit
    await db.commit()
    observed = await connector.query_order(intent_id=intent_id)
    await executor._protect_initial_durable(connector._transport.durable_effects, intent_id, observed,
                                            stop_loss, take_profit)
