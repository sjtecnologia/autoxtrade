"""Per-symbol cost assumptions shared by paper execution and qualification."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from config import settings
from trading.safety import ExposureBlocked


@dataclass(frozen=True)
class PaperCostModel:
    spread_price: Decimal
    commission_rate: Decimal
    slippage_price: Decimal

    def as_dict(self) -> dict[str, str]:
        return {
            "spread_price": str(self.spread_price),
            "commission_rate": str(self.commission_rate),
            "slippage_price": str(self.slippage_price),
        }


def parse_costs(payload: str | dict) -> dict[str, PaperCostModel]:
    try:
        raw = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(raw, dict):
            raise ValueError("cost model must be an object keyed by symbol")
        costs = {}
        for symbol, item in raw.items():
            if not isinstance(symbol, str) or not symbol or not isinstance(item, dict):
                raise ValueError("invalid symbol cost entry")
            spread = Decimal(str(item["spread_price"]))
            commission = Decimal(str(item["commission_rate"]))
            slippage = Decimal(str(item["slippage_price"]))
            if any(not value.is_finite() or value < 0 for value in (spread, commission, slippage)):
                raise ValueError(f"invalid non-negative costs required for {symbol}")
            if spread + commission + slippage <= 0:
                raise ValueError(f"at least one non-zero execution cost is required for {symbol}")
            costs[symbol] = PaperCostModel(spread, commission, slippage)
        return costs
    except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
        raise ExposureBlocked(f"Invalid paper cost configuration: {exc}") from exc


def paper_costs_for(symbol: str) -> PaperCostModel:
    costs = parse_costs(settings.paper_costs_json)
    if symbol not in costs:
        raise ExposureBlocked(f"Paper costs are not configured for {symbol}")
    return costs[symbol]
