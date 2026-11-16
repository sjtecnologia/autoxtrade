"""Persist evidence separately from immutable protection decisions."""
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import inspect
import json

from sqlalchemy import func, select

from paper.durable_effects import (
    DurablePaperEffects, PaperEffectOperation, PaperProtectionAttempt, PaperProtectionEvidenceRecord,
    ProtectionEvidenceRequired,
)
from paper.protection_evidence import (
    ProtectionEvidence, assess_protection_evidence, protection_evidence_from_payload,
)
from trading.connectors.types import OrderSide


def _json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


async def record_protection_evidence(
    effects: DurablePaperEffects, position_id: str, operation_id: str, evidence_id: str,
    evidence: ProtectionEvidence, *, max_age: timedelta,
) -> PaperProtectionEvidenceRecord:
    if not isinstance(evidence_id, str) or not evidence_id.strip() or len(evidence_id) > 64:
        raise ValueError("Invalid evidence ID")
    if not isinstance(max_age, timedelta) or max_age < timedelta(0):
        raise ValueError("Invalid evidence freshness policy")
    payload = {"evidence": _json_value(asdict(evidence)), "max_age_seconds": max_age.total_seconds()}
    key = (effects._account_key, position_id, operation_id, evidence_id)
    async with effects._sessions() as db:
        async with db.begin():
            _, trade, authority, position = await effects._locked_protection_state(db, position_id)
            attempt = (await db.execute(select(PaperProtectionAttempt).where(
                PaperProtectionAttempt.account_key == effects._account_key,
                PaperProtectionAttempt.position_id == position_id,
                PaperProtectionAttempt.operation_id == operation_id,
            ).with_for_update())).scalar_one_or_none()
            if attempt is None or attempt.payload.get("kind") != "protect":
                raise ValueError("Protection attempt not found")
            previous = await db.get(PaperProtectionEvidenceRecord, key)
            if previous is not None:
                if previous.payload != payload:
                    raise ValueError("Evidence ID reused with different payload")
                db.expunge(previous)
                return previous
            if (not isinstance(evidence.desired_stop, Decimal) or not evidence.desired_stop.is_finite()
                    or evidence.desired_stop != Decimal(attempt.payload["stop_loss"])):
                raise ValueError("Evidence stop conflicts with protection attempt")
            if (evidence.side != position.side or evidence.executed_average != position.entry_price
                    or trade is not None and trade.position_id != position_id):
                raise ValueError("Evidence prices or identity conflict with the durable position")
            entry = await db.get(PaperEffectOperation, (
                effects._account_key, position_id, "entry:" + position_id.removeprefix("paper_"),
            ))
            try:
                expected_entry = effects._cost_adjusted_entry(
                    entry, trade, side=position.side, executed_average=position.entry_price,
                )
            except Exception as exc:
                raise ValueError(
                    "Evidence prices or identity cannot be proven without persisted paper costs"
                ) from exc
            if evidence.cost_adjusted_entry != expected_entry:
                raise ValueError("Evidence prices or identity conflict with persisted execution costs")
            if evidence.calendar == "24/7" and effects.context.market != "CRIPTO":
                raise ValueError("Continuous calendar conflicts with market")
            if evidence.execution_history_complete is True:
                if entry is None or entry.result.get("revision") != attempt.revision:
                    raise ValueError("Evidence entry revision is missing or stale")
                receipt = await effects._entry_result(
                    db, position_id.removeprefix("paper_"), position_id, entry.result,
                )
                if (receipt.execution_history_complete is not True
                        or tuple(item.execution for item in evidence.executions) != receipt.executions
                        or evidence.exposure_quantity != receipt.filled
                        or evidence.exposure_quantity != Decimal(attempt.payload["quantity"])
                        or position.quantity != receipt.filled):
                    raise ValueError("Evidence execution history conflicts with durable fills")
                level_effective_at = effects._level_effective_at(authority)
                activations = tuple(
                    max(fill.executed_at, level_effective_at) if level_effective_at else fill.executed_at
                    for fill in receipt.executions
                )
                if (not activations or evidence.stop_effective_at != activations[0]
                        or any(item.effective_at != active
                               for item, active in zip(evidence.executions, activations))):
                    raise ValueError("Evidence activation conflicts with durable authority and fills")
            now = effects._evidence_clock() if effects._evidence_clock is not None else (
                await db.execute(select(func.clock_timestamp()))
            ).scalar_one()
            assessment = assess_protection_evidence(evidence, now=now, max_age=max_age)
            record = PaperProtectionEvidenceRecord(
                account_key=key[0], position_id=position_id, operation_id=operation_id,
                evidence_id=evidence_id, payload=payload,
                assessment={"state": assessment.state.value, "reason": assessment.reason,
                            "crossed_execution_ids": list(assessment.crossed_execution_ids)},
                assessed_at=now,
            )
            db.add(record)
            await db.flush()
            db.expunge(record)
            return record


