"""Engine de execução de ordens com suporte a OCO e fallback de segurança."""
import logging
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Trade, TradeEvent
from trading.connectors.base import BaseConnector
from trading.connectors.types import OrderSide, OrderStatus, OrderType
from trading.safety import require_new_exposure, require_position_management
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
        if entry_order.state == ResultState.PARTIAL:
            self._record_partial_entry(entry_order, bound, symbol, side, quantity, stop_loss,
                                       take_profit, model_version, ml_confidence)
            await self._db.commit()
            raise OrderExecutionError("Entry partially filled; reconciliation required")
        if entry_order.state == ResultState.UNKNOWN:
            self._record_unknown_entry(entry_order, bound, symbol, side, quantity, entry_price,
                                       stop_loss, take_profit, model_version, ml_confidence)
            await self._db.commit()
            raise OrderExecutionError("Entry acceptance unknown; reconciliation required")
        if (entry_order.state != ResultState.CONFIRMED or entry_order.filled != quantity
                or not entry_order.position_id or entry_order.average is None):
            raise OrderExecutionError(f"Entry not fully confirmed: {entry_order.state.value}; reconciliation required")
        avg_price = entry_order.average

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
            execution_context=bound.snapshot(),
            identity_status="bound",
            entry_price=avg_price,
            quantity=quantity,
            entry_value=(avg_price * quantity).quantize(Decimal("0.01")),
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

        # 3. Envia OCO com fallback
        protection = await self._conn.modify_position_protection(
            entry_order.position_id, stop_loss=stop_loss, take_profit=take_profit,
            intent_id=uuid4().hex)
        if protection.state != ResultState.CONFIRMED:
            raise OrderExecutionError("Protection not confirmed; position remains open for reconciliation")
        self._db.add(TradeEvent(trade=trade, event_type="PROTECTION_CONFIRMED",
                                payload={"position_id": protection.position_id}))
        await self._db.commit()

        return trade

    async def close_position(self, trade: Trade, reason: str = "manual") -> Trade:
        """Cancela OCO e fecha posição a mercado."""
        require_position_management(self._conn)
        import datetime

        context = self._conn.context
        if (trade.execution_context != context.snapshot() or not trade.position_id
                or trade.account_id != context.account_id or trade.mode != context.mode.value):
            raise OrderExecutionError("Position identity unresolved or belongs to another account")
        close_intent_id = uuid4().hex
        try:
            close_order = await self._conn.reduce_position(
                trade.position_id, trade.quantity, intent_id=close_intent_id)
        except Exception as exc:
            self._db.add(TradeEvent(
                trade=trade, event_type="CLOSE_FAILED",
                payload={"reason": reason, "intent_id": close_intent_id, "error": str(exc)},
            ))
            await self._db.commit()
            raise OrderExecutionError("Close not confirmed; reconciliation required") from exc
        if close_order.state != ResultState.CONFIRMED or close_order.filled != trade.quantity or close_order.average is None:
            event_type = "CLOSE_PENDING" if close_order.state == ResultState.UNKNOWN else "CLOSE_FAILED"
            self._db.add(TradeEvent(
                trade=trade, event_type=event_type,
                payload={"reason": reason, "intent_id": close_intent_id,
                         "state": close_order.state.value, "filled": str(close_order.filled)},
            ))
            await self._db.commit()
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
        intent_id = uuid4().hex
        try:
            stop_order = await self._conn.place_order(
                symbol=symbol,
                side=close_side,
                order_type=OrderType.STOP_MARKET,
                amount=quantity,
                price=stop_loss,
                params={"intent_id": intent_id},
            )
            order_intent = stop_order.raw.get("intent_id") if stop_order.raw else None
            if order_intent != intent_id:
                raise OrderExecutionError("Stop order identity mismatch")
            if stop_order.status == OrderStatus.UNKNOWN:
                event_type = "CLOSE_PENDING"
                reason = "Stop order acceptance is unknown; reconciliation required"
            elif stop_order.status == OrderStatus.PARTIAL or stop_order.filled != quantity:
                event_type = "PARTIAL_FILL"
                reason = "Stop order partially filled; reconciliation required"
            elif stop_order.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
                event_type = "CLOSE_FAILED"
                reason = f"Stop order {stop_order.status.value}"
            else:
                event_type = "OCO_FAILED"
                reason = None
            self._db.add(TradeEvent(
                trade=trade,
                event_type=event_type,
                payload={"oco_error": oco_error, "fallback": "stop_market", "order_id": stop_order.id,
                         "intent_id": intent_id, "reason": reason},
            ))
            await self._db.commit()
            if event_type == "CLOSE_FAILED":
                raise OrderExecutionError(reason)
            logger.warning("[executor] Stop-market fallback registrado para trade #%d", trade.id)
        except Exception as exc2:
            logger.critical("[executor] Stop-market FALHOU para trade #%d: %s", trade.id, exc2)
            self._db.add(TradeEvent(
                trade=trade,
                event_type="CLOSE_FAILED",
                payload={"oco_error": oco_error, "stop_error": str(exc2), "intent_id": intent_id},
            ))
            await self._db.commit()
            # Notificação de emergência
            try:
                telegram = self._get_telegram()
                from notifications.templates import format_oco_failed
                await telegram.send_emergency_alert(format_oco_failed(trade, oco_error or str(exc2)))
            except Exception:
                pass

    def _record_partial_entry(self, result, context, symbol, side, requested_quantity,
                              stop_loss, take_profit, model_version, ml_confidence) -> None:
        import datetime

        if result.filled <= 0 or result.average is None or not result.position_id:
            return
        trade = Trade(
            market=context.market, exchange=context.venue, symbol=symbol, side=side.value,
            status="partial", mode=context.mode.value, account_id=context.account_id,
            account_nature=context.nature.value, instrument_class=context.instrument.asset_class,
            position_id=result.position_id, execution_context=context.snapshot(), identity_status="bound",
            entry_price=result.average, quantity=result.filled,
            entry_value=(result.average * result.filled).quantize(Decimal("0.01")),
            stop_loss=stop_loss, take_profit=take_profit,
            open_at=datetime.datetime.now(datetime.timezone.utc), open_order_id=result.order_id,
            model_version=model_version, ml_confidence=ml_confidence,
        )
        self._db.add(trade)
        self._db.add(TradeEvent(
            trade=trade, event_type="PARTIAL_FILL",
            payload={"intent_id": result.intent_id, "order_id": result.order_id,
                     "requested": str(requested_quantity), "filled": str(result.filled)},
        ))

    def _record_unknown_entry(self, result, context, symbol, side, requested_quantity,
                              requested_price, stop_loss, take_profit, model_version,
                              ml_confidence) -> None:
        import datetime

        filled = result.filled if result.filled > 0 else Decimal("0")
        average = result.average or requested_price
        trade = Trade(
            market=context.market, exchange=context.venue, symbol=symbol, side=side.value,
            status="pending", mode=context.mode.value, account_id=context.account_id,
            account_nature=context.nature.value, instrument_class=context.instrument.asset_class,
            position_id=result.position_id, execution_context=context.snapshot(), identity_status="bound",
            entry_price=average, quantity=filled,
            entry_value=(average * filled).quantize(Decimal("0.01")), stop_loss=stop_loss,
            take_profit=take_profit, open_at=datetime.datetime.now(datetime.timezone.utc),
            open_order_id=result.order_id, model_version=model_version, ml_confidence=ml_confidence,
        )
        self._db.add(trade)
        self._db.add(TradeEvent(
            trade=trade, event_type="ORDER_UNKNOWN",
            payload={"intent_id": result.intent_id, "order_id": result.order_id,
                     "requested": str(requested_quantity), "filled": str(filled)},
        ))

    def _finalize_trade(
        self,
        trade: Trade,
        exit_price: Decimal,
        reason: str,
        close_order_id: str | None = None,
    ) -> None:
        import datetime

        side_mult = Decimal("1") if trade.side == "buy" else Decimal("-1")
        pnl_gross = (exit_price - trade.entry_price) * trade.quantity * side_mult
        commission = (trade.entry_price + exit_price) * trade.quantity * Decimal("0.001")
        pnl_net = pnl_gross - commission

        trade.status = "closed"
        trade.exit_price = exit_price
        trade.exit_value = (exit_price * trade.quantity).quantize(Decimal("0.01"))
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
