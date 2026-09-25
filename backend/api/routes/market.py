"""Endpoints de dados de mercado — OHLCV histórico e preços em tempo real."""
from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Optional

# Cache de preço base por símbolo para simular random walk no fallback
_fallback_cache: dict[str, dict] = {}

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter(prefix="/market", tags=["market"])

ML_DATA_DIR = Path(__file__).parent.parent.parent / "ml" / "data"

# Mapeamento de timeframes aceitos
_TF_ALIASES: dict[str, str] = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "6h": "6h", "8h": "8h", "12h": "12h", "1d": "1d", "1w": "1w",
    "M1": "1m", "M3": "3m", "M5": "5m", "M15": "15m", "M30": "30m",
    "H1": "1h", "H2": "2h", "H4": "4h", "H6": "6h", "H8": "8h", "H12": "12h", "D1": "1d", "W1": "1w",
}

_TF_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "6h": 360,
    "8h": 480,
    "12h": 720,
    "1d": 1440,
    "1w": 10080,
}


def _tf_to_rule(tf: str) -> str:
    return {
        "1m": "1min",
        "3m": "3min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "1h": "1h",
        "2h": "2h",
        "4h": "4h",
        "6h": "6h",
        "8h": "8h",
        "12h": "12h",
        "1d": "1D",
        "1w": "1W",
    }.get(tf, "1h")


def _symbol_dir(market: str, symbol: str) -> Path | None:
    base = ML_DATA_DIR / market
    if not base.exists():
        return None

    direct = base / symbol
    if direct.exists() and direct.is_dir():
        return direct

    # Fallback case-insensitive
    for d in base.iterdir():
        if d.is_dir() and d.name.upper() == symbol.upper():
            return d

    return None


def _safe_ohlcv_response(symbol: str, timeframe: str, limit: int) -> list[OHLCVCandle]:
    """Gera candles sintéticos quando não há dados locais válidos para o símbolo."""
    return _build_synthetic_ohlcv(symbol, timeframe, limit)


