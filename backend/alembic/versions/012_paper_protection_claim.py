"""Add recoverable leases to paper protection attempts.

Revision ID: 012
Revises: 011
"""
import sqlalchemy as sa
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("paper_protection_attempts", sa.Column("owner", sa.String(64), nullable=True))
    op.add_column("paper_protection_attempts", sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_paper_protection_claim_pair", "paper_protection_attempts",
        "(owner IS NULL) = (lease_until IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_paper_protection_claim_pair", "paper_protection_attempts", type_="check")
    op.drop_column("paper_protection_attempts", "lease_until")
    op.drop_column("paper_protection_attempts", "owner")
