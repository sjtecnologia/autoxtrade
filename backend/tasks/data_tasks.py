"""Tarefas de dados periódicos: snapshot de equity e monitor de drawdown."""
import asyncio
import datetime
import logging
from decimal import Decimal

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="tasks.data_tasks.record_equity_snapshot")
def record_equity_snapshot() -> None:
    """Registra snapshot de equity para cada mercado ativo (roda a cada hora)."""
    asyncio.run(_record_equity_snapshot_async())


@shared_task(name="tasks.data_tasks.check_drawdown")
def check_drawdown() -> None:
    """Verifica drawdown e aplica pausas automáticas (roda a cada 5 min)."""
    asyncio.run(_check_drawdown_async())


async def _record_equity_snapshot_async() -> None:
    from sqlalchemy import select

    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from config import settings
    from db.models import BotConfig, EquitySnapshot
    from trading.equity import collect_market_equity
    from trading.market_feed import MarketDataUnavailable, latest_mark_price

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    recorded = 0
    try:
        async with Session() as db:
            result = await db.execute(select(BotConfig))
            configs = result.scalars().all()

            for config in configs:
                async def _mark(symbol: str, market: str) -> Decimal:
                    return await latest_mark_price(
                        redis_client, symbol, market, settings
                    )

                try:
                    equity = await collect_market_equity(db, config, mark_price=_mark)
                except MarketDataUnavailable as exc:
                    logger.warning(
                        "[snapshot] %s sem preço confiável: %s", config.market, exc
                    )
                    continue

                if equity.status != "ok":
                    logger.warning(
                        "[snapshot] %s sem equity confiável: %s",
                        config.market,
                        equity.detail,
                    )
                    continue

                snapshot = EquitySnapshot(
                    snapshot_at=datetime.datetime.now(datetime.timezone.utc),
                    market=config.market,
                    equity=equity.equity.quantize(Decimal("0.01")),
                    open_pnl=equity.open_pnl.quantize(Decimal("0.01")),
                    realized_pnl=equity.realized_pnl.quantize(Decimal("0.01")),
                    drawdown_1d=Decimal("0"),
                    drawdown_30d=Decimal("0"),
                    open_trades=equity.open_trades,
                )
                db.add(snapshot)
                recorded += 1
                logger.info(
                    "[snapshot] %s %s/%s: saldo=%s open_pnl=%s equity=%s",
                    config.market,
                    equity.venue,
                    equity.account_id,
                    equity.balance,
                    equity.open_pnl,
                    equity.equity,
                )

            await db.commit()
            logger.info("[snapshot] Equity registrado para %d mercados.", recorded)
    finally:
        await redis_client.aclose()
        await engine.dispose()


async def _check_drawdown_async() -> None:
    from sqlalchemy import select

    from config import settings
    from db.models import BotConfig
    from risk.drawdown_monitor import DrawdownMonitor
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        result = await db.execute(select(BotConfig))
        configs = result.scalars().all()

        for config in configs:
            monitor = DrawdownMonitor(db)
            await monitor.startup()
            try:
                await monitor.check_and_enforce(config.market)
            finally:
                await monitor.shutdown()

    await engine.dispose()
