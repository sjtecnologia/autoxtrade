"""Executable acceptance criteria. Strict xfail tracks unresolved stage defects.

Run with --runxfail to reproduce the failures without expected-failure masking.
"""
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.models import Trade
from trading.connectors.types import OrderSide, OrderType
from trading.safety import AccountNature


def make_trade():
    return Trade(id=1, market="FOREX", exchange="fake", symbol="TEST", side="buy",
                 mode="live", status="open", quantity=D("1"), entry_price=D("100"),
                 entry_value=D("100"), stop_loss=D("95"), open_at=datetime.now(timezone.utc))


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F02 / etapa 02: fallback fecha somente no banco")
async def test_failed_protection_cannot_claim_close_without_execution():
    from trading.order_executor import OrderExecutor
    conn = AsyncMock()
    conn.account_nature = AccountNature.SIMULATED
    conn.place_order.side_effect = RuntimeError("simulated rejection")
    conn.get_current_price.return_value = D("100")
    db = AsyncMock()
    db.add = MagicMock()
    executor = OrderExecutor(conn, db)
    executor._telegram = AsyncMock()
    trade = make_trade()
    await executor._send_stop_market_or_close(trade, "TEST", OrderSide.SELL, D("1"), D("95"))
    assert trade.status == "open", "No confirmed closing order; preserve unresolved exposure"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F03 / etapa 03: OCO DWX abre ordem oposta")
async def test_dwx_protection_must_not_open_opposite_order(tmp_path):
    from trading.connectors.dwx import DWXConnector
    conn = DWXConnector(str(tmp_path))
    # Explicit fake transport/account allows exercising the quarantined algorithm.
    conn.account_nature = AccountNature.SIMULATED
    conn.place_order = AsyncMock()
    await conn.place_oco_order("TEST", OrderSide.SELL, D("1"), D("110"), D("95"), D("94"))
    conn.place_order.assert_not_awaited()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F04 / etapa 04: sizing fixo, sizer não chamado")
async def test_risk_must_use_sizer_and_drawdown():
    from risk.manager import RiskManager
    db = AsyncMock()
    config = SimpleNamespace(is_active=True, drawdown_status="OK", max_open_trades=3,
                             mode="paper", max_corr_threshold=D("0.7"))
    cfg_result, trades_result = MagicMock(), MagicMock()
    cfg_result.scalar_one_or_none.return_value = config
    trades_result.scalars.return_value.all.return_value = []
    db.execute.side_effect = [cfg_result, trades_result]
    sizer, drawdown, correlation = MagicMock(), AsyncMock(), AsyncMock()
    sizing = SimpleNamespace(quantity=D("0.25"), risk_amount=D("5"), risk_pct=D("1"))
    sizer.calculate.return_value = sizing
    correlation.check.return_value = SimpleNamespace(blocked=False)
    result = await RiskManager(db, sizer, drawdown, correlation).can_open_position(
        "FOREX", "TEST", D("100"), D("80"))
    assert result.position_size.quantity == sizing.quantity
    sizer.calculate.assert_called_once()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="F05 / etapa 06: DiDi aprova folds reprovados")
async def test_didi_rejects_bad_validation_metrics(monkeypatch):
    import pandas as pd
    from ml.trainer import ModelTrainer
    from ml.data_collector import DataCollector
    from ml.feature_engineer import FeatureEngineer
    df = pd.DataFrame({"timestamp": pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC"),
                       "close": [100.] * 100, "target": [0, 1] * 50})
    monkeypatch.setattr(DataCollector, "load_parquet", lambda *a: df.copy())
    monkeypatch.setattr(FeatureEngineer, "calculate_features", lambda self, data: data)
    trainer = ModelTrainer()
    monkeypatch.setattr(trainer, "_calculate_didi_target", lambda data: data)
    bad_fold = SimpleNamespace(profit_factor=0., win_rate=0., expectancy_pct=-5.,
                               max_drawdown_pct=90., total_trades=0)
    monkeypatch.setattr(trainer, "_walk_forward_cv", lambda *a, **kw: [bad_fold])
    monkeypatch.setattr(trainer, "_train_final_model", lambda *a: (None, None))
    monkeypatch.setattr(trainer, "_save_model_files", lambda *a: ("fake-model", "fake-scaler"))
    result = await trainer.train("TEST/USDT", profile="didi")
    assert result.approved is False, "Performance gates must reject losing/empty validation"


def test_factory_binance_constructor_contract():
    from trading.connectors.factory import get_connector
    from trading.connectors.simulated import SimulatedConnector
    # Previously raised TypeError; safe default now creates a credential-free adapter.
    assert isinstance(get_connector("CRIPTO", force_new=True), SimulatedConnector)
