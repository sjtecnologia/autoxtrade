"""Fail-closed execution policy. No broker capability is validated in stage 01."""
from enum import Enum


class AccountNature(str, Enum):
    SIMULATED = "simulated"
    DEMO = "demo"
    REAL = "real"
    UNKNOWN = "unknown"


class ExposureBlocked(RuntimeError):
    pass


def require_entry_mode(mode: str) -> None:
    if mode == "paper":
        return
    if mode != "live":
        raise ExposureBlocked("Unknown execution mode; entry blocked")
    from config import settings
    if not settings.real_new_exposure_enabled:
        raise ExposureBlocked("New real exposure is disabled")
    # A flag alone cannot certify account identity, fills, sizing or protection.
    raise ExposureBlocked("Live execution capability is not validated (stages 02-09)")


def require_new_exposure(connector) -> None:
    _require_local_binding(connector)


def require_position_management(connector) -> None:
    """Independent of entry pause; real mutations await a validated ticket contract."""
    _require_local_binding(connector)


def _require_local_binding(connector):
    from trading.contracts import ExecutionContext, ExecutionMode
    from trading.connectors.base import BaseConnector
    from trading.connectors.memory_transport import MemoryTransport
    from trading.connectors.fake_transports import FakeCCXTClient, TemporaryDWXTransport
    if not isinstance(connector, BaseConnector):
        raise ExposureBlocked("Unbound connector; a simulation label is not authorization")
    context = getattr(connector, "_context", None)
    transport = getattr(connector, "_transport", None)
    if type(context) is not ExecutionContext:
        raise ExposureBlocked("Execution context is missing")
    context.validate()
    if context.mode is not ExecutionMode.PAPER:
        raise ExposureBlocked("All external execution (demo/testnet/live) remains blocked")
    if type(transport) not in {MemoryTransport, FakeCCXTClient, TemporaryDWXTransport}:
        raise ExposureBlocked("Only explicit local fake transports are authorized")
    if transport.context != context:
        raise ExposureBlocked("Connector/transport account binding conflict")
