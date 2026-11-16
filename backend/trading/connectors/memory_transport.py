"""Explicit local fake exchange state; not an economic backtest."""
from dataclasses import replace
from decimal import Decimal
from threading import RLock
from uuid import uuid4

from trading.contracts import (OrderIntent, ExecutionResult, ResultState, Position, Fill, CapabilityUnavailable)
from trading.connectors.types import OrderSide, OrderType


def unique_id():
    return str(uuid4().int >> 65)


class MemoryTransport:
    def __init__(self, context, *, durable_effects=None):
        if durable_effects is not None:
            from paper.durable_effects import DurablePaperEffects
            if type(durable_effects) is not DurablePaperEffects or durable_effects.context != context:
                raise ValueError("Durable effects/account binding conflict")
        self.context = context
        self.durable_effects = durable_effects
        self.prices, self.intents, self.results = {}, {}, {}
        if durable_effects is None:
            self.positions = {}
        self.fills, self.canceled = [], set()
        self.scenarios = {}  # intent -> reject / timeout / delay / Decimal partial
        self.operations = {}
        self.lock = RLock()

    def _require_memory(self):
        if self.durable_effects is not None:
            raise CapabilityUnavailable("In-memory position path disabled for durable paper transport")

    def _require_durable(self):
        if self.durable_effects is None:
            raise CapabilityUnavailable("Durable paper effects required for fenced recovery")
        return self.durable_effects

    async def durable_create(self, intent):
        effects = self._require_durable()
        if intent.context != self.context:
            raise ValueError("Transport/account binding conflict")
        if self.context.position_model == "netting":
            raise CapabilityUnavailable("Netting allocation unavailable; no implicit hedging")
        if self.context.position_model == "spot" and intent.side == OrderSide.SELL:
            raise CapabilityUnavailable("Spot short unavailable; reduce identified inventory")
        if intent.order_type != OrderType.MARKET:
            raise CapabilityUnavailable("Durable paper entry supports immediate market fills only")
        scenario = self.scenarios.get(intent.id)
        if scenario is not None and not isinstance(scenario, Decimal):
            raise CapabilityUnavailable("Unsupported durable paper entry scenario")
        price = intent.price or self.prices.get(intent.context.instrument.symbol)
        if price is None:
            raise CapabilityUnavailable("Explicit simulated price required")
        result = await effects.create_entry(intent, price, initial_fill=scenario)
        self.intents[intent.id] = intent
        return result

    async def durable_query(self, *, intent_id=None, order_id=None):
        effects = self._require_durable()
        if intent_id is None:
            if not isinstance(order_id, str) or not order_id.startswith("paper_"):
                return ExecutionResult("", ResultState.UNKNOWN, reason="Order not found")
            intent_id = order_id.removeprefix("paper_")
        result = await effects.query_entry(intent_id)
        if order_id is not None and result.order_id != order_id:
            return ExecutionResult(intent_id, ResultState.UNKNOWN, reason="Order identity mismatch")
        return result

    async def durable_positions(self):
        return await self._require_durable().list_positions()

    async def observe_fill(self, intent_id, *, filled, average, terminal, revision):
        return await self._require_durable().observe_entry(
            intent_id, filled=filled, average=average, terminal=terminal, revision=revision
        )

    async def acquire_fence(self, position_id):
        return await self._require_durable().acquire_fence(position_id)

    async def durable_protect(self, position_id, stop_loss, take_profit, operation_id, fence,
                              revision=None, quantity=None, owner=None, evidence_id=None):
        effects = self._require_durable()
        if fence is None or evidence_id is None:
            raise CapabilityUnavailable("Fencing token and market evidence required")
        return await effects.protect(position_id, operation_id, fence, stop_loss=stop_loss,
                                     take_profit=take_profit,
                                     revision=revision, quantity=quantity, owner=owner,
                                     evidence_id=evidence_id)

    async def durable_reduce(self, position_id, amount, operation_id, fence, execution_price, owner=None):
        effects = self._require_durable()
        if fence is None or execution_price is None:
            raise CapabilityUnavailable("Fencing token and explicit execution price required")
        return await effects.reduce(position_id, operation_id, fence, quantity=amount,
                                    execution_price=execution_price, owner=owner)

    def create(self, intent: OrderIntent):
        self._require_memory()
        with self.lock:
            if intent.context != self.context:
                raise ValueError("Transport/account binding conflict")
            if intent.id in self.intents:
                if self.intents[intent.id] != intent:
                    raise ValueError("Intent id reused with different order")
                return self.results[intent.id]
            intent.validate()
            if self.context.position_model == "netting":
                raise CapabilityUnavailable("Netting allocation unavailable; no implicit hedging")
            if self.context.position_model == "spot" and intent.side == OrderSide.SELL:
                raise CapabilityUnavailable("Spot short unavailable; reduce identified inventory")
            price = intent.price or self.prices.get(intent.context.instrument.symbol)
            if price is None:
                raise CapabilityUnavailable("Explicit simulated price required")
            intent.context.instrument.validate(intent.quantity, price)
            self.intents[intent.id] = intent
            self.results[intent.id] = ExecutionResult(intent.id, ResultState.UNKNOWN, unique_id())
            scenario = self.scenarios.get(intent.id)
            if scenario == "reject":
                self.results[intent.id] = replace(self.results[intent.id], state=ResultState.REJECTED, reason="Simulated rejection", terminal=True)
            elif scenario not in {"timeout", "delay"}:
                if intent.order_type == OrderType.LIMIT and not isinstance(scenario, Decimal):
                    self.results[intent.id] = replace(self.results[intent.id], state=ResultState.CONFIRMED, terminal=False)
                else:
                    self.settle(intent.id, scenario if isinstance(scenario, Decimal) else intent.quantity, price)
            return self.results[intent.id]

    def settle(self, intent_id, total_filled, price=None):
        """Cumulative fill: duplicate/stale updates cannot apply fills twice."""
        self._require_memory()
        with self.lock:
            intent, old = self.intents[intent_id], self.results[intent_id]
            if old.order_id in self.canceled or old.state == ResultState.REJECTED or total_filled <= old.filled:
                return old
            if total_filled > intent.quantity:
                raise ValueError("Invalid cumulative fill")
            price = price or self.prices.get(intent.context.instrument.symbol) or intent.price
            intent.context.instrument.validate(total_filled, price)
            pid, delta = old.position_id or unique_id(), total_filled - old.filled
            avg = ((old.average or Decimal(0)) * old.filled + price * delta) / total_filled
            pos = self.positions.get(pid)
            self.positions[pid] = replace(pos, quantity=pos.quantity + delta, entry_price=avg) if pos else Position(pid, intent.context, intent.side, total_filled, avg)
            self.fills.append(Fill(unique_id(), intent_id, old.order_id, pid, delta, price))
            result = replace(old, position_id=pid, filled=total_filled, average=avg,
                             state=ResultState.CONFIRMED if total_filled == intent.quantity else ResultState.PARTIAL,
                             terminal=total_filled == intent.quantity)
            self.results[intent_id] = result
            return result

    def query(self, *, intent_id=None, order_id=None):
        self._require_memory()
        with self.lock:
            if intent_id is not None:
                return self.results.get(intent_id, ExecutionResult(intent_id, ResultState.UNKNOWN, reason="Intent not found"))
            matches = [r for r in self.results.values() if r.order_id == order_id]
            return matches[0] if len(matches) == 1 else ExecutionResult("", ResultState.UNKNOWN, reason="Order not found")

    def pending(self):
        self._require_memory()
        return [r for key, r in self.results.items() if r.state in {ResultState.CONFIRMED, ResultState.PARTIAL}
                and r.filled < self.intents[key].quantity and r.order_id not in self.canceled]

    def _previous(self, operation_id, fingerprint):
        old_key, result = self.operations[operation_id]
        if old_key != fingerprint:
            raise ValueError("Operation id reused with different request")
        return result

    def cancel(self, order_id, operation_id):
        self._require_memory()
        with self.lock:
            key = ("cancel", order_id)
            if operation_id in self.operations:
                return self._previous(operation_id, key)
            result = self.query(order_id=order_id)
            if result not in self.pending():
                return ExecutionResult(operation_id, ResultState.REJECTED, reason="Not a pending order")
            self.canceled.add(order_id)
            self.results[result.intent_id] = replace(result, terminal=True)
            answer = ExecutionResult(operation_id, ResultState.CONFIRMED, order_id)
            self.operations[operation_id] = (key, answer)
            return answer

    def protect(self, position_id, stop_loss, take_profit, operation_id):
        self._require_memory()
        with self.lock:
            key = ("protect", position_id, stop_loss, take_profit)
            if operation_id in self.operations:
                return self._previous(operation_id, key)
            pos = self.positions[position_id]
            for price in (stop_loss, take_profit):
                if price is not None:
                    pos.context.instrument.validate(pos.quantity, price)
            if stop_loss is None:
                raise ValueError("Stop loss required")
            current = self.prices.get(pos.context.instrument.symbol, pos.entry_price)
            if (pos.side == OrderSide.BUY and (stop_loss >= current or take_profit is not None and take_profit <= current)
                    or pos.side == OrderSide.SELL and (stop_loss <= current or take_profit is not None and take_profit >= current)):
                raise ValueError("Invalid protection side")
            self.positions[position_id] = replace(pos, stop_loss=stop_loss, take_profit=take_profit)
            result = ExecutionResult(operation_id, ResultState.CONFIRMED, position_id=position_id)
            self.operations[operation_id] = (key, result)
            return result

    def reduce(self, position_id, amount, operation_id):
        self._require_memory()
        with self.lock:
            key = ("reduce", position_id, amount)
            if operation_id in self.operations:
                return self._previous(operation_id, key)
            pos = self.positions[position_id]
            pos.context.instrument.validate(amount)
            if amount > pos.quantity:
                return ExecutionResult(operation_id, ResultState.REJECTED, position_id=position_id, reason="Reduction exceeds position")
            price = self.prices.get(pos.context.instrument.symbol)
            if price is None:
                raise CapabilityUnavailable("Simulated exit price missing")
            if amount == pos.quantity:
                del self.positions[position_id]
            else:
                self.positions[position_id] = replace(pos, quantity=pos.quantity-amount)
            oid = unique_id()
            self.fills.append(Fill(unique_id(), operation_id, oid, position_id, amount, price))
            result = ExecutionResult(operation_id, ResultState.CONFIRMED, oid, position_id, amount, price)
            self.operations[operation_id] = (key, result)
            return result
