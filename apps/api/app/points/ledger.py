"""Append-only points ledger (one sqlite file, separate from every paper ledger).

* ``points_ledger``: one row per change (topup / spend / adjust / refund) with reason, operator,
  time, idempotency key and a hash chain. UPDATE and DELETE are refused by triggers.
* Balance = SUM(amount) per user; ``balance_after`` is a convenience copy that ``verify()``
  reconciles against the running sum, together with the hash chain and the per-kind sign rules.
* ``points_audit``: append-only log of every points action and refusal (admin and user).
* ``points_orders``: top-up orders of the payment channels (status only moves pending -> paid/failed).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.data_paths import data_dir, guarded_path

KINDS = ("topup", "spend", "adjust", "refund")
MAX_ENTRY = 10_000_000  # |amount| of one row
MAX_BALANCE = 1_000_000_000
GENESIS = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS points_ledger (
  id INTEGER PRIMARY KEY,
  user_id TEXT NOT NULL,
  ts INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('topup','spend','adjust','refund')),
  amount INTEGER NOT NULL CHECK (amount <> 0 AND abs(amount) <= 10000000),
  balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
  reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 200),
  operator TEXT NOT NULL,
  provider TEXT,
  ref TEXT,
  idem_key TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS points_ledger_user ON points_ledger(user_id, id);
CREATE TRIGGER IF NOT EXISTS points_ledger_no_update BEFORE UPDATE ON points_ledger
BEGIN SELECT RAISE(ABORT, 'points ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS points_ledger_no_delete BEFORE DELETE ON points_ledger
BEGIN SELECT RAISE(ABORT, 'points ledger is append-only'); END;

CREATE TABLE IF NOT EXISTS points_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  result TEXT NOT NULL,
  ip TEXT,
  ua TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS points_audit_ts ON points_audit(ts);
CREATE TRIGGER IF NOT EXISTS points_audit_no_update BEFORE UPDATE ON points_audit
BEGIN SELECT RAISE(ABORT, 'points audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS points_audit_no_delete BEFORE DELETE ON points_audit
BEGIN SELECT RAISE(ABORT, 'points audit log is append-only'); END;

CREATE TABLE IF NOT EXISTS points_orders (
  order_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  points INTEGER NOT NULL CHECK (points > 0),
  status TEXT NOT NULL CHECK (status IN ('pending','paid','failed')),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  external_ref TEXT,
  entry_id INTEGER
);
CREATE TRIGGER IF NOT EXISTS points_orders_no_delete BEFORE DELETE ON points_orders
BEGIN SELECT RAISE(ABORT, 'points orders are kept'); END;
CREATE TRIGGER IF NOT EXISTS points_orders_final BEFORE UPDATE ON points_orders
WHEN OLD.status <> 'pending' OR NEW.order_id <> OLD.order_id OR NEW.user_id <> OLD.user_id
  OR NEW.points <> OLD.points OR NEW.provider <> OLD.provider
BEGIN SELECT RAISE(ABORT, 'points order is final'); END;
"""

_ENTRY_COLS = ("id", "user_id", "ts", "kind", "amount", "balance_after", "reason", "operator", "provider", "ref", "idem_key",
               "fingerprint", "prev_hash")


class PointsError(ValueError):
    """Stable error code (POINTS_*), translated on the client."""


