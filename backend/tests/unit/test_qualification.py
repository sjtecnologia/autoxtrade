from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from paper.costs import PaperCostModel
from paper.qualification import qualify_symbol


def test_qualification_excludes_currently_forming_candle():
    timestamps = pd.date_range("2026-10-01", periods=300, freq="h", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 1.0,
    })
    now = (timestamps[-1] + pd.Timedelta(minutes=30)).to_pydatetime()
    costs = PaperCostModel(Decimal("0.01"), Decimal("0.0001"), Decimal("0.001"))

    result = qualify_symbol(frame, "TEST", costs, now=now)

    assert result["bars"] == 299
    assert result["data_end"] == timestamps[-2].isoformat()
    assert result["data_age_days"] >= 0
