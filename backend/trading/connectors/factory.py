"""Account/venue/mode/instrument bound connector cache, scoped to event loop."""
import asyncio
from threading import RLock
from weakref import WeakKeyDictionary

from trading.contracts import ExecutionContext, CapabilityUnavailable
from trading.safety import require_entry_mode
from trading.connectors.memory_transport import MemoryTransport

_instances = WeakKeyDictionary()
_sync_instances = {}
_memory_accounts = {}
_lock = RLock()


def get_connector(context: ExecutionContext, force_new=False, *, mode=None, transport=None):
    if type(context) is not ExecutionContext:
        if mode is not None:
            require_entry_mode(mode)
        raise CapabilityUnavailable("Explicit account/venue/instrument context required; market is not a venue")
    context.validate()
    if mode is not None and mode != context.mode.value:
        raise ValueError("Requested mode conflicts with bound context")
    require_entry_mode(context.mode.value)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    with _lock:
        cache = _sync_instances if loop is None else _instances.setdefault(loop, {})
        key = (context, id(transport) if transport is not None else None)
        old = cache.get(key)
        if old is not None and not old.closed and not force_new:
            return old
        if old is not None:
            old.retire()
        if transport is None:
            transport = _memory_accounts.setdefault(context, MemoryTransport(context))
        connector = _create(context, transport=transport)
        cache[key] = connector
        return connector


def _create(context: ExecutionContext, *, transport=None, mode=None):
    if type(context) is not ExecutionContext:
        if mode is not None:
            require_entry_mode(mode)
        raise CapabilityUnavailable("Explicit context required")
    context.validate()
    require_entry_mode(context.mode.value)
    if mode is not None and mode != context.mode.value:
        raise ValueError("Mode conflict")
    from trading.connectors.fake_transports import TemporaryDWXTransport, FakeCCXTClient
    if transport is None or type(transport) is MemoryTransport:
        from trading.connectors.simulated import SimulatedConnector
        return SimulatedConnector(context, transport=transport)
    if type(transport) is TemporaryDWXTransport and context.venue == "mt5":
        from trading.connectors.dwx import DWXConnector
        return DWXConnector("", context=context, transport=transport)
    if type(transport) is FakeCCXTClient and context.venue == "binance_spot":
        from trading.connectors.binance import BinanceConnector
        return BinanceConnector(context=context, client=transport)
    raise CapabilityUnavailable("Transport/venue not validated; external clients disabled")
