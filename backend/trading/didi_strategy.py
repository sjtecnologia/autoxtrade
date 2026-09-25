"""Regras operacionais do método DiDi Aguiar para entrada, saída e safe break."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import pandas as pd

try:
    import pandas_ta as ta  # type: ignore
except ImportError:  # pragma: no cover
    ta = None  # type: ignore

TradeSide = Literal["buy", "sell"]


@dataclass
class EntryEvaluation:
    direction: TradeSide
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    criteria: dict[str, bool]
    details: dict[str, str]
    gain_safe: Decimal
    loss_safe: Decimal
    risk_per_unit: Decimal
    risk_reward: Decimal
    analysis: list[str]
    chart: dict[str, Any]


@dataclass
class ExitEvaluation:
    should_exit: bool
    criteria: dict[str, bool]
    details: dict[str, str]


@dataclass
class SafeBreakEvaluation:
    should_move: bool
    new_stop_loss: Decimal
    reason: str


def evaluate_entry(
    df: pd.DataFrame,
    *,
    adx_min: float = 23.0,
    bollinger_open_ratio: float = 1.05,
    rr_target: Decimal = Decimal("2.0"),
    safe_rr: Decimal = Decimal("1.0"),
    chart_candles: int = 180,
) -> EntryEvaluation | None:
    """Retorna sinal de entrada DiDi quando os 3 pilares estão ativos.

    Pilares exigidos:
    - Didi: alerta/confirmacao/agulhada para compra ou venda.
    - DMI: tendencia existente e acelerante (ADX crescente).
    - Bollinger: abertura de bandas ("boca de jacare").
    """
    ind = _build_indicator_frame(df)
    if ind is None or len(ind) < 8:
        return None

    last = ind.iloc[-1]
    prev = ind.iloc[-2]

    didi_buy = bool(last["didi_buy_signal"])
    didi_sell = bool(last["didi_sell_signal"])

    trend_buy = bool(last["dmi_buy"]) and float(last["ADX_14"]) >= adx_min
    trend_sell = bool(last["dmi_sell"]) and float(last["ADX_14"]) >= adx_min

    # ADX acelerando no candle atual
    adx_accelerating = float(last["ADX_14"]) > float(prev["ADX_14"])

    bb_open = float(last["bb_width"]) > float(last["bb_width_ma"]) * bollinger_open_ratio and float(last["bb_width"]) > float(prev["bb_width"])

    direction: TradeSide | None = None
    if didi_buy and trend_buy and adx_accelerating and bb_open:
        direction = "buy"
    elif didi_sell and trend_sell and adx_accelerating and bb_open:
        direction = "sell"

    if direction is None:
        return None

    entry = Decimal(str(last["close"]))
    atr = Decimal(str(last["ATRr_14"]))
    stop_loss = _compute_stop_loss(ind, direction, entry, atr)
    risk = abs(entry - stop_loss)
    if risk <= Decimal("0"):
        return None

    take_profit = entry + risk * rr_target if direction == "buy" else entry - risk * rr_target
    # Gain safe: alvo parcial em 1R. Ao atingi-lo o stop vai para o loss safe (break-even).
    gain_safe = entry + risk * safe_rr if direction == "buy" else entry - risk * safe_rr
    loss_safe = entry

    criteria = {
        "didi_signal": True,
        "dmi_trend": True,
        "adx_accelerating": adx_accelerating,
        "bollinger_open": bb_open,
    }
    details = {
        "didi_reason": str(last["didi_reason"]),
        "adx": f"{float(last['ADX_14']):.2f}",
        "plus_di": f"{float(last['DMP_14']):.2f}",
        "minus_di": f"{float(last['DMN_14']):.2f}",
        "bb_width": f"{float(last['bb_width']):.4f}",
        "bb_width_ma": f"{float(last['bb_width_ma']):.4f}",
        "stoch_k": f"{float(last['STOCHk_14_3_3']):.2f}",
        "stoch_d": f"{float(last['STOCHd_14_3_3']):.2f}",
        "trix": f"{float(last['TRIX_9_4']):.4f}",
        "trix_signal": f"{float(last['TRIXs_9_4']):.4f}",
        "atr": f"{float(last['ATRr_14']):.4f}",
    }

    return EntryEvaluation(
        direction=direction,
        entry_price=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        criteria=criteria,
        details=details,
        gain_safe=gain_safe,
        loss_safe=loss_safe,
        risk_per_unit=risk,
        risk_reward=rr_target,
        analysis=_build_analysis(direction, ind, details, rr_target, safe_rr),
        chart=build_chart_snapshot(ind, chart_candles),
    )


def _build_analysis(
    direction: TradeSide,
    ind: pd.DataFrame,
    details: dict[str, str],
    rr_target: Decimal,
    safe_rr: Decimal,
) -> list[str]:
    """Explica em linguagem natural por que o setup DiDi disparou."""
    last = ind.iloc[-1]
    side_label = "COMPRA" if direction == "buy" else "VENDA"
    reason_map = {
        "agulhada_compra": "agulhada do Didi para cima (médias abrem após convergência)",
        "agulhada_venda": "agulhada do Didi para baixo (médias abrem após convergência)",
        "alerta_compra": "alerta do Didi (média de 3 cruza a de 8 para cima)",
        "alerta_venda": "alerta do Didi (média de 3 cruza a de 8 para baixo)",
        "confirmacao_compra": "confirmação do Didi (média de 20 cruza a de 8)",
        "confirmacao_venda": "confirmação do Didi (média de 20 cruza a de 8)",
    }
    didi_reason = details.get("didi_reason", "none")

    trend_label = "+DI acima de -DI (força compradora)" if direction == "buy" else "-DI acima de +DI (força vendedora)"

    return [
        f"Setup de {side_label} pelo método Didi Aguiar com os 3 pilares alinhados.",
        f"1) Didi Index: {reason_map.get(didi_reason, didi_reason)}.",
        f"2) DMI/ADX: {trend_label}, ADX em {details['adx']} e acelerando (+DI {details['plus_di']} / -DI {details['minus_di']}).",
        f"3) Bollinger: bandas abrindo (largura {details['bb_width']} contra média {details['bb_width_ma']}) — volatilidade entrando a favor.",
        f"Reforço: estocástico em {details['stoch_k']}/{details['stoch_d']} e TRIX {details['trix']} vs sinal {details['trix_signal']}.",
        f"Stop técnico no número de força com folga de ATR ({details['atr']}); alvo cheio em {rr_target}R.",
        f"Gain safe em {safe_rr}R: realize parcial e mova o stop para o loss safe (break-even) para zerar o risco.",
        "Saída antecipada se aparecerem os 4 sinais do Didi: ADX perdendo força, Bollinger fechando, estocástico e TRIX contra a posição.",
        f"Última leitura em {_last_timestamp(ind) or 'candle mais recente'}.",
    ]


def _last_timestamp(ind: pd.DataFrame) -> str | None:
    if "timestamp" not in ind.columns or ind.empty:
        return None
    try:
        return pd.Timestamp(ind.iloc[-1]["timestamp"]).isoformat()
    except (TypeError, ValueError):
        return None


def build_chart_snapshot(ind: pd.DataFrame, limit: int = 180) -> dict[str, Any]:
    """Serializa candles e indicadores para replicar o gráfico analisado no dashboard."""
    window = ind.tail(limit)
    candles: list[dict[str, Any]] = []
    overlays: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("ma3", "ma8", "ma20", "bb_upper", "bb_lower", "adx", "plus_di", "minus_di")
    }

    for _, row in window.iterrows():
        ts = _row_epoch(row)
        if ts is None:
            continue
        candles.append(
            {
                "time": ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0.0) or 0.0),
            }
        )
        mapping = {
            "ma3": row["ma3"],
            "ma8": row["ma8"],
            "ma20": row["ma20"],
            "bb_upper": row["bb_upper"],
            "bb_lower": row["bb_lower"],
            "adx": row["ADX_14"],
            "plus_di": row["DMP_14"],
            "minus_di": row["DMN_14"],
        }
        for key, value in mapping.items():
            if pd.notna(value):
                overlays[key].append({"time": ts, "value": float(value)})

    return {"candles": candles, "overlays": overlays}


def _row_epoch(row) -> int | None:
    value = row.get("timestamp") if hasattr(row, "get") else None
    if value is None:
        return None
    try:
        return int(pd.Timestamp(value).timestamp())
    except (TypeError, ValueError):
        return None


def evaluate_exit(df: pd.DataFrame, side: TradeSide) -> ExitEvaluation:
    """Aplica regra de saida ensinada pelo DiDi (4 sinais)."""
    ind = _build_indicator_frame(df)
    if ind is None or len(ind) < 10:
        return ExitEvaluation(False, {}, {"error": "candles insuficientes"})

    a0 = ind.iloc[-1]
    a1 = ind.iloc[-2]
    a2 = ind.iloc[-3]
    a3 = ind.iloc[-4]

    adx_kick = float(a2["ADX_14"]) > float(a3["ADX_14"]) and float(a2["ADX_14"]) > float(a1["ADX_14"])
    bollinger_closing = float(a0["bb_width"]) < float(a1["bb_width"]) and float(a0["bb_width"]) < float(a0["bb_width_ma"])

    if side == "buy":
        stochastic_against = float(a0["STOCHk_14_3_3"]) < float(a0["STOCHd_14_3_3"])
        trix_against = float(a0["TRIX_9_4"]) < float(a0["TRIXs_9_4"])
        didi_reverse = bool(a0["didi_sell_signal"]) and bool(a0["dmi_sell"])
    else:
        stochastic_against = float(a0["STOCHk_14_3_3"]) > float(a0["STOCHd_14_3_3"])
        trix_against = float(a0["TRIX_9_4"]) > float(a0["TRIXs_9_4"])
        didi_reverse = bool(a0["didi_buy_signal"]) and bool(a0["dmi_buy"])

    four_signals = adx_kick and bollinger_closing and stochastic_against and trix_against
    should_exit = four_signals or didi_reverse

    return ExitEvaluation(
        should_exit=should_exit,
        criteria={
            "adx_kick": adx_kick,
            "bollinger_closing": bollinger_closing,
            "stochastic_against": stochastic_against,
            "trix_against": trix_against,
            "didi_reverse_with_trend": didi_reverse,
        },
        details={
            "adx_now": f"{float(a0['ADX_14']):.2f}",
            "adx_prev": f"{float(a1['ADX_14']):.2f}",
            "stoch_k": f"{float(a0['STOCHk_14_3_3']):.2f}",
            "stoch_d": f"{float(a0['STOCHd_14_3_3']):.2f}",
            "trix": f"{float(a0['TRIX_9_4']):.4f}",
            "trix_signal": f"{float(a0['TRIXs_9_4']):.4f}",
        },
    )


def evaluate_safe_break(
    *,
    side: TradeSide,
    entry_price: Decimal,
    stop_loss: Decimal,
    current_price: Decimal,
    trigger_rr: Decimal = Decimal("1.0"),
) -> SafeBreakEvaluation:
    """Move stop para break-even quando lucro >= 1R (safe break)."""
    initial_risk = abs(entry_price - stop_loss)
    if initial_risk <= Decimal("0"):
        return SafeBreakEvaluation(False, stop_loss, "risk_invalido")

    favorable_move = current_price - entry_price if side == "buy" else entry_price - current_price
    trigger = initial_risk * trigger_rr

    if favorable_move < trigger:
        return SafeBreakEvaluation(False, stop_loss, "ainda_sem_1R")

    if side == "buy" and stop_loss >= entry_price:
        return SafeBreakEvaluation(False, stop_loss, "ja_em_break_even")
    if side == "sell" and stop_loss <= entry_price:
        return SafeBreakEvaluation(False, stop_loss, "ja_em_break_even")

    return SafeBreakEvaluation(True, entry_price, "safe_break_ativado")


def _build_indicator_frame(df: pd.DataFrame) -> pd.DataFrame | None:
    if ta is None:
        return None

    if df is None or len(df) < 80:
        return None

    frame = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col not in frame.columns:
            return None
        frame[col] = frame[col].astype(float)

    frame["ma3"] = frame["close"].rolling(3).mean()
    frame["ma8"] = frame["close"].rolling(8).mean()
    frame["ma20"] = frame["close"].rolling(20).mean()

    frame["didi_short"] = frame["ma3"] / frame["ma8"] - 1.0
    frame["didi_long"] = frame["ma20"] / frame["ma8"] - 1.0

    frame["alert_buy"] = (frame["didi_short"].shift(1) <= 0) & (frame["didi_short"] > 0)
    frame["alert_sell"] = (frame["didi_short"].shift(1) >= 0) & (frame["didi_short"] < 0)
    frame["confirm_buy"] = (frame["didi_long"].shift(1) >= 0) & (frame["didi_long"] < 0)
    frame["confirm_sell"] = (frame["didi_long"].shift(1) <= 0) & (frame["didi_long"] > 0)

    # Agulhada: convergencia forte seguida de ordenacao das 3 medias.
    spread = (frame[["ma3", "ma8", "ma20"]].max(axis=1) - frame[["ma3", "ma8", "ma20"]].min(axis=1)).abs()
    spread_pct = spread / frame["close"].replace(0, pd.NA)
    frame["agulhada_base"] = spread_pct <= 0.0015
    frame["agulhada_buy"] = frame["agulhada_base"].shift(1).fillna(False) & (frame["ma3"] > frame["ma8"]) & (frame["ma8"] > frame["ma20"])
    frame["agulhada_sell"] = frame["agulhada_base"].shift(1).fillna(False) & (frame["ma3"] < frame["ma8"]) & (frame["ma8"] < frame["ma20"])

    frame["didi_buy_signal"] = frame["agulhada_buy"] | frame["alert_buy"] | frame["confirm_buy"]
    frame["didi_sell_signal"] = frame["agulhada_sell"] | frame["alert_sell"] | frame["confirm_sell"]

    frame["didi_reason"] = "none"
    frame.loc[frame["agulhada_buy"], "didi_reason"] = "agulhada_compra"
    frame.loc[frame["agulhada_sell"], "didi_reason"] = "agulhada_venda"
    frame.loc[(frame["didi_reason"] == "none") & frame["alert_buy"], "didi_reason"] = "alerta_compra"
    frame.loc[(frame["didi_reason"] == "none") & frame["alert_sell"], "didi_reason"] = "alerta_venda"
    frame.loc[(frame["didi_reason"] == "none") & frame["confirm_buy"], "didi_reason"] = "confirmacao_compra"
    frame.loc[(frame["didi_reason"] == "none") & frame["confirm_sell"], "didi_reason"] = "confirmacao_venda"

    adx = frame.ta.adx(length=14)
    if adx is None:
        return None
    frame = frame.join(adx)
    frame["dmi_buy"] = frame["DMP_14"] > frame["DMN_14"]
    frame["dmi_sell"] = frame["DMN_14"] > frame["DMP_14"]

    bb = frame.ta.bbands(length=20, std=2)
    if bb is None:
        return None
    bbu = next((c for c in bb.columns if c.startswith("BBU_")), None)
    bbl = next((c for c in bb.columns if c.startswith("BBL_")), None)
    bbm = next((c for c in bb.columns if c.startswith("BBM_")), None)
    if not (bbu and bbl and bbm):
        return None

    frame["bb_upper"] = bb[bbu]
    frame["bb_lower"] = bb[bbl]
    frame["bb_middle"] = bb[bbm]
    frame["bb_width"] = (bb[bbu] - bb[bbl]) / bb[bbm].replace(0, pd.NA)
    frame["bb_width_ma"] = frame["bb_width"].rolling(8).mean()

    stoch = frame.ta.stoch(k=14, d=3)
    if stoch is None:
        return None
    frame = frame.join(stoch)

    trix = frame.ta.trix(length=9, signal=4)
    if trix is None:
        return None
    frame = frame.join(trix)

    frame["ATRr_14"] = frame.ta.atr(length=14)
    frame = frame.dropna().reset_index(drop=True)
    return frame


def _compute_stop_loss(ind: pd.DataFrame, side: TradeSide, entry: Decimal, atr: Decimal) -> Decimal:
    """Stop no numero de forca com buffer + fallback por ATR."""
    win = ind.tail(30)
    local_low = Decimal(str(win["low"].min()))
    local_high = Decimal(str(win["high"].max()))

    atr_risk = max(atr * Decimal("1.2"), entry * Decimal("0.004"))

    if side == "buy":
        support_stop = local_low * Decimal("0.999")
        atr_stop = entry - atr_risk
        return min(support_stop, atr_stop)

    resistance_stop = local_high * Decimal("1.001")
    atr_stop = entry + atr_risk
    return max(resistance_stop, atr_stop)
