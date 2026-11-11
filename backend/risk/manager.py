"""Risk checks and atomic per-account entry reservations."""
import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from inspect import isawaitable
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import BotConfig, ExecutionIntent, RiskAccountLock, Trade
from risk.correlation_checker import CorrelationChecker
from risk.drawdown_monitor import DrawdownMonitor
from risk.position_sizer import PositionSizer, PositionSizeResult, PositionSizingError
from trading.contracts import ExecutionContext, context_from_dict

logger = logging.getLogger(__name__)

_RESERVED_STATUSES = ("reserved", "planned", "unknown", "partial")


@dataclass
class RiskCheckResult:
    approved: bool
    reason: str | None = None
    position_size: PositionSizeResult | None = None
    intent_id: str | None = None


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

    def _context_for_symbol(self, config, symbol: str) -> ExecutionContext | None:
        bindings = getattr(config, "execution_context", None) or {}
        if not isinstance(bindings, dict):
            return None
        raw_context = bindings.get(symbol)
        instrument_binding = bindings.get("instrument")
        if (
            raw_context is None
            and isinstance(instrument_binding, dict)
            and instrument_binding.get("symbol") == symbol
        ):
            raw_context = bindings
        if raw_context is None:
            return None
        try:
            return context_from_dict(raw_context)
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "[risk] Execution context inválido para %s; usando metadados do sizer.",
                symbol,
            )
            return None

    @staticmethod
    def _account_scope(
        config: BotConfig,
        context: ExecutionContext | None,
    ) -> tuple[str, tuple]:
        if context is not None:
            identity = (
                context.venue,
                context.account_id,
                context.account_currency or "",
                context.mode.value,
            )
            trade_filter = (
                Trade.exchange == context.venue,
                Trade.account_id == context.account_id,
                Trade.mode == context.mode.value,
            )
        else:
            identity = (config.market, "", "", config.mode)
            trade_filter = (Trade.market == config.market, Trade.mode == config.mode)
        account_key = hashlib.sha256("\0".join(identity).encode()).hexdigest()
        return account_key, trade_filter

    @staticmethod
    def _trade_risk(trade: Trade) -> Decimal:
        if trade.risk_amount is not None:
            return Decimal(trade.risk_amount)
        snapshot = trade.execution_context or {}
        instrument = snapshot.get("instrument") or {}
        contract_size = Decimal(str(instrument.get("contract_size") or "1"))
        return abs(Decimal(trade.entry_price) - Decimal(trade.stop_loss)) * Decimal(
            trade.quantity
        ) * contract_size

    @staticmethod
    def _trade_capital(trade: Trade) -> Decimal:
        if trade.capital_amount is not None:
            return Decimal(trade.capital_amount)
        return Decimal(trade.entry_value or 0)

    async def can_open_position(
        self,
        market: str,
        symbol: str,
        entry_price: Decimal,
        stop_loss_price: Decimal,
        *,
        side: str = "buy",
        intent_id: str | None = None,
        requested_quantity: Decimal | None = None,
        reservation_ttl_seconds: int = 1800,
    ) -> RiskCheckResult:
        """Validate an entry and persist its reservation before returning approval."""
        cfg_result = await self._db.execute(
            select(BotConfig).where(BotConfig.market == market)
        )
        config = cfg_result.scalar_one_or_none()
        if config is None:
            await self._db.rollback()
            return RiskCheckResult(False, f"BotConfig não encontrado para {market}.")

        drawdown = await self._maybe_await(
            self._drawdown.check_and_enforce(market)
        )
        if getattr(drawdown, "status", config.drawdown_status) == "PAUSED":
            return RiskCheckResult(
                False,
                f"Trading pausado por drawdown: {getattr(config, 'paused_reason', None)}",
            )

        cfg_result = await self._db.execute(
            select(BotConfig).where(BotConfig.market == market)
        )
        config = cfg_result.scalar_one_or_none()
        if config is None:
            await self._db.rollback()
            return RiskCheckResult(False, f"BotConfig não encontrado para {market}.")
        if not config.is_active:
            await self._db.rollback()
            return RiskCheckResult(False, "Bot inativo.")
        if config.drawdown_status == "PAUSED":
            await self._db.rollback()
            return RiskCheckResult(
                False, f"Trading pausado: {config.paused_reason}"
            )

        context = self._context_for_symbol(config, symbol)
        account_key, trade_filter = self._account_scope(config, context)

        # Do not keep a read transaction open during broker, cache, or other I/O.
        await self._db.commit()
        result_open = await self._db.execute(
            select(Trade).where(*trade_filter, Trade.status == "open")
        )
        open_trades = list(result_open.scalars().all())
        await self._db.commit()

        corr_result = await self._correlation.check(
            candidate_symbol=symbol,
            open_trades=open_trades,
            threshold=float(config.max_corr_threshold),
            is_paper=config.mode == "paper",
        )
        if corr_result.blocked:
            return RiskCheckResult(False, corr_result.reason)

        instrument = context.instrument if context else None
        quote_asset = (
            getattr(instrument, "quote_currency", None)
            or getattr(config, "account_currency", None)
            or "USDT"
        )
        if config.mode == "paper":
            if context is not None:
                from trading.equity import paper_available_account_capital

                capital = await paper_available_account_capital(
                    self._db,
                    venue=context.venue,
                    account_id=context.account_id,
                )
            else:
                from trading.equity import paper_available_capital

                capital = await paper_available_capital(self._db, market)
            capital_source = "paper_ledger"
        else:
            if context is None:
                return RiskCheckResult(
                    False,
                    "Execution context da conta indisponível; entrada bloqueada.",
                )
            await self._db.commit()
            from trading.equity import available_account_capital

            capital = self._decimal_or_default(
                await available_account_capital(context, config.mode, quote_asset),
                Decimal("0"),
            )
            capital_source = "broker_account"
        await self._db.commit()
        if capital <= Decimal("0"):
            return RiskCheckResult(
                False,
                f"Capital indisponível para {quote_asset} ({capital_source}); entrada bloqueada.",
            )

        contract_size = getattr(instrument, "contract_size", None)
        if market in {"B3", "FOREX"} and contract_size is None:
            return RiskCheckResult(
                False, f"Contract size ausente para {symbol}; entrada bloqueada."
            )
        if contract_size is None:
            contract_size = Decimal("1")
        step_size = getattr(instrument, "volume_step", None)
        if step_size is None:
            step_size = self._decimal_or_default(
                await self._maybe_await(self._sizer.get_step_size(symbol)),
                Decimal("0.001"),
            )

        risk_pct = self._decimal_or_default(
            getattr(config, "risk_per_trade_pct", None), Decimal("1")
        )
        if risk_pct <= 0:
            return RiskCheckResult(False, "Percentual de risco deve ser positivo.")
        leverage = self._decimal_or_default(
            getattr(config, "leverage", None), Decimal("1")
        )
        reservation_id = intent_id or uuid4().hex
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(1, reservation_ttl_seconds)
        )

        await self._db.commit()
        async with self._db.begin():
            await self._db.execute(
                insert(RiskAccountLock)
                .values(account_key=account_key)
                .on_conflict_do_nothing(index_elements=[RiskAccountLock.account_key])
            )
            await self._db.execute(
                select(RiskAccountLock)
                .where(RiskAccountLock.account_key == account_key)
                .with_for_update()
            )

            locked_config_result = await self._db.execute(
                select(BotConfig)
                .where(BotConfig.market == market)
                .with_for_update()
            )
            locked_config = locked_config_result.scalar_one_or_none()
            if (
                locked_config is None
                or not locked_config.is_active
                or locked_config.drawdown_status == "PAUSED"
                or locked_config.mode != config.mode
            ):
                return RiskCheckResult(
                    False, "Trading pausado ou configuração alterada antes da reserva."
                )

            current_intent = None
            if intent_id is not None:
                intent_result = await self._db.execute(
                    select(ExecutionIntent)
                    .where(ExecutionIntent.intent_id == intent_id)
                    .with_for_update()
                )
                current_intent = intent_result.scalar_one_or_none()
                if (
                    current_intent is None
                    or current_intent.account_key != account_key
                    or current_intent.status != "reserved"
                ):
                    return RiskCheckResult(
                        False, "A reserva não está pendente ou não pertence à conta."
                    )
                if current_intent.expires_at and current_intent.expires_at <= datetime.now(
                    timezone.utc
                ):
                    current_intent.status = "failed"
                    current_intent.result_state = "rejected"
                    return RiskCheckResult(False, "A reserva expirou antes da execução.")

            expired_result = await self._db.execute(
                select(ExecutionIntent)
                .where(
                    ExecutionIntent.account_key == account_key,
                    ExecutionIntent.status == "reserved",
                    ExecutionIntent.expires_at <= datetime.now(timezone.utc),
                )
                .with_for_update()
            )
            for expired in expired_result.scalars().all():
                if expired.intent_id != reservation_id:
                    expired.status = "failed"
                    expired.result_state = "rejected"

            account_trades_result = await self._db.execute(
                select(Trade).where(*trade_filter, Trade.status == "open")
            )
            account_trades = list(account_trades_result.scalars().all())
            configs_result = await self._db.execute(select(BotConfig))
            account_configs = []
            for candidate in configs_result.scalars().all():
                if candidate.market == market:
                    account_configs.append(candidate)
                    continue
                for candidate_symbol in candidate.enabled_symbols or []:
                    candidate_context = self._context_for_symbol(
                        candidate, candidate_symbol
                    )
                    candidate_key, _ = self._account_scope(
                        candidate, candidate_context
                    )
                    if candidate_key == account_key:
                        account_configs.append(candidate)
                        break
            account_max_open_trades = min(
                (candidate.max_open_trades for candidate in account_configs),
                default=locked_config.max_open_trades,
            )
            account_risk_pct = min(
                (
                    self._decimal_or_default(
                        candidate.risk_per_trade_pct, risk_pct
                    )
                    for candidate in account_configs
                ),
                default=risk_pct,
            )
            open_intent_ids = {
                trade.intent_id for trade in account_trades if trade.intent_id
            }
            intents_result = await self._db.execute(
                select(ExecutionIntent).where(
                    ExecutionIntent.account_key == account_key,
                    ExecutionIntent.status.in_(_RESERVED_STATUSES),
                )
            )
            active_intents = [
                row
                for row in intents_result.scalars().all()
                if row.intent_id != reservation_id
            ]

            used_risk = sum(
                (self._trade_risk(trade) for trade in account_trades),
                Decimal("0"),
            )
            used_capital = sum(
                (self._trade_capital(trade) for trade in account_trades),
                Decimal("0"),
            )
            pending_slots = 0
            for row in active_intents:
                reserved_quantity = Decimal(row.quantity)
                if reserved_quantity <= 0:
                    return RiskCheckResult(
                        False,
                        f"Intenção pendente inválida ({row.intent_id}); reconciliação necessária.",
                    )
                remaining = reserved_quantity - Decimal(row.filled_quantity or 0)
                ratio = max(remaining, Decimal("0")) / reserved_quantity
                used_risk += Decimal(row.risk_amount or 0) * ratio
                used_capital += Decimal(row.capital_amount or 0) * ratio
                if row.intent_id not in open_intent_ids:
                    pending_slots += 1

            if len(account_trades) + pending_slots >= account_max_open_trades:
                return RiskCheckResult(
                    False,
                    f"Limite de {account_max_open_trades} posições abertas ou reservadas atingido.",
                )

            available_capital = capital - used_capital
            available_risk = (
                capital * account_risk_pct * account_max_open_trades / Decimal("100")
                - used_risk
            )
            if available_capital <= 0 or available_risk <= 0:
                return RiskCheckResult(
                    False, "Orçamento de capital ou risco já reservado para esta conta."
                )

            sizing_capital = min(
                available_capital, available_risk * Decimal("100") / risk_pct
            )
            try:
                sizing = self._sizer.calculate(
                    capital=sizing_capital,
                    risk_pct=risk_pct,
                    entry_price=entry_price,
                    stop_loss_price=stop_loss_price,
                    step_size=step_size,
                    min_quantity=getattr(instrument, "min_volume", None),
                    max_quantity=getattr(instrument, "max_volume", None),
                    leverage=leverage,
                    contract_size=contract_size,
                )
            except PositionSizingError as exc:
                return RiskCheckResult(False, str(exc))

            quantity = sizing.quantity
            if requested_quantity is not None:
                if requested_quantity <= 0 or requested_quantity > sizing.quantity:
                    return RiskCheckResult(
                        False,
                        f"Quantidade solicitada excede o risco disponível ({sizing.quantity}).",
                    )
                factor = requested_quantity / sizing.quantity
                sizing = replace(
                    sizing,
                    quantity=requested_quantity,
                    risk_amount=sizing.risk_amount * factor,
                    position_value=sizing.position_value * factor,
                )
                quantity = requested_quantity

            margin_rate = getattr(instrument, "margin_rate", None)
            reserved_capital = (
                sizing.position_value * margin_rate
                if margin_rate is not None
                else sizing.position_value / leverage
            )
            if (
                sizing.risk_amount > available_risk
                or reserved_capital > available_capital
            ):
                return RiskCheckResult(
                    False, "A reserva excede o orçamento restante de risco ou capital."
                )

            if current_intent is None:
                current_intent = ExecutionIntent(
                    intent_id=reservation_id,
                    market=market,
                    mode=config.mode,
                    account_key=account_key,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    risk_amount=sizing.risk_amount,
                    capital_amount=reserved_capital,
                    status="reserved",
                    expires_at=expires_at,
                )
                self._db.add(current_intent)
            else:
                current_intent.quantity = quantity
                current_intent.side = side
                current_intent.risk_amount = sizing.risk_amount
                current_intent.capital_amount = reserved_capital

        logger.info(
            "[risk] Reservado: %s %s | qty=%s | risco=%s | capital=%s",
            symbol,
            reservation_id,
            sizing.quantity,
            sizing.risk_amount,
            reserved_capital,
        )
        return RiskCheckResult(
            approved=True,
            position_size=sizing,
            intent_id=reservation_id,
        )

    async def release_confirmed_reservation(self, intent_id: str) -> bool:
        """Release only a reservation whose caller has confirmed cancellation."""
        return await release_confirmed_reservation(self._db, intent_id)


async def release_confirmed_reservation(
    db: AsyncSession,
    intent_id: str,
) -> bool:
    """Release a reserved intent after its cancellation has been confirmed."""
    await db.commit()
    async with db.begin():
        lookup = await db.execute(
            select(ExecutionIntent.account_key).where(
                ExecutionIntent.intent_id == intent_id
            )
        )
        account_key = lookup.scalar_one_or_none()
        if account_key is None:
            return False
        await db.execute(
            select(RiskAccountLock)
            .where(RiskAccountLock.account_key == account_key)
            .with_for_update()
        )
        result = await db.execute(
            select(ExecutionIntent)
            .where(ExecutionIntent.intent_id == intent_id)
            .with_for_update()
        )
        intent = result.scalar_one_or_none()
        if intent is None or intent.status != "reserved":
            return False
        trade_result = await db.execute(
            select(Trade.id).where(Trade.intent_id == intent_id).limit(1)
        )
        if trade_result.scalar_one_or_none() is not None:
            return False
        intent.status = "failed"
        intent.result_state = "rejected"
    return True