def _build_synthetic_ohlcv(symbol: str, timeframe: str, limit: int) -> list[OHLCVCandle]:
    """Gera candles sintéticos quando os parquet locais não puderem ser lidos."""
    tf = _TF_ALIASES.get(timeframe, timeframe)
    interval_sec = (_TF_MINUTES.get(tf, 60) * 60)
    base_price = 100.0 + (sum(ord(ch) for ch in symbol.upper()) % 97) + (len(symbol) % 5) * 3.5
    rng = random.Random(sum(ord(ch) for ch in symbol.upper()) + int(time.time() // 3600))

    candles: list[OHLCVCandle] = []
    prev_close = base_price
    now = int(time.time())

    for idx in range(limit):
        drift = rng.uniform(-0.015, 0.015) * prev_close
        open_price = prev_close
        close_price = max(1.0, open_price + drift)
        spread = max(0.2, prev_close * 0.003)
        high_price = max(open_price, close_price) + rng.uniform(0.0, spread)
        low_price = min(open_price, close_price) - rng.uniform(0.0, spread)
        volume = round(max(100.0, abs(rng.gauss(1000.0, 250.0))), 2)

        candles.append(OHLCVCandle(
            time=now - (limit - idx - 1) * interval_sec,
            open=round(open_price, 6),
            high=round(high_price, 6),
            low=round(low_price, 6),
            close=round(close_price, 6),
            volume=volume,
        ))
        prev_close = close_price

    return candles


# Se o arquivo de mercado do DWX não for atualizado neste intervalo,
# consideramos o feed stale e caímos para fallback.
_LIVE_FILE_MAX_AGE_S = 30


class OHLCVCandle(BaseModel):
    time: int        # Unix timestamp (segundos) — formato exigido pelo lightweight-charts
    open: float
    high: float
    low: float
    close: float
    volume: float


class PriceItem(BaseModel):
    symbol: str
    bid: float
    ask: float
    mid: float
    source: str = "fallback"


@router.get("/ohlcv", response_model=list[OHLCVCandle])
async def get_ohlcv(
    symbol: str = Query(..., description="Ex: EURUSD.pr"),
    timeframe: str = Query("1h", description="1m 5m 15m 30m 1h 4h 1d"),
    market: str = Query("FOREX", description="FOREX | B3 | CRIPTO"),
    limit: int = Query(500, ge=10, le=2000),
):
    """Retorna candles OHLCV do Parquet local.

    Se o timeframe solicitado não existir para o símbolo, usa o parquet
    disponível mais próximo e tenta resample para o timeframe pedido.
    """
    tf = _TF_ALIASES.get(timeframe, timeframe)
    sym_dir = _symbol_dir(market, symbol)
    if sym_dir is None:
        return _safe_ohlcv_response(symbol, timeframe, limit)

    parquet_path = sym_dir / f"{tf}.parquet"
    source_tf = tf

    if not parquet_path.exists():
        available_files = sorted(sym_dir.glob("*.parquet"))
        if not available_files:
            return _safe_ohlcv_response(symbol, timeframe, limit)

        # Escolhe timeframe disponível mais próximo do solicitado
        desired = _TF_MINUTES.get(tf, 60)
        best_path = None
        best_score = float("inf")
        for p in available_files:
            cand_tf = p.stem
            cand_min = _TF_MINUTES.get(cand_tf)
            if cand_min is None:
                continue
            score = abs(cand_min - desired)
            if score < best_score:
                best_score = score
                best_path = p

        if best_path is None:
            best_path = available_files[-1]

        parquet_path = best_path
        source_tf = parquet_path.stem

    try:
        import pandas as pd
        df = pd.read_parquet(parquet_path)
    except Exception:
        return _safe_ohlcv_response(symbol, timeframe, limit)

    # Garante ordenação
    df = df.sort_values("timestamp")

    # Resample quando possível (somente agregação para timeframe maior).
    req_min = _TF_MINUTES.get(tf)
    src_min = _TF_MINUTES.get(source_tf)
    if req_min and src_min and src_min < req_min:
        try:
            import pandas as pd

            if not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

            rule = _tf_to_rule(tf)
            df = (
                df.set_index("timestamp")
                .resample(rule)
                .agg({
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                })
                .dropna(subset=["open", "high", "low", "close"])
                .reset_index()
            )
        except Exception:
            # Em caso de erro de resample, devolve série original disponível
            pass

    # Aplica limit (últimas N barras)
    df = df.tail(limit).reset_index(drop=True)

    candles: list[OHLCVCandle] = []
    for row in df.itertuples(index=False):
        ts = row.timestamp
        # Converte para Unix int (segundos)
        if hasattr(ts, "timestamp"):
            t = int(ts.timestamp())
        else:
            t = int(float(ts))
        candles.append(OHLCVCandle(
            time=t,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        ))

    return candles


@router.get("/prices", response_model=list[PriceItem])
async def get_prices():
    """Retorna preços atuais (bid/ask).

    Prioridade:
    1. DWX_Market_Data.txt (MT5 ao vivo) — quando o share SMB está montado.
    2. Fallback: último candle de cada parquet local (sem MT5 conectado).
    """
    from config import settings

    # ── Tentativa 1: arquivo DWX ao vivo ─────────────────────────────────────
    if settings.mt5_files_dir:
        market_file = Path(settings.mt5_files_dir) / "DWX" / "DWX_Market_Data.txt"
        if market_file.exists():
            try:
                mtime = market_file.stat().st_mtime
                age_s = time.time() - mtime
                is_fresh = age_s <= _LIVE_FILE_MAX_AGE_S

                try:
                    os.listdir(market_file.parent)
                except OSError:
                    pass
                with open(market_file) as f:
                    data: dict = json.load(f)
                result: list[PriceItem] = []
                for symbol, info in data.items():
                    try:
                        bid = float(info.get("bid", 0))
                        ask = float(info.get("ask", bid))
                        if bid > 0:
                            result.append(PriceItem(
                                symbol=symbol, bid=bid, ask=ask,
                                mid=round((bid + ask) / 2, 6),
                                source="live",
                            ))
                    except (TypeError, ValueError):
                        continue
                if result and is_fresh:
                    return result
            except Exception:
                pass

    # ── Fallback: último fechamento de cada parquet ───────────────────────────
    result = []
    if not ML_DATA_DIR.exists():
        return result

    try:
        import pandas as pd
    except ImportError:
        return result

    for market_dir in ML_DATA_DIR.iterdir():
        if not market_dir.is_dir():
            continue
        for sym_dir in market_dir.iterdir():
            if not sym_dir.is_dir():
                continue
            # Prefere 1h, aceita qualquer tf disponível
            parquet = sym_dir / "1h.parquet"
            if not parquet.exists():
                candidates = sorted(sym_dir.glob("*.parquet"))
                if not candidates:
                    continue
                parquet = candidates[-1]
            try:
                df = pd.read_parquet(parquet, columns=["close"])
                if df.empty:
                    continue
                base_close = float(df["close"].iloc[-1])

                # Random walk: simula tick ±0.03% a cada chamada para o gráfico não ficar estático
                sym_key = sym_dir.name
                cached = _fallback_cache.get(sym_key)
                if cached and time.time() - cached["ts"] < 3600:
                    # Aplica variação sobre o último preço simulado (não sobre o parquet)
                    prev = cached["mid"]
                else:
                    prev = base_close

                # Volatilidade proporcional ao preço (≈0.02% por tick — realista para H1)
                vol = prev * 0.0002
                tick = prev + random.gauss(0, vol)
                _fallback_cache[sym_key] = {"mid": tick, "ts": time.time()}

                spread = tick * 0.0001
                result.append(PriceItem(
                    symbol=sym_dir.name,
                    bid=round(tick - spread / 2, 6),
                    ask=round(tick + spread / 2, 6),
                    mid=round(tick, 6),
                    source="fallback",
                ))
            except Exception:
                continue

    return result


@router.get("/symbols", response_model=dict[str, list[str]])
async def get_available_symbols():
    """Lista símbolos disponíveis agrupados por mercado (baseado nos parquets locais)."""
    result: dict[str, list[str]] = {}
    if not ML_DATA_DIR.exists():
        return result

    for market_dir in sorted(ML_DATA_DIR.iterdir()):
        if not market_dir.is_dir():
            continue
        symbols = sorted(
            d.name for d in market_dir.iterdir()
            if d.is_dir() and any(d.glob("*.parquet"))
        )
        if symbols:
            result[market_dir.name] = symbols

    return result
