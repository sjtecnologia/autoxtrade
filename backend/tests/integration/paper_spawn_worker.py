"""Spawn-safe, isolated worker for the durable paper integration tests."""
import asyncio
import os
from decimal import Decimal


def run_effect(context, database_url, schema, action, identifier, fence=None, output=None):
    async def run():
        from pydantic_settings.sources import DotEnvSettingsSource
        DotEnvSettingsSource._read_env_files = lambda self: {}

        from sqlalchemy import make_url
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from paper.durable_effects import DurablePaperEffects
        from trading.connectors.memory_transport import MemoryTransport
        from trading.connectors.simulated import SimulatedConnector
        from trading.contracts import ResultState

        url = make_url(database_url)
        if (url.host != "127.0.0.1" or url.port != 15432
                or url.username != "test" or url.database != "autoxtrade_test"
                or "127.0.0.1:15432" not in os.environ.get("AUTOXTRADE_TEST_ENDPOINTS", "").split(",")
                or not schema.startswith("approval_test_") or not schema[14:].isalnum()):
            raise RuntimeError("Only the explicitly isolated test database is allowed")
        engine = create_async_engine(database_url, connect_args={"server_settings": {"search_path": schema}})
        try:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            connector = SimulatedConnector(
                context, transport=MemoryTransport(context, durable_effects=DurablePaperEffects(sessions, context))
            )
            if action == "reduce":
                result = await connector.reduce_position(
                    identifier, Decimal("0.4"), intent_id="lost-response",
                    fence=fence, execution_price=Decimal("100"),
                )
                assert result.state is ResultState.CONFIRMED
            elif action == "fill":
                result = await connector.observe_paper_fill(
                    identifier, filled=Decimal("0.8"), average=Decimal("101.5"),
                    terminal=False, revision=2,
                )
                assert result.filled == Decimal("0.8")
            elif action == "query-entry":
                result = await connector.query_order(intent_id=identifier)
                if output is None:
                    raise ValueError("Query output pipe required")
                output.send(result)
                output.close()
            else:
                raise ValueError("Unsupported test action")
        finally:
            await engine.dispose()

    asyncio.run(run())
    if action == "reduce":
        os._exit(0)
