"""Public, definition-only view of strategies already registered in AUU.

Performance, rankings, and starting NAV / accounting units are omitted so a
missing series cannot be filled in with invented returns. ``orders`` is always
reported as false on this surface.
"""
from __future__ import annotations

from typing import Any

_PUBLIC = (
    "id",
    "version",
    "version_kind",
    "style",
    "direction",
    "timeframe",
    "long_only",
    "primary_book",
    "registered_at",
    "records",
    "params_hash",
)


def _pool(raw: dict[str, Any] | None) -> dict[str, Any]:
    pool = raw if isinstance(raw, dict) else {}
    symbols = pool.get("symbols")
    size = pool.get("size")
    if size is None and isinstance(symbols, list):
        size = len(symbols)
    return {
        "size": size if isinstance(size, int) else None,
        "source": pool.get("source") or "registered_catalog",
        "coverage": pool.get("coverage") or "unknown",
        # Universe membership is identity, not a return series.
        "symbols": list(symbols) if isinstance(symbols, list) else None,
    }


def _hold(raw: dict[str, Any] | None) -> dict[str, Any]:
    hold = raw if isinstance(raw, dict) else {}
    return {
        "kind": hold.get("kind"),
        "enter": hold.get("enter"),
        "exit": hold.get("exit"),
        "hours": hold.get("hours"),
        "fill": hold.get("fill"),
    }


def public_card(raw: dict[str, Any]) -> dict[str, Any]:
    card = {key: raw.get(key) for key in _PUBLIC}
    card["pool"] = _pool(raw.get("pool") if isinstance(raw.get("pool"), dict) else None)
    card["hold"] = _hold(raw.get("hold") if isinstance(raw.get("hold"), dict) else None)
    card["orders"] = False
    card["returns_published"] = False
    card["performance"] = None
    return card


def catalog_payload() -> dict[str, Any]:
    from app.ai_review.catalog import identities

    rows = [public_card(row) for row in identities() if isinstance(row, dict) and row.get("id")]
    return {
        "paper_only": True,
        "live_enabled": False,
        "auto_model": False,
        "used_model": False,
        "source": "registered_catalog",
        "returns_published": False,
        "strategies": rows,
    }
