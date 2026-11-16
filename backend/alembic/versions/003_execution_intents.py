"""Atomic account risk reservations and execution intents

Revision ID: 003
Revises: 002
Create Date: 2026-11-11 00:00:00.000000

Reservations persist account capacity, risk, and capital before any order I/O.
Execution intents remain bound uniquely to trades; nullable intent IDs preserve
legacy trades without an intent.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- execution_intents ---
    op.create_table(
        "execution_intents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("intent_id", sa.String(32), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("account_key", sa.String(64), nullable=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(5), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("risk_amount", sa.Numeric(20, 2), nullable=True),
        sa.Column("capital_amount", sa.Numeric(20, 2), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("order_id", sa.String(100), nullable=True),
        sa.Column("position_id", sa.String(100), nullable=True),
        sa.Column("result_state", sa.String(20), nullable=True),
        sa.Column("filled_quantity", sa.Numeric(20, 8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("intent_id"),
    )
    op.create_index("idx_execution_intent_account", "execution_intents", ["account_key"])

    # --- trades.intent_id (vínculo único intent ↔ trade) ---
    op.add_column("trades", sa.Column("intent_id", sa.String(32), nullable=True))
    op.add_column("trades", sa.Column("capital_amount", sa.Numeric(20, 2), nullable=True))
    op.create_index("idx_trades_intent", "trades", ["intent_id"], unique=True)

    op.create_table(
        "risk_account_locks",
        sa.Column("account_key", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("account_key"),
    )


def downgrade() -> None:
    op.drop_table("risk_account_locks")
    op.drop_index("idx_trades_intent", table_name="trades")
    op.drop_column("trades", "capital_amount")
    op.drop_column("trades", "intent_id")
    op.drop_index("idx_execution_intent_account", table_name="execution_intents")
    op.drop_table("execution_intents")
