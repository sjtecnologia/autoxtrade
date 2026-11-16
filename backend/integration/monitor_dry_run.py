"""Monitoramento DWX em dry-run: consulta e reconciliação, sem mutação."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from trading.order_monitor import OrderMonitor
from integration.probe import (
    ExternalWriteDetected,
    _directory_snapshot,
    _install_write_guards,
    _read_only_connector,
)


class _DryRunDB:
    async def execute(self, _statement):
        class EmptyResult:
            def scalars(self):
                class EmptyScalars:
                    def all(self):
                        return []

                return EmptyScalars()

        return EmptyResult()


async def _run(directory: Path, iterations: int) -> list[int]:
    connector = _read_only_connector(directory, ())
    calls = _install_write_guards(connector)
    monitor = OrderMonitor(connector, _DryRunDB())
    before = _directory_snapshot(directory)
    issue_counts: list[int] = []

    for index in range(iterations):
        report = await monitor.check_orphan_positions()
        issue_counts.append(len(report.issues))
        print(f"dry-run iteração {index + 1}/{iterations}: {len(report.issues)} divergência(s)")

    after = _directory_snapshot(directory)
    if calls:
        raise ExternalWriteDetected(f"Chamadas externas detectadas: {calls}")
    if before != after:
        raise ExternalWriteDetected("O estado do diretório DWX mudou durante o dry-run")
    if any(path.name.startswith("DWX_Commands_") for path in directory.iterdir()):
        raise ExternalWriteDetected("DWX_Commands_* encontrado após dry-run")
    return issue_counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", help="Diretório DWX montado")
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()
    directory_value = args.directory or os.environ.get("AUTOXTRADE_MT5_FILES_DIR")
    if not directory_value:
        parser.error("informe o diretório ou AUTOXTRADE_MT5_FILES_DIR")
    directory = Path(directory_value).expanduser()
    if not directory.is_dir():
        parser.error(f"diretório DWX indisponível: {directory}")
    counts = asyncio.run(_run(directory, max(args.iterations, 1)))
    print(f"dry-run concluído: {len(counts)} iterações; ZERO comandos e ZERO mutações")


if __name__ == "__main__":
    main()
