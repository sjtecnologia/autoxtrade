"""Testes unitários do ModelValidator."""
import pytest

from ml.validator import ModelValidator, ValidationMetrics


class _FakeModel:
    """Mock de modelo sklearn."""

    def __init__(self, predictions: list[str], probas: list[list[float]]) -> None:
        self._predictions = predictions
        self._probas = probas
        self.classes_ = ["LONG", "NEUTRO", "SHORT"]

    def predict(self, X):
        return self._predictions[: len(X)]

    def predict_proba(self, X):
        import numpy as np
        return np.array(self._probas[: len(X)])


import pandas as pd
import numpy as np


def _make_X(n: int) -> pd.DataFrame:
    return pd.DataFrame({"feat1": [0.1] * n})


def test_check_approval_aprovado_limite():
    """Profit Factor exatamente 1.3 → aprovado."""
    validator = ModelValidator()
    metrics = ValidationMetrics(profit_factor=1.3, win_rate=50.0, total_trades=10)
    assert validator.check_approval(metrics) is True


def test_check_approval_rejeitado_abaixo():
    """Profit Factor 1.29 → rejeitado."""
    validator = ModelValidator()
    metrics = ValidationMetrics(profit_factor=1.29, win_rate=49.0, total_trades=10)
    assert validator.check_approval(metrics) is False


def test_check_approval_aprovado_alto():
    """Profit Factor 2.5 → aprovado."""
    validator = ModelValidator()
    metrics = ValidationMetrics(profit_factor=2.5, win_rate=70.0, total_trades=20)
    assert validator.check_approval(metrics) is True


def test_calculo_profit_factor():
    """Simula trades com PnL conhecido e verifica Profit Factor."""
    validator = ModelValidator()
    # Criamos 5 sinais LONG; preços: entrada 100, saída T+5
    # trade 1: +2% | trade 2: -1% | trade 3: +3% | trade 4: -1% | trade 5: +2%
    trades = [
        {"signal": "LONG", "pnl_pct": 0.02},
        {"signal": "LONG", "pnl_pct": -0.01},
        {"signal": "LONG", "pnl_pct": 0.03},
        {"signal": "LONG", "pnl_pct": -0.01},
        {"signal": "LONG", "pnl_pct": 0.02},
    ]
    metrics = validator._calculate_metrics(trades, {})
    # gains = 0.02+0.03+0.02 = 0.07 | losses = 0.01+0.01 = 0.02 | PF = 3.5
    assert abs(metrics.profit_factor - 3.5) < 0.01
    assert metrics.win_rate == 60.0
    assert metrics.total_trades == 5


def test_sem_trades_profit_factor_zero():
    """Nenhum sinal LONG/SHORT → profit_factor=0, total_trades=0."""
    validator = ModelValidator()
    predictions = ["NEUTRO"] * 10
    model = _FakeModel(predictions, [[0.1, 0.8, 0.1]] * 10)
    X = _make_X(10)
    y = pd.Series(["NEUTRO"] * 10)
    closes = pd.Series([100.0] * 20)

    metrics = validator.validate(model, X, y, closes)
    assert metrics.total_trades == 0
    assert metrics.profit_factor == 0.0
    assert metrics.approved is False


def test_profit_factor_infinito_apenas_ganhos():
    """Só vencedores → profit_factor = inf → aprovado."""
    validator = ModelValidator()
    trades = [{"signal": "LONG", "pnl_pct": 0.05} for _ in range(5)]
    metrics = validator._calculate_metrics(trades, {})
    assert metrics.profit_factor == float("inf")
    assert validator.check_approval(metrics) is True
