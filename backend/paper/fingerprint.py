"""Side-effect-free execution-code fingerprint shared by qualification and tests."""
import hashlib
from pathlib import Path

EXECUTION_SOURCES = (
    "trading/didi_strategy.py", "trading/paper_trader.py", "trading/order_executor.py",
    "trading/contracts.py", "trading/safety.py", "trading/approval_service.py", "trading/market_feed.py",
    "config.py", "trading/dwx_readonly.py", "ml/data_collector.py", "ml/mt5_data_collector.py",
    "trading/connectors/base.py", "trading/connectors/factory.py",
    "trading/connectors/simulated.py", "trading/connectors/memory_transport.py",
    "trading/connectors/dwx.py", "trading/connectors/binance.py",
    "trading/connectors/fake_transports.py", "tasks/trading_tasks.py",
    "paper/backtest.py", "paper/costs.py", "paper/qualification.py", "paper/fingerprint.py",
    "paper/durable_effects.py", "paper/entry_time.py", "paper/close_validation.py",
    "paper/partial_stop.py", "paper/safe_break.py", "paper/protection_recovery.py",
    "paper/protection_evidence.py", "paper/protection_evidence_store.py",
    "paper/protection_evidence_provider.py", "risk/manager.py", "risk/position_sizer.py",
    "risk/drawdown_monitor.py", "risk/correlation_checker.py",
)


def source_paths(backend):
    backend = Path(backend)
    migrations = (str(p.relative_to(backend)) for p in (backend / "alembic/versions").glob("*.py"))
    return tuple(sorted(set(EXECUTION_SOURCES).union(migrations)))


def code_fingerprint(backend):
    backend = Path(backend)
    digest = hashlib.sha256()
    for relative in source_paths(backend):
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update((backend / relative).read_bytes())
    return digest.hexdigest()
