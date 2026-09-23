"""Testes unitários do PositionSizer."""
from decimal import Decimal

import pytest

from risk.position_sizer import PositionSizer, PositionSizingError


@pytest.fixture
def sizer() -> PositionSizer:
    return PositionSizer()


def test_calculo_basico(sizer: PositionSizer):
    """
    capital=10000, risk_pct=1%, entry=287450, stop=282900, step=0.001
    stop_distance = (287450-282900)/287450 ≈ 1.583%
    risk_amount = 100
    qty_raw ≈ 0.02197 BTC
    → cap 10% do capital: floor(1000/287450/0.001)*0.001 = 0.003 BTC
    """
    result = sizer.calculate(
        capital=Decimal("10000"),
        risk_pct=Decimal("1"),
        entry_price=Decimal("287450"),
        stop_loss_price=Decimal("282900"),
        step_size=Decimal("0.001"),
    )
    # cap 10%: max = 10000*0.10=1000; 1000/287450 ≈ 0.003479 → floor para 0.003
    assert result.quantity == Decimal("0.003")
    assert result.risk_amount == Decimal("100")
    assert result.position_value == Decimal("287450") * Decimal("0.003")


def test_arredondamento_step_size(sizer: PositionSizer):
    """Garante que qty é arredondado para BAIXO no step size."""
    result = sizer.calculate(
        capital=Decimal("5000"),
        risk_pct=Decimal("1"),
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("48000"),
        step_size=Decimal("0.001"),
    )
    # qty raw = 50 / (50000 * 0.04) = 0.025 → arredondado para 0.025 (múltiplo de 0.001)
    assert result.quantity % Decimal("0.001") == Decimal("0")


def test_rejeita_capital_zero(sizer: PositionSizer):
    with pytest.raises(PositionSizingError, match="Capital"):
        sizer.calculate(
            capital=Decimal("0"),
            risk_pct=Decimal("1"),
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("48000"),
            step_size=Decimal("0.001"),
        )


def test_limite_10pct_capital(sizer: PositionSizer):
    """Posição não pode exceder 10% do capital."""
    # capital=1000, risk=50%, entry=100, stop=99 → qty_raw enorme → capeado
    result = sizer.calculate(
        capital=Decimal("1000"),
        risk_pct=Decimal("50"),
        entry_price=Decimal("100"),
        stop_loss_price=Decimal("99"),
        step_size=Decimal("0.01"),
    )
    max_value = Decimal("1000") * Decimal("0.10")
    assert result.position_value <= max_value


def test_stop_muito_proximo_usa_minimo(sizer: PositionSizer):
    """Stop distance < 0.1% → usa 0.1% e aplica cap de 10%."""
    result = sizer.calculate(
        capital=Decimal("10000"),
        risk_pct=Decimal("1"),
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49990"),  # 0.02% de distância
        step_size=Decimal("0.001"),
    )
    # deve retornar resultado válido (sem exceção)
    assert result.quantity > Decimal("0")
    assert result.position_value <= Decimal("10000") * Decimal("0.10")


def test_rejeita_step_size_zero(sizer: PositionSizer):
    with pytest.raises(PositionSizingError, match="step size"):
        sizer.calculate(
            capital=Decimal("10000"),
            risk_pct=Decimal("1"),
            entry_price=Decimal("50000"),
            stop_loss_price=Decimal("48000"),
            step_size=Decimal("0"),
        )


def test_resultado_consistente(sizer: PositionSizer):
    """risk_amount = quantity * entry * stop_distance (aproximado)."""
    result = sizer.calculate(
        capital=Decimal("20000"),
        risk_pct=Decimal("2"),
        entry_price=Decimal("3000"),
        stop_loss_price=Decimal("2850"),
        step_size=Decimal("0.0001"),
    )
    # 2% de 20000 = 400; stop = 5%
    assert result.risk_amount == Decimal("400")
    assert result.stop_distance_pct > Decimal("0")
    assert result.position_value > Decimal("0")
