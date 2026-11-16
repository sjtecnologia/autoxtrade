from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pandas as pd
import pytest

from ml.trainer import ModelTrainer, TrainingResult
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


@pytest.mark.parametrize("field,value", [
    ("profit_factor", float("inf")), ("profit_factor", float("nan")),
    ("win_rate", float("nan")), ("expectancy_pct", float("nan")),
    ("max_drawdown_pct", float("nan")),
    ("total_trades", float("nan")), ("open_positions", float("nan")),
])
def test_validator_rejects_nonfinite_metrics(field, value):
    metrics = ValidationMetrics(profit_factor=2, win_rate=60, total_trades=10)
    setattr(metrics, field, value)
    assert not ModelValidator().check_approval(metrics)


@pytest.mark.parametrize("factors,expected,reason", [
    ([float("inf"), 0.1], False, "não finita"),
    ([float("inf"), float("inf")], False, "não finita"),
    ([float("nan"), 2.0], False, "não finita"),
    ([1e308, 1e308], False, "agregada não finita"),
    ([-1.0, 3.0], False, "Profit Factor negativo"),
    ([0.0, 0.0], False, "Trades insuficientes"),
    ([1.5, 1.7], True, None),
    ([0.1, 0.1, 6.0], False, "Inconsistência"),
])
@pytest.mark.asyncio
async def test_training_gates_fold_profit_factor(
    monkeypatch, factors, expected, reason,
):
    from ml.data_collector import DataCollector
    from ml.feature_engineer import FeatureEngineer

    frame = _market_data(200)
    frame["target"] = [0, 1] * 100
    monkeypatch.setattr(DataCollector, "load_parquet", lambda *args: frame.copy())
    monkeypatch.setattr(FeatureEngineer, "calculate_features", lambda self, data: data)
    trainer = ModelTrainer()
    monkeypatch.setattr(trainer, "_calculate_didi_target", lambda data: data)
    monkeypatch.setattr(trainer, "_paper_validation_parameters", AsyncMock(return_value={}))
    folds = [
        SimpleNamespace(
            fold=i, profit_factor=pf, win_rate=80.0,
            expectancy_pct=1.0, max_drawdown_pct=1.0,
            total_trades=0 if pf == 0 else 100, open_positions=0,
        )
        for i, pf in enumerate(factors)
    ]
    monkeypatch.setattr(trainer, "_walk_forward_cv", lambda *args, **kwargs: folds)
    train_model = Mock(return_value=(None, None))
    monkeypatch.setattr(trainer, "_train_final_model", train_model)
    monkeypatch.setattr(trainer, "_save_model_files", lambda *args: ("model", "scaler"))
    result = await trainer.train("TEST", profile="didi")
    assert result.approved is expected
    assert result.fold_metrics == folds
    if any(not np.isfinite(pf) for pf in factors):
        assert result.avg_profit_factor is None
    if reason:
        assert reason in result.rejection_reason
        train_model.assert_not_called()
    else:
        train_model.assert_called_once()


@pytest.mark.parametrize("field", ["win_rate", "expectancy_pct", "max_drawdown_pct", "total_trades"])
@pytest.mark.asyncio
async def test_training_rejects_nan_in_other_fold_metrics(monkeypatch, field):
    from ml.data_collector import DataCollector
    from ml.feature_engineer import FeatureEngineer

    frame = _market_data(200)
    frame["target"] = [0, 1] * 100
    monkeypatch.setattr(DataCollector, "load_parquet", lambda *args: frame.copy())
    monkeypatch.setattr(FeatureEngineer, "calculate_features", lambda self, data: data)
    trainer = ModelTrainer()
    monkeypatch.setattr(trainer, "_calculate_didi_target", lambda data: data)
    monkeypatch.setattr(trainer, "_paper_validation_parameters", AsyncMock(return_value={}))
    fold = SimpleNamespace(
        fold=0, profit_factor=2.0, win_rate=80.0, expectancy_pct=1.0,
        max_drawdown_pct=1.0, total_trades=100, open_positions=0,
    )
    setattr(fold, field, float("nan"))
    monkeypatch.setattr(trainer, "_walk_forward_cv", lambda *args, **kwargs: [fold])
    train_model = Mock(return_value=(None, None))
    monkeypatch.setattr(trainer, "_train_final_model", train_model)
    monkeypatch.setattr(trainer, "_save_model_files", lambda *args: ("model", "scaler"))
    result = await trainer.train("TEST", profile="didi")
    assert not result.approved
    assert ("inválida" if field == "total_trades" else "não finita") in result.rejection_reason
    train_model.assert_not_called()


def test_no_loss_replay_preserves_infinite_profit_factor_but_cannot_approve():
    frame = _market_data(10, step=0.1)
    frame.loc[4, "high"] = 110
    metrics = _validate(["LONG"] + ["NEUTRO"] * 9, frame, _costs(
        spread="0", commission="0", slippage="0",
    ))
    assert metrics.total_trades == 1
    assert metrics.profit_factor == float("inf")
    assert not metrics.approved


@pytest.mark.parametrize("profit_factor", [None, float("inf"), float("nan")])
@pytest.mark.asyncio
async def test_nonfinite_profit_factor_is_not_replaced_in_persistence(profit_factor):
    db = Mock()
    result = TrainingResult(
        approved=True, version="test", symbol="TEST", timeframe="1h",
        avg_profit_factor=profit_factor,
    )
    with pytest.raises(ValueError, match="não finito"):
        await ModelTrainer(db=db)._persist_to_db(result, object(), [], "CRIPTO")
    db.add.assert_not_called()


