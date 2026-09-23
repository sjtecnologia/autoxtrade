"""Validação de métricas de modelos ML."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

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
    """Valida performance de modelos com simulação de trades."""

    MIN_PROFIT_FACTOR = 1.3

    def validate(
        self,
        model: Any,
        X_test: pd.DataFrame,
        y_test: pd.Series,
        close_prices: pd.Series,
        forward_candles: int = 5,
    ) -> ValidationMetrics:
        """Simula trades com as predições do modelo e calcula métricas.

        Args:
            model: Modelo sklearn treinado
            X_test: Features de teste
            y_test: Labels reais (não usados na simulação — apenas para distribuição)
            close_prices: Preços de fechamento correspondentes ao X_test
            forward_candles: N candles para calcular retorno de cada trade

        Returns:
            ValidationMetrics com profit_factor, win_rate, total_trades
        """
        signals = model.predict(X_test)

        # Distribuição dos sinais
        unique, counts = np.unique(signals, return_counts=True)
        dist = {str(k): int(v) for k, v in zip(unique, counts)}
        logger.info("[validator] Sinais previstos: %s", dist)

        trades: list[dict] = []
        closes = close_prices.values

        for i, signal in enumerate(signals):
            if signal not in ("LONG", "SHORT"):
                continue

            # Verifica se há candles futuros suficientes
            exit_idx = i + forward_candles
            if exit_idx >= len(closes):
                continue

            entry = closes[i]
            exit_price = closes[exit_idx]

            if entry <= 0:
                continue

            pct_return = (exit_price - entry) / entry
            if signal == "SHORT":
                pct_return = -pct_return  # SHORT: lucro quando preço cai

            trades.append({"signal": signal, "pnl_pct": pct_return})

        metrics = self._calculate_metrics(trades, dist)
        metrics.approved = self.check_approval(metrics)
        return metrics

    def _calculate_metrics(
        self, trades: list[dict], dist: dict[str, int]
    ) -> ValidationMetrics:
        if not trades:
            return ValidationMetrics(
                profit_factor=0.0,
                win_rate=0.0,
                total_trades=0,
                signals_distribution=dist,
            )

        gains = sum(t["pnl_pct"] for t in trades if t["pnl_pct"] > 0)
        losses = abs(sum(t["pnl_pct"] for t in trades if t["pnl_pct"] < 0))
        winners = sum(1 for t in trades if t["pnl_pct"] > 0)
        expectancy = round(sum(t["pnl_pct"] for t in trades) / len(trades) * 100, 4)

        # Equity curve simples para estimar drawdown máximo percentual.
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for t in trades:
            equity *= (1.0 + t["pnl_pct"])
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak
                if dd > max_dd:
                    max_dd = dd

        if losses == 0:
            pf = float("inf")
        else:
            pf = round(gains / losses, 4)

        wr = round(winners / len(trades) * 100, 2)

        return ValidationMetrics(
            profit_factor=pf,
            win_rate=wr,
            total_trades=len(trades),
            expectancy_pct=expectancy,
            max_drawdown_pct=round(max_dd * 100, 4),
            signals_distribution=dist,
        )

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
