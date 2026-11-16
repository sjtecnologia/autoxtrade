"""Server-selected market evidence provider for durable paper protection.

Exactly one contract is supported: a provider object exposing
``evidence_for(request)`` that returns market history for a prepared protection
attempt (see :class:`paper.protection_evidence_store.ProtectionEvidenceRequest`).

The provider used by the active paper flow is chosen by the server from
configuration (``settings.protection_evidence_provider``). Callers must not
inject an arbitrary callable into the operational path; a missing source blocks
protection safely instead of falling back to a synthetic quote.
"""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# Name -> zero-argument factory returning a provider object with ``evidence_for``.
_REGISTRY: dict[str, Callable[[], object]] = {}

_DISABLED_NAMES = frozenset({"", "none", "disabled"})


def register_protection_evidence_provider(name: str, factory: Callable[[], object]) -> None:
    """Register a named provider factory (case-insensitive)."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Protection evidence provider name is required")
    if not callable(factory):
        raise TypeError("Protection evidence provider factory must be callable")
    _REGISTRY[name.strip().lower()] = factory


def resolve_protection_evidence_provider(name: str | None = None):
    """Return the configured provider, or ``None`` when no source is configured.

    The name comes from server settings unless supplied explicitly. An unknown
    name is a configuration error and is never silently ignored, so a typo
    cannot disable the evidence gate without being noticed.
    """
    from config import settings

    value = (settings.protection_evidence_provider if name is None else name) or ""
    value = value.strip()
    if value.lower() in _DISABLED_NAMES:
        return None
    factory = _REGISTRY.get(value.lower())
    if factory is None:
        raise ValueError(f"Unknown protection evidence provider: {value!r}")
    return factory()
