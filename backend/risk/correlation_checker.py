"""Verificação de correlação de Pearson entre posições abertas."""
import json
import logging
from dataclasses import dataclass, field

import numpy as np
import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)

_MIN_CANDLES = 5
_RETURNS_CACHE_TTL = 60  # segundos


@dataclass
class CorrelationCheckResult:
    blocked: bool
    correlated_with: str | None = None
    correlation: float | None = None
    warning: str | None = None
    reason: str | None = None


class CorrelationChecker:
    def __init__(self, connector=None) -> None:
        self._connector = connector
        self._redis: aioredis.Redis | None = None

    async def startup(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def shutdown(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def get_returns(self, symbol: str, n_candles: int = 20, timeframe: str = "1h") -> list[float]:
        """Retorna lista de retornos percentuais dos últimos N candles (cacheable)."""
        cache_key = f"returns:{symbol}:{timeframe}:{n_candles}"
        if self._redis:
            cached = await self._redis.get(cache_key)
            if cached:
                return json.loads(cached)

        closes: list[float] = []
        if self._connector:
            try:
                candles = await self._connector.get_ohlcv(symbol, timeframe, n_candles + 1)
                closes = [float(c.close) for c in candles]
            except Exception as exc:
                logger.warning("Falha ao buscar OHLCV para correlação (%s): %s", symbol, exc)
        else:
            # Tenta ler do cache de OHLCV no Redis
            if self._redis:
                raw = await self._redis.get(f"ohlcv:{symbol}:{timeframe}")
                if raw:
                    data = json.loads(raw)
                    closes = [float(c[4]) for c in data[-(n_candles + 1):]]

        if len(closes) < 2:
            return []

        returns = [
            (closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes))
        ]

        if self._redis:
            await self._redis.setex(cache_key, _RETURNS_CACHE_TTL, json.dumps(returns))

        return returns

    def calculate_correlation(self, returns_a: list[float], returns_b: list[float]) -> float:
        """Calcula coeficiente de Pearson entre dois vetores de retornos."""
        n = min(len(returns_a), len(returns_b))
        if n < _MIN_CANDLES:
            return 0.0
        a = np.array(returns_a[-n:])
        b = np.array(returns_b[-n:])
        if np.std(a) == 0 or np.std(b) == 0:
            return 0.0
        corr_matrix = np.corrcoef(a, b)
        return float(corr_matrix[0][1])

    async def check(
        self,
        candidate_symbol: str,
        open_trades: list,
        threshold: float = 0.7,
        is_paper: bool = False,
    ) -> CorrelationCheckResult:
        """
        Verifica correlação do candidato com todas as posições abertas.
        Em paper mode: não bloqueia, apenas loga.
        """
        if not open_trades:
            return CorrelationCheckResult(blocked=False)

        candidate_returns = await self.get_returns(candidate_symbol)
        if not candidate_returns:
            logger.warning("[correlação] Sem dados para %s — não bloqueando.", candidate_symbol)
            return CorrelationCheckResult(blocked=False, warning="insufficient_data")

        for trade in open_trades:
            existing_symbol = trade.symbol
            if existing_symbol == candidate_symbol:
                continue

            existing_returns = await self.get_returns(existing_symbol)
            if not existing_returns:
                continue

            corr = self.calculate_correlation(candidate_returns, existing_returns)

            logger.info(
                "[correlação] %s ↔ %s = %.4f (threshold=%.2f)",
                candidate_symbol, existing_symbol, corr, threshold,
            )

            if abs(corr) >= threshold:
                reason = (
                    f"Alta correlação ({corr:.2f}) com posição aberta em {existing_symbol}"
                )
                if is_paper:
                    logger.warning("[correlação] Paper mode — correlação alta ignorada: %s", reason)
                    return CorrelationCheckResult(
                        blocked=False,
                        correlated_with=existing_symbol,
                        correlation=corr,
                        warning=reason,
                    )
                logger.warning("[correlação] Bloqueando abertura: %s", reason)
                return CorrelationCheckResult(
                    blocked=True,
                    correlated_with=existing_symbol,
                    correlation=corr,
                    reason=reason,
                )

        return CorrelationCheckResult(blocked=False)
