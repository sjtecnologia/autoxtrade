from decimal import Decimal
import json
from datetime import datetime, timezone
from types import SimpleNamespace


def _dwx_dir(tmp_path):
    tmp_path.mkdir()
    (tmp_path / "DWX_Orders.txt").write_text('{"orders": {}}')
    (tmp_path / "DWX_Market_Data.txt").write_text("{}\n")
    (tmp_path / "MarketWatchSymbols.txt").write_text("EURUSD.pr\n")
    return tmp_path


def _real_feed(tmp_path):
    directory = _dwx_dir(tmp_path)
    (directory / "DWX_Market_Data.txt").write_text('{"EURUSD.pr": {"bid": 1.1, "ask": 1.2}}')
    (directory / "DWX_Bar_Data.txt").write_text(
        '{"EURUSD.pr_M5": {"time": "2026.09.25 20:55", "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15}}'
    )
    timestamps = ["2026.09.25 20:45", "2026.09.25 20:50", "2026.09.25 20:55"]
    historic = {
        "EURUSD.pr_M5": {
            stamp: {"open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15, "tick_volume": 10}
            for stamp in timestamps
        }
    }
    (directory / "DWX_Historic_Data.txt").write_text(json.dumps(historic))
    return directory


def test_paper_runner_registra_ordem_simulada_sem_mutacao(tmp_path, monkeypatch):
    from paper import run

    monkeypatch.setattr(
        run,
        "evaluate_entry",
        lambda _candles: SimpleNamespace(direction="buy", entry_price=Decimal("101.25")),
    )
    ledger = tmp_path / "ledger.json"
    report = tmp_path / "report.md"

    run.run_pilot(
        _dwx_dir(tmp_path / "share"),
        iterations=1,
        ledger_path=ledger,
        report_path=report,
        mode="synthetic",
    )
    entries = json.loads(ledger.read_text())

    assert len(entries) == 1
    assert entries[0]["status"] == "simulated"
    assert entries[0]["price_source"] == "synthetic_seed_20250925"
    assert not list((tmp_path / "share").glob("DWX_Commands_*.txt"))


def test_paper_real_rotula_preco_mt5_e_nao_usa_sintetico(tmp_path, monkeypatch):
    from paper import run

    monkeypatch.setattr(
        run,
        "evaluate_entry",
        lambda _candles: SimpleNamespace(direction="buy", entry_price=Decimal("1.15")),
    )
    ledger = tmp_path / "ledger-real.json"
    run.run_pilot(_real_feed(tmp_path / "real"), iterations=1, ledger_path=ledger, report_path=tmp_path / "real.md", mode="real")

    entries = json.loads(ledger.read_text())
    assert entries[0]["price_source"] == "mt5_real"
    assert "synthetic" not in entries[0]["price_source"]
