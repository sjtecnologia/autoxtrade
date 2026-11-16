"""Persist paper entry execution increments.

Revision ID: 015
Revises: 014
"""
import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None

IMMUTABLE_FUNCTION = """
CREATE FUNCTION autoxtrade_immutable_paper_entry_fill() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Paper entry fill is immutable';
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "paper_entry_fills",
        sa.Column("account_key", sa.String(64), primary_key=True),
        sa.Column("position_id", sa.String(100), primary_key=True),
        sa.Column("operation_id", sa.String(100), primary_key=True),
        sa.Column("revision", sa.BigInteger(), primary_key=True),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("cumulative_quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("clock_timestamp()")),
        sa.CheckConstraint("revision > 0 AND quantity > 0 AND cumulative_quantity >= quantity",
                           name="ck_paper_entry_fill_values"),
        sa.ForeignKeyConstraint(
            ["account_key", "position_id", "operation_id"],
            ["paper_effect_operations.account_key", "paper_effect_operations.position_id",
             "paper_effect_operations.operation_id"],
        ),
    )
    op.execute(IMMUTABLE_FUNCTION)
    op.execute("""
        CREATE TRIGGER autoxtrade_immutable_paper_entry_fill
        BEFORE UPDATE OR DELETE ON paper_entry_fills FOR EACH ROW
        EXECUTE FUNCTION autoxtrade_immutable_paper_entry_fill()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER autoxtrade_immutable_paper_entry_fill ON paper_entry_fills")
    op.execute("DROP FUNCTION autoxtrade_immutable_paper_entry_fill()")
    op.drop_table("paper_entry_fills")
