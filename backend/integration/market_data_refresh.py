"""Refresh periódico e com debounce de dados DWX usando somente whitelist."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ml.data_collector import DataCollector
from integration.market_data_request import write_data_command


@dataclass(frozen=True)
class RefreshSnapshot:
    symbol: str
    timeframe: str
    tick: dict
    bar: dict
    tick_fresh: bool
    bar_fresh: bool
    commands_sent: tuple[str, ...]


class MarketDataUnavailable(RuntimeError):
    pass


class MarketDataRefresh:
    def __init__(
        self,
        directory: str | Path,
        *,
        freshness_seconds: int = 3600,
        command_id_start: int = 20000,
        request=write_data_command,
        clock=time.time,
    ) -> None:
        self.directory = Path(directory).expanduser()
        self.freshness_seconds = freshness_seconds
        self._next_command_id = command_id_start
        self._request = request
        self._clock = clock

    def _supported_symbols(self) -> set[str]:
        path = self.directory / "MarketWatchSymbols.txt"
        if not path.exists():
            return set()
        return set(path.read_text(encoding="utf-8", errors="ignore").split())

    def _read_json(self, filename: str) -> tuple[dict, float]:
        path = self.directory / filename
        if not path.exists():
            return {}, 0.0
        try:
            return json.loads(path.read_text(encoding="utf-8")), path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            return {}, path.stat().st_mtime

    def _fresh(self, modified: float) -> bool:
        return bool(modified) and self._clock() - modified <= self.freshness_seconds

    def _tick(self, symbol: str) -> tuple[dict, bool]:
        payload, modified = self._read_json("DWX_Market_Data.txt")
        tick = payload.get(symbol, {}) if isinstance(payload, dict) else {}
        valid = isinstance(tick, dict) and tick.get("bid") is not None and tick.get("ask") is not None
        return (tick if valid else {}), bool(valid and self._fresh(modified))

    def _bar(self, symbol: str, timeframe: str) -> tuple[dict, bool]:
        payload, modified = self._read_json("DWX_Bar_Data.txt")
        bar = payload.get(f"{symbol}_{timeframe}", {}) if isinstance(payload, dict) else {}
        valid = isinstance(bar, dict) and all(key in bar for key in ("time", "open", "high", "low", "close"))
        return (bar if valid else {}), bool(valid and self._fresh(modified))

    def _request_command(self, command: str, content: str) -> None:
        command_id = self._next_command_id
        self._next_command_id += 1
        self._request(
            self.directory,
            command=command,
            content=content,
            command_id=command_id,
            timeout_seconds=60,
            wait=True,
        )

    def ensure_fresh(self, symbol: str, timeframe: str = "M5") -> RefreshSnapshot:
        supported = self._supported_symbols()
        DataCollector(supported_symbols=supported).validate_symbol(symbol)
        tick, tick_fresh = self._tick(symbol)
        bar, bar_fresh = self._bar(symbol, timeframe)
        commands: list[str] = []
        if not tick_fresh:
            self._request_command("SUBSCRIBE_SYMBOLS", symbol)
            commands.append("SUBSCRIBE_SYMBOLS")
        if not bar_fresh:
            self._request_command("SUBSCRIBE_SYMBOLS_BAR_DATA", f"{symbol},{timeframe}")
            commands.append("SUBSCRIBE_SYMBOLS_BAR_DATA")
        tick, tick_fresh = self._tick(symbol)
        bar, bar_fresh = self._bar(symbol, timeframe)
        if not tick_fresh or not bar_fresh:
            raise MarketDataUnavailable(
                f"Feed não fresco após refresh: tick={tick_fresh} bar={bar_fresh} symbol={symbol}"
            )
        return RefreshSnapshot(symbol, timeframe, tick, bar, tick_fresh, bar_fresh, tuple(commands))
