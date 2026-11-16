"""Pure evidence assessment for paper protection, not an execution authorization."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum

from trading.connectors.types import OrderSide
from trading.contracts import ExecutionIncrement
from trading.market_feed import MarketDataUnavailable, timeframe_seconds


class EvidenceState(str, Enum):
    VALID = "valid"
    MISSING = "missing"
    STALE = "stale"
    INCOMPLETE = "incomplete_or_ambiguous"
    STOP_CROSSED = "stop_crossed"


@dataclass(frozen=True)
class Candle:
    opened_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class MarketReference:
    price: Decimal
    observed_at: datetime
    source: str


@dataclass(frozen=True)
class FillActivation:
    execution: ExecutionIncrement
    effective_at: datetime


@dataclass(frozen=True)
class CoveredInterval:
    """Source asserts complete price coverage for [started_at, ended_at)."""

    execution_id: str
    started_at: datetime
    ended_at: datetime
    low: Decimal
    high: Decimal
    source: str


@dataclass(frozen=True)
class ProtectionEvidence:
    side: OrderSide
    executed_average: Decimal
    cost_adjusted_entry: Decimal
    desired_stop: Decimal
    reference: MarketReference | None
    decision_candle: Candle | None
    stop_effective_at: datetime | None
    history: tuple[Candle, ...]
    timeframe: str
    calendar: str
    history_source: str
    executions: tuple[FillActivation, ...] = ()
    execution_history_complete: bool | None = None
    exposure_quantity: Decimal | None = None
    intervals: tuple[CoveredInterval, ...] = ()


@dataclass(frozen=True)
class EvidenceAssessment:
    state: EvidenceState
    reason: str
    crossed_execution_ids: tuple[str, ...] = ()


def _time(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _price(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _candle_valid(candle: Candle) -> bool:
    return (
        isinstance(candle, Candle) and _time(candle.opened_at) is not None
        and all(_price(value) for value in (candle.open, candle.high, candle.low, candle.close))
        and candle.low <= min(candle.open, candle.close) <= max(candle.open, candle.close) <= candle.high
    )


def assess_protection_evidence(
    evidence: ProtectionEvidence, *, now: datetime, max_age: timedelta,
) -> EvidenceAssessment:
    """Assess closed 24/7 candles through the reference close, never infer prices.

    The result describes only the supplied evidence as of its last close. It
    does not establish claim ownership or authorize an effect.
    """
    missing = EvidenceAssessment(EvidenceState.MISSING, "Market reference or decision/activation time missing")
    ambiguous = EvidenceAssessment(EvidenceState.INCOMPLETE, "Incomplete or ambiguous market history")
    if evidence.reference is None or evidence.decision_candle is None or evidence.stop_effective_at is None:
        return missing
    reference = evidence.reference
    current, observed, effective = _time(now), _time(reference.observed_at), _time(evidence.stop_effective_at)
    if (current is None or observed is None or effective is None or not isinstance(max_age, timedelta)
            or max_age < timedelta(0) or not isinstance(reference.source, str) or not reference.source.strip()
            or not isinstance(evidence.history_source, str) or not evidence.history_source.strip()
            or reference.source != evidence.history_source
            or evidence.side not in (OrderSide.BUY, OrderSide.SELL)
            or not all(_price(v) for v in (evidence.executed_average, evidence.cost_adjusted_entry,
                                           evidence.desired_stop, reference.price))):
        return ambiguous
    if observed > current:
        return ambiguous
    if current - observed > max_age:
        return EvidenceAssessment(EvidenceState.STALE, "Market reference is older than the allowed age")
    if evidence.calendar != "24/7":
        return EvidenceAssessment(EvidenceState.INCOMPLETE, "Unsupported or unverified market calendar")
    if not isinstance(evidence.timeframe, str) or not evidence.timeframe.strip():
        return ambiguous
    try:
        period = timedelta(seconds=timeframe_seconds(evidence.timeframe))
    except (MarketDataUnavailable, AttributeError, TypeError):
        return ambiguous
    decision = evidence.decision_candle
    if not _candle_valid(decision) or any(not _candle_valid(c) for c in evidence.history):
        return ambiguous
    start = _time(decision.opened_at)
    if start is None:
        return ambiguous
    utc_monday = datetime(1970, 1, 5, tzinfo=timezone.utc)
    if (start - utc_monday) % period:
        return ambiguous
    decision_end = start + period
    if effective < start or effective > observed or decision_end > current:
        return ambiguous
    expected = decision_end
    last = decision
    for candle in evidence.history:
        candle_start = _time(candle.opened_at)
        if candle_start is None or candle_start != expected or candle_start + period > current:
            return ambiguous
        expected = candle_start + period
        last = candle
    if expected != observed or last.close != reference.price:
        return ambiguous
    activations: list[tuple[str, datetime]] = []
    if evidence.execution_history_complete is not None or evidence.executions or evidence.intervals:
        if (evidence.execution_history_complete is not True or not evidence.executions
                or not _price(evidence.exposure_quantity)):
            return EvidenceAssessment(EvidenceState.INCOMPLETE, "Execution history is incomplete or missing")
        total = Decimal("0")
        last_revision = 0
        last_execution_time = None
        seen: set[str] = set()
        previous_effective = None
        for activation in evidence.executions:
            if not isinstance(activation, FillActivation) or not isinstance(activation.execution, ExecutionIncrement):
                return ambiguous
            fill = activation.execution
            executed_at, activation_time = _time(fill.executed_at), _time(activation.effective_at)
            if (not isinstance(fill.execution_id, str) or not fill.execution_id.strip()
                    or fill.execution_id in seen or type(fill.revision) is not int or fill.revision <= last_revision
                    or not _price(fill.quantity) or not _price(fill.cumulative_quantity)
                    or executed_at is None or activation_time is None
                    or activation_time != max(executed_at, effective)
                    or activation_time < start or activation_time >= observed
                    or last_execution_time is not None and executed_at < last_execution_time
                    or previous_effective is not None and activation_time < previous_effective):
                return ambiguous
            total += fill.quantity
            if total != fill.cumulative_quantity:
                return EvidenceAssessment(EvidenceState.INCOMPLETE, "Execution increments are incomplete")
            activations.append((fill.execution_id, activation_time))
            seen.add(fill.execution_id)
            last_revision, last_execution_time, previous_effective = fill.revision, executed_at, activation_time
        if total != evidence.exposure_quantity or effective != activations[0][1]:
            return EvidenceAssessment(EvidenceState.INCOMPLETE, "Execution exposure or activation mismatch")
    else:
        return EvidenceAssessment(EvidenceState.INCOMPLETE, "Execution history is incomplete or missing")

    grouped: dict[str, list[CoveredInterval]] = {}
    for interval in evidence.intervals:
        if (not isinstance(interval, CoveredInterval) or interval.execution_id not in {a[0] for a in activations}
                or not isinstance(interval.source, str) or interval.source != evidence.history_source
                or not _price(interval.low) or not _price(interval.high) or interval.low > interval.high):
            return ambiguous
        grouped.setdefault(interval.execution_id, []).append(interval)
    crossed: list[str] = []
    for execution_id, activation_time in activations:
        intervals = grouped.get(execution_id, [])
        used = 0
        hit = False
        for candle in (decision, *evidence.history):
            candle_start = _time(candle.opened_at)
            if candle_start is None:
                return ambiguous
            candle_end = candle_start + period
            if candle_end <= activation_time:
                continue
            if candle_start >= activation_time:
                low, high = candle.low, candle.high
                hit |= (low <= evidence.desired_stop if evidence.side == OrderSide.BUY
                        else high >= evidence.desired_stop)
                continue
            expected_start = activation_time
            while expected_start < candle_end and used < len(intervals):
                interval = intervals[used]
                interval_start, interval_end = _time(interval.started_at), _time(interval.ended_at)
                if (interval_start != expected_start or interval_end is None or interval_end <= interval_start
                        or interval_end > candle_end or interval.low < candle.low or interval.high > candle.high):
                    return EvidenceAssessment(EvidenceState.INCOMPLETE, "Granular coverage is ambiguous or has gaps")
                hit |= (interval.low <= evidence.desired_stop if evidence.side == OrderSide.BUY
                        else interval.high >= evidence.desired_stop)
                expected_start = interval_end
                used += 1
            if expected_start != candle_end:
                return EvidenceAssessment(EvidenceState.INCOMPLETE, "Granular coverage is incomplete after activation")
        if used != len(intervals):
            return EvidenceAssessment(EvidenceState.INCOMPLETE, "Unexpected or unordered granular intervals")
        if hit:
            crossed.append(execution_id)
    if crossed:
        return EvidenceAssessment(EvidenceState.STOP_CROSSED, "Stop crossed during its effective interval",
                                  tuple(identifier for identifier in crossed if identifier))
    if ((evidence.side == OrderSide.BUY and reference.price <= evidence.desired_stop)
            or (evidence.side == OrderSide.SELL and reference.price >= evidence.desired_stop)):
        return EvidenceAssessment(EvidenceState.STOP_CROSSED, "Stop crossed at the reference close",
                                  tuple(identifier for identifier, _ in activations))
    return EvidenceAssessment(EvidenceState.VALID, "Closed market history is complete through the reference")


def _payload_mapping(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid persisted evidence field: {field}")
    return value


def _payload_decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Invalid persisted evidence decimal: {field}")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid persisted evidence decimal: {field}") from exc
    if not number.is_finite():
        raise ValueError(f"Invalid persisted evidence decimal: {field}")
    return number


def _payload_datetime(value: object, field: str) -> datetime:
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid persisted evidence datetime: {field}") from exc
    else:
        raise ValueError(f"Invalid persisted evidence datetime: {field}")
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        raise ValueError(f"Invalid persisted evidence datetime: {field}")
    return candidate


def _payload_candle(value: object) -> Candle:
    item = _payload_mapping(value, "candle")
    return Candle(
        _payload_datetime(item.get("opened_at"), "candle.opened_at"),
        _payload_decimal(item.get("open"), "candle.open"),
        _payload_decimal(item.get("high"), "candle.high"),
        _payload_decimal(item.get("low"), "candle.low"),
        _payload_decimal(item.get("close"), "candle.close"),
    )


def protection_evidence_from_payload(value: object) -> ProtectionEvidence:
    """Rebuild evidence persisted by ``record_protection_evidence``.

    The persisted payload is historical and untrusted for an effect: callers
    must reassess it with the current clock before using it.
    """
    item = _payload_mapping(value, "evidence")
    reference = item.get("reference")
    if reference is not None:
        reference_item = _payload_mapping(reference, "reference")
        reference = MarketReference(
            _payload_decimal(reference_item.get("price"), "reference.price"),
            _payload_datetime(reference_item.get("observed_at"), "reference.observed_at"),
            reference_item.get("source") if isinstance(reference_item.get("source"), str) else "",
        )
    decision = item.get("decision_candle")
    if decision is not None:
        decision = _payload_candle(decision)
    history = item.get("history", ())
    if not isinstance(history, (list, tuple)):
        raise ValueError("Invalid persisted evidence field: history")
    executions = []
    for activation in item.get("executions", ()) or ():
        activation_item = _payload_mapping(activation, "execution activation")
        execution = _payload_mapping(activation_item.get("execution"), "execution")
        executions.append(FillActivation(
            ExecutionIncrement(
                execution_id=execution.get("execution_id")
                if isinstance(execution.get("execution_id"), str) else "",
                revision=execution.get("revision") if type(execution.get("revision")) is int else 0,
                quantity=_payload_decimal(execution.get("quantity"), "execution.quantity"),
                cumulative_quantity=_payload_decimal(
                    execution.get("cumulative_quantity"), "execution.cumulative_quantity"),
                executed_at=_payload_datetime(execution.get("executed_at"), "execution.executed_at"),
            ),
            _payload_datetime(activation_item.get("effective_at"), "execution.effective_at"),
        ))
    intervals = []
    for interval in item.get("intervals", ()) or ():
        interval_item = _payload_mapping(interval, "coverage interval")
        intervals.append(CoveredInterval(
            execution_id=interval_item.get("execution_id")
            if isinstance(interval_item.get("execution_id"), str) else "",
            started_at=_payload_datetime(interval_item.get("started_at"), "interval.started_at"),
            ended_at=_payload_datetime(interval_item.get("ended_at"), "interval.ended_at"),
            low=_payload_decimal(interval_item.get("low"), "interval.low"),
            high=_payload_decimal(interval_item.get("high"), "interval.high"),
            source=interval_item.get("source") if isinstance(interval_item.get("source"), str) else "",
        ))
    exposure = item.get("exposure_quantity")
    complete = item.get("execution_history_complete")
    return ProtectionEvidence(
        side=OrderSide(item.get("side")),
        executed_average=_payload_decimal(item.get("executed_average"), "executed_average"),
        cost_adjusted_entry=_payload_decimal(item.get("cost_adjusted_entry"), "cost_adjusted_entry"),
        desired_stop=_payload_decimal(item.get("desired_stop"), "desired_stop"),
        reference=reference,
        decision_candle=decision,
        stop_effective_at=(
            _payload_datetime(item.get("stop_effective_at"), "stop_effective_at")
            if item.get("stop_effective_at") is not None else None
        ),
        history=tuple(_payload_candle(candle) for candle in history),
        timeframe=item.get("timeframe") if isinstance(item.get("timeframe"), str) else "",
        calendar=item.get("calendar") if isinstance(item.get("calendar"), str) else "",
        history_source=item.get("history_source") if isinstance(item.get("history_source"), str) else "",
        executions=tuple(executions),
        execution_history_complete=complete if type(complete) is bool else None,
        exposure_quantity=(
            _payload_decimal(exposure, "exposure_quantity") if exposure is not None else None
        ),
        intervals=tuple(intervals),
    )
