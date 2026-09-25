"""Treinamento do modelo Random Forest com walk-forward backtesting."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import joblib
import pandas as pd

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).parent / "models"


def _map_mt5_crypto_to_binance(symbol: str) -> str:
    """Converte símbolos MT5 de cripto para par Binance quando possível.

    Ex.: BTCUSD.lv -> BTC/USDT
    """
    s = symbol.strip().upper()
    if s.endswith(".LV"):
        base = s[:-3]
        if base.endswith("USD") and len(base) > 3:
            return f"{base[:-3]}/USDT"
    return symbol


@dataclass
class FoldMetrics:
    fold: int
    profit_factor: float
    win_rate: float
    total_trades: int
    expectancy_pct: float
    max_drawdown_pct: float
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime


@dataclass
class TrainingResult:
    approved: bool
    version: str
    symbol: str
    timeframe: str
    fold_metrics: list[FoldMetrics] = field(default_factory=list)
    avg_profit_factor: float = 0.0
    avg_win_rate: float = 0.0
    avg_expectancy_pct: float = 0.0
    avg_max_drawdown_pct: float = 0.0
    total_trades: int = 0
    model_path: Optional[str] = None
    scaler_path: Optional[str] = None
    train_start: Optional[datetime] = None
    train_end: Optional[datetime] = None
    train_samples: int = 0
    rejection_reason: Optional[str] = None


class ModelTrainer:
    """Treina e valida modelo Random Forest com walk-forward CV."""

    # Gates de validação com viés de robustez (reduzir loss / manter consistência).
    MIN_PROFIT_FACTOR = 1.3
    MIN_WIN_RATE = 48.0
    MIN_TOTAL_TRADES = 80
    MIN_EXPECTANCY_PCT = 0.01
    MAX_DRAWDOWN_PCT = 25.0
    MAX_FOLD_PF_DISPERSION = 1.2
    N_FOLDS = 2
    TRAIN_MONTHS = 2
    TEST_MONTHS = 1

    def __init__(self, db=None) -> None:
        """
        Args:
            db: AsyncSession do SQLAlchemy (opcional; se None, não persiste no DB)
        """
        self._db = db

    # ------------------------------------------------------------------
    # Treino principal
    # ------------------------------------------------------------------

    async def train(
        self,
        symbol: str,
        timeframe: str = "1h",
        market: str = "CRIPTO",
        profile: str = "didi",
    ) -> TrainingResult:
        """Pipeline completo de treinamento.

        1. Carrega dados Parquet
        2. Calcula features e target
        3. Walk-forward CV (4 folds)
        4. Se aprovado: treina modelo final e salva
        5. Persiste no banco

        Returns:
            TrainingResult com status e métricas
        """
        profile_name = (profile or "didi").strip().lower()
        version = self._make_version(symbol, timeframe, market, profile_name)
        result = TrainingResult(
            approved=False, version=version, symbol=symbol, timeframe=timeframe
        )

        # 1. Carregar dados + escolher feature engineer conforme mercado
        if market in ("B3", "FOREX"):
            from config import settings
            from ml.b3_feature_engineer import B3_FEATURE_COLUMNS, B3FeatureEngineer
            from ml.mt5_data_collector import MT5DataCollector

            collector = MT5DataCollector(mt5_files_dir=settings.mt5_files_dir)
            fe = B3FeatureEngineer()
            feature_cols = B3_FEATURE_COLUMNS
            df = collector.load(symbol, timeframe, market)
            if df is None:
                result.rejection_reason = f"Dados não encontrados: {symbol} {timeframe} ({market})"
                logger.error(result.rejection_reason)
                return result

        elif market == "CRIPTO":
            from ml.data_collector import DataCollector
            from ml.feature_engineer import FEATURE_COLUMNS
            from ml.feature_engineer import FeatureEngineer

            df = None

            # Preferência para símbolos MT5 (.lv): usar DWX/MT5 se houver cache local.
            if symbol.upper().endswith(".LV"):
                try:
                    from config import settings
                    from ml.b3_feature_engineer import B3_FEATURE_COLUMNS, B3FeatureEngineer
                    from ml.mt5_data_collector import MT5DataCollector

                    if settings.mt5_files_dir:
                        mt5_collector = MT5DataCollector(mt5_files_dir=settings.mt5_files_dir)
                        df_mt5 = mt5_collector.load(symbol, timeframe, market="CRIPTO")
                        if df_mt5 is not None and not df_mt5.empty:
                            fe = B3FeatureEngineer()
                            feature_cols = B3_FEATURE_COLUMNS
                            df = df_mt5
                except Exception:
                    df = None

            # Fallback: tentar cache Binance para par equivalente.
            if df is None:
                collector_cripto = DataCollector()
                fe = FeatureEngineer()
                feature_cols = FEATURE_COLUMNS
                symbol_for_data = _map_mt5_crypto_to_binance(symbol)
                try:
                    df = collector_cripto.load_parquet(symbol_for_data, timeframe)
                except FileNotFoundError:
                    # Bootstrap: se não há parquet local, baixa histórico do fallback.
                    try:
                        # Janela de 180 dias é suficiente para o walk-forward atual,
                        # e evita bloqueio longo ao inicializar o primeiro treino.
                        since_ms = int((datetime.now(timezone.utc) - timedelta(days=180)).timestamp() * 1000)
                        logger.info(
                            "[trainer] Iniciando bootstrap fallback %s %s (180d)",
                            symbol_for_data,
                            timeframe,
                        )
                        df_dl = collector_cripto.download_historical(
                            symbol_for_data,
                            timeframe,
                            since=since_ms,
                        )
                        if df_dl.empty:
                            result.rejection_reason = (
                                f"Dados não encontrados: {symbol} {timeframe} "
                                f"(fallback={symbol_for_data})"
                            )
                            logger.error(result.rejection_reason)
                            return result
                        df_dl, _ = collector_cripto.validate_and_clean(df_dl, timeframe)
                        collector_cripto._save_parquet(symbol_for_data, timeframe, df_dl)
                        df = df_dl
                        logger.info(
                            "[trainer] Bootstrap fallback %s %s: %d candles",
                            symbol_for_data,
                            timeframe,
                            len(df),
                        )
                    except Exception as exc:
                        result.rejection_reason = (
                            f"Dados não encontrados: {symbol} {timeframe} "
                            f"(fallback={symbol_for_data}, erro={exc})"
                        )
                        logger.error(result.rejection_reason)
                        return result

        else:
            result.rejection_reason = f"Mercado não suportado para treino: {market}"
            logger.error(result.rejection_reason)
            return result

        # 2. Features + target
        cfg = self._resolve_profile_config(profile_name, timeframe, market)

        df = fe.calculate_features(df)
        if cfg["target_mode"] == "didi":
            df = self._calculate_didi_target(df)
        else:
            df = fe.calculate_target(
                df,
                forward_candles=cfg["forward_candles"],
                threshold=cfg["threshold"],
            )

        available_features = [c for c in feature_cols if c in df.columns]
        df = df.dropna(subset=available_features + ["target"])
        # Log distribuição do target para diagnóstico
        target_dist = df["target"].value_counts().to_dict()
        logger.info("[trainer] Distribuição target %s: %s", symbol, target_dist)
        X = df[available_features]
        y = df["target"]
        closes = df["close"].reset_index(drop=True)

        min_required_rows = int(cfg.get("min_required_rows", 200))
        if len(df) < min_required_rows:
            result.rejection_reason = (
                f"Dados insuficientes: {len(df)} candles (mínimo {min_required_rows})"
            )
            logger.error(result.rejection_reason)
            return result

        result.train_start = df["timestamp"].min().to_pydatetime()
        result.train_end = df["timestamp"].max().to_pydatetime()
        result.train_samples = len(df)

        # 3. Walk-forward CV
        fold_metrics = self._walk_forward_cv(
            X,
            y,
            closes,
            df["timestamp"],
            forward_candles=cfg["forward_candles"],
            min_train_samples=int(cfg.get("min_train_samples", 100)),
            min_test_samples=int(cfg.get("min_test_samples", 20)),
        )
        result.fold_metrics = fold_metrics

        if not fold_metrics:
            result.rejection_reason = "Walk-forward sem folds válidos"
            return result

        avg_pf = sum(f.profit_factor for f in fold_metrics) / len(fold_metrics)
        avg_wr = sum(f.win_rate for f in fold_metrics) / len(fold_metrics)
        avg_expectancy = sum(f.expectancy_pct for f in fold_metrics) / len(fold_metrics)
        avg_max_dd = sum(f.max_drawdown_pct for f in fold_metrics) / len(fold_metrics)
        total_trades = sum(f.total_trades for f in fold_metrics)

        result.avg_profit_factor = round(avg_pf, 4)
        result.avg_win_rate = round(avg_wr, 2)
        result.avg_expectancy_pct = round(avg_expectancy, 4)
        result.avg_max_drawdown_pct = round(avg_max_dd, 4)
        result.total_trades = total_trades

        pf_values = [f.profit_factor for f in fold_metrics]
        pf_dispersion = 0.0
        if len(pf_values) > 1 and avg_pf > 0:
            mean_pf = avg_pf
            variance = sum((x - mean_pf) ** 2 for x in pf_values) / len(pf_values)
            std_pf = variance ** 0.5
            pf_dispersion = std_pf / mean_pf

        min_pf = cfg["min_pf"]
        min_wr = cfg["min_wr"]
        min_total_trades = cfg["min_total_trades"]
        min_expectancy = cfg["min_expectancy"]
        max_dd = cfg["max_drawdown"]
        max_pf_dispersion = cfg["max_pf_dispersion"]

        skip_performance_gates = bool(cfg.get("skip_performance_gates", False))
        if not skip_performance_gates:
            if avg_pf < min_pf:
                result.rejection_reason = f"Profit Factor médio {avg_pf:.4f} < {min_pf}"
                logger.info("[trainer] Modelo rejeitado: %s", result.rejection_reason)
                return result

            if avg_wr < min_wr:
                result.rejection_reason = f"Win Rate médio {avg_wr:.2f}% < {min_wr:.2f}%"
                logger.info("[trainer] Modelo rejeitado: %s", result.rejection_reason)
                return result

            if total_trades < min_total_trades:
                result.rejection_reason = f"Trades insuficientes: {total_trades} < {min_total_trades}"
                logger.info("[trainer] Modelo rejeitado: %s", result.rejection_reason)
                return result

            if avg_expectancy < min_expectancy:
                result.rejection_reason = (
                    f"Expectancy médio {avg_expectancy:.4f}% < {min_expectancy:.4f}%"
                )
                logger.info("[trainer] Modelo rejeitado: %s", result.rejection_reason)
                return result

            if avg_max_dd > max_dd:
                result.rejection_reason = (
                    f"Drawdown médio {avg_max_dd:.2f}% > {max_dd:.2f}%"
                )
                logger.info("[trainer] Modelo rejeitado: %s", result.rejection_reason)
                return result

            if pf_dispersion > max_pf_dispersion:
                result.rejection_reason = (
                    f"Inconsistência entre folds (dispersão PF={pf_dispersion:.2f}) > "
                    f"{max_pf_dispersion:.2f}"
                )
                logger.info("[trainer] Modelo rejeitado: %s", result.rejection_reason)
                return result
        else:
            logger.info(
                "[trainer] Perfil didi: gates de performance desativados "
                "(PF=%.4f WR=%.2f%% trades=%d EXP=%.4f%% DD=%.2f%%)",
                avg_pf,
                avg_wr,
                total_trades,
                avg_expectancy,
                avg_max_dd,
            )

        # 4. Treinar modelo final + scaler
        model, scaler = self._train_final_model(X, y, fe, version)

        # 5. Salvar arquivos
        model_path, scaler_path = self._save_model_files(model, scaler, version)
        result.model_path = str(model_path)
        result.scaler_path = str(scaler_path)
        result.approved = True

        # 6. Persistir no banco
        if self._db is not None:
            await self._persist_to_db(result, model, available_features, market)

        logger.info(
            "[trainer] Modelo %s aprovado. PF=%.4f WR=%.1f%% trades=%d",
            version,
            avg_pf,
            avg_wr,
            total_trades,
        )
        return result

    # ------------------------------------------------------------------
    # Walk-forward CV
    # ------------------------------------------------------------------

    def _walk_forward_cv(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        closes: pd.Series,
        timestamps: pd.Series,
        forward_candles: int = 5,
        min_train_samples: int = 100,
        min_test_samples: int = 20,
    ) -> list[FoldMetrics]:
        """Walk-forward com janelas deslizantes.

        Fold i: treino = [start + i*test_months : start + i*test_months + train_months]
                teste  = [train_end : train_end + test_months]
        """
        from sklearn.ensemble import RandomForestClassifier

        from ml.validator import ModelValidator

        validator = ModelValidator()
        fold_results: list[FoldMetrics] = []

        total_months = self.N_FOLDS * self.TEST_MONTHS + self.TRAIN_MONTHS
        start_ts = timestamps.min()
        end_ts = timestamps.max()

        span_months = (end_ts - start_ts) / pd.Timedelta(days=30)
        if span_months < total_months:
            logger.warning(
                "[trainer] Período de %.1f meses < %.1f meses requeridos",
                span_months,
                total_months,
            )

        for fold in range(self.N_FOLDS):
            train_start = start_ts + pd.DateOffset(months=fold * self.TEST_MONTHS)
            train_end = train_start + pd.DateOffset(months=self.TRAIN_MONTHS)
            test_start = train_end
            test_end = test_start + pd.DateOffset(months=self.TEST_MONTHS)

            train_mask = (timestamps >= train_start) & (timestamps < train_end)
            test_mask = (timestamps >= test_start) & (timestamps < test_end)

            X_train, y_train = X[train_mask], y[train_mask]
            X_test = X[test_mask]
            y_test = y[test_mask]
            closes_test = closes[test_mask].reset_index(drop=True)
            X_test = X_test.reset_index(drop=True)

            if len(X_train) < min_train_samples or len(X_test) < min_test_samples:
                logger.warning("[trainer] Fold %d ignorado: dados insuficientes", fold + 1)
                continue

            model = RandomForestClassifier(
                n_estimators=100,
                max_depth=10,
                class_weight="balanced",
                random_state=42,
                n_jobs=-1,
            )
            model.fit(X_train, y_train)

            metrics = validator.validate(
                model,
                X_test,
                y_test,
                closes_test,
                forward_candles=forward_candles,
            )
            fold_results.append(
                FoldMetrics(
                    fold=fold + 1,
                    profit_factor=metrics.profit_factor,
                    win_rate=metrics.win_rate,
                    total_trades=metrics.total_trades,
                    expectancy_pct=metrics.expectancy_pct,
                    max_drawdown_pct=metrics.max_drawdown_pct,
                    train_start=train_start.to_pydatetime(),
                    train_end=train_end.to_pydatetime(),
                    test_start=test_start.to_pydatetime(),
                    test_end=test_end.to_pydatetime(),
                )
            )
            logger.info(
                "[trainer] Fold %d: PF=%.4f WR=%.1f%% trades=%d EXP=%.4f%% DD=%.2f%%",
                fold + 1,
                metrics.profit_factor,
                metrics.win_rate,
                metrics.total_trades,
                metrics.expectancy_pct,
                metrics.max_drawdown_pct,
            )

        if fold_results:
            return fold_results

        # Fallback quando janelas por mês não encaixam (comum em 15m/ativos novos).
        n = len(X)
        min_train = max(min_train_samples, int(n * 0.45))
        test_size = max(min_test_samples, int(n * 0.15), forward_candles + 5)
        if min_train + test_size > n:
            logger.warning("[trainer] Fallback por linhas inviável: n=%d", n)
            return fold_results

        for fold in range(self.N_FOLDS):
            train_end_idx = min_train + (fold * test_size)
            test_end_idx = train_end_idx + test_size
            if test_end_idx > n:
                break

            X_train = X.iloc[:train_end_idx]
            y_train = y.iloc[:train_end_idx]
            X_test = X.iloc[train_end_idx:test_end_idx].reset_index(drop=True)
            y_test = y.iloc[train_end_idx:test_end_idx].reset_index(drop=True)
            closes_test = closes.iloc[train_end_idx:test_end_idx].reset_index(drop=True)

            if len(X_train) < min_train_samples or len(X_test) < min_test_samples:
                continue

            model = RandomForestClassifier(
                n_estimators=100,
                max_depth=10,
                class_weight="balanced",
                random_state=42,
                n_jobs=-1,
            )
            model.fit(X_train, y_train)

            metrics = validator.validate(
                model,
                X_test,
                y_test,
                closes_test,
                forward_candles=forward_candles,
            )

            train_start = timestamps.iloc[0]
            train_end = timestamps.iloc[train_end_idx - 1]
            test_start = timestamps.iloc[train_end_idx]
            test_end = timestamps.iloc[test_end_idx - 1]

            fold_results.append(
                FoldMetrics(
                    fold=fold + 1,
                    profit_factor=metrics.profit_factor,
                    win_rate=metrics.win_rate,
                    total_trades=metrics.total_trades,
                    expectancy_pct=metrics.expectancy_pct,
                    max_drawdown_pct=metrics.max_drawdown_pct,
                    train_start=pd.Timestamp(train_start).to_pydatetime(),
                    train_end=pd.Timestamp(train_end).to_pydatetime(),
                    test_start=pd.Timestamp(test_start).to_pydatetime(),
                    test_end=pd.Timestamp(test_end).to_pydatetime(),
                )
            )
            logger.info(
                "[trainer] Fallback fold %d: PF=%.4f WR=%.1f%% trades=%d EXP=%.4f%% DD=%.2f%%",
                fold + 1,
                metrics.profit_factor,
                metrics.win_rate,
                metrics.total_trades,
                metrics.expectancy_pct,
                metrics.max_drawdown_pct,
            )

        return fold_results

    # ------------------------------------------------------------------
    # Modelo final
    # ------------------------------------------------------------------

    def _train_final_model(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        fe: "FeatureEngineer",
        version: str,
    ) -> tuple[Any, Any]:
        """Treina RandomForest em todos os dados com GridSearchCV."""
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import GridSearchCV, TimeSeriesSplit

        # Normalizar features
        df_norm, scaler = fe.fit_transform(X.copy(), version)
        X_scaled = df_norm.values

        param_grid = {
            "n_estimators": [100, 200],
            "max_depth": [5, 10, None],
            "min_samples_split": [2, 5],
        }

        # Em bases menores/irregulares, tenta GridSearch e cai para fit simples se necessário.
        try:
            if len(X_scaled) >= 90:
                tscv = TimeSeriesSplit(n_splits=3)
            elif len(X_scaled) >= 45:
                tscv = TimeSeriesSplit(n_splits=2)
            else:
                raise ValueError("Amostra pequena para GridSearchCV estável")

            gs = GridSearchCV(
                RandomForestClassifier(
                    class_weight="balanced", random_state=42, n_jobs=-1
                ),
                param_grid,
                cv=tscv,
                scoring="f1_weighted",
                n_jobs=-1,
            )
            gs.fit(X_scaled, y)
            logger.info("[trainer] Melhores params: %s", gs.best_params_)
            return gs.best_estimator_, scaler
        except Exception as exc:
            logger.warning("[trainer] Fallback sem GridSearchCV: %s", exc)
            model = RandomForestClassifier(
                n_estimators=200,
                max_depth=10,
                min_samples_split=2,
                class_weight="balanced",
                random_state=42,
                n_jobs=-1,
            )
            model.fit(X_scaled, y)
            return model, scaler

    # ------------------------------------------------------------------
    # Persistência
    # ------------------------------------------------------------------

    def _save_model_files(
        self, model: Any, scaler: Any, version: str
    ) -> tuple[Path, Path]:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        model_path = MODELS_DIR / f"{version}.pkl"
        scaler_path = MODELS_DIR / f"{version}_scaler.pkl"
        joblib.dump(model, model_path)
        joblib.dump(scaler, scaler_path)
        logger.info("Modelo salvo: %s", model_path)
        return model_path, scaler_path

    async def _persist_to_db(
        self,
        result: TrainingResult,
        model: Any,
        feature_list: list[str],
        market: str,
    ) -> None:
        from db.models import MLModel

        import math

        # Sanitiza valores infinitos que não cabem em NUMERIC(8,4) do banco
        safe_pf = result.avg_profit_factor
        if math.isinf(safe_pf) or math.isnan(safe_pf):
            safe_pf = 999.9999 if safe_pf > 0 else 0.0

        hp = {}
        if hasattr(model, "get_params"):
            hp = {k: str(v) for k, v in model.get_params().items()}

        record = MLModel(
            version=result.version,
            market=market,
            symbol=result.symbol,
            model_type="random_forest",
            status="inactive",
            file_path=result.model_path or "",
            scaler_path=result.scaler_path,
            feature_list={"columns": feature_list},
            hyperparams=hp,
            train_start=result.train_start,
            train_end=result.train_end,
            train_samples=result.train_samples,
            bt_profit_factor=safe_pf,
            bt_win_rate=result.avg_win_rate / 100,
            bt_max_drawdown=result.avg_max_drawdown_pct / 100,
            bt_total_trades=result.total_trades,
            is_approved=result.approved,
        )
        self._db.add(record)
        await self._db.commit()
        await self._db.refresh(record)
        logger.info("[trainer] MLModel id=%d persistido no banco.", record.id)

    # ------------------------------------------------------------------
    # Utilitários
    # ------------------------------------------------------------------

    @staticmethod
    def _make_version(
        symbol: str,
        timeframe: str = "1h",
        market: str = "CRIPTO",
        profile: str = "robust",
    ) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        sym = symbol.replace("/", "_")
        mkt = market.lower()
        tf = timeframe.lower().replace("/", "_")
        profile_suffix = "" if profile in {"", "robust"} else f"_{profile}"
        return f"rf_{mkt}_{sym}_{tf}{profile_suffix}_{ts}"

    def _resolve_profile_config(self, profile: str, timeframe: str, market: str) -> dict[str, Any]:
        tf = timeframe.lower().strip()
        is_low_vol_market = market in {"B3", "FOREX"}

        threshold_by_tf = {
            "1m": 0.0008,
            "3m": 0.0010,
            "5m": 0.0013,
            "15m": 0.0018,
            "30m": 0.0022,
            "1h": 0.0030,
            "2h": 0.0038,
            "4h": 0.0045,
            "6h": 0.0050,
            "8h": 0.0052,
            "12h": 0.0055,
            "1d": 0.0060,
            "1w": 0.0080,
        }
        forward_by_tf = {
            "1m": 3,
            "3m": 4,
            "5m": 5,
            "15m": 8,
            "30m": 8,
            "1h": 10,
            "2h": 10,
            "4h": 12,
            "6h": 12,
            "8h": 14,
            "12h": 14,
            "1d": 16,
            "1w": 10,
        }

        threshold = threshold_by_tf.get(tf, 0.0030)
        if is_low_vol_market:
            threshold *= 0.7

        forward_candles = forward_by_tf.get(tf, 8)

        if profile == "didi":
            min_trades_by_tf = {
                "1m": 30,
                "3m": 26,
                "5m": 24,
                "15m": 12,
                "30m": 10,
                "1h": 8,
                "2h": 8,
                "4h": 6,
                "6h": 6,
                "8h": 6,
                "12h": 5,
                "1d": 4,
                "1w": 3,
            }
            return {
                "target_mode": "didi",
                "threshold": threshold,
                "forward_candles": forward_candles,
                "min_pf": 1.08,
                "min_wr": 40.0,
                "min_total_trades": min_trades_by_tf.get(tf, 12),
                "min_expectancy": -0.01,
                "max_drawdown": 32.0,
                "max_pf_dispersion": 1.8,
                "min_required_rows": 80,
                "min_train_samples": 40,
                "min_test_samples": 8,
                "skip_performance_gates": False,
            }

        # Perfil robusto antigo (default legado)
        return {
            "target_mode": "future_return",
            "threshold": threshold,
            "forward_candles": forward_candles,
            "min_pf": self.MIN_PROFIT_FACTOR,
            "min_wr": self.MIN_WIN_RATE,
            "min_total_trades": self.MIN_TOTAL_TRADES,
            "min_expectancy": self.MIN_EXPECTANCY_PCT,
            "max_drawdown": self.MAX_DRAWDOWN_PCT,
            "max_pf_dispersion": self.MAX_FOLD_PF_DISPERSION,
            "min_required_rows": 200,
            "min_train_samples": 100,
            "min_test_samples": 20,
            "skip_performance_gates": False,
        }

    def _calculate_didi_target(self, df: pd.DataFrame) -> pd.DataFrame:
        """Target operacional baseado nas aulas: DMI + Didi + Bollinger.

        Entrada LONG/SHORT quando há 3 confirmações:
        1) DMI com tendência/força (ADX subindo + direção DI)
        2) Didi com inclinação favorável (MM curta vs base)
        3) Bollinger abrindo (largura em expansão)

        Bônus de confiança: Estocástico e TRIX no mesmo sentido.
        """
        out = df.copy()

        # Didi index simplificado via MMs (rápida/base/lenta)
        out["_didi_fast"] = out["close"].rolling(3).mean()
        out["_didi_mid"] = out["close"].rolling(8).mean()
        out["_didi_slow"] = out["close"].rolling(20).mean()
        out["_didi_fast_rel"] = (out["_didi_fast"] / out["_didi_mid"]) - 1.0
        out["_didi_slow_rel"] = (out["_didi_slow"] / out["_didi_mid"]) - 1.0

        # DMI/ADX (nomes variam conforme pandas-ta)
        adx_col = next((c for c in out.columns if c.startswith("ADX_")), "ADX_14")
        dmp_col = next((c for c in out.columns if c.startswith("DMP_")), None)
        dmn_col = next((c for c in out.columns if c.startswith("DMN_")), None)
        if adx_col not in out.columns:
            out[adx_col] = 0.0
        if dmp_col is None:
            out["_dmp_fallback"] = 0.0
            dmp_col = "_dmp_fallback"
        if dmn_col is None:
            out["_dmn_fallback"] = 0.0
            dmn_col = "_dmn_fallback"

        # Bollinger abertura via largura da banda
        bb_width_col = next((c for c in out.columns if c.startswith("BBB_")), "BBB_20_2.0")
        if bb_width_col not in out.columns:
            out[bb_width_col] = 0.0
        bb_width = out[bb_width_col].fillna(0.0)
        bb_opening = (bb_width > bb_width.shift(1)) & (bb_width > bb_width.rolling(20).median())

        # Tendência DMI "existente acelerante"
        adx = out[adx_col].fillna(0.0)
        dmp = out[dmp_col].fillna(0.0)
        dmn = out[dmn_col].fillna(0.0)
        trend_long = (adx > 20) & (adx > adx.shift(1)) & (dmp > dmn)
        trend_short = (adx > 20) & (adx > adx.shift(1)) & (dmn > dmp)

        # Sinal Didi (agulhada simplificada por aceleração relativa)
        didi_fast = out["_didi_fast_rel"].fillna(0.0)
        didi_slow = out["_didi_slow_rel"].fillna(0.0)
        didi_long = (didi_fast > 0) & (didi_fast > didi_fast.shift(1)) & (didi_slow <= 0.002)
        didi_short = (didi_fast < 0) & (didi_fast < didi_fast.shift(1)) & (didi_slow >= -0.002)

        # Confirmação "santa": estocástico e trix no mesmo sentido
        stoch_k_col = next((c for c in out.columns if c.startswith("STOCHk_")), None)
        stoch_d_col = next((c for c in out.columns if c.startswith("STOCHd_")), None)
        if stoch_k_col and stoch_d_col:
            stoch_long = out[stoch_k_col] > out[stoch_d_col]
            stoch_short = out[stoch_k_col] < out[stoch_d_col]
        else:
            stoch_long = pd.Series(True, index=out.index)
            stoch_short = pd.Series(True, index=out.index)

        trix = out["close"].ewm(span=9, adjust=False).mean()
        trix = trix.ewm(span=9, adjust=False).mean().ewm(span=9, adjust=False).mean()
        trix_line = trix.pct_change() * 100.0
        trix_signal = trix_line.rolling(4).mean()
        trix_long = trix_line > trix_signal
        trix_short = trix_line < trix_signal

        # 3 sinais de entrada + reforço por estocástico/trix.
        entry_long = trend_long & didi_long & bb_opening
        entry_short = trend_short & didi_short & bb_opening
        holy_long = entry_long & stoch_long & trix_long
        holy_short = entry_short & stoch_short & trix_short

        out["target"] = "NEUTRO"
        out.loc[entry_long | holy_long, "target"] = "LONG"
        out.loc[entry_short | holy_short, "target"] = "SHORT"

        return out.reset_index(drop=True)
