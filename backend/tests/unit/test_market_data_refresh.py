import json
import time
from types import SimpleNamespace

import pytest

from integration.market_data_refresh import MarketDataRefresh, MarketDataUnavailable
from integration.market_data_request import CommandNotConsumed


def _feed(tmp_path, *, fresh=True):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "MarketWatchSymbols.txt").write_text("EURUSD.pr\n")
    (tmp_path / "DWX_Market_Data.txt").write_text(
        json.dumps({"EURUSD.pr": {"bid": 1.1, "ask": 1.2}})
    )
    (tmp_path / "DWX_Bar_Data.txt").write_text(
        json.dumps({"EURUSD.pr_M5": {"time": "2026.09.25 20:55", "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.15}})
    )
    if not fresh:
        old = time.time() - 7200
        for name in ("DWX_Market_Data.txt", "DWX_Bar_Data.txt"):
            import os
            os.utime(tmp_path / name, (old, old))
    return tmp_path


def test_refresh_debounce_nao_envia_comando_com_feed_fresco(tmp_path):
    calls = []
    refresh = MarketDataRefresh(_feed(tmp_path), request=lambda *args, **kwargs: calls.append(kwargs))

    result = refresh.ensure_fresh("EURUSD.pr", "M5")

    assert result.commands_sent == ()
    assert calls == []


def test_refresh_nao_sobrescreve_comando_pendente(tmp_path):
    directory = _feed(tmp_path, fresh=False)
    command = directory / "DWX_Commands_0.txt"
    command.write_bytes(b"pending")
    refresh = MarketDataRefresh(directory)

    with pytest.raises(CommandNotConsumed, match="já existe"):
        refresh.ensure_fresh("EURUSD.pr", "M5")

    assert command.read_bytes() == b"pending"
