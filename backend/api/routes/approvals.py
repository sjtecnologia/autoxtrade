"""Endpoints para aprovacoes manuais de entrada."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth import verify_token
from trading.approval_service import EntryApprovalService
from trading.safety import ExposureBlocked

router = APIRouter(prefix="/approvals", tags=["approvals"])


class EntryApprovalResponse(BaseModel):
    id: str
    status: str
    market: str
    symbol: str
    side: str
    mode: str
    timeframe: str = ""
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    gain_safe: Decimal | None = None
    loss_safe: Decimal | None = None
    risk_per_unit: Decimal | None = None
    risk_reward: Decimal | None = None
    risk_amount: Decimal | None = None
    potential_gain: Decimal | None = None
    quantity: Decimal
    suggested_quantity: Decimal | None = None
    criteria: dict[str, bool]
    details: dict[str, str]
    analysis: list[str] = []
    created_at: datetime
    expires_at: datetime
    updated_at: datetime | None = None
    error: str | None = None


class EntryApprovalDecisionResponse(BaseModel):
    id: str
    status: str


class EntryApprovalRequest(BaseModel):
    quantity: Decimal | None = Field(default=None, gt=0, description="Sobrescreve a quantidade sugerida")


class EntryChartPoint(BaseModel):
    time: int
    value: float


class EntryChartCandle(BaseModel):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class EntryChartResponse(BaseModel):
    candles: list[EntryChartCandle]
    overlays: dict[str, list[EntryChartPoint]]


def _opt_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _normalize(item: dict[str, Any]) -> EntryApprovalResponse:
    return EntryApprovalResponse(
        id=item["id"],
        status=item["status"],
        market=item["market"],
        symbol=item["symbol"],
        side=item["side"],
        mode=item["mode"],
        timeframe=item.get("timeframe") or "",
        entry_price=Decimal(str(item["entry_price"])),
        stop_loss=Decimal(str(item["stop_loss"])),
        take_profit=Decimal(str(item["take_profit"])),
        gain_safe=_opt_decimal(item.get("gain_safe")),
        loss_safe=_opt_decimal(item.get("loss_safe")),
        risk_per_unit=_opt_decimal(item.get("risk_per_unit")),
        risk_reward=_opt_decimal(item.get("risk_reward")),
        risk_amount=_opt_decimal(item.get("risk_amount")),
        potential_gain=_opt_decimal(item.get("potential_gain")),
        quantity=Decimal(str(item["quantity"])),
        suggested_quantity=_opt_decimal(item.get("suggested_quantity")),
        criteria=item.get("criteria", {}),
        details=item.get("details", {}),
        analysis=item.get("analysis", []),
        created_at=datetime.fromisoformat(item["created_at"]),
        expires_at=datetime.fromisoformat(item["expires_at"]),
        updated_at=datetime.fromisoformat(item["updated_at"]) if item.get("updated_at") else None,
        error=item.get("error"),
    )


@router.get("/entries", response_model=list[EntryApprovalResponse])
async def list_entry_approvals(
    status: str | None = Query(default=None, description="pending|approved|rejected|executed|failed"),
    _: str = Depends(verify_token),
) -> list[EntryApprovalResponse]:
    svc = EntryApprovalService()
    try:
        rows = await svc.list_entries(status=status)
    finally:
        await svc.close()
    return [_normalize(r) for r in rows]


@router.get("/entries/{approval_id}", response_model=EntryApprovalResponse)
async def get_entry_approval(
    approval_id: str,
    _: str = Depends(verify_token),
) -> EntryApprovalResponse:
    svc = EntryApprovalService()
    try:
        data = await svc.get_entry(approval_id)
    finally:
        await svc.close()

    if data is None:
        raise HTTPException(status_code=404, detail="Aprovacao nao encontrada ou expirada")
    return _normalize(data)


@router.get("/entries/{approval_id}/chart", response_model=EntryChartResponse)
async def get_entry_chart(
    approval_id: str,
    _: str = Depends(verify_token),
) -> EntryChartResponse:
    svc = EntryApprovalService()
    try:
        chart = await svc.get_entry_chart(approval_id)
    finally:
        await svc.close()

    if chart is None:
        raise HTTPException(status_code=404, detail="Grafico da aprovacao nao disponivel")
    return EntryChartResponse(**chart)


@router.post("/entries/{approval_id}/approve", response_model=EntryApprovalDecisionResponse)
async def approve_entry(
    approval_id: str,
    payload: EntryApprovalRequest = Body(default_factory=EntryApprovalRequest),
    _: str = Depends(verify_token),
) -> EntryApprovalDecisionResponse:
    svc = EntryApprovalService()
    try:
        if payload.quantity is not None:
            updated = await svc.set_entry_quantity(approval_id, payload.quantity)
            if updated is None:
                raise HTTPException(status_code=404, detail="Aprovacao nao encontrada ou expirada")
        data = await svc.update_entry_status(approval_id, "approved")
    except ExposureBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        await svc.close()

    if data is None:
        raise HTTPException(status_code=404, detail="Aprovacao nao encontrada ou expirada")

    return EntryApprovalDecisionResponse(id=approval_id, status=data["status"])


@router.post("/entries/{approval_id}/reject", response_model=EntryApprovalDecisionResponse)
async def reject_entry(
    approval_id: str,
    _: str = Depends(verify_token),
) -> EntryApprovalDecisionResponse:
    svc = EntryApprovalService()
    try:
        data = await svc.update_entry_status(approval_id, "rejected")
    finally:
        await svc.close()

    if data is None:
        raise HTTPException(status_code=404, detail="Aprovacao nao encontrada ou expirada")

    return EntryApprovalDecisionResponse(id=approval_id, status=data["status"])
