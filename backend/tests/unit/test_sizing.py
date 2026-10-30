from decimal import Decimal

import pytest

from risk.position_sizer import PositionSizer, PositionSizingError


def test_sizing_respects_step_and_limits():
    result = PositionSizer().calculate(
        capital=Decimal("10000"),
        risk_pct=Decimal("1"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("95"),
        step_size=Decimal("0.03"),
        min_quantity=Decimal("0.06"),
        max_quantity=Decimal("2.40"),
    )

    assert result.quantity == Decimal("2.40")
    assert result.quantity % Decimal("0.03") == Decimal("0")
    assert Decimal("0.06") <= result.quantity <= Decimal("2.40")


def test_sizing_blocks_quantity_below_minimum():
    with pytest.raises(PositionSizingError, match="abaixo do mínimo"):
        PositionSizer().calculate(
            capital=Decimal("100"),
            risk_pct=Decimal("0.1"),
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("45000"),
            step_size=Decimal("0.001"),
            min_quantity=Decimal("0.01"),
            max_quantity=Decimal("1"),
        )


def test_sizing_limits_quantity_above_maximum():
    result = PositionSizer().calculate(
        capital=Decimal("50000"),
        risk_pct=Decimal("5"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("90"),
        step_size=Decimal("0.01"),
        min_quantity=Decimal("0.01"),
        max_quantity=Decimal("3.33"),
        leverage=Decimal("5"),
    )

    assert result.quantity == Decimal("3.33")
    assert result.position_value == Decimal("333.00")


def test_sizing_uses_risk_and_leverage_in_cap():
    low_leverage = PositionSizer().calculate(
        capital=Decimal("1000"),
        risk_pct=Decimal("50"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("99"),
        step_size=Decimal("0.01"),
        leverage=Decimal("1"),
    )
    high_leverage = PositionSizer().calculate(
        capital=Decimal("1000"),
        risk_pct=Decimal("50"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("99"),
        step_size=Decimal("0.01"),
        leverage=Decimal("3"),
    )

    assert low_leverage.risk_amount == Decimal("500")
    assert high_leverage.risk_amount == Decimal("500")
    assert low_leverage.quantity == Decimal("1.00")
    assert high_leverage.quantity == Decimal("3.00")


def test_sizing_uses_contract_size_for_forex():
    # EURUSD: 1 lote = 100.000 EUR. Capital 10.000, risco 1%, stop 50 pips.
    result = PositionSizer().calculate(
        capital=Decimal("10000"),
        risk_pct=Decimal("1"),
        entry_price=Decimal("1.10000"),
        stop_loss_price=Decimal("1.09500"),
        step_size=Decimal("0.01"),
        leverage=Decimal("30"),
        contract_size=Decimal("100000"),
    )

    # risco em dinheiro = 100; risco por lote = (0.005/1.10) * 1.10 * 100000 = 500
    # qty = 0.20 lotes; notional = 0.20 * 1.10 * 100000 = 22.000
    assert result.quantity == Decimal("0.20")
    assert result.position_value == Decimal("22000.00")


def test_sizing_never_exceeds_notional_with_contract_size():
    # cap de 10% do capital = 1.000 de notional; 1 lote = 110.000.
    # Nenhum lote inteiro cabe no cap: deve rejeitar, não superdimensionar.
    with pytest.raises(PositionSizingError, match="zero"):
        PositionSizer().calculate(
            capital=Decimal("10000"),
            risk_pct=Decimal("5"),
            entry_price=Decimal("1.10000"),
            stop_loss_price=Decimal("1.09900"),
            step_size=Decimal("0.01"),
            leverage=Decimal("1"),
            contract_size=Decimal("100000"),
        )


async def test_risk_manager_blocks_invalid_sizing(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from risk.manager import RiskManager
    from trading import equity
    monkeypatch.setattr(equity, "paper_available_capital", AsyncMock(return_value=Decimal("10000")))

    db = AsyncMock()
    config = SimpleNamespace(
        is_active=True, drawdown_status="OK", max_open_trades=3, mode="paper",
        max_corr_threshold=Decimal("0.7"), risk_per_trade_pct=Decimal("1"),
        execution_context={},
    )
    cfg_result, trades_result = MagicMock(), MagicMock()
    cfg_result.scalar_one_or_none.return_value = config
    trades_result.scalars.return_value.all.return_value = []
    db.execute.side_effect = [cfg_result, trades_result]
    sizer = MagicMock()
    sizer.get_available_capital.return_value = Decimal("10000")
    sizer.calculate.side_effect = PositionSizingError("Quantidade inválida")
    correlation = AsyncMock()
    correlation.check.return_value = SimpleNamespace(blocked=False)

    result = await RiskManager(db, sizer, AsyncMock(), correlation).can_open_position(
        "CRIPTO", "TEST", Decimal("100"), Decimal("99"))

    assert result.approved is False
    assert result.reason == "Quantidade inválida"


async def test_risk_manager_fails_closed_without_capital(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from risk.manager import RiskManager
    from trading import equity
    monkeypatch.setattr(equity, "paper_available_capital", AsyncMock(return_value=Decimal("0")))

    db = AsyncMock()
    config = SimpleNamespace(
        is_active=True, drawdown_status="OK", max_open_trades=3, mode="paper",
        max_corr_threshold=Decimal("0.7"), risk_per_trade_pct=Decimal("1"),
        execution_context={}, account_currency="USDT",
    )
    cfg_result, trades_result = MagicMock(), MagicMock()
    cfg_result.scalar_one_or_none.return_value = config
    trades_result.scalars.return_value.all.return_value = []
    db.execute.side_effect = [cfg_result, trades_result]
    sizer = MagicMock()
    sizer.get_available_capital.return_value = Decimal("0")
    correlation = AsyncMock()
    correlation.check.return_value = SimpleNamespace(blocked=False)

    result = await RiskManager(db, sizer, AsyncMock(), correlation).can_open_position(
        "CRIPTO", "TEST", Decimal("100"), Decimal("99"))

    assert result.approved is False
    assert "Capital indisponível" in result.reason
    sizer.calculate.assert_not_called()
