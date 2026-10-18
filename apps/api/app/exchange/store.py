"""SQLite store: credentials (ciphertext only), settings, orders (idempotency), audit."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.data_paths import data_dir

_LOCK = threading.RLock()
_TLS = threading.local()
_GEN = 0  # bumped by reset() so every thread reopens


def _path() -> str:
    raw = os.getenv("AUU_EXCHANGE_STORE", "").strip()
    return raw or str(Path(data_dir()) / "exchange.sqlite")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS creds(user_id TEXT, exchange TEXT, blob TEXT, masked TEXT,
  perms TEXT, created_ts INTEGER, PRIMARY KEY(user_id, exchange));
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS orders(user_id TEXT, exchange TEXT, coid TEXT, symbol TEXT,
  request TEXT, status TEXT, result TEXT, ts INTEGER, PRIMARY KEY(user_id, exchange, coid));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER,
  user_id TEXT, actor TEXT, action TEXT, detail TEXT);
"""


def conn() -> sqlite3.Connection:
    """One SQLite connection per thread (FastAPI runs sync routes in a threadpool)."""
    p = _path()
    c = getattr(_TLS, "conn", None)
    if c is not None and getattr(_TLS, "path", None) == p and getattr(_TLS, "gen", -1) == _GEN:
        return c
    if c is not None:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p, timeout=10, isolation_level=None)
    c.execute("PRAGMA busy_timeout=10000")
    with _LOCK:
        c.executescript(_SCHEMA)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    _TLS.conn, _TLS.path, _TLS.gen = c, p, _GEN
    return c


def reset() -> None:
    global _GEN
    with _LOCK:
        _GEN += 1
        c = getattr(_TLS, "conn", None)
        if c is not None:
            c.close()
            _TLS.conn = None


DEFAULTS: dict[str, Any] = {
    "enabled": False,          # global feature toggle, DEFAULT OFF
    "allowlistOnly": True,     # when enabled, only allowlisted users unless turned off
    "allowlist": [],           # user ids
    "killSwitch": False,       # true = no new orders anywhere (cancel / reduce-only close still allowed)
    "maxOrderUsdt": 50.0,      # default per-order notional cap (USDT)
    "userCaps": {},            # user id -> cap override (USDT)
    "maxLeverage": 5,          # futures: max leverage a user may set
    "maxFuturesUsdt": 50.0,    # futures: default per-order notional cap (USDT), separate from spot
    "userFuturesCaps": {},     # user id -> futures cap override (USDT)
    "futuresAcks": {},         # user id -> ts of first-time futures risk acknowledgement
}


def now_ms() -> int:
    return int(time.time() * 1000)


# settings
def settings() -> dict[str, Any]:
    out = json.loads(json.dumps(DEFAULTS))
    for k, v in conn().execute("SELECT k, v FROM settings"):
        if k in out:
            out[k] = json.loads(v)
    return out


def set_setting(k: str, v: Any) -> None:
    if k not in DEFAULTS:
        raise KeyError(k)
    with _LOCK:
        conn().execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (k, json.dumps(v)))


# audit (never pass secrets in detail)
def audit(user_id: str, actor: str, action: str, detail: Optional[dict[str, Any]] = None) -> None:
    with _LOCK:
        conn().execute(
            "INSERT INTO audit(ts, user_id, actor, action, detail) VALUES(?,?,?,?,?)",
            (now_ms(), user_id, actor, action, json.dumps(detail or {}, ensure_ascii=False, default=str)),
        )


def audit_rows(user_id: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
    q = "SELECT ts, user_id, actor, action, detail FROM audit"
    args: tuple = ()
    if user_id:
        q += " WHERE user_id=?"
        args = (user_id,)
    q += " ORDER BY id DESC LIMIT ?"
    rows = conn().execute(q, args + (int(limit),)).fetchall()
    return [{"ts": r[0], "userId": r[1], "actor": r[2], "action": r[3], "detail": json.loads(r[4])} for r in rows]


# credentials
def put_cred(user_id: str, exchange: str, blob: dict, masked: str, perms: dict) -> None:
    with _LOCK:
        conn().execute(
            "INSERT OR REPLACE INTO creds VALUES(?,?,?,?,?,?)",
            (user_id, exchange, json.dumps(blob), masked, json.dumps(perms), now_ms()),
        )


def get_cred(user_id: str, exchange: str) -> Optional[dict[str, Any]]:
    r = conn().execute(
        "SELECT blob, masked, perms, created_ts FROM creds WHERE user_id=? AND exchange=?", (user_id, exchange)
    ).fetchone()
    if not r:
        return None
    return {"blob": json.loads(r[0]), "masked": r[1], "perms": json.loads(r[2]), "createdTs": r[3]}


def list_creds(user_id: str) -> list[dict[str, Any]]:
    rows = conn().execute(
        "SELECT exchange, masked, perms, created_ts FROM creds WHERE user_id=? ORDER BY exchange", (user_id,)
    ).fetchall()
    return [{"exchange": r[0], "masked": r[1], "perms": json.loads(r[2]), "createdTs": r[3]} for r in rows]


def del_cred(user_id: str, exchange: str) -> bool:
    with _LOCK:
        cur = conn().execute("DELETE FROM creds WHERE user_id=? AND exchange=?", (user_id, exchange))
        return cur.rowcount > 0


def count_creds() -> dict[str, int]:
    return {r[0]: r[1] for r in conn().execute("SELECT exchange, COUNT(*) FROM creds GROUP BY exchange")}


# orders (idempotency)
def get_order(user_id: str, exchange: str, coid: str) -> Optional[dict[str, Any]]:
    r = conn().execute(
        "SELECT symbol, request, status, result, ts FROM orders WHERE user_id=? AND exchange=? AND coid=?",
        (user_id, exchange, coid),
    ).fetchone()
    if not r:
        return None
    return {"symbol": r[0], "request": json.loads(r[1]), "status": r[2], "result": json.loads(r[3] or "null"), "ts": r[4]}


def claim_order(user_id: str, exchange: str, coid: str, symbol: str, request: dict) -> bool:
    """Insert a pending row; False if this clientOrderId already exists (no resend)."""
    with _LOCK:
        try:
            conn().execute(
                "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
                (user_id, exchange, coid, symbol, json.dumps(request), "pending", None, now_ms()),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def finish_order(user_id: str, exchange: str, coid: str, status: str, result: Any) -> None:
    with _LOCK:
        conn().execute(
            "UPDATE orders SET status=?, result=? WHERE user_id=? AND exchange=? AND coid=?",
            (status, json.dumps(result, default=str), user_id, exchange, coid),
        )


def recent_same(user_id: str, exchange: str, request: dict, window_ms: int) -> bool:
    """True if this user sent an identical order (same symbol/side/type/amount/price/reduceOnly) within the window."""
    keys = ("symbol", "side", "type", "amount", "price", "reduceOnly")
    rows = conn().execute(
        "SELECT request FROM orders WHERE user_id=? AND exchange=? AND ts>? AND status!='rejected'",
        (user_id, exchange, now_ms() - int(window_ms)),
    ).fetchall()
    for (raw,) in rows:
        try:
            old = json.loads(raw)
        except ValueError:
            continue
        if all(old.get(k) == request.get(k) for k in keys):
            return True
    return False
