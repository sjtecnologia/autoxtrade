import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import make_url, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config import settings
from database import Base
from db.models import (
    BotConfig,
    EquitySnapshot,
    ExecutionIntent,
    MLModel,
    ProtectionAuthority,
    RiskAccountLock,
    Trade,
    TradeEvent,
)
from risk.correlation_checker import CorrelationCheckResult
from risk.drawdown_monitor import DrawdownMonitor
from risk.manager import RiskManager, release_confirmed_reservation
from risk.position_sizer import PositionSizer
from trading.contracts import (
    Capability,
    ExecutionContext,
    ExecutionMode,
    Instrument,
)
from trading.safety import AccountNature


class _NoCorrelation:
    async def check(self, **kwargs):
        return CorrelationCheckResult(blocked=False)


@pytest_asyncio.fixture
async def risk_budget_database(monkeypatch):
    database_url = make_url(settings.database_url)
    endpoint = f"{database_url.host}:{database_url.port or 5432}"
    if endpoint not in os.environ.get("AUTOXTRADE_TEST_ENDPOINTS", "").split(","):
        pytest.skip("isolated PostgreSQL endpoint is not explicitly enabled")
    schema = f"risk_budget_{uuid4().hex}"
    admin = create_async_engine(str(database_url))
    tenant = None
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        tenant = create_async_engine(
            str(database_url), connect_args={"server_settings": {"search_path": schema}}
        )
        async with tenant.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=[
                MLModel.__table__, BotConfig.__table__, EquitySnapshot.__table__,
                Trade.__table__, ExecutionIntent.__table__, ProtectionAuthority.__table__,
                RiskAccountLock.__table__, TradeEvent.__table__,
            ])
        sessions = async_sessionmaker(tenant, expire_on_commit=False)
        context = _context("CRIPTO", f"account-{uuid4().hex}", "TEST")
        monkeypatch.setattr(settings, "paper_initial_capital", Decimal("10000"))
        async with sessions() as db:
            config = BotConfig(
                market="CRIPTO", is_active=True, mode="paper", exchange=context.venue,
                enabled_symbols=["TEST"], execution_context={"TEST": context.snapshot()},
                risk_per_trade_pct=Decimal("1"), max_open_trades=20,
                max_drawdown_pct=Decimal("15"), drawdown_alert_pct=Decimal("10"),
                max_corr_threshold=Decimal("0.7"), drawdown_status="OK",
            )
            db.add(config)
            await db.commit()
        account_key, _ = RiskManager._account_scope(config, context)
        yield sessions, context, account_key
    finally:
        if tenant is not None:
            await tenant.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def _reserve_budget(sessions, *, requested_quantity=None):
    async with sessions() as db:
        return await RiskManager(
            db, PositionSizer(), DrawdownMonitor(db), _NoCorrelation()
        ).can_open_position(
            "CRIPTO", "TEST", Decimal("100"), Decimal("90"),
            requested_quantity=requested_quantity,
        )


def _budget_intent(account_key, *, status="planned", filled="10", terminal=None):
    return ExecutionIntent(
        intent_id=uuid4().hex, account_key=account_key, market="CRIPTO",
        mode="paper", symbol="TEST", side="buy", quantity=Decimal("10"),
        risk_amount=Decimal("100"), capital_amount=Decimal("1000"),
        status=status, filled_quantity=Decimal(filled), order_terminal=terminal,
    )


def _budget_trade(intent, context, *, quantity="5", status="open"):
    quantity = Decimal(quantity)
    return Trade(
        intent_id=intent.intent_id, market="CRIPTO", mode="paper",
        exchange=context.venue, account_id=context.account_id,
        account_nature=context.nature.value,
        instrument_class=context.instrument.asset_class,
        identity_status="bound", execution_context=context.snapshot(),
        symbol="TEST", side="buy", status=status, entry_price=Decimal("100"),
        quantity=quantity, entry_value=quantity * Decimal("100"),
        stop_loss=Decimal("90"), open_at=datetime.now(timezone.utc),
        risk_amount=quantity * Decimal("10"), capital_amount=quantity * Decimal("100"),
    )


