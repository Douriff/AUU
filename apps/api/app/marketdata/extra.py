"""Coins outside the strategy pool, opened from the 大盘 (top-100) board. Read-only market data.

Only coins that a supported venue currently lists in its top-100 by 24h quote volume are accepted
(plus coins a paper account still holds), so no arbitrary symbol ever reaches an exchange.
Bounded on purpose:
- allowlist: the venue's cached top-100 board (refreshed at most every ``ALLOW_REFRESH_S``),
  entries kept for 24h after they were last seen;
- candles: fetched on demand, one window per (venue, coin, tf), ``MAX_BARS`` bars, short TTL,
  LRU of ``MAX_KEYS`` windows, dropped after 24h without use;
- outbound: a global budget of ``AUU_EXTRA_FETCH_PER_MIN`` OHLCV requests per minute, and a
  per-window 30s back-off after a failure. One fetch per window at a time.
The strategy pool, strategy runner, bookkeeping and risk code never read from here.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

DATA_VENUES = ("binance", "okx")  # venues with a ccxt client in the mainstream service
BOARD_VENUES = ("binance", "okx", "bybit", "coinbase")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}$")
ALLOW_REFRESH_S = 600
ALLOW_KEEP_MS = 24 * 3600 * 1000
MAX_KEYS = 32
MAX_BARS = {"binance": 500, "okx": 300}
KEY_IDLE_MS = 24 * 3600 * 1000
FAIL_BACKOFF_MS = 30_000
TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
TTL_MS = {"1m": 15_000, "5m": 20_000, "15m": 30_000, "1h": 60_000, "4h": 120_000, "1d": 300_000}


def _budget() -> int:
    try:
        return max(1, min(600, int(os.getenv("AUU_EXTRA_FETCH_PER_MIN", "30"))))
    except ValueError:
        return 30


def clean_symbol(raw: str) -> Optional[str]:
    base = (raw or "").split("/")[0].split("-")[0].strip().upper()
    if base.endswith("USDT") and len(base) > 4 and "/" not in (raw or "") and "-" not in (raw or ""):
        base = base[:-4]
    return base if SYMBOL_RE.match(base) else None


class ExtraMarkets:
    def __init__(self, svc: Any, *, board_fn: Optional[Callable[[str], dict]] = None,
                 peek_fn: Optional[Callable[[str], Optional[tuple[float, dict]]]] = None,
                 held_fn: Optional[Callable[[], set[str]]] = None):
        from app.marketdata import tickers

        self.svc = svc
        self._board_fn = board_fn or tickers.load_board
        self._peek_fn = peek_fn or tickers.peek_board
        self.held_fn = held_fn or (lambda: set())
        self._lock = threading.Lock()
        self._allow: dict[str, dict[str, int]] = {v: {} for v in BOARD_VENUES}
        self._rows: dict[str, dict[str, dict]] = {v: {} for v in BOARD_VENUES}
        self._allow_at: dict[str, float] = {}
        self._allow_try: dict[str, float] = {}
        self._win: "OrderedDict[tuple, dict]" = OrderedDict()
        self._starts: deque[int] = deque()
        self._net = threading.Lock()

    # ---- allowlist -------------------------------------------------------
    def _note(self, venue: str, board: dict) -> None:
        if board.get("status") != "ok":
            return
        now = self.svc.now_ms()
        rows = {str(r.get("base") or "").upper(): r for r in (board.get("rows") or [])[:100]}
        with self._lock:
            allow = self._allow[venue]
            for b in rows:
                if SYMBOL_RE.match(b):
                    allow[b] = now
            for b in [b for b, t in allow.items() if now - t > ALLOW_KEEP_MS]:
                allow.pop(b, None)
            self._rows[venue] = rows
            self._allow_at[venue] = time.monotonic()

    def _sync(self, venue: str) -> None:
        peek = self._peek_fn(venue)
        last = self._allow_at.get(venue, 0.0)
        if peek is not None and time.monotonic() - peek[0] > last:
            self._note(venue, peek[1])  # a fresher board already cached (e.g. the 大盘 page loaded it)
        if time.monotonic() - self._allow_at.get(venue, -1e9) < ALLOW_REFRESH_S:
            return
        if time.monotonic() - self._allow_try.get(venue, -1e9) < 60:
            return
        self._allow_try[venue] = time.monotonic()
        try:
            self._note(venue, self._board_fn(venue))
        except Exception as exc:  # board down: keep what we have
            log.warning("extra allowlist %s refresh failed: %s", venue, exc)

    def listed(self, venue: str, base: str) -> bool:
        if venue not in BOARD_VENUES:
            return False
        self._sync(venue)
        with self._lock:
            return base in self._allow[venue]

    def board_row(self, venue: str, base: str) -> Optional[dict]:
        with self._lock:
            return self._rows.get(venue, {}).get(base)

    def pool(self) -> list[str]:
        return list(self.svc.cfg.all_symbols())

    def resolve(self, base: str, hint: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """(data venue, reason-if-none) for an allowed coin; hint = venue tab the user came from."""
        if base is None:
            return None, "BAD_SYMBOL"
        reader = self.svc.exchange_for_read()
        order = [v for v in (hint, reader, *DATA_VENUES) if v in DATA_VENUES]
        order = list(dict.fromkeys(order))
        held = base in self.held_fn()
        for v in order:
            if base in self.pool() or self.listed(v, base) or held:
                return v, None
        if hint in BOARD_VENUES and self.listed(hint, base):
            return None, "VENUE_NO_DATA"  # only on Bybit/Coinbase: board row only, no chart
        return None, "NOT_LISTED"

    def allowed_for_paper(self, base: str) -> bool:
        if base in self.pool() or base in self.held_fn():
            return True
        return any(self.listed(v, base) for v in DATA_VENUES)

    def paper_symbols(self) -> list[str]:
        for v in DATA_VENUES:
            self._sync(v)
        out = dict.fromkeys(self.pool())
        with self._lock:
            for v in DATA_VENUES:
                out.update(dict.fromkeys(self._allow[v]))
        out.update(dict.fromkeys(sorted(self.held_fn())))
        return list(out)

    # ---- candles ---------------------------------------------------------
    def _budget_ok(self, now: int) -> bool:
        while self._starts and now - self._starts[0] >= 60_000:
            self._starts.popleft()
        if len(self._starts) >= _budget():
            return False
        self._starts.append(now)
        return True

    def _fetch(self, venue: str, base: str, tf: str, since: Optional[int], limit: int) -> list[dict]:
        client = self.svc._od_fetcher(venue).client
        with self._net:
            raw = client.fetch_ohlcv(f"{base}/USDT", tf, since=since, limit=limit) or []
        out = []
        for r in raw:
            try:
                out.append({"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                            "close": float(r[4]), "volume": float(r[5] or 0.0)})
            except (TypeError, ValueError, IndexError):
                continue
        return out

    def _prune(self, now: int) -> None:
        for k in [k for k, w in self._win.items() if now - w["used"] > KEY_IDLE_MS and not w["busy"]]:
            self._win.pop(k, None)
        while len(self._win) > MAX_KEYS:
            k = next((k for k, w in self._win.items() if not w["busy"]), None)
            if k is None:
                break
            self._win.pop(k, None)

    def window(self, venue: str, base: str, tf: str) -> dict:
        """Latest window (<= MAX_BARS bars) for one coin/tf, fetched on demand and cached."""
        if tf not in TF_MS or venue not in DATA_VENUES:
            raise KeyError(tf)
        key = (venue, base, tf)
        now = self.svc.now_ms()
        with self._lock:
            w = self._win.get(key)
            if w is None:
                w = self._win[key] = {"at": 0, "rows": [], "error": None, "try_at": 0, "busy": False, "used": now}
            self._win.move_to_end(key)
            w["used"] = now
            due = now - w["at"] >= TTL_MS[tf] and now >= w["try_at"] and not w["busy"]
            go = due and self._budget_ok(now)
            if go:
                w["busy"] = True
            self._prune(now)
        if go:
            try:
                n = MAX_BARS.get(venue, 300)
                step = TF_MS[tf]
                rows = self._fetch(venue, base, tf, (now // step - (n - 1)) * step, n)
                with self._lock:
                    w.update(at=self.svc.now_ms(), rows=rows[-MAX_BARS.get(venue, 300):], error=None, try_at=0, busy=False)
            except Exception as exc:
                with self._lock:
                    w.update(error=f"{type(exc).__name__}: {str(exc)[:120]}", try_at=self.svc.now_ms() + FAIL_BACKOFF_MS, busy=False)
                log.warning("extra candles %s/%s/%s failed: %s", venue, base, tf, exc)
        with self._lock:
            return {"rows": list(w["rows"]), "at": w["at"], "error": w["error"]}

    def candles(self, venue: str, base: str, tf: str, *, limit: int, before: Optional[int] = None) -> dict:
        w = self.window(venue, base, tf)
        rows = w["rows"]
        if before is not None:
            rows = [r for r in rows if r["ts"] < int(before)]
        rows = rows[-int(limit):]
        out: dict[str, Any] = {"exchange": venue, "symbol": base, "pair": f"{base}/USDT", "tf": tf, "candles": rows,
                               "extra": True, "oldestAllowed": w["rows"][0]["ts"] if w["rows"] else None}
        if before is not None:
            out["limited"] = True  # non-pool coins: latest window only, no deep history
        if not w["rows"] and w["error"]:
            out["fetchError"] = "行情暂不可用，稍后再试"
        return out

    def last_price(self, base: str) -> Optional[tuple[float, int]]:
        venue, _ = self.resolve(base)
        if venue is None:
            return None
        rows = self.window(venue, base, "1m")["rows"]
        if not rows:
            return None
        r = rows[-1]
        return float(r["close"]), min(self.svc.now_ms(), int(r["ts"]) + 60_000)

    def bars_since(self, base: str, since: int) -> list[dict]:
        """1m bars from ``since`` for paper TP/SL/limit matching (never returns a gapped series)."""
        venue, _ = self.resolve(base)
        if venue is None:
            return []
        since = int(since) // 60_000 * 60_000
        rows = self.window(venue, base, "1m")["rows"]
        if rows and rows[0]["ts"] <= since:
            return [r for r in rows if r["ts"] >= since]
        # older than the cached window: page forward (budgeted, not cached); give up rather than gap
        out: list[dict] = []
        cursor = since
        for _ in range(3):
            if not self._budget_ok(self.svc.now_ms()):
                return []
            try:
                page = self._fetch(venue, base, "1m", cursor, MAX_BARS.get(venue, 300))
            except Exception:
                return []
            page = [r for r in page if r["ts"] >= cursor]
            if not page:
                break
            out.extend(page)
            cursor = page[-1]["ts"] + 60_000
            if rows and cursor >= rows[0]["ts"]:
                out.extend(r for r in rows if r["ts"] >= cursor)
                return out
        return out  # contiguous from `since`; the matcher continues from its last bar next pass

    def stats(self) -> dict:
        with self._lock:
            return {"windows": len(self._win), "bars": sum(len(w["rows"]) for w in self._win.values()),
                    "allow": {v: len(a) for v, a in self._allow.items()}, "fetchesLastMin": len(self._starts)}


_extra: Optional[ExtraMarkets] = None
_elock = threading.Lock()


def get_extra() -> ExtraMarkets:
    global _extra
    from app.marketdata.mainstream import get_service

    svc = get_service()
    with _elock:
        if _extra is None or _extra.svc is not svc:
            from app.paper.mainstream_account import held_symbols

            _extra = ExtraMarkets(svc, held_fn=held_symbols)
        return _extra


def reset_extra() -> None:
    global _extra
    with _elock:
        _extra = None
