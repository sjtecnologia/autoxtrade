"""Probe read-only do MT5 real e geração do relatório de integração."""
from __future__ import annotations

import argparse
import asyncio
import inspect
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ml.data_collector import DataCollector
from ml.mt5_data_collector import MT5DataCollector
from trading.connectors.dwx import DWXConnector
from trading.connectors.memory_transport import MemoryTransport
from trading.contracts import (
    Capability,
    ExecutionContext,
    ExecutionMode,
    Instrument,
)
from trading.reconciliation import ReconciliationReport, reconcile_positions
from trading.safety import AccountNature

DEFAULT_REPORT = Path(__file__).resolve().parents[2] / "docs" / "correcao" / "integracao-mt5.md"
GUARDED_METHODS = (
    "place_order",
    "_write_command",
    "cancel_order",
    "modify_position_protection",
    "reduce_position",
    "place_oco_order",
)


class ExternalWriteDetected(AssertionError):
    """Tentativa de escrita detectada durante o modo somente leitura."""


def _read_only_connector(directory: Path, symbols: tuple[str, ...]) -> DWXConnector:
    symbol = symbols[0] if symbols else "READ_ONLY"
    context = ExecutionContext(
        venue="mt5",
        broker="dwx-read-only",
        account_id="integration-read-only",
        nature=AccountNature.SIMULATED,
        verified=True,
        mode=ExecutionMode.PAPER,
        market="FOREX",
        account_currency="USD",
        position_model="hedging",
        instrument=Instrument(
            symbol=symbol,
            asset_class="read-only",
            base_currency="BASE",
            quote_currency="QUOTE",
            contract_size=1,
            tick_size=1,
            tick_value=1,
            tick_currency="USD",
            min_volume=1,
            max_volume=100,
            volume_step=1,
            price_precision=0,
            volume_precision=0,
            margin_model="read-only",
            margin_rate=1,
        ),
        capabilities=frozenset({Capability.QUERY, Capability.POSITIONS, Capability.PENDING}),
    )
    return DWXConnector(str(directory), context=context, transport=MemoryTransport(context))


def _install_write_guards(connector: DWXConnector) -> list[str]:
    calls: list[str] = []

    def blocked(name: str):
        def fail(*args: Any, **kwargs: Any):
            calls.append(name)
            raise ExternalWriteDetected(f"Escrita externa proibida acionada: {name}")

        return fail

    def blocked_async(name: str):
        async def fail(*args: Any, **kwargs: Any):
            calls.append(name)
            raise ExternalWriteDetected(f"Escrita externa proibida acionada: {name}")

        return fail

    for name in GUARDED_METHODS:
        method = blocked_async(name) if inspect.iscoroutinefunction(getattr(connector, name)) else blocked(name)
        setattr(connector, name, method)
    return calls


def _directory_snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    return {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in directory.iterdir()
        if path.is_file()
    }


def _file_ages(directory: Path, now: datetime) -> dict[str, float]:
    ages: dict[str, float] = {}
    for path in directory.iterdir():
        if path.is_file() and path.name.startswith("DWX_"):
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            ages[path.name] = max((now - modified).total_seconds(), 0.0)
    return ages


def _validate_symbols(symbols: tuple[str, ...]) -> tuple[str, ...]:
    supported = set(symbols)
    crypto = DataCollector(supported_symbols=supported)
    mt5 = MT5DataCollector("read-only", supported_symbols=supported)
    invalid: list[str] = []
    for symbol in symbols:
        try:
            crypto.validate_symbol(symbol)
            mt5.validate_symbol(symbol, "FOREX")
        except Exception:
            invalid.append(symbol)
    return tuple(invalid)


class _DryRunDB:
    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


async def _read_and_reconcile(connector: DWXConnector) -> ReconciliationReport:
    return await reconcile_positions(connector, [])


async def _reconcile_iterations(
    connector: DWXConnector, iterations: int
) -> list[ReconciliationReport]:
    return [
        await _read_and_reconcile(connector)
        for _ in range(iterations)
    ]


def run_probe(directory: Path, *, iterations: int = 1) -> str:
    now = datetime.now(timezone.utc)
    connector = _read_only_connector(directory, ())
    calls = _install_write_guards(connector)
    before = _directory_snapshot(directory)
    report = connector.probe_files_read_only(now=now)
    connector = _read_only_connector(directory, report.symbols_hint)
    calls = _install_write_guards(connector)
    reports = asyncio.run(_reconcile_iterations(connector, iterations))
    after = _directory_snapshot(directory)

    if calls:
        raise ExternalWriteDetected(f"Chamadas externas detectadas: {calls}")
    if before != after:
        raise ExternalWriteDetected("O estado do diretório DWX mudou durante o probe")
    if any(path.name.startswith("DWX_Commands_") for path in directory.iterdir()):
        raise ExternalWriteDetected("DWX_Commands_* encontrado após probe read-only")

    invalid_symbols = _validate_symbols(report.symbols_hint)
    ages = _file_ages(directory, now)
    average_age = sum(ages.values()) / len(ages) if ages else 0.0
    issues = [issue for reconciliation in reports for issue in reconciliation.issues]
    report_lines = [
        "# Integração MT5/DWX - modo somente leitura",
        "",
        f"- Caminho lido: `{directory}`",
        f"- Data da leitura: `{now.isoformat()}`",
        f"- Arquivos `DWX_*`: {', '.join(report.files)}",
        f"- Padrão derivado: `{report.naming_pattern}`",
        f"- Fonte de ordens: `{report.orders_path or 'ausente'}`",
        f"- Fonte de market data: `{report.market_data_path or 'ausente'}`",
        f"- Símbolos encontrados (hint): {', '.join(report.symbols_hint) or 'nenhum'}",
        f"- Símbolos inválidos: {', '.join(invalid_symbols) or 'nenhum'}",
        f"- Idade média dos arquivos `DWX_*`: {average_age:.1f}s",
        f"- Market data ausente: `{report.data_quality.market_data_missing}`",
        f"- Dado não fresco: `{report.data_quality.stale_data}`",
        f"- `small_gaps_filled`: {report.data_quality.small_gaps_filled}",
        f"- Gaps grandes: {report.data_quality.large_gaps_detected}",
        f"- Ausências: {', '.join(report.missing_sources) or 'nenhuma'}",
        f"- Divergências/órfãos: {len(issues)}",
    ]
    for issue in issues:
        report_lines.append(f"  - `{issue.kind}`: {issue.detail or issue.order_id or issue.position_id or ''}")
    report_lines.extend([
        "",
        "## Achados",
        "",
        *[f"- {finding}" for finding in report.findings],
        "",
        "## Dry-run",
        "",
        f"- Iterações executadas: {iterations}",
        "- OrderMonitor/reconciliação: somente consulta e sinalização.",
        "- ZERO comandos DWX escritos: confirmado por guarda e snapshot.",
        "- ZERO ordens enviadas ou mutações: confirmado por guarda de métodos.",
    ])
    DEFAULT_REPORT.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return "\n".join(report_lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", help="Diretório DWX montado")
    parser.add_argument("--iterations", type=int, default=1)
    args = parser.parse_args()
    directory_value = args.directory or os.environ.get("AUTOXTRADE_MT5_FILES_DIR")
    if not directory_value:
        parser.error("informe o diretório ou AUTOXTRADE_MT5_FILES_DIR")
    directory = Path(directory_value).expanduser()
    if not directory.is_dir():
        parser.error(f"diretório DWX indisponível: {directory}")
    print(run_probe(directory, iterations=max(args.iterations, 1)))


if __name__ == "__main__":
    main()
