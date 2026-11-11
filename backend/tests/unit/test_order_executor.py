"""Testes unitários do OrderExecutor com conector simulado vinculado."""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.models import ExecutionIntent, Trade
from trading.connectors.memory_transport import MemoryTransport
from trading.connectors.simulated import SimulatedConnector
from trading.connectors.types import OrderSide
from trading.contracts import (Capability, ExecutionContext, ExecutionResult,
                                ExecutionMode, Instrument, ResultState)
from trading.order_executor import OrderExecutor, OrderExecutionError
from trading.safety import AccountNature


def _context() -> ExecutionContext:
    return ExecutionContext(
        venue="binance_spot", broker="simulator", account_id="test-account",
        nature=AccountNature.SIMULATED, verified=True, mode=ExecutionMode.PAPER,
        market="CRIPTO", account_currency="USDT", position_model="hedging",
        instrument=Instrument(
            symbol="BTC/USDT", asset_class="crypto", base_currency="BTC",
            quote_currency="USDT", contract_size=Decimal("1"), tick_size=Decimal("1"),
            tick_value=Decimal("1"), tick_currency="USDT", min_volume=Decimal("0.001"),
            max_volume=Decimal("100"), volume_step=Decimal("0.001"), price_precision=0,
            volume_precision=3, margin_model="cash", margin_rate=Decimal("1"),
            min_notional=Decimal("1"),
        ),
        capabilities=frozenset(Capability),
    )


@pytest.fixture
def mock_connector():
    context = _context()
    transport = MemoryTransport(context)
    transport.prices[context.instrument.symbol] = Decimal("50000")
    return SimulatedConnector(context, transport=transport)


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    # Lookups de intent/trade por intent_id precisam devolver "não encontrado"
    # para uma intenção nova (AsyncMock devolveria um mock truthy).
    lookup = MagicMock()
    lookup.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=lookup)
    return db


@pytest.mark.asyncio
async def test_open_position_success(mock_connector, mock_db):
    executor = OrderExecutor(mock_connector, mock_db)

    async def fake_refresh(obj):
        obj.__dict__["id"] = 1

    mock_db.refresh.side_effect = fake_refresh

    trade = await executor.open_position(
        symbol="BTC/USDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.01"),
        entry_price=Decimal("50000"),
        stop_loss=Decimal("48000"),
        take_profit=Decimal("55000"),
    )

    assert trade.status == "open"
    assert trade.position_id is not None
    assert mock_connector._transport.positions[trade.position_id].stop_loss == Decimal("48000")


@pytest.mark.asyncio
async def test_open_position_invalid_sl(mock_connector, mock_db):
    executor = OrderExecutor(mock_connector, mock_db)
    with pytest.raises(OrderExecutionError, match="MENOR"):
        await executor.open_position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.01"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("52000"),  # inválido: SL acima da entrada em LONG
        )


@pytest.mark.asyncio
async def test_oco_fallback_closes_position_when_protection_rejected(mock_connector, mock_db):
    """Proteção rejeitada: fecha a mercado em vez de deixar exposição nua."""
    mock_connector.modify_position_protection = AsyncMock(return_value=ExecutionResult(
        "protect", ResultState.REJECTED, reason="simulated rejection"))

    async def fake_refresh(obj):
        obj.__dict__["id"] = 2

    mock_db.refresh.side_effect = fake_refresh

    executor = OrderExecutor(mock_connector, mock_db)
    with pytest.raises(OrderExecutionError, match="closed to avoid unhedged risk"):
        await executor.open_position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.01"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("48000"),
            take_profit=Decimal("55000"),
        )

    persisted_trade = next(call.args[0] for call in mock_db.add.call_args_list
                           if getattr(call.args[0], "status", None) == "closed")
    assert persisted_trade.close_reason == "PROTECTION_FAILED"
    assert persisted_trade.position_id not in mock_connector._transport.positions


