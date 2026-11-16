"""Persist paper safe-break decisions and their strategic activation.

Revision ID: 017
Revises: 016
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None

AUTHORITY_TRIGGER = """
CREATE OR REPLACE FUNCTION autoxtrade_guard_protection_authority() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    decision paper_safe_break_decisions%ROWTYPE;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.version <> 1 OR NEW.stop_loss IS DISTINCT FROM NEW.initial_stop_loss
            OR NEW.take_profit IS DISTINCT FROM NEW.initial_take_profit
            OR NEW.levels_effective_at IS NOT NULL OR NEW.levels_recorded_at IS NOT NULL THEN
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
        OR NEW.levels_effective_at IS DISTINCT FROM OLD.levels_effective_at
        OR NEW.levels_recorded_at IS DISTINCT FROM OLD.levels_recorded_at THEN
        RAISE EXCEPTION 'Protection authority request is immutable';
    END IF;
    IF NEW.stop_loss IS DISTINCT FROM OLD.stop_loss
        OR NEW.take_profit IS DISTINCT FROM OLD.take_profit THEN
        NEW.version := OLD.version + 1;
        NEW.levels_recorded_at := clock_timestamp();
        SELECT * INTO decision FROM paper_safe_break_decisions
            WHERE intent_id = OLD.intent_id AND levels_version = NEW.version;
        IF FOUND THEN
            IF decision.old_stop IS DISTINCT FROM OLD.stop_loss
                OR decision.new_stop IS DISTINCT FROM NEW.stop_loss
                OR decision.take_profit IS DISTINCT FROM OLD.take_profit
                OR decision.take_profit IS DISTINCT FROM NEW.take_profit THEN
                RAISE EXCEPTION 'Safe break decision conflicts with protection authority';
            END IF;
            NEW.levels_effective_at := decision.effective_at;
        ELSE
            NEW.levels_effective_at := NEW.levels_recorded_at;
        END IF;
    ELSE
        NEW.version := OLD.version;
    END IF;
    RETURN NEW;
END
$$
"""

DECISION_TRIGGER = """
CREATE FUNCTION autoxtrade_guard_paper_safe_break_decision() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    trade trades%ROWTYPE;
    authority protection_authorities%ROWTYPE;
    intent execution_intents%ROWTYPE;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Paper safe break decision is immutable';
    END IF;
    SELECT * INTO trade FROM trades WHERE id = NEW.trade_id FOR UPDATE;
    SELECT * INTO authority FROM protection_authorities
        WHERE intent_id = NEW.intent_id FOR UPDATE;
    SELECT * INTO intent FROM execution_intents WHERE intent_id = NEW.intent_id;
    IF trade.id IS NULL OR authority.intent_id IS NULL
        OR intent.intent_id IS NULL OR intent.observation_revision IS DISTINCT FROM NEW.entry_revision
        OR trade.intent_id IS DISTINCT FROM NEW.intent_id
        OR trade.protection_authority_id IS DISTINCT FROM NEW.intent_id
        OR trade.status <> 'open' OR trade.mode <> 'paper'
        OR NEW.levels_version <> authority.version + 1
        OR trade.protection_levels_version <> authority.version
        OR NEW.old_stop IS DISTINCT FROM trade.stop_loss
        OR NEW.old_stop IS DISTINCT FROM authority.stop_loss
        OR NEW.entry_price IS DISTINCT FROM trade.entry_price
        OR NEW.exposure_quantity IS DISTINCT FROM trade.quantity
        OR NEW.take_profit IS DISTINCT FROM trade.take_profit
        OR NEW.take_profit IS DISTINCT FROM authority.take_profit
        OR (trade.side = 'buy' AND (NEW.new_stop < trade.entry_price OR NEW.new_stop <= NEW.old_stop))
        OR (trade.side = 'sell' AND (NEW.new_stop > trade.entry_price OR NEW.new_stop >= NEW.old_stop))
        OR trade.side NOT IN ('buy', 'sell')
        OR NEW.previous_cursor IS DISTINCT FROM trade.paper_monitor_candle_at
        OR NEW.previous_timeframe IS DISTINCT FROM trade.paper_monitor_timeframe
        OR NEW.interval_seconds <= 0
        OR NEW.effective_at IS DISTINCT FROM NEW.candle_at + NEW.interval_seconds * interval '1 second'
        OR (NEW.previous_cursor IS NOT NULL
            AND NEW.candle_at IS DISTINCT FROM NEW.previous_cursor + NEW.interval_seconds * interval '1 second')
    THEN
        RAISE EXCEPTION 'Invalid paper safe break decision';
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


def upgrade() -> None:
    op.add_column("protection_authorities", sa.Column("levels_recorded_at", sa.DateTime(timezone=True)))
    op.create_table(
        "paper_safe_break_decisions",
        sa.Column("trade_id", sa.BigInteger(), sa.ForeignKey("trades.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("levels_version", sa.BigInteger(), primary_key=True),
        sa.Column("intent_id", sa.String(32), sa.ForeignKey("protection_authorities.intent_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("candle_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("previous_cursor", sa.DateTime(timezone=True)),
        sa.Column("previous_timeframe", sa.String(10)),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("old_stop", sa.Numeric(20, 8), nullable=False),
        sa.Column("new_stop", sa.Numeric(20, 8), nullable=False),
        sa.Column("take_profit", sa.Numeric(20, 8)),
        sa.Column("entry_price", sa.Numeric(20, 8), nullable=False),
        sa.Column("exposure_quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_revision", sa.BigInteger(), nullable=False),
        sa.Column("candle", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.clock_timestamp()),
        sa.UniqueConstraint("intent_id", "levels_version", name="uq_paper_safe_break_authority_version"),
    )
    op.execute(DECISION_TRIGGER)
    op.execute("""
        CREATE TRIGGER autoxtrade_guard_paper_safe_break_decision
        BEFORE INSERT OR UPDATE OR DELETE ON paper_safe_break_decisions FOR EACH ROW
        EXECUTE FUNCTION autoxtrade_guard_paper_safe_break_decision()
    """)
    op.execute(AUTHORITY_TRIGGER)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM paper_safe_break_decisions) THEN
                RAISE EXCEPTION 'Cannot discard durable paper safe break decisions';
            END IF;
        END $$
    """)
    op.execute(PREVIOUS_AUTHORITY_TRIGGER)
    op.drop_table("paper_safe_break_decisions")
    op.execute("DROP FUNCTION autoxtrade_guard_paper_safe_break_decision()")
    op.drop_column("protection_authorities", "levels_recorded_at")
