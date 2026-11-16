"""Validate durable paper costs and fully accounted, already closed exposure."""
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from paper.costs import parse_costs
from trading.safety import ExposureBlocked


def validated_entry_costs(context, trade, entry):
    """Use the immutable entry request, never the current cost configuration."""
    if entry is None or entry.payload.get("kind") != "entry":
        raise ValueError("Immutable entry receipt missing; costs cannot be verified")
    costs_data = entry.payload.get("paper_costs")
    if not isinstance(costs_data, dict) or (trade.execution_context or {}).get("paper_costs") != costs_data:
        raise ValueError("Paper costs conflict with immutable entry receipt")
    costs = parse_costs({trade.symbol: costs_data})[trade.symbol]
    raw = Decimal(entry.result["average"])
    offset = costs.spread_price + costs.slippage_price
    adjusted = raw + offset if trade.side == "buy" else raw - offset
    if (trade.side not in {"buy", "sell"} or not raw.is_finite() or raw <= 0
            or not adjusted.is_finite() or adjusted <= 0
            or trade.entry_price != adjusted
            or trade.entry_value != (
                adjusted * trade.quantity * context.instrument.contract_size
            ).quantize(Decimal("0.01"))):
        raise ValueError("Trade entry accounting conflicts with immutable entry receipt")
    return costs


async def closed_trade_proof(db, effects, position_id):
    """Read-only proof under the normal intent -> trade -> authority -> position locks.

    Released leases are irrelevant to an already committed effect. The immutable
    decision and receipt must still describe the current terminal exposure, and
    the Trade's accounting and event must have committed together with its cursor.
    """
    from db.models import TradeEvent
    from paper.durable_effects import PaperEffectOperation, PaperProtectionAttempt

    intent, trade, authority, position = await effects._locked_protection_state(db, position_id)
    if (trade is None or trade.status != "closed" or position.quantity != 0
            or not trade.close_order_id or not trade.close_order_id.startswith("manual-close:")):
        return None
    key = (effects._account_key, position_id, trade.close_order_id)
    attempt = await db.get(PaperProtectionAttempt, key)
    receipt = await db.get(PaperEffectOperation, key)
    entry = await db.get(PaperEffectOperation, (effects._account_key, position_id, "entry:" + intent.intent_id))
    if attempt is None or receipt is None or attempt.payload != receipt.payload:
        return None
    payload = receipt.payload
    try:
        if (payload.get("kind") != "reduce" or not effects._manual_close_matches(
                intent, trade, authority, position, entry, payload, Decimal("0"))
                or attempt.revision != payload["revision"]
                or receipt.result != {"filled": payload["quantity"], "average": payload["execution_price"]}
                or intent.order_id != trade.open_order_id or intent.order_id != entry.result["order_id"]
                or intent.quantity != authority.quantity
                or intent.result_state != ("confirmed" if trade.quantity == intent.quantity else "partial")
                or not Decimal("0") < trade.quantity <= intent.quantity
                or trade.close_reason != payload["reason"]
                or trade.close_at is None or trade.close_at < trade.open_at
                or trade.entry_value is None or trade.entry_value <= 0):
            return None
        costs = validated_entry_costs(effects.context, trade, entry)
        raw_exit = Decimal(payload["execution_price"])
        offset = costs.spread_price + costs.slippage_price
        exit_price = raw_exit - offset if trade.side == "buy" else raw_exit + offset
        size = effects.context.instrument.contract_size
        commission = (trade.entry_price + exit_price) * trade.quantity * size * costs.commission_rate
        gross = (exit_price - trade.entry_price) * trade.quantity * size * (1 if trade.side == "buy" else -1)
        if (not raw_exit.is_finite() or raw_exit <= 0 or not exit_price.is_finite() or exit_price <= 0
                or trade.exit_price != exit_price
                or trade.exit_value != (exit_price * trade.quantity * size).quantize(Decimal("0.01"))
                or trade.commission != commission.quantize(Decimal("0.01"))
                or trade.pnl_gross != gross.quantize(Decimal("0.01"))
                or trade.pnl_net != (gross - commission).quantize(Decimal("0.01"))
                or trade.pnl_pct != ((gross - commission) / trade.entry_value * 100).quantize(Decimal("0.0001"))):
            return None
        events = (await db.execute(select(TradeEvent).where(
            TradeEvent.trade_id == trade.id,
            TradeEvent.event_type == ("MANUAL_CLOSE" if payload["candle"] is None else payload["reason"]),
            TradeEvent.payload["operation_id"].astext == receipt.operation_id,
        ))).scalars().all()
        if (len(events) != 1 or events[0].payload.get("reason") != payload["reason"]
                or Decimal(events[0].payload["exit_price"]) != trade.exit_price
                or events[0].payload.get("candle") != payload["candle"]):
            return None
        return dict(
            outcome="closed" if trade.quantity == intent.quantity else "closed_partial",
            trade_id=trade.id, close_operation_id=receipt.operation_id, reason=trade.close_reason,
            requested_quantity=str(intent.quantity), filled_quantity=str(trade.quantity),
            unfilled_quantity=str(intent.quantity - trade.quantity),
        )
    except (ExposureBlocked, KeyError, TypeError, ValueError, InvalidOperation):
        return None
