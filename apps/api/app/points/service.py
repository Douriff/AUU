"""Points operations with server-side validation. Routes call these; nothing else writes the ledger.

Rules enforced here (never trusted from the client):
  * amounts are whole positive integers within fixed bounds (strings like "1e3", floats, bools refused);
  * every write carries an idempotency key (scoped per action and actor) and a request fingerprint;
  * spending prices come from catalog.ITEMS; refunds never exceed what is left of the entry;
  * a balance never goes below zero; transfer between users stays forbidden; withdrawal exists only when AUU_WITHDRAW_ENABLED is on (freeze → admin manual payout).
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import unicodedata
from typing import Any, Optional

from app.points import catalog, providers
from app.points.ledger import MAX_ENTRY, PointsError, get_points_ledger

TOPUP_MIN, TOPUP_MAX = 1, 100_000
ADJUST_MAX = 1_000_000
_IDEM = re.compile(r"^[A-Za-z0-9_-]{16,80}$")


def parse_amount(value: Any, *, lo: int = 1, hi: int = MAX_ENTRY) -> int:
    """Whole number of points in [lo, hi]. Accepts a JSON integer or a plain digit string."""
    if isinstance(value, bool):
        raise PointsError("POINTS_BAD_AMOUNT")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value.strip()):
        n = int(value.strip())
    else:
        raise PointsError("POINTS_BAD_AMOUNT")
    if not (lo <= n <= hi):
        raise PointsError("POINTS_BAD_AMOUNT")
    return n


def parse_signed(value: Any, *, hi: int = ADJUST_MAX) -> int:
    """Non-zero whole number with |n| <= hi (admin adjust)."""
    if isinstance(value, bool):
        raise PointsError("POINTS_BAD_AMOUNT")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]{1,10}", value.strip()):
        n = int(value.strip())
    else:
        raise PointsError("POINTS_BAD_AMOUNT")
    if n == 0 or abs(n) > hi:
        raise PointsError("POINTS_BAD_AMOUNT")
    return n


def parse_reason(value: Any, *, required: bool = True) -> str:
    text = value if isinstance(value, str) else ""
    text = "".join(" " if ch.isspace() else ch for ch in text if ch.isspace() or unicodedata.category(ch)[0] != "C").strip()
    text = re.sub(r"\s+", " ", text)
    if required and len(text) < 2:
        raise PointsError("POINTS_REASON_REQUIRED")
    if len(text) > 200:
        raise PointsError("POINTS_REASON_TOO_LONG")
    return text


def parse_idem(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _IDEM.match(text):
        raise PointsError("POINTS_IDEMPOTENCY_REQUIRED")
    return text


def _fp(**fields: Any) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def public_entry(row: dict[str, Any], *, admin: bool = False) -> dict[str, Any]:
    out = {k: row.get(k) for k in ("id", "ts", "kind", "amount", "balance_after", "reason", "provider", "ref")}
    op = str(row.get("operator") or "")
    out["operatorKind"] = op.split(":", 1)[0] if op else ""
    if admin:
        out.update(user_id=row.get("user_id"), operator=op, hash=row.get("hash"))
    return out


def account_view(user_id: str, *, limit: int = 50, before_id: Optional[int] = None, admin: bool = False) -> dict[str, Any]:
    led = get_points_ledger()
    lim = max(1, min(int(limit), 500))
    rows = led.entries(user_id, limit=lim + 1, before_id=before_id)
    all_rows = led.iter_all(user_id)
    refunded = _refunded_map(all_rows)
    from app.points import trading as points_trading
    from app.points import withdraw as points_withdraw

    return {
        "balance": led.balance(user_id),
        "entries": [public_entry(r, admin=admin) for r in rows[:lim]],
        "hasMore": len(rows) > lim,
        "entitlements": catalog.entitlements(all_rows, refunded),
        "shop": {"enabled": catalog.shop_enabled(), "items": catalog.catalog_view()},
        "rules": {
            "withdraw": points_withdraw.enabled(),
            "transfer": False,
            "separateFromPaper": True,
            "pointsTrading": points_trading.enabled(),
        },
        "withdraw": points_withdraw.public_config(admin),
        "pointsTrading": points_trading.public_config(admin),
    }


def _refunded_map(rows: list[dict[str, Any]]) -> dict[int, int]:
    out: dict[int, int] = {}
    for r in rows:
        ref = str(r.get("ref") or "")
        if r["kind"] == "refund" and ref.startswith("entry:"):
            try:
                eid = int(ref[6:])
            except ValueError:
                continue
            out[eid] = out.get(eid, 0) + abs(int(r["amount"]))
    return out


# ---- user actions ------------------------------------------------------------------------------
def spend(user_id: str, item_id: Any, idem: Any) -> tuple[dict[str, Any], bool]:
    if not catalog.shop_enabled():
        raise PointsError("POINTS_SHOP_OFF")
    iid = item_id if isinstance(item_id, str) else ""
    spec = catalog.item(iid)
    if spec is None:
        raise PointsError("POINTS_UNKNOWN_ITEM")
    key = parse_idem(idem)
    cost = int(spec["cost"])
    return get_points_ledger().append(
        user_id=user_id, kind="spend", amount=-cost, reason=f"unlock {iid}", operator=f"user:{user_id}",
        idem_key=f"spend:{user_id}:{key}", fingerprint=_fp(a="spend", u=user_id, item=iid, cost=cost), ref=f"item:{iid}")


def topup(user_id: str, provider_id: Any, points: Any, idem: Any) -> dict[str, Any]:
    """User-started top-up through an enabled channel. Today only the test-only mock can be enabled."""
    prov = providers.get_provider(provider_id if isinstance(provider_id, str) else "")
    if prov is None or prov not in providers.user_channels():
        raise PointsError("POINTS_TOPUP_CLOSED")
    n = parse_amount(points, lo=TOPUP_MIN, hi=TOPUP_MAX)
    key = parse_idem(idem)
    led = get_points_ledger()
    order_id = "po_" + hashlib.sha256(f"topup:{user_id}:{key}".encode()).hexdigest()[:24]
    old = led.order(order_id)
    if old is not None:
        if old["user_id"] != user_id or old["provider"] != prov.id or int(old["points"]) != n:
            raise PointsError("POINTS_IDEMPOTENCY_CONFLICT")
        return {"order": old, "replayed": True}
    led.create_order(order_id=order_id, user_id=user_id, provider=prov.id, points=n)
    res = prov.create_order(order_id=order_id, user_id=user_id, points=n)
    if res.status == "paid":
        row, _ = credit_order(order_id, external_ref=res.external_ref)
        return {"order": led.order(order_id), "entry": public_entry(row), "replayed": False}
    if res.status == "failed":
        led.finish_order(order_id, "failed", external_ref=res.external_ref)
    return {"order": led.order(order_id), "redirect": res.redirect_url, "replayed": False}


def credit_order(order_id: str, *, external_ref: Optional[str]) -> tuple[dict[str, Any], bool]:
    """Credit a paid order exactly once (idempotency key = order id). Called by the channel's verified
    webhook / instant result only."""
    led = get_points_ledger()
    o = led.order(order_id)
    if o is None:
        raise PointsError("POINTS_UNKNOWN_ORDER")
    row, replayed = led.append(
        user_id=o["user_id"], kind="topup", amount=int(o["points"]), reason=f"top-up via {o['provider']}",
        operator=f"provider:{o['provider']}", idem_key=f"order:{order_id}", fingerprint=_fp(a="order", o=order_id, p=int(o["points"])),
        provider=o["provider"], ref=f"order:{order_id}")
    if o["status"] == "pending":
        led.finish_order(order_id, "paid", external_ref=external_ref, entry_id=int(row["id"]))
    return row, replayed


# ---- admin actions (route has already checked admin + password + 2FA) -------------------------------
def admin_adjust(admin_id: str, user_id: str, amount: Any, reason: Any, idem: Any) -> tuple[dict[str, Any], bool]:
    n = parse_signed(amount)
    why = parse_reason(reason)
    key = parse_idem(idem)
    return get_points_ledger().append(
        user_id=user_id, kind="adjust", amount=n, reason=why, operator=f"admin:{admin_id}",
        idem_key=f"adjust:{admin_id}:{key}", fingerprint=_fp(a="adjust", u=user_id, n=n, r=why))


def admin_manual_topup(admin_id: str, user_id: str, points: Any, reason: Any, external_ref: Any, idem: Any) -> tuple[dict[str, Any], bool]:
    prov = providers.get_provider("manual_admin")
    if prov is None or not prov.enabled():
        raise PointsError("POINTS_MANUAL_OFF")
    n = parse_amount(points, lo=TOPUP_MIN, hi=TOPUP_MAX)
    why = parse_reason(reason)
    ext = parse_reason(external_ref, required=False)[:80]
    key = parse_idem(idem)
    return get_points_ledger().append(
        user_id=user_id, kind="topup", amount=n, reason=why, operator=f"admin:{admin_id}", provider="manual_admin",
        ref=f"manual:{ext}" if ext else "manual", idem_key=f"manual:{admin_id}:{key}",
        fingerprint=_fp(a="manual", u=user_id, n=n, r=why, x=ext))


def admin_refund(admin_id: str, entry_id: Any, amount: Any, reason: Any, idem: Any) -> tuple[dict[str, Any], bool]:
    """Refund (part of) a spend (points come back) or a top-up (points go out). Never more than is left."""
    eid = parse_amount(entry_id, lo=1, hi=10**10 - 1)
    led = get_points_ledger()
    target = led.entry(eid)
    if target is None or target["kind"] not in ("spend", "topup"):
        raise PointsError("POINTS_NOT_REFUNDABLE")
    n = parse_amount(amount, lo=1, hi=abs(int(target["amount"])))
    why = parse_reason(reason)
    key = parse_idem(idem)
    signed = n if target["kind"] == "spend" else -n

    def check(db, _bal):
        if led.refunded(db, eid) + n > abs(int(target["amount"])):
            raise PointsError("POINTS_REFUND_EXCEEDS")

    return led.append(
        user_id=target["user_id"], kind="refund", amount=signed, reason=why, operator=f"admin:{admin_id}",
        ref=f"entry:{eid}", provider=target.get("provider"), idem_key=f"refund:{admin_id}:{key}",
        fingerprint=_fp(a="refund", e=eid, n=n, r=why), check=check)


def new_idempotency_key() -> str:
    return secrets.token_urlsafe(18)
