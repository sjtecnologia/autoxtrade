"""Tarefas Celery para ML: retrain semanal e atualização de dados."""
from __future__ import annotations

import asyncio
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="tasks.ml_tasks.update_market_data")
def update_market_data() -> None:
    """Atualiza dados OHLCV incrementalmente (roda diariamente às 03h BRT)."""
    asyncio.run(_update_market_data_async())


@shared_task(name="tasks.ml_tasks.retrain_models")
def retrain_models() -> None:
    """Retreina modelos para todos os pares configurados (roda aos domingos 02h BRT)."""
    asyncio.run(_retrain_models_async())


async def _update_market_data_async() -> None:
    from config import settings
    from ml.data_collector import DataCollector
    from ml.mt5_data_collector import MT5DataCollector
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.models import BotConfig

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        result = await db.execute(select(BotConfig).where(BotConfig.is_active == True))
        configs = result.scalars().all()

    await engine.dispose()

    # --- Cripto (Binance) ---
    cripto_symbols: list[str] = []
    for cfg in configs:
        if cfg.market == "CRIPTO":
            cripto_symbols.extend(["BTC/USDT", "ETH/USDT"])

    if cripto_symbols:
        collector = DataCollector()
        for symbol in set(cripto_symbols):
            for tf in ["5m", "1h", "1d"]:
                try:
                    added = collector.update_incremental(symbol, tf)
                    logger.info("[ml_tasks] CRIPTO %s %s: +%d candles", symbol, tf, added)
                except Exception as exc:
                    logger.error("[ml_tasks] Erro update CRIPTO %s %s: %s", symbol, tf, exc)

    # --- B3 / Forex (MT5 via DWX) ---
    if not settings.mt5_files_dir:
        return

    mt5_collector = MT5DataCollector(mt5_files_dir=settings.mt5_files_dir)

    for cfg in configs:
        if cfg.market not in ("B3", "FOREX"):
            continue
        symbols_raw = cfg.symbols if isinstance(cfg.symbols, list) else []
        for symbol in symbols_raw:
            for tf in ["1h", "1d"]:
                try:
                    added = mt5_collector.update_incremental(symbol, tf, market=cfg.market)
                    logger.info("[ml_tasks] %s %s %s: +%d candles", cfg.market, symbol, tf, added)
                except Exception as exc:
                    logger.error("[ml_tasks] Erro update %s %s %s: %s", cfg.market, symbol, tf, exc)


async def _retrain_models_async() -> None:
    from config import settings
    from ml.trainer import ModelTrainer
    from notifications.telegram import notifier
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.models import BotConfig

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        result = await db.execute(select(BotConfig).where(BotConfig.is_active == True))
        all_configs = result.scalars().all()

        # Montar lista (market, symbol) para treinar
        jobs: list[tuple[str, str]] = []
        for cfg in all_configs:
            if cfg.market == "CRIPTO":
                jobs.extend([("CRIPTO", "BTC/USDT"), ("CRIPTO", "ETH/USDT")])
            elif cfg.market in ("B3", "FOREX"):
                symbols_raw = cfg.symbols if isinstance(cfg.symbols, list) else []
                for sym in symbols_raw:
                    jobs.append((cfg.market, sym))

        for market, symbol in jobs:
            try:
                trainer = ModelTrainer(db=db)
                train_result = await trainer.train(
                    symbol,
                    timeframe="1h",
                    market=market,
                    profile="didi",
                )

                msg = (
                    f"🤖 <b>Retrain [{market}] {symbol}</b>\n"
                    f"Status: {'✅ APROVADO' if train_result.approved else '❌ REJEITADO'}\n"
                    f"PF médio: {train_result.avg_profit_factor:.4f}\n"
                    f"Win Rate: {train_result.avg_win_rate:.1f}%\n"
                    f"Trades: {train_result.total_trades}\n"
                )
                if not train_result.approved:
                    msg += f"Motivo: {train_result.rejection_reason}\n"
                else:
                    msg += f"Versão: <code>{train_result.version}</code>\n"

                await notifier.send_message(msg)
                logger.info("[ml_tasks] Retrain %s %s: approved=%s", market, symbol, train_result.approved)

            except Exception as exc:
                logger.error("[ml_tasks] Erro retrain %s %s: %s", market, symbol, exc)
                await notifier.send_emergency_alert(f"Erro retrain [{market}] {symbol}: {exc}")

    await engine.dispose()
