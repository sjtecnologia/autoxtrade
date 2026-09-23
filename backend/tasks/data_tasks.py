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

    from config import settings
    from database import get_db
    from db.models import BotConfig, EquitySnapshot, Trade
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    import json

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        result = await db.execute(select(BotConfig).where(BotConfig.is_active == True))
        configs = result.scalars().all()

        for config in configs:
            market = config.market
            # Busca saldo USDT via Redis
            import redis.asyncio as aioredis

            r = aioredis.from_url(settings.redis_url, decode_responses=True)
            try:
                usdt_free = Decimal("0")
                cached_balance = await r.get("balance:binance")
                if cached_balance:
                    data = json.loads(cached_balance)
                    usdt_free = Decimal(str(data.get("USDT", {}).get("free", 0)))
            finally:
                await r.aclose()

            # Calcula open PnL das posições abertas
            result_open = await db.execute(
                select(Trade).where(Trade.market == market, Trade.status == "open")
            )
            open_trades = result_open.scalars().all()

            open_pnl = Decimal("0")
            for trade in open_trades:
                # Usa entry_value como proxy (sem preço atual disponível no worker)
                open_pnl += Decimal("0")  # será melhorado quando tiver preços em memória

            equity = usdt_free + open_pnl
            if equity <= Decimal("0"):
                logger.info("[snapshot] Equity zero para %s — pulando.", market)
                continue

            snapshot = EquitySnapshot(
                snapshot_at=datetime.datetime.now(datetime.timezone.utc),
                market=market,
                equity=equity,
                open_pnl=open_pnl,
                realized_pnl=Decimal("0"),
                drawdown_1d=Decimal("0"),
                drawdown_30d=Decimal("0"),
                open_trades=len(open_trades),
            )
            db.add(snapshot)

        await db.commit()
        logger.info("[snapshot] Equity snapshot registrado para %d mercados.", len(configs))

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
