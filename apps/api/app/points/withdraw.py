"""USDT-TRC20 withdrawal (manual payout). Default OFF.

Flow
  1. User applies with amount (points) + their TRC20 receive address → pending + withdraw_freeze
     (points leave available balance immediately).
  2. Admin rejects → withdraw_unfreeze (points return) + status=rejected.
  3. Admin pays USDT-TRC20 manually from TokenPocket (private key NEVER on this server),
     pastes the tx hash → on-chain verify (official USDT Transfer to the user's address for the
     expected micro-amount) → withdraw_complete marker + status=completed.

Config (environment only):
  AUU_WITHDRAW_ENABLED=off|on
  AUU_WITHDRAW_ADMIN_ONLY=on   # soft launch: admins only (like AUU_TRC20_ADMIN_ONLY)
  AUU_WITHDRAW_MIN / MAX       # USDT whole units (default 10 / 1000)
  AUU_WITHDRAW_DAILY_MAX       # USDT per user per Beijing day (default 3000)
  AUU_WITHDRAW_MAX_PENDING     # open applications per user (default 3)
  Rate = AUU_TRC20_RATE (100 points per USDT) when set, else 100.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.points.ledger import PointsError, get_points_ledger
from app.points import trc20

log = logging.getLogger("auu.points.withdraw")
_BJ = timezone(timedelta(hours=8))
_IDEM = re.compile(r"^[A-Za-z0-9_-]{16,80}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS withdraw_requests (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  points INTEGER NOT NULL CHECK (points > 0),
  usdt INTEGER NOT NULL CHECK (usdt > 0),
  amount_micro INTEGER NOT NULL CHECK (amount_micro > 0),
  address TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','completed','cancelled')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  idem_key TEXT NOT NULL UNIQUE,
  freeze_entry_id INTEGER,
  unfreeze_entry_id INTEGER,
  complete_entry_id INTEGER,
  tx_hash TEXT,
  reviewed_by TEXT,
  reviewed_at INTEGER,
  note TEXT
);
CREATE INDEX IF NOT EXISTS withdraw_user ON withdraw_requests(user_id, created_at);
CREATE INDEX IF NOT EXISTS withdraw_status ON withdraw_requests(status, created_at);
CREATE TRIGGER IF NOT EXISTS withdraw_no_delete BEFORE DELETE ON withdraw_requests
BEGIN SELECT RAISE(ABORT, 'withdraw requests are kept'); END;
"""


def _on(name: str, default: str = "off") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "on", "yes"}


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(str(os.getenv(name, "") or default).strip())
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def rate() -> int:
    return _int("AUU_TRC20_RATE", 100, 1, 100_000)


def enabled() -> bool:
    return _on("AUU_WITHDRAW_ENABLED", "off")


def admin_only() -> bool:
    return _on("AUU_WITHDRAW_ADMIN_ONLY", "off")


def enabled_for(is_admin: bool) -> bool:
    return enabled() and (is_admin or not admin_only())


def config() -> dict[str, Any]:
    lo = _int("AUU_WITHDRAW_MIN", 10, 1, 1_000_000)
    hi = max(lo, _int("AUU_WITHDRAW_MAX", 1000, 1, 1_000_000))
    return {
        "rate": rate(),
        "min": lo,
        "max": hi,
        "dailyMax": _int("AUU_WITHDRAW_DAILY_MAX", 3000, 1, 10_000_000),
        "maxPending": _int("AUU_WITHDRAW_MAX_PENDING", 3, 1, 20),
        "network": "TRC20",
        "token": "USDT",
        "contract": trc20.USDT_CONTRACT,
    }


def public_config(is_admin: bool = False) -> dict[str, Any]:
    c = config()
    on = enabled_for(is_admin)
    return {
        "enabled": on,
        "adminOnly": admin_only(),
        "rate": c["rate"],
        "min": c["min"],
        "max": c["max"],
        "dailyMax": c["dailyMax"],
        "maxPending": c["maxPending"],
        "network": "TRC20",
        "token": "USDT",
        "contract": trc20.USDT_CONTRACT,
        "manualPayout": True,
        "privateKeyOnServer": False,
    }


