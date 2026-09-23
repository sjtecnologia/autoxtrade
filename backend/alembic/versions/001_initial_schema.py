"""initial schema

Revision ID: 001
Revises: 
Create Date: 2026-04-28 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- ml_models (sem FK dependência) ---
    op.create_table(
        "ml_models",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=True),
        sa.Column("model_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="inactive"),
        sa.Column("file_path", sa.String(255), nullable=False),
        sa.Column("scaler_path", sa.String(255), nullable=True),
        sa.Column("feature_list", postgresql.JSONB(), nullable=False),
        sa.Column("hyperparams", postgresql.JSONB(), nullable=False),
        sa.Column("train_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("train_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("train_samples", sa.Integer(), nullable=False),
        sa.Column("bt_profit_factor", sa.Numeric(8, 4), nullable=True),
        sa.Column("bt_sharpe_ratio", sa.Numeric(8, 4), nullable=True),
        sa.Column("bt_win_rate", sa.Numeric(6, 4), nullable=True),
        sa.Column("bt_max_drawdown", sa.Numeric(6, 4), nullable=True),
        sa.Column("bt_total_trades", sa.Integer(), nullable=True),
        sa.Column("bt_total_return", sa.Numeric(10, 4), nullable=True),
        sa.Column("ml_accuracy", sa.Numeric(6, 4), nullable=True),
        sa.Column("ml_precision_long", sa.Numeric(6, 4), nullable=True),
        sa.Column("ml_recall_long", sa.Numeric(6, 4), nullable=True),
        sa.Column("ml_precision_short", sa.Numeric(6, 4), nullable=True),
        sa.Column("ml_recall_short", sa.Numeric(6, 4), nullable=True),
        sa.Column("is_approved", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trained_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_index("idx_models_market", "ml_models", ["market"])
    op.create_index("idx_models_status", "ml_models", ["status"])
    op.create_index("idx_models_version", "ml_models", ["version"])

    # --- bot_config ---
    op.create_table(
        "bot_config",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("mode", sa.String(10), nullable=False, server_default="paper"),
        sa.Column("active_model_id", sa.BigInteger(), sa.ForeignKey("ml_models.id"), nullable=True),
        sa.Column("exchange", sa.String(30), nullable=False),
        sa.Column("enabled_symbols", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("risk_per_trade_pct", sa.Numeric(5, 2), nullable=False, server_default="1.0"),
        sa.Column("max_open_trades", sa.SmallInteger(), nullable=False, server_default="3"),
        sa.Column("max_drawdown_pct", sa.Numeric(5, 2), nullable=False, server_default="15.0"),
        sa.Column("drawdown_alert_pct", sa.Numeric(5, 2), nullable=False, server_default="10.0"),
        sa.Column("max_corr_threshold", sa.Numeric(4, 2), nullable=False, server_default="0.70"),
        sa.Column("min_ml_confidence", sa.Numeric(4, 2), nullable=False, server_default="0.60"),
        sa.Column("current_drawdown_1d", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("current_drawdown_30d", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("peak_capital", sa.Numeric(20, 2), nullable=True),
        sa.Column("drawdown_status", sa.String(10), nullable=False, server_default="OK"),
        sa.Column("paused_reason", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market"),
    )

    # --- trades ---
    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("exchange", sa.String(30), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(5), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False, server_default="live"),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_value", sa.Numeric(20, 2), nullable=False),
        sa.Column("stop_loss", sa.Numeric(20, 8), nullable=False),
        sa.Column("take_profit", sa.Numeric(20, 8), nullable=True),
        sa.Column("open_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_order_id", sa.String(100), nullable=True),
        sa.Column("exit_price", sa.Numeric(20, 8), nullable=True),
        sa.Column("exit_value", sa.Numeric(20, 2), nullable=True),
        sa.Column("close_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_order_id", sa.String(100), nullable=True),
        sa.Column("close_reason", sa.String(20), nullable=True),
        sa.Column("pnl_gross", sa.Numeric(20, 2), nullable=True),
        sa.Column("commission", sa.Numeric(20, 2), server_default="0"),
        sa.Column("pnl_net", sa.Numeric(20, 2), nullable=True),
        sa.Column("pnl_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("duration_sec", sa.Integer(), nullable=True),
        sa.Column("model_version", sa.String(50), nullable=True),
        sa.Column("ml_signal", sa.String(10), nullable=True),
        sa.Column("ml_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("market_regime", sa.String(20), nullable=True),
        sa.Column("risk_amount", sa.Numeric(20, 2), nullable=True),
        sa.Column("risk_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_trades_market", "trades", ["market"])
    op.create_index("idx_trades_symbol", "trades", ["symbol"])
    op.create_index("idx_trades_status", "trades", ["status"])
    op.create_index("idx_trades_open_at", "trades", ["open_at"])
    op.create_index("idx_trades_mode", "trades", ["mode"])
    op.create_index("idx_trades_model", "trades", ["model_version"])

    # --- trade_events ---
    op.create_table(
        "trade_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "trade_id",
            sa.BigInteger(),
            sa.ForeignKey("trades.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_events_trade_id", "trade_events", ["trade_id"])
    op.create_index("idx_events_type", "trade_events", ["event_type"])
    op.create_index("idx_events_occurred", "trade_events", ["occurred_at"])

    # --- equity_snapshots ---
    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market", sa.String(10), nullable=True),
        sa.Column("equity", sa.Numeric(20, 2), nullable=False),
        sa.Column("open_pnl", sa.Numeric(20, 2), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(20, 2), nullable=False, server_default="0"),
        sa.Column("drawdown_1d", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("drawdown_30d", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("open_trades", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_equity_snapshot_at", "equity_snapshots", ["snapshot_at"])
    op.create_index("idx_equity_market", "equity_snapshots", ["market", "snapshot_at"])

    # --- config_audit_log ---
    op.create_table(
        "config_audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("market", sa.String(10), nullable=True),
        sa.Column("field_name", sa.String(50), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("changed_by", sa.String(50), nullable=False, server_default="owner"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("config_audit_log")
    op.drop_table("equity_snapshots")
    op.drop_index("idx_events_occurred")
    op.drop_index("idx_events_type")
    op.drop_index("idx_events_trade_id")
    op.drop_table("trade_events")
    op.drop_index("idx_trades_model")
    op.drop_index("idx_trades_mode")
    op.drop_index("idx_trades_open_at")
    op.drop_index("idx_trades_status")
    op.drop_index("idx_trades_symbol")
    op.drop_index("idx_trades_market")
    op.drop_table("trades")
    op.drop_table("bot_config")
    op.drop_index("idx_models_version")
    op.drop_index("idx_models_status")
    op.drop_index("idx_models_market")
    op.drop_table("ml_models")
