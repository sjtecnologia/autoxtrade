from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    exchange: Mapped[str] = mapped_column(String(30), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False, default="unknown")
    account_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    account_nature: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    instrument_class: Mapped[str | None] = mapped_column(String(30), nullable=True)
    position_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    execution_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    identity_status: Mapped[str] = mapped_column(String(30), nullable=False, default="unresolved")

    # Abertura
    entry_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    entry_value: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    take_profit: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    open_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Intenção de execução que originou o trade (unicidade no banco: um trade
    # por intent; legado sem intent fica NULL e o índice único tolera NULLs).
    intent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Fechamento
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    exit_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    close_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Resultado
    pnl_gross: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    commission: Mapped[Decimal] = mapped_column(Numeric(20, 2), default=Decimal("0"))
    pnl_net: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ML metadata
    model_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ml_signal: Mapped[str | None] = mapped_column(String(10), nullable=True)
    ml_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    market_regime: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Risk metadata
    risk_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    capital_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    risk_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    events: Mapped[list["TradeEvent"]] = relationship(
        "TradeEvent", back_populates="trade", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_trades_market", "market"),
        Index("idx_trades_symbol", "symbol"),
        Index("idx_trades_status", "status"),
        Index("idx_trades_open_at", "open_at"),
        Index("idx_trades_mode", "mode"),
        Index("idx_trades_model", "model_version"),
        Index("idx_trades_intent", "intent_id", unique=True),
    )


class MLModel(Base):
    __tablename__ = "ml_models"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="inactive")

    file_path: Mapped[str] = mapped_column(String(255), nullable=False)
    scaler_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    feature_list: Mapped[dict] = mapped_column(JSONB, nullable=False)
    hyperparams: Mapped[dict] = mapped_column(JSONB, nullable=False)

    train_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    train_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    train_samples: Mapped[int] = mapped_column(Integer, nullable=False)

    bt_profit_factor: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    bt_sharpe_ratio: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    bt_win_rate: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    bt_max_drawdown: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    bt_total_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bt_total_return: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)

    ml_accuracy: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    ml_precision_long: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    ml_recall_long: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    ml_precision_short: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)
    ml_recall_short: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)

    is_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    trained_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("idx_models_market", "market"),
        Index("idx_models_status", "status"),
        Index("idx_models_version", "version"),
    )


class BotConfig(Base):
    __tablename__ = "bot_config"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False, unique=True)

    # Estado
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False, default="paper")
    execution_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    active_model_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ml_models.id"), nullable=True
    )

    # Exchange
    exchange: Mapped[str] = mapped_column(String(30), nullable=False)
    enabled_symbols: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )

    # Parâmetros de risco
    risk_per_trade_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("1.0")
    )
    max_open_trades: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=3)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("15.0")
    )
    drawdown_alert_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("10.0")
    )
    max_corr_threshold: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("0.70")
    )
    min_ml_confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, default=Decimal("0.60")
    )

    # Estado de risco em runtime
    current_drawdown_1d: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    current_drawdown_30d: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    peak_capital: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    drawdown_status: Mapped[str] = mapped_column(String(10), nullable=False, default="OK")
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    market: Mapped[str | None] = mapped_column(String(10), nullable=True)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    open_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False, default=Decimal("0"))
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, default=Decimal("0")
    )
    drawdown_1d: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    drawdown_30d: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, default=Decimal("0")
    )
    open_trades: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    __table_args__ = (
        Index("idx_equity_snapshot_at", "snapshot_at"),
        Index("idx_equity_market", "market", "snapshot_at"),
    )


class TradeEvent(Base):
    __tablename__ = "trade_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("trades.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    trade: Mapped["Trade"] = relationship("Trade", back_populates="events")

    __table_args__ = (
        Index("idx_events_trade_id", "trade_id"),
        Index("idx_events_type", "event_type"),
        Index("idx_events_occurred", "occurred_at"),
    )


class ExecutionIntent(Base):
    """Intenção estável de entrada, persistida ANTES do envio da ordem.

    `intent_id` é a mesma chave enviada ao transport (que é idempotente por
    id: reenvios com o mesmo id não geram nova ordem). Vincula intenção,
    resultado externo (order_id/position_id/result_state) e trade
    (`Trade.intent_id`, único no banco).

    Status:
      - "reserved"  — risco/capital/capacidade reservados antes da aprovação;
      - "planned"   — persistida antes do envio; ordem ainda não confirmada;
      - "confirmed" — entrada confirmada externamente e trade persistido;
      - "partial"   — exposição executada persistida e protegida, porém menor
                      que a solicitada (`filled_quantity`); requer resolução;
      - "unknown"   — desfecho externo desconhecido (timeout/resposta perdida);
                      jamais reenviado automaticamente, requer reconciliação;
      - "failed"    — envio rejeitado ou provadamente nunca enviado.
    """

    __tablename__ = "execution_intents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    intent_id: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    account_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    risk_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    capital_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="planned")

    # Resultado externo do envio
    order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    position_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    result_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Quantidade efetivamente executada conhecida até o momento (parciais)
    filled_quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("idx_execution_intent_account", "account_key"),)


class EntryApproval(Base):
    """Durable manual-entry workflow linked one-to-one to its risk intent."""

    __tablename__ = "entry_approvals"

    approval_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    intent_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("execution_intents.intent_id", ondelete="RESTRICT"),
        nullable=False,
    )
    market: Mapped[str] = mapped_column(String(10), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(5), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    error: Mapped[str | None] = mapped_column(String(250), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("intent_id", name="uq_entry_approvals_intent_id"),
        UniqueConstraint("dedupe_key", name="uq_entry_approvals_dedupe_key"),
        Index("idx_entry_approvals_status_expiry", "status", "expires_at"),
        Index("idx_entry_approvals_lease", "status", "lease_until"),
    )


class RiskAccountLock(Base):
    """Durable row used to serialize risk reservations for one account."""

    __tablename__ = "risk_account_locks"

    account_key: Mapped[str] = mapped_column(String(64), primary_key=True)


class ConfigAuditLog(Base):
    __tablename__ = "config_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    market: Mapped[str | None] = mapped_column(String(10), nullable=True)
    field_name: Mapped[str] = mapped_column(String(50), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    changed_by: Mapped[str] = mapped_column(String(50), nullable=False, default="owner")
