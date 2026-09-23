from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class BotConfigResponse(BaseModel):
    id: int
    market: str
    is_active: bool
    mode: str
    exchange: str
    enabled_symbols: list[str]
    risk_per_trade_pct: Decimal
    max_open_trades: int
    max_drawdown_pct: Decimal
    drawdown_alert_pct: Decimal
    max_corr_threshold: Decimal
    min_ml_confidence: Decimal
    current_drawdown_1d: Decimal
    current_drawdown_30d: Decimal
    peak_capital: Decimal | None
    drawdown_status: str
    paused_reason: str | None
    paused_at: datetime | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class BotConfigPatch(BaseModel):
    mode: str | None = Field(None, pattern="^(paper|live)$")
    enabled_symbols: list[str] | None = None
    risk_per_trade_pct: Decimal | None = Field(None, gt=0, le=10)
    max_open_trades: int | None = Field(None, ge=1, le=20)
    max_drawdown_pct: Decimal | None = Field(None, gt=0, le=100)
    drawdown_alert_pct: Decimal | None = Field(None, gt=0, le=100)
    max_corr_threshold: Decimal | None = Field(None, gt=0, le=1)
    min_ml_confidence: Decimal | None = Field(None, gt=0, le=1)
