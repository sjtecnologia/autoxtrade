"""Testes unitários para B3FeatureEngineer."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

import pandas as pd
import pytest

from ml.b3_feature_engineer import B3FeatureEngineer, B3_FEATURE_COLUMNS, B3_HOLIDAYS


@pytest.fixture
def engineer() -> B3FeatureEngineer:
    return B3FeatureEngineer()


@pytest.fixture
def sample_df() -> pd.DataFrame:
    """DataFrame mínimo com 250 candles para o FeatureEngineer base funcionar."""
    import numpy as np

    n = 250
    rng = pd.date_range("2026-01-05 09:00:00", periods=n, freq="1h", tz="UTC")
    np.random.seed(42)
    close = 100 + np.cumsum(np.random.randn(n) * 0.5)
    return pd.DataFrame(
        {
            "timestamp": rng,
            "open": close * 0.999,
            "high": close * 1.001,
            "low": close * 0.998,
            "close": close,
            "volume": np.random.uniform(1000, 5000, n),
        }
    )


# ---------------------------------------------------------------------------
# Feature de sessão B3
# ---------------------------------------------------------------------------

class TestSessionFeature:
    def test_abertura_hour_9(self, engineer, sample_df):
        df = engineer.calculate_features(sample_df)
        abertura_rows = df[df["session_abertura"] == 1]
        assert len(abertura_rows) > 0

    def test_session_columns_exist(self, engineer, sample_df):
        df = engineer.calculate_features(sample_df)
        for col in ("session_abertura", "session_meio_dia", "session_fechamento"):
            assert col in df.columns, f"Coluna {col!r} ausente"

    def test_session_exclusive(self, engineer, sample_df):
        """Cada candle pode estar em no máximo uma sessão."""
        df = engineer.calculate_features(sample_df)
        soma = df["session_abertura"] + df["session_meio_dia"] + df["session_fechamento"]
        # Zero (fora do horário) ou um (dentro de uma sessão)
        assert soma.max() <= 1, "Candle em mais de uma sessão simultaneamente"

    def test_session_values_binary(self, engineer, sample_df):
        df = engineer.calculate_features(sample_df)
        for col in ("session_abertura", "session_meio_dia", "session_fechamento"):
            assert set(df[col].unique()).issubset({0, 1}), f"{col} tem valores != 0/1"


# ---------------------------------------------------------------------------
# Feature de dia da semana
# ---------------------------------------------------------------------------

class TestDayOfWeekFeature:
    def test_dow_columns_exist(self, engineer, sample_df):
        df = engineer.calculate_features(sample_df)
        for col in ("dow_seg", "dow_ter", "dow_qua", "dow_qui", "dow_sex"):
            assert col in df.columns, f"Coluna {col!r} ausente"

    def test_known_monday(self, engineer):
        """Garante que dow_seg=1 aparece para candles de segunda-feira."""
        import numpy as np

        # 300 candles a partir de uma quarta (centro de semana) — assim
        # as primeiras linhas removidas pelo dropna já terão passado, e
        # ainda haverá segundas subsequentes no range.
        n = 300
        rng = pd.date_range("2026-01-07 08:00:00", periods=n, freq="1h", tz="UTC")
        np.random.seed(0)
        close = 100 + np.cumsum(np.random.randn(n) * 0.5)
        df = pd.DataFrame({
            "timestamp": rng, "open": close * 0.999, "high": close * 1.001,
            "low": close * 0.998, "close": close, "volume": np.ones(n) * 1000,
        })
        out = engineer.calculate_features(df)
        mondays = out[out["dow_seg"] == 1]
        assert len(mondays) > 0, "Nenhum candle de segunda-feira encontrado"

    def test_dow_exclusive(self, engineer, sample_df):
        """Cada candle pertence a exatamente 1 dia da semana (seg-sex) ou nenhum (fim de semana)."""
        df = engineer.calculate_features(sample_df)
        dow_cols = ["dow_seg", "dow_ter", "dow_qua", "dow_qui", "dow_sex"]
        soma = df[dow_cols].sum(axis=1)
        assert soma.max() <= 1, "Candle marcado em mais de um dia da semana"

    def test_dow_values_binary(self, engineer, sample_df):
        df = engineer.calculate_features(sample_df)
        for col in ("dow_seg", "dow_ter", "dow_qua", "dow_qui", "dow_sex"):
            assert set(df[col].unique()).issubset({0, 1}), f"{col} tem valores != 0/1"


# ---------------------------------------------------------------------------
# Feature days_to_expiry
# ---------------------------------------------------------------------------

class TestDaysToExpiry:
    def test_non_future_returns_minus_one(self, engineer):
        assert engineer.days_to_expiry("EURUSD", date(2026, 1, 5)) == -1

    def test_non_future_petr4(self, engineer):
        assert engineer.days_to_expiry("PETR4", date(2026, 1, 5)) == -1

    def test_future_winm25_before_expiry(self, engineer):
        """WINM25 vence em 2025-06-18; consultado em 2025-01-05."""
        val = engineer.days_to_expiry("WINM25", date(2025, 1, 5))
        assert val > 0

    def test_future_on_expiry_day(self, engineer):
        val = engineer.days_to_expiry("WINM25", date(2025, 6, 18))
        assert val == 0

    def test_future_after_expiry(self, engineer):
        val = engineer.days_to_expiry("WINM25", date(2025, 7, 1))
        assert val == 0  # max(0, negativo) = 0


# ---------------------------------------------------------------------------
# is_b3_open
# ---------------------------------------------------------------------------

class TestIsB3Open:
    def test_monday_10h(self):
        dt = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)  # 10h BRT
        assert B3FeatureEngineer.is_b3_open(dt) is True

    def test_monday_18h_closed(self):
        # 18h BRT = 21h UTC
        dt = datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc)
        assert B3FeatureEngineer.is_b3_open(dt) is False

    def test_saturday_closed(self):
        dt = datetime(2026, 1, 10, 13, 0, tzinfo=timezone.utc)  # sábado
        assert B3FeatureEngineer.is_b3_open(dt) is False

    def test_sunday_closed(self):
        dt = datetime(2026, 1, 11, 13, 0, tzinfo=timezone.utc)  # domingo
        assert B3FeatureEngineer.is_b3_open(dt) is False

    def test_holiday_closed(self):
        """1 de janeiro é feriado."""
        dt = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        assert B3FeatureEngineer.is_b3_open(dt) is False


# ---------------------------------------------------------------------------
# Features base ainda presentes
# ---------------------------------------------------------------------------

class TestBaseFeaturesPreserved:
    def test_rsi_present(self, engineer, sample_df):
        df = engineer.calculate_features(sample_df)
        assert "RSI_14" in df.columns

    def test_macd_present(self, engineer, sample_df):
        df = engineer.calculate_features(sample_df)
        assert "MACD_12_26_9" in df.columns

    def test_no_nan_in_feature_columns(self, engineer, sample_df):
        from ml.b3_feature_engineer import B3_FEATURE_COLUMNS
        df = engineer.calculate_features(sample_df)
        available = [c for c in B3_FEATURE_COLUMNS if c in df.columns]
        assert df[available].isna().sum().sum() == 0, "NaN encontrado nas features"


# ---------------------------------------------------------------------------
# get_feature_columns
# ---------------------------------------------------------------------------

def test_get_feature_columns_includes_b3(engineer):
    cols = engineer.get_feature_columns()
    assert "session_abertura" in cols
    assert "dow_seg" in cols
    assert "RSI_14" in cols  # base também presente