@pytest.mark.asyncio
async def test_protection_failure_without_close_keeps_trade_marked_unprotected(
        mock_connector, mock_db):
    """Se nem o fechamento confirmar, o trade fica aberto e sinalizado."""
    mock_connector.modify_position_protection = AsyncMock(return_value=ExecutionResult(
        "protect", ResultState.REJECTED, reason="simulated rejection"))
    mock_connector.reduce_position = AsyncMock(return_value=ExecutionResult(
        "close", ResultState.REJECTED, reason="close rejected"))

    async def fake_refresh(obj):
        obj.__dict__["id"] = 3

    mock_db.refresh.side_effect = fake_refresh

    executor = OrderExecutor(mock_connector, mock_db)
    with pytest.raises(OrderExecutionError, match="UNPROTECTED"):
        await executor.open_position(
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.01"),
            entry_price=Decimal("50000"),
            stop_loss=Decimal("48000"),
            take_profit=Decimal("55000"),
        )

    event_types = [
        getattr(call.args[0], "event_type", None) for call in mock_db.add.call_args_list
    ]
    assert "UNPROTECTED_POSITION" in event_types


@pytest.mark.asyncio
async def test_close_position(mock_connector, mock_db):
    executor = OrderExecutor(mock_connector, mock_db)
    trade = await executor.open_position(
        symbol="BTC/USDT", side=OrderSide.BUY, quantity=Decimal("0.01"),
        entry_price=Decimal("50000"), stop_loss=Decimal("48000"),
        take_profit=Decimal("55000"),
    )
    result = await executor.close_position(trade, reason="manual")
    assert result.status == "closed"
    assert result.close_reason == "manual"


# ---------------------------------------------------------------------------
# Intenção estável, recuperação sem reenvio e proteção com banco falho
# ---------------------------------------------------------------------------

class _Lookup:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class FakeSession:
    """Session assíncrona em memória para os fluxos de intent/recuperação.

    Responde aos selects do executor (ExecutionIntent/Trade por intent_id),
    persiste objetos added a cada commit bem-sucedido e simula quedas do
    banco a partir do N-ésimo commit (`fail_from_commit`). O rollback restaura
    o último estado persistido dos intents (rollback expira objetos reais).
    """

    def __init__(self, *, fail_from_commit: int | None = None,
                 error: Exception | None = None) -> None:
        self.fail_from_commit = fail_from_commit
        self.error = error or RuntimeError("simulated database outage")
        self.commit_calls = 0
        self.rollback_calls = 0
        self.pending: list = []
        self.intents: dict[str, ExecutionIntent] = {}
        self.trades: dict[str, Trade] = {}
        self._intent_state: dict[str, tuple] = {}

    def add(self, obj) -> None:
        self.pending.append(obj)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        params = stmt.compile().params
        if entity is ExecutionIntent:
            statuses = params.get("status_1")
            if isinstance(statuses, list):
                # Checagem de conflito: primeira intent não resolvida que
                # casa com market/mode/symbol/side do request.
                for row in self.intents.values():
                    if (row.market == params["market_1"]
                            and row.mode == params["mode_1"]
                            and row.symbol == params["symbol_1"]
                            and row.side == params["side_1"]
                            and row.status in statuses):
                        return _Lookup(row)
                return _Lookup(None)
            key = next(iter(params.values()), None)
            return _Lookup(self.intents.get(key))
        if entity is Trade:
            key = next(iter(params.values()), None)
            return _Lookup(self.trades.get(key))
        return _Lookup(None)

    def _snapshot_intents(self) -> dict:
        return {
            intent_id: (row.status, row.order_id, row.position_id, row.result_state,
                        row.filled_quantity)
            for intent_id, row in self.intents.items()
        }

    async def commit(self) -> None:
        self.commit_calls += 1
        if self.fail_from_commit is not None and self.commit_calls >= self.fail_from_commit:
            raise self.error
        for obj in self.pending:
            if isinstance(obj, ExecutionIntent):
                self.intents[obj.intent_id] = obj
            elif isinstance(obj, Trade):
                self.trades[obj.intent_id] = obj
        self.pending.clear()
        self._intent_state = self._snapshot_intents()

    async def rollback(self) -> None:
        self.rollback_calls += 1
        for intent_id, state in self._intent_state.items():
            row = self.intents.get(intent_id)
            if row is not None:
                (row.status, row.order_id, row.position_id,
                 row.result_state, row.filled_quantity) = state
        self.pending.clear()

    async def refresh(self, obj) -> None:
        if getattr(obj, "id", None) is None:
            obj.__dict__["id"] = 1


