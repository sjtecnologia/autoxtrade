from importlib import import_module
from trading.connectors.types import (
    Balance,
    OHLCVCandle,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)


def __getattr__(name):
    if name == "BaseConnector":
        return getattr(import_module("trading.connectors.base"), name)
    if name == "get_connector":
        return getattr(import_module("trading.connectors.factory"), name)
    # Paper execution must not import/initialize an operational broker adapter.
    if name == "BinanceConnector":
        from trading.connectors.binance import BinanceConnector
        return BinanceConnector
    if name == "DWXConnector":
        from trading.connectors.dwx import DWXConnector
        return DWXConnector
    raise AttributeError(name)

__all__ = [
    "BaseConnector",
    "BinanceConnector",
    "DWXConnector",
    "get_connector",
    "Balance",
    "OHLCVCandle",
    "Order",
    "OrderSide",
    "OrderStatus",
    "OrderType",
]
