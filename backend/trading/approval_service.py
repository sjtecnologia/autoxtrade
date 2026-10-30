"""Fila de aprovacoes manuais de entrada usando Redis."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis

from config import settings
from trading.safety import ExposureBlocked, require_entry_mode, require_paper_new_exposure


class EntryApprovalService:
    _LOCK_TTL_MS = 5000
    _ALLOWED_TRANSITIONS = {
        "pending": {"approved", "rejected"},
        "approved": {"executing", "failed", "rejected"},
        "executing": {"executed", "failed"},
    }

    def __init__(self) -> None:
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)

    async def close(self) -> None:
        await self._redis.aclose()

    @asynccontextmanager
    async def _lock(self, approval_id: str):
        """Serializa transições por aprovação; sem lock não há escrita."""
        lock_key = f"approval:entry:lock:{approval_id}"
        token = uuid4().hex
        acquired = await self._redis.set(
            lock_key, token, nx=True, px=self._LOCK_TTL_MS
        )
        try:
            yield bool(acquired)
        finally:
            if acquired:
                try:
                    await self._redis.eval(
                        "if redis.call('get', KEYS[1]) == ARGV[1] "
                        "then return redis.call('del', KEYS[1]) end",
                        1,
                        lock_key,
                        token,
                    )
                except Exception:
                    pass

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
        if mode == "paper":
            require_paper_new_exposure(symbol)
        else:
            require_entry_mode(mode)
        dedupe_key = f"approval:entry:dedupe:{market}:{symbol}:{side}:{mode}"
        approval_id = str(uuid4())
        # SET NX é atômico: dois scanners não podem criar a mesma entrada.
        claimed = await self._redis.set(
            dedupe_key, approval_id, ex=max(120, ttl_seconds), nx=True
        )
        if not claimed:
            return None

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
        async with self._lock(approval_id) as locked:
            if not locked:
                return None
            key = self._entry_key(approval_id)
            ttl = await self._redis.ttl(key)
            if ttl is None or ttl <= 0:
                return None

            data = await self.get_entry(approval_id)
            if not data or data.get("status") != "pending":
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

    async def update_entry_status(
        self,
        approval_id: str,
        new_status: str,
        *,
        expected_status: str | None = None,
    ) -> dict[str, Any] | None:
        async with self._lock(approval_id) as locked:
            if not locked:
                return None
            key = self._entry_key(approval_id)
            ttl = await self._redis.ttl(key)
            if ttl is None or ttl <= 0:
                return None

            data = await self.get_entry(approval_id)
            if not data:
                return None
            current = data.get("status")
            if expected_status is not None and current != expected_status:
                # Outro worker (ou o usuário) já mudou o estado: não sobrescrever.
                return None

            # Segurança antes de qualquer transição: modo real continua bloqueado.
            if new_status in {"approved", "executing"}:
                if data.get("mode") == "paper":
                    require_paper_new_exposure(str(data.get("symbol") or ""))
                else:
                    require_entry_mode(data.get("mode"))
            allowed = self._ALLOWED_TRANSITIONS.get(current or "")
            if allowed is not None and new_status not in allowed:
                return None
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
                # Claim atômico: só um worker consegue levar "approved" -> "executing".
                updated = await self.update_entry_status(
                    item_id, "executing", expected_status="approved"
                )
            except ExposureBlocked as exc:
                await self.mark_entry_failed(item_id, str(exc))
                continue
            if updated:
                result.append(updated)
        return result

    async def mark_entry_executed(self, approval_id: str) -> dict[str, Any] | None:
        return await self.update_entry_status(
            approval_id, "executed", expected_status="executing"
        )

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
