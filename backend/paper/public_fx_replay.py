"""Diagnostic-only walk-forward replay using delayed public Yahoo FX candles.

This feed and its illustrative cost assumptions must never qualify an account's
paper execution. Use broker-originated market data and costs for that purpose.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pandas as pd

from paper.costs import PaperCostModel
from paper.qualification import qualify_symbol, strategy_fingerprint, strategy_parameters

DEFAULT_SYMBOLS = {
    "EURUSD.pr": ("EURUSD=X", "0.00005", "0.00002"),
    "GBPUSD.pr": ("GBPUSD=X", "0.00008", "0.00003"),
    "USDJPY.pr": ("USDJPY=X", "0.0075", "0.003"),
    "AUDUSD.pr": ("AUDUSD=X", "0.00007", "0.00002"),
    "NZDUSD.pr": ("NZDUSD=X", "0.00008", "0.00003"),
    "USDCAD.pr": ("USDCAD=X", "0.00008", "0.00003"),
    "USDCHF.pr": ("USDCHF=X", "0.00008", "0.00003"),
}
COMMISSION_RATE = Decimal("0.00007")
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "docs" / "correcao" / "public-fx-replay.json"


def fetch_hourly(ticker: str, period: str, now: datetime) -> tuple[pd.DataFrame, dict]:
    url = (
        "https://query2.finance.yahoo.com/v8/finance/chart/"
        f"{quote(ticker, safe='')}?range={period}&interval=1h"
    )
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 AutoXTrade-paper-diagnostic"})
    payload = None
    for attempt in range(4):
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
            break
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise RuntimeError(f"Yahoo Finance indisponível para {ticker}: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == 3:
                raise RuntimeError(f"Yahoo Finance indisponível para {ticker}: {exc}") from exc
        time.sleep(2**attempt)
    if payload is None:
        raise RuntimeError(f"Yahoo Finance sem resposta para {ticker}")

    chart = payload.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        raise RuntimeError(f"Yahoo Finance sem candles para {ticker}: {chart.get('error')}")
    result = chart["result"][0]
    meta = result.get("meta", {})
    if meta.get("dataGranularity") != "1h":
        raise RuntimeError(f"Granularidade inesperada para {ticker}: {meta.get('dataGranularity')}")
    quote_data = result.get("indicators", {}).get("quote", [{}])[0]
    timestamps = result.get("timestamp", [])
    columns = [quote_data.get(name, []) for name in ("open", "high", "low", "close", "volume")]
    if not timestamps or any(len(values) != len(timestamps) for values in columns):
        raise RuntimeError(f"Candles OHLCV incompletos para {ticker}")

    # Yahoo can append a partial current hour. Only score candles whose start is
    # strictly before the current UTC hour.
    current_hour = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
    rows = []
    for values in zip(timestamps, *columns):
        ts, open_, high, low, close, volume = values
        if ts >= current_hour or any(value is None for value in (open_, high, low, close)):
            continue
        rows.append({
            "timestamp": datetime.fromtimestamp(ts, timezone.utc),
            "open": open_, "high": high, "low": low, "close": close,
            "volume": volume or 0,
        })
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    if len(frame) < 500:
        raise RuntimeError(f"Histórico insuficiente de {ticker}: {len(frame)} candles H1")
    return frame, meta


def run(period: str, symbols: list[str], output: Path) -> dict:
    now = datetime.now(timezone.utc)
    report = {
        "diagnostic_only": True,
        "created_at": now.isoformat(),
        "source_type": "public_aggregated_delayed_market_data",
        "source": "Yahoo Finance chart API, CCY FX aggregate",
        "source_url_template": "https://finance.yahoo.com/quote/{ticker}/",
        "period": period,
        "granularity": "1h",
        "partial_last_candle_excluded": True,
        "no_broker_connection": True,
        "no_orders_sent": True,
        "no_real_money_used": True,
        "costs_note": (
            "Illustrative only, not broker-verified: half-spread 0.5-0.8 pip per side, "
            "slippage 0.2-0.3 pip per side, commission 0.00007 of notional per side."
        ),
        "strategy_sha256": strategy_fingerprint(),
        "strategy_parameters": strategy_parameters(),
        "results": {},
        "qualified_symbols": [],
    }
    for symbol in symbols:
        ticker, spread_text, slippage_text = DEFAULT_SYMBOLS[symbol]
        frame, meta = fetch_hourly(ticker, period, now)
        costs = PaperCostModel(
            spread_price=Decimal(spread_text),
            commission_rate=COMMISSION_RATE,
            slippage_price=Decimal(slippage_text),
        )
        result = qualify_symbol(frame, symbol, costs, now=now)
        report["results"][symbol] = {
            "ticker": ticker,
            "source_price": meta.get("regularMarketPrice"),
            "currency": meta.get("currency"),
            "bars": len(frame),
            "data_start": frame.timestamp.iloc[0].isoformat(),
            "last_closed_bar": frame.timestamp.iloc[-1].isoformat(),
            "costs_simulated": costs.as_dict(),
            **result,
        }
    report["qualified_symbols"] = sorted(
        symbol for symbol, item in report["results"].items() if item["qualified"]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", choices=("6mo", "1y", "2y"), default="2y")
    parser.add_argument("--symbols", nargs="+", choices=sorted(DEFAULT_SYMBOLS), default=sorted(DEFAULT_SYMBOLS))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = run(args.period, args.symbols, args.output)
    print(json.dumps({
        "output": str(args.output),
        "symbols": len(report["results"]),
        "qualified_symbols": report["qualified_symbols"],
        "diagnostic_only": report["diagnostic_only"],
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
