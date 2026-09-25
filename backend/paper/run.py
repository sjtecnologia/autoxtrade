"""Piloto DiDi paper com dados reais somente para detecção e fallback sintético."""
from __future__ import annotations

import argparse
import asyncio
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal

import pandas as pd

from integration.market_data_refresh import MarketDataRefresh
from ml.data_collector import DataCollector
from integration.probe import (
    ExternalWriteDetected,
    _directory_snapshot,
    _install_write_guards,
    _read_only_connector,
)
from ml.mt5_data_collector import MT5DataCollector
from paper.ledger import PaperLedger
from trading.didi_strategy import evaluate_entry
from trading.reconciliation import ReconciliationReport, reconcile_positions
from trading.dwx_probe import probe_dwx_directory

DEFAULT_LEDGER = Path(__file__).resolve().parent / "ledger.json"
DEFAULT_REPORT = Path(__file__).resolve().parents[2] / "docs" / "correcao" / "piloto-paper.md"
SYNTHETIC_SEED = 20250925


def _synthetic_ohlcv(periods: int = 240, seed: int = SYNTHETIC_SEED) -> pd.DataFrame:
    rows: list[dict] = []
    previous = 100.0
    start = datetime.now(timezone.utc) - timedelta(hours=periods)
    for index in range(periods):
        drift = 0.08 + math.sin(index / 9.0) * 0.12
        close = previous + drift
        spread = 0.35 + abs(math.sin(index / 5.0)) * 0.08
        rows.append({
            "timestamp": start + timedelta(hours=index),
            "open": previous,
            "high": max(previous, close) + spread,
            "low": min(previous, close) - spread,
            "close": close,
            "volume": 1000.0 + ((index * 37 + seed) % 200),
        })
        previous = close
    return pd.DataFrame(rows)


def _price_from_evaluation(evaluation) -> Decimal:
    return evaluation.entry_price if evaluation is not None else Decimal("100")


def _historic_candles(directory: Path, symbol: str, timeframe: str) -> pd.DataFrame:
    path = directory / "DWX_Historic_Data.txt"
    try:
        payload = __import__("json").loads(path.read_text(encoding="utf-8"))
        rows = payload[f"{symbol}_{timeframe}"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Histórico real indisponível para {symbol} {timeframe}") from exc
    data = []
    for timestamp, row in rows.items():
        data.append({
            "timestamp": datetime.strptime(timestamp, "%Y.%m.%d %H:%M").replace(tzinfo=timezone.utc),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("tick_volume", 0)),
        })
    frame = pd.DataFrame(data).sort_values("timestamp").reset_index(drop=True)
    collector = DataCollector(freshness_window_seconds=3600)
    frame, quality = collector.validate_and_clean(
        frame,
        timeframe="5m",
        now=datetime.now(timezone.utc),
    )
    if quality.stale_data:
        raise RuntimeError("Histórico real não fresco")
    if quality.large_gaps_detected:
        raise RuntimeError(f"Histórico real contém {quality.large_gaps_detected} gap(s) grande(s)")
    return frame


async def _reconcile_iterations(connector, iterations: int) -> list[ReconciliationReport]:
    reports: list[ReconciliationReport] = []
    for _ in range(iterations):
        reports.append(await reconcile_positions(connector, []))
    return reports


