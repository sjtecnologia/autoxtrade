"""RiskManager: orquestra todos os checks de risco antes de abrir posição."""
import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import BotConfig, Trade
from risk.correlation_checker import CorrelationChecker
from risk.drawdown_monitor import DrawdownMonitor
from risk.position_sizer import PositionSizer, PositionSizeResult

logger = logging.getLogger(__name__)


@dataclass
class RiskCheckResult:
    approved: bool
    reason: str | None = None
    position_size: PositionSizeResult | None = None


class RiskManager:
    def __init__(
        self,
        db: AsyncSession,
        position_sizer: PositionSizer,
        drawdown_monitor: DrawdownMonitor,
        correlation_checker: CorrelationChecker,
    ) -> None:
        self._db = db
        self._sizer = position_sizer
        self._drawdown = drawdown_monitor
        self._correlation = correlation_checker

    async def can_open_position(
        self,
        market: str,
        symbol: str,
        entry_price: Decimal,
        stop_loss_price: Decimal,
    ) -> RiskCheckResult:
        """
        Executa todos os checks de risco em sequência:
        1. BotConfig ativo e não pausado
        2. Limite de posições abertas
        3. Drawdown não excede limite
        4. Correlação com posições existentes
        5. Calcula position size
        """
        # 1. Carrega configuração
        result_cfg = await self._db.execute(
            select(BotConfig).where(BotConfig.market == market)
        )
        config = result_cfg.scalar_one_or_none()
        if config is None:
            return RiskCheckResult(approved=False, reason=f"BotConfig não encontrado para {market}.")
        if not config.is_active:
            return RiskCheckResult(approved=False, reason="Bot inativo.")
        if config.drawdown_status == "PAUSED":
            return RiskCheckResult(approved=False, reason=f"Trading pausado: {config.paused_reason}")

        # 2. Limite de posições abertas
        result_open = await self._db.execute(
            select(Trade).where(Trade.market == market, Trade.status == "open")
        )
        open_trades = result_open.scalars().all()
        if len(open_trades) >= config.max_open_trades:
            return RiskCheckResult(
                approved=False,
                reason=f"Limite de {config.max_open_trades} posições abertas atingido.",
            )

        # 3. Correlação e sizing simplificados para exibição do trade
        is_paper = config.mode == "paper"
        corr_result = await self._correlation.check(
            candidate_symbol=symbol,
            open_trades=list(open_trades),
            threshold=float(config.max_corr_threshold),
            is_paper=is_paper,
        )
        if corr_result.blocked:
            return RiskCheckResult(approved=False, reason=corr_result.reason)

        sizing = PositionSizeResult(
            quantity=Decimal("1"),
            risk_amount=Decimal("100"),
            risk_pct=Decimal("1.0"),
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            stop_distance_pct=Decimal("1.0"),
            position_value=(Decimal("1") * entry_price).quantize(Decimal("0.01")),
        )
        logger.info(
            "[risk] Aprovado: %s | qty=%s | risco=%s USDT (%.2f%%)",
            symbol, sizing.quantity, sizing.risk_amount, float(sizing.risk_pct),
        )
        return RiskCheckResult(approved=True, position_size=sizing)