async def _open(executor, **overrides):
    params = dict(
        symbol="BTC/USDT", side=OrderSide.BUY, quantity=Decimal("0.01"),
        entry_price=Decimal("50000"), stop_loss=Decimal("48000"),
        take_profit=Decimal("55000"),
    )
    params.update(overrides)
    return await executor.open_position(**params)


@pytest.mark.asyncio
async def test_intent_e_persistida_antes_do_envio(mock_connector):
    """A intenção chega ao banco ANTES da ordem ir ao transport."""
    db = FakeSession()
    seen = {}
    original_create = mock_connector.create_order

    async def spying_create(intent):
        row = db.intents.get(intent.id)
        seen["persistido_antes_do_envio"] = row is not None
        seen["status_no_envio"] = row.status if row is not None else None
        return await original_create(intent)

    mock_connector.create_order = spying_create
    executor = OrderExecutor(mock_connector, db)

    trade = await _open(executor, intent_id="antes-do-envio")

    assert seen["persistido_antes_do_envio"] is True
    assert seen["status_no_envio"] == "planned"
    assert db.intents["antes-do-envio"].status == "confirmed"
    assert trade.intent_id == "antes-do-envio"
    assert db.intents["antes-do-envio"].position_id == trade.position_id


@pytest.mark.asyncio
async def test_intent_persistence_failure_before_send_blocks_order(mock_connector):
    """Falha de banco ANTES do envio: nenhuma ordem é enviada."""
    db = FakeSession(fail_from_commit=1)
    executor = OrderExecutor(mock_connector, db)

    with pytest.raises(OrderExecutionError, match="no order sent"):
        await _open(executor, intent_id="nunca-enviado")

    transport = mock_connector._transport
    assert transport.intents == {}       # nada chegou ao transport
    assert transport.positions == {}      # nenhuma exposição criada
    assert db.trades == {}
    assert db.rollback_calls >= 1


@pytest.mark.asyncio
async def test_commit_failure_after_send_protects_and_recovers_without_resend(mock_connector):
    """Commit do trade falha após envio: proteção é aplicada e a recuperação
    com o mesmo intent_id completa sem reenviar a ordem."""
    db = FakeSession(fail_from_commit=2)  # intent OK; commit do trade falha
    executor = OrderExecutor(mock_connector, db)
    transport = mock_connector._transport

    with pytest.raises(OrderExecutionError, match="PROTECTED") as exc_info:
        await _open(executor, intent_id="recuperavel")

    message = str(exc_info.value)
    assert "intent_id=recuperavel" in message

    position_id = next(iter(transport.positions))
    # Exposicao confirmada externamente ficou PROTEGIDA mesmo com banco falho.
    assert transport.positions[position_id].stop_loss == Decimal("48000")
    assert db.trades == {}                       # trade não persistiu
    assert db.intents["recuperavel"].status == "planned"
    assert db.rollback_calls >= 1

    # Banco volta: recuperação com o MESMO intent_id, sem reenvio de ordem.
    db.fail_from_commit = None
    recovered = await _open(OrderExecutor(mock_connector, db), intent_id="recuperavel")

    assert recovered.status == "open"
    assert recovered.intent_id == "recuperavel"
    assert recovered.position_id == position_id          # mesma posição
    assert len(transport.intents) == 1                   # nenhum reenvio
    assert len(transport.positions) == 1                  # nenhuma posição nova
    assert db.trades == {"recuperavel": recovered}       # trade único vinculado
    assert db.intents["recuperavel"].status == "confirmed"
    assert transport.positions[position_id].stop_loss == Decimal("48000")


