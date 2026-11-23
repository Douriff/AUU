"""Trimmed, site-wide paper-runner status for the AI section.

GET /api/v1/mainstream/strategy returns the M3 system paper ledger
(``data_dir()/mainstream_strategy.sqlite``), a process-wide singleton. It is
not a per-user agent or a personal account. This view keeps only status
fields that identify that system runner. It drops NAV, returns, position
notionals, quantities, prices, the equity curve, and go/no-go performance
stats so the AI section does not re-expose ledger figures as if they were a
personal book.
"""
from __future__ import annotations

from typing import Any

_GO_KEEP = ("verdict", "lamp", "days", "minDays", "reasons")


def _strategy_name(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()[:80]
    return None


def _go_status(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    out = {key: raw.get(key) for key in _GO_KEEP if key in raw}
    return out or None


def public_runner_status(summary: dict[str, Any] | None) -> dict[str, Any]:
    src = summary if isinstance(summary, dict) else {}
    live = src.get("live") if isinstance(src.get("live"), dict) else {}
    ledger = src.get("ledger")
    if not isinstance(ledger, str) or not ledger.strip():
        ledger = "mainstream_strategy.sqlite"
    return {
        "scope": "site_system_runner",
        "personal": False,
        "account": None,
        "paper_only": True,
        "mode": "paper",
        "live_enabled": bool(live.get("enabled")) if "enabled" in live else False,
        "ledger": ledger,
        "strategy_name": _strategy_name(src.get("strategy")),
        "go_no_go": _go_status(src.get("go_no_go")),
        "source": "system_paper_ledger",
        "agents_connected": False,
        "copy_trading": False,
    }