@pytest.mark.asyncio
async def test_unidentified_legacy_trade_blocks_candidate_account(risk_budget_database):
    sessions, context, _ = risk_budget_database
    async with sessions() as db:
        trade = Trade(
            market="CRIPTO", mode="paper", exchange=context.venue,
            account_id=None, symbol="TEST", side="buy", status="open",
            entry_price=Decimal("100"), quantity=Decimal("10"),
            entry_value=Decimal("1000"), stop_loss=Decimal("100"),
            open_at=datetime.now(timezone.utc),
        )
        db.add(trade)
        db.add(TradeEvent(trade=trade, event_type="SAFE_BREAK_ARMED",
                          payload={"new_stop_loss": "100"}))
        await db.commit()
    result = await _reserve_budget(sessions)
    assert not result.approved
    assert "reconcilia" in result.reason.lower()


@pytest.mark.asyncio
async def test_unidentified_trade_blocks_both_workers_until_reconciled(risk_budget_database):
    sessions, context, _ = risk_budget_database
    async with sessions() as db:
        trade = _budget_trade(_budget_intent("unused"), context, quantity="10")
        trade.intent_id = None
        trade.identity_status = "unresolved"
        trade.account_id = None
        trade.execution_context = None
        db.add(trade)
        await db.commit()
        trade_id = trade.id
    results = await asyncio.gather(_reserve_budget(sessions), _reserve_budget(sessions))
    assert all(not result.approved and "reconcilia" in result.reason.lower() for result in results)
    async with sessions() as db:
        trade = await db.get(Trade, trade_id)
        trade.account_id = context.account_id
        trade.execution_context = context.snapshot()
        trade.identity_status = "bound"
        await db.commit()
    result = await _reserve_budget(sessions)
    assert result.approved
    assert result.position_size.position_value == Decimal("900")


@pytest.mark.asyncio
async def test_verified_other_account_does_not_block_candidate(risk_budget_database):
    sessions, context, _ = risk_budget_database
    other = replace(context, account_id="another-verified-account")
    async with sessions() as db:
        trade = _budget_trade(_budget_intent("unused"), other, quantity="10")
        trade.intent_id = None
        db.add(trade)
        await db.commit()
    result = await _reserve_budget(sessions)
    assert result.approved
    assert result.position_size.position_value == Decimal("1000")


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["unresolved", "snapshot", "currency"])
async def test_unverified_or_conflicting_identity_never_excludes_trade(risk_budget_database, conflict):
    sessions, context, _ = risk_budget_database
    other = replace(context, account_id="another-account")
    async with sessions() as db:
        trade = _budget_trade(_budget_intent("unused"), other, quantity="10")
        trade.intent_id = None
        if conflict == "unresolved":
            trade.identity_status = "unresolved"
        elif conflict == "snapshot":
            trade.account_id = context.account_id
        else:
            trade.account_id = context.account_id
            trade.execution_context = replace(context, account_currency="EUR").snapshot()
        db.add(trade)
        await db.commit()
    result = await _reserve_budget(sessions)
    assert not result.approved and "reconcilia" in result.reason.lower()


@pytest.mark.asyncio
async def test_breakeven_trade_still_uses_slot_and_capital(risk_budget_database):
    sessions, context, _ = risk_budget_database
    async with sessions() as db:
        trade = _budget_trade(_budget_intent("unused"), context, quantity="10")
        trade.intent_id = None
        trade.stop_loss = trade.entry_price
        trade.risk_amount = None
        db.add(trade)
        config = (await db.execute(select(BotConfig))).scalar_one()
        config.max_open_trades = 1
        await db.commit()
        trade_id = trade.id
    result = await _reserve_budget(sessions)
    assert not result.approved and "posições" in result.reason
    async with sessions() as db:
        config = (await db.execute(select(BotConfig))).scalar_one()
        config.max_open_trades = 20
        await db.commit()
    result = await _reserve_budget(sessions)
    assert result.approved and result.position_size.position_value == Decimal("900")
    async with sessions() as db:
        reserved = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == result.intent_id
        ))).scalar_one()
        reserved.status = "failed"
        trade = await db.get(Trade, trade_id)
        trade.capital_amount = Decimal("10000")
        await db.commit()
    result = await _reserve_budget(sessions)
    assert not result.approved and "orçamento" in result.reason.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["risk", "capital", "zero_risk"])
