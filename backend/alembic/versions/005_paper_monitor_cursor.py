"""Persist the last evaluated paper-monitor candle.

Revision ID: 005
Revises: 004
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("paper_monitor_candle_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trades", sa.Column("paper_monitor_timeframe", sa.String(10), nullable=True))


def downgrade() -> None:
    op.drop_column("trades", "paper_monitor_timeframe")
    op.drop_column("trades", "paper_monitor_candle_at")
