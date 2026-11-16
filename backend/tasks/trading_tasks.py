"""Tarefa principal do bot: scan de sinais e execução de trades.

Roda a cada 15 minutos via Celery Beat.
Guard de horário:
  - B3   → só opera entre 10h00 e 17h00 BRT (dias úteis, feriados excluídos)
  - FOREX → Mon-Fri, 24h (sem restrição de horário)
  - CRIPTO → sem restrição
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from celery import shared_task

logger = logging.getLogger(__name__)


async def _require_approved_entry(db, market: str, mode: str, symbol: str = "") -> None:
    """Recheck pause and mode at execution, not only when the signal was created."""
    from sqlalchemy import select
    from db.models import BotConfig
    from trading.safety import ExposureBlocked, require_entry_mode, require_paper_new_exposure
    if mode == "paper":
        require_paper_new_exposure(symbol)
    else:
        require_entry_mode(mode)
    result = await db.execute(select(BotConfig).where(BotConfig.market == market))
    config = result.scalar_one_or_none()
    if config is None or not config.is_active or config.drawdown_status == "PAUSED":
        raise ExposureBlocked("New entries paused or market not configured")
    if config.mode != mode:
        raise ExposureBlocked("Approval mode no longer matches market configuration")
    return config


def _demo_trade_plan(symbol: str, market: str) -> tuple[OrderSide, Decimal, Decimal, Decimal | None, Decimal]:
    """Gera um trade de fallback simples para deixar o sistema visível no dashboard."""
    from trading.connectors.types import OrderSide

    side = OrderSide.BUY
    entry_price = Decimal("100.00")
    stop_loss = Decimal("98.00")
    take_profit = Decimal("104.00")
    quantity = Decimal("1")

    if market == "FOREX":
        entry_price = Decimal("1.1000")
        stop_loss = Decimal("1.0900")
        take_profit = Decimal("1.1150")
    elif market == "CRIPTO":
        entry_price = Decimal("50000.00")
        stop_loss = Decimal("49000.00")
        take_profit = Decimal("52000.00")

    if symbol:
        if symbol.upper().startswith("WDO"):
            entry_price = Decimal("120.00")
            stop_loss = Decimal("118.00")
            take_profit = Decimal("124.00")
        elif symbol.upper().startswith("WIN"):
            entry_price = Decimal("110.00")
            stop_loss = Decimal("108.00")
            take_profit = Decimal("114.00")

    return side, entry_price, stop_loss, take_profit, quantity


@shared_task(name="tasks.trading_tasks.scan_and_trade")
def scan_and_trade() -> None:
    """Varre todos os BotConfigs ativos e executa sinais."""
    asyncio.run(_scan_and_trade_async())


@shared_task(name="tasks.trading_tasks.process_entry_approvals")
def process_entry_approvals() -> None:
    """Executa entradas DiDi aprovadas manualmente."""
    asyncio.run(_process_entry_approvals_async())


@shared_task(name="tasks.trading_tasks.monitor_open_positions")
def monitor_open_positions() -> None:
    """Monitora saida e safe break das posicoes abertas."""
    asyncio.run(_monitor_open_positions_async())


@shared_task(name="tasks.trading_tasks.recover_paper_protection")
def recover_paper_protection() -> dict[str, int]:
    """Recover protection only for persisted simulated paper positions."""
    return asyncio.run(_recover_paper_protection_async())


async def _recover_paper_protection_async() -> dict[str, int]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from config import settings
    from paper.protection_evidence_provider import resolve_protection_evidence_provider
    from paper.protection_recovery import recover_pending_paper_protection

    # The server selects the single evidence source; without one protection
    # stays pending instead of falling back to a synthetic quote.
    try:
        evidence_provider = resolve_protection_evidence_provider()
    except Exception:
        logger.exception(
            "Provider de evidências de proteção inválido; recuperação paper bloqueada com segurança"
        )
        evidence_provider = None

    engine = create_async_engine(settings.database_url)
    try:
        return await recover_pending_paper_protection(
            async_sessionmaker(engine, expire_on_commit=False),
            evidence_provider=evidence_provider,
        )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Guard de horário
# ---------------------------------------------------------------------------

def _is_market_open(market: str) -> bool:
    """Retorna True se o mercado está no horário de operação."""
    now_utc = datetime.now(timezone.utc)

    if market == "B3":
        # Usa is_b3_open da B3FeatureEngineer (horário 09h–17h55 BRT)
        # Internamente restringe a 10h–17h para evitar leilões
        from ml.b3_feature_engineer import B3FeatureEngineer

        if not B3FeatureEngineer.is_b3_open(now_utc):
            return False
        # Restrição adicional: evitar primeiros e últimos 45 min (leilões)
        import pytz

        brt = pytz.timezone("America/Sao_Paulo")
        now_brt = now_utc.astimezone(brt)
        if now_brt.hour < 10 or (now_brt.hour == 17 and now_brt.minute >= 0):
            return False
        return True

    if market == "FOREX":
        # Forex fecha fim de semana (sexta ~22h UTC até domingo ~22h UTC)
        # weekday(): 5=sábado, 6=domingo
        wd = now_utc.weekday()
        if wd == 5:
            return False  # sábado inteiro
        if wd == 6 and now_utc.hour < 22:
            return False  # domingo antes de 22h UTC
        if wd == 4 and now_utc.hour >= 22:
            return False  # sexta após 22h UTC
        return True

    # CRIPTO — opera 24/7
    return True


# ---------------------------------------------------------------------------
# Loop principal
# ---------------------------------------------------------------------------

async def _scan_and_trade_async() -> None:
    from decimal import Decimal

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from config import settings
    from db.models import BotConfig
    from ml.predictor import ModelNotFoundError, ModelPredictor
    from risk.correlation_checker import CorrelationChecker
    from risk.drawdown_monitor import DrawdownMonitor
    from risk.manager import RiskManager
    from risk.position_sizer import PositionSizer
    from trading.connectors.factory import get_connector
    from trading.connectors.types import OrderSide
    from trading.order_executor import OrderExecutor
    from trading.paper_trader import PaperTrader

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        result = await db.execute(
            select(BotConfig).where(BotConfig.is_active == True)
        )
        configs: list[BotConfig] = result.scalars().all()

    await engine.dispose()

    for config in configs:
        market = config.market

        # Guard de horário
        if not _is_market_open(market):
            logger.debug("[trading] %s fora do horário — pulando.", market)
            continue

        await _process_market(config, settings)


async def _process_market(config, settings) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from notifications.templates import format_entry_approval_request
    from notifications.telegram import notifier
    from risk.correlation_checker import CorrelationChecker
    from risk.drawdown_monitor import DrawdownMonitor
    from risk.manager import RiskManager
    from risk.position_sizer import PositionSizer
    from trading.approval_service import EntryApprovalService
    from trading.didi_strategy import evaluate_entry

    market = config.market
    symbols: list[str] = config.enabled_symbols or []

    if not symbols:
        logger.warning("[trading] %s sem simbolos configurados.", market)
        return

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    try:
        strategy_mode = (settings.trading_strategy_mode or "didi_manual").lower().strip()
        if strategy_mode == "didi_manual":
            await _process_didi_manual_entries(config, settings, Session)
            return

        # fallback: fluxo legado ML (mantido para compatibilidade)
        await _process_ml_entries(config, settings, Session)
    finally:
        await engine.dispose()


async def _process_didi_manual_entries(config, settings, Session) -> None:
    from notifications.chart_render import render_entry_chart
    from notifications.templates import format_entry_approval_caption, format_entry_approval_request
    from notifications.telegram import notifier
    from risk.correlation_checker import CorrelationChecker
    from risk.drawdown_monitor import DrawdownMonitor
    from risk.manager import RiskManager
    from risk.position_sizer import PositionSizer
    from trading.approval_service import EntryApprovalService
    from trading.didi_strategy import evaluate_entry
    from trading.safety import ExposureBlocked, require_paper_new_exposure

    market = config.market
    symbols: list[str] = config.enabled_symbols or []

    if not settings.paper_new_exposure_enabled:
        logger.warning("[safety] Novas entradas paper desativadas até qualificação OOS positiva")
        return

    approval_service = EntryApprovalService()
    try:
        for symbol in symbols:
            try:
                require_paper_new_exposure(symbol)
            except ExposureBlocked as exc:
                logger.warning("[safety] %s: entrada paper bloqueada: %s", symbol, exc)
                continue
            # Decisão de entrada exige candles fechados e frescos; sem fallback.
            df = _load_fresh_ohlcv(symbol, market, settings)
            if df is None or len(df) < 80:
                continue

            entry_eval = evaluate_entry(
                df,
                adx_min=float(settings.didi_entry_adx_min),
                rr_target=Decimal(str(settings.didi_rr_target)),
                safe_rr=Decimal(str(settings.didi_safe_break_rr)),
            )
            if entry_eval is None:
                continue

            try:
                async with Session() as db:
                    sizer = PositionSizer(db)
                    dd_monitor = DrawdownMonitor(db)
                    corr_checker = CorrelationChecker(db)
                    risk = RiskManager(
                        db=db,
                        position_sizer=sizer,
                        drawdown_monitor=dd_monitor,
                        correlation_checker=corr_checker,
                    )

                    risk_result = await risk.can_open_position(
                        market=market,
                        symbol=symbol,
                        entry_price=entry_eval.entry_price,
                        stop_loss_price=entry_eval.stop_loss,
                        side=str(getattr(entry_eval.direction, "value", entry_eval.direction)),
                        reservation_ttl_seconds=int(settings.didi_approval_ttl_sec),
                    )
                    if not risk_result.approved:
                        continue

                    qty = risk_result.position_size.quantity if risk_result.position_size else Decimal("0")
                    if qty <= 0:
                        continue
                    if risk_result.intent_id is None:
                        raise RuntimeError("Risk approval returned without a reservation id")

                    from trading.contracts import context_from_dict

                    bindings = config.execution_context or {}
                    bound = context_from_dict(bindings.get(symbol))
                    contract_size = bound.instrument.contract_size or Decimal("1")

                    payload = await approval_service.create_pending_entry(
                        intent_id=risk_result.intent_id,
                        market=market,
                        symbol=symbol,
                        side=entry_eval.direction,
                        mode=config.mode,
                        entry_price=entry_eval.entry_price,
                        stop_loss=entry_eval.stop_loss,
                        take_profit=entry_eval.take_profit,
                        quantity=qty,
                        criteria=entry_eval.criteria,
                        details=entry_eval.details,
                        ttl_seconds=int(settings.didi_approval_ttl_sec),
                        timeframe=str(getattr(settings, "didi_timeframe", "") or ""),
                        gain_safe=entry_eval.gain_safe,
                        loss_safe=entry_eval.loss_safe,
                        risk_per_unit=entry_eval.risk_per_unit,
                        risk_reward=entry_eval.risk_reward,
                        risk_amount=entry_eval.risk_per_unit * qty * contract_size,
                        potential_gain=(
                            abs(entry_eval.take_profit - entry_eval.entry_price)
                            * qty
                            * contract_size
                        ),
                        analysis=entry_eval.analysis,
                        chart=entry_eval.chart,
                    )
                    if payload is None:
                        await risk.release_confirmed_reservation(risk_result.intent_id)
                    if payload:
                        image = render_entry_chart(payload, entry_eval.chart)
                        if image:
                            await notifier.send_photo(image, format_entry_approval_caption(payload))
                        await notifier.send_message(format_entry_approval_request(payload))
                        logger.info(
                            "[didi] Entrada aguardando aprovacao: %s %s (%s)",
                            market,
                            symbol,
                            payload["id"],
                        )
            except Exception as exc:
                logger.error("[didi] Erro ao gerar aprovacao %s %s: %s", market, symbol, exc, exc_info=True)
    finally:
        await approval_service.close()


async def _process_ml_entries(config, settings, Session) -> None:
    from ml.predictor import ModelNotFoundError, ModelPredictor

    market = config.market
    symbols: list[str] = config.enabled_symbols or []
    predictor = ModelPredictor()
    for symbol in symbols:
        df = None
        try:
            async with Session() as db:
                await predictor.load_model(symbol, db)
            df = _load_ohlcv(symbol, market, settings)
        except ModelNotFoundError:
            logger.info("[trading] Sem modelo ativo para %s %s.", market, symbol)
            continue
        except Exception as exc:
            logger.warning("[trading] Falha ao carregar modelo para %s %s: %s", market, symbol, exc)
            continue

        if df is None or len(df) < 50:
            continue

        result = predictor.predict(symbol, df, market=market)
        logger.info("[trading] %s %s → %s (%.2f%%)", market, symbol, result.signal, result.confidence * 100)


async def _current_price_for_approval(
    symbol: str,
    market: str,
    mode: str,
    execution_context,
    settings,
) -> Decimal:
    """Obtém preço atual, rejeitando cotação velha em vez de usar preço congelado."""
    if mode == "paper":
        import pandas as pd

        df = _load_ohlcv(symbol, market, settings)
        if df is None or df.empty:
            raise ValueError("preco_atual_indisponivel")
        last_ts = pd.Timestamp(
            df["timestamp"].iloc[-1] if "timestamp" in df.columns else df.index[-1]
        )
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        age_sec = (pd.Timestamp.now(tz="UTC") - last_ts).total_seconds()
        max_age = int(getattr(settings, "didi_max_quote_age_sec", 7200) or 7200)
        if age_sec > max_age:
            raise ValueError(f"cotacao_desatualizada_{int(age_sec)}s")
        return Decimal(str(df["close"].iloc[-1]))

    from trading.connectors.factory import get_connector

    connector = get_connector(execution_context, mode=mode)
    await connector.connect()
    try:
        price = Decimal(str(await connector.get_current_price(symbol)))
    finally:
        await _close_connector(connector)
    if price <= 0:
        raise ValueError("preco_atual_invalido")
    return price


def _reprice_approval_levels(
    side: str,
    approved_entry: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal | None,
    current_price: Decimal,
    max_slippage_pct: float,
    tick_size: Decimal | None,
) -> tuple[Decimal, Decimal, Decimal | None]:
    """Reposiciona entrada/SL/TP preservando distâncias, ou rejeita se desviou demais."""
    if approved_entry <= 0 or current_price <= 0:
        raise ValueError("preco_atual_invalido")
    slippage_pct = abs(current_price - approved_entry) / approved_entry * Decimal("100")
    if slippage_pct > Decimal(str(max_slippage_pct)):
        raise ValueError(f"preco_desviou_{slippage_pct:.3f}pct")

    risk_distance = abs(approved_entry - stop_loss)
    reward_distance = abs(take_profit - approved_entry) if take_profit is not None else None

    def align(price: Decimal) -> Decimal:
        if not tick_size or tick_size <= 0:
            return price
        from decimal import ROUND_HALF_UP
        return (price / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size

    new_entry = align(current_price)
    if side == "buy":
        new_stop = align(current_price - risk_distance)
        new_target = align(current_price + reward_distance) if reward_distance is not None else None
        if new_stop >= new_entry or (new_target is not None and new_target <= new_entry):
            raise ValueError("niveis_invalidos_apos_reprecificacao")
    else:
        new_stop = align(current_price + risk_distance)
        new_target = align(current_price - reward_distance) if reward_distance is not None else None
        if new_stop <= new_entry or (new_target is not None and new_target >= new_entry):
            raise ValueError("niveis_invalidos_apos_reprecificacao")
    return new_entry, new_stop, new_target


async def _process_entry_approvals_async() -> None:
    import contextlib
    from datetime import datetime, timezone

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from config import settings
    from db.models import BotConfig, ExecutionIntent, ProtectionAuthority, Trade
    from notifications.templates import format_position_opened
    from notifications.telegram import notifier
    from risk.correlation_checker import CorrelationChecker
    from risk.drawdown_monitor import DrawdownMonitor
    from risk.manager import RiskManager
    from risk.position_sizer import PositionSizer
    from trading.approval_service import EntryApprovalService
    from trading.connectors.factory import get_connector
    from trading.connectors.types import OrderSide
    from trading.contracts import ResultState, context_from_dict
    from trading.order_executor import OrderExecutor
    from trading.paper_trader import is_durable_paper_position
    from paper.durable_effects import DurablePaperEffects
    from paper.protection_recovery import _recover_one
    from paper.safe_break import _confirmed_protection
    from trading.connectors.memory_transport import MemoryTransport
    from trading.connectors.simulated import SimulatedConnector

    svc = EntryApprovalService()
    approved = await svc.pop_approved_entries()
    if not approved:
        await svc.close()
        return

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async def keep_claims_alive() -> None:
        while True:
            await asyncio.sleep(60)
            for entry in approved:
                token = entry.get("_claim_token")
                if token:
                    try:
                        renewed = await svc.renew_claim(entry["id"], token)
                    except Exception:
                        logger.exception(
                            "[didi] Falha ao renovar claim da aprovação %s",
                            entry["id"],
                        )
                        continue
                    if not renewed:
                        logger.error(
                            "[didi] Claim da aprovacao %s foi perdido; "
                            "resultado será recuperado pelo intent durável",
                            entry["id"],
                        )

    heartbeat = asyncio.create_task(keep_claims_alive())
    try:
        for item in approved:
            approval_id = item.get("id")
            if not approval_id:
                continue

            intent_id = item.get("intent_id")
            claim_token = item.get("_claim_token")
            recovered = bool(item.get("_recovered_executing"))
            trade = None
            try:
                if not intent_id or not claim_token:
                    raise RuntimeError("Approval claim lacks its durable intent or fencing token")
                market = str(item["market"])
                symbol = str(item["symbol"])
                mode = str(item.get("mode") or "unknown")
                side = OrderSide.BUY if item["side"] == "buy" else OrderSide.SELL
                qty = Decimal(str(item["quantity"]))
                entry_price = Decimal(str(item["entry_price"]))
                stop_loss = Decimal(str(item["stop_loss"]))
                take_profit = Decimal(str(item["take_profit"]))
                expires_at = datetime.fromisoformat(str(item["expires_at"]))

                async with Session() as db:
                    config = (
                        await db.execute(
                            select(BotConfig).where(BotConfig.market == market)
                        )
                    ).scalar_one_or_none()
                    intent = (
                        await db.execute(
                            select(ExecutionIntent).where(
                                ExecutionIntent.intent_id == intent_id
                            )
                        )
                    ).scalar_one_or_none()
                    if config is None or intent is None:
                        raise RuntimeError("Approval configuration or execution intent is missing")
                    execution_context = context_from_dict(
                        (config.execution_context or {}).get(symbol)
                    )
                    if (
                        execution_context.market != market
                        or execution_context.mode.value != mode
                        or execution_context.instrument.symbol != symbol
                    ):
                        raise ValueError("Approval conflicts with server-side execution binding")
                    intent_status = intent.status
                    intent_position_id = intent.position_id
                    await db.commit()

                if recovered and mode == "paper":
                    async with Session() as db:
                        existing_trade = (
                            await db.execute(
                                select(Trade).where(Trade.intent_id == intent_id)
                            )
                        ).scalar_one_or_none()
                        await db.commit()
                    if (existing_trade is not None and existing_trade.status == "closed"
                            and is_durable_paper_position(existing_trade.position_id)):
                        completed = await svc.mark_entry_executed(approval_id, claim_token)
                        if completed is None:
                            logger.error("[didi] Aprovação %s: fechamento ainda não comprovado", approval_id)
                        else:
                            logger.info("[didi] Aprovação %s reconciliada com posição encerrada: %s",
                                        approval_id, completed["status"])
                        continue
                    if (existing_trade is not None
                            and not is_durable_paper_position(existing_trade.position_id)
                            and not is_durable_paper_position(intent_position_id)):
                        if intent_status != "confirmed":
                            logger.error("[didi] Trade paper legado %s requer reconciliação", intent_id)
                            continue
                        trade = existing_trade
                    elif intent_status == "reserved" and existing_trade is not None:
                        logger.error("[didi] Intent %s reservada já possui Trade; reconciliação exigida", intent_id)
                        continue
                    elif intent_status == "reserved" and expires_at <= datetime.now(timezone.utc):
                        await svc.expire_unstarted_claim(approval_id, claim_token)
                        continue
                    elif intent_status != "reserved":
                        async with Session() as db:
                            authority = (await db.execute(select(ProtectionAuthority).where(
                                ProtectionAuthority.intent_id == intent_id
                            ))).scalar_one_or_none()
                            if authority is None:
                                logger.error("[didi] Intent %s sem autoridade durável; reserva preservada", intent_id)
                                continue
                            execution_context = context_from_dict(authority.execution_context)
                            await db.commit()
                        if (execution_context.market != market or execution_context.mode.value != mode
                                or execution_context.instrument.symbol != symbol):
                            raise ValueError("Persisted authority conflicts with approval")
                        effects = DurablePaperEffects(Session, execution_context)
                        connector = SimulatedConnector(
                            execution_context, transport=MemoryTransport(execution_context, durable_effects=effects)
                        )
                        observed = await connector.query_order(intent_id=intent_id)
                        if observed.state not in {ResultState.CONFIRMED, ResultState.PARTIAL}:
                            logger.error("[didi] Intent %s sem recibo de entrada conclusivo; reserva preservada", intent_id)
                            continue
                        confirmed_protection = False
                        if existing_trade is not None and intent_status == "confirmed":
                            async with Session() as db:
                                locked_intent, locked_trade, authority, position = await effects._locked_protection_state(
                                    db, "paper_" + intent_id,
                                )
                                confirmed_protection = await _confirmed_protection(
                                    db, effects, locked_intent, locked_trade, authority, position,
                                )
                        if not confirmed_protection:
                            async with Session() as db:
                                await _recover_one(db, connector, intent_id)
                        async with Session() as db:
                            trade = (await db.execute(select(Trade).where(
                                Trade.intent_id == intent_id
                            ))).scalar_one_or_none()
                            await db.commit()
                        if trade is None:
                            logger.error("[didi] Intent %s sem Trade recuperado; reserva preservada", intent_id)
                            continue

                elif recovered:
                    connector = get_connector(execution_context, mode=mode)
                    try:
                        await connector.connect()
                        observed = await connector.query_order(intent_id=intent_id)
                    finally:
                        await _close_connector(connector)

                    not_found = (
                        observed.state == ResultState.UNKNOWN
                        and observed.order_id is None
                        and observed.position_id is None
                        and observed.filled == 0
                        and observed.reason == "Intent not found"
                    )
                    if not_found:
                        if expires_at <= datetime.now(timezone.utc):
                            await svc.reset_after_confirmed_not_found(
                                approval_id, claim_token
                            )
                            continue
                        if not await svc.reset_after_confirmed_not_found(
                            approval_id, claim_token
                        ):
                            await svc.mark_entry_failed(
                                approval_id,
                                "Intent result remains unresolved after transport lookup",
                                claim_token,
                            )
                            continue
                    elif observed.state in {ResultState.UNKNOWN, ResultState.UNAVAILABLE}:
                        await svc.mark_entry_failed(
                            approval_id,
                            observed.reason or "Execution result is still unknown",
                            claim_token,
                        )
                        continue
                    else:
                        persisted_status = await svc.record_observed_execution(
                            approval_id,
                            claim_token,
                            state=observed.state.value,
                            order_id=observed.order_id,
                            position_id=observed.position_id,
                            filled_quantity=observed.filled,
                        )
                        if persisted_status is None:
                            continue
                        if persisted_status == "failed":
                            await svc.mark_entry_failed(
                                approval_id,
                                observed.reason or "Transport rejected the entry",
                                claim_token,
                            )
                            continue
                        connector = get_connector(execution_context, mode=mode)
                        try:
                            await connector.connect()
                            async with Session() as db:
                                trade = await OrderExecutor(connector, db).open_position(
                                    symbol=symbol,
                                    side=side,
                                    quantity=qty,
                                    entry_price=entry_price,
                                    stop_loss=stop_loss,
                                    take_profit=take_profit,
                                    model_version="didi-manual",
                                    ml_confidence=None,
                                    market=market,
                                    intent_id=intent_id,
                                )
                        finally:
                            await _close_connector(connector)

                if trade is None:
                    if not recovered and expires_at <= datetime.now(timezone.utc):
                        await svc.expire_unstarted_claim(approval_id, claim_token)
                        continue

                    async with Session() as db:
                        config = await _require_approved_entry(db, market, mode, symbol)
                        execution_context = context_from_dict(
                            (config.execution_context or {}).get(symbol)
                        )
                        existing = await db.execute(
                            select(Trade).where(
                                Trade.market == market,
                                Trade.symbol == symbol,
                                Trade.status == "open",
                            )
                        )
                        if existing.scalar_one_or_none() is not None:
                            await db.commit()
                            await svc.mark_entry_failed(
                                approval_id, "ja_existe_posicao_aberta", claim_token
                            )
                            continue
                        await db.commit()

                    # Preço congelado na aprovação não é preço executável.
                    # Reprecifica os níveis e revalida o risco com o mercado atual.
                    current_price = await _current_price_for_approval(
                        symbol, market, mode, execution_context, settings
                    )
                    entry_price, stop_loss, take_profit = _reprice_approval_levels(
                        item["side"],
                        entry_price,
                        stop_loss,
                        take_profit,
                        current_price,
                        float(getattr(settings, "didi_max_entry_slippage_pct", 0.25) or 0.25),
                        execution_context.instrument.tick_size,
                    )

                    async with Session() as db:
                        risk = RiskManager(
                            db=db,
                            position_sizer=PositionSizer(),
                            drawdown_monitor=DrawdownMonitor(db),
                            correlation_checker=CorrelationChecker(db),
                        )
                        risk_result = await risk.can_open_position(
                            market=market,
                            symbol=symbol,
                            entry_price=entry_price,
                            stop_loss_price=stop_loss,
                            side=side.value,
                            intent_id=intent_id,
                            requested_quantity=qty,
                        )
                        if not risk_result.approved or risk_result.position_size is None:
                            raise ValueError(f"risco_reprovado: {risk_result.reason}")
                        if risk_result.intent_id is None:
                            raise RuntimeError("Risk approval returned without a reservation id")
                        if risk_result.intent_id != intent_id:
                            raise RuntimeError("Risk approval changed the durable intent id")
                        max_quantity = risk_result.position_size.quantity
                        if qty <= 0 or qty > max_quantity:
                            raise ValueError(
                                f"quantidade_acima_do_risco: solicitada={qty}, "
                                f"maxima={max_quantity}"
                            )
                        await db.commit()

                    if mode == "paper":
                        execution_context.instrument.validate(qty, current_price)
                        effects = DurablePaperEffects(Session, execution_context)
                        transport = MemoryTransport(execution_context, durable_effects=effects)
                        transport.prices[symbol] = current_price
                        connector = SimulatedConnector(execution_context, transport=transport)
                        async with Session() as db:
                            trade = await OrderExecutor(connector, db).open_position(
                                symbol=symbol, side=side, quantity=qty,
                                entry_price=entry_price, stop_loss=stop_loss,
                                take_profit=take_profit, model_version="didi-manual",
                                ml_confidence=None, market=market, intent_id=intent_id,
                            )

                if trade is None and mode != "paper":
                    connector = get_connector(execution_context, mode=mode)
                    try:
                        await connector.connect()
                        async with Session() as db:
                            trade = await OrderExecutor(connector, db).open_position(
                                symbol=symbol,
                                side=side,
                                quantity=qty,
                                entry_price=entry_price,
                                stop_loss=stop_loss,
                                take_profit=take_profit,
                                model_version="didi-manual",
                                ml_confidence=None,
                                market=market,
                                intent_id=intent_id,
                            )
                    finally:
                        await _close_connector(connector)

                # O resultado durável vem antes da notificação. Um claim perdido
                # deixa a aprovação recuperável pelo próximo worker.
                completed = await svc.mark_entry_executed(approval_id, claim_token)
                if completed is None:
                    logger.critical(
                        "[didi] Aprovação %s pendente: claim inválido ou execução/proteção "
                        "não comprovada; reserva preservada",
                        approval_id,
                    )
                    continue
                try:
                    if trade.status == "open":
                        await notifier.send_message(format_position_opened(trade))
                except Exception as notify_exc:
                    logger.error(
                        "[didi] Posição aberta, mas notificação falhou (%s): %s",
                        approval_id,
                        notify_exc,
                    )
            except Exception as exc:
                intent_status = None
                if intent_id:
                    async with Session() as db:
                        from db.models import ExecutionIntent

                        result = await db.execute(
                            select(ExecutionIntent).where(
                                ExecutionIntent.intent_id == intent_id
                            )
                        )
                        intent = result.scalar_one_or_none()
                        intent_status = intent.status if intent is not None else None
                        await db.commit()

                if intent_id is None or intent_status in {"reserved", "failed"}:
                    await svc.mark_entry_failed(
                        approval_id, str(exc), claim_token
                    )
                    logger.error(
                        "[didi] Falha ao executar aprovacao %s: %s",
                        approval_id,
                        exc,
                        exc_info=True,
                    )
                elif intent_status == "confirmed":
                    async with Session() as db:
                        trade = (
                            await db.execute(
                                select(Trade).where(Trade.intent_id == intent_id)
                            )
                        ).scalar_one_or_none()
                        await db.commit()
                    if trade is not None:
                        completed = await svc.mark_entry_executed(
                            approval_id, claim_token
                        )
                        if completed is not None:
                            try:
                                if trade.status == "open":
                                    await notifier.send_message(format_position_opened(trade))
                            except Exception as notify_exc:
                                logger.error(
                                    "[didi] Posição aberta, mas notificação falhou (%s): %s",
                                    approval_id,
                                    notify_exc,
                                )
                        else:
                            logger.critical(
                                "[didi] Aprovação %s permanece pendente após erro; "
                                "claim ou resultado durável não comprovado: %s",
                                approval_id, exc,
                            )
                else:
                    await svc.mark_entry_failed(
                        approval_id, str(exc), claim_token
                    )
                    logger.critical(
                        "[didi] Resultado incerto para aprovacao %s (intent=%s, status=%s); "
                        "reserva preservada para reconciliacao: %s",
                        approval_id,
                        intent_id,
                        intent_status,
                        exc,
                        exc_info=True,
                    )
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await svc.close()
        await engine.dispose()
        await engine.dispose()


async def _execute_durable_paper_stop(Session, trade_id, snapshot, decision) -> None:
    from datetime import datetime
    from paper.durable_effects import DurablePaperEffects
    from trading.connectors.memory_transport import MemoryTransport
    from trading.connectors.simulated import SimulatedConnector
    from trading.contracts import context_from_dict
    from trading.order_executor import OrderExecutionError, OrderExecutor
    from trading.safety import ExposureBlocked
    from db.models import Trade

    identity = dict(snapshot)
    identity.pop("paper_costs", None)
    try:
        context = context_from_dict(identity)
        connector = SimulatedConnector(
            context, transport=MemoryTransport(context, durable_effects=DurablePaperEffects(Session, context))
        )
        async with Session() as db:
            trade = await db.get(Trade, trade_id)
            if trade is None:
                raise OrderExecutionError("Monitor trade not found")
            await OrderExecutor(connector, db).close_position(
                trade, reason=decision["reason"], execution_price=Decimal(decision["execution_price"]),
                candle=datetime.fromisoformat(decision["candle"]),
                timeframe=decision["timeframe"],
                previous_cursor=datetime.fromisoformat(decision["cursor"]) if decision["cursor"] else None,
                previous_timeframe=decision["previous_timeframe"],
            )
    except (OrderExecutionError, ExposureBlocked, ValueError, KeyError, TypeError) as exc:
        logger.error("[paper] Trade %s: fechamento durável pendente: %s", trade_id, exc)


async def _stage_durable_paper_close(
    db, effects, trade, *, reason, price, candle, timeframe, decision_context=None,
):
    from uuid import uuid4

    owner = uuid4().hex
    operation_id, fence, payload = await effects.prepare_manual_close(
        trade.position_id, trade_id=trade.id, quantity=trade.quantity, reason=reason,
        execution_price=price, owner=owner, candle=candle, timeframe=timeframe,
        previous_cursor=trade.paper_monitor_candle_at,
        previous_timeframe=trade.paper_monitor_timeframe, session=db, decision_context=decision_context,
    )
    await effects.release_initial_protection_claim(db, trade.position_id, operation_id, fence, owner)
    await db.commit()
    return payload


async def _resume_durable_partial_stop(Session, trade_id) -> bool:
    from paper.partial_stop import resume_partial_stop
    try:
        if await resume_partial_stop(Session, trade_id):
            return True
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error("[paper] Trade %s: cancelamento/fechamento parcial pendente: %s", trade_id, exc)
        return True
    return False


async def _resume_durable_paper_stop(Session, trade_id) -> bool:
    if await _resume_durable_partial_stop(Session, trade_id):
        return True
    from sqlalchemy import or_, select
    from db.models import Trade
    from paper.durable_effects import PaperProtectionAttempt
    from trading.paper_trader import is_durable_paper_position

    async with Session() as db:
        trade = (await db.execute(select(Trade).where(
            Trade.id == trade_id
        ).with_for_update())).scalar_one_or_none()
        if (trade is None or trade.status != "open" or trade.mode != "paper"
                or not trade.intent_id or not is_durable_paper_position(trade.position_id)):
            return False
        pending = (await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.position_id == trade.position_id,
            or_(PaperProtectionAttempt.operation_id == f"manual-close:{trade.id}",
                PaperProtectionAttempt.operation_id.like(f"manual-close:{trade.id}:%")),
        ))).scalars().all()
        if not pending:
            return False
        if len(pending) != 1 or pending[0].payload.get("candle") is None:
            logger.error("[paper] Trade %s: fechamento anterior requer reconciliação", trade_id)
            return True
        decision, snapshot = dict(pending[0].payload), dict(trade.execution_context or {})
        await db.rollback()
    await _execute_durable_paper_stop(Session, trade_id, snapshot, decision)
    return True


async def _resume_durable_safe_break(Session, trade_id, *, evidence_provider=None, evidence_clock=None):
    from db.models import Trade
    from notifications.templates import format_safe_break_armed
    from notifications.telegram import notifier
    from paper.safe_break import resume_safe_break

    try:
        resumed = await resume_safe_break(
            Session, trade_id, evidence_provider=evidence_provider, evidence_clock=evidence_clock,
        )
    except Exception as exc:
        logger.exception("[paper] Trade %s: safe break permanece pendente", trade_id)
        try:
            await notifier.send_emergency_alert(
                f"[paper] Trade {trade_id}: safe break pendente; proteção e reserva preservadas: {exc}"
            )
        except Exception:
            logger.exception("[paper] Falha ao enviar alerta de safe break para trade %s", trade_id)
        return True, False
    if resumed:
        try:
            async with Session() as db:
                trade = await db.get(Trade, trade_id)
                if trade is not None:
                    await notifier.send_message(format_safe_break_armed(trade))
        except Exception:
            logger.exception("[paper] Safe break confirmado, mas notificação falhou para trade %s", trade_id)
    return resumed, True


async def _monitor_paper_candles(
    Session, trade_id, df, settings, redis_client, *, evidence_provider=None, evidence_clock=None,
) -> None:
    """Evaluate closed candles in order, committing each decision under a row lock.

    An overlapping candle's open and extremes may predate entry. Only its close
    is observable after entry, without an opening-gap adjustment.
    """
    import pandas as pd
    from sqlalchemy import or_, select

    from db.models import Trade, TradeEvent
    from notifications.templates import format_exit_signal_alert, format_safe_break_armed
    from notifications.telegram import notifier
    from trading.didi_strategy import evaluate_exit, evaluate_safe_break, evaluate_paper_stop_target
    from trading.market_feed import normalize_timeframe, timeframe_seconds
    from trading.paper_trader import PaperTrader, is_durable_paper_position
    from trading.safety import ExposureBlocked
    from paper.durable_effects import PaperProtectionAttempt
    from paper.safe_break import _confirmed_protection, _effects, executable_safe_break_stop, stage_safe_break

    timeframe = normalize_timeframe(getattr(settings, "didi_timeframe", "1h"))
    interval = pd.Timedelta(seconds=timeframe_seconds(timeframe))
    timestamps = pd.to_datetime(df["timestamp"], utc=True)

    while True:
        if await _resume_durable_partial_stop(Session, trade_id):
            return
        _, succeeded = await _resume_durable_safe_break(
            Session, trade_id, evidence_provider=evidence_provider, evidence_clock=evidence_clock,
        )
        if not succeeded:
            return
        safe_notice = None
        exit_notice = None
        closed = False
        async with Session() as db:
            identity = (await db.execute(select(
                Trade.position_id, Trade.intent_id, Trade.execution_context,
            ).where(Trade.id == trade_id))).one_or_none()
            if identity is None:
                return
            durable = is_durable_paper_position(identity.position_id) and bool(identity.intent_id)
            if durable:
                try:
                    effects, _ = _effects(Session, dict(identity.execution_context or {}))
                    intent, trade, authority, position = await effects._locked_protection_state(
                        db, identity.position_id,
                    )
                except (ExposureBlocked, ValueError, KeyError, TypeError) as exc:
                    logger.error("[paper] Trade %s: identidade durável inválida: %s", trade_id, exc)
                    return
            else:
                trade = (await db.execute(
                    select(Trade).where(Trade.id == trade_id).with_for_update()
                )).scalar_one_or_none()
            if trade is None or trade.id != trade_id or trade.status != "open" or trade.mode != "paper":
                return
            if durable:
                pending = (await db.execute(select(PaperProtectionAttempt).where(
                    PaperProtectionAttempt.position_id == trade.position_id,
                    or_(PaperProtectionAttempt.operation_id == f"manual-close:{trade.id}",
                        PaperProtectionAttempt.operation_id.like(f"manual-close:{trade.id}:%")),
                ))).scalars().all()
                if pending:
                    if len(pending) != 1 or pending[0].payload.get("candle") is None:
                        logger.error("[paper] Trade %s: fechamento anterior requer reconciliação", trade_id)
                        return
                    decision, snapshot = dict(pending[0].payload), dict(trade.execution_context or {})
                    await db.rollback()
                    await _execute_durable_paper_stop(Session, trade_id, snapshot, decision)
                    return
                if not await _confirmed_protection(db, effects, intent, trade, authority, position):
                    logger.critical("[paper] Trade %s: proteção atual não confirmada; cursor bloqueado", trade_id)
                    await db.rollback()
                    await notifier.send_emergency_alert(
                        f"[paper] Trade {trade_id}: proteção atual não confirmada; reserva preservada."
                    )
                    return

            if durable:
                from paper.entry_time import proven_entry_time
                from paper.durable_effects import PaperEffectOperation
                try:
                    entry = await db.get(PaperEffectOperation, (
                        effects._account_key, trade.position_id, "entry:" + trade.intent_id,
                    ))
                    if entry is None:
                        raise ValueError("Recibo de entrada ausente")
                    observed = await effects._entry_result(db, trade.intent_id, trade.position_id, entry.result)
                    if trade.open_at != proven_entry_time(observed):
                        raise ValueError("open_at diverge dos fills duráveis; reconciliação temporal necessária")
                except (ValueError, KeyError, TypeError) as exc:
                    await db.rollback()
                    logger.error("[paper] Trade %s: histórico temporal bloqueado: %s", trade_id, exc)
                    await notifier.send_emergency_alert(
                        f"[paper] Trade {trade_id}: histórico temporal bloqueado; "
                        "inspecione com python -m paper.entry_time --intent <intent_id>."
                    )
                    return

            opened = pd.Timestamp(trade.open_at)
            opened = opened.tz_localize("UTC") if opened.tzinfo is None else opened.tz_convert("UTC")
            cursor = trade.paper_monitor_candle_at
            if cursor is not None:
                if trade.paper_monitor_timeframe != timeframe:
                    logger.error("[paper] Trade %s: timeframe do cursor mudou; decisão bloqueada", trade_id)
                    return
                cursor_ts = pd.Timestamp(cursor)
                cursor_ts = cursor_ts.tz_localize("UTC") if cursor_ts.tzinfo is None else cursor_ts.tz_convert("UTC")
                candidates = timestamps[timestamps > cursor_ts]
                expected = cursor_ts + interval
            else:
                previous_safe_break = (await db.execute(
                    select(TradeEvent.id).where(
                        TradeEvent.trade_id == trade_id,
                        TradeEvent.event_type == "SAFE_BREAK_ARMED",
                    ).limit(1)
                )).scalar_one_or_none()
                if previous_safe_break is not None:
                    logger.error(
                        "[paper] Trade %s: safe break anterior sem cursor; "
                        "reconciliação manual necessária antes de retomar",
                        trade_id,
                    )
                    return
                candidates = timestamps[timestamps + interval > opened]
                expected = None

            if candidates.empty:
                return
            idx = candidates.index[0]
            timestamp = timestamps.loc[idx]
            if (expected is not None and timestamp != expected) or (
                expected is None and timestamp > opened
            ):
                logger.error(
                    "[paper] Trade %s: lacuna de candles antes de %s; cursor não avançado",
                    trade_id, timestamp,
                )
                return

            bar = df.loc[idx]
            overlap = timestamp < opened
            try:
                close = Decimal(str(bar["close"]))
                bar_open = close if overlap else Decimal(str(bar["open"]))
                high = close if overlap else Decimal(str(bar["high"]))
                low = close if overlap else Decimal(str(bar["low"]))
                if any(not value.is_finite() or value <= 0 for value in (bar_open, high, low, close)):
                    raise ValueError("preço inválido")
                if not overlap and (low > min(bar_open, close) or high < max(bar_open, close)):
                    raise ValueError("OHLC inconsistente")
            except (KeyError, ValueError, ArithmeticError) as exc:
                logger.error("[paper] Trade %s: candle inválido em %s: %s", trade_id, timestamp, exc)
                return

            is_buy = trade.side == "buy"
            stop_target = evaluate_paper_stop_target(
                "buy" if is_buy else "sell", trade.stop_loss, trade.take_profit,
                bar_open, high, low,
            )
            if stop_target is not None:
                reason, exit_price = stop_target
                if is_durable_paper_position(trade.position_id):
                    if not durable:
                        logger.critical("[paper] Trade %s: identidade durável não resolvida", trade_id)
                        return
                    snapshot = dict(trade.execution_context or {})
                    if intent.order_terminal is not True:
                        from paper.partial_stop import stage_partial_stop, resume_partial_stop
                        try:
                            await stage_partial_stop(
                                db, effects, intent, trade, authority, position,
                                reason=reason, price=exit_price,
                                candle=timestamp.to_pydatetime(), timeframe=timeframe,
                            )
                            await db.commit()
                            await resume_partial_stop(Session, trade_id)
                        except (ValueError, RuntimeError, OSError) as exc:
                            await db.rollback()
                            logger.error("[paper] Trade %s: stop parcial pendente: %s", trade_id, exc)
                        return
                    try:
                        decision = await _stage_durable_paper_close(
                            db, effects, trade, reason=reason, price=exit_price,
                            candle=timestamp.to_pydatetime(), timeframe=timeframe,
                        )
                    except (ValueError, ExposureBlocked) as exc:
                        await db.rollback()
                        logger.error("[paper] Trade %s: decisão de fechamento bloqueada: %s", trade_id, exc)
                        return
                    await _execute_durable_paper_stop(Session, trade_id, snapshot, decision)
                    return
                trade.paper_monitor_candle_at = timestamp.to_pydatetime()
                trade.paper_monitor_timeframe = timeframe
                await PaperTrader(db, market=trade.market)._close_position(trade, exit_price, reason)
                logger.info("[paper] %s %s fechado por %s no candle %s", trade.symbol, trade.side, reason, timestamp)
                closed = True
            else:
                # The overlapping candle's indicators depend on pre-entry extremes.
                exit_eval = None if overlap else evaluate_exit(
                    df.loc[:idx], "buy" if is_buy else "sell"
                )
                if (exit_eval is not None and exit_eval.should_exit
                        and settings.didi_exit_auto_close
                        and is_durable_paper_position(trade.position_id)):
                    if not durable:
                        logger.critical("[paper] Trade %s: identidade durável não resolvida", trade_id)
                        return
                    snapshot = dict(trade.execution_context or {})
                    try:
                        decision = await _stage_durable_paper_close(
                            db, effects, trade, reason="DIDI_EXIT", price=close,
                            candle=timestamp.to_pydatetime(), timeframe=timeframe,
                            decision_context={
                                "candle": {
                                    "open": str(bar_open), "high": str(high), "low": str(low),
                                    "close": str(close),
                                    "volume": str(bar["volume"]) if "volume" in bar else None,
                                },
                                "criteria": dict(exit_eval.criteria),
                                "details": dict(exit_eval.details),
                            },
                        )
                    except (ValueError, ExposureBlocked) as exc:
                        await db.rollback()
                        logger.error("[paper] Trade %s: saída estratégica bloqueada: %s", trade_id, exc)
                        return
                    await _execute_durable_paper_stop(Session, trade_id, snapshot, decision)
                    return
                safe = evaluate_safe_break(
                    side="buy" if is_buy else "sell",
                    entry_price=trade.entry_price,
                    stop_loss=trade.stop_loss,
                    current_price=close,
                    trigger_rr=Decimal(str(settings.didi_safe_break_rr)),
                )
                if safe.should_move:
                    if is_durable_paper_position(trade.position_id):
                        if not durable:
                            logger.critical("[paper] Trade %s: identidade durável não resolvida", trade_id)
                            return
                        snapshot = dict(trade.execution_context or {})
                        try:
                            executable_stop = executable_safe_break_stop(
                                snapshot, trade.side, trade.quantity, safe.new_stop_loss,
                            )
                        except (ExposureBlocked, ValueError, ArithmeticError, KeyError, TypeError) as exc:
                            logger.error("[paper] Trade %s: stop de safe break inválido: %s", trade_id, exc)
                            await db.rollback()
                            await notifier.send_emergency_alert(
                                f"[paper] Trade {trade_id}: stop de safe break inválido: {exc}"
                            )
                            return
                        decision = dict(
                            position_id=trade.position_id, candle_at=timestamp.to_pydatetime(),
                            timeframe=timeframe, interval_seconds=int(interval.total_seconds()),
                            previous_cursor=cursor, previous_timeframe=trade.paper_monitor_timeframe,
                            old_stop=trade.stop_loss, new_stop=executable_stop,
                            take_profit=trade.take_profit, entry_price=trade.entry_price,
                            quantity=trade.quantity, levels_version=authority.version,
                            entry_revision=intent.observation_revision,
                            candle={"open": str(bar_open), "high": str(high), "low": str(low),
                                    "close": str(close), "trigger_rr": str(settings.didi_safe_break_rr)},
                        )
                        await db.rollback()
                        try:
                            staged = await stage_safe_break(Session, trade_id, snapshot, **decision)
                        except Exception as exc:
                            logger.exception("[paper] Trade %s: decisão de safe break bloqueada", trade_id)
                            await notifier.send_emergency_alert(
                                f"[paper] Trade {trade_id}: decisão de safe break bloqueada: {exc}"
                            )
                            return
                        if not staged:
                            return
                        continue
                    trade.stop_loss = safe.new_stop_loss
                    db.add(TradeEvent(
                        trade=trade,
                        event_type="SAFE_BREAK_ARMED",
                        payload={"new_stop_loss": str(safe.new_stop_loss)},
                    ))
                    safe_notice = trade

                trade.paper_monitor_candle_at = timestamp.to_pydatetime()
                trade.paper_monitor_timeframe = timeframe
                if exit_eval is not None and exit_eval.should_exit:
                    exit_notice = (trade, exit_eval.criteria, exit_eval.details)
                    if settings.didi_exit_auto_close:
                        await PaperTrader(db, market=trade.market)._close_position(trade, close, "DIDI_EXIT")
                        closed = True
                if not closed:
                    await db.commit()

        if safe_notice is not None:
            await notifier.send_message(format_safe_break_armed(safe_notice))
        if exit_notice is not None:
            dedupe_key = f"alert:exit:{trade_id}"
            if not await redis_client.get(dedupe_key):
                await redis_client.setex(dedupe_key, 900, "1")
                await notifier.send_message(format_exit_signal_alert(*exit_notice))
        if closed:
            return


async def _alert_legacy_paper_cursors(Session, redis_client, notifier) -> None:
    from redis.exceptions import RedisError
    from trading.paper_cursor_reconciliation import list_blocked_paper_cursors

    for blocked in await list_blocked_paper_cursors(Session):
        message = (
            f"[paper] Trade {blocked.trade_id} bloqueado: safe break legado sem cursor. "
            "Execute python -m trading.paper_cursor_reconciliation list e registre "
            "uma decisão com o comando record; o monitor permanece bloqueado."
        )
        logger.error(message)
        key = f"alert:paper-cursor:{blocked.trade_id}:{blocked.safe_break_event_id}"
        try:
            already_sent = await redis_client.get(key)
        except RedisError:
            logger.exception("[paper] Cache de alerta indisponível para trade %s", blocked.trade_id)
            already_sent = None
        if not already_sent:
            if await notifier.send_message(message):
                try:
                    await redis_client.setex(key, 3600, "1")
                except RedisError:
                    logger.exception("[paper] Falha ao registrar cache de alerta %s", blocked.trade_id)
            else:
                logger.error("[paper] Alerta não entregue para trade %s", blocked.trade_id)


async def _monitor_open_positions_async() -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import redis.asyncio as aioredis

    from config import settings
    from db.models import Trade, TradeEvent
    from notifications.templates import format_exit_signal_alert, format_safe_break_armed
    from notifications.telegram import notifier
    from trading.connectors.factory import get_connector
    from trading.contracts import ResultState
    from trading.didi_strategy import evaluate_exit, evaluate_safe_break
    from trading.paper_trader import is_durable_paper_position

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    try:
        await _alert_legacy_paper_cursors(Session, redis_client, notifier)
        async with Session() as db:
            result = await db.execute(select(Trade).where(Trade.status == "open"))
            open_trades = result.scalars().all()
            await db.commit()

            for trade in open_trades:
                if (trade.mode == "paper" and is_durable_paper_position(trade.position_id)
                        and await _resume_durable_paper_stop(Session, trade.id)):
                    continue
                if trade.mode == "paper" and is_durable_paper_position(trade.position_id):
                    resumed, succeeded = await _resume_durable_safe_break(Session, trade.id)
                    if resumed or not succeeded:
                        continue
                # Monitoramento de stops também decide apenas com candles
                # fechados e frescos; dado indisponível bloqueia a decisão.
                df = _load_fresh_ohlcv(
                    trade.symbol, trade.market, settings,
                    limit=None if trade.mode == "paper" else 300,
                )
                if df is None or df.empty:
                    continue

                if trade.mode == "paper":
                    await _monitor_paper_candles(Session, trade.id, df, settings, redis_client)
                    continue
                if len(df) < 80:
                    continue

                current_price = Decimal(str(df["close"].iloc[-1]))
                safe_eval = evaluate_safe_break(
                    side="buy" if trade.side == "buy" else "sell",
                    entry_price=Decimal(str(trade.entry_price)),
                    stop_loss=Decimal(str(trade.stop_loss)),
                    current_price=current_price,
                    trigger_rr=Decimal(str(settings.didi_safe_break_rr)),
                )
                if safe_eval.should_move:
                    # No modo real, o stop só vale se estiver no broker.
                    if trade.mode != "paper":
                        try:
                            from trading.contracts import context_from_dict

                            snapshot = trade.execution_context or {}
                            from trading.contracts import ExecutionContext

                            allowed = set(ExecutionContext.__dataclass_fields__)
                            context = context_from_dict(
                                {k: v for k, v in snapshot.items() if k in allowed}
                            )
                            connector = get_connector(context, mode=trade.mode)
                            await connector.connect()
                            try:
                                protection = await connector.modify_position_protection(
                                    trade.position_id,
                                    stop_loss=safe_eval.new_stop_loss,
                                    take_profit=trade.take_profit,
                                    intent_id=__import__("uuid").uuid4().hex,
                                )
                            finally:
                                await _close_connector(connector)
                            if protection.state != ResultState.CONFIRMED:
                                raise RuntimeError(
                                    f"proteção não confirmada: {protection.state.value}"
                                )
                        except Exception as exc:
                            logger.critical(
                                "[safety] Safe break NÃO aplicado no broker para trade %s: %s",
                                trade.id,
                                exc,
                            )
                            await notifier.send_emergency_alert(
                                f"Safe break não aplicado no broker para trade {trade.id} "
                                f"({trade.symbol}): {exc}"
                            )
                            continue

                    trade.stop_loss = safe_eval.new_stop_loss
                    db.add(
                        TradeEvent(
                            trade=trade,
                            event_type="SAFE_BREAK_ARMED",
                            payload={"new_stop_loss": str(safe_eval.new_stop_loss)},
                        )
                    )
                    await db.commit()
                    await notifier.send_message(format_safe_break_armed(trade))

                exit_eval = evaluate_exit(df, "buy" if trade.side == "buy" else "sell")
                if not exit_eval.should_exit:
                    continue

                dedupe_key = f"alert:exit:{trade.id}"
                already = await redis_client.get(dedupe_key)
                if already:
                    continue
                await redis_client.setex(dedupe_key, 900, "1")
                await notifier.send_message(
                    format_exit_signal_alert(trade, exit_eval.criteria, exit_eval.details)
                )

                if not settings.didi_exit_auto_close:
                    continue

                # Never claim a real close or resolve it to paper while
                # management is unvalidated.
                logger.error("[safety] Real close blocked for trade %s: management adapter unvalidated", trade.id)
    finally:
        await redis_client.aclose()
        await engine.dispose()


@shared_task(name="tasks.trading_tasks.reconcile_open_positions")
def reconcile_open_positions() -> None:
    """Confere no broker se cada posição aberta existe e tem stop aplicado."""
    asyncio.run(_reconcile_open_positions_async())


async def _reconcile_open_positions_async() -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from config import settings
    from db.models import Trade, TradeEvent
    from notifications.telegram import notifier
    from trading.connectors.factory import get_connector
    from trading.contracts import ExecutionContext, ResultState, context_from_dict
    from trading.order_executor import OrderExecutor

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            result = await db.execute(
                select(Trade).where(Trade.status == "open", Trade.mode != "paper")
            )
            trades = result.scalars().all()
            for trade in trades:
                if not trade.position_id:
                    db.add(TradeEvent(
                        trade=trade,
                        event_type="UNRESOLVED_POSITION",
                        payload={"detail": "open trade sem position_id"},
                    ))
                    await db.commit()
                    continue
                try:
                    snapshot = trade.execution_context or {}
                    allowed = set(ExecutionContext.__dataclass_fields__)
                    context = context_from_dict(
                        {k: v for k, v in snapshot.items() if k in allowed}
                    )
                    connector = get_connector(context, mode=trade.mode)
                    await connector.connect()
                    try:
                        positions = await connector.list_positions()
                        broker_position = next(
                            (p for p in positions if str(p.id) == str(trade.position_id)),
                            None,
                        )
                        if broker_position is None:
                            db.add(TradeEvent(
                                trade=trade,
                                event_type="RECONCILIATION_MISSING_POSITION",
                                payload={"position_id": trade.position_id},
                            ))
                            await db.commit()
                            await notifier.send_emergency_alert(
                                f"Posição {trade.position_id} ({trade.symbol}) "
                                "está aberta no sistema e ausente no broker."
                            )
                            continue

                        broker_stop = getattr(broker_position, "stop_loss", None)
                        stop_matches = (
                            broker_stop is not None
                            and Decimal(str(broker_stop)) == Decimal(str(trade.stop_loss))
                        )
                        if stop_matches:
                            continue

                        protection = await connector.modify_position_protection(
                            trade.position_id,
                            stop_loss=trade.stop_loss,
                            take_profit=trade.take_profit,
                            intent_id=__import__("uuid").uuid4().hex,
                        )
                        if protection.state != ResultState.CONFIRMED:
                            executor = OrderExecutor(connector, db)
                            closed = await executor._fail_safe_after_protection_error(
                                trade,
                                trade.position_id,
                                trade.stop_loss,
                                trade.take_profit,
                                "reconciliação não confirmou proteção",
                            )
                            if not closed:
                                await notifier.send_emergency_alert(
                                    f"Trade {trade.id} ({trade.symbol}) segue sem "
                                    "proteção confirmada após reconciliação."
                                )
                            continue

                        db.add(TradeEvent(
                            trade=trade,
                            event_type="PROTECTION_RECONCILED",
                            payload={
                                "position_id": trade.position_id,
                                "stop_loss": str(trade.stop_loss),
                            },
                        ))
                        await db.commit()
                    finally:
                        await _close_connector(connector)
                except Exception as exc:
                    logger.error(
                        "[reconciliation] Falha no trade %s: %s", trade.id, exc, exc_info=True
                    )
                    db.add(TradeEvent(
                        trade=trade,
                        event_type="RECONCILIATION_ERROR",
                        payload={"error": str(exc)[:250]},
                    ))
                    await db.commit()
    finally:
        await engine.dispose()


async def _close_connector(connector) -> None:
    if hasattr(connector, "disconnect"):
        await connector.disconnect()
        return
    if hasattr(connector, "close"):
        await connector.close()


def _load_ohlcv(symbol: str, market: str, settings) -> "pd.DataFrame | None":  # type: ignore[name-defined]
    """Carrega últimos candles no timeframe configurado do parquet local."""
    import pandas as pd

    timeframe = getattr(settings, "didi_timeframe", "1h") or "1h"

    try:
        if market in ("B3", "FOREX"):
            from ml.mt5_data_collector import MT5DataCollector

            collector = MT5DataCollector(mt5_files_dir=settings.mt5_files_dir)
            df = collector.load(symbol, timeframe, market=market)
        else:
            from ml.data_collector import DataCollector

            collector = DataCollector()
            df = collector.load_parquet(symbol, timeframe)

        return df.tail(300).copy()

    except FileNotFoundError:
        return None
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).warning("Erro ao carregar OHLCV %s %s: %s", market, symbol, exc)
        return None


def _load_fresh_ohlcv(
    symbol: str, market: str, settings, *, limit: int | None = 300
) -> "pd.DataFrame | None":  # type: ignore[name-defined]
    """Candles fechados e frescos para decisões; indisponibilidade bloqueia a decisão.

    Reusa trading.market_feed.load_fresh_ohlcv: filtra o candle em formação,
    rejeita dado antigo/inválido e nunca inventa preço de fallback.
    """
    from trading.market_feed import MarketDataUnavailable, load_fresh_ohlcv

    try:
        return load_fresh_ohlcv(symbol, market, settings, limit=limit)
    except MarketDataUnavailable as exc:
        logger.warning(
            "[data] %s %s sem candle fechado/fresco; decisão bloqueada: %s",
            market, symbol, exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "[data] %s %s falhou ao carregar candles; decisão bloqueada: %s",
            market, symbol, exc,
        )
        return None
