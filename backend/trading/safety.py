"""Fail-closed execution policy. No broker capability is validated in stage 01."""
from enum import Enum
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


class AccountNature(str, Enum):
    SIMULATED = "simulated"
    DEMO = "demo"
    REAL = "real"
    UNKNOWN = "unknown"


class ExposureBlocked(RuntimeError):
    pass


def require_paper_new_exposure(symbol: str) -> None:
    """Require both explicit opt-in and a fresh OOS qualification for this symbol."""
    from config import settings
    if not settings.paper_new_exposure_enabled:
        raise ExposureBlocked("New paper exposure is disabled pending positive out-of-sample validation")
    from paper.costs import paper_costs_for, parse_costs
    from paper.qualification import (
        MAX_DATA_AGE_DAYS,
        MAX_DRAWDOWN_PCT,
        MIN_EXPECTANCY_RETURN_PCT,
        MIN_PROFIT_FACTOR,
        MIN_TRADES_PER_FOLD,
        MIN_WIN_RATE_PCT,
        strategy_fingerprint,
        strategy_parameters,
    )

    costs = paper_costs_for(symbol)
    report_path = (
        Path(settings.paper_qualification_path).expanduser()
        if settings.paper_qualification_path
        else Path(__file__).resolve().parents[1] / "paper" / "qualification.json"
    )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("diagnostic_only") is True:
            raise ValueError("diagnostic-only report cannot qualify broker paper entries")
        created = datetime.fromisoformat(report["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        report_age = (datetime.now(timezone.utc) - created).total_seconds() / 86400
        if report_age < 0 or report_age > 30:
            raise ValueError("qualification report expired")
        data_end = datetime.fromisoformat(report.get("symbols", {}).get(symbol, {}).get("data_end", ""))
        if data_end.tzinfo is None:
            data_end = data_end.replace(tzinfo=timezone.utc)
        if data_end > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("qualification contains future-dated market data")
        if report.get("strategy_sha256") != strategy_fingerprint():
            raise ValueError("strategy changed since qualification")
        if report.get("strategy_parameters") != strategy_parameters():
            raise ValueError("strategy parameters changed since qualification")
        expected_thresholds = {
            "min_trades_per_fold": MIN_TRADES_PER_FOLD,
            "min_win_rate_pct": MIN_WIN_RATE_PCT,
            "min_profit_factor": MIN_PROFIT_FACTOR,
            "min_expectancy_return_pct": MIN_EXPECTANCY_RETURN_PCT,
            "max_drawdown_pct": MAX_DRAWDOWN_PCT,
            "max_data_age_days": MAX_DATA_AGE_DAYS,
        }
        if report.get("thresholds") != expected_thresholds:
            raise ValueError("qualification thresholds changed")
        if report.get("costs_by_symbol", {}).get(symbol) != costs.as_dict():
            raise ValueError("paper cost assumptions differ from qualification")
        if report.get("costs_by_symbol") != {
            key: value.as_dict()
            for key, value in sorted(parse_costs(settings.paper_costs_json).items())
        }:
            raise ValueError("paper cost map differs from qualification")
        symbol_report = report.get("symbols", {}).get(symbol, {})
        if symbol_report.get("data_age_days", MAX_DATA_AGE_DAYS + 1) > MAX_DATA_AGE_DAYS:
            raise ValueError("market data in qualification expired")
        folds = symbol_report.get("folds", [])
        required_checks = {"min_trades", "win_rate", "profit_factor", "expectancy", "drawdown"}
        if len(folds) < 2 or any(
            set(fold.get("checks", {})) != required_checks
            or not all(fold.get("checks", {}).values())
            for fold in folds
        ):
            raise ValueError("walk-forward folds are incomplete or failed")
        if not symbol_report.get("qualified", False):
            reasons = symbol_report.get("reasons", [])
            raise ValueError(f"symbol is not qualified: {', '.join(reasons) or 'unknown reason'}")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExposureBlocked(f"Paper qualification unavailable for {symbol}: {exc}") from exc


def require_entry_mode(mode: str) -> None:
    if mode == "paper":
        return
    if mode != "live":
        raise ExposureBlocked("Unknown execution mode; entry blocked")
    from config import settings
    if not settings.real_new_exposure_enabled:
        raise ExposureBlocked("New real exposure is disabled")
    # A flag alone cannot certify account identity, fills, sizing or protection.
    raise ExposureBlocked("Live execution capability is not validated (stages 02-09)")


def require_new_exposure(connector) -> None:
    _require_local_binding(connector)


def require_position_management(connector) -> None:
    """Independent of entry pause; real mutations await a validated ticket contract."""
    _require_local_binding(connector)


def _require_local_binding(connector):
    from trading.contracts import ExecutionContext, ExecutionMode
    from trading.connectors.base import BaseConnector
    from trading.connectors.memory_transport import MemoryTransport
    from trading.connectors.fake_transports import FakeCCXTClient, TemporaryDWXTransport
    if not isinstance(connector, BaseConnector):
        raise ExposureBlocked("Unbound connector; a simulation label is not authorization")
    context = getattr(connector, "_context", None)
    transport = getattr(connector, "_transport", None)
    if type(context) is not ExecutionContext:
        raise ExposureBlocked("Execution context is missing")
    context.validate()
    if context.mode is not ExecutionMode.PAPER:
        raise ExposureBlocked("All external execution (demo/testnet/live) remains blocked")
    if type(transport) not in {MemoryTransport, FakeCCXTClient, TemporaryDWXTransport}:
        raise ExposureBlocked("Only explicit local fake transports are authorized")
    if transport.context != context:
        raise ExposureBlocked("Connector/transport account binding conflict")