def _db():
    led = get_points_ledger()
    if not getattr(led, "_withdraw_schema", False):
        with led._lock:
            led._db.executescript(SCHEMA)
        led._withdraw_schema = True
    return led


def _now() -> int:
    return int(time.time() * 1000)


def _bj_day_start(ms: int) -> int:
    d = datetime.fromtimestamp(ms / 1000, _BJ).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def _fp(**fields: Any) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _parse_idem(value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not _IDEM.match(text):
        raise PointsError("POINTS_IDEMPOTENCY_REQUIRED")
    return text


def _parse_usdt(value: Any) -> int:
    if isinstance(value, bool):
        raise PointsError("WITHDRAW_BAD_AMOUNT")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value.strip()):
        n = int(value.strip())
    else:
        raise PointsError("WITHDRAW_BAD_AMOUNT")
    c = config()
    if not (c["min"] <= n <= c["max"]):
        raise PointsError("WITHDRAW_BAD_AMOUNT")
    return n


def _parse_address(value: Any) -> str:
    addr = (value if isinstance(value, str) else "").strip()
    if not trc20.valid_address(addr):
        raise PointsError("WITHDRAW_BAD_ADDRESS")
    # refuse paying out to the platform receive address (would look like a top-up loop)
    recv = trc20.receive_address()
    if recv and addr == recv:
        raise PointsError("WITHDRAW_BAD_ADDRESS")
    return addr


def request_view(row: dict[str, Any], *, admin: bool = False) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "userId": row["user_id"],
        "points": int(row["points"]),
        "usdt": int(row["usdt"]),
        "amountMicro": int(row["amount_micro"]),
        "amountUsdt": trc20.fmt_usdt(int(row["amount_micro"])),
        "address": row["address"],
        "status": row["status"],
        "createdAt": int(row["created_at"]),
        "updatedAt": int(row["updated_at"]),
        "txHash": row.get("tx_hash"),
        "note": row.get("note") or "",
    }
    if admin:
        out.update(
            freezeEntryId=row.get("freeze_entry_id"),
            unfreezeEntryId=row.get("unfreeze_entry_id"),
            completeEntryId=row.get("complete_entry_id"),
            reviewedBy=row.get("reviewed_by"),
            reviewedAt=row.get("reviewed_at"),
        )
    return out


def get_request(rid: str) -> Optional[dict[str, Any]]:
    led = _db()
    with led._lock:
        row = led._db.execute("SELECT * FROM withdraw_requests WHERE id=?", (rid,)).fetchone()
    return dict(row) if row else None


def user_requests(user_id: str, limit: int = 30) -> list[dict[str, Any]]:
    led = _db()
    with led._lock:
        rows = led._db.execute(
            "SELECT * FROM withdraw_requests WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(int(limit), 200))),
        ).fetchall()
    return [dict(r) for r in rows]


