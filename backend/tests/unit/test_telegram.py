"""Testes unitários do módulo de notificações Telegram."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notifications.templates import (
    format_daily_report,
    format_drawdown_alert,
    format_oco_failed,
    format_position_closed,
    format_position_opened,
)


def _make_trade(**kw):
    import datetime
    import types

    defaults = dict(
        id=1,
        symbol="BTC/USDT",
        side="buy",
        status="open",
        mode="live",
        entry_price=Decimal("50000"),
        quantity=Decimal("0.01"),
        entry_value=Decimal("500"),
        stop_loss=Decimal("48000"),
        take_profit=Decimal("55000"),
        open_at=datetime.datetime.now(datetime.timezone.utc),
        pnl_net=None,
        pnl_pct=None,
        exit_price=None,
        duration_sec=None,
        close_reason=None,
        model_version="rf_v1",
        ml_confidence=Decimal("0.80"),
    )
    defaults.update(kw)
    trade = types.SimpleNamespace(**defaults)
    return trade


def test_format_position_opened_buy():
    trade = _make_trade()
    msg = format_position_opened(trade)
    assert "POSIÇÃO ABERTA" in msg
    assert "BTC/USDT" in msg
    assert "LONG" in msg
    assert "50.000" in msg or "50000" in msg


def test_format_position_closed_profit():
    trade = _make_trade(
        status="closed",
        exit_price=Decimal("55000"),
        pnl_net=Decimal("48.5"),
        pnl_pct=Decimal("9.7"),
        duration_sec=3600,
        close_reason="TP_HIT",
    )
    msg = format_position_closed(trade)
    assert "FECHADA" in msg
    assert "TP" in msg.upper()
    assert "48" in msg


def test_format_position_closed_loss():
    trade = _make_trade(
        status="closed",
        exit_price=Decimal("48000"),
        pnl_net=Decimal("-21"),
        pnl_pct=Decimal("-4.2"),
        duration_sec=1800,
        close_reason="SL_HIT",
    )
    msg = format_position_closed(trade)
    assert "SL" in msg.upper() or "FECHADA" in msg


def test_format_drawdown_alert_critical():
    msg = format_drawdown_alert(Decimal("-8.5"), "CRITICAL", "CRIPTO")
    assert "DRAWDOWN" in msg
    assert "CRIPTO" in msg
    assert "CRITICAL" in msg


def test_format_oco_failed():
    trade = _make_trade()
    msg = format_oco_failed(trade, "Network timeout")
    assert "OCO FALHOU" in msg
    assert "BTC/USDT" in msg


def test_format_daily_report():
    msg = format_daily_report(
        market="CRIPTO",
        total_trades=10,
        winning=7,
        pnl_day=Decimal("150"),
        pnl_accumulated=Decimal("1200"),
        drawdown_1d=Decimal("-1.2"),
        drawdown_30d=Decimal("-3.5"),
        open_trades=2,
    )
    assert "CRIPTO" in msg
    assert "10" in msg
    assert "70" in msg  # win rate 70%


@pytest.mark.asyncio
async def test_telegram_notifier_send_success():
    from notifications.telegram import TelegramNotifier

    notifier = TelegramNotifier()
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=None)
    notifier._bot = mock_bot

    with patch("notifications.telegram.settings") as mock_settings:
        mock_settings.telegram_bot_token = "token"
        mock_settings.telegram_chat_id = "123456"
        result = await notifier.send_message("teste")

    assert result is True
    mock_bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_notifier_send_skipped_when_no_config():
    from notifications.telegram import TelegramNotifier

    notifier = TelegramNotifier()
    with patch("notifications.telegram.settings") as mock_settings:
        mock_settings.telegram_bot_token = ""
        mock_settings.telegram_chat_id = ""
        result = await notifier.send_message("teste")

    assert result is False
