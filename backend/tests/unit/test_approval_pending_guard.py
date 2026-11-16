from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.integration.test_approval_lifecycle import _context


@pytest.mark.asyncio
@pytest.mark.parametrize("intent_status,fail_before_lookup", [
    ("partial", False),
    ("protection_pending", False),
    ("partial", True),
])
async def test_approval_task_does_not_complete_unresolved_paper_trade(
    monkeypatch, intent_status, fail_before_lookup,
):
    import sqlalchemy.ext.asyncio as sqlalchemy_async
    import trading.approval_service as approvals
    import trading.contracts as contracts
    from db.models import BotConfig, ExecutionIntent, Trade
    from tasks.trading_tasks import _process_entry_approvals_async

    context = _context("BTC/USDT")
    item = {
        "id": "approval", "intent_id": "intent", "_claim_token": "claim",
        "_recovered_executing": True, "market": "CRIPTO", "symbol": "BTC/USDT",
        "mode": "paper", "side": "buy", "quantity": "1", "entry_price": "100",
        "stop_loss": "95", "take_profit": "110",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    service = MagicMock()
    service.pop_approved_entries = AsyncMock(return_value=[item])
    service.close = AsyncMock()
    service.mark_entry_executed = AsyncMock()
    service.mark_entry_failed = AsyncMock(return_value=None)
    service.renew_claim = AsyncMock()

    session = AsyncMock()
    async def execute(statement):
        entity = statement.column_descriptions[0]["entity"]
        rows = {
            BotConfig: None if fail_before_lookup else SimpleNamespace(
                execution_context={"BTC/USDT": {}}
            ),
            ExecutionIntent: SimpleNamespace(status=intent_status),
            Trade: SimpleNamespace(id=1, quantity=Decimal("0.5")),
        }
        result = MagicMock()
        result.scalar_one_or_none.return_value = rows[entity]
        return result
    session.execute.side_effect = execute
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    engine = MagicMock()
    engine.dispose = AsyncMock()
    monkeypatch.setattr(approvals, "EntryApprovalService", lambda: service)
    monkeypatch.setattr(contracts, "context_from_dict", lambda _: context)
    monkeypatch.setattr(sqlalchemy_async, "create_async_engine", lambda *a, **kw: engine)
    monkeypatch.setattr(sqlalchemy_async, "async_sessionmaker", lambda *a, **kw: factory)

    await _process_entry_approvals_async()

    service.mark_entry_executed.assert_not_awaited()
    service.close.assert_awaited_once()
