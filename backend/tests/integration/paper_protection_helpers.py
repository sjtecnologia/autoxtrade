"""Shared helpers for durable paper protection evidence tests.

Valid market evidence requires the entry fill to precede an aligned reference
close, which is impossible for a fill created at "now". Tests therefore age the
durable entry fill before protection is applied. A single explicit provider
contract (``evidence_for``) is used everywhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from paper.protection_evidence_store import provide_and_record_protection_evidence


class ControlledEvidenceClock:
    """Market-evidence clock only; PostgreSQL claims still use clock_timestamp()."""

    def __init__(self):
        self.current = datetime.now(timezone.utc)

    def __call__(self):
        return self.current


class PostFillSyntheticProvider:
    """Build synthetic closed 1s candles from the real persisted fill times."""

    def __init__(self, *, reference="101", history_low="99"):
        self.reference = str(reference)
        self.history_low = str(history_low)

    async def evidence_for(self, request):
        if not request.executions:
            raise AssertionError("expected persisted executions in evidence request")
        reference = Decimal(self.reference)
        candle_open = reference - Decimal("1")
        candle_high = reference + Decimal("1")
        candle_low = min(Decimal(self.history_low), reference - Decimal("2"))
        fills = [item.executed_at.astimezone(timezone.utc) for item in request.executions]
        level_effective = request.levels_effective_at
        activations = [max(fill, level_effective) if level_effective else fill for fill in fills]
        first_minute = min(fills).replace(second=0, microsecond=0)
        start = first_minute - timedelta(minutes=2)
        period = timedelta(minutes=1)
        effective = min(activations)
        observed = max(active.replace(second=0, microsecond=0) for active in activations) + 3 * period
        self.clock.current = observed + period
        history = []
        candle_at = start + period
        while candle_at < observed:
            history.append({
                "opened_at": candle_at.isoformat(), "open": str(candle_open),
                "high": str(candle_high), "low": str(candle_low),
                "close": self.reference,
            })
            candle_at += period
        intervals = []
        for index, activation in enumerate(activations):
            active = max(activation, start + period)
            boundary = active.replace(second=0, microsecond=0) + period
            if active != active.replace(second=0, microsecond=0):
                intervals.append({
                    "execution_id": request.executions[index].execution_id,
                    "started_at": active.isoformat(), "ended_at": boundary.isoformat(),
                    "low": str(candle_low), "high": str(candle_high), "source": "synthetic-test",
                })
        return {
            "timeframe": "1m", "calendar": "24/7", "history_source": "synthetic-test",
            "reference": {"price": self.reference, "observed_at": observed.isoformat(),
                          "source": "synthetic-test"},
            "decision_candle": {
                "opened_at": start.isoformat(), "open": str(candle_open),
                "high": str(candle_high), "low": str(candle_low),
                "close": self.reference,
            },
            "history": history,
            "stop_effective_at": effective.isoformat(),
            "intervals": intervals,
        }

    def bind_clock(self, clock):
        self.clock = clock
        return self


async def apply_prepared_protection(connector, prepared, *, owner: str, provider) -> object:
    """Collect + record evidence for a prepared attempt and apply it."""
    position_id, operation_id, fence, payload = prepared
    effects = connector._transport.durable_effects
    evidence_id, _ = await provide_and_record_protection_evidence(
        effects, position_id, operation_id, provider=provider,
    )
    return await connector.modify_position_protection(
        position_id,
        stop_loss=Decimal(payload["stop_loss"]),
        take_profit=Decimal(payload["take_profit"]) if payload["take_profit"] else None,
        intent_id=operation_id, fence=fence,
        revision=payload.get("revision"),
        quantity=Decimal(payload["quantity"]) if payload.get("quantity") else None,
        owner=owner, evidence_id=evidence_id,
    )


def register_fixed_provider(monkeypatch, provider, *, name: str | None = None) -> str:
    """Select ``provider`` through the server setting used by the real task."""
    from config import settings
    from paper.protection_evidence_provider import register_protection_evidence_provider

    name = name or f"fixed-{uuid4().hex}"
    register_protection_evidence_provider(name, lambda: provider)
    monkeypatch.setattr(settings, "protection_evidence_provider", name)
    return name