@pytest.mark.asyncio
async def test_commit_failure_with_protection_failure_closes_despite_db_down(mock_connector):
    """Banco fora do ar após envio + proteção rejeitada: fechamento seguro
    (fail-safe) é tentado e executado mesmo sem conseguir persistir."""
    db = FakeSession(fail_from_commit=2)  # intent OK; demais commits falham
    mock_connector.modify_position_protection = AsyncMock(return_value=ExecutionResult(
        "protect", ResultState.REJECTED, reason="simulated rejection"))
    executor = OrderExecutor(mock_connector, db)
    transport = mock_connector._transport

    with pytest.raises(OrderExecutionError, match="closed to avoid unhedged risk"):
        await _open(executor, intent_id="fail-safe")

    assert transport.positions == {}              # posição fechada a mercado
    assert db.trades == {}                         # banco fora: nada persistiu
    assert db.intents["fail-safe"].status == "planned"
    assert db.rollback_calls >= 2                  # commits do fail-safe engolidos


@pytest.mark.asyncio
async def test_reinvoke_with_same_intent_returns_open_trade_without_resend(mock_connector):
    """Reinvocação com o mesmo intent_id devolve o trade aberto sem reenviar."""
    db = FakeSession()
    executor = OrderExecutor(mock_connector, db)

    trade = await _open(executor, intent_id="idempotente")
    again = await _open(executor, intent_id="idempotente")

    assert again is trade                          # mesmo trade, sem duplicar
    assert len(mock_connector._transport.intents) == 1
    assert len(mock_connector._transport.positions) == 1
    assert mock_connector._transport.positions[trade.position_id].stop_loss == Decimal("48000")


@pytest.mark.asyncio
async def test_reinvoke_with_same_intent_but_different_request_is_refused(mock_connector):
    """Intent já persistido só pode carregar exatamente o mesmo request."""
    db = FakeSession()
    executor = OrderExecutor(mock_connector, db)

    await _open(executor, intent_id="request-fixado")

    with pytest.raises(OrderExecutionError, match="different request"):
        await _open(executor, intent_id="request-fixado", quantity=Decimal("0.02"))

    assert len(mock_connector._transport.intents) == 1   # nada novo enviado


@pytest.mark.asyncio
async def test_reinvoke_after_trade_closed_is_refused_without_resend(mock_connector):
    """Intent com trade já fechado nunca reenvia ordem."""
    db = FakeSession()
    executor = OrderExecutor(mock_connector, db)

    trade = await _open(executor, intent_id="ja-fechado")
    await executor.close_position(trade, reason="manual")

    with pytest.raises(OrderExecutionError, match="already linked"):
        await _open(executor, intent_id="ja-fechado")

    assert len(mock_connector._transport.intents) == 1
    assert mock_connector._transport.positions == {}


@pytest.mark.asyncio
async def test_rejected_send_marks_intent_failed(mock_connector):
    """Envio rejeitado: intent fica marcado como failed com o resultado externo."""
    db = FakeSession()
    mock_connector._transport.scenarios["rejeitado"] = "reject"
    executor = OrderExecutor(mock_connector, db)

    with pytest.raises(OrderExecutionError, match="Entry not fully confirmed: rejected"):
        await _open(executor, intent_id="rejeitado")

    row = db.intents["rejeitado"]
    assert row.status == "failed"
    assert row.result_state == "rejected"
    assert mock_connector._transport.positions == {}


def _settling_timeout(connector):
    """create_order que liquida no transport e perde a resposta (timeout)."""
    original = connector.create_order
    calls = {"n": 0}

    async def wrapper(intent):
        calls["n"] += 1
        result = await original(intent)  # transport liquida a ordem...
        raise TimeoutError("lost response after acceptance")  # ...mas a resposta se perde

    connector.create_order = wrapper
    return calls


