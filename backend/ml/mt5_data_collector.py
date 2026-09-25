"""Coleta de dados históricos OHLCV via DWXConnector (MT5 file-based)."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from trading.contracts import CapabilityUnavailable

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# Mapeamento timeframe → nome usado pelo DWXConnector / MT5
TIMEFRAME_MAP: dict[str, str] = {
    "1m": "M1",
    "3m": "M3",
    "5m": "M5",
    "15m": "M15",
    "30m": "M30",
    "1h": "H1",
    "2h": "H2",
    "4h": "H4",
    "6h": "H6",
    "8h": "H8",
    "12h": "H12",
    "1d": "D1",
    "1w": "W1",
}


class MT5DataCollector:
    """Baixa dados OHLCV históricos do MT5 via DWXConnector e salva em Parquet.

    Usa a mesma interface de `DataCollector` (Epic 4) mas lê via DWX Connect
    ao invés da Binance/CCXT. Compatível com Mac/Linux (sem lib MetaTrader5).
    """

    def __init__(
        self,
        mt5_files_dir: str,
        *,
        supported_symbols: Optional[set[str] | list[str]] = None,
    ) -> None:
        self._mt5_files_dir = mt5_files_dir
        self._supported_symbols = set(supported_symbols or ())

    def validate_symbol(self, symbol: str, market: str) -> None:
        """Exige símbolo descoberto no instrumento/venue MT5 configurado."""
        if market not in {"FOREX", "B3"}:
            raise CapabilityUnavailable(f"Venue MT5 inválido ou ausente: {market!r}")
        if not symbol or (self._supported_symbols and symbol not in self._supported_symbols):
            raise CapabilityUnavailable(
                f"Símbolo MT5 não suportado ou ambíguo para {market}: {symbol!r}"
            )
        if not self._supported_symbols and "/" in symbol:
            raise CapabilityUnavailable(
                f"Símbolo sintético exige contexto explícito de instrumento: {symbol!r}"
            )

    # ------------------------------------------------------------------
    # Download principal
    # ------------------------------------------------------------------

    def download_historical(
        self,
        symbol: str,
        timeframe: str,
        market: str = "FOREX",
        n_bars: int = 5000,
    ) -> pd.DataFrame:
        """Baixa até `n_bars` candles OHLCV do MT5 e retorna DataFrame padronizado.

        Args:
            symbol: Símbolo MT5, ex. "EURUSD", "PETR4"
            timeframe: Timeframe no formato curto, ex. "1h", "5m", "1d"
            market: "FOREX" ou "B3"
            n_bars: Número de candles a buscar

        Returns:
            DataFrame com colunas [timestamp, open, high, low, close, volume]
        """
        return asyncio.run(self._download_async(symbol, timeframe, market, n_bars))

    async def _download_async(
        self, symbol: str, timeframe: str, market: str, n_bars: int
    ) -> pd.DataFrame:
        self.validate_symbol(symbol, market)
        from trading.connectors.dwx import DWXConnector

        tf_dwx = TIMEFRAME_MAP.get(timeframe, timeframe.upper())
        connector = DWXConnector(mt5_files_dir=self._mt5_files_dir)

        try:
            await connector.connect()
            await asyncio.sleep(0.5)
            candles = await connector.get_ohlcv(symbol, timeframe=tf_dwx, limit=n_bars)
        finally:
            await connector.disconnect()

        if not candles:
            logger.warning("[MT5DataCollector] Nenhum candle retornado: %s %s", symbol, timeframe)
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        rows = [
            {
                "timestamp": datetime.fromtimestamp(c.timestamp, tz=timezone.utc),
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": float(c.volume),
            }
            for c in candles
        ]
        df = pd.DataFrame(rows)
        df.sort_values("timestamp", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df

    # ------------------------------------------------------------------
    # Salvar / carregar Parquet
    # ------------------------------------------------------------------

    def save(self, df: pd.DataFrame, symbol: str, timeframe: str, market: str = "FOREX") -> Path:
        """Salva DataFrame em Parquet no diretório padrão.

        Caminho: `ml/data/{market}/{symbol}/{timeframe}.parquet`
        """
        safe_symbol = symbol.replace("/", "_")
        dest = DATA_DIR / market / safe_symbol / f"{timeframe}.parquet"
        dest.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(dest, index=False)
        logger.info("[MT5DataCollector] Salvo %d candles em %s", len(df), dest)
        return dest

    def load(self, symbol: str, timeframe: str, market: str = "FOREX") -> Optional[pd.DataFrame]:
        """Carrega Parquet salvo anteriormente. Retorna None se não existir."""
        safe_symbol = symbol.replace("/", "_")
        path = DATA_DIR / market / safe_symbol / f"{timeframe}.parquet"
        if not path.exists():
            return None
        return pd.read_parquet(path)

    async def update_incremental_async(
        self,
        symbol: str,
        timeframe: str,
        market: str = "FOREX",
        n_bars: int = 500,
    ) -> int:
        """Versão async de update_incremental — usar quando já há um event loop ativo."""
        existing = self.load(symbol, timeframe, market)
        new_df = await self._download_async(symbol, timeframe, market, n_bars)

        if new_df.empty:
            return 0

        if existing is None or existing.empty:
            self.save(new_df, symbol, timeframe, market)
            return len(new_df)

        combined = pd.concat([existing, new_df], ignore_index=True)
        combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
        combined.sort_values("timestamp", inplace=True)
        combined.reset_index(drop=True, inplace=True)

        added = len(combined) - len(existing)
        if added > 0:
            self.save(combined, symbol, timeframe, market)

        return max(added, 0)

    def update_incremental(
        self,
        symbol: str,
        timeframe: str,
        market: str = "FOREX",
        n_bars: int = 500,
    ) -> int:
        """Atualiza Parquet existente com candles novos. Retorna candles adicionados."""
        existing = self.load(symbol, timeframe, market)
        new_df = self.download_historical(symbol, timeframe, market, n_bars)

        if new_df.empty:
            return 0

        if existing is None or existing.empty:
            self.save(new_df, symbol, timeframe, market)
            return len(new_df)

        combined = pd.concat([existing, new_df], ignore_index=True)
        combined.drop_duplicates(subset=["timestamp"], keep="last", inplace=True)
        combined.sort_values("timestamp", inplace=True)
        combined.reset_index(drop=True, inplace=True)

        added = len(combined) - len(existing)
        if added > 0:
            self.save(combined, symbol, timeframe, market)

        return max(added, 0)
