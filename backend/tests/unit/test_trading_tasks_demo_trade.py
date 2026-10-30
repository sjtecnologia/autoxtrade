from decimal import Decimal

import pytest

from tasks.trading_tasks import _demo_trade_plan, _reprice_approval_levels
from trading.connectors.types import OrderSide


def test_demo_trade_plan_is_generated_for_b3() -> None:
    side, entry_price, stop_loss, take_profit, quantity = _demo_trade_plan("WINM24", "B3")

    assert side == OrderSide.BUY
    assert quantity == Decimal("1")
    assert entry_price > stop_loss
    assert take_profit > entry_price


def test_demo_trade_plan_is_generated_for_forex() -> None:
    _, entry_price, stop_loss, take_profit, quantity = _demo_trade_plan("EURUSD", "FOREX")

    assert quantity == Decimal("1")
    assert entry_price > stop_loss
    assert take_profit > entry_price


def test_reprice_preserves_distances_for_buy() -> None:
    entry, stop, target = _reprice_approval_levels(
        "buy",
        approved_entry=Decimal("1.10000"),
        stop_loss=Decimal("1.09500"),
        take_profit=Decimal("1.11000"),
        current_price=Decimal("1.10100"),
        max_slippage_pct=0.25,
        tick_size=Decimal("0.00001"),
    )

    assert entry == Decimal("1.10100")
    assert stop == Decimal("1.09600")
    assert target == Decimal("1.11100")


def test_reprice_rejects_stale_price_move() -> None:
    with pytest.raises(ValueError, match="preco_desviou"):
        _reprice_approval_levels(
            "buy",
            approved_entry=Decimal("1.10000"),
            stop_loss=Decimal("1.09500"),
            take_profit=Decimal("1.11000"),
            current_price=Decimal("1.11000"),
            max_slippage_pct=0.25,
            tick_size=Decimal("0.00001"),
        )
