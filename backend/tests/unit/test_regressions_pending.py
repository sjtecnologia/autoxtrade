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
from trading.connectors.fake_transports import TemporaryDWXTransport
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.contracts import (Capability, CapabilityUnavailable, ExecutionContext,
                               ExecutionMode, ExecutionResult, Instrument, OrderIntent,
                               ResultState)
from trading.safety import AccountNature, ExposureBlocked


def _context():
    return ExecutionContext(
        venue="binance_spot", broker="simulator", account_id="regression-test",
        nature=AccountNature.SIMULATED, verified=True, mode=ExecutionMode.PAPER,
        market="CRIPTO", account_currency="USDT", position_model="hedging",
        instrument=Instrument(
            symbol="TEST", asset_class="crypto", base_currency="TEST", quote_currency="USDT",
            contract_size=D("1"), tick_size=D("1"), tick_value=D("1"), tick_currency="USDT",
            min_volume=D("0.001"), max_volume=D("100"), volume_step=D("0.001"),
            price_precision=0, volume_precision=3, margin_model="cash", margin_rate=D("1"),
            min_notional=D("1"),
        ), capabilities=frozenset(Capability),
    )


def make_trade():
    return Trade(id=1, market="FOREX", exchange="fake", symbol="TEST", side="buy",
                 mode="live", status="open", quantity=D("1"), entry_price=D("100"),
                 entry_value=D("100"), stop_loss=D("95"), open_at=datetime.now(timezone.utc))


async def test_failed_protection_cannot_claim_close_without_execution():
    from trading.order_executor import OrderExecutor, OrderExecutionError
    context = _context()
    transport = MemoryTransport(context)
    transport.prices[context.instrument.symbol] = D("100")
    conn = SimulatedConnector(context, transport=transport)
    conn.modify_position_protection = AsyncMock(return_value=ExecutionResult(
        "protection", ResultState.REJECTED, reason="simulated rejection"))
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    executor = OrderExecutor(conn, db)
    with pytest.raises(OrderExecutionError, match="Protection not confirmed"):
        await executor.open_position("TEST", OrderSide.BUY, D("1"), D("100"), D("95"))
    trade = next(call.args[0] for call in db.add.call_args_list
                 if getattr(call.args[0], "status", None) == "open")
    assert trade.status == "open", "No confirmed protection; preserve unresolved exposure"


async def test_dwx_protection_must_not_open_opposite_order(tmp_path):
    from trading.connectors.dwx import DWXConnector
    context = _context()
    transport = TemporaryDWXTransport(context)
    transport.prices[context.instrument.symbol] = D("100")
    conn = DWXConnector(str(tmp_path), context=context, transport=transport)
    entry = await conn.create_order(OrderIntent("entry", context, OrderSide.BUY, OrderType.MARKET, D("1")))
    conn.place_order = AsyncMock(side_effect=AssertionError("opposite order fallback is forbidden"))

    result = await conn.modify_position_protection(
        entry.position_id, stop_loss=D("95"), take_profit=D("110"), intent_id="protect")

    assert result.state is ResultState.CONFIRMED
    assert result.position_id == entry.position_id
    conn.place_order.assert_not_awaited()


async def test_risk_must_use_sizer_and_drawdown():
    from risk.manager import RiskManager
    db = AsyncMock()
    config = SimpleNamespace(is_active=True, drawdown_status="OK", max_open_trades=3,
                             mode="paper", max_corr_threshold=D("0.7"),
                             risk_per_trade_pct=D("1"), execution_context={})
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


def test_predictor_rejeita_modelo_fora_do_diretorio_confiavel(tmp_path):
    from ml.predictor import ModelNotFoundError, _trusted_model_path

    with pytest.raises(ModelNotFoundError, match="fora do diretório confiável"):
        _trusted_model_path(tmp_path / "external.pkl")


def test_factory_binance_constructor_contract():
    from trading.connectors.factory import get_connector
    from trading.connectors.simulated import SimulatedConnector
    with pytest.raises(CapabilityUnavailable):
        get_connector("CRIPTO", force_new=True)
    assert isinstance(get_connector(_context(), force_new=True), SimulatedConnector)
