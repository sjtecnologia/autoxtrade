"""Persist desired protection before entry.

Revision ID: 011
Revises: 010
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None

AUTHORITY_TRIGGER = """
CREATE FUNCTION autoxtrade_guard_protection_authority() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 OR NEW.stop_loss IS DISTINCT FROM NEW.initial_stop_loss
            OR NEW.take_profit IS DISTINCT FROM NEW.initial_take_profit THEN
            RAISE EXCEPTION 'Invalid initial protection authority';
        END IF;
        IF EXISTS (SELECT 1 FROM trades WHERE intent_id = NEW.intent_id) THEN
            RAISE EXCEPTION 'Cannot invent protection authority for existing trade';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Protection authority cannot be deleted';
    END IF;
    IF pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'Protection authority must be updated through its linked trade';
    END IF;
    IF NEW.intent_id IS DISTINCT FROM OLD.intent_id
        OR NEW.execution_context IS DISTINCT FROM OLD.execution_context
        OR NEW.side IS DISTINCT FROM OLD.side
        OR NEW.quantity IS DISTINCT FROM OLD.quantity
        OR NEW.entry_price IS DISTINCT FROM OLD.entry_price
        OR NEW.initial_stop_loss IS DISTINCT FROM OLD.initial_stop_loss
        OR NEW.initial_take_profit IS DISTINCT FROM OLD.initial_take_profit THEN
        RAISE EXCEPTION 'Protection authority request is immutable';
    END IF;
    IF NEW.stop_loss IS DISTINCT FROM OLD.stop_loss
        OR NEW.take_profit IS DISTINCT FROM OLD.take_profit THEN
        NEW.version := OLD.version + 1;
    ELSE
        NEW.version := OLD.version;
    END IF;
    RETURN NEW;
END
$$
"""

TRADE_TRIGGER = """
CREATE OR REPLACE FUNCTION autoxtrade_version_protection_levels() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    authority protection_authorities%ROWTYPE;
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.protection_authority_id IS DISTINCT FROM OLD.protection_authority_id THEN
        RAISE EXCEPTION 'Protection authority binding is immutable';
    END IF;
    IF NEW.protection_authority_id IS NULL THEN
        IF NEW.intent_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM protection_authorities WHERE intent_id = NEW.intent_id
        ) THEN
            RAISE EXCEPTION 'Trade must bind its protection authority';
        END IF;
        IF TG_OP = 'UPDATE' THEN
            IF NEW.stop_loss IS DISTINCT FROM OLD.stop_loss
                OR NEW.take_profit IS DISTINCT FROM OLD.take_profit THEN
                NEW.protection_levels_version := OLD.protection_levels_version + 1;
            ELSE
                NEW.protection_levels_version := OLD.protection_levels_version;
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    SELECT * INTO authority FROM protection_authorities
        WHERE intent_id = NEW.protection_authority_id FOR UPDATE;
    IF NOT FOUND OR NEW.intent_id IS DISTINCT FROM authority.intent_id
        OR NEW.side IS DISTINCT FROM authority.side
        OR NEW.market IS DISTINCT FROM authority.execution_context->>'market'
        OR NEW.exchange IS DISTINCT FROM authority.execution_context->>'venue'
        OR NEW.symbol IS DISTINCT FROM authority.execution_context->'instrument'->>'symbol'
        OR NEW.mode IS DISTINCT FROM authority.execution_context->>'mode'
        OR NEW.account_id IS DISTINCT FROM authority.execution_context->>'account_id'
        OR NEW.account_nature IS DISTINCT FROM authority.execution_context->>'nature'
        OR NEW.instrument_class IS DISTINCT FROM authority.execution_context->'instrument'->>'asset_class'
        OR NEW.identity_status IS DISTINCT FROM 'bound'
        OR NEW.execution_context - 'paper_costs' IS DISTINCT FROM authority.execution_context THEN
        RAISE EXCEPTION 'Protection authority identity conflict';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.stop_loss IS DISTINCT FROM authority.stop_loss
            OR NEW.take_profit IS DISTINCT FROM authority.take_profit THEN
            RAISE EXCEPTION 'Protection authority levels conflict';
        END IF;
        NEW.protection_levels_version := authority.version;
    ELSE
        UPDATE protection_authorities SET stop_loss = NEW.stop_loss, take_profit = NEW.take_profit
            WHERE intent_id = authority.intent_id RETURNING version INTO NEW.protection_levels_version;
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.create_table(
        "protection_authorities",
        sa.Column("intent_id", sa.String(32), sa.ForeignKey("execution_intents.intent_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("execution_context", JSONB(), nullable=False),
        sa.Column("side", sa.String(5), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("initial_stop_loss", sa.Numeric(20, 8), nullable=False),
        sa.Column("initial_take_profit", sa.Numeric(20, 8)),
        sa.Column("stop_loss", sa.Numeric(20, 8), nullable=False),
        sa.Column("take_profit", sa.Numeric(20, 8)),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.add_column("trades", sa.Column("protection_authority_id", sa.String(32), nullable=True))
    op.create_foreign_key("fk_trade_protection_authority", "trades", "protection_authorities",
                          ["protection_authority_id"], ["intent_id"], ondelete="RESTRICT")
    op.execute(AUTHORITY_TRIGGER)
    op.execute("""
        CREATE TRIGGER autoxtrade_guard_protection_authority
        BEFORE INSERT OR UPDATE OR DELETE ON protection_authorities FOR EACH ROW
        EXECUTE FUNCTION autoxtrade_guard_protection_authority()
    """)
    op.execute(TRADE_TRIGGER)
    op.execute("DROP TRIGGER autoxtrade_version_protection_levels ON trades")
    op.execute("""
        CREATE TRIGGER autoxtrade_version_protection_levels
        BEFORE INSERT OR UPDATE ON trades FOR EACH ROW
        EXECUTE FUNCTION autoxtrade_version_protection_levels()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER autoxtrade_version_protection_levels ON trades")
    op.execute("""
        CREATE OR REPLACE FUNCTION autoxtrade_version_protection_levels() RETURNS trigger
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
        BEFORE UPDATE ON trades FOR EACH ROW
        EXECUTE FUNCTION autoxtrade_version_protection_levels()
    """)
    op.execute("DROP TRIGGER autoxtrade_guard_protection_authority ON protection_authorities")
    op.execute("DROP FUNCTION autoxtrade_guard_protection_authority()")
    op.drop_constraint("fk_trade_protection_authority", "trades", type_="foreignkey")
    op.drop_column("trades", "protection_authority_id")
    op.drop_table("protection_authorities")
