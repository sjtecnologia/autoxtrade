"""Durable safe-break decisions for the paper candle monitor."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from sqlalchemy import exists, or_, select

from db.models import ExecutionIntent, PaperSafeBreakDecision, Trade, TradeEvent
from paper.durable_effects import (
    DurablePaperEffects, PaperEffectOperation, PaperProtectionAttempt,
    ProtectionEvidenceRequired,
)
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.contracts import context_from_dict
from trading.order_executor import OrderExecutionError, OrderExecutor

def executable_safe_break_stop(snapshot: dict, side: str, quantity: Decimal, stop: Decimal) -> Decimal:
    identity = dict(snapshot)
    identity.pop("paper_costs", None)
    instrument = context_from_dict(identity).instrument
    tick = instrument.tick_size
    if tick is None or not tick.is_finite() or tick <= 0 or side not in {"buy", "sell"}:
        raise ValueError("Safe break requires valid instrument tick and side")
    rounding = ROUND_CEILING if side == "buy" else ROUND_FLOOR
    result = (stop / tick).to_integral_value(rounding=rounding) * tick
    instrument.validate(quantity, result)
    return result


def pending_safe_break_decisions(trade_id: int):
    completed = exists(select(TradeEvent.id).where(
        TradeEvent.trade_id == PaperSafeBreakDecision.trade_id,
        TradeEvent.event_type == "SAFE_BREAK_ARMED",
        TradeEvent.payload["levels_version"].as_integer() == PaperSafeBreakDecision.levels_version,
    )).correlate(PaperSafeBreakDecision)
    return select(PaperSafeBreakDecision).where(
        PaperSafeBreakDecision.trade_id == trade_id, ~completed,
    )


def _effects(sessions, snapshot, *, evidence_clock=None):
    identity = dict(snapshot)
    identity.pop("paper_costs", None)
    context = context_from_dict(identity)
    effects = DurablePaperEffects(sessions, context, evidence_clock=evidence_clock)
    return effects, SimulatedConnector(
        context, transport=MemoryTransport(context, durable_effects=effects)
    )


async def _confirmed_protection(db, effects, intent, trade, authority, position) -> bool:
    if intent.status not in {"confirmed", "partial"}:
        return False
    try:
        effects._level_effective_at(authority)
    except ProtectionEvidenceRequired:
        return False
    if position.stop_loss != authority.stop_loss or position.take_profit != authority.take_profit:
        return False
    entry = await db.get(PaperEffectOperation, (
        effects._account_key, position.position_id, "entry:" + intent.intent_id,
    ))
    attempts = (await db.execute(select(PaperProtectionAttempt).where(
        PaperProtectionAttempt.account_key == effects._account_key,
        PaperProtectionAttempt.position_id == position.position_id,
        PaperProtectionAttempt.revision == intent.observation_revision,
        PaperProtectionAttempt.payload["levels_version"].as_integer() == authority.version,
    ))).scalars().all()
    for attempt in attempts:
        receipt = await db.get(PaperEffectOperation, (
            effects._account_key, position.position_id, attempt.operation_id,
        ))
        if (effects._protection_receipt_matches(receipt, attempt, attempt.payload)
                and effects._protection_state_matches(
                    intent, trade, authority, position, attempt.payload, entry
                )):
            return True
    return False


async def stage_safe_break(
    sessions, trade_id: int, snapshot: dict, *, position_id: str,
    candle_at: datetime, timeframe: str, interval_seconds: int,
    previous_cursor: datetime | None, previous_timeframe: str | None,
    old_stop: Decimal, new_stop: Decimal, take_profit: Decimal | None,
    entry_price: Decimal, quantity: Decimal, levels_version: int, entry_revision: int,
    candle: dict,
) -> bool:
    """Persist desired levels and the decision atomically; do not advance the cursor."""
    effects, _ = _effects(sessions, snapshot)
    async with sessions() as db:
        async with db.begin():
            intent, trade, authority, position = await effects._locked_protection_state(db, position_id)
            if (trade is None or trade.id != trade_id or trade.status != "open"
                    or trade.paper_monitor_candle_at != previous_cursor
                    or trade.paper_monitor_timeframe != previous_timeframe
                    or trade.stop_loss != old_stop or trade.take_profit != take_profit
                    or trade.entry_price != entry_price or trade.quantity != quantity
                    or authority.version != levels_version
                    or intent.observation_revision != entry_revision):
                return False
            if executable_safe_break_stop(snapshot, trade.side, trade.quantity, entry_price) != new_stop:
                raise ValueError("Safe-break stop is not the executable break-even")
            pending_close = (await db.execute(select(PaperProtectionAttempt.operation_id).where(
                PaperProtectionAttempt.account_key == effects._account_key,
                PaperProtectionAttempt.position_id == position_id,
                or_(PaperProtectionAttempt.operation_id == f"manual-close:{trade_id}",
                    PaperProtectionAttempt.operation_id.like(f"manual-close:{trade_id}:%")),
            ).limit(1))).scalar_one_or_none()
            if pending_close is not None or not await _confirmed_protection(
                db, effects, intent, trade, authority, position
            ):
                raise OrderExecutionError("Current paper protection is not confirmed")
            from risk.manager import RiskManager
            previous_risk, previous_capital = RiskManager._trade_budget(trade)
            if (intent.risk_amount is None or intent.capital_amount is None
                    or intent.risk_amount <= 0 or intent.capital_amount <= 0
                    or intent.quantity <= 0):
                raise ValueError("Safe break requires a complete durable risk reservation")
            if take_profit is not None and (
                trade.side == "buy" and new_stop >= take_profit
                or trade.side == "sell" and new_stop <= take_profit
            ):
                raise ValueError("Safe-break stop conflicts with the target")
            decision = PaperSafeBreakDecision(
                trade_id=trade_id, intent_id=intent.intent_id,
                levels_version=authority.version + 1, candle_at=candle_at,
                timeframe=timeframe, interval_seconds=interval_seconds,
                previous_cursor=previous_cursor, previous_timeframe=previous_timeframe,
                effective_at=candle_at + timedelta(seconds=interval_seconds),
                old_stop=old_stop, new_stop=new_stop, take_profit=take_profit,
                entry_price=entry_price, exposure_quantity=quantity, entry_revision=entry_revision,
                candle=candle,
            )
            db.add(decision)
            await db.flush()
            ratio = trade.quantity / intent.quantity
            trade.risk_amount = max(previous_risk, intent.risk_amount * ratio).quantize(
                Decimal("0.01"), rounding=ROUND_CEILING
            )
            trade.capital_amount = max(previous_capital, intent.capital_amount * ratio).quantize(
                Decimal("0.01"), rounding=ROUND_CEILING
            )
            trade.stop_loss = new_stop
            intent.status = "protection_pending"
            await db.flush()
            return True


async def resume_safe_break(
    sessions, trade_id: int, *, evidence_provider=None, evidence_clock=None,
) -> bool:
    """Recover a pending decision even when fresh monitor candles are unavailable."""
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        if trade is None or trade.status != "open" or trade.mode != "paper" or not trade.intent_id:
            return False
        pending = (await db.execute(
            pending_safe_break_decisions(trade_id).order_by(PaperSafeBreakDecision.levels_version)
        )).scalars().all()
        if not pending:
            return False
        if len(pending) != 1:
            raise OrderExecutionError("Multiple pending safe-break decisions require reconciliation")
        decision = pending[0]
        version = decision.levels_version
        position_id, intent_id = trade.position_id, trade.intent_id
        snapshot = dict(trade.execution_context or {})
        stop, target = decision.new_stop, decision.take_profit
    if not position_id:
        raise OrderExecutionError("Safe-break position identity is missing")
    effects, connector = _effects(sessions, snapshot, evidence_clock=evidence_clock)
    entry = await connector.query_order(intent_id=intent_id)

    async def finalize(db, intent: ExecutionIntent, trade: Trade, payload: dict) -> None:
        saved = await db.get(PaperSafeBreakDecision, (trade_id, version))
        if (saved is None or saved.intent_id != intent.intent_id
                or payload.get("levels_version") != saved.levels_version
                or trade.protection_levels_version != saved.levels_version
                or trade.stop_loss != saved.new_stop or trade.take_profit != saved.take_profit):
            raise OrderExecutionError("Safe-break decision changed before confirmation")
        if trade.paper_monitor_candle_at == saved.candle_at:
            return
        if (trade.paper_monitor_candle_at != saved.previous_cursor
                or trade.paper_monitor_timeframe != saved.previous_timeframe):
            raise OrderExecutionError("Safe-break monitor cursor changed before confirmation")
        trade.paper_monitor_candle_at = saved.candle_at
        trade.paper_monitor_timeframe = saved.timeframe
        db.add(TradeEvent(trade=trade, event_type="SAFE_BREAK_ARMED", payload={
            "new_stop_loss": str(saved.new_stop), "levels_version": saved.levels_version,
            "candle": saved.candle_at.isoformat(), "effective_at": saved.effective_at.isoformat(),
        }))

    async with sessions() as db:
        await OrderExecutor(connector, db, evidence_provider=evidence_provider)._protect_initial_durable(
            effects, intent_id, entry, stop, target, finalize=finalize,
        )
    return True
