"""Tarefas Celery para relatórios e notificações."""
import logging
from decimal import Decimal

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name="tasks.report_tasks.send_telegram_message",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def send_telegram_message(self, text: str) -> bool:
    """Envia mensagem Telegram com retry exponencial (3x, backoff 1min/2min/4min)."""
    import asyncio
    from notifications.telegram import notifier

    try:
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(notifier.send_message(text))
        loop.close()
        return result
    except Exception as exc:
        wait = 60 * (2 ** self.request.retries)
        logger.warning("Falha ao enviar Telegram (tentativa %d): %s", self.request.retries + 1, exc)
        raise self.retry(exc=exc, countdown=wait)


@shared_task(name="tasks.report_tasks.send_daily_report")
def send_daily_report() -> None:
    """Gera e envia relatório diário para todos os mercados ativos."""
    import asyncio
    from datetime import date, timedelta, timezone
    import datetime

    from sqlalchemy import select, func as sa_func
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    from config import settings
    from db.models import Trade
    from notifications.templates import format_daily_report

    async def _run():
        engine = create_async_engine(settings.database_url)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as db:
            today_start = datetime.datetime.combine(date.today(), datetime.time.min, tzinfo=timezone.utc)
            tomorrow = today_start + timedelta(days=1)

            for market in ["CRIPTO", "B3"]:
                result_day = await db.execute(
                    select(Trade).where(
                        Trade.market == market,
                        Trade.status == "closed",
                        Trade.close_at >= today_start,
                        Trade.close_at < tomorrow,
                    )
                )
                day_trades = result_day.scalars().all()

                result_open = await db.execute(
                    select(sa_func.count(Trade.id)).where(
                        Trade.market == market, Trade.status == "open"
                    )
                )
                open_count = result_open.scalar() or 0

                result_acc = await db.execute(
                    select(sa_func.sum(Trade.pnl_net)).where(
                        Trade.market == market, Trade.status == "closed"
                    )
                )
                pnl_acc = Decimal(str(result_acc.scalar() or 0))

                pnl_day = sum(Decimal(str(t.pnl_net or 0)) for t in day_trades)
                winning = sum(1 for t in day_trades if (t.pnl_net or 0) > 0)

                msg = format_daily_report(
                    market=market,
                    total_trades=len(day_trades),
                    winning=winning,
                    pnl_day=pnl_day,
                    pnl_accumulated=pnl_acc,
                    drawdown_1d=Decimal("0"),   # calculado pelo risk_monitor
                    drawdown_30d=Decimal("0"),
                    open_trades=open_count,
                )
                send_telegram_message.delay(msg)

        await engine.dispose()

    asyncio.run(_run())
