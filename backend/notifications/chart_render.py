"""Renderiza o gráfico analisado pelo robô em PNG para envio no Telegram."""
from __future__ import annotations

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)


def render_entry_chart(payload: dict[str, Any], chart: dict[str, Any]) -> bytes | None:
    """Gera um PNG com candles, médias do Didi, bandas e níveis do plano de trade."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ImportError:
        logger.debug("matplotlib indisponível — gráfico do Telegram desativado.")
        return None

    candles = chart.get("candles") or []
    if len(candles) < 5:
        return None

    try:
        from datetime import datetime, timezone

        times = [datetime.fromtimestamp(c["time"], tz=timezone.utc) for c in candles]
        xs = mdates.date2num(times)
        width = (xs[-1] - xs[0]) / max(len(xs), 1) * 0.7

        fig, (ax, ax_dmi) = plt.subplots(
            2, 1, figsize=(10, 6.5), dpi=110, sharex=True, gridspec_kw={"height_ratios": [3, 1]}
        )
        fig.patch.set_facecolor("#0f172a")

        for axis in (ax, ax_dmi):
            axis.set_facecolor("#0f172a")
            axis.grid(color="#1e293b", linewidth=0.6)
            axis.tick_params(colors="#94a3b8", labelsize=8)
            for spine in axis.spines.values():
                spine.set_color("#1e293b")

        for x, candle in zip(xs, candles):
            up = candle["close"] >= candle["open"]
            color = "#22c55e" if up else "#ef4444"
            ax.vlines(x, candle["low"], candle["high"], color=color, linewidth=0.8)
            body_low = min(candle["open"], candle["close"])
            body_height = abs(candle["close"] - candle["open"]) or (candle["high"] - candle["low"]) * 0.01
            ax.add_patch(Rectangle((x - width / 2, body_low), width, body_height, color=color))

        overlays = chart.get("overlays") or {}
        overlay_style = {
            "ma3": ("MA3", "#f97316"),
            "ma8": ("MA8", "#e2e8f0"),
            "ma20": ("MA20", "#38bdf8"),
            "bb_upper": ("Bollinger", "#a855f7"),
            "bb_lower": (None, "#a855f7"),
        }
        for key, (label, color) in overlay_style.items():
            points = overlays.get(key) or []
            if not points:
                continue
            px = mdates.date2num([datetime.fromtimestamp(p["time"], tz=timezone.utc) for p in points])
            ax.plot(px, [p["value"] for p in points], color=color, linewidth=1.0, label=label, alpha=0.9)

        levels = [
            ("Entrada", payload.get("entry_price"), "#facc15", "-"),
            ("Stop", payload.get("stop_loss"), "#ef4444", "--"),
            ("Gain safe", payload.get("gain_safe"), "#14b8a6", ":"),
            ("Alvo", payload.get("take_profit"), "#22c55e", "--"),
        ]
        for label, value, color, style in levels:
            if value in (None, ""):
                continue
            ax.axhline(float(value), color=color, linewidth=1.1, linestyle=style, label=label)

        for key, color in (("adx", "#facc15"), ("plus_di", "#22c55e"), ("minus_di", "#ef4444")):
            points = overlays.get(key) or []
            if not points:
                continue
            px = mdates.date2num([datetime.fromtimestamp(p["time"], tz=timezone.utc) for p in points])
            ax_dmi.plot(px, [p["value"] for p in points], color=color, linewidth=1.0, label=key.upper())

        side_label = "COMPRA" if payload.get("side") == "buy" else "VENDA"
        ax.set_title(
            f"{payload.get('symbol')} · {payload.get('market')} · {payload.get('timeframe') or ''} — {side_label} (Didi)",
            color="#e2e8f0",
            fontsize=11,
        )
        ax.legend(loc="upper left", fontsize=7, facecolor="#0f172a", edgecolor="#1e293b", labelcolor="#cbd5e1", ncol=3)
        ax_dmi.legend(loc="upper left", fontsize=7, facecolor="#0f172a", edgecolor="#1e293b", labelcolor="#cbd5e1", ncol=3)
        ax_dmi.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M"))
        fig.autofmt_xdate(rotation=25)
        fig.tight_layout()

        buffer = io.BytesIO()
        fig.savefig(buffer, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        return buffer.getvalue()
    except Exception as exc:  # renderização é opcional, nunca deve travar o alerta
        logger.warning("Falha ao renderizar gráfico da entrada: %s", exc)
        return None