PROTECTION_EVIDENCE_MAX_AGE = timedelta(minutes=5)


@dataclass(frozen=True)
class ProtectionEvidenceRequest:
    """Immutable facts handed to an external market evidence provider."""

    position_id: str
    intent_id: str
    symbol: str
    market: str
    side: OrderSide
    quantity: Decimal
    revision: int
    executed_average: Decimal
    cost_adjusted_entry: Decimal
    desired_stop: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None
    entry_price: Decimal
    max_age: timedelta
    now: datetime
    timeframe: str | None = None
    executions: tuple = ()
    levels_effective_at: datetime | None = None


def _plain(value):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _coerce_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if candidate.tzinfo is None or candidate.utcoffset() is None:
        return None
    return candidate.astimezone(timezone.utc)


def _coerce_max_age(value, default: timedelta) -> timedelta:
    if value is None:
        return default
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise ValueError("Invalid evidence freshness policy")
    if isinstance(value, (int, float, Decimal)):
        return timedelta(seconds=float(value))
    raise ValueError("Invalid evidence freshness policy")


def _provider_callable(provider):
    """Return the single supported contract: ``provider.evidence_for(request)``."""
    if provider is None:
        return None
    candidate = getattr(provider, "evidence_for", None)
    if callable(candidate):
        return candidate
    raise TypeError("Evidence provider must expose evidence_for(request)")


def _provider_values(request: ProtectionEvidenceRequest) -> dict:
    return {
        "position_id": request.position_id,
        "intent_id": request.intent_id,
        "symbol": request.symbol,
        "market": request.market,
        "side": request.side,
        "quantity": request.quantity,
        "revision": request.revision,
        "executed_average": request.executed_average,
        "entry_price": request.entry_price,
        "cost_adjusted_entry": request.cost_adjusted_entry,
        "desired_stop": request.desired_stop,
        "stop_loss": request.stop_loss,
        "stop": request.desired_stop,
        "take_profit": request.take_profit,
        "timeframe": request.timeframe,
        "max_age": request.max_age,
        "now": request.now,
        "request": request,
        "evidence_request": request,
    }