def test_paper_execution_costs_can_turn_a_winner_into_a_loss():
    market_data = _market_data(10, step=0.1)
    market_data.loc[4, "high"] = 110
    before_costs = _validate(["LONG"] + ["NEUTRO"] * 9, market_data, _costs(
        spread="0", commission="0", slippage="0"
    ))
    metrics = _validate(["LONG"] + ["NEUTRO"] * 9, market_data, _costs(
        spread="3", commission="0.001", slippage="0.5"
    ))
    assert before_costs.total_trades == 1
    assert before_costs.win_rate == 100
    assert metrics.total_trades == 1
    assert metrics.profit_factor == 0
    assert metrics.approved is False


def test_replay_prevents_overlapping_entries_and_caps_capital():
    frame = _market_data(15, step=1)
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


def test_replay_reserves_full_costs_with_leverage():
    frame = _paper_case()
    frame.loc[3, "low"] = 97
    result = replay_signals(
        frame, ["LONG", "LONG"] + ["NEUTRO"] * 6,
        forward_candles=5, spread_price=Decimal("0"),
        commission_rate=Decimal("0.1"), slippage_price=Decimal("0"),
        initial_capital=Decimal("1000"), max_open_positions=2,
        risk_per_trade_pct=Decimal("100"), max_position_pct=Decimal("100"),
        leverage=Decimal("2"), margin_rate=Decimal("0.5"),
    )
    assert result["trades"]
    committed = sum(
        Decimal(str(trade["quantity"])) * Decimal("70")
        for trade in result["trades"]
    )
    assert committed <= Decimal("1000")


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


def _paper_case():
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=8, freq="h"),
        "open": [100.0] * 8, "high": [101.0] * 8,
        "low": [99.0] * 8, "close": [100.0] * 8, "atr": [2.0] * 8,
    })


def _paper_replay(frame, *, horizon=5, **kwargs):
    return replay_signals(
        frame, ["LONG"] + ["NEUTRO"] * (len(frame) - 1),
        forward_candles=horizon, spread_price=Decimal("0"),
        commission_rate=Decimal("0"), slippage_price=Decimal("0"),
        initial_capital=Decimal("10000"), **kwargs,
    )


@pytest.mark.parametrize(
    "bar,reason,price",
    [
        ({"low": 97}, "SL_HIT", 97.6),
        ({"high": 105}, "TP_HIT", 104.8),
        ({"low": 97, "high": 105}, "SL_HIT", 97.6),
        ({"open": 95, "low": 94}, "SL_HIT", 95),
        ({"open": 106, "high": 107}, "TP_HIT", 106),
    ],
)
def test_paper_replay_stop_target_intrabar_and_gap(bar, reason, price):
    frame = _paper_case()
    for key, value in bar.items():
        frame.loc[2, key] = value
    result = _paper_replay(frame)
    trade = result["trades"][0]
    assert trade["reason"] == reason
    assert trade["exit"] == pytest.approx(price)
    assert trade["exit_time"] == str(frame.loc[2, "timestamp"])
    assert trade["fill_time"] == str(frame.loc[1, "timestamp"])
    assert result["open_positions"] == 0


def test_paper_safe_break_applies_from_next_candle():
    frame = _paper_case()
    frame.loc[2, ["high", "low", "close"]] = [104, 99, 103]
    frame.loc[3, "low"] = 99
    result = _paper_replay(frame)
    trade = result["trades"][0]
    assert trade["safe_break_armed"] is True
    assert trade["reason"] == "SL_HIT"
    assert trade["exit"] == pytest.approx(100)
    assert trade["exit_time"] == str(frame.loc[3, "timestamp"])


def test_short_paper_gap_and_stop_target_priority():
    frame = _paper_case()
    frame.loc[2, ["open", "high", "low"]] = [110, 111, 95]
    result = replay_signals(
        frame, ["SHORT"] + ["NEUTRO"] * 7, forward_candles=5,
        spread_price=Decimal("0"), commission_rate=Decimal("0"),
        slippage_price=Decimal("0"), initial_capital=Decimal("10000"),
    )
    assert result["trades"][0]["reason"] == "SL_HIT"
    assert result["trades"][0]["exit"] == pytest.approx(110)


def test_label_horizon_does_not_close_a_position():
    frame = _paper_case()
    for horizon in (1, 5, 20):
        result = _paper_replay(frame, horizon=horizon)
        assert result["total_trades"] == 0
        assert result["open_positions"] == 1
    metrics = _validate(["LONG"] + ["NEUTRO"] * 7, frame)
    assert metrics.open_positions == 1
    assert metrics.approved is False


def test_paper_strategy_exit_follows_configuration(monkeypatch):
    from types import SimpleNamespace
    from paper import backtest

    frame = pd.concat([_paper_case()] * 10, ignore_index=True)
    frame["timestamp"] = pd.date_range("2025-01-01", periods=len(frame), freq="h")
    frame["volume"] = 1.0
    seen = []

    def strategy(window, side):
        seen.append(len(window))
        return SimpleNamespace(should_exit=len(window) == 4)

    monkeypatch.setattr(backtest, "evaluate_exit", strategy)
    enabled = _paper_replay(frame, auto_close_on_exit=True)
    disabled = _paper_replay(frame, auto_close_on_exit=False)
    assert enabled["trades"][0]["reason"] == "DIDI_EXIT"
    assert enabled["trades"][0]["exit_time"] == str(frame.loc[3, "timestamp"])
    assert disabled["total_trades"] == 0
    assert disabled["open_positions"] == 1
    assert seen == [2, 3, 4]


def test_strategy_validation_requires_prior_history_and_volume(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "didi_exit_auto_close", True)
    with pytest.raises(ValueError, match="histórico OHLCV"):
        _validate(["LONG"] + ["NEUTRO"] * 7, _paper_case())


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
