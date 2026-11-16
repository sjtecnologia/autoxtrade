"""PostgreSQL-only simulated position effects, independent of in-memory transports."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Numeric, String, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from database import Base
from paper.protection_evidence import (
    EvidenceState, ProtectionEvidence, assess_protection_evidence, protection_evidence_from_payload,
)
from trading.connectors.types import OrderSide
from trading.contracts import (Capability, ExecutionContext, ExecutionIncrement, ExecutionMode,
                               ExecutionResult, OrderIntent, Position, ResultState)
from trading.safety import AccountNature, ExposureBlocked


class ProtectionClaimOccupied(ValueError):
    """Another owner holds the current protection lease."""


class ProtectionEvidenceRequired(ValueError):
    """The current protection effect has no valid market evidence."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class PaperEffectPosition(Base):
    __tablename__ = "paper_effect_positions"

    account_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    context: Mapped[dict] = mapped_column(JSONB, nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_paper_effect_quantity"),
        CheckConstraint("fence >= 0", name="ck_paper_effect_fence"),
    )


class PaperEffectOperation(Base):
    __tablename__ = "paper_effect_operations"

    account_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_key", "position_id"],
            ["paper_effect_positions.account_key", "paper_effect_positions.position_id"],
        ),
    )


class PaperEntryFill(Base):
    __tablename__ = "paper_entry_fills"

    account_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    cumulative_quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_key", "position_id", "operation_id"],
            ["paper_effect_operations.account_key", "paper_effect_operations.position_id",
             "paper_effect_operations.operation_id"],
        ),
        CheckConstraint("revision > 0 AND quantity > 0 AND cumulative_quantity >= quantity",
                        name="ck_paper_entry_fill_values"),
    )


class PaperProtectionAttempt(Base):
    __tablename__ = "paper_protection_attempts"

    account_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_key", "position_id"],
            ["paper_effect_positions.account_key", "paper_effect_positions.position_id"],
        ),
        CheckConstraint("(owner IS NULL) = (lease_until IS NULL)", name="ck_paper_protection_claim_pair"),
    )


class PaperProtectionEvidenceRecord(Base):
    __tablename__ = "paper_protection_evidence"

    account_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    position_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    assessment: Mapped[dict] = mapped_column(JSONB, nullable=False)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_key", "position_id", "operation_id"],
            ["paper_protection_attempts.account_key", "paper_protection_attempts.position_id",
             "paper_protection_attempts.operation_id"],
        ),
    )


def _decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("A positive finite Decimal is required")
    if value.adjusted() >= 12 or value.normalize().as_tuple().exponent < -8:
        raise ValueError("Value exceeds persisted numeric precision")
    return format(value.normalize(), "f")