# ---------------------------------------------------------------------------
# PARTIAL: quantidade executada registrada, protegida e sem reenvio
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_fill_records_quantity_state_and_protects_exposure(mock_connector):
    """Parcial: intent registra estado/quantidade executada; exposição fica
    protegida e a reinvocação devolve o mesmo trade sem reenviar."""
    db = FakeSession()
    mock_connector._transport.scenarios["parcial"] = Decimal("0.005")
    executor = OrderExecutor(mock_connector, db)

    with pytest.raises(OrderExecutionError, match="partially executed") as exc_info:
        await _open(executor, intent_id="parcial")
    assert "intent_id=parcial" in str(exc_info.value)

    row = db.intents["parcial"]
    assert row.status == "partial"
    assert row.filled_quantity == Decimal("0.005")
    assert row.position_id is not None
    trade = db.trades["parcial"]
    assert trade.status == "open"
    assert trade.quantity == Decimal("0.005")          # só o que executou
    assert mock_connector._transport.positions[trade.position_id].stop_loss == Decimal("48000")

    again = await _open(executor, intent_id="parcial")
    assert again is trade                              # mesmo trade, sem duplicar
    assert len(mock_connector._transport.intents) == 1  # nenhum reenvio
    assert len(mock_connector._transport.positions) == 1


@pytest.mark.asyncio
async def test_partial_with_protection_failure_reduces_only_executed_exposure(mock_connector):
    """Parcial + proteção falha: fail-safe reduz APENAS a exposição
    confirmada (0.005), não a solicitada (0.01); intent segue partial."""
    db = FakeSession()
    mock_connector._transport.scenarios["parcial-falha"] = Decimal("0.005")
    mock_connector.modify_position_protection = AsyncMock(return_value=ExecutionResult(
        "protect", ResultState.REJECTED, reason="simulated rejection"))
    executor = OrderExecutor(mock_connector, db)

    with pytest.raises(OrderExecutionError, match="closed to avoid unhedged risk"):
        await _open(executor, intent_id="parcial-falha")

    row = db.intents["parcial-falha"]
    assert row.status == "partial"                    # resultado real registrado
    assert row.filled_quantity == Decimal("0.005")
    trade = db.trades["parcial-falha"]
    assert trade.quantity == Decimal("0.005")         # redução foi da confirmada
    assert trade.status == "closed"
    assert trade.close_reason == "PROTECTION_FAILED"
    assert mock_connector._transport.positions == {}   # exposição 0.005 encerrada

    # Intent não resolvida bloqueia novas entradas até resolução.
    with pytest.raises(OrderExecutionError, match="Conflicting unresolved entry intent"):
        await _open(executor, intent_id="nova-entrada")
    assert "nova-entrada" not in db.intents
    assert len(mock_connector._transport.intents) == 1


# ---------------------------------------------------------------------------
# UNKNOWN: timeout com execução, reconciliação pelo mesmo intent_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_with_execution_marks_unknown_and_recovers_without_resend(mock_connector):
    """Timeout após aceitação: intent "unknown" (nunca "failed"), e a
    recuperação pelo mesmo intent_id completa SEM reenviar a ordem."""
    db = FakeSession()
    calls = _settling_timeout(mock_connector)
    transport = mock_connector._transport
    executor = OrderExecutor(mock_connector, db)

    with pytest.raises(OrderExecutionError, match="outcome unknown") as exc_info:
        await _open(executor, intent_id="timeout-1")
    message = str(exc_info.value)
    assert "intent_id=timeout-1" in message
    assert "no resend" in message

    row = db.intents["timeout-1"]
    assert row.status == "unknown"                    # timeout ≠ rejeição
    assert db.trades == {}                             # trade ainda não persistido
    position_id = next(iter(transport.positions))
    assert calls["n"] == 1                             # create_order ocorreu 1x

    # Recuperação: reconciliação por query_order, sem novo create_order.
    recovered = await _open(OrderExecutor(mock_connector, db), intent_id="timeout-1")

    assert recovered.status == "open"
    assert recovered.position_id == position_id       # mesma posição externa
    assert recovered.quantity == Decimal("0.01")
    assert calls["n"] == 1                             # nenhum reenvio
    assert len(transport.intents) == 1
    assert len(transport.positions) == 1
    assert db.intents["timeout-1"].status == "confirmed"
    assert db.trades == {"timeout-1": recovered}
    assert transport.positions[position_id].stop_loss == Decimal("48000")


