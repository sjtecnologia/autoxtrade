"""Critérios de continuidade, frescor e identidade de instrumentos."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.data_collector import DataCollector
from ml.mt5_data_collector import MT5DataCollector
from trading.contracts import CapabilityUnavailable


def _exchange(*symbols: str):
    return SimpleNamespace(symbols=list(symbols), markets={})


def _ohlcv(timestamp: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([timestamp], utc=True),
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [10.0],
        }
    )


def test_dados_velhos_sao_sinalizados():
    collector = DataCollector(freshness_window_seconds=3600)
    result, report = collector.validate_and_clean(
        _ohlcv("2026-09-24T00:00:00Z"),
        now=datetime(2026, 9, 25, tzinfo=timezone.utc),
    )

    assert len(result) == 1
    assert report.stale_data is True
    assert report.freshness_age_seconds is not None


def test_simbolo_invalido_e_rejeitado():
    collector = DataCollector(exchange=_exchange("BTC/USDT"))

    with pytest.raises(CapabilityUnavailable, match="não suportado"):
        collector.validate_symbol("FAKE/USDT")


def test_simbolo_sintetico_exige_contexto_de_instrumento():
    collector = MT5DataCollector("/tmp/mt5-read-only")

    with pytest.raises(CapabilityUnavailable, match="instrumento"):
        collector.validate_symbol("SYNTH_A/SYNTH_B", "FOREX")

    contextualized = MT5DataCollector(
        "/tmp/mt5-read-only", supported_symbols={"SYNTH_A/SYNTH_B"}
    )
    contextualized.validate_symbol("SYNTH_A/SYNTH_B", "FOREX")


def test_dados_fora_de_ordem_sao_sinalizados_e_ordenados():
    df = pd.concat(
        [_ohlcv("2026-09-25T01:00:00Z"), _ohlcv("2026-09-25T00:00:00Z")],
        ignore_index=True,
    )
    result, report = DataCollector().validate_and_clean(df)

    assert report.out_of_order_detected is True
    assert result["timestamp"].is_monotonic_increasing
