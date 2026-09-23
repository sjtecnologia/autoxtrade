"""Templates de mensagens Telegram em HTML."""
from decimal import Decimal

from db.models import Trade


def _fmt_price(value) -> str:
    if value is None:
        return "—"
    return f"R$ {Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _fmt_pct(value) -> str:
    if value is None:
        return "—"
    v = Decimal(str(value))
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _duration(seconds: int | None) -> str:
    if not seconds:
        return "—"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m}min"
    return f"{m}min"


def format_position_opened(trade: Trade) -> str:
    side_label = "LONG 📈" if trade.side == "buy" else "SHORT 📉"
    sl_pct = ""
    if trade.stop_loss and trade.entry_price:
        diff = (Decimal(str(trade.stop_loss)) - Decimal(str(trade.entry_price))) / Decimal(str(trade.entry_price)) * 100
        sl_pct = f" ({_fmt_pct(diff)})"
    tp_line = ""
    if trade.take_profit and trade.entry_price:
        diff = (Decimal(str(trade.take_profit)) - Decimal(str(trade.entry_price))) / Decimal(str(trade.entry_price)) * 100
        tp_line = f"\nTake Profit: {_fmt_price(trade.take_profit)} ({_fmt_pct(diff)})"

    conf_line = ""
    if trade.ml_confidence:
        conf_line = f"\nConfiança ML: {Decimal(str(trade.ml_confidence)) * 100:.0f}%"
    if trade.model_version:
        conf_line += f" | Modelo: <code>{trade.model_version}</code>"

    return (
        f"📊 <b>POSIÇÃO ABERTA</b>\n"
        f"<b>{trade.symbol} {side_label}</b>\n"
        f"Entrada: {_fmt_price(trade.entry_price)}\n"
        f"Quantidade: {trade.quantity}\n"
        f"Stop Loss: {_fmt_price(trade.stop_loss)}{sl_pct}"
        f"{tp_line}"
        f"\nCapital em risco: {_fmt_price(trade.entry_value)}"
        f"{conf_line}"
    )


def format_position_closed(trade: Trade) -> str:
    pnl = Decimal(str(trade.pnl_net)) if trade.pnl_net else Decimal("0")
    emoji = "✅" if pnl >= 0 else "❌"
    reason_map = {
        "TP_HIT": "TP atingido",
        "SL_HIT": "SL atingido",
        "manual": "Fechamento manual",
        "DD_LIMIT_CLOSE": "Limite de drawdown",
        "orphan_no_stop": "Sem proteção — emergência",
    }
    reason_label = reason_map.get(trade.close_reason or "", trade.close_reason or "—")
    side_label = "LONG" if trade.side == "buy" else "SHORT"

    conf_line = ""
    if trade.ml_confidence and trade.model_version:
        conf_line = f"\nModelo: <code>{trade.model_version}</code> (conf: {Decimal(str(trade.ml_confidence)) * 100:.0f}%)"

    return (
        f"{emoji} <b>POSIÇÃO FECHADA — {reason_label.upper()}</b>\n"
        f"<b>{trade.symbol} {side_label}</b>\n"
        f"Resultado: <b>{_fmt_price(pnl)} ({_fmt_pct(trade.pnl_pct)})</b>\n"
        f"Entrada: {_fmt_price(trade.entry_price)} → Saída: {_fmt_price(trade.exit_price)}\n"
        f"Duração: {_duration(trade.duration_sec)}"
        f"{conf_line}"
    )


def format_drawdown_alert(drawdown_pct: Decimal, status: str, market: str) -> str:
    emoji = "🚨" if status == "CRITICAL" else "⚠️"
    return (
        f"{emoji} <b>ALERTA DE DRAWDOWN — {market}</b>\n"
        f"Drawdown atual: <b>{_fmt_pct(drawdown_pct)}</b>\n"
        f"Status: <b>{status}</b>\n"
        "Verifique o dashboard e considere pausar o bot."
    )


def format_oco_failed(trade: Trade, error: str) -> str:
    return (
        f"🚨 <b>OCO FALHOU — EMERGÊNCIA</b>\n"
        f"Trade #{trade.id} | <b>{trade.symbol}</b>\n"
        f"Lado: {'LONG' if trade.side == 'buy' else 'SHORT'}\n"
        f"Erro: <code>{error[:200]}</code>\n"
        "Posição pode estar sem proteção. Verificar imediatamente!"
    )


def format_daily_report(
    market: str,
    total_trades: int,
    winning: int,
    pnl_day: Decimal,
    pnl_accumulated: Decimal,
    drawdown_1d: Decimal,
    drawdown_30d: Decimal,
    open_trades: int,
) -> str:
    win_rate = (winning / total_trades * 100) if total_trades else Decimal("0")
    emoji = "📈" if pnl_day >= 0 else "📉"
    return (
        f"{emoji} <b>RELATÓRIO DIÁRIO — {market}</b>\n"
        f"Operações: {total_trades} | Vencedoras: {winning} ({win_rate:.1f}%)\n"
        f"P&L do dia: <b>{_fmt_price(pnl_day)}</b>\n"
        f"P&L acumulado: <b>{_fmt_price(pnl_accumulated)}</b>\n"
        f"Drawdown 1d: {_fmt_pct(drawdown_1d)} | 30d: {_fmt_pct(drawdown_30d)}\n"
        f"Posições abertas: {open_trades}"
    )


