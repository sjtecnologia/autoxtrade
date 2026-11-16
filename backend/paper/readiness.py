"""Configuration-only preflight. Never opens a broker, database or MT5 share."""
import argparse
import json
from decimal import Decimal, InvalidOperation


def configuration_report(config, *, market="FOREX", symbol=None):
    from paper.costs import parse_costs
    from paper.protection_evidence_provider import _REGISTRY
    from trading.safety import ExposureBlocked

    checks = []
    def add(code, ok, reason):
        checks.append(dict(code=code, ok=bool(ok), reason=reason))
    # The actual execution policy rejects every external mode even with its flag on.
    add("external_execution", False,
        "A fábrica/política ativa aceita apenas transports locais simulados/de teste; MT5 real não validado.")
    add("evidence_calendar", market == "CRIPTO",
        "O avaliador suporta somente CRIPTO 24/7; calendários Forex/B3 não estão implementados.")
    name = (config.protection_evidence_provider or "").strip().lower()
    add("evidence_provider_selected", name not in {"", "none", "disabled"} and name in _REGISTRY,
        "É necessária uma fonte registrada com cobertura real verificável após cada fill; seleção não certifica a fonte.")
    add("paper_entries_enabled", config.paper_new_exposure_enabled,
        "Novas entradas paper exigem habilitação explícita e qualificação OOS válida; o diagnóstico não habilita nada.")
    try:
        capital = Decimal(str(config.paper_initial_capital))
        capital_ok = capital.is_finite() and capital > 0
    except (InvalidOperation, ValueError, TypeError):
        capital_ok = False
    add("paper_capital", capital_ok, "O capital paper deve ser positivo e explícito.")
    try:
        costs = parse_costs(config.paper_costs_json)
        costs_ok = bool(costs) and (symbol is None or symbol in costs)
    except ExposureBlocked:
        costs_ok = False
    add("paper_costs", costs_ok, "Custos válidos por instrumento são obrigatórios; não foram consultados preços ou contas.")
    return dict(
        scope="configuration_only", market=market, can_enable_real_orders=False,
        runtime_validation_required=True, checks=checks,
        qualification_verified=False,
        resources_accessed={"broker": False, "database": False, "mt5_share": False},
        next_steps=[
            "Validar identidade e especificações dos instrumentos sem enviar ordens.",
            "Integrar um transport operacional com recibos, tickets, idempotência e proteção comprovados.",
            "Fornecer histórico granular e calendário do mercado, sem preencher lacunas artificialmente.",
            "Requalificar a estratégia/custos para o fingerprint atual e validar paper ponta a ponta.",
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["CRIPTO", "FOREX", "B3"], default="FOREX")
    parser.add_argument("--symbol")
    args = parser.parse_args()
    try:
        from config import settings
    except Exception as exc:
        # Never print validation input values: they may contain credentials.
        from types import SimpleNamespace
        import os
        settings = SimpleNamespace(
            protection_evidence_provider=os.getenv("PROTECTION_EVIDENCE_PROVIDER", ""),
            paper_new_exposure_enabled=os.getenv("PAPER_NEW_EXPOSURE_ENABLED", "").lower() in {"true", "1"},
            paper_initial_capital=os.getenv("PAPER_INITIAL_CAPITAL"),
            paper_costs_json=os.getenv("PAPER_COSTS_JSON", "{}"),
        )
        missing = [".".join(str(x) for x in error["loc"]) for error in exc.errors()] if hasattr(exc, "errors") else ["project_settings"]
        source = "environment_fallback"
    else:
        missing, source = [], "project_settings"
    report = configuration_report(settings, market=args.market, symbol=args.symbol)
    report.update(configuration_source=source, configuration_error_fields=missing)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
