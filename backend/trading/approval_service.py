"""Fila de aprovacoes manuais de entrada usando Redis."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis

from config import settings
from trading.safety import ExposureBlocked, require_entry_mode


class EntryApprovalService:
    _CLAIM_SCRIPT = """
    local raw = redis.call('GET', KEYS[1])
    if not raw then return false end
    local item = cjson.decode(raw)
    if item.status ~= 'approved' then return false end
    if item.expires_at and item.expires_at <= ARGV[2] then return false end
    item.status = ARGV[1]
    item.updated_at = ARGV[2]
    local ttl = redis.call('TTL', KEYS[1])
    if ttl <= 0 then return false end
    redis.call('SETEX', KEYS[1], ttl, cjson.encode(item))
    return cjson.encode(item)
    """
    def __init__(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._redis.aclose()

    async def create_pending_entry(
        self,
        *,
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
        dedupe_key = f"approval:entry:dedupe:{market}:{symbol}:{side}:{mode}"
        if await self._redis.exists(dedupe_key):
            return None

        approval_id = str(uuid4())
        now = datetime.now(timezone.utc)
        payload = {
            "id": approval_id,
            "status": "pending",
            "market": market,
            "symbol": symbol,
            "side": side,
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
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        }

        key = self._entry_key(approval_id)
        await self._redis.setex(key, ttl_seconds, json.dumps(payload, ensure_ascii=True))
        if chart:
            await self._redis.setex(
                self._chart_key(approval_id),
                ttl_seconds,
                json.dumps(chart, ensure_ascii=True),
            )
        await self._redis.setex(dedupe_key, max(120, ttl_seconds // 2), approval_id)
        return payload

    async def get_entry_chart(self, approval_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(self._chart_key(approval_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def set_entry_quantity(self, approval_id: str, quantity: Decimal) -> dict[str, Any] | None:
        key = self._entry_key(approval_id)
        ttl = await self._redis.ttl(key)
        if ttl is None or ttl <= 0:
            return None

        data = await self.get_entry(approval_id)
        if not data:
            return None

        data["quantity"] = str(quantity)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self._redis.setex(key, ttl, json.dumps(data, ensure_ascii=True))
        return data

    async def list_entries(self, status: str | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        async for key in self._redis.scan_iter(match="approval:entry:*", count=100):
            if ":dedupe:" in key or ":chart:" in key:
                continue
            raw = await self._redis.get(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if status and data.get("status") != status:
                continue
            items.append(data)

        items.sort(key=lambda it: it.get("created_at", ""), reverse=True)
        return items

    async def get_entry(self, approval_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(self._entry_key(approval_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    async def update_entry_status(self, approval_id: str, new_status: str) -> dict[str, Any] | None:
        key = self._entry_key(approval_id)
        ttl = await self._redis.ttl(key)
        if ttl is None or ttl <= 0:
            return None

        data = await self.get_entry(approval_id)
        if not data:
            return None

        if new_status in {"approved", "executing"}:
            require_entry_mode(data.get("mode"))
        if new_status == "executing":
            raw = await self._redis.eval(
                self._CLAIM_SCRIPT, 1, key, "executing", datetime.now(timezone.utc).isoformat()
            )
            return json.loads(raw) if raw else None
        data["status"] = new_status
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        await self._redis.setex(key, ttl, json.dumps(data, ensure_ascii=True))
        return data

    async def pop_approved_entries(self) -> list[dict[str, Any]]:
        approved = await self.list_entries(status="approved")
        result: list[dict[str, Any]] = []
        for item in approved:
            item_id = item.get("id")
            if not item_id:
                continue
            try:
                updated = await self.update_entry_status(item_id, "executing")
            except ExposureBlocked as exc:
                await self.mark_entry_failed(item_id, str(exc))
                continue
            if updated:
                result.append(updated)
        return result

    async def mark_entry_executed(self, approval_id: str) -> dict[str, Any] | None:
        return await self.update_entry_status(approval_id, "executed")

    async def mark_entry_failed(self, approval_id: str, reason: str) -> dict[str, Any] | None:
        data = await self.update_entry_status(approval_id, "failed")
        if data is None:
            return None
        data["error"] = reason[:250]
        key = self._entry_key(approval_id)
        ttl = await self._redis.ttl(key)
        if ttl and ttl > 0:
            await self._redis.setex(key, ttl, json.dumps(data, ensure_ascii=True))
        return data

    @staticmethod
    def _entry_key(approval_id: str) -> str:
        return f"approval:entry:{approval_id}"

    @staticmethod
    def _chart_key(approval_id: str) -> str:
        return f"approval:entry:chart:{approval_id}"