async def test_insufficient_trade_budget_blocks_entry(risk_budget_database, missing):
    sessions, context, _ = risk_budget_database
    async with sessions() as db:
        trade = _budget_trade(_budget_intent("unused"), context, quantity="10")
        trade.intent_id = None
        if missing == "risk":
            trade.risk_amount = None
            snapshot = context.snapshot()
            snapshot["instrument"]["contract_size"] = None
            trade.execution_context = snapshot
        elif missing == "capital":
            trade.capital_amount = None
            trade.entry_value = Decimal("0")
        else:
            trade.risk_amount = Decimal("0")
        db.add(trade)
        await db.commit()
    result = await _reserve_budget(sessions)
    assert not result.approved and "reconcilia" in result.reason.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,filled,terminal", [
    ("partial", "5", False), ("partial", "5", True),
    ("planned", "10", None), ("confirmed", "10", True),
    ("failed", "5", True),
])
async def test_missing_trade_preserves_entire_budget(risk_budget_database, status, filled, terminal):
    sessions, _, account_key = risk_budget_database
    async with sessions() as db:
        db.add(_budget_intent(account_key, status=status, filled=filled, terminal=terminal))
        await db.commit()
    result = await _reserve_budget(sessions)
    assert result.approved
    assert result.position_size.position_value == Decimal("900")


@pytest.mark.asyncio
async def test_eleven_thousand_in_fills_cannot_exceed_ten_thousand(risk_budget_database):
    sessions, _, account_key = risk_budget_database
    async with sessions() as db:
        db.add_all([_budget_intent(account_key) for _ in range(10)])
        await db.commit()
    result = await _reserve_budget(sessions)
    assert not result.approved
    assert "orçamento" in result.reason.lower()


@pytest.mark.asyncio
async def test_real_reservations_cannot_repeat_thousand_after_unlinked_fill(risk_budget_database):
    sessions, _, _ = risk_budget_database
    first = await _reserve_budget(sessions, requested_quantity=Decimal("10"))
    assert first.approved and first.position_size.position_value == Decimal("1000")
    async with sessions() as db:
        intent = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == first.intent_id
        ))).scalar_one()
        intent.status = "planned"
        intent.filled_quantity = Decimal("10")
        await db.commit()
    second = await _reserve_budget(sessions, requested_quantity=Decimal("10"))
    assert not second.approved


@pytest.mark.asyncio
async def test_unlinked_fill_reserves_risk_as_well_as_capital(risk_budget_database):
    sessions, _, account_key = risk_budget_database
    intent = _budget_intent(account_key, status="confirmed", terminal=True)
    intent.risk_amount = Decimal("2000")
    async with sessions() as db:
        db.add(intent)
        await db.commit()
    result = await _reserve_budget(sessions)
    assert not result.approved
    assert "orçamento" in result.reason.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("risk,capital", [(None, Decimal("1000")), (Decimal("100"), 0)])
async def test_missing_budget_metadata_fails_closed(risk_budget_database, risk, capital):
    sessions, _, account_key = risk_budget_database
    intent = _budget_intent(account_key, status="confirmed", terminal=True)
    intent.risk_amount = risk
    intent.capital_amount = capital
    async with sessions() as db:
        db.add(intent)
        await db.commit()
    result = await _reserve_budget(sessions)
    assert not result.approved
    assert "reconciliação" in result.reason.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,filled,expected", [
    (False, "5", "900"), (True, "5", "950"),
    (True, "10", "900"), (False, "10", "900"),
])
async def test_linked_trade_charges_only_unrepresented_amount(
    risk_budget_database, terminal, filled, expected,
):
    sessions, context, account_key = risk_budget_database
    intent = _budget_intent(account_key, status="partial", filled=filled, terminal=terminal)
    async with sessions() as db:
        db.add_all([intent, _budget_trade(intent, context)])
        await db.commit()
    result = await _reserve_budget(sessions)
    assert result.approved
    assert result.position_size.position_value == Decimal(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal,filled,expected", [
    (False, "5", "950"), (True, "5", "1000"), (True, "10", "950"),
])
async def test_closed_trade_releases_only_resolved_exposure(
    risk_budget_database, terminal, filled, expected,
):
    sessions, context, account_key = risk_budget_database
    intent = _budget_intent(account_key, status="partial", filled=filled, terminal=terminal)
    async with sessions() as db:
        db.add_all([intent, _budget_trade(intent, context, status="closed")])
        await db.commit()
    result = await _reserve_budget(sessions)
    assert result.approved
    assert result.position_size.position_value == Decimal(expected)


