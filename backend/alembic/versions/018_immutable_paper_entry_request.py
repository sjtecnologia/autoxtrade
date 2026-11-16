"""Preserve durable paper entry requests and their captured costs.

Revision ID: 018
Revises: 017
"""
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None

RECEIPT_TRIGGER = """
CREATE OR REPLACE FUNCTION autoxtrade_immutable_paper_close_receipt() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.operation_id LIKE 'manual-close:%' OR OLD.payload->>'kind' = 'protect' THEN
        RAISE EXCEPTION 'Paper protection or close receipt is immutable';
    END IF;
    IF OLD.operation_id LIKE 'entry:%' OR OLD.payload->>'kind' = 'entry' THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Paper entry request is immutable';
        END IF;
        IF NEW.account_key IS DISTINCT FROM OLD.account_key
            OR NEW.position_id IS DISTINCT FROM OLD.position_id
            OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
            OR NEW.fence IS DISTINCT FROM OLD.fence
            OR NEW.payload IS DISTINCT FROM OLD.payload THEN
            RAISE EXCEPTION 'Paper entry request is immutable';
        END IF;
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


def upgrade() -> None:
    op.execute(RECEIPT_TRIGGER)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM paper_effect_operations
                       WHERE operation_id LIKE 'entry:%' OR payload->>'kind' = 'entry') THEN
                RAISE EXCEPTION 'Cannot remove immutability from durable paper entry requests';
            END IF;
        END $$
    """)
    op.execute(PREVIOUS_TRIGGER)
