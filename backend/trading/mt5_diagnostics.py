"""Explicit, read-only DWX layout report. Does not create a connector or send orders."""
import argparse
import json
from pathlib import Path


def read_only_report(directory):
    # Do not import settings/collectors/connectors: layout inspection needs no secrets.
    import time
    from trading.dwx_readonly import directory as resolve_directory, snapshot, DWXDataUnavailable
    root = resolve_directory(directory)
    files = sorted(p.name for p in root.glob("DWX_*.txt") if p.is_file())
    hint_path = root / "MarketWatchSymbols.txt"
    hints = []
    if hint_path.is_file() and hint_path.stat().st_size <= 100_000:
        hints = sorted(set(hint_path.read_text(encoding="utf-8-sig").replace(",", "\n").split()))
    path = root / "DWX_Market_Data.txt"
    age = time.time() - path.stat().st_mtime if path.is_file() else None
    missing, findings = [], [
        "DWX_Server_MT5 1.0: outputs sem sufixo são esperados; apenas comandos usam slot.",
        "Ausência de DWX_Commands_* é normal quando não há comandos pendentes.",
        "Idade do arquivo não certifica o timestamp do tick no broker.",
    ]
    try:
        snapshot(path, now=time.time(), max_age=10)
        payload_missing = False
    except DWXDataUnavailable as exc:
        payload_missing = True
        findings.append(str(exc))
    for name in ("DWX_Orders.txt", "DWX_Historic_Data.txt"):
        if not (root / name).is_file():
            missing.append(name)
    return dict(
        scope="read_only_layout_and_freshness", read_only=True, orders_sent=0,
        directory=str(root), naming_pattern="DWX_<output>.txt / DWX_Commands_<slot>.txt",
        files=files, symbol_hints=hints, missing_sources=missing, findings=findings,
        market_data_missing=payload_missing,
        market_data_file_stale=age is None or age < -2 or age > 10,
        market_data_file_age_seconds=age,
        account_identity_verified=False, execution_authorized=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--read-only", action="store_true", required=True,
                        help="Explicit acknowledgement that only the supplied directory will be inspected")
    args = parser.parse_args()
    try:
        report = read_only_report(args.directory)
    except Exception as exc:
        report = dict(scope="read_only_layout_and_freshness", read_only=True, orders_sent=0,
                      available=False, error=str(exc), execution_authorized=False)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