def format_entry_approval_caption(data: dict) -> str:
    """Versão curta para legenda da imagem do gráfico (limite de 1024 caracteres)."""
    side_label = "COMPRA" if data.get("side") == "buy" else "VENDA"
    return (
        f"📈 <b>{data.get('symbol')}</b> · {data.get('market')} · {data.get('timeframe') or 'n/d'}\n"
        f"Setup DiDi de <b>{side_label}</b> — aguardando sua autorizacao.\n"
        f"Entrada {_fmt_price(data.get('entry_price'))} | Stop {_fmt_price(data.get('stop_loss'))} | "
        f"Alvo {_fmt_price(data.get('take_profit'))}"
    )


def format_entry_approval_request(data: dict) -> str:
    from config import settings

    side_label = "COMPRA" if data.get("side") == "buy" else "VENDA"
    criteria = data.get("criteria", {})
    details = data.get("details", {})
    checks = [
        f"Didi: {'OK' if criteria.get('didi_signal') else 'NOK'}",
        f"DMI/ADX: {'OK' if criteria.get('dmi_trend') and criteria.get('adx_accelerating') else 'NOK'}",
        f"Bollinger abrindo: {'OK' if criteria.get('bollinger_open') else 'NOK'}",
    ]

    analysis = data.get("analysis") or []
    analysis_block = "\n".join(f"• {line}" for line in analysis[:6])

    gain_safe = data.get("gain_safe")
    loss_safe = data.get("loss_safe")
    safe_block = ""
    if gain_safe or loss_safe:
        safe_block = (
            f"\nGain safe (parcial 1R): {_fmt_price(gain_safe)}"
            f"\nLoss safe (stop apos 1R): {_fmt_price(loss_safe)}"
        )

    risk_block = ""
    if data.get("risk_amount"):
        risk_block = f"\nRisco estimado: {_fmt_price(data.get('risk_amount'))}"
    if data.get("potential_gain"):
        risk_block += f" | Ganho no alvo: {_fmt_price(data.get('potential_gain'))}"

    link = ""
    if getattr(settings, "dashboard_url", ""):
        link = f"\n\n👉 Analisar grafico e aprovar: {settings.dashboard_url.rstrip('/')}/approvals"

    return (
        f"🧭 <b>PONTO DE ENTRADA DETECTADO (DiDi)</b>\n"
        f"ID: <code>{data.get('id')}</code>\n"
        f"Ativo: <b>{data.get('symbol')}</b> ({data.get('market')} · {data.get('timeframe') or 'n/d'})\n"
        f"Direcao: <b>{side_label}</b>\n"
        f"Entrada: {_fmt_price(data.get('entry_price'))}\n"
        f"Stop: {_fmt_price(data.get('stop_loss'))}\n"
        f"Alvo: {_fmt_price(data.get('take_profit'))}"
        f"{safe_block}\n"
        f"Qtd sugerida: <b>{data.get('quantity')}</b> cotas"
        f"{risk_block}\n"
        f"Criterios: {' | '.join(checks)}\n"
        f"Motivo DiDi: <code>{details.get('didi_reason', 'n/d')}</code>\n"
        f"\n<b>Analise</b>\n{analysis_block}"
        f"{link}"
    )


def format_exit_signal_alert(trade: Trade, criteria: dict[str, bool], details: dict[str, str]) -> str:
    side_label = "LONG" if trade.side == "buy" else "SHORT"
    checks = [
        f"ADX kick: {'OK' if criteria.get('adx_kick') else 'NOK'}",
        f"Bollinger fechando: {'OK' if criteria.get('bollinger_closing') else 'NOK'}",
        f"Estocastico contrario: {'OK' if criteria.get('stochastic_against') else 'NOK'}",
        f"Trix contrario: {'OK' if criteria.get('trix_against') else 'NOK'}",
        f"Didi reverso c/ tendencia: {'OK' if criteria.get('didi_reverse_with_trend') else 'NOK'}",
    ]
    return (
        f"⚠️ <b>ALERTA DE SAIDA (DiDi)</b>\n"
        f"Trade #{trade.id} | <b>{trade.symbol} {side_label}</b>\n"
        f"Entrada: {_fmt_price(trade.entry_price)} | Stop atual: {_fmt_price(trade.stop_loss)}\n"
        f"Criterios: {' | '.join(checks)}\n"
        f"ADX: {details.get('adx_now', 'n/d')} | Stoch K/D: {details.get('stoch_k', 'n/d')}/{details.get('stoch_d', 'n/d')}\n"
        f"TRIX: {details.get('trix', 'n/d')} / {details.get('trix_signal', 'n/d')}"
    )


def format_safe_break_armed(trade: Trade) -> str:
    return (
        f"🛡️ <b>SAFE BREAK ATIVADO</b>\n"
        f"Trade #{trade.id} | <b>{trade.symbol}</b>\n"
        f"Stop ajustado para break-even em {_fmt_price(trade.entry_price)}"
    )
