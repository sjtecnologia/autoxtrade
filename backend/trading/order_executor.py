"""Engine de execução de ordens com suporte a OCO e fallback de segurança."""
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ExecutionIntent, Trade, TradeEvent
from trading.connectors.base import BaseConnector
from trading.connectors.types import OrderSide, OrderType
from trading.safety import (
    require_new_exposure,
    require_paper_new_exposure,
    require_position_management,
)
from trading.contracts import (Capability, ExecutionContext, ExecutionResult,
                               OrderIntent, ResultState)
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
        """Abre posição: intenção persistida, envio idempotente e proteção.

        1. A intenção (intent_id) é persistida ANTES do envio (status
           "planned"); sem essa persistência a ordem não é enviada. Uma
           entrada conflitante não resolvida (planned/unknown/partial para
           o mesmo request) bloqueia novas intents.
        2. O envio usa o mesmo intent_id no transport, que é idempotente
           por id: reinvocações com o mesmo intent_id NÃO reenviam a ordem.
           `create_order` acontece UMA vez por intent; toda reinvocação
           reconcilia pela leitura de `query_order(intent_id=...)`.
        3. Exposição executada (mesmo parcial) gera trade com a quantidade
           efetivamente executada; trade + desfecho do intent são gravados
           no MESMO commit, vinculados por trades.intent_id (unicidade).
           Timeout/resposta perdida é "unknown", nunca rejeição; entrada
           incerta jamais é reenviada.
        4. Havendo exposição confirmada, a proteção é sempre tentada —
           mesmo que a persistência pós-envio tenha falhado. Se a proteção
           falhar, o fail-safe reduz APENAS a exposição confirmada.
        """
        self._validate_stops(side, entry_price, stop_loss, take_profit)
        if intent_id is None:
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

        # Intenção estável: o mesmo id vai para o banco e para o transport.
        # OrderIntent reutiliza os contratos existentes (id seguro + contexto).
        intent_key = intent_id or uuid4().hex
        order_intent = OrderIntent(intent_key, bound, side, OrderType.MARKET, quantity)
        order_intent.validate()

        # 1. Persiste (ou recupera) a intenção ANTES de qualquer envio.
        intent_row, linked_trade, is_new = await self._resolve_intent(
            intent_key, bound, symbol, side, quantity)
        if linked_trade is not None:
            return linked_trade

        if is_new:
            try:
                require_new_exposure(self._conn)
                if bound.mode.value == "paper":
                    require_paper_new_exposure(symbol)
            except Exception:
                await self._record_intent_outcome(intent_key, None, status="failed")
                raise
            # 2. Primeiro envio deste intent: create_order exatamente UMA vez.
            try:
                entry_order = await self._conn.create_order(order_intent)
            except Exception as exc:
                # Timeout/resposta perdida NÃO é rejeição nem confirmação.
                # Nada é reenviado: o desfecho segue desconhecido até a
                # reconciliação pelo mesmo intent_id.
                await self._record_intent_outcome(intent_key, None, status="unknown")
                raise OrderExecutionError(
                    f"Entry send failed with outcome unknown (intent_id={intent_key}); "
                    f"no resend — reconcile by re-invoking open_position with "
                    f"intent_id={intent_key}"
                ) from exc
        else:
            # 2b. Intent persistido sem trade vinculado: reconciliação
            # somente-leitura pelo MESMO intent_id — nunca um novo envio.
            await self._db.commit()
            entry_order = await self._conn.query_order(intent_id=intent_key)

        # 3. Classifica o desfecho externo.
        if entry_order.state == ResultState.REJECTED:
            await self._record_intent_outcome(intent_key, entry_order, status="failed")
            raise OrderExecutionError(f"Entry not fully confirmed: {entry_order.state.value}; reconciliation required")

        executed = entry_order.filled or Decimal("0")
        has_exposure = (executed > 0 and entry_order.position_id is not None
                        and entry_order.average is not None)
        if not has_exposure:
            if (entry_order.state == ResultState.UNKNOWN
                    and entry_order.order_id is None
                    and entry_order.position_id is None
                    and executed == 0
                    and entry_order.reason == "Intent not found"):
                # O transport declara que jamais recebeu este intent: sem
                # exposição e sem ambiguidade, uma nova intent pode ser usada.
                await self._record_intent_outcome(intent_key, entry_order, status="failed")
                raise OrderExecutionError(
                    f"Entry never reached the transport (intent_id={intent_key}); "
                    f"retry with a new intent_id")
            # Desfecho incerto (inclusive timeout sem resposta do transport):
            # nunca rejeitar, nunca reenviar, exigir reconciliação.
            await self._record_intent_outcome(intent_key, entry_order, status="unknown")
            raise OrderExecutionError(
                f"Entry outcome unknown (intent_id={intent_key}); no resend — "
                f"reconcile by re-invoking open_position with intent_id={intent_key}")

        # 4. Exposição executada (total ou parcial): trade registra SEMPRE
        #    a quantidade confirmada, e o intent o desfecho real.
        full = executed == quantity
        avg_price = entry_order.average
        execution_snapshot = bound.snapshot()
        entry_value = avg_price * executed
        if bound.mode.value == "paper":
            from paper.costs import paper_costs_for
            costs = paper_costs_for(symbol)
            contract_size = bound.instrument.contract_size
            entry_offset = costs.spread_price + costs.slippage_price
            avg_price = avg_price + entry_offset if side == OrderSide.BUY else avg_price - entry_offset
            entry_value = avg_price * executed * contract_size
            execution_snapshot["paper_costs"] = costs.as_dict()

        # 5. Persiste trade como "open" e o desfecho do intent no MESMO commit.
        intent_row.status = "confirmed" if full else "partial"
        intent_row.order_id = entry_order.order_id
        intent_row.position_id = entry_order.position_id
        intent_row.result_state = entry_order.state.value
        intent_row.filled_quantity = executed
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
            quantity=executed,
            entry_value=entry_value.quantize(Decimal("0.01")),
            risk_amount=(
                Decimal(intent_row.risk_amount or 0) * executed / quantity
                if intent_row.risk_amount is not None
                else None
            ),
            capital_amount=(
                Decimal(intent_row.capital_amount or 0) * executed / quantity
                if intent_row.capital_amount is not None
                else None
            ),
            stop_loss=stop_loss,
            take_profit=take_profit,
            open_at=datetime.datetime.now(datetime.timezone.utc),
            open_order_id=entry_order.order_id,
            intent_id=intent_key,
            model_version=model_version,
            ml_confidence=ml_confidence,
        )
        self._db.add(trade)
        self._db.add(
            TradeEvent(
                trade=trade,
                event_type="ORDER_SENT",
                payload={"order_id": entry_order.order_id, "intent_id": intent_key, "price": str(avg_price)},
            )
        )
        try:
            await self._db.commit()
        except Exception as exc:
            await self._safe_rollback()
            logger.critical(
                "[executor] Persistência do trade falhou APÓS envio confirmado "
                "(intent %s): exposição existe externamente; proteção será tentada "
                "mesmo com banco indisponível.",
                intent_key,
            )
            # Exposição confirmada externamente: proteção não pode ser
            # bloqueada pela falha de banco.
            protection_error = None
            protection = None
            try:
                protection = await self._conn.modify_position_protection(
                    entry_order.position_id, stop_loss=stop_loss,
                    take_profit=take_profit, intent_id=uuid4().hex)
            except Exception as exc2:
                protection_error = str(exc2)
            if protection is not None and protection.state == ResultState.CONFIRMED:
                raise OrderExecutionError(
                    f"Trade persistence failed after confirmed send; position is PROTECTED. "
                    f"Recover by re-invoking open_position with intent_id={intent_key}"
                ) from exc
            closed = await self._fail_safe_after_protection_error(
                trade, entry_order.position_id, stop_loss, take_profit, protection_error
            )
            if closed:
                raise OrderExecutionError(
                    "Trade persistence failed after confirmed send; "
                    "position was closed to avoid unhedged risk"
                ) from exc
            raise OrderExecutionError(
                "Trade persistence failed after confirmed send; position remains open "
                f"UNPROTECTED for reconciliation. Recover by re-invoking open_position "
                f"with intent_id={intent_key}"
            ) from exc
        await self._db.refresh(trade)

        # 6. Proteção. Se falhar, não deixamos exposição nua: tentamos
        # fechar imediatamente (apenas a exposição CONFIRMADA — trade.quantity
        # é sempre a quantidade executada). Sem confirmação do fechamento, o
        # trade fica marcado para reconciliação e o erro é propagado.
        await self._protect_position_or_fail_safe(
            trade, entry_order.position_id, stop_loss, take_profit)

        if not full:
            # Entrada parcial: exposição executada está persistida e
            # protegida, mas o intent permanece "partial" (não resolvido) e
            # bloqueia novas entradas até a resolução explícita.
            raise OrderExecutionError(
                f"Entry partially executed ({executed}/{quantity}); executed "
                f"exposure is protected (intent_id={intent_key}); "
                f"reconciliation required")

        return trade

    async def _fail_safe_after_protection_error(
        self,
        trade: Trade,
        position_id: str,
        stop_loss: Decimal,
        take_profit: Decimal | None,
        protection_error: str | None,
    ) -> bool:
        """Fecha a posição quando a proteção não pôde ser confirmada.

        Persistência é best-effort: falha de banco nunca impede a tentativa
        de fechamento seguro nem a sinalização explícita para reconciliação.
        """
        self._add_event_best_effort(trade, "PROTECTION_FAILED", {
            "position_id": position_id,
            "stop_loss": str(stop_loss),
            "take_profit": str(take_profit) if take_profit is not None else None,
            "error": (protection_error or "not confirmed")[:250],
        })
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
            self._add_event_best_effort(trade, "PROTECTION_FAILED_CLOSED", {
                "position_id": position_id, "exit_price": str(close_order.average)},
            )
            await self._commit_best_effort()
            logger.critical(
                "[executor] Proteção falhou no trade #%s; posição fechada a mercado.",
                trade.id,
            )
            return True

        self._add_event_best_effort(trade, "UNPROTECTED_POSITION", {
            "position_id": position_id,
            "close_error": (close_error or "close not confirmed")[:250],
        })
        await self._commit_best_effort()
        logger.critical(
            "[executor] Trade #%s segue ABERTO E SEM PROTEÇÃO; reconciliação obrigatória.",
            trade.id,
        )
        return False

    # ------------------------------------------------------------------
    # Intenção estável / recuperação sem reenvio
    # ------------------------------------------------------------------

    async def _resolve_intent(
        self,
        intent_key: str,
        bound: ExecutionContext,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
    ) -> tuple[ExecutionIntent, Trade | None, bool]:
        """Persiste a intenção ANTES do envio, ou recupera uma existente.

        Retorna (linha do intent, trade já vinculado ou None, is_new).
        Recuperação exige exatamente o mesmo request; a reinvocação com o
        mesmo intent_id nunca reenvia a ordem (transport idempotente por id)
        nem duplica o trade (unicidade de trades.intent_id no banco).

        Uma intent NOVA só é criada quando nenhuma entrada não resolvida
        (planned/unknown/partial) existe para o mesmo request: exposição
        incerta bloqueia novas entradas até ser reconciliada.
        """
        try:
            intent_row = (await self._db.execute(
                select(ExecutionIntent)
                .where(ExecutionIntent.intent_id == intent_key)
                .with_for_update()
            )).scalar_one_or_none()
        except Exception as exc:
            await self._safe_rollback()
            logger.critical(
                "[executor] Consulta do intent %s falhou; ordem NÃO enviada: %s",
                intent_key, exc,
            )
            raise OrderExecutionError(
                f"Intent lookup failed before send; no order sent: {exc}") from exc

        if intent_row is None:
            # Sem checagem de conflito não há envio: exposição incerta de
            # outra intent para o mesmo request precisa ser resolvida antes.
            try:
                conflict = (await self._db.execute(
                    select(ExecutionIntent).where(
                        ExecutionIntent.market == bound.market,
                        ExecutionIntent.mode == bound.mode.value,
                        ExecutionIntent.symbol == symbol,
                        ExecutionIntent.side == side.value,
                        ExecutionIntent.status.in_(("planned", "unknown", "partial")),
                    ).limit(1)
                )).scalar_one_or_none()
            except Exception as exc:
                await self._safe_rollback()
                logger.critical(
                    "[executor] Consulta de intents conflitantes para %s %s falhou; "
                    "ordem NÃO enviada: %s",
                    symbol, side.value, exc,
                )
                raise OrderExecutionError(
                    f"Intent conflict lookup failed before send; no order sent: {exc}") from exc
            if conflict is not None:
                raise OrderExecutionError(
                    f"Conflicting unresolved entry intent {conflict.intent_id} "
                    f"({conflict.status}) for {symbol} {side.value}; "
                    f"reconcile it before new entries")
            intent_row = ExecutionIntent(
                intent_id=intent_key,
                market=bound.market,
                mode=bound.mode.value,
                symbol=symbol,
                side=side.value,
                quantity=quantity,
                status="planned",
            )
            self._db.add(intent_row)
            try:
                await self._db.commit()
            except Exception as exc:
                await self._safe_rollback()
                logger.critical(
                    "[executor] Intent %s não persistiu; ordem NÃO enviada: %s",
                    intent_key, exc,
                )
                raise OrderExecutionError(
                    f"Intent persistence failed before send; no order sent: {exc}") from exc
            return intent_row, None, True

        # Intenção já existe: só pode carregar exatamente o mesmo request.
        stored = (intent_row.market, intent_row.mode, intent_row.symbol,
                  intent_row.side, intent_row.quantity)
        requested = (bound.market, bound.mode.value, symbol, side.value, quantity)
        if stored != requested:
            raise OrderExecutionError(
                f"Intent {intent_key} already persisted with a different request; refusing to send")

        if intent_row.status == "failed":
            raise OrderExecutionError(
                f"Intent {intent_key} was rejected or cancelled; refusing to send")

        try:
            linked_trade = (await self._db.execute(
                select(Trade).where(Trade.intent_id == intent_key)
            )).scalar_one_or_none()
        except Exception as exc:
            await self._safe_rollback()
            logger.critical(
                "[executor] Consulta do trade do intent %s falhou; ordem NÃO enviada: %s",
                intent_key, exc,
            )
            raise OrderExecutionError(
                f"Trade lookup failed before send; no order sent: {exc}") from exc

        if linked_trade is None:
            if intent_row.status == "reserved":
                intent_row.status = "planned"
                try:
                    await self._db.commit()
                except Exception as exc:
                    await self._safe_rollback()
                    raise OrderExecutionError(
                        f"Intent activation failed before send; no order sent: {exc}"
                    ) from exc
                return intent_row, None, True
            logger.info(
                "[executor] Recuperando intent %s (status=%s) sem reenvio de ordem.",
                intent_key, intent_row.status,
            )
            return intent_row, None, False
        if linked_trade.status != "open":
            raise OrderExecutionError(
                f"Intent {intent_key} already linked to trade #{linked_trade.id} "
                f"({linked_trade.status}); no resend")

        # Trade já aberto por esta intenção: garantir proteção e devolvê-lo.
        logger.info(
            "[executor] Intent %s já executada (trade #%s); garantindo proteção sem reenvio.",
            intent_key, linked_trade.id,
        )
        await self._db.commit()
        await self._protect_position_or_fail_safe(
            linked_trade, linked_trade.position_id,
            linked_trade.stop_loss, linked_trade.take_profit,
        )
        return intent_row, linked_trade, False

    async def _protect_position_or_fail_safe(
        self,
        trade: Trade,
        position_id: str,
        stop_loss: Decimal,
        take_profit: Decimal | None,
    ) -> None:
        """Aplica proteção à exposição confirmada; sem confirmação, fail-safe.

        A tentativa de proteção nunca depende do banco: o evento de
        confirmação só é persistido depois de confirmado externamente.
        """
        protection = None
        protection_error = None
        try:
            protection = await self._conn.modify_position_protection(
                position_id, stop_loss=stop_loss, take_profit=take_profit,
                intent_id=uuid4().hex)
        except Exception as exc:
            protection_error = str(exc)
        if protection is None or protection.state != ResultState.CONFIRMED:
            closed = await self._fail_safe_after_protection_error(
                trade, position_id, stop_loss, take_profit, protection_error
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

    async def _record_intent_outcome(
        self,
        intent_key: str,
        result: ExecutionResult | None,
        *,
        status: str,
    ) -> None:
        """Best-effort: registra o desfecho externo de um envio sem trade.

        Só promove intents ainda não resolvidos ("planned"/"unknown"):
        nunca rebaixa um intent já confirmado, parcial ou resolvido.
        """
        try:
            row = (await self._db.execute(
                select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_key)
            )).scalar_one_or_none()
            if row is None or row.status not in ("planned", "unknown"):
                return
            row.status = status
            row.result_state = result.state.value if result is not None else None
            row.order_id = result.order_id if result is not None else None
            row.position_id = result.position_id if result is not None else None
            row.filled_quantity = result.filled if result is not None and result.filled else None
            await self._db.commit()
        except Exception as exc:
            await self._safe_rollback()
            logger.warning(
                "[executor] Desfecho do intent %s não persistiu: %s", intent_key, exc)

    def _add_event_best_effort(self, trade: Trade, event_type: str, payload: dict) -> None:
        try:
            self._db.add(TradeEvent(trade=trade, event_type=event_type, payload=payload))
        except Exception:
            logger.warning(
                "[executor] Evento %s não pôde ser agendado.", event_type, exc_info=True)

    async def _commit_best_effort(self) -> None:
        """Commit best-effort: falha de banco não bloqueia o caminho seguro."""
        try:
            await self._db.commit()
        except Exception as exc:
            await self._safe_rollback()
            logger.warning(
                "[executor] Persistência do fail-safe falhou (banco indisponível?): %s",
                exc,
            )

    async def _safe_rollback(self) -> None:
        """Rollback best-effort: não pode mascarar o erro original."""
        try:
            await self._db.rollback()
        except Exception:
            logger.warning(
                "[executor] Rollback falhou; sessão pode exigir descarte.",
                exc_info=True,
            )

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
