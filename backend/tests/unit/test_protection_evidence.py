from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from paper.protection_evidence import (
    Candle, CoveredInterval, EvidenceState, FillActivation, MarketReference,
    ProtectionEvidence, assess_protection_evidence,
)
from trading.connectors.types import OrderSide
from trading.contracts import ExecutionIncrement
from trading.didi_strategy import evaluate_safe_break


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def candle(hour, *, low="101", high="105", close="103", opening="102"):
    return Candle(START + hour * HOUR, D(opening), D(high), D(low), D(close))


def evidence(side=OrderSide.BUY):
    stop = D("100.01") if side == OrderSide.BUY else D("99.99")
    if side == OrderSide.BUY:
        decision = candle(0, low="90", high="105")
        history = (candle(1), candle(2))
        reference = D("103")
    else:
        decision = candle(0, low="95", high="110", opening="98", close="97")
        history = (candle(1, low="95", high="98", opening="97", close="96"),
                   candle(2, low="95", high="98", opening="97", close="96"))
        reference = D("96")
    fill = ExecutionIncrement("entry:synthetic:1", 1, D("1"), D("1"), START + HOUR)
    return ProtectionEvidence(
        side=side, executed_average=D("100"), cost_adjusted_entry=stop, desired_stop=stop,
        reference=MarketReference(reference, START + 3 * HOUR, "synthetic-feed"),
        decision_candle=decision, stop_effective_at=START + HOUR, history=history,
        timeframe="1h", calendar="24/7", history_source="synthetic-feed",
        executions=(FillActivation(fill, START + HOUR),),
        execution_history_complete=True, exposure_quantity=D("1"),
    )


def check(item, *, now=START + 3 * HOUR, max_age=HOUR):
    return assess_protection_evidence(item, now=now, max_age=max_age).state


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_cost_adjusted_safe_break_ignores_pre_activation_extremes(side):
    item = evidence(side)
    initial_stop = D("99") if side == OrderSide.BUY else D("101")
    safe = evaluate_safe_break(side=side.value, entry_price=item.cost_adjusted_entry,
                               stop_loss=initial_stop, current_price=item.reference.price)
    assert safe.should_move and safe.new_stop_loss == item.desired_stop
    assert item.executed_average != item.cost_adjusted_entry
    assert check(item) == EvidenceState.VALID


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_cross_after_activation(side):
    item = evidence(side)
    crossed = (candle(1, low="99") if side == OrderSide.BUY else
               candle(1, low="95", high="101", opening="97", close="96"))
    assert check(replace(item, history=(crossed, item.history[1]))) == EvidenceState.STOP_CROSSED


def test_absent_and_stale_reference():
    item = evidence()
    assert check(replace(item, reference=None)) == EvidenceState.MISSING
    assert check(item, now=START + 5 * HOUR) == EvidenceState.STALE
    assert check(replace(item, stop_effective_at=None)) == EvidenceState.MISSING


@pytest.mark.parametrize("change", [
    lambda x: replace(x, history=x.history[1:]),
    lambda x: replace(x, history=(x.history[1], x.history[0])),
    lambda x: replace(x, history=(x.history[0], x.history[0], x.history[1])),
    lambda x: replace(x, history=x.history[:1]),
    lambda x: replace(x, calendar="exchange-calendar"),
    lambda x: replace(x, timeframe="unknown"),
    lambda x: replace(x, timeframe=""),
    lambda x: replace(x, history_source="other-source"),
    lambda x: replace(x, reference=replace(x.reference, price=D("104"))),
    lambda x: replace(x, stop_effective_at=START + HOUR + timedelta(minutes=5)),
    lambda x: replace(x, stop_effective_at=START),
    lambda x: replace(x, stop_effective_at=START + 4 * HOUR),
    lambda x: replace(x, history=(replace(x.history[0], low=D("104")), x.history[1])),
    lambda x: replace(x, reference=replace(x.reference, observed_at=START + 4 * HOUR)),
    lambda x: replace(x, decision_candle=replace(x.decision_candle, opened_at=START.replace(tzinfo=None))),
    lambda x: replace(x, decision_candle=replace(x.decision_candle, opened_at=START + timedelta(minutes=5))),
    lambda x: replace(x, reference=replace(x.reference, source="")),
    lambda x: replace(x, executed_average=D("NaN")),
    lambda x: replace(x, cost_adjusted_entry=D("0")),
    lambda x: replace(x, history=(replace(x.history[0], high=D("NaN")), x.history[1])),
])
def test_ambiguous_evidence_is_never_valid(change):
    assert check(change(evidence())) == EvidenceState.INCOMPLETE