@pytest.mark.asyncio
async def test_linking_trade_does_not_double_count_and_closed_trade_releases(
    risk_budget_database,
):
    sessions, context, account_key = risk_budget_database
    intent = _budget_intent(account_key, status="confirmed", terminal=True)
    async with sessions() as db:
        db.add(intent)
        await db.commit()
    before = await _reserve_budget(sessions)
    assert before.position_size.position_value == Decimal("900")
    async with sessions() as db:
        reserved = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == before.intent_id
        ))).scalar_one()
        reserved.status = "failed"
        db.add(_budget_trade(intent, context, quantity="10"))
        await db.commit()
    linked = await _reserve_budget(sessions)
    assert linked.position_size.position_value == Decimal("900")
    async with sessions() as db:
        reserved = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == linked.intent_id
        ))).scalar_one()
        reserved.status = "failed"
        trade = (await db.execute(select(Trade).where(Trade.intent_id == intent.intent_id))).scalar_one()
        trade.status = "closed"
        await db.commit()
    closed = await _reserve_budget(sessions)
    assert closed.position_size.position_value == Decimal("1000")


@pytest.mark.asyncio
async def test_parallel_reservations_count_missing_trade_slot(risk_budget_database):
    sessions, _, account_key = risk_budget_database
    async with sessions() as db:
        config = (await db.execute(select(BotConfig))).scalar_one()
        config.max_open_trades = 10
        db.add_all([_budget_intent(account_key, status="confirmed", terminal=True) for _ in range(9)])
        await db.commit()
    results = await asyncio.gather(_reserve_budget(sessions), _reserve_budget(sessions))
    assert sum(result.approved for result in results) == 1


@pytest.mark.asyncio
async def test_parallel_reservations_respect_budget_with_unlinked_fills(risk_budget_database):
    sessions, _, account_key = risk_budget_database
    async with sessions() as db:
        db.add_all([_budget_intent(account_key, status="confirmed", terminal=True) for _ in range(9)])
        await db.commit()
    results = await asyncio.gather(_reserve_budget(sessions), _reserve_budget(sessions))
    assert all(result.approved for result in results)
    async with sessions() as db:
        intents = (await db.execute(select(ExecutionIntent))).scalars().all()
    assert sum((intent.capital_amount for intent in intents), Decimal("0")) <= Decimal("10000")
    assert sorted(result.position_size.position_value for result in results) == [
        Decimal("90"), Decimal("100"),
    ]


@pytest.mark.asyncio
async def test_expired_reservation_with_fill_is_not_released(risk_budget_database):
    sessions, _, account_key = risk_budget_database
    intent = _budget_intent(account_key, status="reserved", filled="5")
    intent.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with sessions() as db:
        db.add(intent)
        await db.commit()
    async with sessions() as db:
        assert not await release_confirmed_reservation(db, intent.intent_id)
    result = await _reserve_budget(sessions)
    assert result.approved and result.position_size.position_value == Decimal("900")
    async with sessions() as db:
        saved = (await db.execute(select(ExecutionIntent).where(
            ExecutionIntent.intent_id == intent.intent_id
        ))).scalar_one()
        assert saved.status == "reserved"


def _context(market: str, account_id: str, symbol: str) -> ExecutionContext:
    return ExecutionContext(
        venue="risk-test-venue",
        broker="risk-test-broker",
        account_id=account_id,
        nature=AccountNature.SIMULATED,
        verified=True,
        mode=ExecutionMode.PAPER,
        market=market,
        account_currency="USDT",
        position_model="spot",
        instrument=Instrument(
            symbol=symbol,
            asset_class="crypto",
            base_currency="TEST",
            quote_currency="USDT",
            contract_size=Decimal("1"),
            tick_size=Decimal("1"),
            tick_value=Decimal("1"),
            tick_currency="USDT",
            min_volume=Decimal("0.001"),
            max_volume=Decimal("100"),
            volume_step=Decimal("0.001"),
            price_precision=0,
            volume_precision=3,
            margin_model="cash",
            margin_rate=Decimal("1"),
            min_notional=Decimal("1"),
        ),
        capabilities=frozenset(Capability),
    )


