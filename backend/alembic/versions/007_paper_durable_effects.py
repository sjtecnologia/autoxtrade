"""Persist isolated simulated positions, fencing and effect results.

Revision ID: 007
Revises: 006
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "paper_effect_positions",
        sa.Column("account_key", sa.String(64), primary_key=True),
        sa.Column("position_id", sa.String(100), primary_key=True),
        sa.Column("context", JSONB(), nullable=False),
        sa.Column("side", sa.String(5), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("stop_loss", sa.Numeric(20, 8)),
        sa.Column("take_profit", sa.Numeric(20, 8)),
        sa.Column("fence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("quantity >= 0", name="ck_paper_effect_quantity"),
        sa.CheckConstraint("fence >= 0", name="ck_paper_effect_fence"),
    )
    op.create_table(
        "paper_effect_operations",
        sa.Column("account_key", sa.String(64), primary_key=True),
        sa.Column("position_id", sa.String(100), primary_key=True),
        sa.Column("operation_id", sa.String(100), primary_key=True),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_key", "position_id"],
            ["paper_effect_positions.account_key", "paper_effect_positions.position_id"],
        ),
    )


def downgrade() -> None:
    op.drop_table("paper_effect_operations")
    op.drop_table("paper_effect_positions")