def test_stop_only_applies_from_next_selected_boundary():
    item = evidence()
    crossed_before = candle(1, low="99")
    assert check(replace(item, stop_effective_at=START + 2 * HOUR,
                         executions=(replace(item.executions[0], effective_at=START + 2 * HOUR),),
                         history=(crossed_before, item.history[1]))) == EvidenceState.VALID
    assert check(replace(item, stop_effective_at=START + HOUR,
                         history=(crossed_before, item.history[1]))) == EvidenceState.STOP_CROSSED


def test_unclosed_candle_never_counts_as_history():
    item = evidence()
    assert check(item, now=START + 2 * HOUR + timedelta(minutes=30)) == EvidenceState.INCOMPLETE


def intrabar(side=OrderSide.BUY):
    item = evidence(side)
    executed_at = START + HOUR + timedelta(minutes=20)
    fill = ExecutionIncrement("entry:synthetic:1", 1, D("1"), D("1"), executed_at)
    interval = CoveredInterval(fill.execution_id, executed_at, START + 2 * HOUR,
                               D("101") if side == OrderSide.BUY else D("95"),
                               D("105") if side == OrderSide.BUY else D("98"), "synthetic-feed")
    first = (candle(1, low="99") if side == OrderSide.BUY else
             candle(1, low="95", high="101", opening="97", close="96"))
    return replace(item, stop_effective_at=executed_at, history=(first, item.history[1]),
                   executions=(FillActivation(fill, executed_at),),
                   execution_history_complete=True, exposure_quantity=D("1"), intervals=(interval,))


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_intrabar_touch_before_fill_does_not_count(side):
    assert check(intrabar(side)) == EvidenceState.VALID


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_intrabar_touch_after_fill_counts_for_execution(side):
    item = intrabar(side)
    interval = (replace(item.intervals[0], low=D("99")) if side == OrderSide.BUY
                else replace(item.intervals[0], high=D("101")))
    result = assess_protection_evidence(replace(item, intervals=(interval,)),
                                        now=START + 3 * HOUR, max_age=HOUR)
    assert result.state == EvidenceState.STOP_CROSSED
    assert result.crossed_execution_ids == ("entry:synthetic:1",)


def test_activation_inside_decision_candle_requires_granular_coverage():
    item = evidence()
    fill_time = START + timedelta(minutes=20)
    fill = replace(item.executions[0].execution, executed_at=fill_time)
    interval = CoveredInterval(fill.execution_id, fill_time, START + HOUR,
                               D("101"), D("105"), "synthetic-feed")
    item = replace(item, stop_effective_at=fill_time,
                   executions=(FillActivation(fill, fill_time),), intervals=(interval,))
    assert check(item) == EvidenceState.VALID
    assert check(replace(item, intervals=())) == EvidenceState.INCOMPLETE
    assert check(replace(item, intervals=(replace(interval, low=D("99")),))) == EvidenceState.STOP_CROSSED


@pytest.mark.parametrize("change", [
    lambda x: replace(x, intervals=()),
    lambda x: replace(x, intervals=(replace(x.intervals[0],
        started_at=x.intervals[0].started_at + timedelta(microseconds=1)),)),
    lambda x: replace(x, intervals=(replace(x.intervals[0],
        ended_at=x.intervals[0].ended_at - timedelta(microseconds=1)),)),
    lambda x: replace(x, intervals=(replace(x.intervals[0], source="another-feed"),)),
    lambda x: replace(x, intervals=(replace(x.intervals[0], execution_id="other"),)),
    lambda x: replace(x, intervals=(x.intervals[0], x.intervals[0])),
    lambda x: replace(x, intervals=(replace(x.intervals[0], low=D("1")),)),
    lambda x: replace(x, execution_history_complete=False),
    lambda x: replace(x, execution_history_complete=None),
    lambda x: replace(x, exposure_quantity=D("2")),
    lambda x: replace(x, executions=(replace(x.executions[0],
        effective_at=x.executions[0].effective_at - timedelta(microseconds=1)),)),
])
def test_intrabar_requires_proven_contiguous_coverage(change):
    assert check(change(intrabar())) == EvidenceState.INCOMPLETE


