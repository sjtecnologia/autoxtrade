from decimal import Decimal

import pandas as pd

from trading.didi_strategy import evaluate_exit, evaluate_safe_break


def test_safe_break_moves_stop_to_entry_on_1r() -> None:
    result = evaluate_safe_break(
        side="buy",
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        current_price=Decimal("105"),
        trigger_rr=Decimal("1.0"),
    )

    assert result.should_move is True
    assert result.new_stop_loss == Decimal("100")


def test_safe_break_not_triggered_before_1r() -> None:
    result = evaluate_safe_break(
        side="sell",
        entry_price=Decimal("100"),
        stop_loss=Decimal("105"),
        current_price=Decimal("96"),
        trigger_rr=Decimal("1.0"),
    )

    assert result.should_move is False


def test_exit_returns_false_with_insufficient_candles() -> None:
    df = pd.DataFrame(
        {
            "open": [1, 2, 3],
            "high": [2, 3, 4],
            "low": [0, 1, 2],
            "close": [1, 2, 3],
            "volume": [10, 10, 10],
        }
    )

    result = evaluate_exit(df, "buy")
    assert result.should_exit is False