def all_requests(status: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    led = _db()
    sql, args = "SELECT * FROM withdraw_requests WHERE 1=1", []
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with led._lock:
        return [dict(r) for r in led._db.execute(sql, args).fetchall()]


def apply(user_id: str, usdt: Any, address: Any, idem: Any, *, is_admin: bool = False) -> tuple[dict[str, Any], bool]:
    if not enabled_for(is_admin):
        raise PointsError("WITHDRAW_OFF")
    n_usdt = _parse_usdt(usdt)
    addr = _parse_address(address)
    key = _parse_idem(idem)
    c = config()
    points = n_usdt * c["rate"]
    amount_micro = n_usdt * trc20.MICRO
    order_id = "wd_" + hashlib.sha256(f"withdraw:{user_id}:{key}".encode()).hexdigest()[:24]
    led = _db()
    now = _now()

    with led._lock:
        old = led._db.execute("SELECT * FROM withdraw_requests WHERE idem_key=?", (key,)).fetchone()
        if old is not None:
            if old["user_id"] != user_id or int(old["usdt"]) != n_usdt or old["address"] != addr:
                raise PointsError("POINTS_IDEMPOTENCY_CONFLICT")
            return dict(old), True
        pending = led._db.execute(
            "SELECT COUNT(*) FROM withdraw_requests WHERE user_id=? AND status='pending'", (user_id,)
        ).fetchone()[0]
        if pending >= c["maxPending"]:
            raise PointsError("WITHDRAW_TOO_MANY_PENDING")
        day0 = _bj_day_start(now)
        used = led._db.execute(
            "SELECT COALESCE(SUM(usdt),0) FROM withdraw_requests WHERE user_id=? AND created_at>=? "
            "AND status IN ('pending','approved','completed')",
            (user_id, day0),
        ).fetchone()[0]
        if int(used) + n_usdt > c["dailyMax"]:
            raise PointsError("WITHDRAW_DAILY_LIMIT")

    # freeze points (outside the withdraw-table lock held above — append has its own lock)
    row, replayed = led.append(
        user_id=user_id,
        kind="withdraw_freeze",
        amount=-points,
        reason=f"withdraw freeze {n_usdt} USDT",
        operator=f"user:{user_id}",
        idem_key=f"withdraw_freeze:{order_id}",
        fingerprint=_fp(a="withdraw_freeze", o=order_id, u=user_id, p=points, addr=addr),
        ref=f"withdraw:{order_id}",
        provider="withdraw_trc20",
    )
    if replayed:
        existing = get_request(order_id)
        if existing:
            return existing, True

    with led._lock:
        led._db.execute(
            "INSERT INTO withdraw_requests(id,user_id,points,usdt,amount_micro,address,status,created_at,updated_at,"
            "idem_key,freeze_entry_id) VALUES (?,?,?,?,?,?, 'pending',?,?,?,?)",
            (order_id, user_id, points, n_usdt, amount_micro, addr, now, now, key, int(row["id"])),
        )
    return get_request(order_id) or {}, False


def reject(admin_id: str, request_id: str, reason: str) -> dict[str, Any]:
    why = (reason or "").strip()
    if len(why) < 2:
        raise PointsError("POINTS_REASON_REQUIRED")
    if len(why) > 200:
        raise PointsError("POINTS_REASON_TOO_LONG")
    led = _db()
    req = get_request(request_id)
    if req is None:
        raise PointsError("WITHDRAW_UNKNOWN")
    if req["status"] != "pending":
        raise PointsError("WITHDRAW_NOT_PENDING")
    entry, _ = led.append(
        user_id=req["user_id"],
        kind="withdraw_unfreeze",
        amount=int(req["points"]),
        reason=f"withdraw reject: {why}"[:200],
        operator=f"admin:{admin_id}",
        idem_key=f"withdraw_unfreeze:{request_id}",
        fingerprint=_fp(a="withdraw_unfreeze", o=request_id, p=int(req["points"]), r=why),
        ref=f"withdraw:{request_id}",
        provider="withdraw_trc20",
    )
    now = _now()
    with led._lock:
        led._db.execute(
            "UPDATE withdraw_requests SET status='rejected', updated_at=?, unfreeze_entry_id=?, reviewed_by=?, "
            "reviewed_at=?, note=? WHERE id=? AND status='pending'",
            (now, int(entry["id"]), admin_id, now, why[:200], request_id),
        )
    out = get_request(request_id)
    if out is None or out["status"] != "rejected":
        raise PointsError("WITHDRAW_NOT_PENDING")
    return out


def _fetch_outgoing_usdt(client: trc20.ChainClient, tx: str, to_addr: str) -> Optional[dict[str, Any]]:
    """Read one tx; return {value, from, to, ts} if it is a confirmed official USDT transfer to to_addr."""
    # TronGrid gettransactioninfobyid (non-solidity) for block timestamp + logs
    try:
        info = client.http(
            f"{client.trongrid}/wallet/gettransactioninfobyid",
            headers=trc20._tg_headers(),  # noqa: SLF001 — shared read-only headers
            data={"value": tx},
        )
    except Exception as exc:
        log.warning("withdraw tx lookup failed: %s", exc)
        info = None
    if not isinstance(info, dict) or not info.get("id"):
        # fallback: solidity node (may lack blockTimeStamp)
        try:
            info = client.http(
                f"{client.trongrid}/walletsolidity/gettransactioninfobyid",
                headers=trc20._tg_headers(),
                data={"value": tx},
            )
        except Exception:
            return None
    if not isinstance(info, dict) or not info.get("id"):
        return None
    receipt = (info.get("receipt") or {}).get("result")
    if receipt not in (None, "SUCCESS"):
        return None
    want_contract, want_to = trc20.hex20(trc20.USDT_CONTRACT), trc20.hex20(to_addr)
    for lg in info.get("log") or []:
        topics = lg.get("topics") or []
        if (
            str(lg.get("address") or "").lower()[-40:] == want_contract
            and len(topics) >= 3
            and topics[0].lower() == trc20.TRANSFER_TOPIC
            and topics[2].lower()[-40:] == want_to
        ):
            value = int(str(lg.get("data") or "0") or "0", 16)
            from_addr = trc20.from_hex20(topics[1])
            return {
                "value": value,
                "from": from_addr,
                "to": to_addr,
                "ts": int(info.get("blockTimeStamp") or 0),
            }
    return None


def complete(
    admin_id: str,
    request_id: str,
    tx_hash: Any,
    reason: str,
    *,
    client: Optional[trc20.ChainClient] = None,
    skip_chain: bool = False,
) -> dict[str, Any]:
    """Register a manual payout: verify on-chain then mark completed. Never moves private keys."""
    why = (reason or "").strip()
    if len(why) < 2:
        raise PointsError("POINTS_REASON_REQUIRED")
    if len(why) > 200:
        raise PointsError("POINTS_REASON_TOO_LONG")
    tx = (tx_hash if isinstance(tx_hash, str) else "").strip().lower().removeprefix("0x")
    if len(tx) != 64 or any(c not in "0123456789abcdef" for c in tx):
        raise PointsError("WITHDRAW_BAD_TX")
    req = get_request(request_id)
    if req is None:
        raise PointsError("WITHDRAW_UNKNOWN")
    if req["status"] != "pending":
        raise PointsError("WITHDRAW_NOT_PENDING")

    # refuse reusing a tx hash already bound to another withdrawal
    led = _db()
    with led._lock:
        clash = led._db.execute(
            "SELECT id FROM withdraw_requests WHERE tx_hash=? AND id<>?", (tx, request_id)
        ).fetchone()
    if clash is not None:
        raise PointsError("WITHDRAW_TX_USED")

    if not skip_chain:
        cli = client or trc20.ChainClient()
        got = _fetch_outgoing_usdt(cli, tx, req["address"])
        if got is None:
            raise PointsError("WITHDRAW_TX_NOT_FOUND")
        if int(got["value"]) != int(req["amount_micro"]):
            raise PointsError("WITHDRAW_TX_AMOUNT")
        # prefer solidified confirmation when available
        solid = cli.verify_solid(tx, req["address"], int(req["amount_micro"]))
        if solid is False:
            raise PointsError("WITHDRAW_TX_VERIFY")
        # solid is None (network) or True: accept when gettransactioninfobyid already showed the Transfer

    entry, replayed = led.append(
        user_id=req["user_id"],
        kind="withdraw_complete",
        amount=0,
        reason=f"withdraw complete tx {tx[:12]}…",
        operator=f"admin:{admin_id}",
        idem_key=f"withdraw_complete:{request_id}",
        fingerprint=_fp(a="withdraw_complete", o=request_id, tx=tx, p=int(req["points"])),
        ref=f"withdraw:{request_id}:tx:{tx}",
        provider="withdraw_trc20",
    )
    now = _now()
    with led._lock:
        cur = led._db.execute(
            "UPDATE withdraw_requests SET status='completed', updated_at=?, complete_entry_id=?, tx_hash=?, "
            "reviewed_by=?, reviewed_at=?, note=? WHERE id=? AND status='pending'",
            (now, int(entry["id"]), tx, admin_id, now, why[:200], request_id),
        )
        if cur.rowcount == 0 and not replayed:
            raise PointsError("WITHDRAW_NOT_PENDING")
    out = get_request(request_id)
    if out is None:
        raise PointsError("WITHDRAW_UNKNOWN")
    return out


def feature_flags() -> dict[str, Any]:
    return {
        "withdraw": {"enabled": enabled(), "adminOnly": admin_only(), **{k: config()[k] for k in ("min", "max", "dailyMax", "rate")}},
        "trc20Topup": {"enabled": trc20.enabled(), "adminOnly": trc20.admin_only()},
    }