class DurablePaperEffects:
    """Atomic paper-only effects; position state is never mirrored in memory."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], context: ExecutionContext,
                 *, evidence_provider=None, provider=None, evidence_clock=None):
        context.validate()
        if context.mode != ExecutionMode.PAPER or context.nature != AccountNature.SIMULATED:
            raise ExposureBlocked("Durable effects only support simulated paper accounts")
        self._sessions = sessions
        self._context = context
        self._evidence_provider = evidence_provider if evidence_provider is not None else provider
        # Test-only composition hook. Production leaves this unset and uses
        # PostgreSQL's authoritative clock; it never affects claim leases.
        self._evidence_clock = evidence_clock
        identity = [context.venue, context.broker, context.account_id, context.mode.value]
        self._account_key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()

    def _snapshot(self) -> dict:
        data = self._context.snapshot()
        data["capabilities"] = sorted(data["capabilities"])
        return data

    @property
    def context(self) -> ExecutionContext:
        return self._context

    @property
    def evidence_provider(self):
        return self._evidence_provider

    @staticmethod
    def _attempt_payloads_match(stored: object, supplied: object) -> bool:
        """Compare immutable decisions, excluding a variable quote in v2 attempts."""
        if not isinstance(stored, dict) or not isinstance(supplied, dict):
            return stored == supplied
        if stored.get("payload_version") != supplied.get("payload_version"):
            return False
        if supplied.get("payload_version") == 2:
            return ({key: value for key, value in stored.items() if key != "mark_price"}
                    == {key: value for key, value in supplied.items() if key != "mark_price"})
        return stored == supplied

    @staticmethod
    def _entry_payloads_match(stored: object, supplied: object) -> bool:
        """Paper costs are captured on first write, so replays compare the stable entry."""
        if not isinstance(stored, dict) or not isinstance(supplied, dict):
            return stored == supplied
        return ({key: value for key, value in stored.items() if key != "paper_costs"}
                == {key: value for key, value in supplied.items() if key != "paper_costs"})

    def _cost_adjusted_entry(self, entry: PaperEffectOperation, trade, *, side: str,
                             executed_average: Decimal) -> Decimal:
        """Return the persisted cost-adjusted entry, never an invented fallback."""
        if trade is not None:
            return trade.entry_price
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        costs = payload.get("paper_costs")
        try:
            if not isinstance(costs, dict):
                raise ValueError("missing paper costs")
            spread = Decimal(str(costs["spread_price"]))
            slippage = Decimal(str(costs["slippage_price"]))
            if (not spread.is_finite() or not slippage.is_finite()
                    or spread < 0 or slippage < 0):
                raise ValueError("invalid paper costs")
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise ProtectionEvidenceRequired(
                "Persisted paper costs are required to validate evidence without a trade"
            ) from exc
        offset = spread + slippage
        adjusted = (executed_average + offset if side == "buy"
                    else executed_average - offset)
        if not adjusted.is_finite() or adjusted <= 0:
            raise ProtectionEvidenceRequired("Persisted paper costs produce an invalid entry")
        return adjusted

    async def create_entry(self, intent: OrderIntent, price: Decimal,
                           initial_fill: Decimal | None = None) -> ExecutionResult:
        """Create a market entry and its first cumulative observation atomically."""
        if intent.context != self._context:
            raise ValueError("Intent/account binding conflict")
        intent.validate()
        _decimal(intent.quantity)
        _decimal(price)
        intent.context.instrument.validate(intent.quantity, price)
        filled = intent.quantity if initial_fill is None else initial_fill
        _decimal(filled)
        intent.context.instrument.validate(filled, price)
        if filled > intent.quantity:
            raise ValueError("Fill exceeds requested quantity")
        position_id = "paper_" + intent.id
        operation_id = "entry:" + intent.id
        payload = {"kind": "entry", "side": intent.side.value, "order_type": intent.order_type.value,
                   "quantity": _decimal(intent.quantity), "price": _decimal(price),
                   "limit_price": _decimal(intent.price) if intent.price is not None else None}
        from paper.costs import paper_costs_for
        try:
            payload["paper_costs"] = paper_costs_for(self._context.instrument.symbol).as_dict()
        except ExposureBlocked:
            # Without persisted costs, evidence without a linked trade is not provable.
            pass
        result = {"filled": _decimal(filled), "average": payload["price"],
                  "order_id": position_id, "terminal": filled == intent.quantity, "revision": 1}
        values = dict(account_key=self._account_key, position_id=position_id,
                      context=self._snapshot(), side=intent.side.value,
                      quantity=filled, entry_price=price)
        async with self._sessions() as db:
            async with db.begin():
                from db.models import ExecutionIntent

                persisted_intent = (await db.execute(
                    select(ExecutionIntent).where(ExecutionIntent.intent_id == intent.id)
                    .with_for_update()
                )).scalar_one_or_none()
                if persisted_intent is not None:
                    if (persisted_intent.market != self._context.market
                            or persisted_intent.mode != self._context.mode.value
                            or persisted_intent.symbol != self._context.instrument.symbol
                            or persisted_intent.side != intent.side.value
                            or persisted_intent.quantity != intent.quantity):
                        raise ValueError("Entry intent conflicts with persisted request")
                    if persisted_intent.status not in {"reserved", "planned", "unknown"}:
                        previous = await db.get(PaperEffectOperation, (
                            self._account_key, position_id, operation_id,
                        ))
                        if previous is None:
                            raise ValueError("Entry intent no longer permits a new effect")
                inserted = (await db.execute(
                    insert(PaperEffectPosition).values(**values).on_conflict_do_nothing()
                    .returning(PaperEffectPosition.position_id)
                )).scalar_one_or_none()
                position = await self._locked_position(db, position_id)
                previous = await db.get(PaperEffectOperation, (self._account_key, position_id, operation_id))
                if previous is not None:
                    if not self._entry_payloads_match(previous.payload, payload):
                        raise ValueError("Entry intent reused with different payload")
                    result = previous.result
                elif inserted is None:
                    raise ValueError("Position already exists without matching entry receipt")
                else:
                    db.add(PaperEffectOperation(account_key=self._account_key, position_id=position_id,
                                                operation_id=operation_id, fence=position.fence,
                                                payload=payload, result=result))
                    await db.flush()
                    db.add(PaperEntryFill(
                        account_key=self._account_key, position_id=position_id, operation_id=operation_id,
                        revision=1, quantity=filled, cumulative_quantity=filled,
                    ))
                    await db.flush()
                return await self._entry_result(db, intent.id, position_id, result)

    async def _entry_result(self, db: AsyncSession, intent_id: str,
                            position_id: str, result: dict) -> ExecutionResult:
        terminal = result.get("terminal", True)
        revision = result.get("revision", 1)
        rows = (await db.execute(select(PaperEntryFill).where(
            PaperEntryFill.account_key == self._account_key,
            PaperEntryFill.position_id == position_id,
            PaperEntryFill.operation_id == "entry:" + intent_id,
            PaperEntryFill.revision <= revision,
        ).order_by(PaperEntryFill.revision))).scalars().all()
        executions = tuple(ExecutionIncrement(
            execution_id=f"entry:{intent_id}:{row.revision}", revision=row.revision,
            quantity=row.quantity, cumulative_quantity=row.cumulative_quantity, executed_at=row.executed_at,
        ) for row in rows)
        total = Decimal("0")
        complete = True
        for row in rows:
            total += row.quantity
            if row.cumulative_quantity != total:
                complete = False
        complete = complete and total == Decimal(result["filled"])
        return ExecutionResult(intent_id, ResultState.CONFIRMED if terminal and result.get("full", True)
                               else ResultState.PARTIAL, result["order_id"], position_id,
                               Decimal(result["filled"]), Decimal(result["average"]),
                               terminal=terminal, revision=result.get("revision"), executions=executions,
                               execution_history_complete=complete)

    async def observe_entry(self, intent_id: str, *, filled: Decimal, average: Decimal,
                            terminal: bool, revision: int) -> ExecutionResult:
        """Apply a cumulative observation and its position delta under the same row lock."""
        _decimal(filled)
        _decimal(average)
        if type(terminal) is not bool or type(revision) is not int or revision < 1:
            raise ValueError("Invalid terminality or observation revision")
        self._context.instrument.validate(filled)
        position_id = "paper_" + intent_id
        async with self._sessions() as db:
            async with db.begin():
                position = await self._locked_position(db, position_id)
                operation = await db.get(PaperEffectOperation, (self._account_key, position_id, "entry:" + intent_id))
                if operation is None or operation.payload.get("kind") != "entry":
                    raise ValueError("Entry receipt not found")
                cancelled = (await db.execute(select(PaperEffectOperation.operation_id).where(
                    PaperEffectOperation.account_key == self._account_key,
                    PaperEffectOperation.position_id == position_id,
                    PaperEffectOperation.payload["kind"].astext == "cancel-entry-stop",
                ))).first()
                if cancelled and (revision != operation.result["revision"]
                                  or filled != Decimal(operation.result["filled"])
                                  or average != Decimal(operation.result["average"]) or not terminal):
                    raise ValueError("Entry cancelled by stop; new observations are rejected")
                old = operation.result
                old_filled = Decimal(old["filled"])
                requested = Decimal(operation.payload["quantity"])
                old_revision = old.get("revision", 1)
                old_terminal = old.get("terminal", True)
                if revision < old_revision or filled < old_filled:
                    raise ValueError("Stale observation revision or cumulative fill")
                if filled > requested or (filled == requested and not terminal):
                    raise ValueError("Invalid cumulative fill or terminality")
                if old_terminal and (filled != old_filled or not terminal):
                    raise ValueError("Observation contradicts terminal entry")
                result = {"filled": _decimal(filled), "average": _decimal(average),
                          "order_id": old["order_id"], "terminal": terminal,
                          "revision": revision, "full": filled == requested}
                if revision == old_revision:
                    if (filled != old_filled or average != Decimal(old["average"])
                            or terminal != old_terminal):
                        raise ValueError("Observation revision reused with different payload")
                    return await self._entry_result(db, intent_id, position_id, old)
                if position.quantity != old_filled:
                    raise ValueError("Entry observation after reduction requires manual reconciliation")
                position.quantity += filled - old_filled
                position.entry_price = average
                operation.result = result
                if filled > old_filled:
                    db.add(PaperEntryFill(
                        account_key=self._account_key, position_id=position_id,
                        operation_id="entry:" + intent_id, revision=revision,
                        quantity=filled - old_filled, cumulative_quantity=filled,
                    ))
                await db.flush()
                return await self._entry_result(db, intent_id, position_id, result)

    async def query_entry(self, intent_id: str) -> ExecutionResult:
        position_id = "paper_" + intent_id
        async with self._sessions() as db:
            position = await db.get(PaperEffectPosition, (self._account_key, position_id))
            if position is None:
                return ExecutionResult(intent_id, ResultState.UNKNOWN, reason="Intent not found")
            if position.context != self._snapshot():
                raise ValueError("Paper position context does not match")
            operation = await db.get(PaperEffectOperation, (self._account_key, position_id, "entry:" + intent_id))
            if operation is None or operation.payload.get("kind") != "entry":
                return ExecutionResult(intent_id, ResultState.UNKNOWN, reason="Entry receipt not found")
            return await self._entry_result(db, intent_id, position_id, operation.result)

    async def pending_entries(self) -> list[ExecutionResult]:
        self._context.require(Capability.PENDING)
        async with self._sessions() as db:
            rows = (await db.execute(
                select(PaperEffectOperation, PaperEffectPosition).join(
                    PaperEffectPosition,
                    (PaperEffectOperation.account_key == PaperEffectPosition.account_key)
                    & (PaperEffectOperation.position_id == PaperEffectPosition.position_id),
                ).where(
                    PaperEffectOperation.account_key == self._account_key,
                    PaperEffectOperation.payload["kind"].astext == "entry",
                    PaperEffectPosition.context["instrument"]["symbol"].astext == self._context.instrument.symbol,
                )
            )).all()
            pending = []
            for operation, position in rows:
                if position.context != self._snapshot():
                    raise ValueError("Paper position context does not match")
                if not operation.result.get("terminal", True):
                    pending.append(await self._entry_result(
                        db, operation.operation_id.removeprefix("entry:"), position.position_id, operation.result))
            return pending

    async def list_positions(self) -> list[Position]:
        self._context.require(Capability.POSITIONS)
        async with self._sessions() as db:
            rows = (await db.execute(select(PaperEffectPosition).where(
                PaperEffectPosition.account_key == self._account_key,
                PaperEffectPosition.quantity > 0,
                PaperEffectPosition.context["instrument"]["symbol"].astext == self._context.instrument.symbol,
            ))).scalars().all()
            if any(row.context != self._snapshot() for row in rows):
                raise ValueError("Paper position context does not match")
            return [
                Position(row.position_id, self._context, OrderSide(row.side), row.quantity,
                         row.entry_price, row.stop_loss, row.take_profit)
                for row in rows
            ]

    async def create_position(
        self, position_id: str, side: OrderSide, quantity: Decimal, entry_price: Decimal,
    ) -> None:
        if not position_id or len(position_id) > 100 or not isinstance(side, OrderSide):
            raise ValueError("Invalid position identity")
        _decimal(quantity)
        _decimal(entry_price)
        self._context.instrument.validate(quantity, entry_price)
        values = dict(account_key=self._account_key, position_id=position_id,
                      context=self._snapshot(), side=side.value, quantity=quantity,
                      entry_price=entry_price)
        async with self._sessions() as db:
            async with db.begin():
                inserted = await db.execute(
                    insert(PaperEffectPosition).values(**values)
                    .on_conflict_do_nothing().returning(PaperEffectPosition.position_id)
                )
                if inserted.scalar_one_or_none() is None:
                    position = await self._locked_position(db, position_id)
                    if (position.context != values["context"] or position.side != side.value
                            or position.quantity != quantity or position.entry_price != entry_price
                            or position.fence != 0):
                        raise ValueError("Position already exists with different or modified state")

    async def _locked_position(self, db: AsyncSession, position_id: str) -> PaperEffectPosition:
        position = (await db.execute(
            select(PaperEffectPosition).where(
                PaperEffectPosition.account_key == self._account_key,
                PaperEffectPosition.position_id == position_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if position is None or position.context != self._snapshot():
            raise ValueError("Paper position not found or context does not match")
        return position

    async def acquire_fence(self, position_id: str) -> int:
        async with self._sessions() as db:
            async with db.begin():
                position = await self._locked_position(db, position_id)
                position.fence += 1
                return position.fence

    async def _locked_protection_state(self, db: AsyncSession, position_id: str):
        from db.models import ExecutionIntent, ProtectionAuthority, Trade

        if not position_id.startswith("paper_") or not position_id.removeprefix("paper_"):
            raise ValueError("Invalid paper entry position identity")
        intent_id = position_id.removeprefix("paper_")
        intent = (await db.execute(
            select(ExecutionIntent).where(ExecutionIntent.intent_id == intent_id).with_for_update()
        )).scalar_one_or_none()
        if intent is None:
            raise ValueError("Execution intent missing; reconciliation required")
        trade = (await db.execute(
            select(Trade).where(Trade.intent_id == intent_id).with_for_update()
        )).scalar_one_or_none()
        authority = (await db.execute(
            select(ProtectionAuthority).where(ProtectionAuthority.intent_id == intent_id).with_for_update()
        )).scalar_one_or_none()
        if authority is None:
            raise ValueError("Protection authority missing; reconciliation required")
        if trade is None:
            appeared = (await db.execute(
                select(Trade.id).where(Trade.intent_id == intent_id)
            )).scalar_one_or_none()
            if appeared is not None:
                raise ValueError("Trade appeared during protection; retry reconciliation")
        position = await self._locked_position(db, position_id)
        return intent, trade, authority, position

    def _protection_state_matches(self, intent, trade, authority, position, payload, entry) -> bool:
        intent_id = position.position_id.removeprefix("paper_")
        snapshot = self._snapshot()
        target = Decimal(payload["take_profit"]) if payload["take_profit"] is not None else None
        if (
            intent.intent_id != intent_id or intent.status == "failed"
            or intent.market != self._context.market or intent.mode != self._context.mode.value
            or intent.symbol != self._context.instrument.symbol or intent.side != authority.side
            or intent.quantity != authority.quantity
            or authority.intent_id != intent_id or authority.execution_context != snapshot
            or authority.side != position.side or position.context != snapshot
            or payload.get("identity") != snapshot or payload.get("intent_id") != intent_id
            or payload.get("levels_version") != authority.version
            or payload.get("levels_effective_at") != (
                authority.levels_effective_at.astimezone(timezone.utc).isoformat()
                if authority.levels_effective_at is not None else None
            )
            or authority.stop_loss != Decimal(payload["stop_loss"]) or authority.take_profit != target
            or entry is None or entry.payload.get("kind") != "entry"
            or entry.payload.get("side") != authority.side
            or Decimal(entry.payload["quantity"]) != authority.quantity
            or entry.result.get("order_id") != position.position_id
            or entry.result.get("revision") != payload["revision"]
            or Decimal(entry.result["filled"]) != Decimal(payload["quantity"])
            or position.quantity != Decimal(payload["quantity"])
        ):
            return False
        if trade is None:
            return True
        trade_context = dict(trade.execution_context or {})
        trade_context.pop("paper_costs", None)
        return (
            trade.intent_id == intent_id and trade.protection_authority_id == intent_id
            and trade.status == "open" and trade.identity_status == "bound"
            and trade.position_id == position.position_id and trade.side == authority.side
            and trade.market == self._context.market and trade.exchange == self._context.venue
            and trade.symbol == self._context.instrument.symbol and trade.mode == self._context.mode.value
            and trade.account_id == self._context.account_id
            and trade.account_nature == self._context.nature.value
            and trade.instrument_class == self._context.instrument.asset_class
            and trade_context == snapshot and trade.quantity == position.quantity
            and intent.observation_revision == payload["revision"]
            and trade.stop_loss == authority.stop_loss and trade.take_profit == authority.take_profit
            and trade.protection_levels_version == authority.version
        )

    @staticmethod
    def _level_effective_at(authority) -> datetime | None:
        if authority.version == 1:
            if (authority.levels_effective_at is not None
                    or authority.stop_loss != authority.initial_stop_loss
                    or authority.take_profit != authority.initial_take_profit):
                raise ProtectionEvidenceRequired("Initial protection authority is inconsistent")
            return None
        effective = authority.levels_effective_at
        if (type(authority.version) is not int or authority.version < 2
                or not isinstance(effective, datetime) or effective.tzinfo is None
                or effective.utcoffset() is None):
            raise ProtectionEvidenceRequired(
                "Changed protection levels require a versioned persisted effective time"
            )
        return effective.astimezone(timezone.utc)

    async def prepare_initial_protection(
        self, position_id: str, *, quantity: Decimal, revision: int,
        stop_loss: Decimal, take_profit: Decimal | None,
        owner: str, lease_seconds: int = 60,
    ) -> tuple[str, int, dict]:
        self._context.require(Capability.PROTECT)
        if not isinstance(owner, str) or not owner or len(owner) > 64:
            raise ValueError("Protection claim requires a valid owner")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid protection claim lease")
        if type(revision) is not int or revision < 1:
            raise ValueError("Initial protection requires a confirmed entry revision")
        # New attempts are always evidence-backed. The quote is variable market
        # state, so it is not part of the immutable decision and never invented
        # from the entry average.
        payload = {"kind": "protect", "stop_loss": _decimal(stop_loss),
                   "take_profit": _decimal(take_profit) if take_profit is not None else None,
                   "revision": revision, "quantity": _decimal(quantity), "payload_version": 2}
        async with self._sessions() as db:
            async with db.begin():
                intent, trade, authority, position = await self._locked_protection_state(db, position_id)
                pending_stop = (await db.execute(select(PaperProtectionAttempt.operation_id).where(
                    PaperProtectionAttempt.account_key == self._account_key,
                    PaperProtectionAttempt.position_id == position_id,
                    PaperProtectionAttempt.operation_id.like("partial-stop:%"),
                ))).first()
                if pending_stop:
                    raise ValueError("Partial stop must be recovered before protection")
                payload.update(intent_id=authority.intent_id, identity=self._snapshot(),
                               levels_version=authority.version)
                if (authority.stop_loss != stop_loss or authority.take_profit != take_profit):
                    raise ValueError("Initial protection identity, levels or exposure changed; reconciliation required")
                level_effective_at = self._level_effective_at(authority)
                if level_effective_at is not None:
                    payload["levels_effective_at"] = level_effective_at.isoformat()
                operation_id = "initial:" + hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                entry = await db.get(PaperEffectOperation, (
                    self._account_key, position_id, "entry:" + position_id.removeprefix("paper_")
                ))
                if not self._protection_state_matches(intent, trade, authority, position, payload, entry):
                    raise ValueError("Initial protection identity, levels or exposure changed; reconciliation required")
                attempt = await db.get(PaperProtectionAttempt, (self._account_key, position_id, operation_id))
                if attempt is not None and not self._attempt_payloads_match(attempt.payload, payload):
                    raise ValueError("Initial protection attempt payload conflict")
                prior = (await db.execute(select(PaperProtectionAttempt).where(
                    PaperProtectionAttempt.account_key == self._account_key,
                    PaperProtectionAttempt.position_id == position_id,
                ))).scalars().all()
                for previous_attempt in prior:
                    receipt = await db.get(PaperEffectOperation, (
                        self._account_key, position_id, previous_attempt.operation_id
                    ))
                    if receipt is not None and not self._attempt_payloads_match(
                        receipt.payload, previous_attempt.payload
                    ):
                        raise ValueError("Protection receipt conflicts with its attempt")
                now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
                active = (await db.execute(select(PaperProtectionAttempt).where(
                    PaperProtectionAttempt.account_key == self._account_key,
                    PaperProtectionAttempt.position_id == position_id,
                    PaperProtectionAttempt.lease_until > now,
                    PaperProtectionAttempt.fence == position.fence,
                ))).scalars().all()
                if active and (active[0].owner != owner or active[0].operation_id != operation_id):
                    raise ProtectionClaimOccupied("Protection claim is occupied")
                if active:
                    active[0].lease_until = now + timedelta(seconds=lease_seconds)
                    return operation_id, position.fence, active[0].payload
                position.fence += 1
                if attempt is None:
                    attempt = PaperProtectionAttempt(account_key=self._account_key, position_id=position_id,
                                                     operation_id=operation_id, payload=payload,
                                                     revision=revision, fence=position.fence,
                                                     owner=owner, lease_until=now + timedelta(seconds=lease_seconds))
                    db.add(attempt)
                else:
                    attempt.fence = position.fence
                    attempt.owner = owner
                    attempt.lease_until = now + timedelta(seconds=lease_seconds)
                return operation_id, position.fence, attempt.payload

    async def _validate_evidence_at_effect(
        self, db: AsyncSession, *, position_id: str, operation_id: str, evidence_id: str | None,
        attempt: PaperProtectionAttempt, intent, trade, authority, position, entry,
    ) -> ProtectionEvidence:
        """Reassess a persisted evidence record in the effect transaction."""
        if not isinstance(evidence_id, str) or not evidence_id or len(evidence_id) > 64:
            raise ProtectionEvidenceRequired("Market evidence is missing for this protection attempt")
        record = (await db.execute(select(PaperProtectionEvidenceRecord).where(
            PaperProtectionEvidenceRecord.account_key == self._account_key,
            PaperProtectionEvidenceRecord.position_id == position_id,
            PaperProtectionEvidenceRecord.operation_id == operation_id,
            PaperProtectionEvidenceRecord.evidence_id == evidence_id,
        ).with_for_update())).scalar_one_or_none()
        if record is None:
            raise ProtectionEvidenceRequired("Market evidence record not found for this protection attempt")
        try:
            evidence = protection_evidence_from_payload(record.payload["evidence"])
            max_age = timedelta(seconds=float(record.payload["max_age_seconds"]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ProtectionEvidenceRequired("Market evidence payload is malformed") from exc
        now = self._evidence_clock() if self._evidence_clock is not None else (
            await db.execute(select(func.clock_timestamp()))
        ).scalar_one()
        assessment = assess_protection_evidence(evidence, now=now, max_age=max_age)
        if assessment.state is not EvidenceState.VALID:
            raise ProtectionEvidenceRequired(
                f"Market evidence is {assessment.state.value}: {assessment.reason}"
            )
        if evidence.calendar != "24/7" or self._context.market != "CRIPTO":
            raise ProtectionEvidenceRequired("Market calendar is unsupported for this instrument")
        payload = attempt.payload
        expected_quantity = Decimal(payload["quantity"])
        if (
            evidence.side != OrderSide(authority.side)
            or evidence.side != OrderSide(position.side)
            or evidence.desired_stop != Decimal(payload["stop_loss"])
            or evidence.exposure_quantity != expected_quantity
            or evidence.executed_average != position.entry_price
        ):
            raise ProtectionEvidenceRequired("Market evidence conflicts with the protection decision")
        if entry is None or entry.payload.get("kind") != "entry":
            raise ProtectionEvidenceRequired("Market evidence has no durable entry receipt")
        if (attempt.revision != payload["revision"]
                or entry.result.get("revision") != payload["revision"]):
            raise ProtectionEvidenceRequired("Market evidence revision is stale")
        receipt = await self._entry_result(
            db, position_id.removeprefix("paper_"), position_id, entry.result
        )
        if (
            receipt.execution_history_complete is not True
            or tuple(item.execution for item in evidence.executions) != receipt.executions
            or receipt.filled != expected_quantity
            or position.quantity != expected_quantity
        ):
            raise ProtectionEvidenceRequired(
                "Market evidence execution history conflicts with durable fills"
            )
        level_effective_at = self._level_effective_at(authority)
        activations = tuple(
            max(fill.executed_at, level_effective_at) if level_effective_at else fill.executed_at
            for fill in receipt.executions
        )
        if (not activations or evidence.stop_effective_at != activations[0]
                or any(item.effective_at != active
                       for item, active in zip(evidence.executions, activations))):
            raise ProtectionEvidenceRequired("Protection activation conflicts with durable authority and fills")
        expected_entry = self._cost_adjusted_entry(
            entry, trade, side=authority.side, executed_average=position.entry_price
        )
        if evidence.cost_adjusted_entry != expected_entry:
            raise ProtectionEvidenceRequired(
                "Market evidence entry price conflicts with persisted execution costs"
            )
        return evidence

    async def confirm_initial_protection(self, db: AsyncSession, position_id: str,
                                         operation_id: str, fence: int, payload: dict, owner: str,
                                         evidence_id: str | None = None) -> bool:
        intent, trade, authority, position = await self._locked_protection_state(db, position_id)
        attempt = await db.get(PaperProtectionAttempt, (self._account_key, position_id, operation_id))
        entry = await db.get(PaperEffectOperation, (
            self._account_key, position_id, "entry:" + position_id.removeprefix("paper_")
        ))
        result = await db.get(PaperEffectOperation, (self._account_key, position_id, operation_id))
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        valid = bool(
            attempt is not None and attempt.fence == fence == position.fence
            and attempt.owner == owner and attempt.lease_until is not None and attempt.lease_until > now
            and self._attempt_payloads_match(attempt.payload, payload)
            and result is not None and self._attempt_payloads_match(result.payload, payload)
            and self._protection_state_matches(intent, trade, authority, position, attempt.payload, entry)
            and position.stop_loss == Decimal(payload["stop_loss"])
            and position.take_profit == (Decimal(payload["take_profit"]) if payload["take_profit"] else None)
        )
        if not valid or not self._protection_receipt_matches(result, attempt, payload):
            return False
        try:
            self._level_effective_at(authority)
        except ProtectionEvidenceRequired:
            return False
        return True

    def _protection_receipt_matches(self, result, attempt, payload) -> bool:
        return bool(
            result is not None and attempt is not None
            and attempt.revision == payload.get("revision")
            and self._attempt_payloads_match(attempt.payload, payload)
            and self._attempt_payloads_match(result.payload, attempt.payload)
            and result.result == {"filled": "0", "average": None}
        )

    async def query_initial_protection_receipt(
        self, position_id: str, operation_id: str, fence: int, payload: dict, owner: str,
        evidence_id: str | None = None,
    ) -> ExecutionResult | None:
        async with self._sessions() as db:
            async with db.begin():
                position = await self._locked_position(db, position_id)
                attempt = await db.get(PaperProtectionAttempt, (self._account_key, position_id, operation_id))
                result = await db.get(PaperEffectOperation, (self._account_key, position_id, operation_id))
                now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
                if (attempt is None or attempt.owner != owner or attempt.fence != fence
                        or position.fence != fence or attempt.lease_until is None
                        or attempt.lease_until <= now
                        or not self._attempt_payloads_match(attempt.payload, payload)):
                    raise ValueError("Protection receipt or claim is stale")
                if result is None:
                    return None
                if not self._protection_receipt_matches(result, attempt, payload):
                    raise ValueError("Protection receipt conflicts with its immutable attempt")
                return self._result(operation_id, position_id, result.result)

    async def release_initial_protection_claim(
        self, db: AsyncSession, position_id: str, operation_id: str, fence: int, owner: str,
    ) -> None:
        attempt = await db.get(PaperProtectionAttempt, (self._account_key, position_id, operation_id))
        if attempt is None or attempt.owner != owner or attempt.fence != fence:
            raise ValueError("Protection claim changed before release")
        attempt.owner = None
        attempt.lease_until = None

    def _manual_close_matches(self, intent, trade, authority, position, entry, payload, remaining):
        snapshot = self._snapshot()
        trade_snapshot = dict(trade.execution_context or {}) if trade is not None else {}
        trade_snapshot.pop("paper_costs", None)
        candle = payload.get("candle")
        previous_cursor = payload.get("cursor")
        cursor = trade.paper_monitor_candle_at if trade is not None else None
        cursor_text = cursor.astimezone(timezone.utc).isoformat() if cursor is not None else None
        cursor_matches = (
            candle is None or (
                trade is not None and (
                    (trade.status == "open" and cursor_text == previous_cursor
                     and trade.paper_monitor_timeframe == payload["previous_timeframe"])
                    or (trade.status == "closed" and cursor_text == candle
                        and trade.paper_monitor_timeframe == payload["timeframe"])
                )
            )
        )
        return bool(
            trade is not None and trade.id == payload["trade_id"]
            and cursor_matches
            and trade.intent_id == intent.intent_id == authority.intent_id == payload["intent_id"]
            and trade.protection_authority_id == intent.intent_id
            and trade.position_id == position.position_id == payload["position_id"]
            and trade.status in {"open", "closed"} and trade.identity_status == "bound"
            and trade_snapshot == position.context == authority.execution_context == payload["identity"] == snapshot
            and trade.exchange == self._context.venue and trade.account_id == self._context.account_id
            and trade.mode == intent.mode == self._context.mode.value
            and trade.market == intent.market == self._context.market
            and trade.symbol == intent.symbol == self._context.instrument.symbol
            and trade.side == intent.side == authority.side == position.side
            and intent.status in {"confirmed", "partial", "protection_pending"}
            and intent.order_terminal is True
            and trade.account_nature == self._context.nature.value
            and trade.instrument_class == self._context.instrument.asset_class
            and trade.quantity == Decimal(payload["quantity"])
            and intent.filled_quantity == trade.quantity
            and intent.position_id == position.position_id
            and trade.entry_price == Decimal(payload["entry_price"])
            and trade.entry_value == Decimal(payload["entry_value"])
            and (trade.execution_context or {}).get("paper_costs") == payload["paper_costs"]
            and position.quantity == remaining
            and intent.observation_revision == payload["revision"]
            and entry is not None and entry.payload.get("kind") == "entry"
            and Decimal(entry.payload["quantity"]) == intent.quantity == authority.quantity
            and entry.result.get("revision") == payload["revision"]
            and entry.result.get("terminal") is True
            and Decimal(entry.result["filled"]) == Decimal(payload["quantity"])
            and entry.result.get("order_id") == position.position_id
            and position.entry_price == Decimal(entry.result["average"])
            and trade.stop_loss == authority.stop_loss
            and trade.take_profit == authority.take_profit
            and trade.protection_levels_version == authority.version == payload["levels_version"]
        )

    async def prepare_manual_close(self, position_id: str, *, trade_id: int,
                                   quantity: Decimal, reason: str, execution_price: Decimal,
                                   owner: str, lease_seconds: int = 60,
                                   candle: datetime | None = None, timeframe: str | None = None,
                                   previous_cursor: datetime | None = None,
                                   previous_timeframe: str | None = None,
                                   session: AsyncSession | None = None,
                                   decision_context: dict | None = None) -> tuple[str, int, dict]:
        self._context.require(Capability.REDUCE)
        if not isinstance(owner, str) or not owner or len(owner) > 64:
            raise ValueError("Close claim requires a valid owner")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("Invalid close claim lease")
        if type(trade_id) is not int or trade_id <= 0 or not isinstance(reason, str) or not reason or len(reason) > 20:
            raise ValueError("Invalid close decision")
        self._context.instrument.validate(quantity, execution_price)
        if candle is not None:
            if (candle.tzinfo is None or reason not in {"SL_HIT", "TP_HIT", "DIDI_EXIT"}
                    or not timeframe or len(timeframe) > 10
                    or previous_cursor is not None and previous_cursor.tzinfo is None):
                raise ValueError("Invalid monitor close candle")
            candle_text = candle.astimezone(timezone.utc).isoformat()
            cursor_text = previous_cursor.astimezone(timezone.utc).isoformat() if previous_cursor else None
            decision_key = hashlib.sha256(f"{timeframe}:{candle_text}".encode()).hexdigest()[:32]
            operation_id = f"manual-close:{trade_id}:{decision_key}"
        else:
            if (timeframe is not None or previous_cursor is not None or previous_timeframe is not None
                    or decision_context is not None):
                raise ValueError("Manual close cannot specify a cursor without a candle")
            candle_text = cursor_text = None
            operation_id = f"manual-close:{trade_id}"
        if session is not None and not session.in_transaction():
            raise ValueError("Close decision requires an active transaction")
        async with (self._sessions() if session is None else nullcontext(session)) as db:
            async with (db.begin() if session is None else nullcontext()):
                intent, trade, authority, position = await self._locked_protection_state(db, position_id)
                if trade is None:
                    raise ValueError("Close requires a linked trade")
                existing = None
                if candle is not None:
                    from paper.safe_break import _confirmed_protection, pending_safe_break_decisions
                    pending_safe_break = (await db.execute(
                        pending_safe_break_decisions(trade_id).limit(1)
                    )).scalar_one_or_none()
                    if pending_safe_break is not None:
                        raise ValueError("Pending safe-break protection must be reconciled before a candle close")
                    existing = await db.get(PaperProtectionAttempt, (
                        self._account_key, position_id, operation_id,
                    ))
                    if existing is None and not await _confirmed_protection(
                        db, self, intent, trade, authority, position
                    ):
                        from paper.partial_stop import cancelled_stop_authorizes_close
                        if not await cancelled_stop_authorizes_close(
                            db, self, intent, trade, authority, position,
                            reason=reason, price=execution_price, candle=candle, timeframe=timeframe,
                        ):
                            raise ValueError("Current protection must be confirmed before a candle close")
                if decision_context is None and existing is not None:
                    decision_context = existing.payload.get("decision_context")
                if decision_context is not None:
                    if reason != "DIDI_EXIT" or not isinstance(decision_context, dict):
                        raise ValueError("Invalid strategic decision context")
                    bar = decision_context.get("candle")
                    criteria = decision_context.get("criteria")
                    details = decision_context.get("details")
                    if (set(decision_context) != {"candle", "criteria", "details"}
                            or not isinstance(bar, dict)
                            or set(bar) != {"open", "high", "low", "close", "volume"}
                            or not all(isinstance(bar[key], str) for key in ("open", "high", "low", "close"))
                            or (bar["volume"] is not None and not isinstance(bar["volume"], str))
                            or not isinstance(criteria, dict)
                            or not all(isinstance(key, str) and type(value) is bool
                                       for key, value in criteria.items())
                            or not isinstance(details, dict)
                            or not all(isinstance(key, str) and isinstance(value, str)
                                       for key, value in details.items())):
                        raise ValueError("Invalid strategic decision context")
                from paper.partial_stop import decision_for, cancelled_stop_authorizes_close
                partial_stop = await decision_for(db, self, position_id, trade_id)
                if partial_stop is not None and existing is None:
                    if candle is None or not await cancelled_stop_authorizes_close(
                        db, self, intent, trade, authority, position,
                        reason=reason, price=execution_price, candle=candle, timeframe=timeframe,
                    ):
                        raise ValueError("Persisted partial stop owns the close decision")
                costs = (trade.execution_context or {}).get("paper_costs")
                try:
                    if not isinstance(costs, dict):
                        raise ValueError("Missing paper costs")
                    numbers = [Decimal(str(costs[key])) for key in (
                        "spread_price", "slippage_price", "commission_rate"
                    )]
                    if any(not number.is_finite() or number < 0 for number in numbers):
                        raise ValueError("Invalid paper costs")
                    offset = numbers[0] + numbers[1]
                    if trade.side == "buy" and execution_price <= offset:
                        raise ValueError("Costs exceed execution price")
                except (KeyError, InvalidOperation, TypeError) as exc:
                    raise ValueError("Invalid paper costs; reconciliation required") from exc
                payload = {
                    "kind": "reduce", "intent_id": intent.intent_id, "position_id": position_id,
                    "trade_id": trade_id, "identity": self._snapshot(),
                    "quantity": _decimal(quantity), "execution_price": _decimal(execution_price),
                    "reason": reason, "revision": intent.observation_revision,
                    "levels_version": authority.version, "candle": candle_text, "cursor": cursor_text,
                    "entry_price": _decimal(trade.entry_price),
                    "entry_value": _decimal(trade.entry_value), "paper_costs": costs,
                }
                if candle is not None:
                    payload.update(timeframe=timeframe, previous_timeframe=previous_timeframe)
                if decision_context is not None:
                    payload["decision_context"] = decision_context
                other = (await db.execute(select(PaperProtectionAttempt).where(
                    PaperProtectionAttempt.account_key == self._account_key,
                    PaperProtectionAttempt.position_id == position_id,
                    PaperProtectionAttempt.operation_id.like("manual-close:%"),
                    PaperProtectionAttempt.operation_id != operation_id,
                ))).scalars().first()
                if other is not None:
                    raise ValueError("Another close decision requires reconciliation")
                attempt = await db.get(PaperProtectionAttempt, (self._account_key, position_id, operation_id))
                if attempt is not None and attempt.payload != payload:
                    raise ValueError("Close decision ID reused with different payload")
                receipt = await db.get(PaperEffectOperation, (self._account_key, position_id, operation_id))
                if receipt is not None and (receipt.payload != payload or
                                            Decimal(receipt.result["filled"]) != quantity):
                    raise ValueError("Close receipt conflicts with decision or is partial")
                entry = await db.get(PaperEffectOperation, (
                    self._account_key, position_id, "entry:" + intent.intent_id
                ))
                remaining = Decimal("0") if receipt is not None else quantity
                if not self._manual_close_matches(intent, trade, authority, position, entry, payload, remaining):
                    raise ValueError("Close identity or exposure changed; reconciliation required")
                if trade.status == "closed" and receipt is None:
                    raise ValueError("Closed trade has no close receipt")
                now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
                active = (await db.execute(select(PaperProtectionAttempt).where(
                    PaperProtectionAttempt.account_key == self._account_key,
                    PaperProtectionAttempt.position_id == position_id,
                    PaperProtectionAttempt.lease_until > now,
                    PaperProtectionAttempt.fence == position.fence,
                ))).scalars().all()
                if active and (active[0].owner != owner or active[0].operation_id != operation_id):
                    raise ProtectionClaimOccupied("Position claim is occupied")
                if active:
                    active[0].lease_until = now + timedelta(seconds=lease_seconds)
                    return operation_id, position.fence, payload
                position.fence += 1
                if attempt is None:
                    attempt = PaperProtectionAttempt(
                        account_key=self._account_key, position_id=position_id, operation_id=operation_id,
                        payload=payload, revision=payload["revision"], fence=position.fence,
                        owner=owner, lease_until=now + timedelta(seconds=lease_seconds),
                    )
                    db.add(attempt)
                else:
                    attempt.fence = position.fence
                    attempt.owner = owner
                    attempt.lease_until = now + timedelta(seconds=lease_seconds)
                return operation_id, position.fence, payload

    async def confirm_manual_close(self, db: AsyncSession, position_id: str, operation_id: str,
                                   fence: int, payload: dict, owner: str):
        intent, trade, authority, position = await self._locked_protection_state(db, position_id)
        attempt = await db.get(PaperProtectionAttempt, (self._account_key, position_id, operation_id))
        receipt = await db.get(PaperEffectOperation, (self._account_key, position_id, operation_id))
        entry = await db.get(PaperEffectOperation, (self._account_key, position_id, "entry:" + intent.intent_id))
        now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
        if (attempt is None or attempt.owner != owner or attempt.fence != fence
                or position.fence != fence or attempt.lease_until is None or attempt.lease_until <= now
                or attempt.payload != payload):
            raise ValueError("Stale close claim")
        if receipt is None:
            return None, trade
        if (receipt.payload != payload or Decimal(receipt.result["filled"]) != Decimal(payload["quantity"])
                or Decimal(receipt.result["average"]) != Decimal(payload["execution_price"])
                or not self._manual_close_matches(intent, trade, authority, position, entry, payload, Decimal("0"))):
            raise ValueError("Close receipt or exposure is inconsistent")
        return self._result(operation_id, position_id, receipt.result), trade

    async def protect(self, position_id: str, operation_id: str, fence: int, *,
                      stop_loss: Decimal,
                      take_profit: Decimal | None = None, revision: int | None = None,
                      quantity: Decimal | None = None, owner: str | None = None,
                      evidence_id: str | None = None) -> ExecutionResult:
        self._context.require(Capability.PROTECT)
        # A quote is never part of a protection decision: it is supplied by
        # market evidence, validated at effect time.
        payload = {"kind": "protect", "stop_loss": _decimal(stop_loss),
                   "take_profit": _decimal(take_profit) if take_profit is not None else None,
                   "payload_version": 2}
        if revision is not None:
            if quantity is None:
                raise ValueError("Protection quantity required")
            payload.update(revision=revision, quantity=_decimal(quantity))
        return await self._apply(position_id, operation_id, fence, payload, owner=owner,
                                 evidence_id=evidence_id)

    async def reduce(self, position_id: str, operation_id: str, fence: int, *,
                     quantity: Decimal, execution_price: Decimal,
                     owner: str | None = None) -> ExecutionResult:
        self._context.require(Capability.REDUCE)
        payload = {"kind": "reduce", "quantity": _decimal(quantity),
                   "execution_price": _decimal(execution_price)}
        return await self._apply(position_id, operation_id, fence, payload, owner=owner)

    async def _apply(self, position_id: str, operation_id: str, fence: int,
                     payload: dict, *, owner: str | None = None,
                     evidence_id: str | None = None) -> ExecutionResult:
        if not operation_id or len(operation_id) > 100 or type(fence) is not int or fence <= 0:
            raise ValueError("Invalid operation ID or fencing token")
        if operation_id.startswith("initial:") and (payload["kind"] != "protect" or "revision" not in payload):
            raise ValueError("Initial protection requires its prepared exposure")
        async with self._sessions() as db:
            async with db.begin():
                initial = payload["kind"] == "protect" and "revision" in payload
                manual_close = payload["kind"] == "reduce" and operation_id.startswith("manual-close:")
                if initial or manual_close:
                    intent, trade, authority, position = await self._locked_protection_state(db, position_id)
                    attempt = await db.get(PaperProtectionAttempt, (self._account_key, position_id, operation_id))
                    if attempt is None:
                        raise ValueError("Operation attempt missing; reconciliation required")
                    if any(attempt.payload.get(key) != value for key, value in payload.items()):
                        raise ValueError("Operation ID reused with different payload")
                    payload = attempt.payload
                    now = (await db.execute(select(func.clock_timestamp()))).scalar_one()
                    if (not owner or attempt.owner != owner or attempt.fence != fence
                            or fence != position.fence or attempt.lease_until is None
                            or attempt.lease_until <= now):
                        raise ValueError("Stale fencing token or expired protection claim")
                else:
                    position = await self._locked_position(db, position_id)
                previous = await db.get(PaperEffectOperation, (self._account_key, position_id, operation_id))
                if previous is not None:
                    if previous.payload != payload:
                        raise ValueError("Operation ID reused with different payload")
                    if initial and not self._protection_receipt_matches(previous, attempt, payload):
                        raise ValueError("Protection receipt conflicts with its immutable attempt")
                    return self._result(operation_id, position_id, previous.result)
                if fence != position.fence:
                    raise ValueError("Stale fencing token")
                if position.quantity <= 0:
                    raise ValueError("Paper position is closed")
                if payload["kind"] == "protect":
                    version = payload.get("payload_version")
                    if version == 2 and not initial:
                        raise ProtectionEvidenceRequired(
                            "Evidence-backed protection requires a prepared attempt"
                        )
                    if version != 2:
                        # Immutable legacy attempts and receipts are replayed above,
                        # but a legacy attempt can never produce a new effect: only a
                        # versioned, evidence-backed attempt may establish protection.
                        raise ProtectionEvidenceRequired(
                            "Legacy protection cannot produce a new effect without valid evidence"
                        )
                    entry = await db.get(PaperEffectOperation, (
                        self._account_key, position_id, "entry:" + position_id.removeprefix("paper_")
                    ))
                    if (attempt is None or attempt.fence != fence or attempt.payload != payload
                            or attempt.revision != payload["revision"]
                            or not self._protection_state_matches(intent, trade, authority, position, payload, entry)):
                        raise ValueError("Protection attempt identity, levels or exposure is stale")
                    stop = Decimal(payload["stop_loss"])
                    target = Decimal(payload["take_profit"]) if payload["take_profit"] else None
                    evidence = await self._validate_evidence_at_effect(
                        db, position_id=position_id, operation_id=operation_id,
                        evidence_id=evidence_id, attempt=attempt, intent=intent,
                        trade=trade, authority=authority, position=position, entry=entry,
                    )
                    mark = evidence.reference.price
                    self._context.instrument.validate(position.quantity, stop)
                    self._context.instrument.validate(position.quantity, mark)
                    if target is not None:
                        self._context.instrument.validate(position.quantity, target)
                    if (position.side == "buy" and (stop >= mark or target is not None and target <= mark)
                            or position.side == "sell" and (stop <= mark or target is not None and target >= mark)):
                        raise ValueError("Protection conflicts with position side")
                    position.stop_loss, position.take_profit = stop, target
                    result = {"filled": "0", "average": None}
                else:
                    amount, price = Decimal(payload["quantity"]), Decimal(payload["execution_price"])
                    self._context.instrument.validate(amount, price)
                    if manual_close:
                        entry = await db.get(PaperEffectOperation, (
                            self._account_key, position_id, "entry:" + intent.intent_id
                        ))
                        if (trade is None or trade.status != "open"
                                or not self._manual_close_matches(
                                    intent, trade, authority, position, entry, payload, amount
                                )):
                            raise ValueError("Close decision identity or exposure is stale")
                    if amount > position.quantity:
                        raise ValueError("Reduction exceeds position")
                    position.quantity -= amount
                    result = {"filled": payload["quantity"], "average": payload["execution_price"]}
                db.add(PaperEffectOperation(account_key=self._account_key, position_id=position_id,
                                            operation_id=operation_id, fence=fence,
                                            payload=payload, result=result))
                return self._result(operation_id, position_id, result)

    @staticmethod
    def _result(operation_id: str, position_id: str, result: dict) -> ExecutionResult:
        return ExecutionResult(operation_id, ResultState.CONFIRMED, position_id=position_id,
                               filled=Decimal(result["filled"]),
                               average=Decimal(result["average"]) if result["average"] is not None else None)
