"""Refresh loop + read models for the mainstream market-data layer."""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

from app.marketdata.mainstream.config import (
    FUNDING_STEP_MS,
    INTRADAY,
    TF_MS,
    TIMEFRAMES,
    MainstreamConfig,
    load_config,
)
from app.marketdata.mainstream.fetcher import FetchError, Fetcher, make_exchange
from app.marketdata.mainstream.store import MarketStore

log = logging.getLogger("auu.mainstream")

DAY_MS = 86_400_000
# A series is stale when its newest point is older than this (minutes).
STALE_MIN = {"1h": 180, "4h": 360, "1d": 2 * 24 * 60 + 60, "funding": 12 * 60 + 30}


class MainstreamService:
    def __init__(
        self,
        cfg: Optional[MainstreamConfig] = None,
        store: Optional[MarketStore] = None,
        *,
        exchange_factory: Optional[Callable[[str, MainstreamConfig], Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.cfg = cfg or load_config()
        self._store = store
        self._factory = exchange_factory or make_exchange
        self._sleep = sleep
        self.now_ms = now_ms
        self._clients: dict[str, Fetcher] = {}
        self._lock = threading.Lock()
        self._funding_backfilled: set[str] = set()
        self.active: Optional[str] = None
        self.blocked: dict[str, str] = {}
        self.last_refresh_ms: Optional[int] = None
        self.last_error: Optional[str] = None
        self.funding_live: dict[str, dict] = {}
        self.gaps: dict[str, list] = {}
        self._stop_flag = False
        # On-demand intraday fetches use their own clients + lock so a chart
        # request never waits behind (or races) the background refresh pass.
        self._od_clients: dict[str, Fetcher] = {}
        self._od_lock = threading.Lock()
        self._od_tail_at: dict[tuple, int] = {}
        self._od_fill_at: dict[tuple, int] = {}
        # Market list: store-built rows cached briefly; optional batched 24h tickers (one request
        # for every coin, only while someone is looking, refreshed in the background).
        self._mk_lock = threading.Lock()
        self._mk_cache: Optional[dict] = None
        self._tk: dict[str, Any] = {"at": 0, "ex": None, "data": {}, "error": None, "try_at": 0, "busy": False}

    @property
    def store(self) -> MarketStore:
        if self._store is None:
            self._store = MarketStore()
        return self._store

    def fetcher(self, name: str) -> Fetcher:
        if name not in self._clients:
            self._clients[name] = Fetcher(
                name, self._factory(name, self.cfg), self.cfg, sleep=self._sleep, now_ms=self.now_ms
            )
        return self._clients[name]

    # ---- refresh -------------------------------------------------------
    def candidates(self) -> list[str]:
        order = list(self.cfg.exchanges)
        if self.active in order:
            order.remove(self.active)
            order.insert(0, self.active)
        return order

    def refresh_once(self) -> dict:
        """One incremental pass over every symbol/timeframe + funding.

        Tries exchanges in order; an exchange that is geo-blocked or fails
        the whole pass is skipped and the next one is used.
        """
        with self._lock:
            errors: dict[str, str] = {}
            for name in self.candidates():
                try:
                    summary = self._refresh_exchange(name)
                except FetchError as exc:
                    errors[name] = str(exc)
                    if exc.blocked:
                        self.blocked[name] = str(exc)[:160]
                    log.warning("mainstream %s unavailable: %s", name, exc)
                    continue
                if self.active and self.active != name:
                    log.warning("mainstream exchange switched %s -> %s", self.active, name)
                self.active = name
                self.blocked.pop(name, None)
                self.last_refresh_ms = self.now_ms()
                self.last_error = summary.get("error")
                return {"exchange": name, **summary, "skipped": errors}
            self.last_error = "; ".join(f"{k}: {v}" for k, v in errors.items()) or "no exchange configured"
            return {"exchange": None, "error": self.last_error, "skipped": errors}

    def _refresh_exchange(self, name: str) -> dict:
        f = self.fetcher(name)
        st = self.store
        now = self.now_ms()
        added: dict[str, int] = {}
        failures: list[str] = []
        ok_any = False
        display = set(self.cfg.symbols)
        for base in self.cfg.all_symbols():
            spot, perp = self.cfg.spot(base), self.cfg.perp(base)
            strategy_only = base not in display
            # strategy-only coins: 1d for the strategy, 1h for the risk caps' hourly marks
            for tf in (("1d", "1h") if strategy_only else TIMEFRAMES):
                key = f"{base}:{tf}"
                step = TF_MS[tf]
                try:
                    _, last, _ = st.candle_bounds(name, base, tf)
                    since = last if last is not None else now - self.cfg.backfill(tf) * DAY_MS
                    rows = f.ohlcv(spot, tf, since)
                    n = st.upsert_candles(name, base, tf, rows)
                    n += self._repair_gaps(f, name, base, spot, tf, step)
                    added[key] = n
                    st.log_fetch(name, base, tf, attempt_ms=now, ok=True, error=None, rows=n)
                    ok_any = True
                except FetchError as exc:
                    st.log_fetch(name, base, tf, attempt_ms=now, ok=False, error=str(exc), rows=0)
                    failures.append(f"{key} {exc}")
                    if exc.blocked or not ok_any and len(failures) >= 2:
                        raise
            try:
                _, last, _ = st.funding_bounds(name, base)
                fdays = max(self.cfg.funding_backfill_days, self.cfg.strategy_funding_days if base in self.cfg.strategy_symbols else 0)
                since = last + 1 if last is not None else now - fdays * DAY_MS
                n = st.upsert_funding(name, base, f.funding_history(perp, since))
                n += self._backfill_funding_older(f, name, base, perp, now)
                added[f"{base}:funding"] = n
                st.log_fetch(name, base, "funding", attempt_ms=now, ok=True, error=None, rows=n)
                ok_any = True
            except FetchError as exc:
                st.log_fetch(name, base, "funding", attempt_ms=now, ok=False, error=str(exc), rows=0)
                failures.append(f"{base}:funding {exc}")
            if strategy_only:
                continue
            try:
                self.funding_live[f"{name}:{base}"] = f.funding_now(perp)
            except FetchError:
                pass
        if not ok_any:
            raise FetchError("; ".join(failures)[:300] or "no data")
        return {"added": added, "error": "; ".join(failures)[:300] or None}

    def _backfill_funding_older(self, f: Fetcher, name: str, base: str, perp: str, now: int) -> int:
        """Strategy coins: extend funding history backwards once per process (display coins
        were first backfilled with the short default). Venues with short history (OKX ~3
        months) return what they have; trying once per process avoids refetch loops."""
        if base not in self.cfg.strategy_symbols:
            return 0
        key = f"{name}:{base}"
        if key in self._funding_backfilled:
            return 0
        self._funding_backfilled.add(key)
        first, _, _ = self.store.funding_bounds(name, base)
        target = now - self.cfg.strategy_funding_days * DAY_MS
        if first is None or first <= target + 2 * FUNDING_STEP_MS:
            return 0
        rows = [r for r in f.funding_history(perp, target) if r[0] < first]
        return self.store.upsert_funding(name, base, rows)

    def _repair_gaps(self, f: Fetcher, name: str, base: str, spot: str, tf: str, step: int) -> int:
        gaps = self.store.find_gaps(name, base, tf, step)
        n = 0
        for a, b in gaps[: self.cfg.max_gap_repairs]:
            n += self.store.upsert_candles(name, base, tf, f.ohlcv(spot, tf, a, until=b))
        remaining = self.store.find_gaps(name, base, tf, step)
        self.gaps[f"{name}:{base}:{tf}"] = remaining
        return n

    async def run_loop(self) -> None:
        self._stop_flag = False
        while not self._stop_flag:
            try:
                await asyncio.to_thread(self.refresh_once)
            except Exception as exc:  # never kill the API
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.exception("mainstream refresh failed")
            for _ in range(self.cfg.refresh_sec):
                if self._stop_flag:
                    return
                await asyncio.sleep(1)

    def stop(self) -> None:
        self._stop_flag = True

    # ---- on-demand intraday (1m/5m/15m) ---------------------------------
    def _od_fetcher(self, name: str) -> Fetcher:
        if name not in self._od_clients:
            cfg = self.cfg
            self._od_clients[name] = Fetcher(
                name, self._factory(name, cfg), cfg, sleep=self._sleep, now_ms=self.now_ms
            )
        return self._od_clients[name]

    def intraday_window(self, tf: str) -> tuple[int, int]:
        """(oldest kept bar ts, current bar ts) for an intraday timeframe."""
        step = TF_MS[tf]
        cur = self.now_ms() // step * step
        return cur - self.cfg.intraday_days * DAY_MS, cur

    def ensure_intraday(self, ex: str, base: str, tf: str, limit: int, before: Optional[int] = None) -> dict:
        """Make sure the store covers the requested intraday window, fetching only what's missing.

        Keeps at most ``intraday_days`` of history per series (older rows are pruned),
        so 1m/5m/15m never grow without bound. The forming bar is re-fetched at most
        every ``intraday_tail_sec``; a hole the venue can't fill is retried at most once a minute.
        """
        step = TF_MS[tf]
        keep_from, cur = self.intraday_window(tf)
        end = cur if before is None else min(cur, (int(before) - 1) // step * step)
        meta: dict[str, Any] = {"retentionDays": self.cfg.intraday_days, "oldestAllowed": keep_from, "fetched": 0}
        if end < keep_from:
            meta["limited"] = True
            return meta
        start = max(keep_from, end - (max(1, limit) - 1) * step)
        if start == keep_from and before is not None:
            meta["limited"] = True
        key = (ex, base, tf)
        now = self.now_ms()
        with self._od_lock:
            lo, hi, n = self.store.range_stats(ex, base, tf, start, end)
            expected = (end - start) // step + 1
            since: Optional[int] = None
            if n < expected:
                contiguous_tail = lo is not None and lo <= start and n == (hi - lo) // step + 1
                fill_key = (key, start, end)
                if contiguous_tail:
                    since = hi  # only newer bars missing: resume at the last stored bar
                elif now - self._od_fill_at.get(fill_key, 0) >= 60_000:
                    self._od_fill_at[fill_key] = now
                    since = start
            if since is None and before is None and now - self._od_tail_at.get(key, 0) >= self.cfg.intraday_tail_sec * 1000:
                since = hi if hi is not None else start  # refresh the forming bar
            if since is None:
                return meta
            if before is None:
                self._od_tail_at[key] = now
            try:
                rows = self._od_fetcher(ex).ohlcv(self.cfg.spot(base), tf, since, until=end)
                meta["fetched"] = self.store.upsert_candles(ex, base, tf, rows)
            except FetchError as exc:
                meta["fetchError"] = str(exc)[:200]
                log.warning("mainstream on-demand %s %s %s failed: %s", ex, base, tf, exc)
            self.store.prune_candles(ex, base, tf, keep_from)
        return meta

    def chart_candles(self, base: str, tf: str, *, limit: int, before: Optional[int] = None) -> dict:
        ex = self.exchange_for_read()
        meta: dict[str, Any] = {}
        if ex and tf in INTRADAY:
            meta = self.ensure_intraday(ex, base, tf, limit, before)
        until = None if before is None else int(before) - 1
        if ex and tf == "4h" and base not in self.cfg.symbols:
            rows = self._agg_4h_from_1h(ex, base, limit=limit, before=before)
            return {"exchange": ex, "symbol": base, "pair": self.cfg.spot(base), "tf": tf, "candles": rows,
                    "derived": "4h from stored 1h", "oldestAllowed": rows[0]["ts"] if rows else None,
                    **({"limited": True} if before is not None and len(rows) < limit else {})}
        rows = self.store.candles(ex, base, tf, until=until, limit=limit) if ex else []
        if tf in INTRADAY and "oldestAllowed" in meta:
            rows = [r for r in rows if r["ts"] >= meta["oldestAllowed"]]
        elif ex and tf in TIMEFRAMES:
            lo, _, _ = self.store.candle_bounds(ex, base, tf)
            meta["oldestAllowed"] = lo
            if before is not None and (not rows or (lo is not None and rows[0]["ts"] <= lo)):
                meta["limited"] = True
        return {"exchange": ex, "symbol": base, "pair": self.cfg.spot(base), "tf": tf, "candles": rows, **meta}

    # ---- market list (all display + strategy coins) -----------------------
    MARKETS_CACHE_MS = 15_000

    def markets(self, holdings: Optional[dict[str, float]] = None) -> dict:
        """One batched read for the market list. Rows come from the local store (1h/1d candles and
        funding that the refresh loop already keeps for every strategy coin); when available, a
        cached batched 24h ticker overlays last price / 24h change / 24h quote volume."""
        now = self.now_ms()
        ex = self.exchange_for_read()
        with self._mk_lock:
            c = self._mk_cache
            if c is None or c["ex"] != ex or now - c["at"] >= self.MARKETS_CACHE_MS:
                c = {"ex": ex, "at": now, "items": [self._market_row(ex, b) for b in self.cfg.all_symbols()]}
                self._mk_cache = c
        tk = self._tickers(ex)
        fresh = tk["ex"] == ex and tk["data"] and now - int(tk["at"]) <= 3 * self._ticker_ttl_ms()
        hold = holdings or {}
        items = []
        for row in c["items"]:
            it = dict(row)
            t = tk["data"].get(it["symbol"]) if fresh else None
            if t and t.get("last"):
                it["price"], it["priceTs"], it["priceSource"] = t["last"], t.get("ts") or tk["at"], "ticker"
                if t.get("change24h") is not None:
                    it["change24h"] = t["change24h"]
                if t.get("quoteVolume") is not None:
                    it["quoteVolume24h"] = t["quoteVolume"]
            w = hold.get(it["symbol"])
            it["held"] = bool(w)
            it["weight"] = w if w else None
            items.append(it)
        return {"exchange": ex, "quote": self.cfg.quote, "items": items, "asOf": now,
                "tickers": {"enabled": self._tickers_enabled(), "source": "ticker" if fresh else "store",
                            "at": tk["at"] or None, "error": tk["error"]}}

    def _market_row(self, ex: Optional[str], base: str) -> dict:
        it: dict[str, Any] = {"symbol": base, "pair": self.cfg.spot(base), "perp": self.cfg.perp(base),
                              "display": base in self.cfg.symbols, "strategy": base in self.cfg.strategy_symbols}
        if not ex:
            return it
        h = self.store.candles(ex, base, "1h", limit=25)
        d = self.store.candles(ex, base, "1d", limit=31)
        last = h[-1] if h else (d[-1] if d else None)
        it["price"] = last["close"] if last else None
        it["priceTs"] = last["ts"] if last else None
        it["priceSource"] = "store"
        it["change24h"] = h[-1]["close"] / h[0]["close"] - 1 if len(h) >= 25 and h[0]["close"] else None
        it["quoteVolume24h"] = sum(float(x["volume"] or 0) * float(x["close"] or 0) for x in h[-24:]) if len(h) >= 24 else None
        it["change7d"] = d[-1]["close"] / d[-8]["close"] - 1 if len(d) >= 8 and d[-8]["close"] else None
        it["change30d"] = d[-1]["close"] / d[0]["close"] - 1 if len(d) >= 31 and d[0]["close"] else None
        it["spark30"] = [x["close"] for x in d]
        fr = self.store.funding(ex, base, limit=21)
        live = self.funding_live.get(f"{ex}:{base}") or {}
        it["fundingLast"] = fr[-1]["rate"] if fr else None
        it["funding7dAvg"] = (sum(x["rate"] for x in fr) / len(fr)) if fr else None
        it["fundingNow"] = live.get("rate")
        it["nextFundingMs"] = live.get("nextFundingMs")
        ref = it["fundingNow"] if it["fundingNow"] is not None else it["fundingLast"]
        it["funding"] = ref
        it["fundingAnnualized"] = None if ref is None else ref * (DAY_MS / FUNDING_STEP_MS) * 365
        return it

    @staticmethod
    def _tickers_enabled() -> bool:
        return os.getenv("AUU_MAINSTREAM_TICKERS", "on").strip().lower() not in {"0", "false", "off", "no"}

    @staticmethod
    def _ticker_ttl_ms() -> int:
        try:
            return max(15, min(600, int(os.getenv("AUU_MAINSTREAM_TICKER_SEC", "30")))) * 1000
        except ValueError:
            return 30_000

    TICKER_BACKOFF_MS = 300_000

    def _tickers(self, ex: Optional[str]) -> dict:
        """Cached batched tickers; never blocks the request. Starts at most one background
        refresh when the cache is older than the TTL (and not inside the failure back-off)."""
        tk = self._tk
        if not ex or not self._tickers_enabled():
            return tk
        now = self.now_ms()
        with self._mk_lock:
            due = (tk["ex"] != ex or now - int(tk["at"]) >= self._ticker_ttl_ms()) and now >= int(tk["try_at"]) and not tk["busy"]
            if due:
                tk["busy"] = True
        if due:
            threading.Thread(target=self._refresh_tickers, args=(ex,), name="mainstream-tickers", daemon=True).start()
        return tk

    def _refresh_tickers(self, ex: str) -> None:
        tk = self._tk
        bases = self.cfg.all_symbols()
        try:
            with self._od_lock:
                raw = self._od_fetcher(ex).call("fetch_tickers", [self.cfg.spot(b) for b in bases]) or {}
            data = {}
            for b in bases:
                t = raw.get(self.cfg.spot(b)) or {}
                last = t.get("last") or t.get("close")
                if not last:
                    continue
                pct = t.get("percentage")
                if pct is None and t.get("open"):
                    pct = (float(last) / float(t["open"]) - 1) * 100
                data[b] = {"last": float(last), "change24h": None if pct is None else float(pct) / 100,
                           "quoteVolume": None if t.get("quoteVolume") is None else float(t["quoteVolume"]),
                           "ts": int(t["timestamp"]) if t.get("timestamp") else None}
            with self._mk_lock:
                tk.update(at=self.now_ms(), ex=ex, data=data, error=None, try_at=0, busy=False)
        except Exception as exc:  # keep serving the store rows; back off before the next try
            with self._mk_lock:
                tk.update(error=f"{type(exc).__name__}: {str(exc)[:120]}", try_at=self.now_ms() + self.TICKER_BACKOFF_MS, busy=False)
            log.warning("mainstream tickers %s failed: %s", ex, exc)

    def _agg_4h_from_1h(self, ex: str, base: str, *, limit: int, before: Optional[int]) -> list[dict]:
        """Strategy-only coins store 1h (not 4h): build UTC-aligned 4h bars from it."""
        step = TF_MS["4h"]
        until = None if before is None else int(before) - 1
        rows = self.store.candles(ex, base, "1h", until=until, limit=limit * 4 + 4)
        out: list[dict] = []
        for r in rows:
            b = int(r["ts"]) // step * step
            if out and out[-1]["ts"] == b:
                o = out[-1]
                o["high"], o["low"] = max(o["high"], r["high"]), min(o["low"], r["low"])
                o["close"], o["volume"] = r["close"], (o["volume"] or 0) + (r["volume"] or 0)
            else:
                out.append({"ts": b, "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"]})
        if rows and out and int(rows[0]["ts"]) != out[0]["ts"]:
            out = out[1:]  # first bucket only partially covered
        return out[-limit:]

    # ---- read models ---------------------------------------------------
    def exchange_for_read(self) -> Optional[str]:
        if self.active:
            return self.active
        # Before the first refresh of this process: whichever exchange has data.
        for name in self.cfg.exchanges:
            if any(self.store.candle_bounds(name, base, "1h")[2] for base in self.cfg.symbols):
                return name
        return None

    def freshness(self) -> dict:
        now = self.now_ms()
        ex = self.exchange_for_read()
        series: dict[str, dict] = {}
        stale: list[str] = []
        for base in self.cfg.symbols:
            for kind in (*TIMEFRAMES, "funding"):
                if ex is None:
                    last, count = None, 0
                elif kind == "funding":
                    _, last, count = self.store.funding_bounds(ex, base)
                else:
                    _, last, count = self.store.candle_bounds(ex, base, kind)
                # Candle ts is the open time; a bar is "current" until open+step.
                ref = last + TF_MS[kind] if (last is not None and kind in TF_MS) else last
                age = None if ref is None else max(0.0, (now - ref) / 60_000)
                is_stale = age is None or age > STALE_MIN[kind]
                key = f"{base}:{kind}"
                series[key] = {"lastTs": last, "count": count, "ageMin": None if age is None else round(age, 1), "stale": is_stale}
                if is_stale:
                    stale.append(key)
        gaps = {k: len(v) for k, v in self.gaps.items() if v and (ex is None or k.startswith(f"{ex}:"))}
        return {
            "enabled": self.cfg.enabled,
            "exchange": ex,
            "exchanges": list(self.cfg.exchanges),
            "blocked": dict(self.blocked),
            "symbols": list(self.cfg.symbols),
            "lastRefreshMs": self.last_refresh_ms,
            "lastError": self.last_error,
            "stale": bool(stale),
            "staleSeries": stale,
            "gaps": gaps,
            "series": series,
        }

    def overview(self) -> dict:
        ex = self.exchange_for_read()
        items = []
        for base in self.cfg.symbols:
            item: dict[str, Any] = {"symbol": base, "pair": self.cfg.spot(base), "perp": self.cfg.perp(base)}
            if ex:
                h = self.store.candles(ex, base, "1h", limit=25)
                d = self.store.candles(ex, base, "1d", limit=31)
                last = h[-1] if h else (d[-1] if d else None)
                item["price"] = last["close"] if last else None
                item["priceTs"] = last["ts"] if last else None
                if len(h) >= 25 and h[0]["close"]:
                    item["change24h"] = h[-1]["close"] / h[0]["close"] - 1
                else:
                    item["change24h"] = None
                if len(d) >= 31 and d[0]["close"]:
                    item["change30d"] = d[-1]["close"] / d[0]["close"] - 1
                else:
                    item["change30d"] = None
                item["spark1d"] = [c["close"] for c in d]
                fr = self.store.funding(ex, base, limit=21)
                item["fundingLast"] = fr[-1]["rate"] if fr else None
                item["fundingLastTs"] = fr[-1]["ts"] if fr else None
                item["funding7dAvg"] = (sum(x["rate"] for x in fr) / len(fr)) if fr else None
                live = self.funding_live.get(f"{ex}:{base}") or {}
                item["fundingNow"] = live.get("rate")
                item["nextFundingMs"] = live.get("nextFundingMs")
                ref = item["fundingNow"] if item["fundingNow"] is not None else item["fundingLast"]
                item["fundingAnnualized"] = None if ref is None else ref * (DAY_MS / FUNDING_STEP_MS) * 365
            items.append(item)
        return {"exchange": ex, "quote": self.cfg.quote, "items": items, "freshness": self.freshness()}


_service: Optional[MainstreamService] = None


def get_service() -> MainstreamService:
    global _service
    if _service is None:
        _service = MainstreamService()
    return _service


def reset_service(svc: Optional[MainstreamService] = None) -> None:
    global _service
    _service = svc
