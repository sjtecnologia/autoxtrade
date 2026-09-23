"""Local protocol doubles. These classes cannot connect to a broker.

DWX command syntax follows darwinex/dwxconnect/python/api/dwx_client.py.
The SQLite receipt/state protocol here is ONLY a local test harness, not an EA API.
"""
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from dataclasses import fields, is_dataclass
from decimal import Decimal

from trading.contracts import (ExecutionContext, Instrument, OrderIntent, ExecutionResult,
    ResultState, Position, Fill, Capability, ExecutionMode, CapabilityUnavailable)
from trading.safety import AccountNature
from trading.connectors.types import OrderSide, OrderType
from trading.connectors.memory_transport import MemoryTransport


def _encode(value):
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, frozenset):
        return list(value)
    if is_dataclass(value):
        return {"__class__": type(value).__name__, **{f.name: getattr(value, f.name) for f in fields(value)}}
    if isinstance(value, set):
        return list(value)
    raise TypeError(type(value).__name__)


def _decode(obj):
    if "__decimal__" in obj:
        return Decimal(obj["__decimal__"])
    kind = obj.pop("__class__", None)
    if kind == "ExecutionContext":
        obj.update(nature=AccountNature(obj["nature"]), mode=ExecutionMode(obj["mode"]),
                   capabilities=frozenset(Capability(c) for c in obj["capabilities"]))
    if kind in {"OrderIntent", "Position"}:
        obj["side"] = OrderSide(obj["side"])
    if kind == "OrderIntent":
        obj["order_type"] = OrderType(obj["order_type"])
    if kind == "ExecutionResult":
        obj["state"] = ResultState(obj["state"])
    classes = {c.__name__: c for c in (ExecutionContext, Instrument, OrderIntent, ExecutionResult, Position, Fill)}
    return classes[kind](**obj) if kind else obj


class FakeCCXTClient(MemoryTransport):
    """CCXT 4.3.1 spot signature, with deterministic offline outcomes."""
    version = "4.3.1"

    def __init__(self, context):
        super().__init__(context)
        self.calls = []
        self.timeout_after_accept = set()

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.calls.append(("create_order", symbol, type, side, amount, price, dict(params or {})))
        intent_id = params["newClientOrderId"]
        result = super().create(OrderIntent(intent_id, self.context, OrderSide(side), OrderType(type),
                                            Decimal(str(amount)), Decimal(str(price)) if price else None))
        if intent_id in self.timeout_after_accept:
            raise TimeoutError("Simulated lost response after acceptance")
        return self.raw(result)

    async def fetch_order(self, id, symbol=None, params=None):
        self.calls.append(("fetch_order", id, symbol, dict(params or {})))
        result = self.query(intent_id=(params or {}).get("origClientOrderId"), order_id=id)
        return self.raw(result)

    def raw(self, result):
        intent = self.intents.get(result.intent_id)
        return {"id": result.order_id, "clientOrderId": result.intent_id,
                "symbol": self.context.instrument.symbol,
                "status": {ResultState.REJECTED: "rejected", ResultState.UNKNOWN: "unknown"}.get(
                    result.state, "closed" if intent and result.filled == intent.quantity else "open"),
                "filled": str(result.filled), "average": str(result.average) if result.average else None,
                "amount": str(intent.quantity) if intent else "0", "position_id": result.position_id}


