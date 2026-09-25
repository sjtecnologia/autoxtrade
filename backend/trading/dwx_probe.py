"""Sondagem read-only do layout real de arquivos DWX/MT5."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from ml.data_collector import DataQualityReport
from trading.contracts import CapabilityUnavailable

_DWX_FILE_RE = re.compile(r"^DWX_(?P<kind>[A-Za-z]+(?:_[A-Za-z]+)*?)(?:_(?P<slot>\d+))?\.txt$")
_EXPECTED_SOURCES = (
    "DWX_Commands_*",
    "DWX_Positions_*",
    "DWX_Open_Orders_*",
)


@dataclass(frozen=True)
class DWXProbeReport:
    directory: Path
    files: tuple[str, ...]
    naming_pattern: str
    orders_path: Path | None
    market_data_path: Path | None
    symbols_hint_path: Path | None
    symbols_hint: tuple[str, ...]
    missing_sources: tuple[str, ...]
    findings: tuple[str, ...]
    data_quality: DataQualityReport


def _iter_dwx_files(directory: Path) -> list[Path]:
    try:
        return sorted(
            path for path in directory.iterdir()
            if path.is_file() and path.name.startswith("DWX_")
        )
    except FileNotFoundError as exc:
        raise CapabilityUnavailable(
            f"Nenhum arquivo DWX_* encontrado em {directory}: diretório ausente"
        ) from exc
    except OSError as exc:
        raise CapabilityUnavailable(
            f"Não foi possível listar arquivos DWX em {directory}: {exc}"
        ) from exc


def _derive_naming_pattern(files: Iterable[Path]) -> str:
    parsed = [_DWX_FILE_RE.match(path.name) for path in files]
    slots = [match.group("slot") for match in parsed if match and match.group("slot")]
    if slots:
        return "DWX_<tipo>_N.txt"
    if any(match for match in parsed):
        return "DWX_<tipo>.txt"
    return "não derivado (nomes fora do padrão DWX_*.txt)"


def _read_symbol_hint(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ()
    tokens = re.split(r"[\s,;]+", raw)
    return tuple(sorted({token.strip() for token in tokens if token.strip()}))


def _inspect_market_data(
    path: Path | None,
    report: DataQualityReport,
    *,
    now: datetime,
    freshness_window_seconds: int,
) -> None:
    report.market_data_source = str(path) if path else None
    if path is None:
        report.market_data_missing = True
        report.stale_data = True
        report.missing_sources.append("DWX_Market_Data.txt")
        return

    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").strip()
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        raw = ""
        modified = now

    age = max((now - modified).total_seconds(), 0.0)
    report.freshness_age_seconds = age
    empty_payload = not raw
    if raw:
        try:
            empty_payload = not bool(json.loads(raw))
        except json.JSONDecodeError:
            empty_payload = True

    report.market_data_missing = empty_payload
    report.stale_data = empty_payload or age > freshness_window_seconds
    if empty_payload:
        report.missing_sources.append("DWX_Market_Data.txt (vazio/sem payload)")


def probe_dwx_directory(
    directory: str | Path,
    *,
    now: datetime | None = None,
    freshness_window_seconds: int = 3600,
) -> DWXProbeReport:
    """Lista e classifica o estado DWX sem escrever no diretório informado."""
    root = Path(directory).expanduser()
    files = _iter_dwx_files(root)
    if not files:
        raise CapabilityUnavailable(
            f"Nenhum arquivo DWX_* encontrado em {root}; integração DWX indisponível"
        )

    by_name = {path.name: path for path in files}
    orders_path = by_name.get("DWX_Orders.txt")
    market_data_path = by_name.get("DWX_Market_Data.txt")
    symbols_path = root / "MarketWatchSymbols.txt"
    if not symbols_path.is_file():
        symbols_path = None

    report = DataQualityReport()
    reference = now or datetime.now(timezone.utc)
    _inspect_market_data(
        market_data_path,
        report,
        now=reference,
        freshness_window_seconds=freshness_window_seconds,
    )

    findings: list[str] = []
    observed_unsuffixed = orders_path is not None or market_data_path is not None
    if observed_unsuffixed:
        findings.append(
            "ACHADO: servidor usa DWX_Orders.txt/DWX_Market_Data.txt sem sufixo; "
            "o conector/protocolo espera variantes com slot (_N) em alguns canais; "
            "recomendação: parametrizar nomes ou alinhar o conector ao EA."
        )
    if orders_path is None:
        report.missing_sources.append("DWX_Orders.txt")
        findings.append("DWX_Orders.txt ausente: reconciliação limitada sem fonte de ordens.")
    if symbols_path is not None:
        findings.append("MarketWatchSymbols.txt usado somente como hint de símbolos suportados.")
    else:
        report.missing_sources.append("MarketWatchSymbols.txt (hint ausente)")
    for source in _EXPECTED_SOURCES:
        if not any(path.name.startswith(source[:-1]) for path in files):
            report.missing_sources.append(source)

    return DWXProbeReport(
        directory=root,
        files=tuple(path.name for path in files),
        naming_pattern=_derive_naming_pattern(files),
        orders_path=orders_path,
        market_data_path=market_data_path,
        symbols_hint_path=symbols_path,
        symbols_hint=_read_symbol_hint(symbols_path),
        missing_sources=tuple(report.missing_sources),
        findings=tuple(findings),
        data_quality=report,
    )
