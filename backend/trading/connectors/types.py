"""Tipos compartilhados entre connectors."""
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    OCO = "oco"


class OrderStatus(str, Enum):
    UNKNOWN = "unknown"
    REJECTED = "rejected"
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"
    EXPIRED = "expired"
    PARTIAL = "partial"


@dataclass
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass
class Order:
    id: str
    symbol: str
    side: OrderSide
    type: OrderType
    status: OrderStatus
    amount: Decimal
    price: Decimal | None
    average: Decimal | None
    filled: Decimal
    remaining: Decimal
    timestamp: int | None
    raw: dict = field(default_factory=dict)


@dataclass
class OHLCVCandle:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
