"""
DWX Connect Connector — comunicação baseada em arquivos com MT5.

Usa o protocolo do EA oficial:
  https://github.com/darwinex/dwxconnect   (mql/dwx_server_mt5.mq5)

O EA escreve/lê arquivos JSON no diretório MQL5/Files/DWX/ do MT5.
Python também lê/escreve nesse mesmo diretório.

Para setup cross-machine (Mac + Windows MT5):
  - Compartilhe a pasta MQL5/Files via SMB no Windows
  - Monte no Mac: sudo mount -t smbfs //IP-WINDOWS/MT5Files /mnt/mt5
  - Configure MT5_FILES_DIR=/mnt/mt5 no .env
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from decimal import Decimal
from os.path import exists, join
from traceback import print_exc
from typing import Any

from trading.connectors.simulated import SimulatedConnector
from trading.contracts import Capability, CapabilityUnavailable
from trading.safety import require_new_exposure, require_position_management
from trading.connectors.types import (
    Balance,
    OHLCVCandle,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)

logger = logging.getLogger(__name__)

_TF_MAP: dict[str, str] = {
    "1m": "M1", "3m": "M3", "5m": "M5", "15m": "M15", "30m": "M30",
    "1h": "H1", "2h": "H2", "4h": "H4", "6h": "H6", "8h": "H8",
    "12h": "H12", "1d": "D1", "1w": "W1",
    "M1": "M1", "M5": "M5", "M15": "M15", "M30": "M30",
    "H1": "H1", "H4": "H4", "D1": "D1", "W1": "W1",
}

_NUM_COMMAND_FILES = 50


class DWXConnector(SimulatedConnector):
    """
    Connector para MetaTrader 5 via DWX Connect (comunicação por arquivos).

    Parâmetros:
        mt5_files_dir  — caminho para o diretório MQL5/Files do MT5
                         (local ou montado via SMB/NFS)
        sleep_delay    — intervalo de polling em segundos (padrão 5ms)
        max_retry_s    — segundos máximos para tentar escrever um comando
    """

    def __init__(
        self,
        mt5_files_dir: str,
        sleep_delay: float = 0.005,
        max_retry_s: int = 10,
        *, context=None, transport=None,
    ) -> None:
        self._bind(context, transport)
        self.prices = transport.prices if transport is not None else {}
        self._dir = mt5_files_dir
        self._dwx_dir = join(mt5_files_dir, "DWX")
        self._sleep = sleep_delay
        self._max_retry = max_retry_s
        # Usa offset baseado em timestamp para evitar reutilizar IDs já
        # processados pelo EA em sessões anteriores (EA ignora ID <= lastCommandId).
        self._cmd_id = int(time.time()) % 99_000
        self._lock = threading.Lock()
        self._active = False

        self.open_orders: dict[str, Any] = {}
        self.account_info: dict[str, Any] = {}
        self.market_data: dict[str, Any] = {}
        self.historic_data: dict[str, Any] = {}

        self._loop: asyncio.AbstractEventLoop | None = None
        self._order_event: asyncio.Event | None = None
        self._historic_event: asyncio.Event | None = None

        self._last_orders_str = ""
        self._last_market_str = ""
        self._last_historic_str = ""

        self._path_orders = join(self._dwx_dir, "DWX_Orders.txt")
        self._path_market = join(self._dwx_dir, "DWX_Market_Data.txt")
        self._path_historic = join(self._dwx_dir, "DWX_Historic_Data.txt")
        self._path_cmd_prefix = join(self._dwx_dir, "DWX_Commands_")

    async def connect(self) -> None:
        self._check_use(Capability.QUERY)

    async def disconnect(self) -> None:
        self.retire()

    async def close(self) -> None:
        self.retire()

    async def get_balance(self) -> dict[str, Balance]:
        self._check_use(Capability.QUERY)
        info = self.account_info
        if not info:
            return {}
        currency = str(info.get("currency", "USD"))
        equity = Decimal(str(info.get("equity", 0)))
        balance = Decimal(str(info.get("balance", 0)))
        free_margin = Decimal(str(info.get("free_margin", 0)))
        locked = max(equity - free_margin, Decimal("0"))
        return {currency: Balance(asset=currency, free=balance, locked=locked)}

    async def get_ohlcv(
        self, symbol: str, timeframe: str = "H1", limit: int = 200
    ) -> list[OHLCVCandle]:
        tf = _TF_MAP.get(timeframe, timeframe.upper())
        end_ts = int(time.time())
        _tf_secs = {
            "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
            "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800,
        }
        secs = _tf_secs.get(tf, 3600)
        start_ts = end_ts - limit * secs * 2

        key = f"{symbol}_{tf}"

        # Limpa dados obsoletos deste key antes de enviar o comando
        self.historic_data.pop(key, None)
        if self._historic_event:
            self._historic_event.clear()

        self._write_command("GET_HISTORIC_DATA", f"{symbol},{tf},{start_ts},{end_ts}")

        # Aguarda até 180s polling o key específico (MT5 leva ~60-90s para buscar do broker)
        deadline = asyncio.get_event_loop().time() + 180.0
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.warning("Timeout aguardando historico de %s %s", symbol, tf)
                return []
            if self._historic_event:
                self._historic_event.clear()
            try:
                await asyncio.wait_for(self._historic_event.wait(), timeout=min(remaining, 2.0))
            except asyncio.TimeoutError:
                pass
            if key in self.historic_data:
                break

        raw = self.historic_data.get(key, {})

        def _parse_ts(ts_str: str) -> int:
            """Converte timestamp MT5 (float Unix ou string 'YYYY.MM.DD HH:MM') para int Unix."""
            try:
                return int(float(ts_str))
            except ValueError:
                # Formato MT5: '2026.03.23 01:00'
                from datetime import datetime, timezone
                dt = datetime.strptime(ts_str, "%Y.%m.%d %H:%M").replace(tzinfo=timezone.utc)
                return int(dt.timestamp())

        candles: list[OHLCVCandle] = [
            OHLCVCandle(
                timestamp=_parse_ts(ts_str),
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row.get("tick_volume", 0))),
            )
            for ts_str, row in raw.items()
        ]
        candles.sort(key=lambda c: c.timestamp)
        return candles[-limit:]

    async def place_oco_order(self, symbol, side, amount, take_profit_price, stop_price, stop_limit_price):
        require_position_management(self)
        raise CapabilityUnavailable("DWX OCO without position ticket is unsupported; use modify_position_protection")

    async def query_order(self, *, intent_id=None, order_id=None):
        self._check_use(Capability.QUERY)
        self._transport.refresh()
        return await super().query_order(intent_id=intent_id, order_id=order_id)

    async def list_positions(self):
        self._check_use(Capability.POSITIONS)
        self._transport.refresh()
        return await super().list_positions()

    async def list_pending_orders(self):
        self._check_use(Capability.PENDING)
        self._transport.refresh()
        return await super().list_pending_orders()

    async def get_current_price(self, symbol: str) -> Decimal:
        return await super().get_current_price(symbol)
        data = self.market_data.get(symbol, {})
        bid = float(data.get("bid", 0))
        ask = float(data.get("ask", 0))
        if bid and ask:
            return Decimal(str((bid + ask) / 2))
        return Decimal("0")

    async def subscribe_price_stream(self, symbols: list[str]) -> None:
        self._write_command("SUBSCRIBE_SYMBOLS", ",".join(symbols))

    def _poll_orders(self) -> None:
        while self._active:
            time.sleep(self._sleep)
            try:
                text = self._read_file(self._path_orders)
                if not text or text == self._last_orders_str:
                    continue
                self._last_orders_str = text
                data = json.loads(text)
                self.account_info = data.get("account_info", {})
                self.open_orders = data.get("orders", {})
                if self._loop and self._order_event:
                    self._loop.call_soon_threadsafe(self._order_event.set)
            except Exception:
                print_exc()

    def _poll_market(self) -> None:
        while self._active:
            time.sleep(self._sleep)
            try:
                text = self._read_file(self._path_market)
                if not text or text == self._last_market_str:
                    continue
                self._last_market_str = text
                self.market_data = json.loads(text)
            except Exception:
                print_exc()

    def _poll_historic(self) -> None:
        while self._active:
            time.sleep(self._sleep)
            try:
                text = self._read_file(self._path_historic)
                if not text or text == self._last_historic_str:
                    continue
                self._last_historic_str = text
                data = json.loads(text)
                for key, val in data.items():
                    self.historic_data[key] = val
                # NÃO deleta o arquivo — deletar cria cache negativo no SMB macOS
                # O EA sobrescreve o arquivo na próxima requisição
                if self._loop and self._historic_event:
                    self._loop.call_soon_threadsafe(self._historic_event.set)
            except Exception:
                print_exc()

    def _write_command(self, command: str, content: str) -> None:
        require_position_management(self)
        raise CapabilityUnavailable("Raw DWX writes unavailable; use typed operations and explicit local transport")

    @staticmethod
    def _read_file(path: str) -> str:
        # Força um readdir para invalidar cache negativo de SMB (macOS)
        try:
            os.listdir(os.path.dirname(path))
        except OSError:
            pass
        try:
            with open(path) as f:
                return f.read()
        except (IOError, PermissionError, FileNotFoundError, OSError):
            pass
        return ""

    @staticmethod
    def _map_order_type(side: OrderSide, ot: OrderType) -> str:
        if ot == OrderType.MARKET:
            return "buy" if side == OrderSide.BUY else "sell"
        if ot == OrderType.LIMIT:
            return "buylimit" if side == OrderSide.BUY else "selllimit"
        return "buystop" if side == OrderSide.BUY else "sellstop"

    @staticmethod
    def _build_order(ticket: str, od: dict) -> Order:
        otype = od.get("type", "buy")
        side = OrderSide.BUY if otype in ("buy", "buylimit", "buystop") else OrderSide.SELL
        lots = Decimal(str(od.get("lots", 0)))
        return Order(
            id=str(ticket),
            symbol=od.get("symbol", ""),
            side=side,
            type=OrderType.MARKET,
            status=OrderStatus.OPEN,
            amount=lots,
            price=Decimal(str(od.get("open_price", 0))),
            average=Decimal(str(od.get("open_price", 0))),
            filled=lots,
            remaining=Decimal("0"),
            timestamp=None,
            raw=od,
        )
