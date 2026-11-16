from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from paper.entry_time import proven_entry_time
from trading.contracts import ExecutionIncrement, ExecutionResult, ResultState


def observed():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fills = (ExecutionIncrement("first", 1, Decimal("0.5"), Decimal("0.5"), now),
             ExecutionIncrement("second", 2, Decimal("0.5"), Decimal("1"), now + timedelta(minutes=1)))
    return ExecutionResult("intent", ResultState.CONFIRMED, filled=Decimal("1"), revision=2,
                           executions=fills, execution_history_complete=True)


def test_opening_time_is_first_actual_fill_not_latest_fill():
    value = observed()
    assert proven_entry_time(value) == value.executions[0].executed_at
    assert proven_entry_time(replace(value, revision=3)) == value.executions[0].executed_at


@pytest.mark.parametrize("problem", ["missing", "naive", "duplicate", "time_order", "quantity", "revision"])
def test_incomplete_execution_timeline_is_rejected(problem):
    value = observed()
    fills = list(value.executions)
    if problem == "missing":
        value = replace(value, execution_history_complete=False)
    elif problem == "naive":
        fills[0] = replace(fills[0], executed_at=fills[0].executed_at.replace(tzinfo=None))
    elif problem == "duplicate":
        fills[1] = replace(fills[1], execution_id=fills[0].execution_id)
    elif problem == "time_order":
        fills[1] = replace(fills[1], executed_at=fills[0].executed_at - timedelta(seconds=1))
    elif problem == "quantity":
        fills[1] = replace(fills[1], cumulative_quantity=Decimal("2"))
    else:
        fills[1] = replace(fills[1], revision=1)
    with pytest.raises(ValueError):
        proven_entry_time(replace(value, executions=tuple(fills)))
