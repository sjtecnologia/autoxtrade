"""Testes unitários do DataCollector."""
import datetime

import pandas as pd
import pytest

from ml.data_collector import DataCollector, DataQualityReport


def _make_df(n: int = 10, freq: str = "1h") -> pd.DataFrame:
    """Cria DataFrame OHLCV sintético limpo."""
    times = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "timestamp": times,
        "open": [100.0 + i for i in range(n)],
        "high": [101.0 + i for i in range(n)],
        "low": [99.0 + i for i in range(n)],
        "close": [100.5 + i for i in range(n)],
        "volume": [1000.0 + i for i in range(n)],
    })


def test_validate_remove_duplicatas():
    df = _make_df(10)
    # Duplicar última linha
    df = pd.concat([df, df.iloc[[-1]]], ignore_index=True)
    assert len(df) == 11

    collector = DataCollector()
    result, report = collector.validate_and_clean(df, "1h")

    assert len(result) == 10
    assert report.duplicates_removed == 1


def test_validate_remove_nulos():
    df = _make_df(10)
    df.loc[3, "close"] = None

    collector = DataCollector()
    result, report = collector.validate_and_clean(df, "1h")

    assert len(result) == 9
    assert report.nulls_removed == 1


def test_validate_ordena_por_timestamp():
    df = _make_df(10)
    # Embaralhar
    df = df.sample(frac=1, random_state=42).reset_index(drop=True)

    collector = DataCollector()
    result, _ = collector.validate_and_clean(df, "1h")

    assert list(result["timestamp"]) == sorted(result["timestamp"])


def test_detecta_gap_grande():
    """Gap de 5 candles consecutivos → registrado em large_gaps."""
    df = _make_df(20, "1h")
    # Inserir gap: remover linhas 5-9 (5 candles faltantes)
    df = pd.concat([df.iloc[:5], df.iloc[10:]], ignore_index=True)

    collector = DataCollector()
    result, report = collector.validate_and_clean(df, "1h")

    assert len(report.large_gaps) >= 1


def test_detecta_gap_pequeno():
    """Gap de 2 candles consecutivos → small_gaps_filled."""
    df = _make_df(20, "1h")
    # Remover 2 linhas → gap pequeno (2 candles perdidos)
    df = pd.concat([df.iloc[:5], df.iloc[7:]], ignore_index=True)

    collector = DataCollector()
    result, report = collector.validate_and_clean(df, "1h")

    assert report.small_gaps_filled > 0
    assert len(report.large_gaps) == 0
    assert len(result) == 20


def test_df_vazio_retorna_vazio():
    df = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    collector = DataCollector()
    result, report = collector.validate_and_clean(df, "1h")
    assert len(result) == 0
    assert report.duplicates_removed == 0
    assert report.nulls_removed == 0
