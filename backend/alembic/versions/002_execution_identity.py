"""Preserve legacy mode/exchange; missing account identity remains unresolved.

Revision ID: 002
Revises: 001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "002"
down_revision = "001"
branch_labels = depends_on = None


def upgrade():
    op.add_column("trades", sa.Column("account_id", sa.String(100), nullable=True))
    op.add_column("trades", sa.Column("account_nature", sa.String(20), nullable=False, server_default="unknown"))
    op.add_column("trades", sa.Column("instrument_class", sa.String(30), nullable=True))
    op.add_column("trades", sa.Column("position_id", sa.String(100), nullable=True))
    op.add_column("trades", sa.Column("execution_context", JSONB(), nullable=True))
    op.add_column("trades", sa.Column("identity_status", sa.String(30), nullable=False, server_default="unresolved"))
    op.add_column("bot_config", sa.Column("execution_context", JSONB(), nullable=True))
    # Do not infer account, venue, or demo/live nature from historical labels.


def downgrade():
    op.drop_column("bot_config", "execution_context")
    for name in ("identity_status", "execution_context", "position_id", "instrument_class", "account_nature", "account_id"):
        op.drop_column("trades", name)
