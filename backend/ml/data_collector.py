"""Coleta e armazenamento de dados históricos OHLCV via CCXT."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import ccxt
import pandas as pd

from trading.contracts import CapabilityUnavailable

logger = logging.getLogger(__name__)

# Mapeamento de timeframe → segundos (para detecção de gaps)
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

DATA_DIR = Path(__file__).parent / "data"


class DataQualityReport:
    def __init__(self) -> None:
        self.duplicates_removed: int = 0
        self.nulls_removed: int = 0
        self.small_gaps_filled: int = 0
        self.large_gaps: list[str] = []  # timestamps dos gaps grandes (ISO string)
        self.out_of_order_detected: bool = False
        self.stale_data: bool = False
        self.freshness_age_seconds: float | None = None

    @property
    def large_gaps_detected(self) -> int:
        """Quantidade de gaps grandes que exigem tratamento explícito."""
        return len(self.large_gaps)


class DataCollector:
    """Baixa dados OHLCV históricos da Binance e armazena em Parquet."""

    def __init__(
        self,
        exchange: Optional[ccxt.Exchange] = None,
        *,
        supported_symbols: Optional[set[str] | list[str]] = None,
        freshness_window_seconds: int | None = None,
        small_gap_tolerance: int = 2,
    ) -> None:
        self._exchange = exchange or ccxt.binance({"enableRateLimit": True})
        self._supported_symbols = set(supported_symbols or ())
        self._freshness_window_seconds = freshness_window_seconds
        if small_gap_tolerance < 1:
            raise ValueError("small_gap_tolerance deve ser positivo")
        self._small_gap_tolerance = small_gap_tolerance

    def validate_symbol(self, symbol: str) -> None:
        """Recusa símbolos fora do catálogo conhecido do conector."""
        known = self._supported_symbols or set(getattr(self._exchange, "symbols", ()) or ())
        if not known and getattr(self._exchange, "markets", None):
            known = set(self._exchange.markets)
        if not symbol or symbol not in known:
            raise CapabilityUnavailable(
                f"Símbolo não suportado ou sem contexto de venue/instrumento: {symbol!r}"
            )

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_historical(
        self,
        symbol: str,
        timeframe: str,
        since: int,
        until: Optional[int] = None,
    ) -> pd.DataFrame:
        """Baixa candles OHLCV de `since` até `until` (timestamps em ms).

        Args:
            symbol: Par de trading, ex. "BTC/USDT"
            timeframe: Timeframe, ex. "1h"
            since: Timestamp inicial em milissegundos (UTC)
            until: Timestamp final em ms; usa `now` se None

        Returns:
            DataFrame com colunas [timestamp, open, high, low, close, volume]
        """
        self.validate_symbol(symbol)
        if until is None:
            until = self._exchange.milliseconds()

        all_ohlcv: list[list] = []
        current_since = since

        while current_since < until:
            batch = self._exchange.fetch_ohlcv(
                symbol, timeframe, since=current_since, limit=1000
            )
            if not batch:
                break
            all_ohlcv.extend(batch)
            last_ts = batch[-1][0]
            if last_ts >= until:
                break
            current_since = last_ts + 1  # próximo ms

        if not all_ohlcv:
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )

        df = pd.DataFrame(
            all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        # Filtrar pelo `until`
        until_dt = pd.Timestamp(until, unit="ms", tz="UTC")
        df = df[df["timestamp"] <= until_dt].copy()
        return df

    def download_all(
        self,
        symbols: list[str],
        timeframes: list[str],
        years_back: int = 2,
    ) -> None:
        """Baixa e salva dados para todas combinações symbol × timeframe.

        Args:
            symbols: Lista de pares, ex. ["BTC/USDT", "ETH/USDT"]
            timeframes: Lista de timeframes, ex. ["5m", "1h", "1d"]
            years_back: Quantos anos de histórico baixar
        """
        import time

        since_ms = self._years_back_ms(years_back)
        total = len(symbols) * len(timeframes)
        done = 0

        for symbol in symbols:
            for tf in timeframes:
                done += 1
                logger.info("[%d/%d] Baixando %s %s ...", done, total, symbol, tf)
                try:
                    df = self.download_historical(symbol, tf, since=since_ms)
                    if df.empty:
                        logger.warning("Sem dados para %s %s", symbol, tf)
                        continue
                    df, report = self.validate_and_clean(df, tf)
                    self._save_parquet(symbol, tf, df)
                    logger.info(
                        "  → %d candles | dups=%d nulls=%d gaps_fill=%d gaps_large=%d",
                        len(df),
                        report.duplicates_removed,
                        report.nulls_removed,
                        report.small_gaps_filled,
                        len(report.large_gaps),
                    )
                    # Respeitar rate limit
                    time.sleep(0.3)
                except Exception as exc:  # pragma: no cover
                    logger.error("Erro baixando %s %s: %s", symbol, tf, exc)

    # ------------------------------------------------------------------
    # Validação e limpeza
    # ------------------------------------------------------------------

    def validate_and_clean(
        self,
        df: pd.DataFrame,
        timeframe: str = "1h",
        *,
        now: datetime | pd.Timestamp | None = None,
        freshness_window_seconds: int | None = None,
    ) -> tuple[pd.DataFrame, DataQualityReport]:
        """Valida e limpa DataFrame OHLCV.

        Returns:
            Tupla (df_limpo, report)
        """
        report = DataQualityReport()
        original_len = len(df)

        # 1. Remover duplicatas por timestamp
        df = df.drop_duplicates(subset="timestamp")
        report.duplicates_removed = original_len - len(df)

        # 2. Remover nulos em colunas OHLCV essenciais
        null_timestamps = set(
            df.loc[df[["open", "high", "low", "close", "volume"]].isna().any(axis=1), "timestamp"]
        )
        before_null = len(df)
        df = df.dropna(subset=["open", "high", "low", "close", "volume"])
        report.nulls_removed = before_null - len(df)

        # 3. Sinalizar ordem recebida antes de ordenar para consumo downstream
        report.out_of_order_detected = not df["timestamp"].is_monotonic_increasing
        df = df.sort_values("timestamp").reset_index(drop=True)

        freshness_window = (
            freshness_window_seconds
            if freshness_window_seconds is not None
            else self._freshness_window_seconds
        )
        if freshness_window is not None and len(df):
            reference = pd.Timestamp(now or datetime.now(timezone.utc))
            if reference.tzinfo is None:
                reference = reference.tz_localize("UTC")
            age = (reference - pd.Timestamp(df["timestamp"].iloc[-1])).total_seconds()
            report.freshness_age_seconds = max(age, 0.0)
            report.stale_data = age > freshness_window

        # 4. Detectar e tratar gaps
        tf_seconds = TIMEFRAME_SECONDS.get(timeframe, 3600)
        expected_delta = pd.Timedelta(seconds=tf_seconds)
        if len(df) >= 2:
            time_diffs = df["timestamp"].diff()
            gap_mask = time_diffs > expected_delta

            for idx in df[gap_mask].index:
                gap_start = df.loc[idx - 1, "timestamp"]
                gap_end = df.loc[idx, "timestamp"]
                gap_size = (gap_end - gap_start) / expected_delta
                missing_count = int(round(gap_size)) - 1

                if missing_count <= self._small_gap_tolerance:
                    inserted = []
                    previous = df.loc[idx - 1].copy()
                    for timestamp in pd.date_range(
                        gap_start + expected_delta,
                        gap_end - expected_delta,
                        freq=expected_delta,
                    ):
                        if timestamp in null_timestamps:
                            continue
                        row = previous.copy()
                        row["timestamp"] = timestamp
                        row["volume"] = 0.0
                        inserted.append(row)
                    if inserted:
                        df = pd.concat([df, pd.DataFrame(inserted)], ignore_index=True)
                        report.small_gaps_filled += len(inserted)
                else:
                    report.large_gaps.append(gap_start.isoformat())
                    logger.warning(
                        "[data_quality] Gap de %.0f candles em %s → não preenchido",
                        gap_size,
                        gap_start.isoformat(),
                    )

        if report.small_gaps_filled:
            df = df.sort_values("timestamp").reset_index(drop=True)
        return df, report

    # ------------------------------------------------------------------
    # Atualização incremental
    # ------------------------------------------------------------------

    def update_incremental(self, symbol: str, timeframe: str) -> int:
        """Baixa apenas candles novos desde o último timestamp salvo.

        Returns:
            Número de novos candles adicionados.
        """
        path = self._parquet_path(symbol, timeframe)
        if not path.exists():
            logger.warning("Arquivo não encontrado: %s — baixando completo.", path)
            since_ms = self._years_back_ms(2)
            df = self.download_historical(symbol, timeframe, since=since_ms)
            if df.empty:
                return 0
            df, _ = self.validate_and_clean(df, timeframe)
            self._save_parquet(symbol, timeframe, df)
            return len(df)

        existing = pd.read_parquet(path, engine="pyarrow")
        last_ts: pd.Timestamp = existing["timestamp"].max()
        since_ms = int(last_ts.timestamp() * 1000) + 1  # +1ms

        new_df = self.download_historical(symbol, timeframe, since=since_ms)
        if new_df.empty:
            logger.info("[incremental] Sem novos candles para %s %s", symbol, timeframe)
            return 0

        combined = pd.concat([existing, new_df], ignore_index=True)
        combined, _ = self.validate_and_clean(combined, timeframe)
        self._save_parquet(symbol, timeframe, combined)
        added = len(new_df)
        logger.info("[incremental] +%d candles para %s %s", added, symbol, timeframe)
        return added

    # ------------------------------------------------------------------
    # I/O Parquet
    # ------------------------------------------------------------------

    def _parquet_path(self, symbol: str, timeframe: str) -> Path:
        symbol_dir = symbol.replace("/", "_")
        return DATA_DIR / symbol_dir / f"{timeframe}.parquet"

    def _save_parquet(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        path = self._parquet_path(symbol, timeframe)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(path, engine="pyarrow", compression="snappy", index=False)
        logger.debug("Salvo: %s (%d linhas)", path, len(df))

    def load_parquet(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Carrega dados Parquet salvo para o par/timeframe."""
        path = self._parquet_path(symbol, timeframe)
        if not path.exists():
            raise FileNotFoundError(f"Dados não encontrados: {path}")
        return pd.read_parquet(path, engine="pyarrow")

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------

    @staticmethod
    def _years_back_ms(years: int) -> int:
        import time

        now_ms = int(time.time() * 1000)
        year_ms = years * 365 * 24 * 3600 * 1000
        return now_ms - year_ms


# ---------------------------------------------------------------------------
# CLI — python -m ml.data_collector --all --years 2
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Download de dados históricos OHLCV")
    parser.add_argument("--all", action="store_true", help="Baixar todos os pares configurados")
    parser.add_argument("--symbol", default=None, help="Par específico, ex. BTC/USDT")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--years", type=int, default=2)
    args = parser.parse_args()

    collector = DataCollector()

    if args.all:
        symbols = ["BTC/USDT", "ETH/USDT"]
        timeframes = ["5m", "1h", "1d"]
        collector.download_all(symbols, timeframes, years_back=args.years)
    elif args.symbol:
        since = collector._years_back_ms(args.years)
        df = collector.download_historical(args.symbol, args.timeframe, since=since)
        df, report = collector.validate_and_clean(df, args.timeframe)
        collector._save_parquet(args.symbol, args.timeframe, df)
        print(f"Salvo {len(df)} candles para {args.symbol} {args.timeframe}")
    else:
        parser.print_help()
