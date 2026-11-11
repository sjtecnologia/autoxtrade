import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
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
    RiskAccountLock,
    Trade,
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
