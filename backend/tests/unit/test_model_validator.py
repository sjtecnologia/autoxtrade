from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from ml.trainer import ModelTrainer
from ml.validator import ModelValidator, ValidationMetrics
from paper.backtest import replay_signals
from paper.costs import PaperCostModel
from trading.contracts import (
    Capability,
    ExecutionContext,
    ExecutionMode,
    Instrument,
)
from trading.safety import AccountNature


class _FakeModel:
    def __init__(self, predictions: list[str]) -> None:
        self._predictions = predictions

    def predict(self, X):
        return self._predictions[: len(X)]


def _market_data(n: int, step: float = 1.0) -> pd.DataFrame:
    closes = [100 + i * step for i in range(n)]
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="h"),
        "open": closes,
        "high": [value + 1 for value in closes],
        "low": [value - 1 for value in closes],
        "close": closes,
    })


def _costs(
    spread: str = "0.01",
    commission: str = "0.001",
    slippage: str = "0.01",
) -> PaperCostModel:
    return PaperCostModel(
        Decimal(spread), Decimal(commission), Decimal(slippage)
    )


def _validate(predictions: list[str], market_data: pd.DataFrame, costs=None):
    features = pd.DataFrame({"feat1": [0.1] * len(predictions)})
    labels = pd.Series(["NEUTRO"] * len(predictions))
    return ModelValidator().validate(
        _FakeModel(predictions),
        features,
        labels,
        market_data,
        3,
        costs=costs or _costs(),
        initial_capital=Decimal("10000"),
    )


def test_check_approval_gates_are_unchanged():
    validator = ModelValidator()
    assert validator.check_approval(
        ValidationMetrics(profit_factor=1.3, win_rate=50, total_trades=10)
    )
    assert not validator.check_approval(
        ValidationMetrics(profit_factor=1.29, win_rate=49, total_trades=10)
    )
    assert validator.check_approval(
        ValidationMetrics(profit_factor=2.5, win_rate=70, total_trades=20)
    )


def test_paper_execution_costs_can_turn_a_winner_into_a_loss():
    market_data = _market_data(10, step=0.1)
    metrics = _validate(["LONG"] + ["NEUTRO"] * 9, market_data, _costs(
        spread="0.5", commission="0.001", slippage="0.5"
    ))
    assert metrics.total_trades == 1
    assert metrics.profit_factor == 0
    assert metrics.approved is False


def test_replay_prevents_overlapping_entries_and_caps_capital():
    frame = _market_data(15, step=0.1)
    result = replay_signals(
        frame,
        ["LONG"] * len(frame),
        forward_candles=4,
        spread_price=Decimal("0.001"),
        commission_rate=Decimal("0.0001"),
        slippage_price=Decimal("0.001"),
        initial_capital=Decimal("1000"),
        max_open_positions=1,
        max_position_pct=Decimal("10"),
    )
    trades = result["trades"]
    assert 0 < len(trades) < len(frame) - 4
    assert all(
        Decimal(str(trade["position_value"]))
        <= Decimal(str(trade["equity_at_entry"])) * Decimal("0.1")
        + Decimal("0.00000001")
        for trade in trades
    )
    for previous, following in zip(trades, trades[1:]):
        assert pd.Timestamp(following["fill_time"]) > pd.Timestamp(previous["exit_time"])


def test_replay_enforces_configured_concurrent_position_limit():
    frame = _market_data(15, step=0.1)
    result = replay_signals(
        frame,
        ["LONG"] * len(frame),
        forward_candles=5,
        spread_price=Decimal("0.001"),
        commission_rate=Decimal("0.0001"),
        slippage_price=Decimal("0.001"),
        initial_capital=Decimal("1000"),
        max_open_positions=2,
        max_position_pct=Decimal("60"),
    )
    assert result["max_concurrent_positions"] == 2
    assert result["max_concurrent_positions"] <= 2


def test_equity_drawdown_uses_bar_mark_to_market_not_sequential_trade_compounding():
    frame = _market_data(10, step=1)
    frame.loc[2:4, "close"] = [98, 96, 95]
    result = replay_signals(
        frame,
        ["LONG"] + ["NEUTRO"] * 9,
        forward_candles=4,
        spread_price=Decimal("0.001"),
        commission_rate=Decimal("0"),
        slippage_price=Decimal("0.001"),
        initial_capital=Decimal("10000"),
    )
    equity_curve = result["net_metrics_under_project_paper_costs"]["equity_curve"]
    assert len(equity_curve) == len(frame)
    assert min(equity_curve) < max(equity_curve)
    assert result["net_metrics_under_project_paper_costs"]["max_drawdown_pct"] > 0


