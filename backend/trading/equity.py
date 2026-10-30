"""Account equity and PnL sources, never shared across venues."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from db.models import BotConfig, Trade
from trading.contracts import ExecutionContext, context_from_dict


class EquityUnavailable(RuntimeError):
    """Equity cannot be established from an explicit, reliable source."""


@dataclass(frozen=True)
class MarketEquity:
    market: str
    venue: str
    account_id: str
    mode: str
    currency: str
    balance: Decimal
    realized_pnl: Decimal
    open_pnl: Decimal
    equity: Decimal
    open_trades: int
    source: str
    status: str = "ok"
    detail: str | None = None


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EquityUnavailable(f"Valor numérico inválido: {value!r}") from exc
    if not result.is_finite():
        raise EquityUnavailable(f"Valor numérico não finito: {value!r}")
    return result


def contract_size_for_trade(trade: Trade) -> Decimal:
    snapshot = trade.execution_context or {}
    instrument = snapshot.get("instrument") or {}
    raw = instrument.get("contract_size")
    if raw is None:
        if getattr(trade, "market", "") in {"B3", "FOREX"}:
            raise EquityUnavailable(
                f"Contract size ausente para trade {trade.id} ({trade.symbol})"
            )
        return Decimal("1")
    size = _decimal(raw)
    if size <= 0:
        raise EquityUnavailable(f"Contract size inválido para trade {trade.id}")
    return size


def open_position_pnl(trade: Trade, mark_price: Decimal) -> Decimal:
    """Mark-to-market PnL using the trade's real contract size and side."""
    price = _decimal(mark_price)
    if price <= 0:
        raise EquityUnavailable(f"Preço de marca inválido para {trade.symbol}: {price}")
    entry = _decimal(trade.entry_price)
    quantity = _decimal(trade.quantity)
    direction = Decimal("1") if trade.side == "buy" else Decimal("-1")
    return (price - entry) * quantity * contract_size_for_trade(trade) * direction


def _value_from_trade_snapshot(trade: Trade, key: str, default: str = "") -> str:
    snapshot = trade.execution_context or {}
    value = snapshot.get(key)
    return str(value) if value not in (None, "") else default


def _context_for_symbol(config: BotConfig, symbol: str) -> ExecutionContext | None:
    bindings = getattr(config, "execution_context", None) or {}
    if not isinstance(bindings, dict):
        return None
    raw = bindings.get(symbol)
    if raw is None and (bindings.get("instrument") or {}).get("symbol") == symbol:
        raw = bindings
    if raw is None:
        return None
    try:
        return context_from_dict(raw)
    except Exception:
        return None


def _market_identity(
    config: BotConfig, trades: list[Trade]
) -> tuple[str, str, str, str, ExecutionContext | None]:
    """Resolve one account for the market; mixed accounts are not aggregated."""
    contexts: list[ExecutionContext] = []
    for symbol in config.enabled_symbols or []:
        context = _context_for_symbol(config, symbol)
        if context is not None:
            contexts.append(context)
    for trade in trades:
        snapshot = trade.execution_context or {}
        allowed = set(ExecutionContext.__dataclass_fields__)
        try:
            contexts.append(
                context_from_dict({k: v for k, v in snapshot.items() if k in allowed})
            )
        except Exception:
            continue

    unique = {
        (ctx.venue, ctx.account_id, ctx.account_currency, ctx.mode.value): ctx
        for ctx in contexts
    }
    if not unique:
        raise EquityUnavailable(
            f"Sem execution_context resolvido para {config.market}; equity indisponível"
        )
    if len(unique) > 1:
        raise EquityUnavailable(
            f"{config.market} mistura contas/venues; equity não pode ser agregada"
        )
    (venue, account_id, currency, mode), context = next(iter(unique.items()))
    if config.mode and mode != config.mode:
        raise EquityUnavailable(
            f"Modo do execution_context ({mode}) difere do BotConfig ({config.mode})"
        )
    return venue, account_id, currency, mode, context


async def _closed_realized_pnl(db: AsyncSession, market: str, mode: str) -> Decimal:
    result = await db.execute(
        select(func.coalesce(func.sum(Trade.pnl_net), 0)).where(
            Trade.market == market,
            Trade.mode == mode,
            Trade.status == "closed",
        )
    )
    return _decimal(result.scalar_one() or 0)


async def paper_available_capital(db: AsyncSession, market: str) -> Decimal:
    """Cash available for paper sizing: explicit starting capital + realized PnL."""
    base = settings.paper_initial_capital
    if base is None:
        return Decimal("0")
    starting = _decimal(base)
    if starting <= 0:
        return Decimal("0")
    realized = await _closed_realized_pnl(db, market, "paper")
    return max(starting + realized, Decimal("0"))


async def _broker_balance(
    context: ExecutionContext, mode: str, currency: str
) -> Decimal:
    from trading.connectors.factory import get_connector

    connector = get_connector(context, mode=mode)
    await connector.connect()
    try:
        balances = await connector.get_balance()
    finally:
        try:
            await connector.close()
        except Exception:
            pass
    balance = balances.get(currency)
    if balance is None:
        raise EquityUnavailable(
            f"Saldo {currency} ausente na conta {context.account_id}"
        )
    total = _decimal(balance.free) + _decimal(balance.locked)
    if total < 0:
        raise EquityUnavailable(f"Saldo negativo na conta {context.account_id}")
    return total


async def collect_market_equity(
    db: AsyncSession,
    config: BotConfig,
    *,
    mark_price: Callable[[str, str], Awaitable[Decimal]],
) -> MarketEquity:
    """Build equity for one market from its own venue/account balance and marks."""
    result_open = await db.execute(
        select(Trade).where(Trade.market == config.market, Trade.status == "open")
    )
    open_trades = list(result_open.scalars().all())

    try:
        venue, account_id, currency, mode, context = _market_identity(config, open_trades)
        realized = await _closed_realized_pnl(db, config.market, mode)

        if mode == "paper":
            starting = settings.paper_initial_capital
            if starting is None or _decimal(starting) <= 0:
                raise EquityUnavailable("PAPER_INITIAL_CAPITAL não configurado")
            balance = max(_decimal(starting) + realized, Decimal("0"))
            source = "paper_ledger"
        else:
            if context is None:
                raise EquityUnavailable("Execution context ausente para saldo real")
            balance = await _broker_balance(context, mode, currency)
            source = "broker"

        marks: dict[str, Decimal] = {}
        open_pnl = Decimal("0")
        for trade in open_trades:
            if trade.symbol not in marks:
                marks[trade.symbol] = await mark_price(trade.symbol, config.market)
            open_pnl += open_position_pnl(trade, marks[trade.symbol])

        equity = balance + open_pnl
        return MarketEquity(
            market=config.market,
            venue=venue,
            account_id=account_id,
            mode=mode,
            currency=currency,
            balance=balance,
            realized_pnl=realized,
            open_pnl=open_pnl,
            equity=equity,
            open_trades=len(open_trades),
            source=source,
        )
    except EquityUnavailable as exc:
        return MarketEquity(
            market=config.market,
            venue=_value_from_trade_snapshot(open_trades[0], "venue", "unknown")
            if open_trades
            else "unknown",
            account_id=_value_from_trade_snapshot(open_trades[0], "account_id", "unknown")
            if open_trades
            else "unknown",
            mode=config.mode or "unknown",
            currency="unknown",
            balance=Decimal("0"),
            realized_pnl=Decimal("0"),
            open_pnl=Decimal("0"),
            equity=Decimal("0"),
            open_trades=len(open_trades),
            source="unavailable",
            status="unavailable",
            detail=str(exc),
        )
