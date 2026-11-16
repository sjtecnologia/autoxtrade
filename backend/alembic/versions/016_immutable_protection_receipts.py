"""Persist versioned level activation and make protection receipts immutable.

Revision ID: 016
Revises: 015
"""
from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None

AUTHORITY_TRIGGER = """
CREATE OR REPLACE FUNCTION autoxtrade_guard_protection_authority() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 OR NEW.stop_loss IS DISTINCT FROM NEW.initial_stop_loss
            OR NEW.take_profit IS DISTINCT FROM NEW.initial_take_profit
            OR NEW.levels_effective_at IS NOT NULL THEN
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
        OR NEW.initial_take_profit IS DISTINCT FROM OLD.initial_take_profit
        OR NEW.levels_effective_at IS DISTINCT FROM OLD.levels_effective_at THEN
        RAISE EXCEPTION 'Protection authority request is immutable';
    END IF;
    IF NEW.stop_loss IS DISTINCT FROM OLD.stop_loss
        OR NEW.take_profit IS DISTINCT FROM OLD.take_profit THEN
        NEW.version := OLD.version + 1;
        NEW.levels_effective_at := clock_timestamp();
    ELSE
        NEW.version := OLD.version;
    END IF;
    RETURN NEW;
END
$$
"""

PREVIOUS_AUTHORITY_TRIGGER = """
CREATE OR REPLACE FUNCTION autoxtrade_guard_protection_authority() RETURNS trigger
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

RECEIPT_TRIGGER = """
CREATE OR REPLACE FUNCTION autoxtrade_immutable_paper_close_receipt() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.operation_id LIKE 'manual-close:%' OR OLD.payload->>'kind' = 'protect' THEN
        RAISE EXCEPTION 'Paper protection or close receipt is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
"""

PREVIOUS_TRIGGER = """
CREATE OR REPLACE FUNCTION autoxtrade_immutable_paper_close_receipt() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.operation_id LIKE 'manual-close:%' THEN
        RAISE EXCEPTION 'Paper close receipt is immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.add_column(
        "protection_authorities", sa.Column("levels_effective_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(AUTHORITY_TRIGGER)
    op.execute(RECEIPT_TRIGGER)


def downgrade() -> None:
    op.execute(PREVIOUS_TRIGGER)
    op.execute(PREVIOUS_AUTHORITY_TRIGGER)
    op.drop_column("protection_authorities", "levels_effective_at")
