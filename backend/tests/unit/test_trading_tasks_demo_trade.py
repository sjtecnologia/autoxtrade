from decimal import Decimal

from tasks.trading_tasks import _demo_trade_plan
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
