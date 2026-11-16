from types import SimpleNamespace

from paper.readiness import configuration_report
from paper.protection_evidence_provider import register_protection_evidence_provider


def config(**changes):
    return SimpleNamespace(**dict(dict(protection_evidence_provider="", paper_new_exposure_enabled=False,
        paper_initial_capital=None, paper_costs_json="{}"), **changes))


def test_readiness_reports_real_and_forex_blockers_without_secrets():
    settings = config()
    settings.api_secret_token = "do-not-print-secret"
    report = configuration_report(settings)
    assert report["can_enable_real_orders"] is False
    assert report["qualification_verified"] is False
    assert all(value is False for value in report["resources_accessed"].values())
    assert not any(row["ok"] for row in report["checks"])
    assert "do-not-print-secret" not in str(report)


def test_readiness_never_instantiates_provider_or_authorizes_real(monkeypatch):
    from paper import protection_evidence_provider
    monkeypatch.setattr(protection_evidence_provider, "_REGISTRY", {})
    def forbidden():
        raise AssertionError("A configuration report must not instantiate any source")
    register_protection_evidence_provider("configured", forbidden)
    costs = '{"BTC/USDT":{"spread_price":"0.1","commission_rate":"0.001","slippage_price":"0"}}'
    report = configuration_report(config(protection_evidence_provider="configured", paper_new_exposure_enabled=True,
        paper_initial_capital="1000", paper_costs_json=costs), market="CRIPTO", symbol="BTC/USDT")
    assert all(row["ok"] for row in report["checks"] if row["code"] != "external_execution")
    assert report["can_enable_real_orders"] is False and report["runtime_validation_required"] is True
