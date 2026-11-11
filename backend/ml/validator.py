"""Validação de métricas de modelos ML."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from paper.backtest import replay_signals
from paper.costs import PaperCostModel

logger = logging.getLogger(__name__)


@dataclass
class ValidationMetrics:
    profit_factor: float
    win_rate: float  # percentual 0-100
    total_trades: int
    expectancy_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    signals_distribution: dict[str, int] = field(default_factory=dict)
    approved: bool = False


class ModelValidator:
    """Valida modelos pelo replay paper de sinais e limites de portfólio."""

    MIN_PROFIT_FACTOR = 1.3

    def validate(
        self,
        model: Any,
        X_test: pd.DataFrame,
        y_test: pd.Series,
        market_data: pd.DataFrame,
        forward_candles: int,
        *,
        costs: PaperCostModel,
        initial_capital: Decimal,
        max_open_positions: int = 1,
        risk_per_trade_pct: Decimal = Decimal("1"),
        max_position_pct: Decimal = Decimal("10"),
        contract_size: Decimal = Decimal("1"),
        volume_step: Decimal = Decimal("0.00000001"),
        min_quantity: Decimal | None = None,
        max_quantity: Decimal | None = None,
        margin_rate: Decimal | None = None,
    ) -> ValidationMetrics:
        """Prediz e simula apenas os sinais executáveis no período de teste."""
        signals = model.predict(X_test)
        if (
            len(signals) != len(X_test)
            or len(y_test) != len(X_test)
            or len(market_data) != len(X_test)
        ):
            raise ValueError("Predictions, test features, and OHLC data must align")

        unique, counts = np.unique(signals, return_counts=True)
        dist = {str(key): int(count) for key, count in zip(unique, counts)}
        logger.info("[validator] Sinais previstos: %s", dist)

        replay = replay_signals(
            market_data,
            signals,
            forward_candles=forward_candles,
            spread_price=costs.spread_price,
            commission_rate=costs.commission_rate,
            slippage_price=costs.slippage_price,
            initial_capital=initial_capital,
            max_open_positions=max_open_positions,
            risk_per_trade_pct=risk_per_trade_pct,
            max_position_pct=max_position_pct,
            contract_size=contract_size,
            volume_step=volume_step,
            min_quantity=min_quantity,
            max_quantity=max_quantity,
            margin_rate=margin_rate,
        )
        net = replay["net_metrics_under_project_paper_costs"]
        metrics = ValidationMetrics(
            profit_factor=float(net["profit_factor"]),
            win_rate=float(net["win_rate_pct"]),
            total_trades=int(replay["total_trades"]),
            expectancy_pct=float(net["expectancy_return_pct"]),
            max_drawdown_pct=float(net["max_drawdown_pct"]),
            signals_distribution=dist,
        )
        metrics.approved = self.check_approval(metrics)
        return metrics

    def check_approval(self, metrics: ValidationMetrics) -> bool:
        """Aprova modelo se Profit Factor ≥ 1.3."""
        approved = metrics.profit_factor >= self.MIN_PROFIT_FACTOR
        logger.info(
            "[validator] PF=%.4f WR=%.1f%% trades=%d → %s",
            metrics.profit_factor,
            metrics.win_rate,
            metrics.total_trades,
            "APROVADO" if approved else "REJEITADO",
        )
        return approved
