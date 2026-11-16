"""Record market evidence without changing protection attempts.

Revision ID: 014
Revises: 013
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None

IMMUTABLE_FUNCTION = """
CREATE FUNCTION autoxtrade_immutable_protection_evidence() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Paper protection evidence is immutable';
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "paper_protection_evidence",
        sa.Column("account_key", sa.String(64), primary_key=True),
        sa.Column("position_id", sa.String(100), primary_key=True),
        sa.Column("operation_id", sa.String(100), primary_key=True),
        sa.Column("evidence_id", sa.String(64), primary_key=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("assessment", JSONB(), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["account_key", "position_id", "operation_id"],
            ["paper_protection_attempts.account_key", "paper_protection_attempts.position_id",
             "paper_protection_attempts.operation_id"],
        ),
    )
    op.execute(IMMUTABLE_FUNCTION)
    op.execute("""
        CREATE TRIGGER autoxtrade_immutable_protection_evidence
        BEFORE UPDATE OR DELETE ON paper_protection_evidence FOR EACH ROW
        EXECUTE FUNCTION autoxtrade_immutable_protection_evidence()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER autoxtrade_immutable_protection_evidence ON paper_protection_evidence")
    op.execute("DROP FUNCTION autoxtrade_immutable_protection_evidence()")
    op.drop_table("paper_protection_evidence")
