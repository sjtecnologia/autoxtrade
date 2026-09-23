"""Script de seed: insere BotConfig padrão para os mercados suportados.

Uso:
    cd backend
    python scripts/seed_db.py
"""
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

# Garante que o diretório backend está no sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from config import settings
from db.models import BotConfig

DEFAULT_CONFIGS = [
    {
        "market": "CRIPTO",
        "is_active": False,
        "mode": "paper",
        "exchange": "mt5_dwx",
        "enabled_symbols": ["BTCUSD.lv", "ETHUSD.lv", "BCHUSD.lv"],
        "risk_per_trade_pct": Decimal("1.0"),
        "max_open_trades": 3,
        "max_drawdown_pct": Decimal("15.0"),
        "drawdown_alert_pct": Decimal("10.0"),
        "max_corr_threshold": Decimal("0.70"),
        "min_ml_confidence": Decimal("0.60"),
    },
    {
        "market": "B3",
        "is_active": False,
        "mode": "paper",
        "exchange": "mt5_dwx",
        "enabled_symbols": ["PETR4", "VALE3", "ITUB4", "BBDC4"],
        "risk_per_trade_pct": Decimal("1.0"),
        "max_open_trades": 3,
        "max_drawdown_pct": Decimal("15.0"),
        "drawdown_alert_pct": Decimal("10.0"),
        "max_corr_threshold": Decimal("0.70"),
        "min_ml_confidence": Decimal("0.60"),
    },
    {
        "market": "FOREX",
        "is_active": False,
        "mode": "paper",
        "exchange": "mt5_dwx",
        "enabled_symbols": ["EURUSD.pr", "GBPUSD.pr", "USDJPY.pr", "XAUUSD.pr"],
        "risk_per_trade_pct": Decimal("1.0"),
        "max_open_trades": 3,
        "max_drawdown_pct": Decimal("15.0"),
        "drawdown_alert_pct": Decimal("10.0"),
        "max_corr_threshold": Decimal("0.70"),
        "min_ml_confidence": Decimal("0.60"),
    },
]


async def seed() -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session:
        async with session.begin():
            for cfg_data in DEFAULT_CONFIGS:
                result = await session.execute(
                    select(BotConfig).where(BotConfig.market == cfg_data["market"])
                )
                existing = result.scalar_one_or_none()
                if existing is None:
                    session.add(BotConfig(**cfg_data))
                    print(f"[seed] BotConfig inserido: market={cfg_data['market']}")
                else:
                    print(f"[seed] BotConfig já existe: market={cfg_data['market']} — ignorando")

    await engine.dispose()
    print("[seed] Concluído.")


if __name__ == "__main__":
    asyncio.run(seed())
