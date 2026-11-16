"""Recoverable cancel-then-close decisions for partially filled paper entries.

The position lock is also held by observe_entry: cancellation linearizes with
fills at that lock. A stop decision never advances the candle cursor itself.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select

from paper.costs import parse_costs
from paper.durable_effects import (
    PaperEffectOperation, PaperProtectionAttempt, ProtectionClaimOccupied,
)
from trading.contracts import Capability


def decision_id(trade_id):
    return f"partial-stop:{trade_id}"


async def decision_for(db, effects, position_id, trade_id):
    return await db.get(PaperProtectionAttempt, (
        effects._account_key, position_id, decision_id(trade_id),
    ))


def _matches(effects, intent, trade, authority, position, payload):
    cursor = trade.paper_monitor_candle_at
    identity = dict(trade.execution_context or {})
    identity.pop("paper_costs", None)
    return (
        identity == effects._snapshot()
        and intent.status in {"confirmed", "partial", "protection_pending"}
        and trade.status == "open" and trade.id == payload["trade_id"]
        and trade.intent_id == intent.intent_id == payload["intent_id"]
        and trade.position_id == intent.position_id == position.position_id == payload["position_id"]
        and trade.protection_authority_id == authority.intent_id == intent.intent_id
        and trade.identity_status == "bound"
        and authority.execution_context == position.context == payload["identity"] == effects._snapshot()
        and trade.side == intent.side == authority.side == position.side
        and trade.market == intent.market == effects.context.market
        and trade.mode == intent.mode == "paper"
        and trade.exchange == effects.context.venue and trade.account_id == effects.context.account_id
        and authority.version == trade.protection_levels_version == payload["levels_version"]
        and str(trade.stop_loss) == payload["stop_loss"]
        and (str(trade.take_profit) if trade.take_profit is not None else None) == payload["take_profit"]
        and trade.stop_loss == authority.stop_loss and trade.take_profit == authority.take_profit
        and (cursor.astimezone(timezone.utc).isoformat() if cursor else None) == payload["cursor"]
        and trade.paper_monitor_timeframe == payload["previous_timeframe"]
        and (trade.execution_context or {}).get("paper_costs") == payload["paper_costs"]
    )


async def stage_partial_stop(db, effects, intent, trade, authority, position, *, reason, price, candle, timeframe):
    """Caller owns the authoritative state locks and commits before any effect."""
    from paper.safe_break import _confirmed_protection

    effects.context.require(Capability.REDUCE)
    effects.context.require(Capability.CANCEL)
    if (reason not in {"SL_HIT", "TP_HIT"} or candle.tzinfo is None
            or intent.order_terminal is True or not await _confirmed_protection(
                db, effects, intent, trade, authority, position)):
        raise ValueError("Partial stop requires a protected nonterminal entry")
    effects.context.instrument.validate(trade.quantity, price)
    entry = await db.get(PaperEffectOperation, (
        effects._account_key, position.position_id, "entry:" + intent.intent_id,
    ))
    costs = (trade.execution_context or {}).get("paper_costs")
    if entry is None or entry.payload.get("paper_costs") != costs:
        raise ValueError("Entry costs diverged; reconciliation required")
    payload = dict(
        kind="cancel-entry-stop", trade_id=trade.id, intent_id=intent.intent_id,
        position_id=position.position_id, identity=effects._snapshot(),
        reason=reason, execution_price=str(price), candle=candle.astimezone(timezone.utc).isoformat(),
        timeframe=timeframe,
        cursor=trade.paper_monitor_candle_at.astimezone(timezone.utc).isoformat() if trade.paper_monitor_candle_at else None,
        previous_timeframe=trade.paper_monitor_timeframe, levels_version=authority.version,
        stop_loss=str(trade.stop_loss), take_profit=str(trade.take_profit) if trade.take_profit is not None else None,
        revision=intent.observation_revision, paper_costs=costs,
    )
    existing = await decision_for(db, effects, position.position_id, trade.id)
    if existing is not None:
        if existing.payload != payload:
            raise ValueError("Partial stop decision conflicts with persisted decision")
        return existing
    attempt = PaperProtectionAttempt(
        account_key=effects._account_key, position_id=position.position_id,
        operation_id=decision_id(trade.id), payload=payload,
        revision=intent.observation_revision, fence=position.fence,
    )
    db.add(attempt)
    await db.flush()
    return attempt


async def claim_partial_stop(effects, position_id, trade_id, owner, *, lease_seconds=60):
    if (not isinstance(owner, str) or not owner or len(owner) > 64
            or type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600):
        raise ValueError("Invalid partial-stop claim")
    async with effects._sessions() as db, db.begin():
        intent, trade, authority, position = await effects._locked_protection_state(db, position_id)
        attempt = await decision_for(db, effects, position_id, trade_id)
        if attempt is None or trade is None or not _matches(effects, intent, trade, authority, position, attempt.payload):
            raise ValueError("Partial stop state changed; reconciliation required")
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        active = (await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.account_key == effects._account_key,
            PaperProtectionAttempt.position_id == position_id,
            PaperProtectionAttempt.fence == position.fence,
            PaperProtectionAttempt.lease_until > now,
        ))).scalars().all()
        if active and (active[0].owner != owner or active[0].operation_id != attempt.operation_id):
            raise ProtectionClaimOccupied("Position claim is occupied")
        if not active:
            position.fence += 1
        attempt.fence, attempt.owner = position.fence, owner
        attempt.lease_until = now + timedelta(seconds=lease_seconds)
        return attempt.operation_id, attempt.fence


async def cancel_partial_entry(effects, position_id, trade_id, owner, fence):
    """Fence, cancellation receipt and final exposure synchronization commit together."""
    effects.context.require(Capability.REDUCE)
    effects.context.require(Capability.CANCEL)
    if type(fence) is not int or fence <= 0:
        raise ValueError("Invalid partial-stop fencing token")
    async with effects._sessions() as db, db.begin():
        intent, trade, authority, position = await effects._locked_protection_state(db, position_id)
        attempt = await decision_for(db, effects, position_id, trade_id)
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (attempt is None or attempt.owner != owner or attempt.fence != fence or position.fence != fence
                or attempt.lease_until is None or attempt.lease_until <= now):
            raise ValueError("Stale partial-stop fencing token")
        if trade is None or not _matches(effects, intent, trade, authority, position, attempt.payload):
            raise ValueError("Partial stop state changed; reconciliation required")
        key = (effects._account_key, position_id, attempt.operation_id)
        receipt = await db.get(PaperEffectOperation, key)
        entry = await db.get(PaperEffectOperation, (effects._account_key, position_id, "entry:" + intent.intent_id))
        if entry is None or entry.payload.get("paper_costs") != attempt.payload["paper_costs"]:
            raise ValueError("Entry receipt or costs inconsistent")
        result = dict(entry.result)
        filled = Decimal(result["filled"])
        if (result["order_id"] != position_id or filled != position.quantity
                or filled < trade.quantity or filled > intent.quantity
                or result["revision"] < intent.observation_revision):
            raise ValueError("Final entry exposure inconsistent")
        if receipt is not None:
            if receipt.payload != attempt.payload or receipt.result != result or result.get("terminal") is not True:
                raise ValueError("Cancellation receipt inconsistent")
        else:
            if result.get("terminal") is not True:
                result.update(terminal=True, revision=result["revision"] + 1, full=filled == intent.quantity)
                entry.result = result
            receipt = PaperEffectOperation(
                account_key=effects._account_key, position_id=position_id,
                operation_id=attempt.operation_id, fence=fence, payload=attempt.payload, result=result,
            )
            db.add(receipt)
        costs = parse_costs({intent.symbol: attempt.payload["paper_costs"]})[intent.symbol]
        offset = costs.spread_price + costs.slippage_price
        average = Decimal(result["average"]) + (offset if trade.side == "buy" else -offset)
        effects.context.instrument.validate(filled, Decimal(result["average"]))
        if average <= 0:
            raise ValueError("Invalid final entry price")
        trade.quantity = intent.filled_quantity = filled
        trade.entry_price = average
        trade.entry_value = (average * filled * effects.context.instrument.contract_size).quantize(Decimal("0.01"))
        if intent.risk_amount is not None:
            trade.risk_amount = intent.risk_amount * filled / intent.quantity
        if intent.capital_amount is not None:
            trade.capital_amount = intent.capital_amount * filled / intent.quantity
        intent.observation_revision = result["revision"]
        intent.order_terminal = True
        intent.result_state = "confirmed" if filled == intent.quantity else "partial"
        intent.status = "partial"
        await effects.release_initial_protection_claim(db, position_id, attempt.operation_id, fence, owner)
        return dict(attempt.payload)


async def cancelled_stop_authorizes_close(db, effects, intent, trade, authority, position, *, reason, price, candle, timeframe):
    """Permit only the original stop decision after a confirmed cancellation."""
    attempt = await decision_for(db, effects, position.position_id, trade.id)
    if attempt is None or not _matches(effects, intent, trade, authority, position, attempt.payload):
        return False
    payload = attempt.payload
    receipt = await db.get(PaperEffectOperation, (effects._account_key, position.position_id, attempt.operation_id))
    entry = await db.get(PaperEffectOperation, (effects._account_key, position.position_id, "entry:" + intent.intent_id))
    return bool(
        receipt is not None and entry is not None and receipt.payload == payload
        and receipt.result == entry.result and entry.result.get("terminal") is True
        and intent.order_terminal is True and intent.observation_revision == entry.result["revision"]
        and trade.quantity == intent.filled_quantity == position.quantity == Decimal(entry.result["filled"])
        and payload["reason"] == reason and Decimal(payload["execution_price"]) == price
        and payload["candle"] == candle.astimezone(timezone.utc).isoformat() and payload["timeframe"] == timeframe
    )


async def resume_partial_stop(sessions, trade_id):
    from db.models import Trade
    from trading.paper_trader import is_durable_paper_position
    from paper.safe_break import _effects
    from tasks.trading_tasks import _execute_durable_paper_stop, _stage_durable_paper_close

    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        if (trade is None or trade.status != "open" or not getattr(trade, "intent_id", None) or trade.mode != "paper"
                or not is_durable_paper_position(getattr(trade, "position_id", None))):
            return False
        effects, _ = _effects(sessions, dict(trade.execution_context or {}))
        attempt = await decision_for(db, effects, trade.position_id, trade.id)
        if attempt is None:
            return False
        closing = (await db.execute(select(PaperProtectionAttempt.operation_id).where(
            PaperProtectionAttempt.account_key == effects._account_key,
            PaperProtectionAttempt.position_id == trade.position_id,
            PaperProtectionAttempt.operation_id.like("manual-close:%"),
        ))).first()
        if closing:
            return False  # Existing close claims/receipts own the rest of recovery.
        position_id = trade.position_id
    owner = uuid4().hex
    _, fence = await claim_partial_stop(effects, position_id, trade_id, owner)
    payload = await cancel_partial_entry(effects, position_id, trade_id, owner, fence)
    async with sessions() as db:
        intent, trade, authority, position = await effects._locked_protection_state(db, position_id)
        decision = await _stage_durable_paper_close(
            db, effects, trade, reason=payload["reason"], price=Decimal(payload["execution_price"]),
            candle=datetime.fromisoformat(payload["candle"]), timeframe=payload["timeframe"],
        )
        snapshot = dict(trade.execution_context or {})
    await _execute_durable_paper_stop(sessions, trade_id, snapshot, decision)
    return True
