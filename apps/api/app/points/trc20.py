"""USDT-TRC20 top-up channel (watch-only: the server knows the receive address, never a key).

Flow
  1. The user picks a whole USDT amount (min..max). We create an order whose amount carries a
     unique 4-decimal tail (e.g. 50.0137 USDT), valid ``AUU_TRC20_ORDER_TTL_MIN`` (30) minutes.
     No other order (any user, any status) used that exact amount in the last 24 h, so a payment
     can always be traced back to one order, even when it arrives late.
  2. A primary-only background loop polls TronGrid (fallback Tronscan) every 30-60 s for
     *confirmed* TRC20 transfers into the receive address. Only the official USDT contract
     ``TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t`` counts. Each candidate is re-checked on the solidity
     node (``walletsolidity/gettransactioninfobyid``: receipt SUCCESS + Transfer log of the USDT
     contract to our address for exactly that value) and must be older than
     ``AUU_TRC20_MIN_AGE_SEC`` (60) before it can credit anything.
  3. Exact amount + inside the order window -> points credited once, idempotency key = tx hash.
     Anything else (amount matches no order, paid after expiry, second payment of a paid order,
     look-alike "USDT" contract) goes to the manual review list and credits nothing. An admin can
     attribute it to a user (password + 2FA + reason) or dismiss it.

Config (environment only; there is deliberately no web endpoint that changes the address):
  AUU_TRC20_RECEIVE_ADDRESS  base58 T-address. Unset/invalid = channel closed.
  AUU_TRC20_RATE (100 points per USDT) · AUU_TRC20_MIN (10) · AUU_TRC20_MAX (1000) USDT per order
  AUU_TRC20_DAILY_MAX (3000 USDT per user per Beijing day, pending + paid)
  AUU_TRC20_MAX_PENDING (3 open orders per user) · AUU_TRC20_ORDER_TTL_MIN (30)
  AUU_TRC20_POLL_SEC (45, clamped 30..60) · AUU_TRC20_MIN_AGE_SEC (60) · AUU_TRC20_POLL (on)
  AUU_TRONGRID_API_KEY / AUU_TRONSCAN_API_KEY (optional) · AUU_TRC20_ALERT_TO (douriff3@gmail.com)
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.points.ledger import PointsError, get_points_ledger

log = logging.getLogger("auu.points.trc20")

PROVIDER_ID = "usdt_trc20"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_DECIMALS = 6
MICRO = 10 ** USDT_DECIMALS
TAIL_UNIT = 100  # 0.0001 USDT in micro units
TRANSFER_TOPIC = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
REUSE_BLOCK_MS = 24 * 3600 * 1000
EARLY_SLACK_MS = 120_000  # chain clock vs ours
_BJ = timezone(timedelta(hours=8))
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trc20_orders (
  order_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  base_usdt INTEGER NOT NULL CHECK (base_usdt > 0),
  amount_micro INTEGER NOT NULL CHECK (amount_micro > 0),
  points INTEGER NOT NULL CHECK (points > 0),
  address TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','paid','expired')),
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  idem_key TEXT NOT NULL UNIQUE,
  tx_hash TEXT,
  entry_id INTEGER
);
CREATE INDEX IF NOT EXISTS trc20_orders_amount ON trc20_orders(amount_micro, created_at);
CREATE INDEX IF NOT EXISTS trc20_orders_user ON trc20_orders(user_id, created_at);
CREATE TRIGGER IF NOT EXISTS trc20_orders_no_delete BEFORE DELETE ON trc20_orders
BEGIN SELECT RAISE(ABORT, 'trc20 orders are kept'); END;
CREATE TRIGGER IF NOT EXISTS trc20_orders_final BEFORE UPDATE ON trc20_orders
WHEN OLD.status = 'paid' OR NEW.order_id <> OLD.order_id OR NEW.user_id <> OLD.user_id
  OR NEW.amount_micro <> OLD.amount_micro OR NEW.points <> OLD.points OR NEW.address <> OLD.address
BEGIN SELECT RAISE(ABORT, 'trc20 order is final'); END;

CREATE TABLE IF NOT EXISTS trc20_txs (
  tx_hash TEXT PRIMARY KEY,
  block_ts INTEGER NOT NULL,
  from_addr TEXT,
  to_addr TEXT NOT NULL,
  contract TEXT NOT NULL,
  symbol TEXT,
  amount_micro INTEGER NOT NULL,
  source TEXT NOT NULL,
  seen_at INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('matched','exception','resolved','ignored')),
  reason TEXT,
  order_id TEXT,
  user_id TEXT,
  points INTEGER,
  entry_id INTEGER,
  resolved_by TEXT,
  resolved_at INTEGER,
  note TEXT
);
CREATE INDEX IF NOT EXISTS trc20_txs_status ON trc20_txs(status, block_ts);
CREATE TRIGGER IF NOT EXISTS trc20_txs_no_delete BEFORE DELETE ON trc20_txs
BEGIN SELECT RAISE(ABORT, 'trc20 transfers are kept'); END;
CREATE TRIGGER IF NOT EXISTS trc20_txs_final BEFORE UPDATE ON trc20_txs
WHEN OLD.status IN ('matched','resolved','ignored') OR NEW.tx_hash <> OLD.tx_hash
  OR NEW.amount_micro <> OLD.amount_micro OR NEW.contract <> OLD.contract
BEGIN SELECT RAISE(ABORT, 'trc20 transfer is final'); END;

CREATE TABLE IF NOT EXISTS trc20_state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


# ---- address helpers -----------------------------------------------------------------------------
def b58decode_check(addr: str) -> Optional[bytes]:
    if not isinstance(addr, str) or not (25 <= len(addr) <= 40):
        return None
    n = 0
    for ch in addr:
        i = _B58.find(ch)
        if i < 0:
            return None
        n = n * 58 + i
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\0" * (len(addr) - len(addr.lstrip("1"))) + raw
    if len(raw) != 25:
        return None
    body, chk = raw[:21], raw[21:]
    if hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != chk:
        return None
    return body


def b58encode_check(body: bytes) -> str:
    raw = body + hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    n = int.from_bytes(raw, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + s


def valid_address(addr: str) -> bool:
    body = b58decode_check((addr or "").strip())
    return body is not None and body[0] == 0x41


def hex20(addr: str) -> str:
    """Base58 T-address -> 20-byte hex (as used in event logs)."""
    body = b58decode_check(addr)
    return body[1:].hex() if body else ""


def from_hex20(h: str) -> str:
    h = (h or "").lower().removeprefix("0x")[-40:]
    return b58encode_check(bytes.fromhex("41" + h)) if len(h) == 40 else ""


def fmt_usdt(micro: int) -> str:
    whole, frac = divmod(int(micro), MICRO)
    return f"{whole}.{frac:06d}".rstrip("0").rstrip(".") if frac else f"{whole}"


def fmt_order_amount(micro: int) -> str:
    """Always 4 decimals so the user copies the exact figure (50.0137)."""
    whole, frac = divmod(int(micro), MICRO)
    return f"{whole}.{frac // TAIL_UNIT:04d}"


# ---- config --------------------------------------------------------------------------------------
def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(str(os.getenv(name, "") or default).strip())
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def receive_address() -> str:
    a = (os.getenv("AUU_TRC20_RECEIVE_ADDRESS") or "").strip()
    return a if valid_address(a) else ""


def config() -> dict[str, Any]:
    lo = _int("AUU_TRC20_MIN", 10, 1, 1_000_000)
    hi = max(lo, _int("AUU_TRC20_MAX", 1000, 1, 1_000_000))
    return {
        "address": receive_address(),
        "addressConfigured": bool((os.getenv("AUU_TRC20_RECEIVE_ADDRESS") or "").strip()),
        "rate": _int("AUU_TRC20_RATE", 100, 1, 100_000),
        "min": lo,
        "max": hi,
        "dailyMax": _int("AUU_TRC20_DAILY_MAX", 3000, 1, 10_000_000),
        "maxPending": _int("AUU_TRC20_MAX_PENDING", 3, 1, 20),
        "ttlMin": _int("AUU_TRC20_ORDER_TTL_MIN", 30, 5, 120),
        "pollSec": _int("AUU_TRC20_POLL_SEC", 45, 30, 60),
        "minAgeSec": _int("AUU_TRC20_MIN_AGE_SEC", 60, 30, 3600),
        "contract": USDT_CONTRACT,
        "network": "TRC20",
    }


def enabled() -> bool:
    return bool(receive_address())


def poll_wanted() -> bool:
    """Configured to poll on this node: address set, primary role (a standby only takes over after failover)."""
    from app.role import is_standby

    if not enabled() or is_standby():
        return False
    return os.getenv("AUU_TRC20_POLL", "on").strip().lower() not in {"0", "false", "off", "no"}


def poll_enabled() -> bool:
    from app.data_paths import tests_active

    return poll_wanted() and not tests_active()


def public_config() -> dict[str, Any]:
    from app.role import is_standby

    c = config()
    return {"enabled": enabled() and not is_standby(), "address": c["address"] or None, "rate": c["rate"], "min": c["min"], "max": c["max"],
            "dailyMax": c["dailyMax"], "maxPending": c["maxPending"], "ttlMin": c["ttlMin"], "network": "TRC20",
            "token": "USDT", "contract": USDT_CONTRACT}


def qr_data_uri(text: str) -> str:
    import segno

    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="png", scale=6, border=4, dark="#000000", light="#ffffff")
    return "data:image/png;base64," + base64.standard_b64encode(buf.getvalue()).decode("ascii")


# ---- store ---------------------------------------------------------------------------------------
def _db():
    led = get_points_ledger()
    if not getattr(led, "_trc20_schema", False):
        with led._lock:
            led._db.executescript(SCHEMA)
        led._trc20_schema = True
    return led


def _now() -> int:
    return int(time.time() * 1000)


def _bj_day_start(ms: int) -> int:
    d = datetime.fromtimestamp(ms / 1000, _BJ).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def _status_of(o: dict[str, Any], now: int) -> str:
    if o["status"] == "pending" and now > int(o["expires_at"]):
        return "expired"
    return o["status"]


def order_view(o: dict[str, Any], *, now: Optional[int] = None, admin: bool = False, qr: bool = False) -> dict[str, Any]:
    now = _now() if now is None else now
    out = {
        "orderId": o["order_id"], "status": _status_of(o, now), "usdt": int(o["base_usdt"]),
        "amount": fmt_order_amount(int(o["amount_micro"])), "points": int(o["points"]), "address": o["address"],
        "network": "TRC20", "createdAt": int(o["created_at"]), "expiresAt": int(o["expires_at"]),
        "txHash": o.get("tx_hash"), "secondsLeft": max(0, (int(o["expires_at"]) - now) // 1000),
    }
    if admin:
        out["userId"] = o["user_id"]
        out["entryId"] = o.get("entry_id")
    if qr and out["status"] == "pending":
        out["qr"] = qr_data_uri(o["address"])
    return out


def _parse_usdt(value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        raise PointsError("TRC20_BAD_AMOUNT")
    if isinstance(value, int):
        n = value
    elif isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 9:
        n = int(value.strip())
    else:
        raise PointsError("TRC20_BAD_AMOUNT")
    if not (lo <= n <= hi):
        raise PointsError("TRC20_BAD_AMOUNT")
    return n


def create_order(user_id: str, usdt: Any, idem: Any, *, now: Optional[int] = None,
                 rand: Callable[[int], int] = secrets.randbelow) -> tuple[dict[str, Any], bool]:
    """Returns (order row, replayed)."""
    if not enabled():
        raise PointsError("POINTS_TOPUP_CLOSED")
    from app.points.service import parse_idem

    c = config()
    n = _parse_usdt(usdt, c["min"], c["max"])
    key = parse_idem(idem)
    idem_key = f"trc20:{user_id}:{key}"
    now = _now() if now is None else now
    led = _db()
    with led._lock:
        db = led._db
        db.execute("BEGIN IMMEDIATE")
        try:
            old = db.execute("SELECT * FROM trc20_orders WHERE idem_key=?", (idem_key,)).fetchone()
            if old is not None:
                db.execute("ROLLBACK")
                if int(old["base_usdt"]) != n:
                    raise PointsError("POINTS_IDEMPOTENCY_CONFLICT")
                return dict(old), True
            pending = db.execute("SELECT COUNT(*) FROM trc20_orders WHERE user_id=? AND status='pending' AND expires_at>?",
                                 (user_id, now)).fetchone()[0]
            if pending >= c["maxPending"]:
                raise PointsError("TRC20_TOO_MANY_PENDING")
            day = db.execute("SELECT COALESCE(SUM(base_usdt),0) FROM trc20_orders WHERE user_id=? AND created_at>=? AND "
                             "(status='paid' OR (status='pending' AND expires_at>?))", (user_id, _bj_day_start(now), now)).fetchone()[0]
            if int(day) + n > c["dailyMax"]:
                raise PointsError("TRC20_DAILY_LIMIT")
            used = {int(r[0]) for r in db.execute(
                "SELECT amount_micro FROM trc20_orders WHERE amount_micro>=? AND amount_micro<? AND created_at>?",
                (n * MICRO, (n + 1) * MICRO, now - REUSE_BLOCK_MS))}
            amount = 0
            for _ in range(64):
                tail = 1 + rand(9999)  # 0.0001 .. 0.9999
                cand = n * MICRO + tail * TAIL_UNIT
                if cand not in used:
                    amount = cand
                    break
            if not amount:
                free = [n * MICRO + t * TAIL_UNIT for t in range(1, 10000) if n * MICRO + t * TAIL_UNIT not in used]
                if not free:
                    raise PointsError("TRC20_BUSY")
                amount = free[rand(len(free))]
            oid = "tu_" + secrets.token_hex(10)
            row = {"order_id": oid, "user_id": user_id, "base_usdt": n, "amount_micro": amount, "points": n * c["rate"],
                   "address": c["address"], "status": "pending", "created_at": now, "expires_at": now + c["ttlMin"] * 60_000,
                   "updated_at": now, "idem_key": idem_key}
            db.execute("INSERT INTO trc20_orders(order_id,user_id,base_usdt,amount_micro,points,address,status,created_at,expires_at,"
                       "updated_at,idem_key) VALUES (:order_id,:user_id,:base_usdt,:amount_micro,:points,:address,:status,:created_at,"
                       ":expires_at,:updated_at,:idem_key)", row)
            db.execute("COMMIT")
        except Exception:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    return row, False


def get_order(order_id: str) -> Optional[dict[str, Any]]:
    led = _db()
    with led._lock:
        r = led._db.execute("SELECT * FROM trc20_orders WHERE order_id=?", (order_id,)).fetchone()
    return dict(r) if r else None


def user_orders(user_id: str, limit: int = 30) -> list[dict[str, Any]]:
    led = _db()
    with led._lock:
        rows = led._db.execute("SELECT * FROM trc20_orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                               (user_id, max(1, min(limit, 200)))).fetchall()
    return [dict(r) for r in rows]


def all_orders(limit: int = 200) -> list[dict[str, Any]]:
    led = _db()
    with led._lock:
        rows = led._db.execute("SELECT * FROM trc20_orders ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
    return [dict(r) for r in rows]


def txs(status: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    led = _db()
    sql, args = "SELECT * FROM trc20_txs", []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY block_ts DESC LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    with led._lock:
        return [dict(r) for r in led._db.execute(sql, args).fetchall()]


def _state_get(k: str) -> Optional[str]:
    led = _db()
    with led._lock:
        r = led._db.execute("SELECT v FROM trc20_state WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def _state_set(k: str, v: Any) -> None:
    led = _db()
    with led._lock:
        led._db.execute("INSERT INTO trc20_state(k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (k, v if isinstance(v, str) else json.dumps(v)))


def expire_due(now: Optional[int] = None) -> int:
    """Mark pending orders expired once the window plus a confirmation grace has passed.
    A transfer mined inside the window still matches an expired order (block time decides)."""
    now = _now() if now is None else now
    grace = config()["minAgeSec"] * 1000 + 10 * 60_000
    led = _db()
    with led._lock:
        cur = led._db.execute("UPDATE trc20_orders SET status='expired', updated_at=? WHERE status='pending' AND expires_at<?",
                              (now, now - grace))
        return cur.rowcount


# ---- chain sources -------------------------------------------------------------------------------
def _http_json(url: str, *, headers: Optional[dict[str, str]] = None, data: Optional[dict] = None, timeout: float = 15.0) -> Any:
    h = {"Accept": "application/json", "User-Agent": "auu-trc20-watch/1"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _tg_headers() -> dict[str, str]:
    k = (os.getenv("AUU_TRONGRID_API_KEY") or "").strip()
    return {"TRON-PRO-API-KEY": k} if k else {}


def _ts_headers() -> dict[str, str]:
    k = (os.getenv("AUU_TRONSCAN_API_KEY") or "").strip()
    return {"TRON-PRO-API-KEY": k} if k else {}


class ChainClient:
    """Read-only access to TronGrid (primary) and Tronscan (fallback). ``http`` is injectable for tests."""

    def __init__(self, http: Callable[..., Any] = _http_json,
                 trongrid: str = "https://api.trongrid.io", tronscan: str = "https://apilist.tronscanapi.com"):
        self.http = http
        self.trongrid = trongrid.rstrip("/")
        self.tronscan = tronscan.rstrip("/")

    def incoming_trongrid(self, address: str, min_ts: int, pages: int = 5) -> list[dict[str, Any]]:
        """All confirmed TRC20 transfers *to* address since min_ts (any contract; filtered later)."""
        q = urllib.parse.urlencode({"only_to": "true", "only_confirmed": "true", "limit": 200, "min_timestamp": int(min_ts),
                                    "order_by": "block_timestamp,asc"})
        url = f"{self.trongrid}/v1/accounts/{address}/transactions/trc20?{q}"
        out: list[dict[str, Any]] = []
        for _ in range(pages):
            data = self.http(url, headers=_tg_headers())
            if not isinstance(data, dict) or data.get("success") is False:
                raise RuntimeError(f"trongrid: {str(data)[:120]}")
            for t in data.get("data") or []:
                ti = t.get("token_info") or {}
                if str(t.get("to") or "") != address or str(t.get("type") or "Transfer") != "Transfer":
                    continue
                out.append({"tx": str(t.get("transaction_id") or ""), "ts": int(t.get("block_timestamp") or 0),
                            "from": str(t.get("from") or ""), "to": address, "contract": str(ti.get("address") or ""),
                            "symbol": str(ti.get("symbol") or "")[:16], "decimals": int(ti.get("decimals") or 0),
                            "value": int(str(t.get("value") or "0")), "source": "trongrid", "confirmed": True})
            nxt = ((data.get("meta") or {}).get("links") or {}).get("next")
            if not nxt:
                break
            url = nxt
        return out

    def incoming_tronscan(self, address: str, min_ts: int) -> list[dict[str, Any]]:
        q = urllib.parse.urlencode({"limit": 50, "start": 0, "toAddress": address, "start_timestamp": int(min_ts),
                                    "confirm": "true", "filterTokenValue": 0})
        data = self.http(f"{self.tronscan}/api/token_trc20/transfers?{q}", headers=_ts_headers())
        rows = (data or {}).get("token_transfers") if isinstance(data, dict) else None
        if rows is None:
            raise RuntimeError(f"tronscan: {str(data)[:120]}")
        out = []
        for t in rows:
            if str(t.get("to_address") or "") != address:
                continue
            ok = bool(t.get("confirmed")) and str(t.get("finalResult") or "SUCCESS") == "SUCCESS" \
                and str(t.get("contractRet") or "SUCCESS") == "SUCCESS" and not t.get("revert")
            ti = t.get("tokenInfo") or {}
            out.append({"tx": str(t.get("transaction_id") or ""), "ts": int(t.get("block_ts") or 0),
                        "from": str(t.get("from_address") or ""), "to": address, "contract": str(t.get("contract_address") or ""),
                        "symbol": str(ti.get("tokenAbbr") or "")[:16], "decimals": int(ti.get("tokenDecimal") or 0),
                        "value": int(str(t.get("quant") or "0")), "source": "tronscan", "confirmed": ok})
        return out

    def incoming(self, address: str, min_ts: int) -> tuple[list[dict[str, Any]], str, Optional[str]]:
        try:
            return self.incoming_trongrid(address, min_ts), "trongrid", None
        except Exception as exc:
            err = f"trongrid {type(exc).__name__}: {str(exc)[:100]}"
            log.warning("TRC20 %s; trying tronscan", err)
        try:
            return self.incoming_tronscan(address, min_ts), "tronscan", err
        except Exception as exc:
            raise RuntimeError(f"{err}; tronscan {type(exc).__name__}: {str(exc)[:100]}") from exc

    def verify_solid(self, tx: str, address: str, value: int) -> Optional[bool]:
        """True: solidified, SUCCESS, USDT Transfer log to address for value. False: definitely not.
        None: could not ask (network) or not solidified yet."""
        try:
            info = self.http(f"{self.trongrid}/walletsolidity/gettransactioninfobyid", headers=_tg_headers(), data={"value": tx})
        except Exception:
            return None
        if not isinstance(info, dict) or not info.get("id"):
            return None  # not solidified yet (empty object)
        if (info.get("receipt") or {}).get("result") != "SUCCESS":
            return False
        want_contract, want_to = hex20(USDT_CONTRACT), hex20(address)
        for lg in info.get("log") or []:
            topics = lg.get("topics") or []
            if (str(lg.get("address") or "").lower()[-40:] == want_contract and len(topics) >= 3
                    and topics[0].lower() == TRANSFER_TOPIC and topics[2].lower()[-40:] == want_to
                    and int(str(lg.get("data") or "0") or "0", 16) == int(value)):
                return True
        return False


# ---- matching ------------------------------------------------------------------------------------
def _fp(**f: Any) -> str:
    return hashlib.sha256(json.dumps(f, sort_keys=True).encode()).hexdigest()


def _credit(tx_hash: str, user_id: str, points: int, *, operator: str, reason: str, ref: str) -> dict[str, Any]:
    row, _ = get_points_ledger().append(
        user_id=user_id, kind="topup", amount=int(points), reason=reason, operator=operator, provider=PROVIDER_ID,
        ref=ref, idem_key=f"trc20tx:{tx_hash}", fingerprint=_fp(a="trc20", tx=tx_hash, u=user_id, p=int(points)))
    return row


def _insert_tx(t: dict[str, Any], status: str, reason: Optional[str], *, order_id=None, user_id=None, points=None,
               entry_id=None, now: int) -> None:
    led = _db()
    with led._lock:
        led._db.execute(
            "INSERT OR IGNORE INTO trc20_txs(tx_hash,block_ts,from_addr,to_addr,contract,symbol,amount_micro,source,seen_at,status,"
            "reason,order_id,user_id,points,entry_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (t["tx"], t["ts"], t["from"], t["to"], t["contract"], t.get("symbol"), int(t["value"]), t["source"], now, status,
             reason, order_id, user_id, points, entry_id))


def _known_tx(tx: str) -> Optional[dict[str, Any]]:
    led = _db()
    with led._lock:
        r = led._db.execute("SELECT * FROM trc20_txs WHERE tx_hash=?", (tx,)).fetchone()
    return dict(r) if r else None


def _recent_amounts(now: int) -> set[int]:
    led = _db()
    with led._lock:
        return {int(r[0]) for r in led._db.execute("SELECT amount_micro FROM trc20_orders WHERE created_at>?", (now - 2 * REUSE_BLOCK_MS,))}


def process_transfer(t: dict[str, Any], client: ChainClient, *, now: Optional[int] = None,
                     notify: Optional[Callable[[str, str], None]] = None) -> str:
    """Handle one incoming transfer. Returns what happened (for logs/tests)."""
    now = _now() if now is None else now
    address = receive_address()
    tx = t.get("tx") or ""
    if not tx or t.get("to") != address:
        return "skip"
    known = _known_tx(tx)
    if known is not None:
        return "seen"
    if not t.get("confirmed"):
        return "unconfirmed"
    if now - int(t["ts"]) < config()["minAgeSec"] * 1000:
        return "young"
    official = t.get("contract") == USDT_CONTRACT
    if not official:
        sym = str(t.get("symbol") or "").upper()
        # look-alike USDT, or any token whose raw value looks like one of our order amounts -> review
        if "USD" in sym or int(t["value"]) in _recent_amounts(now):
            _insert_tx(t, "exception", "fake_contract", now=now)
            _notify(notify, "TRC20 待人工处理：非官方 USDT 合约", _tx_text(t, "fake_contract", address))
            return "fake_contract"
        return "ignored_token"
    if t.get("decimals") not in (None, 0, USDT_DECIMALS):
        return "skip"
    ok = client.verify_solid(tx, address, int(t["value"]))
    if ok is None:
        if t.get("source") == "tronscan" and t.get("confirmed"):
            pass  # TronGrid unreachable: Tronscan's confirmed + SUCCESS flags are the fallback evidence
        else:
            return "unverified"
    elif ok is False:
        _insert_tx(t, "exception", "verify_failed", now=now)
        _notify(notify, "TRC20 待人工处理：链上校验不通过", _tx_text(t, "verify_failed", address))
        return "verify_failed"
    amount = int(t["value"])
    led = _db()
    with led._lock:
        rows = [dict(r) for r in led._db.execute(
            "SELECT * FROM trc20_orders WHERE amount_micro=? AND created_at>? ORDER BY created_at DESC",
            (amount, now - 2 * REUSE_BLOCK_MS)).fetchall()]
    hit = next((o for o in rows if o["status"] != "paid" and int(o["created_at"]) - EARLY_SLACK_MS <= int(t["ts"]) <= int(o["expires_at"])), None)
    if hit is None:
        late = next((o for o in rows if o["status"] != "paid" and int(t["ts"]) > int(o["expires_at"])), None)
        dup = next((o for o in rows if o["status"] == "paid"), None)
        reason = "late" if late else ("already_paid" if dup else "amount_mismatch")
        ref = (late or dup or {}).get("order_id")
        _insert_tx(t, "exception", reason, order_id=ref, user_id=(late or dup or {}).get("user_id"), now=now)
        _notify(notify, "TRC20 待人工处理：" + {"late": "订单超时后到账", "already_paid": "订单已入账后再次付款",
                                            "amount_mismatch": "金额对不上任何订单"}[reason], _tx_text(t, reason, address))
        return reason
    entry = _credit(tx, hit["user_id"], int(hit["points"]), operator=f"provider:{PROVIDER_ID}",
                    reason=f"USDT-TRC20 {fmt_order_amount(amount)}", ref=f"tx:{tx}")
    _insert_tx(t, "matched", None, order_id=hit["order_id"], user_id=hit["user_id"], points=int(hit["points"]),
               entry_id=int(entry["id"]), now=now)
    with led._lock:
        led._db.execute("UPDATE trc20_orders SET status='paid', tx_hash=?, entry_id=?, updated_at=? WHERE order_id=? AND status<>'paid'",
                        (tx, int(entry["id"]), now, hit["order_id"]))
    log.warning("TRC20 credited %s points to %s (order %s, tx %s)", hit["points"], hit["user_id"], hit["order_id"], tx)
    _notify(notify, f"TRC20 充值到账：{fmt_order_amount(amount)} USDT → {hit['points']} 积分",
            _tx_text(t, "matched", address, extra=f"订单 {hit['order_id']}\n用户 {hit['user_id']}\n积分 +{hit['points']}（流水 #{entry['id']}）"))
    return "matched"


def _tx_text(t: dict[str, Any], what: str, address: str, extra: str = "") -> str:
    when = datetime.fromtimestamp(int(t["ts"]) / 1000, _BJ).strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"结果：{what}", f"收款地址：{address}", f"金额：{fmt_usdt(int(t['value']))} {t.get('symbol') or ''}",
             f"合约：{t.get('contract')}" + ("（官方 USDT）" if t.get("contract") == USDT_CONTRACT else "（不是官方 USDT 合约）"),
             f"付款地址：{t.get('from')}", f"链上时间（北京）：{when}", f"交易哈希：{t['tx']}",
             f"Tronscan：https://tronscan.org/#/transaction/{t['tx']}"]
    if extra:
        lines.append(extra)
    if what != "matched":
        lines.append("未自动加积分。请在 积分管理 → USDT 充值 → 待人工处理 里归属或忽略。")
    return "\n".join(lines)


def _default_notify(subject: str, body: str) -> None:
    try:
        from app.alerts import AlertCenter, alerts_enabled

        if not alerts_enabled():
            return
        to = (os.getenv("AUU_TRC20_ALERT_TO") or "douriff3@gmail.com").strip()
        center = AlertCenter(to=to)
        try:
            center.raise_alert(f"trc20:{hashlib.sha256(body.encode()).hexdigest()[:16]}", "trc20", f"AUUTRADE {subject}", body,
                               bypass_limits=True)
        finally:
            center.close()
    except Exception:
        log.exception("TRC20 alert mail failed")


def _notify(notify: Optional[Callable[[str, str], None]], subject: str, body: str) -> None:
    (notify or _default_notify)(subject, body)


def poll_once(client: Optional[ChainClient] = None, *, now: Optional[int] = None,
              notify: Optional[Callable[[str, str], None]] = None) -> dict[str, Any]:
    now = _now() if now is None else now
    address = receive_address()
    if not address:
        return {"ok": False, "reason": "disabled"}
    client = client or ChainClient()
    expire_due(now)
    led = _db()
    with led._lock:
        oldest = led._db.execute("SELECT MIN(created_at) FROM trc20_orders WHERE created_at>?", (now - 2 * REUSE_BLOCK_MS,)).fetchone()[0]
    last_ok = int(_state_get("last_ok_ts") or 0)
    floor = now - 72 * 3600 * 1000
    start = min(x for x in (oldest or now, (last_ok - 3600_000) if last_ok else now - 6 * 3600_000))
    start = max(floor, start - EARLY_SLACK_MS)
    try:
        items, source, warn = client.incoming(address, start)
    except Exception as exc:
        _state_set("last_error", {"at": now, "error": str(exc)[:200]})
        log.warning("TRC20 poll failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}
    results: dict[str, int] = {}
    for t in sorted(items, key=lambda x: x["ts"]):
        try:
            r = process_transfer(t, client, now=now, notify=notify)
        except Exception:
            log.exception("TRC20 transfer %s", t.get("tx"))
            r = "error"
        results[r] = results.get(r, 0) + 1
    _state_set("last_ok_ts", str(now))
    _state_set("last_poll", {"at": now, "source": source, "warn": warn, "count": len(items), "results": results})
    return {"ok": True, "source": source, "count": len(items), "results": results}


# ---- admin -------------------------------------------------------------------------------------
def resolve(tx_hash: str, user_id: str, points: int, reason: str, admin_id: str, *, now: Optional[int] = None) -> dict[str, Any]:
    """Attribute a review-list transfer to a user (route checked admin + password + 2FA)."""
    now = _now() if now is None else now
    t = _known_tx(tx_hash)
    if t is None:
        raise PointsError("TRC20_UNKNOWN_TX")
    if t["status"] != "exception":
        raise PointsError("TRC20_NOT_PENDING")
    entry = _credit(tx_hash, user_id, points, operator=f"admin:{admin_id}", reason=reason, ref=f"tx:{tx_hash}")
    led = _db()
    with led._lock:
        led._db.execute("UPDATE trc20_txs SET status='resolved', user_id=?, points=?, entry_id=?, resolved_by=?, resolved_at=?, note=? "
                        "WHERE tx_hash=? AND status='exception'", (user_id, int(points), int(entry["id"]), admin_id, now, reason, tx_hash))
    return entry


def dismiss(tx_hash: str, reason: str, admin_id: str, *, now: Optional[int] = None) -> dict[str, Any]:
    now = _now() if now is None else now
    t = _known_tx(tx_hash)
    if t is None:
        raise PointsError("TRC20_UNKNOWN_TX")
    if t["status"] != "exception":
        raise PointsError("TRC20_NOT_PENDING")
    led = _db()
    with led._lock:
        led._db.execute("UPDATE trc20_txs SET status='ignored', resolved_by=?, resolved_at=?, note=? WHERE tx_hash=? AND status='exception'",
                        (admin_id, now, reason, tx_hash))
    return _known_tx(tx_hash) or {}


def tx_view(t: dict[str, Any]) -> dict[str, Any]:
    return {"txHash": t["tx_hash"], "blockTs": int(t["block_ts"]), "from": t.get("from_addr"), "contract": t["contract"],
            "official": t["contract"] == USDT_CONTRACT, "symbol": t.get("symbol"), "amount": fmt_usdt(int(t["amount_micro"])),
            "amountMicro": int(t["amount_micro"]), "status": t["status"], "reason": t.get("reason"), "orderId": t.get("order_id"),
            "userId": t.get("user_id"), "points": t.get("points"), "entryId": t.get("entry_id"), "resolvedBy": t.get("resolved_by"),
            "note": t.get("note"), "source": t.get("source"), "url": f"https://tronscan.org/#/transaction/{t['tx_hash']}"}


def reconcile() -> dict[str, Any]:
    """Official-USDT transfers seen on chain = credited (auto + manual) + pending review + dismissed."""
    led = _db()
    with led._lock:
        rows = led._db.execute("SELECT status, COUNT(*), COALESCE(SUM(amount_micro),0) FROM trc20_txs WHERE contract=? GROUP BY status",
                               (USDT_CONTRACT,)).fetchall()
        fake = led._db.execute("SELECT COUNT(*), COALESCE(SUM(amount_micro),0) FROM trc20_txs WHERE contract<>?", (USDT_CONTRACT,)).fetchone()
        pts = led._db.execute("SELECT COALESCE(SUM(amount),0), COUNT(*) FROM points_ledger WHERE provider=? AND kind='topup'",
                              (PROVIDER_ID,)).fetchone()
        tx_pts = led._db.execute("SELECT COALESCE(SUM(points),0) FROM trc20_txs WHERE status IN ('matched','resolved')").fetchone()[0]
    by = {r[0]: (int(r[1]), int(r[2])) for r in rows}
    g = lambda k: by.get(k, (0, 0))  # noqa: E731
    chain = sum(v[1] for v in by.values())
    credited = g("matched")[1] + g("resolved")[1]
    pending = g("exception")[1]
    ignored = g("ignored")[1]
    return {
        "chainTotal": fmt_usdt(chain), "chainCount": sum(v[0] for v in by.values()),
        "creditedTotal": fmt_usdt(credited), "creditedAuto": fmt_usdt(g("matched")[1]), "creditedManual": fmt_usdt(g("resolved")[1]),
        "pendingTotal": fmt_usdt(pending), "pendingCount": g("exception")[0], "ignoredTotal": fmt_usdt(ignored),
        "balanced": chain == credited + pending + ignored,
        "fakeContractCount": int(fake[0]),
        "pointsCredited": int(pts[0]), "pointsMatchLedger": int(pts[0]) == int(tx_pts),
        "lastPoll": json.loads(_state_get("last_poll") or "null"), "lastError": json.loads(_state_get("last_error") or "null"),
    }


def health() -> dict[str, Any]:
    return {"enabled": enabled(), "polling": poll_enabled(), "lastPoll": json.loads(_state_get("last_poll") or "null")
            if enabled() else None}


async def run_loop() -> None:
    import asyncio

    await asyncio.sleep(15)
    while True:
        try:
            if poll_enabled():
                await asyncio.to_thread(poll_once)
        except Exception:
            log.exception("trc20 loop")
        await asyncio.sleep(config()["pollSec"])


def main(argv: Optional[list[str]] = None) -> int:
    """python -m app.points.trc20 config | poll | reconcile | check-address <T...>"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "config"
    if cmd == "config":
        c = config()
        print(json.dumps({**c, "enabled": enabled(), "pollEnabled": poll_enabled()}, ensure_ascii=False, indent=1))
    elif cmd == "check-address":
        a = args[1] if len(args) > 1 else ""
        print(json.dumps({"address": a, "valid": valid_address(a)}))
    elif cmd == "poll":
        print(json.dumps(poll_once(), ensure_ascii=False, indent=1))
    elif cmd == "reconcile":
        print(json.dumps(reconcile(), ensure_ascii=False, indent=1))
    else:
        print(main.__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