@pytest.mark.asyncio
async def test_restart_during_reconciliation_completes_on_second_recovery(mock_connector):
    """Reinício durante a reconciliação: timeout → recuperação com banco
    caído (posição fica PROTECTED) → recuperação final com banco de pé."""
    db = FakeSession(fail_from_commit=4)  # intent, unknown e lookup persistem
    calls = _settling_timeout(mock_connector)
    transport = mock_connector._transport
    executor = OrderExecutor(mock_connector, db)

    with pytest.raises(OrderExecutionError, match="outcome unknown"):
        await _open(executor, intent_id="reinicio")
    assert db.intents["reinicio"].status == "unknown"
    position_id = next(iter(transport.positions))

    # Recuperação 1: banco cai exatamente no commit do trade.
    with pytest.raises(OrderExecutionError, match="PROTECTED") as exc_info:
        await _open(OrderExecutor(mock_connector, db), intent_id="reinicio")
    assert "intent_id=reinicio" in str(exc_info.value)
    assert db.trades == {}
    assert db.intents["reinicio"].status == "unknown"  # rollback restaurou
    assert transport.positions[position_id].stop_loss == Decimal("48000")

    # Recuperação 2: banco volta e a reconciliação completa sem reenvio.
    db.fail_from_commit = None
    recovered = await _open(OrderExecutor(mock_connector, db), intent_id="reinicio")

    assert recovered.status == "open"
    assert recovered.position_id == position_id
    assert calls["n"] == 1
    assert len(transport.intents) == 1
    assert db.trades == {"reinicio": recovered}
    assert db.intents["reinicio"].status == "confirmed"


# ---------------------------------------------------------------------------
# Bloqueio de entradas conflitantes até resolução
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_entry_blocked_while_previous_intent_unresolved(mock_connector):
    """Intent não resolvida (unknown) bloqueia nova entrada para o request."""
    db = FakeSession()
    _settling_timeout(mock_connector)
    transport = mock_connector._transport
    executor = OrderExecutor(mock_connector, db)

    with pytest.raises(OrderExecutionError, match="outcome unknown"):
        await _open(executor, intent_id="pendente")

    with pytest.raises(OrderExecutionError, match="Conflicting unresolved entry intent"):
        await _open(executor, intent_id="segunda")

    assert "segunda" not in db.intents                # nem persistiu a nova
    assert len(transport.intents) == 1                # nada novo foi enviado


@pytest.mark.asyncio
async def test_new_entry_allowed_after_intent_resolved_failed(mock_connector):
    """Intent resolvida como failed (rejeitada) libera nova entrada."""
    db = FakeSession()
    transport = mock_connector._transport
    transport.scenarios["rejeitada"] = "reject"
    executor = OrderExecutor(mock_connector, db)

    with pytest.raises(OrderExecutionError, match="Entry not fully confirmed: rejected"):
        await _open(executor, intent_id="rejeitada")
    assert db.intents["rejeitada"].status == "failed"

    trade = await _open(executor, intent_id="apos-rejeitada")
    assert trade.status == "open"
    assert len(transport.intents) == 2                # nova ordem, nova intent


@pytest.mark.asyncio
async def test_never_sent_intent_reconciles_failed_and_allows_new_intent(mock_connector):
    """Intent persistida que jamais chegou ao transport: reconcilia como
    failed (sem exposição/ambiguidade) e uma nova intent pode entrar."""
    db = FakeSession()
    executor = OrderExecutor(mock_connector, db)
    db.add(ExecutionIntent(
        intent_id="nunca-enviado", market="CRIPTO", mode="paper",
        symbol="BTC/USDT", side="buy", quantity=Decimal("0.01"), status="planned",
    ))
    await db.commit()

    with pytest.raises(OrderExecutionError, match="never reached the transport"):
        await _open(executor, intent_id="nunca-enviado")

    assert db.intents["nunca-enviado"].status == "failed"
    assert db.trades == {}
    assert mock_connector._transport.positions == {}

    trade = await _open(executor, intent_id="depois-do-nao-enviado")
    assert trade.status == "open"
    assert len(mock_connector._transport.intents) == 1  # só a nova ordem
