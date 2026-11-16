"""Persist initial paper protection attempts.

Revision ID: 009
Revises: 008
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_protection_attempts",
        sa.Column("account_key", sa.String(64), primary_key=True),
        sa.Column("position_id", sa.String(100), primary_key=True),
        sa.Column("operation_id", sa.String(100), primary_key=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_key", "position_id"],
            ["paper_effect_positions.account_key", "paper_effect_positions.position_id"],
        ),
    )


def downgrade() -> None:
    op.drop_table("paper_protection_attempts")
