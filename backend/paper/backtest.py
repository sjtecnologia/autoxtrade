"""Replay offline do setup DiDi sobre candles históricos DWX, sem I/O externo."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd

from config import settings
from trading.didi_strategy import _build_indicator_frame, evaluate_entry, evaluate_exit
from trading.didi_strategy import evaluate_safe_break
from trading.connectors.dwx import parse_server_timestamp

DEFAULT_DATA = Path("/Volumes/MT5Files/DWX/DWX_Historic_Data.txt")
SPREAD_RATE = Decimal("0.001")
COMMISSION_RATE = Decimal("0.001")
RR_TARGET = Decimal("2")
SIGNAL_EXIT_COST_SPREAD = Decimal("0.0001")  # 1 pip EURUSD por lado, hipótese ilustrativa


def load_candles(path: Path, symbol: str, timeframe: str = "M5") -> pd.DataFrame:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload[f"{symbol}_{timeframe}"]
    frame = pd.DataFrame([
        {
            "timestamp": datetime.fromtimestamp(
                parse_server_timestamp(ts, settings.mt5_server_timezone), tz=timezone.utc
            ),
            "open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "volume": float(row.get("tick_volume", 0)),
        }
        for ts, row in rows.items()
    ])
    return frame.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def replay(
    frame: pd.DataFrame, *, adx_min: float = 23.0,
    require_momentum: bool = False, rr_target: float = 2.0,
    safe_break_rr: float = 1.0,
    auto_close_on_exit: bool = False,
    spread_price: Decimal = SPREAD_RATE,
    commission_rate: Decimal = COMMISSION_RATE,
    slippage_price: Decimal = Decimal("0"),
    score_from=None,
) -> dict:
    """Usa só barras já fechadas para sinal e evita posições simultâneas."""
    frame = _build_indicator_frame(frame)
    if frame is None or len(frame) < 12:
        return _metrics([])
    trades: list[dict] = []
    open_trade: dict | None = None
    last_entry_bar = -1
    for i in range(1, len(frame)):
        window = frame.iloc[: i + 1]
        bar = frame.iloc[i]
        if open_trade:
            side = open_trade["side"]
            sl_hit = bar.low <= open_trade["sl"] if side == "buy" else bar.high >= open_trade["sl"]
            tp_hit = bar.high >= open_trade["tp"] if side == "buy" else bar.low <= open_trade["tp"]
            # OHLC não revela a ordem intrabar quando ambos são tocados: assume SL.
            if sl_hit or tp_hit:
                reason = "SL" if sl_hit else "TP"
                raw_exit = open_trade["sl"] if sl_hit else open_trade["tp"]
                if side == "buy":
                    if sl_hit and bar.open < raw_exit:
                        raw_exit = bar.open
                    elif tp_hit and bar.open > raw_exit:
                        raw_exit = bar.open
                else:
                    if sl_hit and bar.open > raw_exit:
                        raw_exit = bar.open
                    elif tp_hit and bar.open < raw_exit:
                        raw_exit = bar.open
                open_trade["exit"] = float(raw_exit)
                open_trade["exit_time"] = str(bar.timestamp)
                open_trade["reason"] = reason
                trades.append(_settle(open_trade, spread_price, commission_rate, slippage_price))
                open_trade = None
            else:
                # Runtime arms safe break from the latest completed candle.
                # Apply it after this candle's existing SL/TP checks, so it only
                # protects trades from the next bar onward.
                safe = evaluate_safe_break(
                    side=side,
                    entry_price=Decimal(str(open_trade["entry"])),
                    stop_loss=Decimal(str(open_trade["sl"])),
                    current_price=Decimal(str(bar.close)),
                    trigger_rr=Decimal(str(safe_break_rr)),
                )
                if safe.should_move:
                    open_trade["sl"] = float(safe.new_stop_loss)
                    open_trade["safe_break_armed"] = True
                exit_eval = evaluate_exit(window, side, _prepared=True)
                if auto_close_on_exit and exit_eval.should_exit:
                    open_trade["exit"] = float(bar.close)
                    open_trade["exit_time"] = str(bar.timestamp)
                    open_trade["reason"] = "STRATEGY"
                    trades.append(_settle(open_trade, spread_price, commission_rate, slippage_price))
                    open_trade = None
        if not open_trade and i - last_entry_bar >= 1 and (
            score_from is None or pd.Timestamp(bar.timestamp) >= pd.Timestamp(score_from)
        ):
            evaluation = evaluate_entry(
                window, adx_min=adx_min,
                require_momentum_confirmation=require_momentum,
                rr_target=Decimal(str(rr_target)),
                safe_rr=Decimal(str(safe_break_rr)),
                _prepared=True,
            )
            if evaluation is not None:
                if i + 1 >= len(frame):
                    continue
                next_bar = frame.iloc[i + 1]
                open_trade = {
                    "side": evaluation.direction,
                    "signal_time": str(bar.timestamp),
                    "fill_time": str(next_bar.timestamp),
                    "entry": float(next_bar.open),
                    "sl": float(evaluation.stop_loss),
                    # Runtime keeps the stop and target derived from the signal
                    # candle; a gap at the next open changes the realized R:R.
                    "tp": float(evaluation.take_profit),
                    "safe_break_armed": False,
                }
                last_entry_bar = i
    if open_trade:
        final = frame.iloc[-1]
        open_trade["exit"] = float(final.close)
        open_trade["exit_time"] = str(final.timestamp)
        open_trade["reason"] = "END_OF_DATA"
        trades.append(_settle(open_trade, spread_price, commission_rate, slippage_price))
    return _metrics(trades)


def _settle(
    trade: dict, spread_price: Decimal, commission_rate: Decimal,
    slippage_price: Decimal = Decimal("0"),
) -> dict:
    side_mult = 1 if trade["side"] == "buy" else -1
    raw_entry, raw_exit = Decimal(str(trade["entry"])), Decimal(str(trade["exit"]))
    execution_cost = spread_price + slippage_price
    entry = raw_entry + execution_cost if side_mult == 1 else raw_entry - execution_cost
    exit_price = raw_exit - execution_cost if side_mult == 1 else raw_exit + execution_cost
    gross = (raw_exit - raw_entry) * side_mult
    spread_cost = spread_price * 2
    slippage_cost = slippage_price * 2
    commission = (entry + exit_price) * commission_rate
    net = gross - spread_cost - slippage_cost - commission
    return {**trade, "fill_entry": float(entry), "fill_exit": float(exit_price),
            "pnl_gross": float(gross), "spread_cost": float(spread_cost),
            "slippage_cost": float(slippage_cost),
            "pnl_after_spread": float(gross - spread_cost), "commission": float(commission),
            "pnl_net": float(net), "return_pct": float(net / entry * 100)}


def _metrics(trades: list[dict]) -> dict:
    def summarize(pnls: list[float], returns: list[float]) -> dict:
        gains = sum(x for x in pnls if x > 0)
        losses = abs(sum(x for x in pnls if x < 0))
        equity, peak, max_dd = 1.0, 1.0, 0.0
        for pct in returns:
            equity *= 1 + pct / 100
            peak = max(peak, equity)
            if peak:
                max_dd = max(max_dd, (peak - equity) / peak * 100)
        return {
            "wins": sum(x > 0 for x in pnls), "losses": sum(x <= 0 for x in pnls),
            "win_rate_pct": round(sum(x > 0 for x in pnls) / len(pnls) * 100, 2) if pnls else 0,
            "profit_factor": round(gains / losses, 4) if losses else ("inf" if gains else 0),
            "pnl_price_units": round(sum(pnls), 6),
            "expectancy_price_units": round(sum(pnls) / len(pnls), 6) if pnls else 0,
            "expectancy_return_pct": round(sum(returns) / len(returns), 6) if returns else 0,
            "max_drawdown_pct": round(max_dd, 4),
        }
    net = summarize([t["pnl_net"] for t in trades], [t["return_pct"] for t in trades])
    after_spread = summarize(
        [t["pnl_after_spread"] for t in trades],
        [t["pnl_after_spread"] / t["entry"] * 100 for t in trades],
    )
    gross = summarize([t["pnl_gross"] for t in trades], [t["pnl_gross"] / t["entry"] * 100 for t in trades])
    return {
        "bars": None, "trades": trades, "total_trades": len(trades),
        "gross_metrics": gross,
        "after_simulated_spread_metrics": after_spread,
        "net_metrics_under_project_paper_costs": net,
        "exit_reasons": {reason: sum(t["reason"] == reason for t in trades)
                         for reason in sorted({t["reason"] for t in trades})},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--symbol", default="EURUSD.pr")
    parser.add_argument("--timeframe", default="M5")
    parser.add_argument("--parquet", action="store_true", help="Lê backend/ml/data/FOREX/{symbol}/{timeframe}.parquet")
    parser.add_argument("--adx-min", type=float, default=23.0)
    parser.add_argument("--rr-target", type=float, default=2.0)
    parser.add_argument("--safe-break-rr", type=float, default=1.0)
    parser.add_argument("--auto-close-on-exit", action="store_true")
    parser.add_argument("--require-momentum", action="store_true")
    parser.add_argument("--spread-price", type=Decimal, default=SPREAD_RATE)
    parser.add_argument("--commission-rate", type=Decimal, default=COMMISSION_RATE)
    parser.add_argument("--slippage-price", type=Decimal, default=Decimal("0"))
    args = parser.parse_args()
    if args.parquet:
        frame = pd.read_parquet(
            Path(__file__).resolve().parents[1] / "ml" / "data" / "FOREX" / args.symbol / f"{args.timeframe}.parquet"
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        source = "backend/ml/data parquet cache"
    else:
        frame = load_candles(args.data, args.symbol, args.timeframe)
        source = str(args.data)
    raw_bars = len(frame)
    result = replay(
        frame, adx_min=args.adx_min, rr_target=args.rr_target,
        safe_break_rr=args.safe_break_rr,
        auto_close_on_exit=args.auto_close_on_exit,
        require_momentum=args.require_momentum,
        spread_price=args.spread_price, commission_rate=args.commission_rate,
        slippage_price=args.slippage_price,
    )
    result.update({"symbol": args.symbol, "timeframe": args.timeframe, "bars": raw_bars,
                   "first_candle": str(frame.timestamp.iloc[0]), "last_candle": str(frame.timestamp.iloc[-1]),
                   "price_source": source, "spread_price_per_side": str(args.spread_price),
                   "commission_rate_per_side": str(args.commission_rate),
                   "slippage_price_per_side": str(args.slippage_price),
                   "illustrative_one_pip_cost_metrics": _metrics_with_cost(trades=result["trades"]),
                   "note": "Offline replay; signal on completed bar, entry next bar open, exit rule on completed bar, OHLC ambiguity resolves to stop first."})
    print(json.dumps(result, indent=2, ensure_ascii=False))


def _metrics_with_cost(trades: list[dict]) -> dict:
    """Recalcula custos ilustrativos para mostrar sensibilidade, sem alegar corretagem real."""
    adjusted = []
    for trade in trades:
        side = Decimal("1") if trade["side"] == "buy" else Decimal("-1")
        entry, exit_price = Decimal(str(trade["entry"])), Decimal(str(trade["exit"]))
        cost = SIGNAL_EXIT_COST_SPREAD * 2
        pnl = Decimal(str(trade["pnl_gross"])) - cost
        adjusted.append({"pnl": float(pnl), "pct": float(pnl / entry * 100)})
    return {
        "assumed_round_trip_cost_price_units": float(SIGNAL_EXIT_COST_SPREAD * 2),
        **_metrics([{ "pnl_net": x["pnl"], "pnl_after_spread": x["pnl"],
                      "return_pct": x["pct"], "pnl_gross": x["pnl"],
                      "entry": 1, "reason": "sensitivity"} for x in adjusted])["net_metrics_under_project_paper_costs"],
    }


if __name__ == "__main__":
    main()
