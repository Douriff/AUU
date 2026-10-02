"""Refresh loop + read models for the mainstream market-data layer."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Optional

from app.marketdata.mainstream.config import FUNDING_STEP_MS, TF_MS, TIMEFRAMES, MainstreamConfig, load_config
from app.marketdata.mainstream.fetcher import FetchError, Fetcher, make_exchange
from app.marketdata.mainstream.store import MarketStore

log = logging.getLogger("auu.mainstream")

DAY_MS = 86_400_000
# A series is stale when its newest point is older than this (minutes).
STALE_MIN = {"1h": 180, "1d": 2 * 24 * 60 + 60, "funding": 12 * 60 + 30}


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
        self.active: Optional[str] = None
        self.blocked: dict[str, str] = {}
        self.last_refresh_ms: Optional[int] = None
        self.last_error: Optional[str] = None
        self.funding_live: dict[str, dict] = {}
        self.gaps: dict[str, list] = {}
        self._stop_flag = False

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
        for base in self.cfg.symbols:
            spot, perp = self.cfg.spot(base), self.cfg.perp(base)
            for tf in TIMEFRAMES:
                key = f"{base}:{tf}"
                step = TF_MS[tf]
                try:
                    _, last, _ = st.candle_bounds(name, base, tf)
                    since = last if last is not None else now - self.cfg.backfill_days[tf] * DAY_MS
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
                since = last + 1 if last is not None else now - self.cfg.funding_backfill_days * DAY_MS
                n = st.upsert_funding(name, base, f.funding_history(perp, since))
                added[f"{base}:funding"] = n
                st.log_fetch(name, base, "funding", attempt_ms=now, ok=True, error=None, rows=n)
                ok_any = True
            except FetchError as exc:
                st.log_fetch(name, base, "funding", attempt_ms=now, ok=False, error=str(exc), rows=0)
                failures.append(f"{base}:funding {exc}")
            try:
                self.funding_live[f"{name}:{base}"] = f.funding_now(perp)
            except FetchError:
                pass
        if not ok_any:
            raise FetchError("; ".join(failures)[:300] or "no data")
        return {"added": added, "error": "; ".join(failures)[:300] or None}

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
