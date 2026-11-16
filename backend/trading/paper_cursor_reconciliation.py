"""List and record operator decisions for blocked legacy paper cursors."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.models import Trade, TradeEvent

logger = logging.getLogger(__name__)
DECISION_EVENT = "PAPER_CURSOR_DECISION"


@dataclass(frozen=True)
class BlockedPaperCursor:
    trade_id: int
    market: str
    symbol: str
    safe_break_event_id: int


async def list_blocked_paper_cursors(sessions: async_sessionmaker[AsyncSession]) -> list[BlockedPaperCursor]:
    async with sessions() as db:
        rows = (await db.execute(
            select(Trade.id, Trade.market, Trade.symbol, TradeEvent.id)
            .join(TradeEvent, TradeEvent.trade_id == Trade.id)
            .where(
                Trade.status == "open", Trade.mode == "paper",
                Trade.paper_monitor_candle_at.is_(None),
                TradeEvent.event_type == "SAFE_BREAK_ARMED",
            )
            .order_by(Trade.id, TradeEvent.id)
        )).all()
    blocked = {}
    for trade_id, market, symbol, event_id in rows:
        blocked.setdefault(trade_id, BlockedPaperCursor(trade_id, market, symbol, event_id))
    return list(blocked.values())


async def record_paper_cursor_decision(
    sessions: async_sessionmaker[AsyncSession], trade_id: int, *,
    decision_id: str, operator: str, reason: str, evidence: str,
) -> bool:
    """Record a decision to retain the block; never infer or move a cursor."""
    try:
        decision_id = str(UUID(decision_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("decision_id deve ser um UUID") from exc
    operator, reason, evidence = operator.strip(), reason.strip(), evidence.strip()
    if not operator or not reason or not evidence:
        raise ValueError("operador, justificativa e referência às evidências são obrigatórios")
    if len(operator) > 100 or len(reason) > 2000 or len(evidence) > 2000:
        raise ValueError("decisão excede tamanho permitido")
    payload = {
        "decision_id": decision_id, "operator": operator, "reason": reason,
        "evidence": evidence, "decision": "keep_blocked",
    }
    async with sessions() as db:
        async with db.begin():
            trade = (await db.execute(
                select(Trade).where(Trade.id == trade_id).with_for_update()
            )).scalar_one_or_none()
            if trade is None:
                raise ValueError("Trade não encontrado")
            prior = (await db.execute(select(TradeEvent).where(
                TradeEvent.trade_id == trade_id, TradeEvent.event_type == DECISION_EVENT,
                TradeEvent.payload["decision_id"].astext == decision_id,
            ))).scalar_one_or_none()
            if prior is not None:
                if prior.payload != payload:
                    raise ValueError("decision_id reutilizado com payload diferente")
                return False
            safe_event = await db.scalar(select(exists().where(
                TradeEvent.trade_id == trade_id, TradeEvent.event_type == "SAFE_BREAK_ARMED",
            )))
            if trade.status != "open" or trade.mode != "paper" or trade.paper_monitor_candle_at is not None or not safe_event:
                raise ValueError("Trade não está bloqueado por safe break legado sem cursor")
            db.add(TradeEvent(trade_id=trade_id, event_type=DECISION_EVENT, payload=payload))
    return True


async def _main(args: argparse.Namespace) -> None:
    from config import settings

    engine = create_async_engine(settings.database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if args.command == "list":
            print(json.dumps([asdict(item) for item in await list_blocked_paper_cursors(sessions)]))
        else:
            created = await record_paper_cursor_decision(
                sessions, args.trade_id, decision_id=args.decision_id,
                operator=args.operator, reason=args.reason, evidence=args.evidence,
            )
            print(json.dumps({"recorded": created, "cursor": "blocked"}))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Listar trades paper bloqueados")
    record = commands.add_parser("record", help="Registrar decisão de manter bloqueio")
    record.add_argument("--trade-id", required=True, type=int)
    record.add_argument("--decision-id", required=True, help="UUID estável para repetição")
    record.add_argument("--operator", required=True)
    record.add_argument("--reason", required=True)
    record.add_argument("--evidence", required=True)
    asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    main()
