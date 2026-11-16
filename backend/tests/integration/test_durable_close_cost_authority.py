import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from config import settings
from db.models import ExecutionIntent, Trade
from paper.durable_effects import PaperEffectOperation, PaperEffectPosition, PaperProtectionAttempt
from tests.integration.test_approval_lifecycle import approval_database
from tests.integration.test_paper_durable_effects import effect_sessions
from tests.integration.test_manual_paper_close import opened, close, expire_close_claim
from trading.order_executor import OrderExecutionError


async def change_costs(sessions, trade_id, key, value):
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        original = dict(trade.execution_context["paper_costs"])
        trade.execution_context = {**trade.execution_context, "paper_costs": {**original, key: value}}
        await db.commit()
        return original


async def restore_costs(sessions, trade_id, costs):
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.execution_context = {**trade.execution_context, "paper_costs": costs}
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [("commission_rate", "0.5"), ("spread_price", "1"), ("slippage_price", "1")])
async def test_changed_trade_costs_before_decision_are_rejected(effect_sessions, key, value):
    connector, intent_id, trade_id = await opened(effect_sessions)
    original = await change_costs(effect_sessions, trade_id, key, value)
    with pytest.raises(OrderExecutionError, match="pending"):
        await close(effect_sessions, connector, trade_id)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        intent = (await db.execute(select(ExecutionIntent))).scalar_one()
        assert trade.status == "open" and trade.pnl_net is None
        assert (await db.execute(select(PaperEffectPosition))).scalar_one().quantity == 1
        assert intent.risk_amount == 20 and intent.capital_amount == 500
        assert not (await db.execute(select(PaperProtectionAttempt).where(
            PaperProtectionAttempt.operation_id.like("manual-close:%")
        ))).scalars().all()
    await restore_costs(effect_sessions, trade_id, original)
    trade = await close(effect_sessions, connector, trade_id)
    assert trade.commission == Decimal("0.20") and trade.pnl_net == Decimal("4.79")


@pytest.mark.asyncio
async def test_changed_configuration_does_not_change_captured_close_costs(effect_sessions, monkeypatch):
    connector, _, trade_id = await opened(effect_sessions)
    costs = json.loads(settings.paper_costs_json)
    costs["BTC/USDT"].update(commission_rate="0.5", spread_price="1", slippage_price="1")
    monkeypatch.setattr(settings, "paper_costs_json", json.dumps(costs))
    trade = await close(effect_sessions, connector, trade_id)
    assert trade.commission == Decimal("0.20") and trade.pnl_net == Decimal("4.79")


@pytest.mark.asyncio
async def test_cost_divergence_between_decision_and_effect_is_rejected(effect_sessions):
    connector, _, trade_id = await opened(effect_sessions)
    effects = connector._transport.durable_effects
    position_id = (await connector.list_positions())[0].id
    operation_id, fence, _ = await effects.prepare_manual_close(
        position_id, trade_id=trade_id, quantity=Decimal("1"), reason="manual",
        execution_price=Decimal("105"), owner="cost-test",
    )
    original = await change_costs(effect_sessions, trade_id, "commission_rate", "0.5")
    with pytest.raises(ValueError, match="stale"):
        await connector.reduce_position(position_id, Decimal("1"), intent_id=operation_id,
                                        fence=fence, owner="cost-test", execution_price=Decimal("105"))
    async with effect_sessions() as db:
        assert (await db.execute(select(PaperEffectPosition))).scalar_one().quantity == 1
        assert not (await db.execute(select(PaperEffectOperation).where(
            PaperEffectOperation.operation_id == operation_id
        ))).scalars().all()
    await restore_costs(effect_sessions, trade_id, original)
    await expire_close_claim(effect_sessions)
    trade = await close(effect_sessions, connector, trade_id)
    assert trade.commission == Decimal("0.20")


@pytest.mark.asyncio
async def test_cost_divergence_after_effect_prevents_wrong_accounting(effect_sessions, monkeypatch):
    connector, _, trade_id = await opened(effect_sessions)
    reduce = connector.reduce_position
    original_costs = None
    async with effect_sessions() as db:
        initial_commission = (await db.get(Trade, trade_id)).commission
    async def corrupt_after_effect(*args, **kwargs):
        nonlocal original_costs
        result = await reduce(*args, **kwargs)
        original_costs = await change_costs(effect_sessions, trade_id, "commission_rate", "0.5")
        return result
    monkeypatch.setattr(connector, "reduce_position", corrupt_after_effect)
    with pytest.raises(OrderExecutionError, match="pending"):
        await close(effect_sessions, connector, trade_id)
    async with effect_sessions() as db:
        trade = await db.get(Trade, trade_id)
        assert trade.status == "open" and trade.commission == initial_commission and trade.pnl_net is None
        assert (await db.execute(select(PaperEffectPosition))).scalar_one().quantity == 0
    await restore_costs(effect_sessions, trade_id, original_costs)
    await expire_close_claim(effect_sessions)
    async def no_reapply(*args, **kwargs):
        raise AssertionError("Durable close receipt must be reused")
    monkeypatch.setattr(connector, "reduce_position", no_reapply)
    trade = await close(effect_sessions, connector, trade_id)
    assert trade.commission == Decimal("0.20") and trade.pnl_net == Decimal("4.79")
