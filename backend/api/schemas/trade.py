from decimal import Decimal

from pydantic import BaseModel


class BalanceAsset(BaseModel):
    asset: str
    free: Decimal
    locked: Decimal
    total: Decimal


class PositionSchema(BaseModel):
    id: int
    symbol: str
    side: str
    mode: str
    entry_price: Decimal
    quantity: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None
    current_price: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pnl_pct: Decimal | None
    open_at: str
    model_version: str | None
    ml_confidence: Decimal | None

    model_config = {"from_attributes": True}
