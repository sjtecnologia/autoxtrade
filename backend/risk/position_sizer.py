"""Position Sizing dinâmico baseado em risco fixo por trade."""
import json
import logging
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)

# Step sizes padrão por símbolo (fallback quando exchange não responde)
_DEFAULT_STEP_SIZES: dict[str, Decimal] = {
    "BTC/USDT": Decimal("0.00001"),
    "ETH/USDT": Decimal("0.0001"),
    "BNB/USDT": Decimal("0.001"),
}
_STEP_CACHE_TTL = 3600  # 1 hora


@dataclass
class PositionSizeResult:
    quantity: Decimal
    risk_amount: Decimal
    risk_pct: Decimal
    entry_price: Decimal
    stop_loss_price: Decimal
    stop_distance_pct: Decimal
    position_value: Decimal


class PositionSizingError(ValueError):
    """Erro de validação no cálculo de position size."""


class PositionSizer:
    def __init__(self, connector=None) -> None:
        """
        Args:
            connector: instância de BaseConnector (opcional — usado para buscar step size real).
        """
        self._connector = connector
        self._redis: aioredis.Redis | None = None

    async def startup(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def shutdown(self) -> None:
        if self._redis:
            await self._redis.aclose()

    def calculate(
        self,
        capital: Decimal,
        risk_pct: Decimal,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        step_size: Decimal,
    ) -> PositionSizeResult:
        """
        Calcula o tamanho da posição com base no risco fixo por trade.

        Fórmula:
            stop_distance = |entry - stop| / entry
            risk_amount   = capital * (risk_pct / 100)
            quantity_raw  = risk_amount / (entry * stop_distance)
            quantity      = floor(quantity_raw / step_size) * step_size

        Limites:
            - quantity * entry <= capital * 10%
            - capital > 0
            - stop_distance > 0

        Raises:
            PositionSizingError: se os parâmetros violarem as regras.
        """
        if capital <= Decimal("0"):
            raise PositionSizingError("Capital deve ser positivo.")
        if entry_price <= Decimal("0"):
            raise PositionSizingError("Preço de entrada deve ser positivo.")
        if step_size <= Decimal("0"):
            raise PositionSizingError("Step size deve ser positivo.")

        stop_distance = abs(entry_price - stop_loss_price) / entry_price
        if stop_distance < Decimal("0.001"):
            # Stop muito próximo (< 0.1%) — limita ao máximo (10% do capital)
            logger.warning(
                "Stop distance muito pequeno (%.4f%%) — usando posição máxima de 10%% do capital.",
                float(stop_distance * 100),
            )
            stop_distance = Decimal("0.001")

        risk_amount = capital * (risk_pct / Decimal("100"))
        quantity_raw = risk_amount / (entry_price * stop_distance)
        quantity = (quantity_raw / step_size).to_integral_value(ROUND_DOWN) * step_size

        # Cap: máximo 10% do capital
        max_position_value = capital * Decimal("0.10")
        if quantity * entry_price > max_position_value:
            quantity = (
                (max_position_value / entry_price / step_size).to_integral_value(ROUND_DOWN)
                * step_size
            )
            logger.info(
                "Posição limitada a 10%% do capital: %.6f unidades.", float(quantity)
            )

        if quantity <= Decimal("0"):
            raise PositionSizingError(
                f"Quantidade calculada é zero ou negativa ({quantity}). "
                "Capital insuficiente para o step size mínimo."
            )

        return PositionSizeResult(
            quantity=quantity,
            risk_amount=risk_amount,
            risk_pct=risk_pct,
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            stop_distance_pct=(stop_distance * 100).quantize(Decimal("0.0001")),
            position_value=(quantity * entry_price).quantize(Decimal("0.01")),
        )

    async def get_step_size(self, symbol: str) -> Decimal:
        """Retorna step size do par (cache Redis 1h, fallback tabela estática)."""
        cache_key = f"step_size:{symbol}"
        if self._redis:
            cached = await self._redis.get(cache_key)
            if cached:
                return Decimal(cached)

        # Tenta buscar da exchange via connector
        if self._connector:
            try:
                markets = await self._connector._exchange.load_markets()
                market_info = markets.get(symbol, {})
                limits = market_info.get("precision", {})
                amount_precision = limits.get("amount")
                if amount_precision is not None:
                    # precision pode ser inteiro (casas decimais) ou Decimal diretamente
                    if isinstance(amount_precision, int):
                        step = Decimal(10) ** (-amount_precision)
                    else:
                        step = Decimal(str(amount_precision))
                    if self._redis:
                        await self._redis.setex(cache_key, _STEP_CACHE_TTL, str(step))
                    return step
            except Exception as exc:
                logger.warning("Falha ao buscar step size de %s: %s — usando default.", symbol, exc)

        # Fallback
        step = _DEFAULT_STEP_SIZES.get(symbol, Decimal("0.001"))
        if self._redis:
            await self._redis.setex(cache_key, _STEP_CACHE_TTL, str(step))
        return step

    async def get_available_capital(self, quote_asset: str = "USDT") -> Decimal:
        """Busca saldo disponível da exchange via cache Redis."""
        if self._redis:
            cached = await self._redis.get("balance:binance")
            if cached:
                data = json.loads(cached)
                asset_data = data.get(quote_asset, {})
                return Decimal(str(asset_data.get("free", 0)))

        # Fallback: busca direto do connector
        if self._connector:
            try:
                balances = await self._connector.get_balance()
                b = balances.get(quote_asset)
                if b:
                    return b.free
            except Exception as exc:
                logger.warning("Falha ao buscar balance de %s: %s", quote_asset, exc)

        return Decimal("0")
