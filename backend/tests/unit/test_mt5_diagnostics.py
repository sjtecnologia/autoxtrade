import json
import pytest

from trading.mt5_diagnostics import read_only_report


@pytest.mark.parametrize("subfolder", [False, True])
def test_mt5_report_is_read_only_and_omits_private_order_payload(tmp_path, subfolder):
    root = tmp_path / "mt5"
    directory = root / "DWX" if subfolder else root
    directory.mkdir(parents=True)
    (directory / "DWX_Market_Data.txt").write_text(json.dumps({"EURUSD": {"bid": 1.1, "ask": 1.1001}}))
    (directory / "DWX_Orders.txt").write_text('{"account":"DO-NOT-EXPORT","orders":[1,2]}')
    (directory / "MarketWatchSymbols.txt").write_text("EURUSD\nGBPUSD")
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    report = read_only_report(root)
    assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
    assert report["orders_sent"] == 0 and report["read_only"] is True
    assert report["execution_authorized"] is False and report["account_identity_verified"] is False
    assert "DO-NOT-EXPORT" not in str(report)
    assert report["symbol_hints"] == ["EURUSD", "GBPUSD"]
