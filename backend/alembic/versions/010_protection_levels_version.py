"""Version desired trade protection levels in PostgreSQL.

Revision ID: 010
Revises: 009
"""
import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column("protection_levels_version", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.execute("""
        CREATE FUNCTION autoxtrade_version_protection_levels() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.stop_loss IS DISTINCT FROM OLD.stop_loss
                OR NEW.take_profit IS DISTINCT FROM OLD.take_profit THEN
                NEW.protection_levels_version := OLD.protection_levels_version + 1;
            ELSE
                NEW.protection_levels_version := OLD.protection_levels_version;
            END IF;
            RETURN NEW;
        END
        $$
    """)
    op.execute("""
        CREATE TRIGGER autoxtrade_version_protection_levels
        BEFORE UPDATE ON trades
        FOR EACH ROW EXECUTE FUNCTION autoxtrade_version_protection_levels()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER autoxtrade_version_protection_levels ON trades")
    op.execute("DROP FUNCTION autoxtrade_version_protection_levels()")
    op.drop_column("trades", "protection_levels_version")
