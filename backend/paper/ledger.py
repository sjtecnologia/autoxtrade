"""Ledger JSON de ordens exclusivamente simuladas."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4


class PaperLedger:
    """Persiste apenas ordens com status ``simulated`` em arquivo local."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[dict]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ledger paper inválido: {self.path}") from exc
        if not isinstance(payload, list):
            raise RuntimeError("Ledger paper deve conter uma lista JSON")
        return payload

    def _write(self, entries: list[dict]) -> None:
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(entries, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def record_order(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        price_source: str,
    ) -> dict:
        if side not in {"buy", "sell"}:
            raise ValueError(f"Lado inválido para paper: {side}")
        if quantity <= 0 or price <= 0:
            raise ValueError("Quantidade e preço paper devem ser positivos")
        if not price_source.strip():
            raise ValueError("A fonte do preço deve ser rotulada")
        entry = {
            "id": f"paper-{uuid4().hex}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "price": str(price),
            "price_source": price_source,
            "status": "simulated",
        }
        entries = self._read()
        entries.append(entry)
        self._write(entries)
        return entry

    def entries(self) -> list[dict]:
        return self._read()
