"""Persist confirmed entry-order terminality.

Revision ID: 006
Revises: 005
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("execution_intents", sa.Column("order_terminal", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("execution_intents", "order_terminal")
