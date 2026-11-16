"""Cache Redis para dados de mercado (preços, OHLCV)."""
import json
import logging
from decimal import Decimal

import redis.asyncio as aioredis

from config import settings

logger = logging.getLogger(__name__)

PRICE_TTL = 5   # segundos
OHLCV_TTL = 10  # segundos


class MarketDataCache:
    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None

    async def startup(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def shutdown(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def set_price(self, symbol: str, price: Decimal) -> None:
        if self._redis:
            await self._redis.setex(f"price:{symbol}", PRICE_TTL, str(price))

    async def get_price(self, symbol: str) -> Decimal | None:
        if not self._redis:
            return None
        val = await self._redis.get(f"price:{symbol}")
        return Decimal(val) if val else None

    async def set_ohlcv(self, symbol: str, timeframe: str, candles: list) -> None:
        if self._redis:
            await self._redis.setex(
                f"ohlcv:{symbol}:{timeframe}", OHLCV_TTL, json.dumps(candles)
            )

    async def get_ohlcv(self, symbol: str, timeframe: str) -> list | None:
        if not self._redis:
            return None
        val = await self._redis.get(f"ohlcv:{symbol}:{timeframe}")
        return json.loads(val) if val else None

    async def publish_price_update(self, symbol: str, price: Decimal) -> None:
        if self._redis:
            await self._redis.publish(
                "market_data",
                json.dumps({"type": "PRICE_UPDATE", "symbol": symbol, "price": str(price)}),
            )


market_cache = MarketDataCache()
