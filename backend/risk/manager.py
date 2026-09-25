"""RiskManager: orquestra todos os checks de risco antes de abrir posição."""
import logging
from dataclasses import dataclass
from decimal import Decimal
from inspect import isawaitable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import BotConfig, Trade
from risk.correlation_checker import CorrelationChecker
from risk.drawdown_monitor import DrawdownMonitor
from risk.position_sizer import PositionSizer, PositionSizeResult, PositionSizingError
from trading.contracts import context_from_dict

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

    async def _maybe_await(self, value):
        if isawaitable(value):
            return await value
        return value

    def _decimal_or_default(self, value, default: Decimal) -> Decimal:
        if isinstance(value, Decimal):
            return value
        if isinstance(value, (int, float, str)):
            return Decimal(str(value))
        return default

    def _context_for_symbol(self, config, symbol: str):
        bindings = getattr(config, "execution_context", None) or {}
        if not isinstance(bindings, dict):
            return None
        raw_context = bindings.get(symbol)
        if raw_context is None and bindings.get("instrument", {}).get("symbol") == symbol:
            raw_context = bindings
        if raw_context is None:
            return None
        try:
            return context_from_dict(raw_context)
        except Exception:
            logger.warning("[risk] Execution context inválido para %s; usando metadados do sizer.", symbol)
            return None

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

        # 3. Correlação e sizing
        is_paper = config.mode == "paper"
        corr_result = await self._correlation.check(
            candidate_symbol=symbol,
            open_trades=list(open_trades),
            threshold=float(config.max_corr_threshold),
            is_paper=is_paper,
        )
        if corr_result.blocked:
            return RiskCheckResult(approved=False, reason=corr_result.reason)

        context = self._context_for_symbol(config, symbol)
        instrument = context.instrument if context else None
        quote_asset = (getattr(instrument, "quote_currency", None)
                       or getattr(config, "account_currency", None) or "USDT")
        capital = self._decimal_or_default(
            await self._maybe_await(self._sizer.get_available_capital(quote_asset)),
            Decimal("10000"),
        )
        if capital <= Decimal("0"):
            capital = Decimal("10000")
        step_size = getattr(instrument, "volume_step", None)
        if step_size is None:
            step_size = self._decimal_or_default(
                await self._maybe_await(self._sizer.get_step_size(symbol)), Decimal("0.001"))
        risk_pct = self._decimal_or_default(getattr(config, "risk_per_trade_pct", None), Decimal("1"))
        leverage = self._decimal_or_default(getattr(config, "leverage", None), Decimal("1"))
        try:
            sizing = self._sizer.calculate(
                capital=capital,
                risk_pct=risk_pct,
                entry_price=entry_price,
                stop_loss_price=stop_loss_price,
                step_size=step_size,
                min_quantity=getattr(instrument, "min_volume", None),
                max_quantity=getattr(instrument, "max_volume", None),
                leverage=leverage,
            )
        except PositionSizingError as exc:
            return RiskCheckResult(approved=False, reason=str(exc))
        logger.info(
            "[risk] Aprovado: %s | qty=%s | risco=%s USDT (%.2f%%)",
            symbol, sizing.quantity, sizing.risk_amount, float(sizing.risk_pct),
        )
        return RiskCheckResult(approved=True, position_size=sizing)
