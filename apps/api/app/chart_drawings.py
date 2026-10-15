"""Chart drawings (KLineChart trendlines, fib, rectangles ...) stored per user and symbol.

Table ``chart_drawings(user_id, symbol, tf, json, updated_at)`` in its own ``chart_drawings.sqlite``
(isolated from every ledger / strategy / points DB; never read by trading code). ``tf`` is ``''``
for drawings shared by all timeframes (what the web app uses); the API also accepts a timeframe.

Validation is strict because the payload comes from the browser: tool names are whitelisted,
points are finite numbers, text is length-capped, and the whole document is size-capped.
Optimistic concurrency: PUT may carry ``baseUpdatedAt``; a mismatch returns the current document
(409) so a second device never silently overwrites newer drawings.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from app.data_paths import data_dir

LEDGER_NAME = "chart_drawings.sqlite"

ALLOWED_TOOLS = frozenset(
    {"segment", "rayLine", "horizontalStraightLine", "verticalStraightLine", "fibonacciLine", "auuRect", "parallelStraightLine", "auuText"}
)
ALLOWED_TF = frozenset({"", "1m", "5m", "15m", "1h", "4h", "1d"})
MAX_DRAWINGS = 200
MAX_POINTS = 5
MAX_TEXT = 200
MAX_STYLES_BYTES = 2048
MAX_DOC_BYTES = 60_000  # nginx client_max_body_size is 64k
MAX_KEYS_PER_USER = 300  # (symbol, tf) documents per user
PRICE_LIMIT = 1e12
TS_MIN, TS_MAX = 946_684_800_000, 4_102_444_800_000  # 2000-01-01 .. 2100-01-01 (ms)

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,20}$")
_UID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chart_drawings (
  user_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  tf TEXT NOT NULL DEFAULT '',
  json TEXT NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, symbol, tf)
);
CREATE INDEX IF NOT EXISTS chart_drawings_user ON chart_drawings (user_id);
"""


