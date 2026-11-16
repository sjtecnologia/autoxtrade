"""Engine de execução de ordens com suporte a OCO e fallback de segurança."""
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ExecutionIntent, ProtectionAuthority, Trade, TradeEvent
from trading.connectors.base import BaseConnector
from trading.connectors.simulated import SimulatedConnector
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

    def __init__(self, connector: BaseConnector, db: AsyncSession, *,
                 evidence_provider=None, provider=None) -> None:
        self._conn = connector
        self._db = db
        self._evidence_provider = evidence_provider if evidence_provider is not None else provider
        self._protection_owner = uuid4().hex
        # Importação tardia para evitar circular (notificações dependem de models)
        self._telegram = None

    def _get_telegram(self):
        if self._telegram is None:
            from notifications.telegram import notifier
            self._telegram = notifier
        return self._telegram

    def _resolve_evidence_provider(self):
        """Explicit provider, else the single server-configured source.

        Without a configured source protection stays pending instead of
        inventing a quote. A misconfigured name is loud but never crashes the
        entry flow: it is treated as "no source" so the position stays pending.
        """
        if self._evidence_provider is not None:
            return self._evidence_provider
        try:
            from paper.protection_evidence_provider import resolve_protection_evidence_provider
            return resolve_protection_evidence_provider()
        except Exception:
            logger.exception(
                "[executor] Provider de evidências de proteção inválido; "
                "proteção paper permanecerá pendente"
            )
            return None

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
            intent_key, bound, symbol, side, quantity, entry_price, stop_loss, take_profit)
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
        executed = entry_order.filled or Decimal("0")
        if entry_order.state == ResultState.REJECTED and executed <= 0:
            await self._record_intent_outcome(intent_key, entry_order, status="failed")
            raise OrderExecutionError(f"Entry not fully confirmed: {entry_order.state.value}; reconciliation required")

        has_exposure = executed > 0 and entry_order.position_id is not None
        if not has_exposure:
            # Desfecho incerto (inclusive timeout sem resposta do transport):
            # ausência do recibo não impede um envio já ativado de chegar depois.
            await self._record_intent_outcome(intent_key, entry_order, status="unknown")
            raise OrderExecutionError(
                f"Entry outcome unknown (intent_id={intent_key}); no resend — "
                f"reconcile by re-invoking open_position with intent_id={intent_key}")

        # 4. Exposição executada (total ou parcial): trade registra SEMPRE
        #    a quantidade confirmada, e o intent o desfecho real.
        full = executed == quantity
        trade = None
        persistence_error = None
        try:
            trade = await self._persist_confirmed_entry(
                intent_row, intent_key, entry_order, bound, symbol, side,
                quantity, executed, stop_loss, take_profit, model_version,
                ml_confidence, full,
            )
        except Exception as exc:
            persistence_error = exc
            await self._safe_rollback()
            logger.critical(
                "[executor] Persistência/leitura do trade falhou após execução "
                "confirmada (intent %s); proteção será tentada: %s", intent_key, exc,
            )

        durable = getattr(getattr(self._conn, "_transport", None), "durable_effects", None)
        if durable is not None:
            await self._protect_initial_durable(
                durable, intent_key, entry_order, stop_loss, take_profit
            )
        else:
            await self._protect_position_or_fail_safe(
                trade, entry_order.position_id, stop_loss, take_profit,
                confirmed_quantity=executed, intent_key=intent_key,
            )
        if persistence_error is not None:
            raise OrderExecutionError(
                f"Trade persistence/read failed after confirmed send; position is PROTECTED. "
                f"Recover by re-invoking open_position with intent_id={intent_key}"
            ) from persistence_error
        if not full:
            raise OrderExecutionError(
                f"Entry partially executed ({executed}/{quantity}); executed "
                f"exposure is protected (intent_id={intent_key}); "
                f"reconciliation required")
        return trade

    async def _persist_confirmed_entry(
        self, intent_row, intent_key, entry_order, bound, symbol, side,
        quantity, executed, stop_loss, take_profit, model_version, ml_confidence, full,
    ) -> Trade:
        import datetime

        avg_price = entry_order.average
        execution_snapshot = bound.snapshot()
        execution_snapshot["capabilities"] = sorted(execution_snapshot["capabilities"])
        entry_value = avg_price * executed
        durable = None
        if bound.mode.value == "paper":
            from paper.costs import paper_costs_for, parse_costs
            durable = getattr(getattr(self._conn, "_transport", None), "durable_effects", None)
            if durable is not None:
                from paper.durable_effects import PaperEffectOperation
                entry_receipt = await self._db.get(PaperEffectOperation, (
                    durable._account_key, entry_order.position_id, "entry:" + intent_key,
                ))
                if entry_receipt is None or entry_receipt.payload.get("kind") != "entry":
                    raise OrderExecutionError("Durable entry receipt missing; costs cannot be reconstructed")
                costs = parse_costs({symbol: entry_receipt.payload.get("paper_costs")})[symbol]
            else:
                costs = paper_costs_for(symbol)
            contract_size = bound.instrument.contract_size
            entry_offset = costs.spread_price + costs.slippage_price
            avg_price = avg_price + entry_offset if side == OrderSide.BUY else avg_price - entry_offset
            entry_value = avg_price * executed * contract_size
            execution_snapshot["paper_costs"] = costs.as_dict()

        # 5. Persiste trade como "open" e o desfecho do intent no MESMO commit.
        durable = getattr(getattr(self._conn, "_transport", None), "durable_effects", None)
        intent_row.status = "protection_pending" if durable is not None else ("confirmed" if full else "partial")
        intent_row.order_id = entry_order.order_id
        intent_row.position_id = entry_order.position_id
        intent_row.result_state = entry_order.state.value
        intent_row.filled_quantity = executed
        intent_row.observation_revision = entry_order.revision
        intent_row.order_terminal = True if entry_order.terminal is True else None
        opened_at = datetime.datetime.now(datetime.timezone.utc)
        if durable is not None:
            from paper.entry_time import proven_entry_time
            opened_at = proven_entry_time(entry_order)
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
            open_at=opened_at,
            open_order_id=entry_order.order_id,
            intent_id=intent_key,
            protection_authority_id=intent_key,
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
        await self._db.commit()
        await self._db.refresh(trade)
        await self._db.commit()
        return trade

    async def _fail_safe_after_protection_error(
        self,
        trade: Trade | None,
        position_id: str,
        stop_loss: Decimal,
        take_profit: Decimal | None,
        protection_error: str | None,
        *,
        confirmed_quantity: Decimal | None = None,
        intent_key: str | None = None,
    ) -> bool:
        """Fecha a posição quando a proteção não pôde ser confirmada.

        Persistência é best-effort: falha de banco nunca impede a tentativa
        de fechamento seguro nem a sinalização explícita para reconciliação.
        """
        quantity = confirmed_quantity if confirmed_quantity is not None else trade.quantity
        close_error = None
        try:
            close_order = await self._conn.reduce_position(
                position_id, quantity, intent_id=uuid4().hex
            )
        except Exception as exc:
            close_order = None
            close_error = str(exc)

        closed = (
            close_order is not None
            and close_order.state == ResultState.CONFIRMED
            and close_order.filled == quantity
            and close_order.average is not None
        )
        try:
            if intent_key is not None:
                stored_trade = (await self._db.execute(
                    select(Trade).where(Trade.intent_id == intent_key)
                )).scalar_one_or_none()
                if stored_trade is not None:
                    trade = stored_trade
            if trade is None:
                logger.critical("[executor] Trade indisponível para registrar fail-safe do intent %s", intent_key)
                await self._commit_best_effort()
            else:
                self._add_event_best_effort(trade, "PROTECTION_FAILED", {
                    "position_id": position_id,
                    "stop_loss": str(stop_loss),
                    "take_profit": str(take_profit) if take_profit is not None else None,
                    "error": (protection_error or "not confirmed")[:250],
                })
                if closed:
                    self._finalize_trade(
                        trade, close_order.average, "PROTECTION_FAILED", close_order.order_id
                    )
                    self._add_event_best_effort(trade, "PROTECTION_FAILED_CLOSED", {
                        "position_id": position_id, "exit_price": str(close_order.average),
                    })
                else:
                    self._add_event_best_effort(trade, "UNPROTECTED_POSITION", {
                        "position_id": position_id,
                        "close_error": (close_error or "close not confirmed")[:250],
                        "close_state": close_order.state.value if close_order is not None else "unknown",
                        "close_filled": str(close_order.filled) if close_order is not None else None,
                        "close_order_id": close_order.order_id if close_order is not None else None,
                        "close_average": str(close_order.average) if close_order is not None and close_order.average is not None else None,
                        "close_reason": close_order.reason if close_order is not None else close_error,
                    })
                await self._commit_best_effort()
        except Exception as exc:
            await self._safe_rollback()
            logger.critical("[executor] Falha ao registrar resultado do fail-safe para intent %s: %s", intent_key, exc)
        if closed:
            logger.critical(
                "[executor] Proteção falhou para posição %s; fechamento confirmado.", position_id,
            )
            return True

        logger.critical(
            "[executor] Posição %s sem proteção; fechamento não confirmado (estado=%s, quantidade=%s); reconciliação obrigatória.",
            position_id, close_order.state.value if close_order is not None else "unknown",
            close_order.filled if close_order is not None else None,
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
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal | None,
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
                        ExecutionIntent.status.in_(("planned", "unknown", "partial", "protection_pending")),
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
                await self._ensure_protection_authority(
                    intent_key, bound, side, quantity, entry_price, stop_loss, take_profit,
                    create=True,
                )
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
            await self._ensure_protection_authority(
                intent_key, bound, side, quantity, entry_price, stop_loss, take_profit,
                create=intent_row.status == "reserved",
            )
        except Exception:
            await self._safe_rollback()
            raise

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

        if intent_row.status in {"partial", "protection_pending"}:
            await self._db.commit()
            durable = getattr(getattr(self._conn, "_transport", None), "durable_effects", None)
            if durable is not None and intent_row.observation_revision == 1:
                observed = await self._conn.query_order(intent_id=intent_key)
                if observed.revision == 1 and observed.state in {ResultState.CONFIRMED, ResultState.PARTIAL}:
                    await self._protect_initial_durable(
                        durable, intent_key, observed, linked_trade.stop_loss, linked_trade.take_profit
                    )
                    return intent_row, linked_trade, False

            return await self._reconcile_partial_trade(intent_key, bound, quantity)

        # Trade já aberto por esta intenção: garantir proteção e devolvê-lo.
        logger.info(
            "[executor] Intent %s já executada (trade #%s); garantindo proteção sem reenvio.",
            intent_key, linked_trade.id,
        )
        position_id = linked_trade.position_id
        stop_loss = linked_trade.stop_loss
        take_profit = linked_trade.take_profit
        confirmed_quantity = linked_trade.quantity
        commit_error = None
        try:
            await self._db.commit()
        except Exception as exc:
            commit_error = exc
            await self._safe_rollback()
        durable = getattr(getattr(self._conn, "_transport", None), "durable_effects", None)
        if durable is not None:
            observed = await self._conn.query_order(intent_id=intent_key)
            await self._protect_initial_durable(durable, intent_key, observed, stop_loss, take_profit)
        else:
            await self._protect_position_or_fail_safe(
                linked_trade, position_id, stop_loss, take_profit,
                confirmed_quantity=confirmed_quantity, intent_key=intent_key,
            )
        if commit_error is not None:
            raise OrderExecutionError(
                f"Recovery persistence failed; position is PROTECTED, reconcile intent_id={intent_key}"
            ) from commit_error
        return intent_row, linked_trade, False

    async def _ensure_protection_authority(
        self, intent_key: str, bound: ExecutionContext, side: OrderSide,
        quantity: Decimal, entry_price: Decimal, stop_loss: Decimal,
        take_profit: Decimal | None, *, create: bool,
    ) -> None:
        snapshot = bound.snapshot()
        snapshot["capabilities"] = sorted(snapshot["capabilities"])
        authority = (await self._db.execute(
            select(ProtectionAuthority).where(ProtectionAuthority.intent_id == intent_key)
            .with_for_update()
        )).scalar_one_or_none()
        if authority is None:
            if not create:
                raise OrderExecutionError(
                    f"Intent {intent_key} has no durable protection authority; reconciliation required")
            self._db.add(ProtectionAuthority(
                intent_id=intent_key, execution_context=snapshot, side=side.value,
                quantity=quantity, entry_price=entry_price,
                initial_stop_loss=stop_loss, initial_take_profit=take_profit,
                stop_loss=stop_loss, take_profit=take_profit,
            ))
        elif (
            authority.execution_context != snapshot or authority.side != side.value
            or authority.quantity != quantity or authority.entry_price != entry_price
            or authority.initial_stop_loss != stop_loss
            or authority.initial_take_profit != take_profit
        ):
            raise OrderExecutionError(
                f"Intent {intent_key} already persisted with a different request; refusing to send")

    async def _reconcile_partial_trade(
        self, intent_key: str, bound: ExecutionContext, quantity: Decimal,
    ) -> tuple[ExecutionIntent, Trade, bool]:
        """Reconcile cumulative fills without changing external protection."""
        try:
            observed = await self._conn.query_order(intent_id=intent_key)
        except Exception as exc:
            raise OrderExecutionError(
                f"Partial order query unavailable for intent_id={intent_key}; reservation preserved"
            ) from exc
        if observed.state in {ResultState.UNKNOWN, ResultState.UNAVAILABLE}:
            raise OrderExecutionError(
                f"Partial order outcome {observed.state.value} for intent_id={intent_key}; reservation preserved"
            )
        if (
            observed.intent_id != intent_key
            or observed.filled is None
            or not observed.filled.is_finite()
            or observed.filled <= 0
            or observed.filled > quantity
            or observed.average is None
            or not observed.average.is_finite()
            or observed.average <= 0
            or not observed.position_id
            or (observed.revision is not None
                and (type(observed.revision) is not int or observed.revision < 1))
        ):
            raise OrderExecutionError(f"Invalid partial order observation for intent_id={intent_key}")

        try:
            intent = (await self._db.execute(
                select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_key)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            trade = (await self._db.execute(
                select(Trade).where(Trade.intent_id == intent_key)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if (
                intent is None or trade is None or trade.status != "open"
                or intent.quantity != quantity
                or intent.status not in {"partial", "protection_pending"}
                or trade.position_id != observed.position_id
                or (intent.order_id and observed.order_id != intent.order_id)
            ):
                raise OrderExecutionError(f"Partial order identity conflict for intent_id={intent_key}")
            stored_filled = Decimal(intent.filled_quantity or 0)
            if trade.quantity != stored_filled:
                raise OrderExecutionError(
                    f"Partial trade and intent quantities disagree for intent_id={intent_key}; reconciliation required"
                )
            if intent.order_terminal is True and observed.filled > stored_filled:
                raise OrderExecutionError(
                    f"Fill contradicts terminal observation for intent_id={intent_key}; reconciliation required"
                )
            stored_revision = intent.observation_revision
            revision_is_new = (
                observed.revision is not None
                and (stored_revision is None or observed.revision > stored_revision)
            )
            revision_is_stale = (
                stored_revision is not None
                and (observed.revision is None or observed.revision <= stored_revision)
            )
            if not revision_is_stale and (
                observed.filled > stored_filled
                or (observed.filled == stored_filled and revision_is_new)
            ):
                average = observed.average
                if bound.mode.value == "paper":
                    from paper.costs import parse_costs
                    costs = parse_costs({
                        bound.instrument.symbol: (trade.execution_context or {}).get("paper_costs")
                    })[bound.instrument.symbol]
                    offset = costs.spread_price + costs.slippage_price
                    average = average + offset if trade.side == "buy" else average - offset
                contract_size = bound.instrument.contract_size if bound.mode.value == "paper" else Decimal("1")
                trade.quantity = observed.filled
                trade.entry_price = average
                trade.entry_value = (average * observed.filled * contract_size).quantize(Decimal("0.01"))
                if intent.risk_amount is not None:
                    trade.risk_amount = intent.risk_amount * observed.filled / quantity
                if intent.capital_amount is not None:
                    trade.capital_amount = intent.capital_amount * observed.filled / quantity
                intent.filled_quantity = observed.filled
                intent.result_state = observed.state.value
                if observed.revision is not None:
                    intent.observation_revision = observed.revision
            if not revision_is_stale and observed.filled == Decimal(intent.filled_quantity or 0) and observed.terminal is True:
                intent.order_terminal = True
            intent.status = "protection_pending"
            await self._db.commit()
            logger.warning(
                "[executor] Intent %s reconciliada; proteção e fencing requerem verificação",
                intent_key,
            )
            return intent, trade, False
        except Exception:
            await self._safe_rollback()
            raise

    async def _protect_initial_durable(
        self, durable, intent_key: str, entry: ExecutionResult,
        stop_loss: Decimal, take_profit: Decimal | None,
        *, finalize: Callable[[AsyncSession, ExecutionIntent, Trade, dict], Awaitable[None]] | None = None,
    ) -> None:
        if (entry.state not in {ResultState.CONFIRMED, ResultState.PARTIAL}
                or not entry.position_id or not entry.revision or entry.filled <= 0):
            raise OrderExecutionError("Initial protection exposure is uncertain; reservation preserved")
        try:
            # The attempt never carries a quote: protection is established only
            # through validated market evidence, never an invented entry price.
            operation_id, fence, payload = await durable.prepare_initial_protection(
                entry.position_id, quantity=entry.filled, revision=entry.revision,
                stop_loss=stop_loss, take_profit=take_profit,
                owner=self._protection_owner,
            )
            evidence_id = None
            result = await durable.query_initial_protection_receipt(
                entry.position_id, operation_id, fence, payload, self._protection_owner,
            )
            if result is None:
                provider = self._resolve_evidence_provider()
                if provider is None:
                    raise OrderExecutionError(
                        "Initial protection requires a configured market evidence provider"
                    )
                from paper.protection_evidence_store import provide_and_record_protection_evidence
                evidence_id, _ = await provide_and_record_protection_evidence(
                    durable, entry.position_id, operation_id, provider=provider,
                )
                result = await self._conn.modify_position_protection(
                    entry.position_id, stop_loss=stop_loss, take_profit=take_profit,
                    intent_id=operation_id, fence=fence,
                    revision=entry.revision, quantity=entry.filled, owner=self._protection_owner,
                    evidence_id=evidence_id,
                )
            if result.state != ResultState.CONFIRMED or result.position_id != entry.position_id:
                raise OrderExecutionError("Initial protection outcome is not confirmed")
        except Exception as exc:
            logger.critical("[executor] Proteção inicial incerta para intent %s; reserva preservada: %s",
                            intent_key, exc)
            raise OrderExecutionError("Initial protection pending; reservation preserved") from exc

        try:
            intent = (await self._db.execute(
                select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_key)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            trade = (await self._db.execute(
                select(Trade).where(Trade.intent_id == intent_key)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            valid = await durable.confirm_initial_protection(
                self._db, entry.position_id, operation_id, fence, payload, self._protection_owner,
                evidence_id=evidence_id,
            )
            if (not valid or intent is None or trade is None or trade.status != "open"
                    or trade.position_id != entry.position_id or trade.quantity != entry.filled
                    or intent.observation_revision != entry.revision or trade.stop_loss != stop_loss
                    or trade.take_profit != take_profit):
                await self._db.rollback()
                raise OrderExecutionError("Initial protection confirmed but state changed; reconciliation required")
            if finalize is not None:
                await finalize(self._db, intent, trade, payload)
            intent.status = "confirmed" if entry.filled == intent.quantity else "partial"
            self._db.add(TradeEvent(trade=trade, event_type="PROTECTION_CONFIRMED",
                                    payload={"position_id": entry.position_id, "operation_id": operation_id,
                                             "revision": entry.revision}))
            await durable.release_initial_protection_claim(
                self._db, entry.position_id, operation_id, fence, self._protection_owner)
            await self._db.commit()
        except Exception:
            await self._safe_rollback()
            raise

    async def _protect_position_or_fail_safe(
        self,
        trade: Trade | None,
        position_id: str,
        stop_loss: Decimal,
        take_profit: Decimal | None,
        *,
        confirmed_quantity: Decimal | None = None,
        intent_key: str | None = None,
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
            if protection is not None:
                protection_error = f"{protection.state.value}: {protection.reason or 'not confirmed'}"
            closed = await self._fail_safe_after_protection_error(
                trade, position_id, stop_loss, take_profit, protection_error,
                confirmed_quantity=confirmed_quantity, intent_key=intent_key,
            )
            if closed:
                raise OrderExecutionError(
                    "Protection not confirmed; position was closed to avoid unhedged risk"
                )
            raise OrderExecutionError(
                "Protection not confirmed; position remains open UNPROTECTED for reconciliation"
            )
        try:
            if intent_key is not None:
                stored_trade = (await self._db.execute(
                    select(Trade).where(Trade.intent_id == intent_key)
                )).scalar_one_or_none()
                if stored_trade is not None:
                    trade = stored_trade
            if trade is None:
                logger.error("[executor] Proteção confirmada mas trade do intent %s indisponível", intent_key)
                return
            self._db.add(TradeEvent(trade=trade, event_type="PROTECTION_CONFIRMED",
                                    payload={"position_id": protection.position_id}))
            await self._db.commit()
        except Exception as exc:
            await self._safe_rollback()
            logger.error("[executor] Proteção confirmada, mas evento não persistiu: %s", exc)
            raise OrderExecutionError(
                f"Protection confirmed but persistence failed; reconcile intent_id={intent_key}"
            ) from exc

    async def _record_intent_outcome(
        self,
        intent_key: str,
        result: ExecutionResult | None,
        *,
        status: str,
    ) -> None:
        """Record an unresolved outcome without overwriting newer durable facts."""
        try:
            row = (await self._db.execute(
                select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_key)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if row is None or row.status not in ("planned", "unknown"):
                await self._db.commit()
                return
            if (row.observation_revision is not None
                    or row.order_terminal is True
                    or Decimal(row.filled_quantity or 0) > 0):
                await self._db.commit()
                return
            if status == "failed" and (row.order_id is not None or row.position_id is not None):
                logger.error("[executor] Intent %s já possui identidade; falha não aplicada", intent_key)
                await self._db.commit()
                return
            if result is not None and (
                (result.order_id is not None and row.order_id not in (None, result.order_id))
                or (result.position_id is not None and row.position_id not in (None, result.position_id))
            ):
                logger.error("[executor] Identidade de resultado conflitante para intent %s", intent_key)
                await self._db.commit()
                return
            durable = getattr(getattr(self._conn, "_transport", None), "durable_effects", None)
            if status == "failed" and durable is not None:
                from paper.durable_effects import PaperEffectPosition
                position = (await self._db.execute(select(PaperEffectPosition).where(
                    PaperEffectPosition.account_key == durable._account_key,
                    PaperEffectPosition.position_id == "paper_" + intent_key,
                ).execution_options(populate_existing=True))).scalar_one_or_none()
                if position is not None:
                    logger.error("[executor] Intent %s possui efeito paper; falha não aplicada", intent_key)
                    await self._db.commit()
                    return
            row.status = status
            if result is not None:
                if row.result_state in (None, ResultState.UNKNOWN.value):
                    row.result_state = result.state.value
                if row.order_id is None and result.order_id is not None:
                    row.order_id = result.order_id
                if row.position_id is None and result.position_id is not None:
                    row.position_id = result.position_id
                if result.filled and not row.filled_quantity:
                    row.filled_quantity = result.filled
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

    async def close_position(
        self, trade: Trade, reason: str = "manual", *, execution_price: Decimal | None = None,
        candle: datetime | None = None, timeframe: str | None = None,
        previous_cursor: datetime | None = None,
        previous_timeframe: str | None = None,
    ) -> Trade:
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
        durable = getattr(getattr(self._conn, "_transport", None), "durable_effects", None)
        if durable is not None:
            if not isinstance(self._conn, SimulatedConnector):
                raise OrderExecutionError("Durable paper close requires the simulated connector")
            if execution_price is None:
                raise OrderExecutionError("Durable paper close requires an explicit execution price")
            trade_id, position_id, quantity = trade.id, trade.position_id, trade.quantity
            await self._db.commit()
            return await self._close_durable_paper(
                self._conn, durable, trade_id, position_id, quantity, reason, execution_price,
                candle, timeframe, previous_cursor, previous_timeframe,
            )
        if candle is not None:
            raise OrderExecutionError("Monitor decision requires durable paper effects")
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

    async def _close_durable_paper(self, connector: SimulatedConnector, durable, trade_id: int, position_id: str,
                                   quantity: Decimal, reason: str, execution_price: Decimal,
                                   candle: datetime | None, timeframe: str | None,
                                   previous_cursor: datetime | None, previous_timeframe: str | None) -> Trade:
        try:
            operation_id, fence, payload = await durable.prepare_manual_close(
                position_id, trade_id=trade_id, quantity=quantity, reason=reason,
                execution_price=execution_price, owner=self._protection_owner,
                candle=candle, timeframe=timeframe, previous_cursor=previous_cursor,
                previous_timeframe=previous_timeframe,
            )
            result, _ = await durable.confirm_manual_close(
                self._db, position_id, operation_id, fence, payload, self._protection_owner
            )
            await self._db.commit()
            if result is None:
                result = await connector.reduce_position(
                    position_id, quantity, intent_id=operation_id, fence=fence,
                    execution_price=execution_price, owner=self._protection_owner,
                )
            if (result.state != ResultState.CONFIRMED or result.position_id != position_id
                    or result.filled != quantity or result.average != execution_price):
                raise OrderExecutionError("Close outcome uncertain or partial; reservation preserved")
            receipt, locked_trade = await durable.confirm_manual_close(
                self._db, position_id, operation_id, fence, payload, self._protection_owner
            )
            if receipt is None or receipt.average is None or locked_trade is None:
                raise OrderExecutionError("Close receipt missing; reservation preserved")
            if locked_trade.status == "open":
                self._finalize_trade(locked_trade, receipt.average, reason, operation_id)
                if candle is not None:
                    locked_trade.paper_monitor_candle_at = candle
                    locked_trade.paper_monitor_timeframe = timeframe
                self._db.add(TradeEvent(
                    trade=locked_trade, event_type=reason if candle is not None else "MANUAL_CLOSE",
                    payload={"reason": reason, "exit_price": str(locked_trade.exit_price),
                             "operation_id": operation_id, "candle": payload["candle"]},
                ))
            elif locked_trade.close_order_id != operation_id or locked_trade.close_reason != reason:
                raise OrderExecutionError("Closed trade conflicts with close decision")
            await durable.release_initial_protection_claim(
                self._db, position_id, operation_id, fence, self._protection_owner
            )
            await self._db.commit()
            return locked_trade
        except Exception as exc:
            await self._safe_rollback()
            logger.warning("[executor] Fechamento paper %s não resolvido: %s", position_id, exc)
            raise OrderExecutionError("Durable close pending; reconciliation required") from exc

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
