"""Monitor de drawdown com pausas automáticas."""
import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import BotConfig, EquitySnapshot

logger = logging.getLogger(__name__)


@dataclass
class DrawdownResult:
    dd_1d: Decimal
    dd_30d: Decimal
    peak_capital: Decimal
    current_equity: Decimal
    status: str  # "OK" | "WARNING" | "PAUSED"


class DrawdownMonitor:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._redis: aioredis.Redis | None = None

    async def startup(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def shutdown(self) -> None:
        if self._redis:
            await self._redis.aclose()

    async def calculate_drawdown(self, market: str) -> DrawdownResult:
        """Calcula drawdown atual a partir dos snapshots de equity."""
        now = datetime.datetime.now(datetime.timezone.utc)
        since_30d = now - datetime.timedelta(days=30)
        since_1d = now - datetime.timedelta(days=1)

        result_30d = await self._db.execute(
            select(EquitySnapshot)
            .where(EquitySnapshot.market == market, EquitySnapshot.snapshot_at >= since_30d)
            .order_by(EquitySnapshot.snapshot_at.asc())
        )
        snapshots_30d = result_30d.scalars().all()

        if not snapshots_30d:
            return DrawdownResult(
                dd_1d=Decimal("0"),
                dd_30d=Decimal("0"),
                peak_capital=Decimal("0"),
                current_equity=Decimal("0"),
                status="OK",
            )

        equities = [s.equity for s in snapshots_30d]
        current_equity = equities[-1]
        peak_30d = max(equities)
        dd_30d = (
            (peak_30d - current_equity) / peak_30d * 100
            if peak_30d > 0
            else Decimal("0")
        )

        # DD 1d: peak no último dia
        snapshots_1d = [s for s in snapshots_30d if s.snapshot_at >= since_1d]
        if snapshots_1d:
            peak_1d = max(s.equity for s in snapshots_1d)
            dd_1d = (
                (peak_1d - current_equity) / peak_1d * 100
                if peak_1d > 0
                else Decimal("0")
            )
        else:
            dd_1d = Decimal("0")

        # Determinar status atual do BotConfig
        result_cfg = await self._db.execute(
            select(BotConfig).where(BotConfig.market == market)
        )
        config = result_cfg.scalar_one_or_none()
        status = config.drawdown_status if config else "OK"

        return DrawdownResult(
            dd_1d=dd_1d.quantize(Decimal("0.01")),
            dd_30d=dd_30d.quantize(Decimal("0.01")),
            peak_capital=peak_30d,
            current_equity=current_equity,
            status=status,
        )

    async def check_and_enforce(self, market: str) -> DrawdownResult:
        """Verifica limites e aplica pausas automáticas se necessário."""
        result_cfg = await self._db.execute(
            select(BotConfig).where(BotConfig.market == market)
        )
        config = result_cfg.scalar_one_or_none()
        if config is None:
            logger.warning("[drawdown] BotConfig não encontrado para market=%s", market)
            return DrawdownResult(Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), "OK")

        dd_result = await self.calculate_drawdown(market)
        dd_30d = dd_result.dd_30d

        # Atualiza métricas no BotConfig
        config.current_drawdown_1d = dd_result.dd_1d
        config.current_drawdown_30d = dd_30d
        if dd_result.peak_capital > (config.peak_capital or Decimal("0")):
            config.peak_capital = dd_result.peak_capital

        # CRITICAL: ≥ max_drawdown_pct (15%) → PAUSED
        if dd_30d >= config.max_drawdown_pct and config.drawdown_status != "PAUSED":
            logger.critical(
                "[drawdown] Drawdown %.2f%% >= limite %.2f%% → PAUSANDO %s",
                dd_30d, config.max_drawdown_pct, market,
            )
            config.drawdown_status = "PAUSED"
            config.is_active = False
            config.paused_reason = f"Drawdown {dd_30d:.2f}% >= limite {config.max_drawdown_pct}%"
            config.paused_at = datetime.datetime.now(datetime.timezone.utc)
            dd_result = DrawdownResult(
                dd_result.dd_1d, dd_30d, dd_result.peak_capital, dd_result.current_equity, "PAUSED"
            )
            await self._notify_and_broadcast(market, dd_30d, "CRITICAL")

        # WARNING: ≥ alert_pct (10%) → WARNING
        elif dd_30d >= config.drawdown_alert_pct and config.drawdown_status == "OK":
            logger.warning(
                "[drawdown] Drawdown %.2f%% >= alerta %.2f%% → WARNING %s",
                dd_30d, config.drawdown_alert_pct, market,
            )
            config.drawdown_status = "WARNING"
            dd_result = DrawdownResult(
                dd_result.dd_1d, dd_30d, dd_result.peak_capital, dd_result.current_equity, "WARNING"
            )
            await self._notify_and_broadcast(market, dd_30d, "WARNING")

        await self._db.commit()
        return dd_result

    async def _notify_and_broadcast(self, market: str, dd_pct: Decimal, level: str) -> None:
        try:
            from notifications.telegram import notifier
            from notifications.templates import format_drawdown_alert

            await notifier.send_message(format_drawdown_alert(dd_pct, level, market))
        except Exception as exc:
            logger.warning("[drawdown] Falha ao enviar notificação Telegram: %s", exc)

        if self._redis:
            import json

            await self._redis.publish(
                "ws:broadcast",
                json.dumps(
                    {
                        "type": "DRAWDOWN_UPDATE",
                        "payload": {"market": market, "dd_30d": str(dd_pct), "status": level},
                    }
                ),
            )
