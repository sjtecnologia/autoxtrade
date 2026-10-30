"""Engine de execução de ordens com suporte a OCO e fallback de segurança."""
import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Trade, TradeEvent
from trading.connectors.base import BaseConnector
from trading.connectors.types import OrderSide, OrderType
from trading.safety import (
    require_new_exposure,
    require_paper_new_exposure,
    require_position_management,
)
from trading.contracts import Capability, ExecutionContext, OrderIntent, ResultState
from uuid import uuid4

logger = logging.getLogger(__name__)


class OrderExecutionError(Exception):
    """Erro fatal na execução de ordens."""


class OrderExecutor:
    """Orquestra abertura e fechamento de posições com proteção via OCO."""

    def __init__(self, connector: BaseConnector, db: AsyncSession) -> None:
        self._conn = connector
        self._db = db
        # Importação tardia para evitar circular (notificações dependem de models)
        self._telegram = None

    def _get_telegram(self):
        if self._telegram is None:
            from notifications.telegram import notifier
            self._telegram = notifier
        return self._telegram

    async def open_position(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal | None = None,
        model_version: str | None = None,
        ml_confidence: Decimal | None = None,
        market: str | None = None,
        context: ExecutionContext | None = None,
        intent_id: str | None = None,
    ) -> Trade:
        """Abre posição: envia ordem de entrada e OCO de proteção."""
        self._validate_stops(side, entry_price, stop_loss, take_profit)
        require_new_exposure(self._conn)
        bound = self._conn.context
        if bound.mode.value == "paper":
            require_paper_new_exposure(symbol)
        if context is not None and context != bound:
            raise OrderExecutionError("Request conflicts with authorized execution context")
        if symbol != bound.instrument.symbol or market is not None and market != bound.market:
            raise OrderExecutionError("Request market/native symbol conflicts with authorized context")
        self._conn._check_use(Capability.PROTECT)
        bound.instrument.validate(quantity, entry_price)
        bound.instrument.validate(quantity, stop_loss)
        if take_profit is not None:
            bound.instrument.validate(quantity, take_profit)

        import datetime

        # 1. Ordem de entrada (market)
        entry_order = await self._conn.create_order(OrderIntent(
            intent_id or uuid4().hex, bound, side, OrderType.MARKET, quantity))
        if (entry_order.state != ResultState.CONFIRMED or entry_order.filled != quantity
                or not entry_order.position_id or entry_order.average is None):
            raise OrderExecutionError(f"Entry not fully confirmed: {entry_order.state.value}; reconciliation required")
        avg_price = entry_order.average
        execution_snapshot = bound.snapshot()
        entry_value = avg_price * quantity
        if bound.mode.value == "paper":
            from paper.costs import paper_costs_for
            costs = paper_costs_for(symbol)
            contract_size = bound.instrument.contract_size
            entry_offset = costs.spread_price + costs.slippage_price
            avg_price = avg_price + entry_offset if side == OrderSide.BUY else avg_price - entry_offset
            entry_value = avg_price * quantity * contract_size
            execution_snapshot["paper_costs"] = costs.as_dict()

        # 2. Persiste trade como "open"
        trade = Trade(
            market=bound.market,
            exchange=bound.venue,
            symbol=symbol,
            side=side.value,
            status="open",
            mode=bound.mode.value,
            account_id=bound.account_id,
            account_nature=bound.nature.value,
            instrument_class=bound.instrument.asset_class,
            position_id=entry_order.position_id,
            execution_context=execution_snapshot,
            identity_status="bound",
            entry_price=avg_price,
            quantity=quantity,
            entry_value=entry_value.quantize(Decimal("0.01")),
            stop_loss=stop_loss,
            take_profit=take_profit,
            open_at=datetime.datetime.now(datetime.timezone.utc),
            open_order_id=entry_order.order_id,
            model_version=model_version,
            ml_confidence=ml_confidence,
        )
        self._db.add(trade)
        self._db.add(
            TradeEvent(
                trade=trade,
                event_type="ORDER_SENT",
                payload={"order_id": entry_order.order_id, "intent_id": entry_order.intent_id, "price": str(avg_price)},
            )
        )
        await self._db.commit()
        await self._db.refresh(trade)

        # 3. Envia proteção. Se falhar, não deixamos exposição nua: tentamos
        # fechar imediatamente. Sem confirmação do fechamento, o trade fica
        # marcado para reconciliação e o erro é propagado.
        protection = None
        protection_error = None
        try:
            protection = await self._conn.modify_position_protection(
                entry_order.position_id, stop_loss=stop_loss, take_profit=take_profit,
                intent_id=uuid4().hex)
        except Exception as exc:
            protection_error = str(exc)
        if protection is None or protection.state != ResultState.CONFIRMED:
            closed = await self._fail_safe_after_protection_error(
                trade, entry_order.position_id, stop_loss, take_profit, protection_error
            )
            if closed:
                raise OrderExecutionError(
                    "Protection not confirmed; position was closed to avoid unhedged risk"
                )
            raise OrderExecutionError(
                "Protection not confirmed; position remains open UNPROTECTED for reconciliation"
            )
        self._db.add(TradeEvent(trade=trade, event_type="PROTECTION_CONFIRMED",
                                payload={"position_id": protection.position_id}))
        await self._db.commit()

        return trade

    async def _fail_safe_after_protection_error(
        self,
        trade: Trade,
        position_id: str,
        stop_loss: Decimal,
        take_profit: Decimal | None,
        protection_error: str | None,
    ) -> bool:
        """Fecha a posição quando a proteção não pôde ser confirmada."""
        self._db.add(TradeEvent(
            trade=trade,
            event_type="PROTECTION_FAILED",
            payload={
                "position_id": position_id,
                "stop_loss": str(stop_loss),
                "take_profit": str(take_profit) if take_profit is not None else None,
                "error": (protection_error or "not confirmed")[:250],
            },
        ))
        close_error = None
        try:
            close_order = await self._conn.reduce_position(
                position_id, trade.quantity, intent_id=uuid4().hex
            )
        except Exception as exc:
            close_order = None
            close_error = str(exc)

        if (
            close_order is not None
            and close_order.state == ResultState.CONFIRMED
            and close_order.filled == trade.quantity
            and close_order.average is not None
        ):
            self._finalize_trade(
                trade, close_order.average, "PROTECTION_FAILED", close_order.order_id
            )
            self._db.add(TradeEvent(
                trade=trade,
                event_type="PROTECTION_FAILED_CLOSED",
                payload={"position_id": position_id, "exit_price": str(close_order.average)},
            ))
            await self._db.commit()
            logger.critical(
                "[executor] Proteção falhou no trade #%s; posição fechada a mercado.",
                trade.id,
            )
            return True

        self._db.add(TradeEvent(
            trade=trade,
            event_type="UNPROTECTED_POSITION",
            payload={
                "position_id": position_id,
                "close_error": (close_error or "close not confirmed")[:250],
            },
        ))
        await self._db.commit()
        logger.critical(
            "[executor] Trade #%s segue ABERTO E SEM PROTEÇÃO; reconciliação obrigatória.",
            trade.id,
        )
        return False

    async def close_position(self, trade: Trade, reason: str = "manual") -> Trade:
        """Cancela OCO e fecha posição a mercado."""
        require_position_management(self._conn)
        import datetime

        context = self._conn.context
        snapshot = trade.execution_context or {}
        expected = context.snapshot()
        # paper_costs é metadado de execução e não faz parte da identidade.
        identity_fields = (
            "venue", "broker", "account_id", "nature", "mode", "market",
            "instrument", "position_model",
        )
        identity_matches = all(
            snapshot.get(field) == expected.get(field) for field in identity_fields
        )
        if (not identity_matches or not trade.position_id
                or trade.account_id != context.account_id or trade.mode != context.mode.value):
            raise OrderExecutionError("Position identity unresolved or belongs to another account")
        close_order = await self._conn.reduce_position(trade.position_id, trade.quantity, intent_id=uuid4().hex)
        if close_order.state != ResultState.CONFIRMED or close_order.filled != trade.quantity or close_order.average is None:
            raise OrderExecutionError("Close not confirmed; reconciliation required")
        exit_price = close_order.average

        self._finalize_trade(trade, exit_price, reason, close_order.order_id)
        self._db.add(TradeEvent(
            trade=trade,
            event_type="MANUAL_CLOSE",
            payload={"reason": reason, "exit_price": str(exit_price)},
        ))
        await self._db.commit()
        return trade

    # ------------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------------

    def _validate_stops(
        self,
        side: OrderSide,
        entry: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal | None,
    ) -> None:
        if stop_loss is None:
            raise OrderExecutionError("stop_loss é obrigatório para abrir posição.")
        if take_profit is not None and ((side == OrderSide.BUY and take_profit <= entry)
                                        or (side == OrderSide.SELL and take_profit >= entry)):
            raise OrderExecutionError("Take profit conflicts with position direction")
        if side == OrderSide.BUY and stop_loss >= entry:
            raise OrderExecutionError(
                f"Stop loss ({stop_loss}) deve ser MENOR que a entrada ({entry}) em LONG."
            )
        if side == OrderSide.SELL and stop_loss <= entry:
            raise OrderExecutionError(
                f"Stop loss ({stop_loss}) deve ser MAIOR que a entrada ({entry}) em SHORT."
            )

    async def _send_oco_with_fallback(
        self,
        trade: Trade,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal | None,
    ) -> None:
        close_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

        if take_profit is None:
            # Sem TP definido: apenas stop-market
            await self._send_stop_market_or_close(trade, symbol, close_side, quantity, stop_loss)
            return

        # Margem de 0.05% abaixo do stop para o stop-limit
        stop_limit = stop_loss * (Decimal("1") - Decimal("0.0005")) if side == OrderSide.BUY \
            else stop_loss * (Decimal("1") + Decimal("0.0005"))

        try:
            oco = await self._conn.place_oco_order(
                symbol=symbol,
                side=close_side,
                amount=quantity,
                take_profit_price=take_profit,
                stop_price=stop_loss,
                stop_limit_price=stop_limit,
            )
            self._db.add(TradeEvent(
                trade=trade,
                event_type="OCO_SET",
                payload={"oco_order_id": oco.id},
            ))
            await self._db.commit()
            logger.info("[executor] OCO configurado para trade #%d", trade.id)
        except Exception as exc:
            logger.error("[executor] OCO falhou para trade #%d: %s — tentando fallback", trade.id, exc)
            await self._send_stop_market_or_close(trade, symbol, close_side, quantity, stop_loss, oco_error=str(exc))

    async def _send_stop_market_or_close(
        self,
        trade: Trade,
        symbol: str,
        close_side: OrderSide,
        quantity: Decimal,
        stop_loss: Decimal,
        oco_error: str | None = None,
    ) -> None:
        try:
            stop_order = await self._conn.place_order(
                symbol=symbol,
                side=close_side,
                order_type=OrderType.STOP_MARKET,
                amount=quantity,
                price=stop_loss,
            )
            self._db.add(TradeEvent(
                trade=trade,
                event_type="OCO_FAILED",
                payload={"oco_error": oco_error, "fallback": "stop_market", "order_id": stop_order.id},
            ))
            await self._db.commit()
            logger.warning("[executor] Stop-market fallback configurado para trade #%d", trade.id)
        except Exception as exc2:
            logger.critical("[executor] Stop-market FALHOU para trade #%d: %s — fechando a mercado", trade.id, exc2)
            current_price = await self._conn.get_current_price(symbol)
            self._finalize_trade(trade, current_price, "DD_LIMIT_CLOSE")
            self._db.add(TradeEvent(
                trade=trade,
                event_type="OCO_FAILED",
                payload={"oco_error": oco_error, "stop_error": str(exc2), "fallback": "market_close"},
            ))
            await self._db.commit()
            # Notificação de emergência
            try:
                telegram = self._get_telegram()
                from notifications.templates import format_oco_failed
                await telegram.send_emergency_alert(format_oco_failed(trade, oco_error or str(exc2)))
            except Exception:
                pass

    def _finalize_trade(
        self,
        trade: Trade,
        exit_price: Decimal,
        reason: str,
        close_order_id: str | None = None,
    ) -> None:
        import datetime

        side_mult = Decimal("1") if trade.side == "buy" else Decimal("-1")
        snapshot = trade.execution_context or {}
        cost_data = snapshot.get("paper_costs") or {}
        instrument = snapshot.get("instrument") or {}
        contract_size = Decimal(str(instrument.get("contract_size") or "1")) if cost_data else Decimal("1")
        if cost_data:
            spread_price = Decimal(str(cost_data["spread_price"]))
            slippage_price = Decimal(str(cost_data["slippage_price"]))
            commission_rate = Decimal(str(cost_data["commission_rate"]))
            offset = spread_price + slippage_price
            exit_price = exit_price - offset if trade.side == "buy" else exit_price + offset
            commission = (trade.entry_price + exit_price) * trade.quantity * contract_size * commission_rate
        else:
            commission = (trade.entry_price + exit_price) * trade.quantity * Decimal("0.001")
        pnl_gross = (exit_price - trade.entry_price) * trade.quantity * contract_size * side_mult
        pnl_net = pnl_gross - commission

        trade.status = "closed"
        trade.exit_price = exit_price
        trade.exit_value = (exit_price * trade.quantity * contract_size).quantize(Decimal("0.01"))
        trade.close_at = datetime.datetime.now(datetime.timezone.utc)
        trade.close_reason = reason
        trade.close_order_id = close_order_id
        trade.pnl_gross = pnl_gross.quantize(Decimal("0.01"))
        trade.commission = commission.quantize(Decimal("0.01"))
        trade.pnl_net = pnl_net.quantize(Decimal("0.01"))
        if trade.entry_value:
            trade.pnl_pct = (pnl_net / trade.entry_value * 100).quantize(Decimal("0.0001"))
        if trade.close_at and trade.open_at:
            trade.duration_sec = int((trade.close_at - trade.open_at).total_seconds())
