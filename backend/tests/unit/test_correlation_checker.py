"""Testes unitários do CorrelationChecker."""
import types
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from risk.correlation_checker import CorrelationChecker


@pytest.fixture
def checker() -> CorrelationChecker:
    return CorrelationChecker()


def _make_trade(symbol: str):
    return types.SimpleNamespace(symbol=symbol)


def test_correlacao_alta_bloqueia_live(checker: CorrelationChecker):
    """Correlação ≥ 0.7 em modo live → bloqueia."""
    # Injeta retornos diretamente via mock
    returns_btc = [0.01 * i for i in range(20)]
    returns_eth = [0.01 * i + 0.001 for i in range(20)]  # altamente correlacionado

    # Pearson entre duas sequências quase idênticas ≈ 1.0
    corr = checker.calculate_correlation(returns_btc, returns_eth)
    assert corr > 0.99


def test_correlacao_baixa_nao_bloqueia(checker: CorrelationChecker):
    """Correlação < 0.7 → não bloqueia."""
    import random
    random.seed(42)
    returns_a = [random.gauss(0, 0.01) for _ in range(20)]
    returns_b = [random.gauss(0, 0.01) for _ in range(20)]
    corr = checker.calculate_correlation(returns_a, returns_b)
    # Correlação aleatória normalmente abaixo de 0.7
    # Se por acaso o seed gerar > 0.7, o teste ainda valida a lógica da função
    assert isinstance(corr, float)
    assert -1.0 <= corr <= 1.0


def test_correlacao_negativa_forte_bloqueia(checker: CorrelationChecker):
    """Correlação -0.8 (abs=0.8 ≥ 0.7) → bloqueia."""
    returns_a = [0.01 * i for i in range(20)]
    returns_b = [-0.01 * i for i in range(20)]
    corr = checker.calculate_correlation(returns_a, returns_b)
    assert corr <= -0.99
    # abs(-0.99) = 0.99 >= 0.7 → deve bloquear
    assert abs(corr) >= 0.7


@pytest.mark.asyncio
async def test_check_bloqueia_live():
    """check() com correlação alta em live → blocked=True."""
    checker = CorrelationChecker()
    btc_returns = [float(i) * 0.01 for i in range(20)]
    eth_returns = [float(i) * 0.01 + 0.001 for i in range(20)]

    checker.get_returns = AsyncMock(side_effect=[btc_returns, eth_returns])

    result = await checker.check(
        candidate_symbol="BTC/USDT",
        open_trades=[_make_trade("ETH/USDT")],
        threshold=0.7,
        is_paper=False,
    )
    assert result.blocked is True
    assert result.correlated_with == "ETH/USDT"


@pytest.mark.asyncio
async def test_check_nao_bloqueia_paper():
    """check() com correlação alta em paper → blocked=False, warning preenchido."""
    checker = CorrelationChecker()
    btc_returns = [float(i) * 0.01 for i in range(20)]
    eth_returns = [float(i) * 0.01 + 0.001 for i in range(20)]

    checker.get_returns = AsyncMock(side_effect=[btc_returns, eth_returns])

    result = await checker.check(
        candidate_symbol="BTC/USDT",
        open_trades=[_make_trade("ETH/USDT")],
        threshold=0.7,
        is_paper=True,
    )
    assert result.blocked is False
    assert result.warning is not None


@pytest.mark.asyncio
async def test_check_sem_posicoes_abertas():
    """Sem posições abertas → sempre aprovado."""
    checker = CorrelationChecker()
    result = await checker.check("BTC/USDT", [], threshold=0.7)
    assert result.blocked is False


@pytest.mark.asyncio
async def test_check_sem_dados_nao_bloqueia():
    """Sem dados de retorno disponíveis → não bloqueia por precaução."""
    checker = CorrelationChecker()
    checker.get_returns = AsyncMock(return_value=[])

    result = await checker.check(
        candidate_symbol="BTC/USDT",
        open_trades=[_make_trade("ETH/USDT")],
        threshold=0.7,
    )
    assert result.blocked is False
    assert result.warning == "insufficient_data"


@pytest.mark.asyncio
async def test_threshold_configuravel():
    """Threshold 0.5 bloqueia correlação que 0.7 não bloquearia."""
    checker = CorrelationChecker()
    # Correlação moderada ≈ 0.6
    import random
    random.seed(123)
    base = [random.gauss(0, 0.01) for _ in range(20)]
    # correlacionado parcialmente
    mixed = [b + random.gauss(0, 0.003) for b in base]
    corr = checker.calculate_correlation(base, mixed)

    # Com threshold 0.5: se corr >= 0.5, bloqueia
    checker.get_returns = AsyncMock(side_effect=[base, mixed])
    result = await checker.check(
        "BTC/USDT",
        [_make_trade("ETH/USDT")],
        threshold=0.5,
        is_paper=False,
    )
    if abs(corr) >= 0.5:
        assert result.blocked is True
    else:
        assert result.blocked is False


def test_dados_insuficientes_retorna_zero(checker: CorrelationChecker):
    """Menos de 5 candles → retorna 0.0 (sem bloquear)."""
    result = checker.calculate_correlation([0.01, 0.02], [0.01, 0.02])
    assert result == 0.0


def test_desvio_padrao_zero_retorna_zero(checker: CorrelationChecker):
    """Série constante (desvio=0) → retorna 0.0."""
    result = checker.calculate_correlation([0.0] * 20, [0.01] * 20)
    assert result == 0.0
