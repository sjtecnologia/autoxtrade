"""Persisted manual-entry approval lifecycle with Redis used only as a cache."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from config import settings
from database import AsyncSessionLocal
from db.models import EntryApproval, ExecutionIntent
from trading.safety import require_entry_mode, require_paper_new_exposure

logger = logging.getLogger(__name__)
_DEFAULT_REDIS = object()


class EntryApprovalService:
    _CLAIM_LEASE = timedelta(minutes=5)
    _ACTIVE_STATES = ("pending", "approved", "executing")
    _ALLOWED_TRANSITIONS = {
        "pending": {"approved", "rejected", "expired", "failed"},
        "approved": {"executing", "rejected", "expired", "failed"},
        "executing": {"executed", "failed", "expired"},
    }

    def __init__(
        self,
        session_factory: async_sessionmaker | None = None,
        redis_client: Any = _DEFAULT_REDIS,
    ) -> None:
        self._sessions = session_factory or AsyncSessionLocal
        self._redis = (
            redis_client
            if redis_client is not _DEFAULT_REDIS
            else aioredis.from_url(settings.redis_url, decode_responses=True)
        )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def create_pending_entry(
        self,
        *,
        intent_id: str,
        market: str,
        symbol: str,
        side: str,
        mode: str,
        entry_price: Decimal,
        stop_loss: Decimal,
        take_profit: Decimal,
        quantity: Decimal,
        criteria: dict[str, bool],
        details: dict[str, str],
        ttl_seconds: int,
        timeframe: str = "",
        gain_safe: Decimal | None = None,
        loss_safe: Decimal | None = None,
        risk_per_unit: Decimal | None = None,
        risk_reward: Decimal | None = None,
        risk_amount: Decimal | None = None,
        potential_gain: Decimal | None = None,
        analysis: list[str] | None = None,
        chart: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if mode == "paper":
            require_paper_new_exposure(symbol)
        else:
            require_entry_mode(mode)
        if ttl_seconds <= 0 or quantity <= 0:
            raise ValueError("Approval TTL and quantity must be positive")

        now = datetime.now(timezone.utc)
        approval_id = str(uuid4())
        dedupe_key = self._dedupe_key(market, symbol, side, mode)
        expires_at = now + timedelta(seconds=ttl_seconds)
        payload = {
            "id": approval_id,
            "intent_id": intent_id,
            "status": "pending",
            "market": market,
            "symbol": symbol,
            "side": str(getattr(side, "value", side)),
            "mode": mode,
            "timeframe": timeframe,
            "entry_price": str(entry_price),
            "stop_loss": str(stop_loss),
            "take_profit": str(take_profit),
            "gain_safe": str(gain_safe) if gain_safe is not None else None,
            "loss_safe": str(loss_safe) if loss_safe is not None else None,
            "risk_per_unit": str(risk_per_unit) if risk_per_unit is not None else None,
            "risk_reward": str(risk_reward) if risk_reward is not None else None,
            "risk_amount": str(risk_amount) if risk_amount is not None else None,
            "potential_gain": str(potential_gain) if potential_gain is not None else None,
            "quantity": str(quantity),
            "suggested_quantity": str(quantity),
            "criteria": criteria,
            "details": details,
            "analysis": analysis or [],
            "chart": chart,
        }

        async with self._sessions() as db:
            async with db.begin():
                intent = (
                    await db.execute(
                        select(ExecutionIntent)
                        .where(ExecutionIntent.intent_id == intent_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    intent is None
                    or intent.status != "reserved"
                    or (intent.market, intent.mode, intent.symbol, intent.side)
                    != (market, mode, symbol, payload["side"])
                    or intent.quantity != quantity
                    or (intent.expires_at is not None and intent.expires_at <= now)
                ):
                    raise ValueError("Approval must reference its active matching risk intent")
                if intent.expires_at is not None:
                    expires_at = min(expires_at, intent.expires_at)

                inserted_id = (
                    await db.execute(
                        pg_insert(EntryApproval)
                        .values(
                            approval_id=approval_id,
                            intent_id=intent_id,
                            market=market,
                            symbol=symbol,
                            side=payload["side"],
                            mode=mode,
                            status="pending",
                            dedupe_key=dedupe_key,
                            payload=payload,
                            created_at=now,
                            updated_at=now,
                            expires_at=expires_at,
                        )
                        .on_conflict_do_nothing()
                        .returning(EntryApproval.approval_id)
                    )
                ).scalar_one_or_none()
                if inserted_id is None:
                    existing = (
                        await db.execute(
                            select(EntryApproval).where(
                                EntryApproval.intent_id == intent_id
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        result = self._as_dict(existing)
                    else:
                        return None
                else:
                    row = await db.get(EntryApproval, inserted_id)
                    if row is None:
                        raise RuntimeError("Inserted approval could not be loaded from PostgreSQL")
                    result = self._as_dict(row)

        await self._cache_entry(result)
        return result

    async def get_entry_chart(self, approval_id: str) -> dict[str, Any] | None:
        data = await self.get_entry(approval_id)
        chart = data.get("chart") if data else None
        return chart if isinstance(chart, dict) else None

    async def set_entry_quantity(
        self, approval_id: str, quantity: Decimal
    ) -> dict[str, Any] | None:
        if quantity <= 0:
            raise ValueError("Approval quantity must be positive")
        async with self._sessions() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(EntryApproval)
                        .where(EntryApproval.approval_id == approval_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                if await self._expire_if_due(db, row, datetime.now(timezone.utc)):
                    return None
                if row.status != "pending":
                    return None
                row.payload = {**row.payload, "quantity": str(quantity)}
                row.updated_at = datetime.now(timezone.utc)
                result = self._as_dict(row)
        await self._cache_entry(result)
        return result

    async def list_entries(self, status: str | None = None) -> list[dict[str, Any]]:
        async with self._sessions() as db:
            async with db.begin():
                now = datetime.now(timezone.utc)
                expired = (
                    await db.execute(
                        select(EntryApproval)
                        .where(
                            EntryApproval.status.in_(("pending", "approved")),
                            EntryApproval.expires_at <= now,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()
                for row in expired:
                    await self._expire_if_due(db, row, now)
                query = select(EntryApproval).order_by(EntryApproval.created_at.desc())
                if status:
                    query = query.where(EntryApproval.status == status)
                rows = (await db.execute(query)).scalars().all()
                return [self._as_dict(row) for row in rows]

    async def get_entry(self, approval_id: str) -> dict[str, Any] | None:
        async with self._sessions() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(EntryApproval)
                        .where(EntryApproval.approval_id == approval_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None
                await self._expire_if_due(db, row, datetime.now(timezone.utc))
                result = self._as_dict(row)
        await self._cache_entry(result)
        return result

    async def update_entry_status(
        self,
        approval_id: str,
        new_status: str,
        *,
        expected_status: str | None = None,
        claim_token: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._sessions() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(EntryApproval)
                        .where(EntryApproval.approval_id == approval_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if row is None:
                    return None

                now = datetime.now(timezone.utc)
                if await self._expire_if_due(db, row, now):
                    return None
                current = row.status
                if expected_status is not None and current != expected_status:
                    return None
                if claim_token is not None and row.claim_token != claim_token:
                    return None
                if new_status not in self._ALLOWED_TRANSITIONS.get(current, set()):
                    return None
                if new_status == "approved" and row.expires_at <= now:
                    await self._expire_if_due(db, row, now)
                    return None

                if new_status in {"approved", "executing"}:
                    if row.mode == "paper":
                        require_paper_new_exposure(row.symbol)
                    else:
                        require_entry_mode(row.mode)

                if new_status == "executed":
                    intent = (
                        await db.execute(
                            select(ExecutionIntent)
                            .where(ExecutionIntent.intent_id == row.intent_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if intent is None or intent.status not in {"confirmed", "partial"}:
                        return None

                if new_status in {"rejected", "expired", "failed"}:
                    intent = (
                        await db.execute(
                            select(ExecutionIntent)
                            .where(ExecutionIntent.intent_id == row.intent_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if intent is not None and intent.status == "reserved":
                        intent.status = "failed"
                        intent.result_state = new_status
                        intent.updated_at = now
                    elif intent is not None and intent.status not in {"failed"}:
                        return None

                row.status = new_status
                row.updated_at = now
                if new_status not in self._ACTIVE_STATES:
                    row.dedupe_key = None
                    row.claim_token = None
                    row.lease_until = None
                result = self._as_dict(row)
        await self._cache_entry(result)
        return result

    async def pop_approved_entries(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1:
            return []
        async with self._sessions() as db:
            async with db.begin():
                now = datetime.now(timezone.utc)
                expired = (
                    await db.execute(
                        select(EntryApproval)
                        .where(
                            EntryApproval.status.in_(("pending", "approved")),
                            EntryApproval.expires_at <= now,
                        )
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()
                for row in expired:
                    await self._expire_if_due(db, row, now)

                rows = (
                    await db.execute(
                        select(EntryApproval)
                        .where(
                            or_(
                                and_(
                                    EntryApproval.status == "approved",
                                    EntryApproval.expires_at > now,
                                ),
                                and_(
                                    EntryApproval.status == "executing",
                                    or_(
                                        EntryApproval.lease_until.is_(None),
                                        EntryApproval.lease_until <= now,
                                    ),
                                ),
                            )
                        )
                        .order_by(EntryApproval.created_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()
                claimed: list[dict[str, Any]] = []
                for row in rows:
                    recovered = row.status == "executing"
                    row.status = "executing"
                    row.claim_token = uuid4().hex
                    row.lease_until = now + self._CLAIM_LEASE
                    row.updated_at = now
                    item = self._as_dict(row)
                    item["_recovered_executing"] = recovered
                    item["_claim_token"] = row.claim_token
                    claimed.append(item)
        for item in claimed:
            await self._cache_entry(item)
        return claimed

    async def renew_claim(self, approval_id: str, claim_token: str) -> bool:
        async with self._sessions() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(EntryApproval)
                        .where(EntryApproval.approval_id == approval_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    row is None
                    or row.status != "executing"
                    or row.claim_token != claim_token
                ):
                    return False
                row.lease_until = datetime.now(timezone.utc) + self._CLAIM_LEASE
                row.updated_at = datetime.now(timezone.utc)
                return True

    async def expire_unstarted_claim(
        self, approval_id: str, claim_token: str
    ) -> bool:
        """Expire a claimed approval only when no order send has begun."""
        async with self._sessions() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(EntryApproval)
                        .where(EntryApproval.approval_id == approval_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    row is None
                    or row.status != "executing"
                    or row.claim_token != claim_token
                    or row.expires_at > datetime.now(timezone.utc)
                ):
                    return False
                intent = (
                    await db.execute(
                        select(ExecutionIntent)
                        .where(ExecutionIntent.intent_id == row.intent_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if intent is None or intent.status != "reserved":
                    return False
                now = datetime.now(timezone.utc)
                intent.status = "failed"
                intent.result_state = "expired"
                intent.updated_at = now
                row.status = "expired"
                row.error = None
                row.dedupe_key = None
                row.claim_token = None
                row.lease_until = None
                row.updated_at = now
                result = self._as_dict(row)
        await self._cache_entry(result)
        return True

    async def reset_after_confirmed_not_found(
        self, approval_id: str, claim_token: str
    ) -> bool:
        """Reopen only a reserved/planned intent after an authoritative miss."""
        async with self._sessions() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(EntryApproval)
                        .where(EntryApproval.approval_id == approval_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    row is None
                    or row.status != "executing"
                    or row.claim_token != claim_token
                ):
                    return False
                now = datetime.now(timezone.utc)
                intent = (
                    await db.execute(
                        select(ExecutionIntent)
                        .where(ExecutionIntent.intent_id == row.intent_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if intent is None or intent.status not in {"reserved", "planned"}:
                    return False
                if row.expires_at <= now:
                    intent.status = "failed"
                    intent.result_state = "expired"
                    intent.updated_at = now
                    row.status = "expired"
                    row.dedupe_key = None
                    row.claim_token = None
                    row.lease_until = None
                    row.updated_at = now
                    result = self._as_dict(row)
                    expired = True
                else:
                    if intent.status == "planned":
                        intent.status = "reserved"
                        intent.result_state = None
                        intent.order_id = None
                        intent.position_id = None
                        intent.filled_quantity = None
                        intent.updated_at = now
                    return True
        if expired:
            await self._cache_entry(result)
            return False
        return True

    async def record_observed_execution(
        self,
        approval_id: str,
        claim_token: str,
        *,
        state: str,
        order_id: str | None,
        position_id: str | None,
        filled_quantity: Decimal,
    ) -> str | None:
        """Persist a broker observation before handing it to the executor."""
        async with self._sessions() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(EntryApproval)
                        .where(EntryApproval.approval_id == approval_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    row is None
                    or row.status != "executing"
                    or row.claim_token != claim_token
                ):
                    return None
                intent = (
                    await db.execute(
                        select(ExecutionIntent)
                        .where(ExecutionIntent.intent_id == row.intent_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if intent is None:
                    return None
                if intent.status == "reserved":
                    intent.status = "failed" if state == "rejected" else "planned"
                    intent.result_state = state
                    intent.order_id = order_id
                    intent.position_id = position_id
                    intent.filled_quantity = filled_quantity or None
                    intent.updated_at = datetime.now(timezone.utc)
                return intent.status

    async def mark_entry_executed(
        self, approval_id: str, claim_token: str | None = None
    ) -> dict[str, Any] | None:
        return await self.update_entry_status(
            approval_id,
            "executed",
            expected_status="executing",
            claim_token=claim_token,
        )

    async def mark_entry_failed(
        self,
        approval_id: str,
        reason: str,
        claim_token: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._sessions() as db:
            async with db.begin():
                row = (
                    await db.execute(
                        select(EntryApproval)
                        .where(EntryApproval.approval_id == approval_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    row is not None
                    and row.status == "executing"
                    and (claim_token is None or row.claim_token == claim_token)
                ):
                    intent = (
                        await db.execute(
                            select(ExecutionIntent)
                            .where(ExecutionIntent.intent_id == row.intent_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    if intent is not None and intent.status in {"reserved", "failed"}:
                        if intent.status == "reserved":
                            intent.status = "failed"
                            intent.result_state = "failed"
                            intent.updated_at = datetime.now(timezone.utc)
                        row.status = "failed"
                        row.dedupe_key = None
                        row.claim_token = None
                        row.lease_until = None
                    row.error = reason[:250]
                    row.updated_at = datetime.now(timezone.utc)
                    if row.status == "failed":
                        result = self._as_dict(row)
                    else:
                        result = None
                else:
                    result = None
        if result is not None:
            await self._cache_entry(result)
        return result

    async def _expire_if_due(self, db: Any, row: EntryApproval, now: datetime) -> bool:
        if row.status not in {"pending", "approved"} or row.expires_at > now:
            return False
        intent = (
            await db.execute(
                select(ExecutionIntent)
                .where(ExecutionIntent.intent_id == row.intent_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if intent is not None and intent.status in {"planned", "unknown", "partial", "confirmed"}:
            row.status = "executing"
            row.claim_token = None
            row.lease_until = None
            row.updated_at = now
            return False
        if intent is not None and intent.status == "reserved":
            intent.status = "failed"
            intent.result_state = "expired"
            intent.updated_at = now
        row.status = "expired"
        row.dedupe_key = None
        row.claim_token = None
        row.lease_until = None
        row.updated_at = now
        return True

    async def _cache_entry(self, data: dict[str, Any]) -> None:
        if self._redis is None:
            return
        expires_at = datetime.fromisoformat(data["expires_at"])
        ttl = max(60, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
        if data.get("status") not in self._ACTIVE_STATES:
            ttl = 86400
        try:
            cache_value = {key: value for key, value in data.items() if not key.startswith("_")}
            await self._redis.setex(
                self._entry_key(data["id"]),
                ttl,
                json.dumps(cache_value, ensure_ascii=True),
            )
        except Exception:
            logger.warning("Approval cache write failed for %s", data.get("id"), exc_info=True)

    @staticmethod
    def _as_dict(row: EntryApproval) -> dict[str, Any]:
        data = dict(row.payload)
        data.update(
            {
                "id": row.approval_id,
                "intent_id": row.intent_id,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
                "expires_at": row.expires_at.isoformat(),
                "error": row.error,
            }
        )
        return data

    @staticmethod
    def _dedupe_key(market: str, symbol: str, side: str, mode: str) -> str:
        value = ":".join(
            (market.strip().upper(), symbol.strip().upper(), side.strip().lower(), mode.strip().lower())
        )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _entry_key(approval_id: str) -> str:
        return f"approval:entry:{approval_id}"

    @staticmethod
    def _chart_key(approval_id: str) -> str:
        return f"approval:entry:chart:{approval_id}"