def test_granular_intervals_must_cover_without_gaps_or_overlaps():
    item = intrabar()
    segment = item.intervals[0]
    middle = segment.started_at + timedelta(minutes=10)
    first, second = replace(segment, ended_at=middle), replace(segment, started_at=middle)
    assert check(replace(item, intervals=(first, second))) == EvidenceState.VALID
    assert check(replace(item, intervals=(first, replace(second, started_at=middle + timedelta(microseconds=1))))) == EvidenceState.INCOMPLETE
    assert check(replace(item, intervals=(first, replace(second, started_at=middle - timedelta(microseconds=1))))) == EvidenceState.INCOMPLETE
    assert check(replace(item, intervals=(second, first))) == EvidenceState.INCOMPLETE


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
def test_partial_fills_have_independent_activation_and_complete_coverage(side):
    item = intrabar(side)
    first_time = item.executions[0].effective_at
    second_time = START + 2 * HOUR + timedelta(minutes=10)
    first = ExecutionIncrement("entry:synthetic:1", 1, D("0.4"), D("0.4"), first_time)
    second = ExecutionIncrement("entry:synthetic:2", 2, D("0.6"), D("1"), second_time)
    latter = item.history[1]
    second_interval = CoveredInterval(second.execution_id, second_time, START + 3 * HOUR,
                                      latter.low, latter.high, "synthetic-feed")
    item = replace(item, executions=(FillActivation(first, first_time), FillActivation(second, second_time)),
                   intervals=(item.intervals[0], second_interval))
    assert check(item) == EvidenceState.VALID
    missing = replace(item, intervals=(item.intervals[0],))
    assert check(missing) == EvidenceState.INCOMPLETE
    crossed_bar = (replace(latter, low=D("99")) if side == OrderSide.BUY
                   else replace(latter, high=D("101")))
    crossed = replace(item, history=(item.history[0], crossed_bar))
    result = assess_protection_evidence(crossed, now=START + 3 * HOUR, max_age=HOUR)
    assert result.state == EvidenceState.STOP_CROSSED
    assert result.crossed_execution_ids == (first.execution_id,)
    crossed_interval = (replace(second_interval, low=D("99")) if side == OrderSide.BUY
                        else replace(second_interval, high=D("101")))
    result = assess_protection_evidence(replace(crossed, intervals=(item.intervals[0], crossed_interval)),
                                        now=START + 3 * HOUR, max_age=HOUR)
    assert result.crossed_execution_ids == (first.execution_id, second.execution_id)


def test_incomplete_legacy_exposure_cannot_be_supplied_as_complete():
    item = intrabar()
    fill = replace(item.executions[0].execution, quantity=D("0.5"), cumulative_quantity=D("1"))
    item = replace(item, executions=(FillActivation(fill, fill.executed_at),))
    assert check(item) == EvidenceState.INCOMPLETE
    assert check(replace(item, executions=(), intervals=(),
                         execution_history_complete=None, exposure_quantity=None)) == EvidenceState.INCOMPLETE


def test_later_fill_cannot_have_artificially_delayed_activation():
    item = intrabar()
    first_time = item.executions[0].effective_at
    second_time = START + 2 * HOUR + timedelta(minutes=10)
    first = ExecutionIncrement("entry:synthetic:1", 1, D("0.4"), D("0.4"), first_time)
    second = ExecutionIncrement("entry:synthetic:2", 2, D("0.6"), D("1"), second_time)
    delayed = second_time + timedelta(minutes=1)
    item = replace(item, executions=(FillActivation(first, first_time), FillActivation(second, delayed)))
    assert check(item) == EvidenceState.INCOMPLETE


def test_fill_at_reference_close_has_no_post_fill_coverage():
    item = intrabar()
    at_close = item.reference.observed_at
    fill = replace(item.executions[0].execution, executed_at=at_close)
    item = replace(item, stop_effective_at=at_close, executions=(FillActivation(fill, at_close),), intervals=())
    assert check(item) == EvidenceState.INCOMPLETE
