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
            df = _load_ohlcv(symbol, market, settings)
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
                    )
                    if not risk_result.approved:
                        continue

                    qty = risk_result.position_size.quantity if risk_result.position_size else Decimal("0")
                    if qty <= 0:
                        continue

                    from trading.contracts import context_from_dict

                    bindings = config.execution_context or {}
                    bound = context_from_dict(bindings.get(symbol))
                    contract_size = bound.instrument.contract_size or Decimal("1")

                    payload = await approval_service.create_pending_entry(
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
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from config import settings
    from db.models import Trade
    from notifications.templates import format_position_opened
    from notifications.telegram import notifier
    from risk.correlation_checker import CorrelationChecker
    from risk.drawdown_monitor import DrawdownMonitor
    from risk.manager import RiskManager
    from risk.position_sizer import PositionSizer
    from trading.approval_service import EntryApprovalService
    from trading.connectors.factory import get_connector
    from trading.connectors.types import OrderSide
    from trading.order_executor import OrderExecutor
    from trading.paper_trader import PaperTrader

    svc = EntryApprovalService()
    approved = await svc.pop_approved_entries()
    if not approved:
        await svc.close()
        return

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        for item in approved:
            approval_id = item.get("id")
            if not approval_id:
                continue

            try:
                market = str(item["market"])
                symbol = str(item["symbol"])
                mode = str(item.get("mode") or "unknown")
                side = OrderSide.BUY if item["side"] == "buy" else OrderSide.SELL
                qty = Decimal(str(item["quantity"]))
                entry_price = Decimal(str(item["entry_price"]))
                stop_loss = Decimal(str(item["stop_loss"]))
                take_profit = Decimal(str(item["take_profit"]))

                async with Session() as db:
                    config = await _require_approved_entry(db, market, mode, symbol)
                    from trading.contracts import context_from_dict
                    bindings = config.execution_context or {}
                    execution_context = context_from_dict(bindings.get(symbol))
                    if execution_context.market != market or execution_context.mode.value != mode or execution_context.instrument.symbol != symbol:
                        raise ValueError("Approval conflicts with server-side execution binding")
                    existing = await db.execute(
                        select(Trade).where(
                            Trade.market == market,
                            Trade.symbol == symbol,
                            Trade.status == "open",
                        )
                    )
                    if existing.scalar_one_or_none() is not None:
                        await svc.mark_entry_failed(approval_id, "ja_existe_posicao_aberta")
                        continue

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
                    )
                    if not risk_result.approved or risk_result.position_size is None:
                        raise ValueError(f"risco_reprovado: {risk_result.reason}")
                    max_quantity = risk_result.position_size.quantity
                    if qty <= 0 or qty > max_quantity:
                        raise ValueError(
                            f"quantidade_acima_do_risco: solicitada={qty}, "
                            f"maxima={max_quantity}"
                        )

                    if mode == "paper":
                        trader = PaperTrader(db, market=market, context=execution_context)
                        await trader.startup()
                        try:
                            trade = await trader.open_position(
                                symbol=symbol,
                                side=side,
                                quantity=qty,
                                entry_price=entry_price,
                                stop_loss=stop_loss,
                                take_profit=take_profit,
                                model_version="didi-manual",
                                ml_confidence=None,
                            )
                        finally:
                            await trader.shutdown()
                    else:
                        connector = get_connector(execution_context, mode=mode)
                        await connector.connect()
                        try:
                            executor = OrderExecutor(connector, db)
                            trade = await executor.open_position(
                                symbol=symbol,
                                side=side,
                                quantity=qty,
                                entry_price=entry_price,
                                stop_loss=stop_loss,
                                take_profit=take_profit,
                                model_version="didi-manual",
                                ml_confidence=None,
                                market=market,
                            )
                        finally:
                            await _close_connector(connector)

                # A posição já existe: finaliza o estado antes de notificar para
                # que uma falha de Telegram não marque a entrada como falha.
                await svc.mark_entry_executed(approval_id)
                try:
                    await notifier.send_message(format_position_opened(trade))
                except Exception as notify_exc:
                    logger.error(
                        "[didi] Posição aberta, mas notificação falhou (%s): %s",
                        approval_id,
                        notify_exc,
                    )
            except Exception as exc:
                await svc.mark_entry_failed(approval_id, str(exc))
                logger.error("[didi] Falha ao executar aprovacao %s: %s", approval_id, exc, exc_info=True)
    finally:
        await svc.close()
        await engine.dispose()


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
    from trading.order_executor import OrderExecutor
    from trading.paper_trader import PaperTrader

    engine = create_async_engine(settings.database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)

    try:
        async with Session() as db:
            result = await db.execute(select(Trade).where(Trade.status == "open"))
            open_trades = result.scalars().all()

            for trade in open_trades:
                df = _load_ohlcv(trade.symbol, trade.market, settings)
                if df is None or len(df) < 80:
                    continue

                if trade.mode == "paper":
                    bar = df.iloc[-1]
                    is_buy = trade.side == "buy"
                    hit_stop = (
                        float(bar["low"]) <= float(trade.stop_loss)
                        if is_buy else float(bar["high"]) >= float(trade.stop_loss)
                    )
                    hit_target = bool(trade.take_profit) and (
                        float(bar["high"]) >= float(trade.take_profit)
                        if is_buy else float(bar["low"]) <= float(trade.take_profit)
                    )
                    if hit_stop or hit_target:
                        # OHLC não revela qual nível foi tocado primeiro; assume SL.
                        reason = "SL_HIT" if hit_stop else "TP_HIT"
                        bar_open = Decimal(str(bar["open"]))
                        exit_price = trade.stop_loss if hit_stop else trade.take_profit
                        if is_buy and hit_stop and bar_open < exit_price:
                            exit_price = bar_open
                        elif is_buy and hit_target and bar_open > exit_price:
                            exit_price = bar_open
                        elif not is_buy and hit_stop and bar_open > exit_price:
                            exit_price = bar_open
                        elif not is_buy and hit_target and bar_open < exit_price:
                            exit_price = bar_open
                        paper_trader = PaperTrader(db, market=trade.market)
                        await paper_trader._close_position(trade, exit_price, reason)
                        logger.info(
                            "[paper] %s %s fechado por %s no candle %s",
                            trade.symbol, trade.side, reason, bar.get("timestamp"),
                        )
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

                if trade.mode == "paper":
                    trader = PaperTrader(db, market=trade.market)
                    await trader.startup()
                    try:
                        await trader._close_position(trade, current_price, "DIDI_EXIT")
                    finally:
                        await trader.shutdown()
                else:
                    # Keep monitoring other positions. Never claim a real close or
                    # silently resolve it to paper while management is unvalidated.
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
