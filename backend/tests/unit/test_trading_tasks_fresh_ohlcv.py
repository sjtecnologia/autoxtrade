"""Scanner DiDi e monitor decidem apenas com candles fechados e frescos.

Exercita o caminho real (trading.market_feed.load_fresh_ohlcv via
tasks.trading_tasks._load_fresh_ohlcv) substituindo somente a fonte local de
parquet: candle em formação nunca decide, dado antigo bloqueia a decisão com
log claro e dado válido decide sobre o último candle fechado.
"""
import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd

from config import settings


def _fixture(*, periods: int = 80, hours_back: int = 1, close: float = 100.0,
             last_high: float | None = None, forming: dict | None = None) -> pd.DataFrame:
    """Candles OHLC planos; `hours_back` controla a idade do último fechado.

    `last_high` aplica um pico no último candle fechado; `forming` adiciona um
    candle em formação (abre agora, fecha em um timeframe) com high/close dados.
    """
    now = pd.Timestamp.now(tz="UTC")
    opens = pd.date_range(
        end=now.floor("h") - pd.Timedelta(hours=hours_back), periods=periods, freq="h"
    )
    frame = pd.DataFrame({
        "timestamp": list(opens),
        "open": [close] * periods,
        "high": [close] * periods,
        "low": [close] * periods,
        "close": [close] * periods,
        "volume": [1.0] * periods,
    })
    if last_high is not None:
        frame.loc[frame.index[-1], "high"] = last_high
    if forming is not None:
        row = {
            "timestamp": now,  # abertura agora: ainda em formação no 1h
            "open": close,
            "high": forming.get("high", close),
            "low": close,
            "close": forming.get("close", close),
            "volume": 1.0,
        }
        frame = pd.concat([frame, pd.DataFrame([row])], ignore_index=True)
    return frame


def _patch_source(monkeypatch, frame: pd.DataFrame) -> None:
    from trading import market_feed

    monkeypatch.setattr(
        market_feed, "load_ohlcv", lambda symbol, market, settings, *, timeframe=None: frame
    )


def _paper_trade(**overrides) -> SimpleNamespace:
    trade = SimpleNamespace(
        id=1, mode="paper", market="FOREX", symbol="TEST", side="sell",
        entry_price=Decimal("100"), stop_loss=Decimal("150"),
        take_profit=None, position_id="pos-1",
    )
    for key, value in overrides.items():
        setattr(trade, key, value)
    return trade


def _patch_monitor_env(monkeypatch, trades: list, frames: dict):
    import redis.asyncio as redis
    import sqlalchemy.ext.asyncio as sa
    from notifications.telegram import notifier
    from trading import didi_strategy, market_feed
    from trading.paper_trader import PaperTrader

    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = trades
    db.execute.return_value = result
    session = MagicMock()
    session.return_value.__aenter__.return_value = db
    monkeypatch.setattr(sa, "create_async_engine", lambda *a, **kw: AsyncMock())
    monkeypatch.setattr(sa, "async_sessionmaker", lambda *a, **kw: session)
    cache = AsyncMock()
    cache.get.return_value = None
    monkeypatch.setattr(redis, "from_url", lambda *a, **kw: cache)
    monkeypatch.setattr(
        market_feed,
        "load_ohlcv",
        lambda symbol, market, settings, *, timeframe=None: frames[symbol],
    )
    closed = AsyncMock()
    monkeypatch.setattr(PaperTrader, "_close_position", closed)
    monkeypatch.setattr(notifier, "send_message", AsyncMock())
    breaks: list = []
    exits: list = []

    def _safe_break(**kwargs):
        breaks.append(kwargs)
        return SimpleNamespace(should_move=False)

    def _exit(df, side):
        exits.append((df, side))
        return SimpleNamespace(should_exit=False, criteria={}, details={})

    monkeypatch.setattr(didi_strategy, "evaluate_safe_break", _safe_break)
    monkeypatch.setattr(didi_strategy, "evaluate_exit", _exit)
    return closed, breaks, exits


# ---------------------------------------------------------------------------
# Scanner DiDi (_process_didi_manual_entries)
# ---------------------------------------------------------------------------