@pytest.mark.asyncio
async def test_parallel_workers_reserve_account_budget_and_drawdown_blocks(monkeypatch):
    database_url = make_url(settings.database_url)
    endpoint = f"{database_url.host}:{database_url.port or 5432}"
    if endpoint not in os.environ.get("AUTOXTRADE_TEST_ENDPOINTS", "").split(","):
        pytest.skip("isolated PostgreSQL endpoint is not explicitly enabled")

    schema = f"risk_test_{uuid4().hex}"
    admin_engine = create_async_engine(str(database_url))
    tenant_engine = None
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (OSError, DBAPIError):
        await admin_engine.dispose()
        pytest.skip("isolated PostgreSQL endpoint is unavailable")

    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        tenant_engine = create_async_engine(
            str(database_url),
            connect_args={"server_settings": {"search_path": schema}},
        )
        tables = [
            MLModel.__table__,
            BotConfig.__table__,
            EquitySnapshot.__table__,
            Trade.__table__,
            ExecutionIntent.__table__,
            ProtectionAuthority.__table__,
            RiskAccountLock.__table__,
        ]
        async with tenant_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=tables)

        session_factory = async_sessionmaker(tenant_engine, expire_on_commit=False)
        account_id = f"risk-account-{uuid4().hex}"
        symbols = {"CRIPTO": f"C{uuid4().hex[:8]}", "FOREX": f"F{uuid4().hex[:8]}"}
        contexts = {
            market: _context(market, account_id, symbol)
            for market, symbol in symbols.items()
        }
        async with session_factory() as db:
            for market, symbol in symbols.items():
                db.add(
                    BotConfig(
                        market=market,
                        is_active=True,
                        mode="paper",
                        exchange="risk-test-venue",
                        enabled_symbols=[symbol],
                        execution_context={symbol: contexts[market].snapshot()},
                        risk_per_trade_pct=Decimal("1"),
                        max_open_trades=2,
                        max_drawdown_pct=Decimal("15"),
                        drawdown_alert_pct=Decimal("10"),
                        max_corr_threshold=Decimal("0.7"),
                        drawdown_status="OK",
                    )
                )
            await db.commit()

        async def reserve(market: str):
            async with session_factory() as db:
                manager = RiskManager(
                    db,
                    PositionSizer(),
                    DrawdownMonitor(db),
                    _NoCorrelation(),
                )
                symbol = symbols[market]
                return await manager.can_open_position(
                    market,
                    symbol,
                    Decimal("100"),
                    Decimal("90"),
                )

        monkeypatch.setattr(
            DrawdownMonitor,
            "_notify_and_broadcast",
            __import__("unittest.mock").mock.AsyncMock(),
        )
        results = await asyncio.gather(
            reserve("CRIPTO"),
            reserve("FOREX"),
            reserve("CRIPTO"),
        )
        assert sum(result.approved for result in results) == 2
        assert all(result.intent_id for result in results if result.approved)

        async with session_factory() as db:
            intents = (
                await db.execute(
                    select(ExecutionIntent).where(
                        ExecutionIntent.status == "reserved"
                    )
                )
            ).scalars().all()
            assert len(intents) == 2
            assert len({intent.account_key for intent in intents}) == 1
            assert sum((intent.risk_amount for intent in intents), Decimal("0")) <= Decimal("200")
            assert sum((intent.capital_amount for intent in intents), Decimal("0")) <= Decimal("10000")
            uncertain_id = intents[0].intent_id
            intents[0].status = "unknown"
            await db.commit()

        async with session_factory() as db:
            assert await release_confirmed_reservation(db, uncertain_id) is False
            intent = (
                await db.execute(
                    select(ExecutionIntent).where(
                        ExecutionIntent.intent_id == uncertain_id
                    )
                )
            ).scalar_one()
            assert intent.status == "unknown"
            intent.status = "reserved"
            await db.commit()

        async with session_factory() as db:
            now = datetime.now(timezone.utc)
            db.add_all(
                [
                    EquitySnapshot(
                        market="CRIPTO",
                        snapshot_at=now - timedelta(hours=1),
                        equity=Decimal("10000"),
                        open_pnl=Decimal("0"),
                        realized_pnl=Decimal("0"),
                        drawdown_1d=Decimal("0"),
                        drawdown_30d=Decimal("0"),
                        open_trades=0,
                    ),
                    EquitySnapshot(
                        market="CRIPTO",
                        snapshot_at=now,
                        equity=Decimal("8000"),
                        open_pnl=Decimal("0"),
                        realized_pnl=Decimal("0"),
                        drawdown_1d=Decimal("0"),
                        drawdown_30d=Decimal("0"),
                        open_trades=0,
                    ),
                ]
            )
            await db.commit()

        blocked = await reserve("CRIPTO")
        assert blocked.approved is False
        assert "drawdown" in (blocked.reason or "").lower()

        async with session_factory() as db:
            config = (
                await db.execute(
                    select(BotConfig).where(BotConfig.market == "CRIPTO")
                )
            ).scalar_one()
            assert config.drawdown_status == "PAUSED"
            assert config.is_active is False
    finally:
        if tenant_engine is not None:
            await tenant_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
