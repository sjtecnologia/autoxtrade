"""Immutable identities and capability contracts; no broker defaults or credentials."""
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from enum import Enum
import json
import re

from trading.safety import AccountNature, ExposureBlocked
from trading.connectors.types import OrderSide, OrderType


class CapabilityUnavailable(ExposureBlocked):
    pass


class ExecutionMode(str, Enum):
    PAPER = "paper"
    DEMO = "demo"
    TESTNET = "testnet"
    LIVE = "live"


class Capability(str, Enum):
    CREATE = "create"
    QUERY = "query"
    PENDING = "pending"
    POSITIONS = "positions"
    CANCEL = "cancel"
    PROTECT = "protect"
    REDUCE = "reduce"
    FILLS = "fills"


class ResultState(str, Enum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: str
    base_currency: str | None = None
    quote_currency: str | None = None
    contract_size: Decimal | None = None
    tick_size: Decimal | None = None
    tick_value: Decimal | None = None
    tick_currency: str | None = None
    min_volume: Decimal | None = None
    max_volume: Decimal | None = None
    volume_step: Decimal | None = None
    price_precision: int | None = None
    volume_precision: int | None = None
    margin_model: str | None = None
    margin_rate: Decimal | None = None
    min_notional: Decimal | None = None

    def validate(self, amount: Decimal, price: Decimal | None = None):
        required = (self.base_currency, self.quote_currency, self.tick_currency, self.margin_model)
        numbers = (self.contract_size, self.tick_size, self.tick_value, self.min_volume,
                   self.max_volume, self.volume_step, self.margin_rate)
        if not all(required) or any(v is None or not v.is_finite() or v <= 0 for v in numbers):
            raise CapabilityUnavailable("Incomplete instrument/currency/margin metadata")
        if self.price_precision is None or self.volume_precision is None:
            raise CapabilityUnavailable("Missing instrument precision")
        if self.min_volume > self.max_volume or min(self.price_precision, self.volume_precision) < 0:
            raise ValueError("Invalid instrument limits")
        if not amount.is_finite() or not self.min_volume <= amount <= self.max_volume:
            raise ValueError("Volume outside instrument limits")
        if amount % self.volume_step or amount != amount.quantize(Decimal(1).scaleb(-self.volume_precision)):
            raise ValueError("Invalid volume step/precision")
        if price is not None:
            if not price.is_finite() or price <= 0 or price % self.tick_size:
                raise ValueError("Invalid price tick")
            if price != price.quantize(Decimal(1).scaleb(-self.price_precision)):
                raise ValueError("Invalid price precision")
            if self.min_notional is not None and amount * price * self.contract_size < self.min_notional:
                raise ValueError("Below minimum notional")


@dataclass(frozen=True)
class ExecutionContext:
    venue: str
    broker: str
    account_id: str
    nature: AccountNature
    verified: bool
    mode: ExecutionMode
    market: str
    account_currency: str | None
    position_model: str  # spot, hedging, netting, unknown
    instrument: Instrument
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    def validate(self):
        if not all(isinstance(v, str) and v.strip() for v in
                   (self.venue, self.broker, self.account_id, self.instrument.symbol)):
            raise ExposureBlocked("Execution identity missing")
        if not self.verified or self.nature == AccountNature.UNKNOWN:
            raise ExposureBlocked("Account nature is not verified")
        expected = {ExecutionMode.PAPER: AccountNature.SIMULATED,
                    ExecutionMode.DEMO: AccountNature.DEMO,
                    ExecutionMode.TESTNET: AccountNature.DEMO,
                    ExecutionMode.LIVE: AccountNature.REAL}
        if expected.get(self.mode) != self.nature:
            raise ExposureBlocked("Execution mode/account nature conflict")
        if self.market not in {"CRIPTO", "B3", "FOREX"} or not self.account_currency:
            raise ExposureBlocked("Market/account currency missing")
        if self.position_model not in {"spot", "hedging", "netting"}:
            raise CapabilityUnavailable("Position model is unknown")

    def require(self, capability: Capability):
        self.validate()
        if capability not in self.capabilities:
            raise CapabilityUnavailable(f"Capability unavailable: {capability.value}")

    def snapshot(self):
        return json.loads(json.dumps(asdict(self), default=lambda v: list(v) if isinstance(v, frozenset) else str(v)))


def context_from_dict(data: dict | None) -> ExecutionContext:
    if not data:
        raise ExposureBlocked("Server-side account/instrument binding is missing")
    values = dict(data)
    values["nature"] = AccountNature(values["nature"])
    values["mode"] = ExecutionMode(values["mode"])
    values["capabilities"] = frozenset(Capability(c) for c in values.get("capabilities", []))
    spec = dict(values["instrument"])
    for key in ("contract_size", "tick_size", "tick_value", "min_volume", "max_volume",
                "volume_step", "margin_rate", "min_notional"):
        if spec.get(key) is not None:
            spec[key] = Decimal(str(spec[key]))
    values["instrument"] = Instrument(**spec)
    result = ExecutionContext(**values)
    result.validate()
    return result


@dataclass(frozen=True)
class OrderIntent:
    id: str
    context: ExecutionContext
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    price: Decimal | None = None

    def validate(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", self.id):
            raise ValueError("Intent id must be 1-32 safe characters")
        if self.order_type not in {OrderType.MARKET, OrderType.LIMIT}:
            raise CapabilityUnavailable("Use identified position protection, not a generic stop/OCO order")
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError("Limit price required")
        self.context.require(Capability.CREATE)
        self.context.instrument.validate(self.quantity, self.price)


@dataclass(frozen=True)
class ExecutionResult:
    intent_id: str
    state: ResultState
    order_id: str | None = None
    position_id: str | None = None
    filled: Decimal = Decimal("0")
    average: Decimal | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Position:
    id: str
    context: ExecutionContext
    side: OrderSide
    quantity: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None


@dataclass(frozen=True)
class Fill:
    id: str
    intent_id: str
    order_id: str
    position_id: str
    quantity: Decimal
    price: Decimal