async def test_scanner_ignora_candle_em_formacao(monkeypatch):
    from tasks import trading_tasks
    from trading import didi_strategy

    # 80 candles fechados + 1 em formação com close fictício de 999.
    _patch_source(monkeypatch, _fixture(forming={"high": 200.0, "close": 999.0}))
    calls: list = []

    def _entry(df, **kwargs):
        calls.append(df)
        return None

    monkeypatch.setattr(didi_strategy, "evaluate_entry", _entry)
    stub = AsyncMock()
    monkeypatch.setattr("trading.approval_service.EntryApprovalService", lambda: stub)
    monkeypatch.setattr(settings, "paper_new_exposure_enabled", True)
    config = SimpleNamespace(
        market="FOREX", enabled_symbols=["TEST"], mode="paper", execution_context={}
    )

    await trading_tasks._process_didi_manual_entries(config, settings, MagicMock())

    assert len(calls) == 1
    assert len(calls[0]) == 80  # o candle em formação não chega à estratégia
    assert 999.0 not in calls[0]["close"].values
    assert calls[0]["timestamp"].iloc[-1] < pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=1)


async def test_scanner_bloqueia_dado_antigo(monkeypatch, caplog):
    from tasks import trading_tasks
    from trading import didi_strategy

    _patch_source(monkeypatch, _fixture(hours_back=72))
    calls: list = []
    monkeypatch.setattr(
        didi_strategy, "evaluate_entry", lambda df, **kwargs: (calls.append(df), None)[1]
    )
    stub = AsyncMock()
    monkeypatch.setattr("trading.approval_service.EntryApprovalService", lambda: stub)
    monkeypatch.setattr(settings, "paper_new_exposure_enabled", True)
    config = SimpleNamespace(
        market="FOREX", enabled_symbols=["TEST"], mode="paper", execution_context={}
    )

    with caplog.at_level(logging.WARNING):
        await trading_tasks._process_didi_manual_entries(config, settings, MagicMock())

    assert calls == []  # decisão bloqueada
    assert "TEST" in caplog.text
    assert "decisão bloqueada" in caplog.text
    assert "Cotação desatualizada" in caplog.text  # motivo explícito, sem fallback


async def test_scanner_avalia_dado_valido(monkeypatch):
    from tasks import trading_tasks
    from trading import didi_strategy

    _patch_source(monkeypatch, _fixture())
    calls: list = []
    monkeypatch.setattr(
        didi_strategy, "evaluate_entry", lambda df, **kwargs: (calls.append(df), None)[1]
    )
    stub = AsyncMock()
    monkeypatch.setattr("trading.approval_service.EntryApprovalService", lambda: stub)
    monkeypatch.setattr(settings, "paper_new_exposure_enabled", True)
    config = SimpleNamespace(
        market="FOREX", enabled_symbols=["TEST"], mode="paper", execution_context={}
    )

    await trading_tasks._process_didi_manual_entries(config, settings, MagicMock())

    assert len(calls) == 1  # dado válido flui para a estratégia
    assert len(calls[0]) == 80
    assert calls[0]["close"].iloc[-1] == 100.0


# ---------------------------------------------------------------------------
# Monitor de stops (_monitor_open_positions_async)
# ---------------------------------------------------------------------------

async def test_monitor_ignora_pico_em_candle_em_formacao(monkeypatch):
    from tasks import trading_tasks

    # Sell com stop em 150: o pico (high 200) existe apenas no candle em
    # formação; o último candle fechado tem high 100 e não pode fechar a posição.
    trade = _paper_trade()
    closed, breaks, exits = _patch_monitor_env(
        monkeypatch, [trade], {"TEST": _fixture(forming={"high": 200.0})}
    )

    await trading_tasks._monitor_open_positions_async()

    closed.assert_not_awaited()  # pico intrabar em formação não decide fechamento
    # Com dado fechado/fresco, o monitor segue gerindo a posição normalmente.
    assert len(breaks) == 1
    assert len(exits) == 1


async def test_monitor_bloqueia_dado_antigo(monkeypatch, caplog):
    from tasks import trading_tasks

    trade = _paper_trade()
    closed, breaks, exits = _patch_monitor_env(
        monkeypatch, [trade], {"TEST": _fixture(hours_back=72)}
    )

    with caplog.at_level(logging.WARNING):
        await trading_tasks._monitor_open_positions_async()

    closed.assert_not_awaited()
    assert breaks == []  # nem safe break nem saída são avaliados sem dado fresco
    assert exits == []
    assert "TEST" in caplog.text
    assert "decisão bloqueada" in caplog.text


async def test_monitor_fecha_stop_no_candle_fechado_valido(monkeypatch):
    from tasks import trading_tasks

    # O pico (high 200) está no ÚLTIMO candle FECHADO: o monitor de stops deve
    # agir sobre extremos do candle fechado e fechar a posição sell no stop 150.
    trade = _paper_trade()
    closed, breaks, exits = _patch_monitor_env(
        monkeypatch, [trade], {"TEST": _fixture(last_high=200.0)}
    )

    await trading_tasks._monitor_open_positions_async()

    closed.assert_awaited_once_with(trade, Decimal("150"), "SL_HIT")