def entry_hash(row: dict[str, Any]) -> str:
    body = json.dumps({k: row.get(k) for k in _ENTRY_COLS}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _sign_ok(kind: str, amount: int) -> bool:
    if kind == "topup":
        return amount > 0
    if kind == "spend":
        return amount < 0
    return amount != 0  # adjust: either way; refund: opposite of the refunded entry (checked on write)


def store_path() -> Path:
    raw = (os.getenv("AUU_POINTS_STORE") or "").strip()
    return Path(raw).expanduser() if raw else data_dir() / "points.sqlite"


class PointsLedger:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = guarded_path(Path(path) if path else store_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- writes ---------------------------------------------------------------------------
    def append(self, *, user_id: str, kind: str, amount: int, reason: str, operator: str, idem_key: str, fingerprint: str,
               provider: Optional[str] = None, ref: Optional[str] = None,
               check: Optional[Callable[[sqlite3.Connection, int], None]] = None,
               now_ms: Optional[int] = None) -> tuple[dict[str, Any], bool]:
        """Append one row atomically. Returns (row, replayed). Same idempotency key + same fingerprint
        returns the stored row (replayed=True); same key with a different request is a conflict."""
        if kind not in KINDS:
            raise PointsError("POINTS_BAD_KIND")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0 or abs(amount) > MAX_ENTRY or not _sign_ok(kind, amount):
            raise PointsError("POINTS_BAD_AMOUNT")
        reason = (reason or "").strip()
        if not (1 <= len(reason) <= 200):
            raise PointsError("POINTS_REASON_REQUIRED")
        if not user_id or not operator or not idem_key or not fingerprint:
            raise PointsError("POINTS_BAD_REQUEST")
        with self._lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                old = db.execute("SELECT * FROM points_ledger WHERE idem_key=?", (idem_key,)).fetchone()
                if old is not None:
                    db.execute("ROLLBACK")
                    if old["fingerprint"] != fingerprint:
                        raise PointsError("POINTS_IDEMPOTENCY_CONFLICT")
                    return dict(old), True
                bal = int(db.execute("SELECT COALESCE(SUM(amount), 0) FROM points_ledger WHERE user_id=?", (user_id,)).fetchone()[0])
                if check is not None:
                    check(db, bal)
                new = bal + amount
                if new < 0:
                    raise PointsError("POINTS_INSUFFICIENT")
                if new > MAX_BALANCE:
                    raise PointsError("POINTS_TOO_LARGE")
                head = db.execute("SELECT id, hash FROM points_ledger ORDER BY id DESC LIMIT 1").fetchone()
                row = {"id": (int(head["id"]) + 1) if head else 1, "user_id": user_id,
                       "ts": int(now_ms if now_ms is not None else time.time() * 1000), "kind": kind, "amount": int(amount),
                       "balance_after": new, "reason": reason, "operator": operator, "provider": provider, "ref": ref,
                       "idem_key": idem_key, "fingerprint": fingerprint, "prev_hash": head["hash"] if head else GENESIS}
                row["hash"] = entry_hash(row)
                cols = ",".join(row)
                db.execute(f"INSERT INTO points_ledger({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))
                db.execute("COMMIT")
                return row, False
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def audit(self, *, actor: str, action: str, result: str, target: str = "", ip: str = "", ua: str = "",
              detail: Optional[dict[str, Any]] = None) -> None:
        with self._lock:
            self._db.execute("INSERT INTO points_audit(ts, actor, action, target, result, ip, ua, detail) VALUES (?,?,?,?,?,?,?,?)",
                             (int(time.time() * 1000), actor, action, target or "", result, (ip or "")[:64], (ua or "")[:200],
                              json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)[:2000]))

    # orders (payment channels)
    def create_order(self, *, order_id: str, user_id: str, provider: str, points: int) -> dict[str, Any]:
        now = int(time.time() * 1000)
        with self._lock:
            self._db.execute("INSERT INTO points_orders(order_id, user_id, provider, points, status, created_at, updated_at)"
                             " VALUES (?,?,?,?, 'pending', ?, ?)", (order_id, user_id, provider, int(points), now, now))
        return self.order(order_id) or {}

    def finish_order(self, order_id: str, status: str, *, external_ref: Optional[str] = None, entry_id: Optional[int] = None) -> None:
        if status not in ("paid", "failed"):
            raise PointsError("POINTS_BAD_REQUEST")
        with self._lock:
            self._db.execute("UPDATE points_orders SET status=?, updated_at=?, external_ref=?, entry_id=? WHERE order_id=?",
                             (status, int(time.time() * 1000), external_ref, entry_id, order_id))

    def order(self, order_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM points_orders WHERE order_id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    # ---- reads ----------------------------------------------------------------------------
    def balance(self, user_id: str) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COALESCE(SUM(amount), 0) FROM points_ledger WHERE user_id=?", (user_id,)).fetchone()[0])

    def entries(self, user_id: Optional[str] = None, *, limit: int = 50, before_id: Optional[int] = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM points_ledger WHERE 1=1", []
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        if before_id is not None:
            sql += " AND id<?"
            args.append(int(before_id))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def iter_all(self, user_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            if user_id is None:
                rows = self._db.execute("SELECT * FROM points_ledger ORDER BY id").fetchall()
            else:
                rows = self._db.execute("SELECT * FROM points_ledger WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def entry(self, entry_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM points_ledger WHERE id=?", (int(entry_id),)).fetchone()
        return dict(row) if row else None

    def refunded(self, db: sqlite3.Connection, entry_id: int) -> int:
        """Absolute points already refunded against one entry (call inside append's check)."""
        return int(db.execute("SELECT COALESCE(SUM(abs(amount)), 0) FROM points_ledger WHERE kind='refund' AND ref=?",
                              (f"entry:{int(entry_id)}",)).fetchone()[0])

    def audit_rows(self, limit: int = 50, target: Optional[str] = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM points_audit", []
        if target:
            sql += " WHERE target=?"
            args.append(target)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def verify(self) -> dict[str, Any]:
        """Reconcile: hash chain, per-kind signs, running balance == balance_after, no negative balance."""
        problems: list[str] = []
        running: dict[str, int] = {}
        prev = GENESIS
        n = 0
        for row in self.iter_all():
            n += 1
            rid = row["id"]
            if row["prev_hash"] != prev:
                problems.append(f"#{rid}: prev_hash does not link")
            if entry_hash(row) != row["hash"]:
                problems.append(f"#{rid}: hash mismatch")
            if not _sign_ok(row["kind"], int(row["amount"])):
                problems.append(f"#{rid}: sign not allowed for {row['kind']}")
            bal = running.get(row["user_id"], 0) + int(row["amount"])
            running[row["user_id"]] = bal
            if bal != int(row["balance_after"]):
                problems.append(f"#{rid}: balance_after {row['balance_after']} != running {bal}")
            if bal < 0:
                problems.append(f"#{rid}: negative balance")
            prev = row["hash"]
        with self._lock:
            sums = {r[0]: int(r[1]) for r in self._db.execute("SELECT user_id, SUM(amount) FROM points_ledger GROUP BY user_id")}
        if sums != running:
            problems.append("per-user SUM differs from the running balance")
        return {"ok": not problems, "entries": n, "users": len(running), "total": sum(running.values()), "headHash": prev,
                "problems": problems[:50]}


_ledger: Optional[PointsLedger] = None
_glock = threading.Lock()


def get_points_ledger() -> PointsLedger:
    global _ledger
    with _glock:
        if _ledger is None:
            _ledger = PointsLedger()
        return _ledger


def reset_points_ledger(ledger: Optional[PointsLedger] = None) -> None:
    global _ledger
    with _glock:
        if _ledger is not None and _ledger is not ledger:
            try:
                _ledger.close()
            except Exception:
                pass
        _ledger = ledger


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m app.points.ledger verify`` -> reconciliation report (exit 1 on any problem)."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["verify"]:
        print("usage: python -m app.points.ledger verify")
        return 2
    out = get_points_ledger().verify()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
