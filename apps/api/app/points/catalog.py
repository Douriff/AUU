"""What points can buy (framework only).

Every existing feature stays free: nothing in the app checks ``has_entitlement`` yet. The single
example item is a cosmetic "supporter badge" shown on the points page; the shop is OFF unless
AUU_POINTS_SHOP=on. Prices are fixed here on the server; a client never sends an amount for a spend.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

DAY_MS = 86_400_000

ITEMS: dict[str, dict[str, Any]] = {
    # id: cost in points, duration in days (0 = permanent), what it unlocks (i18n: points.item.<id>.*)
    "example_badge": {"cost": 100, "days": 30, "example": True},
}


def shop_enabled() -> bool:
    return os.getenv("AUU_POINTS_SHOP", "").strip().lower() in {"1", "true", "on", "yes"}


def item(item_id: str) -> Optional[dict[str, Any]]:
    return ITEMS.get((item_id or "").strip())


def entitlements(rows: list[dict[str, Any]], refunded: dict[int, int], now_ms: Optional[int] = None) -> dict[str, dict[str, Any]]:
    """Active unlocks derived from the ledger: spend rows ``ref=item:<id>`` minus fully refunded ones."""
    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    out: dict[str, dict[str, Any]] = {k: {"active": False, "until": None} for k in ITEMS}
    for row in sorted(rows, key=lambda r: r["id"]):
        ref = str(row.get("ref") or "")
        if row["kind"] != "spend" or not ref.startswith("item:"):
            continue
        iid = ref[5:]
        spec = ITEMS.get(iid)
        if spec is None or refunded.get(int(row["id"]), 0) >= abs(int(row["amount"])):
            continue
        cur = out[iid]
        if spec["days"] <= 0:
            cur["until"] = None
            cur["permanent"] = True
        else:
            start = max(int(row["ts"]), int(cur["until"] or 0))
            cur["until"] = start + spec["days"] * DAY_MS
    for iid, cur in out.items():
        cur["active"] = bool(cur.get("permanent")) or bool(cur["until"] and cur["until"] > now)
    return out


def catalog_view() -> list[dict[str, Any]]:
    return [{"id": k, "cost": v["cost"], "days": v["days"], "example": bool(v.get("example"))} for k, v in ITEMS.items()]
