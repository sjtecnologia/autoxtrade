"""Engenharia de features para o modelo ML."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import joblib
import numpy as np
import pandas as pd

try:
    import pandas_ta as ta  # type: ignore
except ImportError:  # pragma: no cover
    ta = None  # type: ignore

logger = logging.getLogger(__name__)

# Lista canônica de features — mesma ordem em treino e inferência
FEATURE_COLUMNS: list[str] = [
    "RSI_14",
    "MACD_12_26_9",
    "MACDh_12_26_9",
    "MACDs_12_26_9",
    "BBL_20_2.0",
    "BBM_20_2.0",
    "BBU_20_2.0",
    "BBB_20_2.0",
    "BBP_20_2.0",
    "EMA_9",
    "EMA_21",
    "EMA_50",
    "EMA_200",
    "ATRr_14",
    "OBV",
    "STOCHk_14_3_3",
    "STOCHd_14_3_3",
    "ADX_14",
    "volume_ratio",
]

MODELS_DIR = Path(__file__).parent / "models"


class FeatureEngineer:
    """Calcula indicadores técnicos e prepara features para o modelo."""

    # ------------------------------------------------------------------
    # Cálculo de features
    # ------------------------------------------------------------------

    def calculate_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calcula todos os indicadores técnicos no DataFrame OHLCV.

        Args:
            df: DataFrame com colunas [timestamp, open, high, low, close, volume]

        Returns:
            DataFrame com features calculadas + colunas OHLCV originais.
            Linhas com NaN (primeiras N candles sem dados) são removidas.
        """
        if ta is None:  # pragma: no cover
            raise ImportError("pandas-ta não instalado. Execute: pip install pandas-ta")

        df = df.copy()

        # Garante que colunas de entrada estão em float64
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)

        # RSI
        df.ta.rsi(length=14, append=True)

        # MACD
        df.ta.macd(fast=12, slow=26, signal=9, append=True)

        # Bollinger Bands — pandas-ta pode gerar BBL_20_2.0_2.0 dependendo da versão;
        # normalizamos para os nomes canônicos usados em FEATURE_COLUMNS.
        bb = df.ta.bbands(length=20, std=2)
        if bb is not None:
            _bb_map = {
                "BBL_20_2.0": next((c for c in bb.columns if c.startswith("BBL_")), None),
                "BBM_20_2.0": next((c for c in bb.columns if c.startswith("BBM_")), None),
                "BBU_20_2.0": next((c for c in bb.columns if c.startswith("BBU_")), None),
                "BBB_20_2.0": next((c for c in bb.columns if c.startswith("BBB_")), None),
                "BBP_20_2.0": next((c for c in bb.columns if c.startswith("BBP_")), None),
            }
            for canonical, actual in _bb_map.items():
                if actual:
                    df[canonical] = bb[actual]

        # EMAs
        df.ta.ema(length=9, append=True)
        df.ta.ema(length=21, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.ema(length=200, append=True)

        # ATR
        df.ta.atr(length=14, append=True)

        # OBV
        df.ta.obv(append=True)

        # Stochastic
        df.ta.stoch(k=14, d=3, append=True)

        # ADX
        df.ta.adx(length=14, append=True)

        # Volume ratio
        df["volume_ratio"] = df["volume"] / df["volume"].rolling(20).mean()

        # Remover linhas com NaN em features obrigatórias
        available = [c for c in FEATURE_COLUMNS if c in df.columns]
        df = df.dropna(subset=available).reset_index(drop=True)

        return df

    # ------------------------------------------------------------------
    # Target (label)
    # ------------------------------------------------------------------

    def calculate_target(
        self,
        df: pd.DataFrame,
        forward_candles: int = 5,
        threshold: float = 0.005,
    ) -> pd.DataFrame:
        """Calcula o target (LONG / SHORT / NEUTRO) baseado no retorno futuro.

        Args:
            df: DataFrame com coluna `close`
            forward_candles: Número de candles à frente para calcular retorno
            threshold: Limiar de retorno (0.5% = 0.005)

        Returns:
            DataFrame com coluna `target` adicionada (sem as últimas N linhas).
        """
        df = df.copy()
        future_return = (df["close"].shift(-forward_candles) - df["close"]) / df["close"]

        df["target"] = np.where(
            future_return > threshold,
            "LONG",
            np.where(future_return < -threshold, "SHORT", "NEUTRO"),
        )
        # Remover últimas N linhas onde o retorno futuro não existe
        df = df.iloc[:-forward_candles].copy()
        df = df.reset_index(drop=True)
        return df

    # ------------------------------------------------------------------
    # Normalização
    # ------------------------------------------------------------------

    def fit_transform(
        self, df: pd.DataFrame, version: str
    ) -> tuple[pd.DataFrame, object]:
        """Treina StandardScaler e normaliza features.

        Args:
            df: DataFrame com FEATURE_COLUMNS presentes
            version: Versão do modelo para nomear o arquivo do scaler

        Returns:
            Tupla (df_normalizado, scaler)
        """
        from sklearn.preprocessing import StandardScaler

        available = [c for c in FEATURE_COLUMNS if c in df.columns]
        df = df.copy()

        scaler = StandardScaler()
        df[available] = scaler.fit_transform(df[available])

        # Salvar scaler
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        scaler_path = MODELS_DIR / f"{version}_scaler.pkl"
        joblib.dump(scaler, scaler_path)
        logger.info("Scaler salvo em %s", scaler_path)

        return df, scaler

    def transform(self, df: pd.DataFrame, scaler: object) -> pd.DataFrame:
        """Aplica scaler já treinado (para inferência).

        Args:
            df: DataFrame com FEATURE_COLUMNS
            scaler: StandardScaler treinado

        Returns:
            DataFrame normalizado.
        """
        available = [c for c in FEATURE_COLUMNS if c in df.columns]
        df = df.copy()
        df[available] = scaler.transform(df[available])
        return df