def test_validation_rejects_missing_alignment():
    features = pd.DataFrame({"feat1": [0.1, 0.2]})
    with pytest.raises(ValueError, match="align"):
        ModelValidator().validate(
            _FakeModel(["LONG"]),
            features,
            pd.Series(["LONG", "NEUTRO"]),
            _market_data(2),
            1,
            costs=_costs(),
            initial_capital=Decimal("1000"),
        )


def test_purge_gap_excludes_rows_whose_labels_reach_test_period():
    train_positions = np.arange(100)
    purged = ModelTrainer._purge_training_positions(
        train_positions, test_start_position=100, forward_candles=5
    )
    assert purged[-1] == 94
    assert purged[-1] + 5 < 100


def test_scaler_is_fitted_only_on_fold_training_rows():
    train = pd.DataFrame({"RSI_14": [1.0, 3.0], "extra": [10.0, 30.0]})
    test = pd.DataFrame({"RSI_14": [1000.0], "extra": [3000.0]})
    scaled_train, scaled_test = ModelTrainer._scale_fold_data(train, test)
    assert scaled_train["RSI_14"].mean() == pytest.approx(0)
    assert scaled_test["RSI_14"].iloc[0] > 100
    assert scaled_test["extra"].iloc[0] == 3000.0


def test_walk_forward_fold_purges_label_horizon_before_test():
    n = 100
    timestamps = pd.Series(pd.date_range("2025-01-01", periods=n, freq="h"))
    X = pd.DataFrame({
        "RSI_14": np.linspace(0, 100, n),
        "other": np.sin(np.arange(n)),
    })
    y = pd.Series(np.where(np.arange(n) % 2, "LONG", "NEUTRO"))
    market_data = _market_data(n)
    folds = ModelTrainer()._walk_forward_cv(
        X,
        y,
        market_data,
        timestamps,
        costs=_costs(),
        initial_capital=Decimal("10000"),
        max_open_positions=2,
        risk_per_trade_pct=Decimal("1"),
        contract_size=Decimal("1"),
        volume_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100"),
        margin_rate=Decimal("1"),
        forward_candles=3,
        min_train_samples=30,
        min_test_samples=8,
    )
    assert folds
    assert folds[0].train_end + pd.Timedelta(hours=3) < folds[0].test_start


@pytest.mark.asyncio
async def test_ml_validation_loads_configured_account_and_instrument_limits(monkeypatch):
    from config import settings

    context = ExecutionContext(
        venue="paper-test",
        broker="simulator",
        account_id="ml-validation-test",
        nature=AccountNature.SIMULATED,
        verified=True,
        mode=ExecutionMode.PAPER,
        market="FOREX",
        account_currency="USD",
        position_model="hedging",
        instrument=Instrument(
            symbol="EURUSD",
            asset_class="forex",
            base_currency="EUR",
            quote_currency="USD",
            contract_size=Decimal("100000"),
            tick_size=Decimal("0.00001"),
            tick_value=Decimal("1"),
            tick_currency="USD",
            min_volume=Decimal("0.01"),
            max_volume=Decimal("5"),
            volume_step=Decimal("0.01"),
            price_precision=5,
            volume_precision=2,
            margin_model="forex",
            margin_rate=Decimal("0.02"),
            min_notional=Decimal("1"),
        ),
        capabilities=frozenset(Capability),
    )
    monkeypatch.setattr(settings, "paper_initial_capital", Decimal("20000"))
    monkeypatch.setattr(
        "paper.costs.paper_costs_for",
        lambda symbol: _costs(),
    )
    trainer = ModelTrainer(
        validation_config={
            "market": "FOREX",
            "mode": "paper",
            "enabled_symbols": ["EURUSD"],
            "risk_per_trade_pct": Decimal("0.75"),
            "max_open_trades": 4,
            "execution_context": {"EURUSD": context.snapshot()},
        }
    )

    parameters = await trainer._paper_validation_parameters("FOREX", "EURUSD")
    assert parameters["contract_size"] == Decimal("100000")
    assert parameters["volume_step"] == Decimal("0.01")
    assert parameters["max_quantity"] == Decimal("5")
    assert parameters["max_open_positions"] == 4
    assert parameters["risk_per_trade_pct"] == Decimal("0.75")
    assert parameters["margin_rate"] == Decimal("0.02")
