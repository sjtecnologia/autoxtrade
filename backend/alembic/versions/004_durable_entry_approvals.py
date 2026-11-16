"""Persist manual entry approvals and recoverable worker claims

Revision ID: 004
Revises: 003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "entry_approvals",
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("intent_id", sa.String(32), nullable=False),
        sa.Column("market", sa.String(10), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(5), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("dedupe_key", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.String(250), nullable=True),
        sa.Column("claim_token", sa.String(32), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["execution_intents.intent_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("approval_id"),
        sa.UniqueConstraint("intent_id", name="uq_entry_approvals_intent_id"),
        sa.UniqueConstraint("dedupe_key", name="uq_entry_approvals_dedupe_key"),
    )
    op.create_index(
        "idx_entry_approvals_status_expiry",
        "entry_approvals",
        ["status", "expires_at"],
    )
    op.create_index(
        "idx_entry_approvals_lease",
        "entry_approvals",
        ["status", "lease_until"],
    )


def downgrade() -> None:
    op.drop_index("idx_entry_approvals_lease", table_name="entry_approvals")
    op.drop_index("idx_entry_approvals_status_expiry", table_name="entry_approvals")
    op.drop_table("entry_approvals")