class DrawingsError(ValueError):
    """Invalid payload (``code`` is the API error code)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Conflict(Exception):
    def __init__(self, current: dict[str, Any]):
        super().__init__("conflict")
        self.current = current


def normalize_symbol(raw: Any) -> str:
    base = "".join(ch for ch in str(raw or "").split("/")[0].upper() if ch.isalnum())
    if not _SYMBOL_RE.match(base):
        raise DrawingsError("BAD_SYMBOL", "symbol format")
    return base


def normalize_tf(raw: Any) -> str:
    tf = str(raw or "").strip()
    if tf not in ALLOWED_TF:
        raise DrawingsError("BAD_TF", f"tf must be empty or one of {sorted(t for t in ALLOWED_TF if t)}")
    return tf


def _num(v: Any, lo: float, hi: float, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise DrawingsError("BAD_DRAWING", f"{what} must be a number")
    f = float(v)
    if not math.isfinite(f) or f < lo or f > hi:
        raise DrawingsError("BAD_DRAWING", f"{what} out of range")
    return f


def _plain(v: Any, depth: int = 0) -> bool:
    """JSON-plain value of bounded depth (styles are an opaque, size-capped object)."""
    if depth > 4:
        return False
    if v is None or isinstance(v, (bool, int, str)):
        return not isinstance(v, str) or len(v) <= 64
    if isinstance(v, float):
        return math.isfinite(v)
    if isinstance(v, list):
        return len(v) <= 16 and all(_plain(x, depth + 1) for x in v)
    if isinstance(v, dict):
        return len(v) <= 24 and all(isinstance(k, str) and len(k) <= 32 and _plain(x, depth + 1) for k, x in v.items())
    return False


def validate_drawings(items: Any) -> list[dict[str, Any]]:
    """Return the cleaned list or raise ``DrawingsError``."""
    if not isinstance(items, list):
        raise DrawingsError("BAD_DRAWINGS", "drawings must be a list")
    if len(items) > MAX_DRAWINGS:
        raise DrawingsError("TOO_MANY", f"at most {MAX_DRAWINGS} drawings per chart")
    out: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise DrawingsError("BAD_DRAWING", "drawing must be an object")
        name = raw.get("name")
        if name not in ALLOWED_TOOLS:
            raise DrawingsError("BAD_DRAWING", "unknown drawing tool")
        pts = raw.get("points")
        if not isinstance(pts, list) or not (1 <= len(pts) <= MAX_POINTS):
            raise DrawingsError("BAD_DRAWING", f"1..{MAX_POINTS} points required")
        clean_pts = []
        for p in pts:
            if not isinstance(p, dict):
                raise DrawingsError("BAD_DRAWING", "point must be an object")
            clean_pts.append(
                {
                    "timestamp": int(_num(p.get("timestamp"), TS_MIN, TS_MAX, "timestamp")),
                    "value": _num(p.get("value"), -PRICE_LIMIT, PRICE_LIMIT, "value"),
                }
            )
        d: dict[str, Any] = {"name": name, "points": clean_pts}
        ext = raw.get("extendData")
        if ext is not None:
            if not isinstance(ext, str):
                raise DrawingsError("BAD_DRAWING", "extendData must be text")
            ext = ext.replace("\x00", "")[:MAX_TEXT]
            if ext:
                d["extendData"] = ext
        styles = raw.get("styles")
        if styles is not None:
            if not isinstance(styles, dict) or not _plain(styles) or len(json.dumps(styles, separators=(",", ":"))) > MAX_STYLES_BYTES:
                raise DrawingsError("BAD_DRAWING", "styles invalid or too large")
            if styles:
                d["styles"] = styles
        out.append(d)
    if len(json.dumps(out, separators=(",", ":"))) > MAX_DOC_BYTES:
        raise DrawingsError("TOO_LARGE", "drawings document too large")
    return out


class DrawingsStore:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else data_dir() / LEDGER_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def get(self, user_id: str, symbol: str, tf: str = "") -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT json, updated_at FROM chart_drawings WHERE user_id=? AND symbol=? AND tf=?", (user_id, symbol, tf)
            ).fetchone()
        if row is None:
            return {"symbol": symbol, "tf": tf, "drawings": [], "updatedAt": 0}
        return {"symbol": symbol, "tf": tf, "drawings": json.loads(row["json"]), "updatedAt": int(row["updated_at"])}

    def put(
        self,
        user_id: str,
        symbol: str,
        tf: str,
        drawings: list[dict[str, Any]],
        *,
        base_updated_at: Optional[int] = None,
        now_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Replace the document. Empty list deletes it. Raises ``Conflict`` on a stale base."""
        if not _UID_RE.match(user_id or ""):
            raise DrawingsError("BAD_USER", "user id")
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._lock:
            cur = self.get(user_id, symbol, tf)
            if base_updated_at is not None and int(base_updated_at) != cur["updatedAt"]:
                raise Conflict(cur)
            if not drawings:
                self._db.execute("DELETE FROM chart_drawings WHERE user_id=? AND symbol=? AND tf=?", (user_id, symbol, tf))
                return {"symbol": symbol, "tf": tf, "drawings": [], "updatedAt": 0}
            if cur["updatedAt"] == 0:
                n = self._db.execute("SELECT COUNT(*) FROM chart_drawings WHERE user_id=?", (user_id,)).fetchone()[0]
                if n >= MAX_KEYS_PER_USER:
                    raise DrawingsError("TOO_MANY_CHARTS", f"at most {MAX_KEYS_PER_USER} charts with drawings per account")
            stamp = max(now, cur["updatedAt"] + 1)  # strictly increasing per document (base check relies on it)
            self._db.execute(
                "INSERT INTO chart_drawings (user_id, symbol, tf, json, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(user_id, symbol, tf) DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at",
                (user_id, symbol, tf, json.dumps(drawings, separators=(",", ":")), stamp),
            )
        return {"symbol": symbol, "tf": tf, "drawings": drawings, "updatedAt": stamp}

    def list_charts(self, user_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT symbol, tf, updated_at, json FROM chart_drawings WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
                (user_id, MAX_KEYS_PER_USER),
            ).fetchall()
        return [{"symbol": r["symbol"], "tf": r["tf"], "updatedAt": int(r["updated_at"]), "count": len(json.loads(r["json"]))} for r in rows]


class RateLimiter:
    """Per-user sliding window (in-memory, one process) for writes."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._lock = threading.Lock()
        self._hits: dict[str, deque] = {}

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        t = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and t - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(t)
            if len(self._hits) > 20_000:
                for k in [k for k, v in self._hits.items() if not v or t - v[-1] > self.window]:
                    self._hits.pop(k, None)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


WRITE_LIMIT = RateLimiter(limit=30, window=60.0)
READ_LIMIT = RateLimiter(limit=240, window=60.0)

_STORE: Optional[DrawingsStore] = None
_STORE_LOCK = threading.Lock()


def get_store() -> DrawingsStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = DrawingsStore()
        return _STORE


def reset_store(store: Optional[DrawingsStore] = None) -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = store
