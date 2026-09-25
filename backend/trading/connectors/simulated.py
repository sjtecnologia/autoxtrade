"""Contract adapter backed only by explicit in-memory fake transport."""
from decimal import Decimal
from uuid import uuid4
from trading.connectors.base import BaseConnector
from trading.connectors.types import Order, OrderType, OrderStatus
from trading.connectors.memory_transport import MemoryTransport
from trading.contracts import Capability, CapabilityUnavailable, OrderIntent, ResultState
from trading.safety import AccountNature, require_new_exposure


class SimulatedConnector(BaseConnector):
    supported_capabilities = frozenset(Capability)
    account_nature = AccountNature.SIMULATED  # informational only, never authorization

    def __init__(self, context=None, *, transport=None):
        self._bind(context, transport if transport is not None else MemoryTransport(context))
        self.prices = self._transport.prices

    async def connect(self):
        self._check_use(Capability.QUERY)

    async def close(self):
        self.retire()

    async def get_balance(self):
        raise CapabilityUnavailable("Simulator has no economic ledger (stage 04)")

    async def get_ohlcv(self, symbol, timeframe="5m", limit=200):
        raise CapabilityUnavailable("Simulator has no market data feed")

    async def get_current_price(self, symbol):
        self._check_use(Capability.QUERY)
        if symbol != self.context.instrument.symbol or symbol not in self.prices:
            raise CapabilityUnavailable("Explicit simulated price for bound instrument required")
        return self.prices[symbol]

    async def create_order(self, intent):
        require_new_exposure(self)
        self._check_use(Capability.CREATE)
        if intent.context != self.context:
            raise ValueError("Request conflicts with bound execution identity")
        return self._transport.create(intent)

    async def query_order(self, *, intent_id=None, order_id=None):
        self._check_use(Capability.QUERY)
        return self._transport.query(intent_id=intent_id, order_id=order_id)

    async def list_pending_orders(self):
        self._check_use(Capability.PENDING)
        return list(self._transport.pending())

    async def list_positions(self):
        self._check_use(Capability.POSITIONS)
        return list(self._transport.positions.values())

    async def cancel_pending_order(self, order_id, *, intent_id):
        self._check_use(Capability.CANCEL)
        return self._transport.cancel(order_id, intent_id)

    async def modify_position_protection(self, position_id, *, stop_loss, take_profit=None, intent_id):
        self._check_use(Capability.PROTECT)
        return self._transport.protect(position_id, stop_loss, take_profit, intent_id)

    async def reduce_position(self, position_id, quantity, *, intent_id):
        self._check_use(Capability.REDUCE)
        return self._transport.reduce(position_id, quantity, intent_id)

    async def get_fills(self, *, order_id=None):
        self._check_use(Capability.FILLS)
        return [f for f in self._transport.fills if order_id is None or f.order_id == order_id]

    async def place_order(self, symbol, side, order_type, amount, price=None, params=None):
        require_new_exposure(self)
        if symbol != self.context.instrument.symbol:
            raise ValueError("Native symbol conflicts with context")
        if params and set(params) - {"intent_id"}:
            raise ValueError("Unvalidated order parameters")
        result = await self.create_order(OrderIntent((params or {}).get("intent_id", uuid4().hex),
                                                     self.context, side, order_type, amount, price))
        return self._legacy_order(result)

    def _legacy_order(self, result):
        intent = self._transport.intents[result.intent_id]
        status = {ResultState.UNKNOWN: OrderStatus.UNKNOWN, ResultState.REJECTED: OrderStatus.REJECTED,
                  ResultState.PARTIAL: OrderStatus.PARTIAL}.get(result.state,
                  OrderStatus.CLOSED if result.filled == intent.quantity else OrderStatus.OPEN)
        return Order(result.order_id or "", intent.context.instrument.symbol, intent.side, intent.order_type,
                     status, intent.quantity, intent.price, result.average, result.filled,
                     intent.quantity-result.filled, None,
                     {"intent_id": intent.id, "position_id": result.position_id, "context": self.context.snapshot()})

    async def place_oco_order(self, symbol, side, amount, take_profit_price, stop_price, stop_limit_price):
        raise CapabilityUnavailable("OCO needs position identity; use modify_position_protection")

    async def cancel_order(self, order_id, symbol):
        if self.context is None or symbol != self.context.instrument.symbol:
            raise CapabilityUnavailable("Cancellation identity missing/conflicting")
        result = await self.cancel_pending_order(order_id, intent_id="cancel_" + order_id)
        return result.state == ResultState.CONFIRMED

    async def get_open_orders(self, symbol=None):
        rows = await self.list_pending_orders()
        if symbol is not None and symbol != self.context.instrument.symbol:
            raise ValueError("Symbol conflicts with context")
        return [self._legacy_order(r) for r in rows]