async def _invoke_provider(function, request: ProtectionEvidenceRequest):
    values = _provider_values(request)
    args: tuple = ()
    kwargs: dict = {}
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        args = (request,)
    else:
        if any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
            kwargs = values
        else:
            kwargs = {name: values[name] for name in parameters if name in values}
            missing = [
                name for name, item in parameters.items()
                if item.default is inspect.Parameter.empty
                and item.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                  inspect.Parameter.POSITIONAL_ONLY)
                and name not in kwargs
            ]
            if missing:
                args, kwargs = (request,), {}
    result = function(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _normalize_provider_result(result, default_max_age: timedelta):
    evidence_id = None
    max_age = default_max_age
    value = result
    if isinstance(value, (tuple, list)) and len(value) == 2:
        first, second = value
        if isinstance(first, str) and not isinstance(second, str):
            evidence_id, value = first, second
        elif isinstance(second, str) and not isinstance(first, str):
            evidence_id, value = second, first
        else:
            value = first
            max_age = _coerce_max_age(second, max_age)
    elif isinstance(value, (tuple, list)) and len(value) == 1:
        value = value[0]
    if isinstance(value, dict):
        if isinstance(value.get("evidence_id"), str):
            evidence_id = value["evidence_id"]
        elif isinstance(value.get("id"), str):
            evidence_id = value["id"]
        if "max_age" in value:
            max_age = _coerce_max_age(value["max_age"], max_age)
        elif "max_age_seconds" in value:
            max_age = _coerce_max_age(value["max_age_seconds"], max_age)
        if "evidence" in value:
            value = value["evidence"]
    elif not isinstance(value, ProtectionEvidence) and hasattr(value, "evidence"):
        nested = value.evidence
        if isinstance(getattr(value, "evidence_id", None), str):
            evidence_id = value.evidence_id
        max_age = _coerce_max_age(getattr(value, "max_age", None), max_age)
        value = nested
    return value, evidence_id, max_age


def _market_mapping(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    raise ProtectionEvidenceRequired("Evidence provider returned an unsupported payload")


def _build_protection_evidence(market, facts: dict) -> ProtectionEvidence:
    data = _plain(_market_mapping(market))
    timeframe = data.get("timeframe")
    level_effective_at = facts["levels_effective_at"]
    first_fill = _coerce_datetime(facts["executions"][0].executed_at)
    stop_effective_at = max(first_fill, level_effective_at) if first_fill and level_effective_at else first_fill
    if stop_effective_at is None:
        raise ProtectionEvidenceRequired("Initial protection fill activation is missing")
    executions = []
    for execution in facts["executions"]:
        executed_at = _coerce_datetime(execution.executed_at)
        if executed_at is None:
            raise ProtectionEvidenceRequired("Initial protection fill activation is missing")
        effective_at = max(executed_at, level_effective_at) if level_effective_at else executed_at
        executions.append({
            "execution": {
                "execution_id": execution.execution_id,
                "revision": execution.revision,
                "quantity": str(execution.quantity),
                "cumulative_quantity": str(execution.cumulative_quantity),
                "executed_at": execution.executed_at.isoformat(),
            },
            "effective_at": effective_at.isoformat(),
        })
    history = data.get("history", ())
    intervals = data.get("intervals", ())
    merged = {
        "side": facts["side"],
        "executed_average": str(facts["executed_average"]),
        "cost_adjusted_entry": str(facts["cost_adjusted_entry"]),
        "desired_stop": str(facts["desired_stop"]),
        "reference": data.get("reference"),
        "decision_candle": data.get("decision_candle"),
        "stop_effective_at": stop_effective_at.isoformat() if stop_effective_at else None,
        "history": list(history) if isinstance(history, (list, tuple)) else history,
        "timeframe": timeframe,
        "calendar": data.get("calendar"),
        "history_source": data.get("history_source"),
        "executions": executions,
        "execution_history_complete": facts["execution_history_complete"],
        "exposure_quantity": str(facts["exposure_quantity"]),
        "intervals": list(intervals) if isinstance(intervals, (list, tuple)) else intervals,
    }
    return protection_evidence_from_payload(merged)


def _evidence_id_for(evidence: ProtectionEvidence, max_age: timedelta) -> str:
    payload = {"evidence": _json_value(asdict(evidence)), "max_age_seconds": max_age.total_seconds()}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _evidence_facts(effects: DurablePaperEffects, position_id: str, operation_id: str) -> dict:
    try:
        async with effects._sessions() as db:
            async with db.begin():
                _, trade, authority, position = await effects._locked_protection_state(db, position_id)
                attempt = await db.get(
                    PaperProtectionAttempt, (effects._account_key, position_id, operation_id)
                )
                if attempt is None or attempt.payload.get("kind") != "protect":
                    raise ProtectionEvidenceRequired("Protection attempt not found for evidence")
                if attempt.payload.get("payload_version") != 2:
                    raise ProtectionEvidenceRequired("Protection attempt is not evidence-backed")
                level_effective_at = effects._level_effective_at(authority)
                if (attempt.payload.get("levels_version") != authority.version
                        or attempt.payload.get("levels_effective_at") != (
                            level_effective_at.isoformat() if level_effective_at else None
                        )):
                    raise ProtectionEvidenceRequired(
                        "Protection levels or exposure is stale: authority version or activation changed"
                    )
                entry = await db.get(PaperEffectOperation, (
                    effects._account_key, position_id,
                    "entry:" + position_id.removeprefix("paper_"),
                ))
                if entry is None or entry.payload.get("kind") != "entry":
                    raise ProtectionEvidenceRequired("Durable entry receipt is required for evidence")
                receipt = await effects._entry_result(
                    db, position_id.removeprefix("paper_"), position_id, entry.result,
                )
                if receipt.execution_history_complete is not True or not receipt.executions:
                    raise ProtectionEvidenceRequired("Durable fill history is incomplete")
                cost_adjusted_entry = effects._cost_adjusted_entry(
                    entry, trade, side=position.side, executed_average=position.entry_price,
                )
                return {
                    "intent_id": position_id.removeprefix("paper_"),
                    "symbol": effects.context.instrument.symbol,
                    "market": effects.context.market,
                    "side": position.side,
                    "quantity": Decimal(attempt.payload["quantity"]),
                    "revision": attempt.revision,
                    "executed_average": position.entry_price,
                    "cost_adjusted_entry": cost_adjusted_entry,
                    "desired_stop": Decimal(attempt.payload["stop_loss"]),
                    "take_profit": (
                        Decimal(attempt.payload["take_profit"])
                        if attempt.payload.get("take_profit") else None
                    ),
                    "executions": receipt.executions,
                    "execution_history_complete": receipt.execution_history_complete,
                    "exposure_quantity": receipt.filled,
                    "levels_effective_at": level_effective_at,
                }
    except ProtectionEvidenceRequired:
        raise
    except Exception as exc:
        raise ProtectionEvidenceRequired(
            "Protection evidence requires a consistent durable position and entry"
        ) from exc


async def collect_protection_evidence(
    effects: DurablePaperEffects, position_id: str, operation_id: str, *,
    provider=None, max_age: timedelta = PROTECTION_EVIDENCE_MAX_AGE,
    now: datetime | None = None,
) -> tuple[ProtectionEvidence, str, timedelta]:
    """Collect and normalize market evidence without applying any protection."""
    provider = provider if provider is not None else effects.evidence_provider
    function = _provider_callable(provider)
    if function is None:
        raise ProtectionEvidenceRequired("No market evidence provider configured")
    facts = await _evidence_facts(effects, position_id, operation_id)
    request = ProtectionEvidenceRequest(
        position_id=position_id,
        intent_id=facts["intent_id"],
        symbol=facts["symbol"],
        market=facts["market"],
        side=OrderSide(facts["side"]),
        quantity=facts["quantity"],
        revision=facts["revision"],
        executed_average=facts["executed_average"],
        cost_adjusted_entry=facts["cost_adjusted_entry"],
        desired_stop=facts["desired_stop"],
        stop_loss=facts["desired_stop"],
        take_profit=facts["take_profit"],
        entry_price=facts["executed_average"],
        max_age=max_age,
        now=now or (effects._evidence_clock() if effects._evidence_clock is not None
                    else datetime.now(timezone.utc)),
        executions=facts["executions"],
        levels_effective_at=facts["levels_effective_at"],
    )
    result = await _invoke_provider(function, request)
    value, evidence_id, actual_max_age = _normalize_provider_result(result, max_age)
    if value is None:
        raise ProtectionEvidenceRequired("Evidence provider returned no market evidence")
    evidence = _build_protection_evidence(value, facts)
    return evidence, evidence_id or _evidence_id_for(evidence, actual_max_age), actual_max_age


async def provide_and_record_protection_evidence(
    effects: DurablePaperEffects, position_id: str, operation_id: str, *,
    provider=None, max_age: timedelta = PROTECTION_EVIDENCE_MAX_AGE,
    now: datetime | None = None,
) -> tuple[str, PaperProtectionEvidenceRecord]:
    """Collect market evidence and persist it for one prepared attempt."""
    evidence, evidence_id, actual_max_age = await collect_protection_evidence(
        effects, position_id, operation_id, provider=provider, max_age=max_age, now=now,
    )
    record = await record_protection_evidence(
        effects, position_id, operation_id, evidence_id, evidence, max_age=actual_max_age,
    )
    return evidence_id, record
