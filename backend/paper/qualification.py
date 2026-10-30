"""Out-of-sample qualification gate for enabling new paper entries."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from config import settings
from paper.backtest import DEFAULT_DATA, load_candles, replay
from paper.costs import PaperCostModel, parse_costs
from trading.market_feed import MarketDataUnavailable, closed_candles

MIN_TRADES_PER_FOLD = 30
MIN_WIN_RATE_PCT = 48.0
MIN_PROFIT_FACTOR = 1.30
MIN_EXPECTANCY_RETURN_PCT = 0.01
MAX_DRAWDOWN_PCT = 20.0
MAX_DATA_AGE_DAYS = 30


def strategy_fingerprint() -> str:
    backend = Path(__file__).resolve().parents[1]
    parts = []
    for relative in (
        "trading/didi_strategy.py",
        "trading/paper_trader.py",
        "trading/order_executor.py",
        "trading/contracts.py",
        "trading/safety.py",
        "tasks/trading_tasks.py",
        "paper/backtest.py",
        "paper/costs.py",
        "paper/qualification.py",
    ):
        parts.append((relative, (backend / relative).read_bytes()))
    digest = hashlib.sha256()
    for name, payload in parts:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(payload)
    return digest.hexdigest()


def strategy_parameters() -> dict:
    return {
        "timeframe": settings.didi_timeframe,
        "adx_min": float(settings.didi_entry_adx_min),
        "rr_target": float(settings.didi_rr_target),
        "safe_break_rr": float(settings.didi_safe_break_rr),
        "momentum_confirmation": False,
        "exit_auto_close": bool(settings.didi_exit_auto_close),
    }


def qualify_symbol(
    frame: pd.DataFrame,
    symbol: str,
    costs: PaperCostModel | None,
    *,
    now: datetime | None = None,
) -> dict:
    reasons = []
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return {"qualified": False, "reasons": ["missing_ohlcv_columns"], "folds": []}
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame["timestamp"].isna().any():
        reasons.append("invalid_timestamps")
    elif not frame["timestamp"].is_monotonic_increasing:
        reasons.append("timestamps_out_of_order")
    if frame["timestamp"].duplicated().any():
        reasons.append("duplicate_timestamps")
    numeric = frame[["open", "high", "low", "close", "volume"]]
    if numeric.isna().any().any():
        reasons.append("null_ohlcv_values")
    valid_ohlc = (
        (frame["open"] > 0) & (frame["high"] > 0) & (frame["low"] > 0)
        & (frame["close"] > 0)
        & (frame["high"] >= frame[["open", "low", "close"]].max(axis=1))
        & (frame["low"] <= frame[["open", "high", "close"]].min(axis=1))
    )
    if not valid_ohlc.all():
        reasons.append("invalid_ohlc_candles")
    frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
    if frame.empty:
        return {"qualified": False, "reasons": ["no_data"], "folds": []}

    reference = now or datetime.now(timezone.utc)
    try:
        frame = closed_candles(frame, settings.didi_timeframe, now=reference)
    except MarketDataUnavailable:
        return {"qualified": False, "reasons": reasons + ["no_closed_candles"], "folds": []}
    frame = frame.reset_index(drop=True)
    if frame.empty:
        return {"qualified": False, "reasons": reasons + ["no_closed_candles"], "folds": []}
    data_end = pd.Timestamp(frame.timestamp.iloc[-1])
    age_days = max((pd.Timestamp(reference) - data_end).total_seconds() / 86400, 0.0)
    if data_end > pd.Timestamp(reference) + pd.Timedelta(minutes=5):
        reasons.append("future_data_timestamp")
    if age_days > MAX_DATA_AGE_DAYS:
        reasons.append("stale_data")
    if costs is None:
        reasons.append("missing_instrument_costs")

    folds = []
    n = len(frame)
    starts = [int(n * 0.50), int(n * 0.75)]
    ends = [int(n * 0.75), n]
    for fold_number, (start, end) in enumerate(zip(starts, ends), start=1):
        if end - start < 100 or start < 80:
            reasons.append(f"fold_{fold_number}_too_short")
            continue
        # Indicators use all history up to the fold end; trades begin only at its start.
        fold = replay(
            frame.iloc[:end].copy(),
            adx_min=settings.didi_entry_adx_min,
            rr_target=settings.didi_rr_target,
            safe_break_rr=settings.didi_safe_break_rr,
            auto_close_on_exit=settings.didi_exit_auto_close,
            require_momentum=False,
            spread_price=costs.spread_price if costs else 0,
            commission_rate=costs.commission_rate if costs else 0,
            slippage_price=costs.slippage_price if costs else 0,
            score_from=frame.timestamp.iloc[start],
        )
        net = fold["net_metrics_under_project_paper_costs"]
        item = {
            "fold": fold_number,
            "start": pd.Timestamp(frame.timestamp.iloc[start]).isoformat(),
            "end": pd.Timestamp(frame.timestamp.iloc[end - 1]).isoformat(),
            "trades": fold["total_trades"],
            "win_rate_pct": net["win_rate_pct"],
            "profit_factor": net["profit_factor"],
            "expectancy_return_pct": net["expectancy_return_pct"],
            "max_drawdown_pct": net["max_drawdown_pct"],
            "pnl_price_units": net["pnl_price_units"],
        }
        checks = {
            "min_trades": item["trades"] >= MIN_TRADES_PER_FOLD,
            "win_rate": item["win_rate_pct"] >= MIN_WIN_RATE_PCT,
            "profit_factor": isinstance(item["profit_factor"], (int, float))
            and item["profit_factor"] >= MIN_PROFIT_FACTOR,
            "expectancy": item["expectancy_return_pct"] > MIN_EXPECTANCY_RETURN_PCT,
            "drawdown": item["max_drawdown_pct"] <= MAX_DRAWDOWN_PCT,
        }
        item["checks"] = checks
        if not all(checks.values()):
            reasons.extend(f"fold_{fold_number}_{name}_failed" for name, ok in checks.items() if not ok)
        folds.append(item)

    qualified = bool(folds) and not reasons
    return {
        "qualified": qualified,
        "reasons": reasons,
        "data_start": pd.Timestamp(frame.timestamp.iloc[0]).isoformat(),
        "data_end": data_end.isoformat(),
        "data_age_days": round(age_days, 2),
        "bars": len(frame),
        "costs": costs.as_dict() if costs else None,
        "folds": folds,
    }


def build_report(results: dict[str, dict], costs: dict[str, PaperCostModel]) -> dict:
    normalized_costs = {key: value.as_dict() for key, value in sorted(costs.items())}
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "strategy_sha256": strategy_fingerprint(),
        "strategy_parameters": strategy_parameters(),
        "thresholds": {
            "min_trades_per_fold": MIN_TRADES_PER_FOLD,
            "min_win_rate_pct": MIN_WIN_RATE_PCT,
            "min_profit_factor": MIN_PROFIT_FACTOR,
            "min_expectancy_return_pct": MIN_EXPECTANCY_RETURN_PCT,
            "max_drawdown_pct": MAX_DRAWDOWN_PCT,
            "max_data_age_days": MAX_DATA_AGE_DAYS,
        },
        "costs_by_symbol": normalized_costs,
        "symbols": results,
        "qualified_symbols": sorted(symbol for symbol, result in results.items() if result["qualified"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--parquet", action="store_true")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--costs-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    timeframe = args.timeframe or settings.didi_timeframe
    costs = parse_costs(args.costs_file.read_text(encoding="utf-8"))
    if args.symbol not in costs:
        parser.error(f"custos não configurados para {args.symbol}")
    if args.parquet:
        source = Path(__file__).resolve().parents[1] / "ml" / "data" / "FOREX" / args.symbol / f"{timeframe}.parquet"
        frame = pd.read_parquet(source)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    else:
        source = args.data
        frame = load_candles(source, args.symbol, timeframe.upper())
    result = qualify_symbol(frame, args.symbol, costs[args.symbol])
    result["source"] = str(source)
    output = args.output or (
        Path(settings.paper_qualification_path).expanduser()
        if settings.paper_qualification_path
        else Path(__file__).with_name("qualification.json")
    )
    existing_results = {}
    existing_costs = {}
    if output.exists():
        try:
            previous = json.loads(output.read_text(encoding="utf-8"))
            if (
                previous.get("strategy_sha256") == strategy_fingerprint()
                and previous.get("strategy_parameters") == strategy_parameters()
            ):
                for old_symbol, old_result in previous.get("symbols", {}).items():
                    old_costs = previous.get("costs_by_symbol", {}).get(old_symbol)
                    if old_symbol == args.symbol or old_costs == (
                        costs[old_symbol].as_dict() if old_symbol in costs else None
                    ):
                        existing_results[old_symbol] = old_result
                        if old_symbol in costs:
                            existing_costs[old_symbol] = costs[old_symbol]
        except (OSError, ValueError, TypeError):
            existing_results = {}
    existing_results[args.symbol] = result
    existing_costs[args.symbol] = costs[args.symbol]
    report = build_report(existing_results, existing_costs)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "symbol": args.symbol, **result}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