def run_pilot(
    directory: Path,
    *,
    iterations: int = 5,
    ledger_path: Path = DEFAULT_LEDGER,
    report_path: Path = DEFAULT_REPORT,
    mode: str = "real",
) -> str:
    probe = probe_dwx_directory(directory)
    if mode not in {"real", "synthetic"}:
        raise ValueError("mode deve ser real ou synthetic")
    data_mode = "real_mt5"
    price_source = "mt5_real"
    if mode == "synthetic":
        data_mode = f"synthetic_seed_{SYNTHETIC_SEED}"
        price_source = data_mode
        candles = _synthetic_ohlcv(seed=SYNTHETIC_SEED)
    else:
        if probe.data_quality.market_data_missing or probe.data_quality.stale_data:
            raise RuntimeError("Market data real ausente ou não fresco; use --mode synthetic explicitamente")

    symbol = next((item for item in probe.symbols_hint if item == "EURUSD.pr"), None)
    symbol = symbol or (probe.symbols_hint[0] if probe.symbols_hint else "SIMULATED")
    collector = MT5DataCollector(str(directory), supported_symbols=set(probe.symbols_hint))
    collector.validate_symbol(symbol, "FOREX")
    refresh = MarketDataRefresh(directory)
    ledger = PaperLedger(ledger_path)
    before_commands = {
        path.name for path in directory.iterdir()
        if path.name.startswith("DWX_Commands_")
    }
    connector = _read_only_connector(directory, probe.symbols_hint)
    calls = _install_write_guards(connector)

    orders: list[dict] = []
    signals: list[dict] = []
    refreshes = []
    for index in range(1, iterations + 1):
        if mode == "real":
            current = refresh.ensure_fresh(symbol, "M5")
            candles = _historic_candles(directory, symbol, "M5")
            refreshes.append(current)
        evaluation = evaluate_entry(candles)
        signal = evaluation.direction if evaluation is not None else None
        signals.append({
            "iteration": index,
            "symbol": symbol,
            "signal": signal,
            "data_mode": data_mode,
            "tick": refreshes[-1].tick if refreshes else None,
            "candle": refreshes[-1].bar if refreshes else None,
        })
        if evaluation is not None:
            orders.append(ledger.record_order(
                symbol=symbol,
                side=evaluation.direction,
                quantity=Decimal("1"),
                price=_price_from_evaluation(evaluation),
                price_source=price_source,
            ))

    reconciliation = asyncio.run(_reconcile_iterations(connector, iterations))
    after_commands = {
        path.name for path in directory.iterdir()
        if path.name.startswith("DWX_Commands_")
    }
    if calls:
        raise ExternalWriteDetected(f"Chamadas mutantes no piloto paper: {calls}")
    if before_commands != after_commands:
        raise ExternalWriteDetected("DWX_Commands_* mudou durante o piloto paper")
    if after_commands:
        raise ExternalWriteDetected("DWX_Commands_* encontrado após o piloto paper")

    issues = [issue for report in reconciliation for issue in report.issues]
    health = reconciliation[-1].positions_health
    lines = [
        "# Piloto paper-trading simulado",
        "",
        f"- Executado em: `{datetime.now(timezone.utc).isoformat()}`",
        f"- Diretório DWX lido: `{directory}`",
        f"- Iterações: {iterations}",
        f"- Modo de dados: `{data_mode}`",
        f"- Fonte de preço: `{price_source}`",
        f"- Modo de dados usado: `{data_mode}`; fonte de preço: `{price_source}`.",
        f"- Símbolo avaliado: `{symbol}`",
        f"- Sinais: {sum(signal['signal'] is not None for signal in signals)}",
        f"- Ordens simuladas gravadas: {len(orders)}",
        f"- Ledger: `{ledger.path}`",
        f"- Saúde da fonte de posições: availability=`{health.availability}` ({health.detail or 'sem detalhe'})",
        f"- Divergências/órfãos: {len(issues)}",
        "",
        "## Sinais",
        "",
    ]
    lines.extend(
        f"- iteração {item['iteration']}: signal={item['signal'] or 'none'}; "
        f"data={item['data_mode']}; tick={item['tick']}; candle={item['candle']}"
        for item in signals
    )
    lines.extend([
        "",
        "## Garantias",
        "",
        "- O modo paper nunca chama `place_order`, `_write_command`, cancelamento, proteção ou redução.",
        "- ZERO comandos `DWX_Commands_*` escritos.",
        "- ZERO ordens reais e ZERO mutações no MT5.",
        "- O gate fail-closed de `trading/safety.py` permanece intacto.",
    ])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--datadir", default=None)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--mode", choices=("real", "synthetic"), default="real")
    args = parser.parse_args()
    directory_value = args.datadir or os.environ.get("AUTOXTRADE_MT5_FILES_DIR")
    if not directory_value:
        parser.error("informe --datadir ou AUTOXTRADE_MT5_FILES_DIR")
    directory = Path(directory_value).expanduser()
    if not directory.is_dir():
        parser.error(f"diretório DWX indisponível: {directory}")
    print(run_pilot(
        directory,
        iterations=max(args.iterations, 1),
        ledger_path=args.ledger,
        report_path=args.report,
        mode=args.mode,
    ))


if __name__ == "__main__":
    main()
