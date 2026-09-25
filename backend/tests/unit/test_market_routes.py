import asyncio
from pathlib import Path

import pandas as pd

from api.routes import market as market_routes


def test_get_ohlcv_falls_back_to_synthetic_candles_when_parquet_reader_fails(monkeypatch, tmp_path):
    symbol_dir = tmp_path / "TESTSYM"
    symbol_dir.mkdir()
    (symbol_dir / "15m.parquet").write_bytes(b"placeholder")

    monkeypatch.setattr(market_routes, "_symbol_dir", lambda market, symbol: symbol_dir)

    def fake_read_parquet(*args, **kwargs):
        raise ImportError("Missing optional dependency 'pyarrow'")

    monkeypatch.setattr(pd, "read_parquet", fake_read_parquet)

    candles = asyncio.run(market_routes.get_ohlcv(symbol="TESTSYM", timeframe="15m", market="TEST", limit=5))

    assert len(candles) == 5
    assert candles[0].open > 0
    assert candles[0].high >= candles[0].open
    assert candles[0].low <= candles[0].open
    assert candles[0].close > 0


def test_get_ohlcv_falls_back_to_synthetic_candles_when_parquet_is_missing(monkeypatch, tmp_path):
    symbol_dir = tmp_path / "TESTSYM"
    symbol_dir.mkdir()

    monkeypatch.setattr(market_routes, "_symbol_dir", lambda market, symbol: symbol_dir)

    candles = asyncio.run(market_routes.get_ohlcv(symbol="TESTSYM", timeframe="15m", market="TEST", limit=5))

    assert len(candles) == 5
    assert candles[0].open > 0
    assert candles[0].high >= candles[0].open
    assert candles[0].low <= candles[0].open
    assert candles[0].close > 0