class TemporaryDWXTransport(MemoryTransport):
    """Owns its temporary directory. There is deliberately no arbitrary path option.

    SQLite BEGIN IMMEDIATE serializes independent instances/processes. Reservation
    is committed before publication; a crash leaves UNKNOWN and is never resent.
    Immutable command files are atomically published, never partially visible.
    """
    def __init__(self, context):
        super().__init__(context)
        self._temporary = tempfile.TemporaryDirectory(prefix="autoxtrade-dwx-fake-")
        self.directory = Path(self._temporary.name)
        self._initialize()

    def fork(self):
        """New client/restart of this same local fake account; no external path accepted."""
        clone = object.__new__(type(self))
        MemoryTransport.__init__(clone, self.context)
        clone._temporary, clone.directory = self._temporary, self.directory
        return clone

    def _database(self):
        return sqlite3.connect(self.directory / "journal.sqlite", timeout=10, isolation_level=None)

    def _initialize(self):
        with self._database() as db:
            db.execute("CREATE TABLE commands (id INTEGER PRIMARY KEY AUTOINCREMENT, intent TEXT UNIQUE NOT NULL, command TEXT, content TEXT, result TEXT)")
            db.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            self._save(db)

    def _save(self, db):
        state = {name: getattr(self, name) for name in ("prices", "intents", "results", "positions", "fills", "canceled", "operations", "scenarios")}
        db.execute("INSERT OR REPLACE INTO state VALUES (1, ?)", (json.dumps(state, default=_encode),))

    def _load(self, db):
        data = json.loads(db.execute("SELECT payload FROM state WHERE id=1").fetchone()[0], object_hook=_decode)
        for key, value in data.items():
            setattr(self, key, value)
        self.canceled = set(self.canceled)
        self.operations = {k: (tuple(v[0]), v[1]) for k, v in self.operations.items()}

    def _dispatch(self, intent_id, command, content, operation):
        if any(c in str(intent_id) for c in "|<>\n\r,"):
            raise ValueError("Unsafe intent identifier")
        prices, scenarios = dict(self.prices), dict(self.scenarios)
        with self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT id, command, content, result FROM commands WHERE intent=?", (intent_id,)).fetchone()
            if previous:
                db.commit()
                if (previous[1], previous[2]) != (command, content):
                    raise ValueError("Intent reused with different DWX command")
                return json.loads(previous[3], object_hook=_decode) if previous[3] else ExecutionResult(intent_id, ResultState.UNKNOWN)
            if db.execute("SELECT 1 FROM commands WHERE result IS NULL LIMIT 1").fetchone():
                db.rollback()
                raise CapabilityUnavailable("Unresolved DWX publication; reconcile before another command")
            self._load(db)
            self.prices.update(prices)
            self.scenarios.update(scenarios)
            # Validate and apply only to local state; publish+state commit below.
            result = operation()
            row = db.execute("INSERT INTO commands(intent,command,content) VALUES(?,?,?)", (intent_id, command, content))
            command_id = row.lastrowid
            if command_id >= 100000:
                db.rollback()
                raise CapabilityUnavailable("DWX command id exhausted; no wrap/reset")
            db.commit()  # journal reservation survives process crash
            body = f"<:{command_id}|{command}|{content}:>"
            final = self.directory / f"DWX_Commands_{command_id}.txt"
            staging = self.directory / f".{command_id}.staging"
            with staging.open("x", encoding="utf-8") as file:
                file.write(body)
                file.flush()
                os.fsync(file.fileno())
            os.replace(staging, final)
            db.execute("BEGIN IMMEDIATE")
            # Other commands must not overtake an unconfirmed reservation.
            db.execute("UPDATE commands SET result=? WHERE id=?", (json.dumps(result, default=_encode), command_id))
            self._save(db)
            db.commit()
            return result

    def create(self, intent):
        intent.validate()
        symbol = intent.context.instrument.symbol
        if any(c in symbol for c in ",|<>\n\r"):
            raise ValueError("Unsafe native symbol")
        kind = intent.side.value + ("limit" if intent.order_type == OrderType.LIMIT else "")
        content = f"{symbol},{kind},{intent.quantity},{intent.price or 0},0,0,0,{intent.id},0"
        return self._dispatch(intent.id, "OPEN_ORDER", content, lambda: MemoryTransport.create(self, intent))

    def protect(self, position_id, stop_loss, take_profit, operation_id):
        return self._dispatch(operation_id, "MODIFY_ORDER", f"{position_id},0,{stop_loss},{take_profit or 0},0",
                              lambda: MemoryTransport.protect(self, position_id, stop_loss, take_profit, operation_id))

    def reduce(self, position_id, amount, operation_id):
        return self._dispatch(operation_id, "CLOSE_ORDER", f"{position_id},{amount}",
                              lambda: MemoryTransport.reduce(self, position_id, amount, operation_id))

    def cancel(self, order_id, operation_id):
        # Official wire command is shared; local contract verifies pending identity.
        return self._dispatch(operation_id, "CLOSE_ORDER", f"{order_id},0",
                              lambda: MemoryTransport.cancel(self, order_id, operation_id))

    def refresh(self):
        with self._database() as db:
            self._load(db)

    def confirm(self, intent_id, command_id, cumulative_filled):
        with self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT intent FROM commands WHERE id=?", (command_id,)).fetchone()
            if row is None or row[0] != intent_id:
                raise ValueError("Confirmation does not match command and intent")
            self._load(db)
            result = self.settle(intent_id, cumulative_filled)
            self._save(db)
            db.execute("UPDATE commands SET result=? WHERE id=?", (json.dumps(result, default=_encode), command_id))
            db.commit()
            return result
