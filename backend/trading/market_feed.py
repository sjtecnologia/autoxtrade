"""Local market feed with closed-candle and freshness discipline.

Decision paths must never trade the currently-forming candle. They also must
not silently accept an old parquet file: stale data is an explicit error.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}

_ALIASES = {
    "M1": "1m", "M3": "3m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H2": "2h", "H4": "4h", "H6": "6h", "H8": "8h",
    "H12": "12h", "D1": "1d", "W1": "1w",
}


class MarketDataUnavailable(RuntimeError):
    """No reliable closed candle is available for a decision."""


def normalize_timeframe(timeframe: str | None, default: str = "1h") -> str:
    value = (timeframe or default).strip()
    value = _ALIASES.get(value, value)
    if value not in TIMEFRAME_SECONDS:
        raise MarketDataUnavailable(f"Timeframe não suportado: {timeframe!r}")
    return value


def timeframe_seconds(timeframe: str | None, default: str = "1h") -> int:
    return TIMEFRAME_SECONDS[normalize_timeframe(timeframe, default)]


def _utc_now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp_series(frame: pd.DataFrame) -> pd.Series:
    if "timestamp" in frame.columns:
        raw = frame["timestamp"]
    elif isinstance(frame.index, pd.DatetimeIndex):
        raw = pd.Series(frame.index, index=frame.index)
    else:
        raise MarketDataUnavailable("OHLCV sem coluna timestamp")

    try:
        if pd.api.types.is_numeric_dtype(raw):
            values = pd.to_numeric(raw, errors="coerce")
            # Accept seconds, milliseconds and nanoseconds without guessing wrong.
            magnitude = float(values.abs().max(skipna=True) or 0)
            unit = "ns" if magnitude > 1e17 else "ms" if magnitude > 1e14 else "s"
            return pd.to_datetime(values, unit=unit, utc=True, errors="coerce")
        return pd.to_datetime(raw, utc=True, errors="coerce")
    except (TypeError, ValueError) as exc:
        raise MarketDataUnavailable(f"Timestamp OHLCV inválido: {exc}") from exc


def closed_candles(
    frame: pd.DataFrame | None,
    timeframe: str | None,
    *,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Return only candles whose full period has already closed."""
    if frame is None or frame.empty:
        raise MarketDataUnavailable("OHLCV vazio")

    seconds = timeframe_seconds(timeframe)
    result = frame.copy()
    timestamps = _timestamp_series(result)
    if timestamps.isna().all():
        raise MarketDataUnavailable("OHLCV sem timestamps válidos")

    result = result.assign(__timestamp=timestamps)
    result = (
        result.dropna(subset=["__timestamp"])
        .sort_values("__timestamp")
        .drop_duplicates(subset=["__timestamp"], keep="last")
    )
    # A candle timestamp is its opening time. It only becomes usable after
    # open + timeframe has elapsed.
    cutoff = pd.Timestamp(_utc_now(now)) - pd.Timedelta(seconds=seconds)
    result = result[result["__timestamp"] <= cutoff].copy()
    if result.empty:
        raise MarketDataUnavailable(
            f"Nenhum candle fechado disponível para {timeframe or 'timeframe padrão'}"
        )
    if "timestamp" in result.columns:
        result["timestamp"] = result["__timestamp"]
    else:
        result = result.rename(columns={"__timestamp": "timestamp"})
    return result.drop(columns=["__timestamp"]).reset_index(drop=True)


def last_candle_at(frame: pd.DataFrame) -> pd.Timestamp:
    if frame is None or frame.empty:
        raise MarketDataUnavailable("OHLCV vazio")
    timestamps = _timestamp_series(frame).dropna()
    if timestamps.empty:
        raise MarketDataUnavailable("OHLCV sem timestamps válidos")
    return pd.Timestamp(timestamps.max())


def quote_age_seconds(
    frame: pd.DataFrame,
    timeframe: str | None,
    *,
    now: datetime | None = None,
) -> float:
    """Seconds since the last closed candle finished forming."""
    seconds = timeframe_seconds(timeframe)
    closed_at = last_candle_at(frame) + pd.Timedelta(seconds=seconds)
    return (pd.Timestamp(_utc_now(now)) - closed_at).total_seconds()


def ensure_fresh(
    frame: pd.DataFrame,
    timeframe: str | None,
    *,
    max_age_seconds: int,
    now: datetime | None = None,
) -> None:
    age = quote_age_seconds(frame, timeframe, now=now)
    if age < -5:
        raise MarketDataUnavailable(f"Candle com timestamp futuro ({age:.0f}s)")
    if age > max_age_seconds:
        raise MarketDataUnavailable(
            f"Cotação desatualizada: {age:.0f}s > {max_age_seconds}s"
        )


def load_ohlcv(
    symbol: str,
    market: str,
    settings: Any,
    *,
    timeframe: str | None = None,
) -> pd.DataFrame:
    """Load the configured timeframe from local parquet files."""
    tf = normalize_timeframe(timeframe or getattr(settings, "didi_timeframe", "1h"))
    if market in ("B3", "FOREX"):
        from ml.mt5_data_collector import MT5DataCollector

        collector = MT5DataCollector(mt5_files_dir=settings.mt5_files_dir)
        frame = collector.load(symbol, tf, market=market)
    else:
        from ml.data_collector import DataCollector

        frame = DataCollector().load_parquet(symbol, tf)

    if frame is None or frame.empty:
        raise MarketDataUnavailable(f"Sem OHLCV local para {market} {symbol} {tf}")
    return frame


def load_fresh_ohlcv(
    symbol: str,
    market: str,
    settings: Any,
    *,
    timeframe: str | None = None,
    now: datetime | None = None,
    limit: int = 300,
) -> pd.DataFrame:
    """Load local data, keep only closed candles and reject stale quotes."""
    tf = normalize_timeframe(timeframe or getattr(settings, "didi_timeframe", "1h"))
    frame = load_ohlcv(symbol, market, settings, timeframe=tf)
    closed = closed_candles(frame, tf, now=now)
    ensure_fresh(
        closed,
        tf,
        max_age_seconds=int(getattr(settings, "didi_max_quote_age_sec", 7200) or 7200),
        now=now,
    )
    return closed.tail(limit).reset_index(drop=True)


async def latest_mark_price(
    redis: Any,
    symbol: str,
    market: str,
    settings: Any,
    *,
    timeframe: str | None = None,
    now: datetime | None = None,
) -> Decimal:
    """Return a fresh mark price: short-lived Redis quote or closed candle close."""
    if redis is not None:
        cached = await redis.get(f"price:{symbol}")
        if cached:
            try:
                price = Decimal(str(cached))
            except (InvalidOperation, ValueError):
                price = Decimal("0")
            if price.is_finite() and price > 0:
                return price

    frame = load_fresh_ohlcv(symbol, market, settings, timeframe=timeframe, now=now)
    try:
        price = Decimal(str(frame["close"].iloc[-1]))
    except (KeyError, IndexError, InvalidOperation, ValueError) as exc:
        raise MarketDataUnavailable(f"Preço de fechamento inválido para {symbol}") from exc
    if not price.is_finite() or price <= 0:
        raise MarketDataUnavailable(f"Preço de fechamento inválido para {symbol}")
    return price
