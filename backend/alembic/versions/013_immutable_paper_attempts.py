"""Prevent changes to persisted paper operation decisions.

Revision ID: 013
Revises: 012
"""
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None

ATTEMPT_TRIGGER = """
CREATE FUNCTION autoxtrade_immutable_paper_attempt() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Paper operation decision is immutable';
    END IF;
    IF NEW.account_key IS DISTINCT FROM OLD.account_key
        OR NEW.position_id IS DISTINCT FROM OLD.position_id
        OR NEW.operation_id IS DISTINCT FROM OLD.operation_id
        OR NEW.payload IS DISTINCT FROM OLD.payload
        OR NEW.revision IS DISTINCT FROM OLD.revision THEN
        RAISE EXCEPTION 'Paper operation decision is immutable';
    END IF;
    RETURN NEW;
END
$$
"""

RECEIPT_TRIGGER = """
CREATE FUNCTION autoxtrade_immutable_paper_close_receipt() RETURNS trigger
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
    op.execute(ATTEMPT_TRIGGER)
    op.execute("""
        CREATE TRIGGER autoxtrade_immutable_paper_attempt
        BEFORE UPDATE OR DELETE ON paper_protection_attempts FOR EACH ROW
        EXECUTE FUNCTION autoxtrade_immutable_paper_attempt()
    """)
    op.execute(RECEIPT_TRIGGER)
    op.execute("""
        CREATE TRIGGER autoxtrade_immutable_paper_close_receipt
        BEFORE UPDATE OR DELETE ON paper_effect_operations FOR EACH ROW
        EXECUTE FUNCTION autoxtrade_immutable_paper_close_receipt()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER autoxtrade_immutable_paper_close_receipt ON paper_effect_operations")
    op.execute("DROP FUNCTION autoxtrade_immutable_paper_close_receipt()")
    op.execute("DROP TRIGGER autoxtrade_immutable_paper_attempt ON paper_protection_attempts")
    op.execute("DROP FUNCTION autoxtrade_immutable_paper_attempt()")
