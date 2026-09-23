"""Binance spot/CCXT contract. External clients remain blocked; no constructor I/O.

Supported offline syntax: CCXT 4.3.1 create_order/fetch_order for spot MARKET/LIMIT.
OCO/conditional orders and margin/futures require separate validated capabilities.
"""
from decimal import Decimal
import ccxt.async_support as ccxt

from trading.contracts import Capability, CapabilityUnavailable, ExecutionResult, ResultState
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.fake_transports import FakeCCXTClient
from trading.safety import AccountNature, require_new_exposure, require_position_management


class BinanceConnector(SimulatedConnector):
    supported_capabilities = frozenset(Capability) - {Capability.PROTECT}
    account_nature = AccountNature.UNKNOWN

    def __init__(self, *, context=None, client=None):
        self._bind(context, client)
        self._exchange = client  # injectable; never constructed from .env credentials
        self.prices = client.prices if type(client) is FakeCCXTClient else {}
        if type(client) is FakeCCXTClient:
            if not hasattr(client, "attempts"):
                client.attempts = {}

    async def create_order(self, intent):
        require_new_exposure(self)
        self._check_use(Capability.CREATE)
        if intent.context != self.context:
            raise ValueError("Request conflicts with bound account/venue/mode")
        if self.context.venue != "binance_spot" or self.context.instrument.asset_class != "spot":
            raise CapabilityUnavailable("Only explicitly bound Binance spot is supported")
        if self.context.position_model != "spot" or self._exchange.version != "4.3.1":
            raise CapabilityUnavailable("Unvalidated CCXT version or market type")
        intent.validate()
        price = intent.price or self.prices.get(intent.context.instrument.symbol)
        if price is None:
            raise CapabilityUnavailable("Price required to validate spot notional filters")
        self.context.instrument.validate(intent.quantity, price)
        if intent.side.value == "sell":
            raise CapabilityUnavailable("Spot SHORT entry unsupported; no automatic margin/futures")
        with self._exchange.lock:
            previous = self._exchange.attempts.get(intent.id)
            if previous is not None and previous != intent:
                raise ValueError("Intent id already bound to another request")
            self._exchange.attempts[intent.id] = intent
        if previous is not None:
            return await self.query_order(intent_id=intent.id)
        try:
            raw = await self._exchange.create_order(
                symbol=intent.context.instrument.symbol, type=intent.order_type.value,
                side=intent.side.value, amount=str(intent.quantity),
                price=str(intent.price) if intent.price is not None else None,
                params={"newClientOrderId": intent.id})
        except (TimeoutError, ccxt.NetworkError):
            # A lost response is not rejection. Query first, NEVER repeat create.
            return await self.query_order(intent_id=intent.id)
        except ccxt.InvalidOrder:
            return ExecutionResult(intent.id, ResultState.REJECTED, reason="Order explicitly rejected")
        return self._parse_result(raw, intent.id)

    def _parse_result(self, raw, intent_id):
        if raw.get("clientOrderId") != intent_id or raw.get("symbol") != self.context.instrument.symbol:
            return ExecutionResult(intent_id, ResultState.UNKNOWN, reason="Response identity mismatch")
        filled = Decimal(str(raw.get("filled") or 0))
        amount = Decimal(str(raw.get("amount") or 0))
        status = raw.get("status")
        state = {"rejected": ResultState.REJECTED, "open": ResultState.CONFIRMED,
                 "closed": ResultState.CONFIRMED, "canceled": ResultState.CONFIRMED}.get(status, ResultState.UNKNOWN)
        if status == "closed" and (amount <= 0 or filled != amount):
            state = ResultState.UNKNOWN
        elif 0 < filled < amount:
            state = ResultState.PARTIAL
        return ExecutionResult(intent_id, state, str(raw["id"]) if raw.get("id") else None,
                               raw.get("position_id"), filled,
                               Decimal(str(raw["average"])) if raw.get("average") is not None else None)

    async def query_order(self, *, intent_id=None, order_id=None):
        self._check_use(Capability.QUERY)
        try:
            raw = await self._exchange.fetch_order(order_id, self.context.instrument.symbol,
                                                   {"origClientOrderId": intent_id} if intent_id else {})
        except (TimeoutError, ccxt.NetworkError, ccxt.OrderNotFound):
            return ExecutionResult(intent_id or "", ResultState.UNKNOWN, order_id, reason="Acceptance remains unknown; no resend")
        return self._parse_result(raw, intent_id or raw.get("clientOrderId", ""))

    async def place_oco_order(self, symbol, side, amount, take_profit_price, stop_price, stop_limit_price):
        require_position_management(self)
        raise CapabilityUnavailable("Spot OCO endpoint is not validated; type='oco' is not a generic create_order type")

    async def modify_position_protection(self, position_id, *, stop_loss, take_profit=None, intent_id):
        require_position_management(self)
        raise CapabilityUnavailable("Binance spot protection requires a validated order-list contract")

    async def reduce_position(self, position_id, quantity, *, intent_id):
        # Local inventory reduction only. This never selects a derivatives endpoint.
        self._check_use(Capability.REDUCE)
        return await super().reduce_position(position_id, quantity, intent_id=intent_id)
