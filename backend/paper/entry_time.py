"""Authoritative fill times and conservative repair of unprocessed paper Trades."""
from datetime import datetime, timezone
from decimal import Decimal


def proven_entry_time(observed):
    if (observed.execution_history_complete is not True or not observed.executions
            or type(observed.revision) is not int or observed.revision < 1):
        raise ValueError("Complete durable fill history is required for entry time")
    total = Decimal("0")
    previous_revision = 0
    previous_time = None
    seen = set()
    for fill in observed.executions:
        timestamp = fill.executed_at
        if (not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None
                or not fill.execution_id or fill.execution_id in seen
                or type(fill.revision) is not int or not previous_revision < fill.revision <= observed.revision
                or not isinstance(fill.quantity, Decimal) or not fill.quantity.is_finite() or fill.quantity <= 0
                or previous_time is not None and timestamp < previous_time):
            raise ValueError("Durable fill timeline is incomplete or inconsistent")
        total += fill.quantity
        if total != fill.cumulative_quantity:
            raise ValueError("Durable cumulative fills are inconsistent")
        seen.add(fill.execution_id)
        previous_revision, previous_time = fill.revision, timestamp
    if total != observed.filled:
        raise ValueError("Durable fill timeline does not cover the exposure")
    return observed.executions[0].executed_at.astimezone(timezone.utc)


async def reconcile_entry_time(sessions, intent_id, *, apply=False):
    """Repair only before any cursor or management decision; never replay history blindly."""
    from sqlalchemy import or_, select
    from db.models import PaperSafeBreakDecision, Trade, TradeEvent
    from paper.durable_effects import PaperEffectOperation, PaperProtectionAttempt
    from paper.safe_break import _effects
    from trading.paper_trader import is_durable_paper_position

    async with sessions() as db, db.begin():
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent_id))).scalar_one_or_none()
        if (trade is None or trade.mode != "paper" or trade.status != "open"
                or not is_durable_paper_position(trade.position_id)):
            raise ValueError("An open durable paper Trade is required")
        effects, _ = _effects(sessions, dict(trade.execution_context or {}))
        intent, trade, authority, position = await effects._locked_protection_state(db, trade.position_id)
        entry = await db.get(PaperEffectOperation, (effects._account_key, position.position_id, "entry:" + intent_id))
        if entry is None:
            raise ValueError("Durable entry receipt is missing")
        observed = await effects._entry_result(db, intent_id, position.position_id, entry.result)
        timestamp = proven_entry_time(observed)
        result = dict(intent_id=intent_id, trade_id=trade.id, current_open_at=trade.open_at.isoformat(),
                      proven_open_at=timestamp.isoformat(), status="already_correct")
        if trade.open_at == timestamp:
            return result
        management = (await db.execute(select(TradeEvent.id).where(
            TradeEvent.trade_id == trade.id,
            TradeEvent.event_type.in_(["SAFE_BREAK_ARMED", "MANUAL_CLOSE", "SL_HIT", "TP_HIT", "DIDI_EXIT"]),
        ).limit(1))).first()
        decisions = (await db.execute(select(PaperProtectionAttempt.operation_id).where(
            PaperProtectionAttempt.account_key == effects._account_key,
            PaperProtectionAttempt.position_id == position.position_id,
            or_(PaperProtectionAttempt.operation_id.like("manual-close:%"),
                PaperProtectionAttempt.operation_id.like("partial-stop:%")),
        ).limit(1))).first()
        safe_break = (await db.execute(select(PaperSafeBreakDecision.trade_id).where(
            PaperSafeBreakDecision.trade_id == trade.id,
        ).limit(1))).first()
        if (trade.paper_monitor_candle_at is not None or management or decisions or safe_break
                or trade.quantity != observed.filled or position.quantity != observed.filled
                or intent.observation_revision != observed.revision):
            result["status"] = "history_reconciliation_required"
            return result
        result["status"] = "repair_available"
        if apply:
            trade.open_at = timestamp
            db.add(TradeEvent(trade_id=trade.id, event_type="ENTRY_TIME_RECONCILED", payload=dict(result)))
            result["status"] = "repaired"
        return result


def main():
    import argparse
    import asyncio
    import json
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from config import settings

    parser = argparse.ArgumentParser(description="Inspect/repair entry time of a single durable paper Trade")
    parser.add_argument("--intent", required=True)
    parser.add_argument("--apply", action="store_true", help="Repair only when no candle/management history exists")
    args = parser.parse_args()
    async def run():
        engine = create_async_engine(settings.database_url)
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            return await reconcile_entry_time(sessions, args.intent, apply=args.apply)
        finally:
            await engine.dispose()
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
