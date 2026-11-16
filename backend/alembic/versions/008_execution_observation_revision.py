"""Persist the source revision used when reconciling partial entries.

Revision ID: 008
Revises: 007
"""
import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("execution_intents", sa.Column("observation_revision", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("execution_intents", "observation_revision")
