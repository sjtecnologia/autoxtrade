"""Serving de modelos ML em produção (inferência em tempo real)."""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import joblib
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.60
MODELS_DIR = Path(__file__).parent / "models"


@dataclass
class PredictionResult:
    signal: str  # "LONG" | "SHORT" | "NEUTRO"
    confidence: float
    model_version: str


class ModelNotFoundError(Exception):
    """Nenhum modelo ativo encontrado para o símbolo."""


def _trusted_model_path(path_value: str | Path) -> Path:
    """Permite desserialização somente de artefatos dentro do diretório local."""
    root = MODELS_DIR.resolve()
    path = Path(path_value).resolve()
    if not path.is_relative_to(root):
        raise ModelNotFoundError(f"Artefato de modelo fora do diretório confiável: {path}")
    return path


@dataclass
class _LoadedModel:
    model: Any
    scaler: Any
    feature_columns: list[str]
    version: str


class ModelPredictor:
    """Carrega modelos em memória e serve predições thread-safe."""

    def __init__(self) -> None:
        self._models: dict[str, _LoadedModel] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Carregamento
    # ------------------------------------------------------------------

    async def load_model(self, symbol: str, db) -> None:
        """Carrega modelo ativo do banco para o cache em memória.

        Args:
            symbol: Par de trading, ex. "BTC/USDT"
            db: AsyncSession do SQLAlchemy
        """
        from sqlalchemy import select

        from db.models import MLModel

        result = await db.execute(
            select(MLModel)
            .where(MLModel.symbol == symbol, MLModel.status == "active")
            .order_by(MLModel.activated_at.desc())
            .limit(1)
        )
        record: Optional[MLModel] = result.scalar_one_or_none()

        if record is None:
            raise ModelNotFoundError(
                f"Nenhum modelo ativo encontrado para {symbol}"
            )

        model = joblib.load(_trusted_model_path(record.file_path))
        scaler = (
            joblib.load(_trusted_model_path(record.scaler_path))
            if record.scaler_path
            else None
        )
        feature_columns: list[str] = record.feature_list.get("columns", [])

        with self._lock:
            self._models[symbol] = _LoadedModel(
                model=model,
                scaler=scaler,
                feature_columns=feature_columns,
                version=record.version,
            )
        logger.info("[predictor] Modelo %s carregado para %s", record.version, symbol)

    async def reload(self, symbol: str, db) -> None:
        """Re-carrega modelo após ativação/rollback."""
        await self.load_model(symbol, db)

    # ------------------------------------------------------------------
    # Inferência
    # ------------------------------------------------------------------

    def predict(
        self,
        symbol: str,
        latest_ohlcv_df: pd.DataFrame,
        market: str = "CRIPTO",
    ) -> PredictionResult:
        """Gera predição para o último candle do DataFrame.

        Args:
            symbol: Par de trading
            latest_ohlcv_df: DataFrame com pelo menos 250 candles OHLCV
            market: "CRIPTO", "B3" ou "FOREX"

        Returns:
            PredictionResult com signal e confiança

        Raises:
            ModelNotFoundError: Se o modelo não estiver carregado
        """
        with self._lock:
            loaded = self._models.get(symbol)

        if loaded is None:
            raise ModelNotFoundError(
                f"Modelo não carregado para {symbol}. Chame load_model() primeiro."
            )

        # Seleciona feature engineer correto por mercado
        if market in ("B3", "FOREX"):
            from ml.b3_feature_engineer import B3FeatureEngineer

            fe = B3FeatureEngineer()
        else:
            from ml.feature_engineer import FeatureEngineer

            fe = FeatureEngineer()

        df_feat = fe.calculate_features(latest_ohlcv_df)

        if df_feat.empty:
            logger.warning("[predictor] Sem features calculadas para %s", symbol)
            return PredictionResult(
                signal="NEUTRO", confidence=0.0, model_version=loaded.version
            )

        available = [c for c in loaded.feature_columns if c in df_feat.columns]
        X_last = df_feat[available].iloc[[-1]]

        # Aplicar scaler se disponível
        if loaded.scaler is not None:
            X_last = loaded.scaler.transform(X_last)
        else:
            X_last = X_last.values

        # Predição probabilística
        proba = loaded.model.predict_proba(X_last)[0]
        classes = loaded.model.classes_

        confidence = float(np.max(proba))
        predicted_class = str(classes[int(np.argmax(proba))])

        # Filtrar por confiança mínima
        if confidence < MIN_CONFIDENCE:
            predicted_class = "NEUTRO"

        return PredictionResult(
            signal=predicted_class,
            confidence=round(confidence, 4),
            model_version=loaded.version,
        )


# Singleton global — carregado no startup da aplicação
predictor = ModelPredictor()
