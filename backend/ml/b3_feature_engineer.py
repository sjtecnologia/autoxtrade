"""Feature engineering estendido para B3 e Forex (mercados MT5)."""
from __future__ import annotations

import logging
from datetime import date, time, timedelta
from typing import Optional

import pandas as pd

from ml.feature_engineer import FEATURE_COLUMNS, FeatureEngineer

logger = logging.getLogger(__name__)

# Features base + features B3/Forex
B3_FEATURE_COLUMNS: list[str] = FEATURE_COLUMNS + [
    "session_abertura",
    "session_meio_dia",
    "session_fechamento",
    "dow_seg",
    "dow_ter",
    "dow_qua",
    "dow_qui",
    "dow_sex",
    "days_to_expiry",
]

# Feriados nacionais brasileiros (fixos e móveis aproximados)
# Atualizar anualmente — inclui apenas fixos aqui
B3_HOLIDAYS: list[date] = [
    # 2025
    date(2025, 1, 1),   # Confraternização Universal
    date(2025, 4, 18),  # Sexta-feira Santa
    date(2025, 4, 21),  # Tiradentes
    date(2025, 5, 1),   # Dia do Trabalho
    date(2025, 6, 19),  # Corpus Christi
    date(2025, 9, 7),   # Independência
    date(2025, 10, 12), # Nossa Senhora Aparecida
    date(2025, 11, 2),  # Finados
    date(2025, 11, 15), # Proclamação da República
    date(2025, 12, 25), # Natal
    # 2026
    date(2026, 1, 1),
    date(2026, 2, 16),  # Carnaval (segunda)
    date(2026, 2, 17),  # Carnaval (terça)
    date(2026, 4, 3),   # Sexta-feira Santa
    date(2026, 4, 21),
    date(2026, 5, 1),
    date(2026, 6, 4),   # Corpus Christi
    date(2026, 9, 7),
    date(2026, 10, 12),
    date(2026, 11, 2),
    date(2026, 11, 15),
    date(2026, 12, 25),
]

# Datas de vencimento de contratos futuros B3 (atualizar conforme calendário)
# Minicontrato de Índice (WIN) vence 3ª quarta-feira de meses pares
# Minicontrato de Dólar (WDO) vence 1ª quarta-feira do mês
FUTURES_EXPIRY: dict[str, date] = {
    "WINM25": date(2025, 6, 18),
    "WINQ25": date(2025, 8, 20),
    "WINV25": date(2025, 10, 15),
    "WINZ25": date(2025, 12, 17),
    "WDOM25": date(2025, 6, 4),
    "WDOQ25": date(2025, 8, 6),
    "WDOV25": date(2025, 10, 1),
    "WDOZ25": date(2025, 12, 3),
}


class B3FeatureEngineer(FeatureEngineer):
    """FeatureEngineer estendido com features específicas para B3 e Forex.

    Herda todos os indicadores técnicos base (RSI, MACD, Bollinger, etc.) e
    acrescenta features de calendário e sessão relevantes para mercados brasileiros.
    """

    B3_OPEN = time(9, 0)
    B3_CLOSE = time(17, 55)

    def calculate_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calcula features base + features B3/Forex.

        Args:
            df: DataFrame com colunas [timestamp, open, high, low, close, volume].
                `timestamp` deve ser datetime com timezone (UTC).

        Returns:
            DataFrame com features base + sessão + dia da semana + days_to_expiry.
        """
        # Features técnicas base
        df = super().calculate_features(df)

        # Garante que timestamp é datetime
        if "timestamp" not in df.columns:
            logger.warning("[B3FeatureEngineer] Coluna timestamp ausente — features de calendário ignoradas")
            for col in ["session_abertura", "session_meio_dia", "session_fechamento",
                        "dow_seg", "dow_ter", "dow_qua", "dow_qui", "dow_sex", "days_to_expiry"]:
                df[col] = 0
            return df

        ts = pd.to_datetime(df["timestamp"])

        # Converter para BRT (UTC-3) sem depender de pytz
        ts_brt = ts.dt.tz_convert("America/Sao_Paulo") if ts.dt.tz is not None else ts

        hour = ts_brt.dt.hour

        # ---- Sessão B3 ------------------------------------------------
        # abertura: 09h–10h59, meio_dia: 11h–15h59, fechamento: 16h–17h55
        df["session_abertura"] = ((hour >= 9) & (hour < 11)).astype(int)
        df["session_meio_dia"] = ((hour >= 11) & (hour < 16)).astype(int)
        df["session_fechamento"] = ((hour >= 16) & (hour < 18)).astype(int)

        # ---- Dia da semana (one-hot) -----------------------------------
        dow = ts_brt.dt.dayofweek  # 0=segunda, 4=sexta
        df["dow_seg"] = (dow == 0).astype(int)
        df["dow_ter"] = (dow == 1).astype(int)
        df["dow_qua"] = (dow == 2).astype(int)
        df["dow_qui"] = (dow == 3).astype(int)
        df["dow_sex"] = (dow == 4).astype(int)

        # ---- Days to expiry -------------------------------------------
        df["days_to_expiry"] = -1  # -1 = não é contrato futuro

        return df

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def days_to_expiry(symbol: str, current_date: date) -> int:
        """Retorna dias úteis até o vencimento do contrato futuro.

        Retorna -1 se o símbolo não for um contrato futuro reconhecido.
        """
        key = symbol[:6].upper()
        expiry = FUTURES_EXPIRY.get(key)
        if expiry is None:
            return -1
        return max(0, (expiry - current_date).days)

    @staticmethod
    def is_b3_open(dt: "datetime") -> bool:  # type: ignore[name-defined]
        """Verifica se a B3 está aberta no datetime fornecido (BRT aware)."""
        if dt.weekday() >= 5:  # sábado ou domingo
            return False
        if dt.date() in B3_HOLIDAYS:
            return False
        t = dt.time()
        return time(9, 0) <= t <= time(17, 55)

    def get_feature_columns(self) -> list[str]:
        """Retorna lista de colunas de features para B3/Forex."""
        return B3_FEATURE_COLUMNS
