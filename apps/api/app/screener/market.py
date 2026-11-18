"""Public-REST book for the spot screener.

No API key and no order endpoint. A failed or empty fetch never replaces the last
valid snapshot; the snapshot is then stale and cannot count as a current hit.
In-flight requests for the same URL are merged. Daily klines are limited in
concurrency and honor Retry-After. The universe is every Binance spot USDT
symbol whose status is TRADING — not a hidden top-N, and not "price near 1".
"""
from __future__ import annotations

import json
import os
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.screener.logic import (
    DAY_MS,
    DEFAULT_QUERY,
    LIVE_ENABLED,
    Reading,
    SchemaError,
    accept_query,
    bars_are_fresh,
    chart_path,
    collect_metrics,
    eval_query,
    export_csv,
    history_limit,
    indicator_reading,
    metric_id,
    needs_klines,
    preview_value,
    split_bars,
    verdict_of,
)

import re

_BASE = re.compile(r"^[A-Z0-9]{2,20}$")
_STATUS_OK = frozenset({"TRADING"})
_UA = "AUUTRADE-screener/0 (read-only public spot market data; no api key)"

TICKER_FRESH_MS = 45_000
KLINE_FRESH_MS = 15 * 60 * 1000
INFO_FRESH_MS = 30 * 60 * 1000
_RETRY_CAP_S = 60.0


def fixture_enabled() -> bool:
    """Off unless explicitly opted in. Never the production default."""
    return os.getenv("AUU_SCREENER_FIXTURE", "").strip().lower() in {"1", "true", "on", "yes"}


def kline_budget() -> int:
    try:
        return max(1, min(40, int(os.getenv("AUU_SCREENER_KLINE_BUDGET", "8"))))
    except ValueError:
        return 8


class HttpResult:
    def __init__(self, *, ok: bool, status: int, headers: dict[str, str], body: Any, error: Optional[str]) -> None:
        self.ok = ok
        self.status = status
        self.headers = {str(k).lower(): str(v) for k, v in headers.items()}
        self.body = body
        self.error = error

    @classmethod
    def success(cls, body: Any, status: int = 200) -> "HttpResult":
        return cls(ok=True, status=status, headers={}, body=body, error=None)

    @classmethod
    def failure(cls, status: int, error: str, body: Any = None, headers: Optional[dict[str, str]] = None) -> "HttpResult":
        return cls(ok=False, status=status, headers=headers or {}, body=body, error=error)


def parse_retry_after(headers: dict[str, str]) -> float:
    """Seconds to stay quiet. Missing or unreadable values fall back to 5s, capped at 60s."""
    raw = (headers.get("retry-after") or "").strip()
    if not raw:
        return 5.0
    try:
        return min(_RETRY_CAP_S, max(0.0, float(raw)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
        if when is None:
            return 5.0
        return min(_RETRY_CAP_S, max(0.0, when.timestamp() - time.time()))
    except (TypeError, ValueError, OverflowError, OSError):
        return 5.0


class _Limiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.until = 0.0

    def allow(self, now: float) -> bool:
        with self._lock:
            return now >= self.until

    def penalize(self, seconds: float, now: float) -> None:
        with self._lock:
            self.until = max(self.until, now + min(_RETRY_CAP_S, max(0.0, seconds)))


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def _has_spot(raw: dict[str, Any]) -> bool:
    if raw.get("isSpotTradingAllowed") is False:
        return False
    if raw.get("contractType"):
        return False
    perms = raw.get("permissions")
    sets = raw.get("permissionSets")
    if perms is None and sets is None:
        return True
    found = False
    if isinstance(perms, list) and any(str(item).upper() == "SPOT" for item in perms):
        found = True
    if isinstance(sets, list):
        for group in sets:
            if isinstance(group, list) and any(str(item).upper() == "SPOT" for item in group):
                found = True
    return found


def classify_symbol(raw: Any) -> tuple[Optional[dict[str, str]], str]:
    """Return ``(listing, reason)``. Non-spot and non-TRADING symbols are rejected, not screened."""
    if not isinstance(raw, dict):
        return None, "bad_row"
    base = str(raw.get("baseAsset") or "")
    quote = str(raw.get("quoteAsset") or "")
    symbol = str(raw.get("symbol") or "")
    status = str(raw.get("status") or "")
    if quote != "USDT" or not _BASE.fullmatch(base) or symbol != f"{base}USDT":
        return None, "not_usdt_spot"
    if status not in _STATUS_OK:
        return None, "status"
    if not _has_spot(raw):
        return None, "non_spot"
    return {"base": base, "symbol": symbol}, "ok"


def parse_ticker_row(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    symbol = str(raw.get("symbol") or "")
    if not symbol.endswith("USDT"):
        return None
    base = symbol[: -len("USDT")]
    if not _BASE.fullmatch(base):
        return None
    source_time = raw.get("closeTime")
    try:
        source_ms = int(source_time) if source_time is not None else None
    except (TypeError, ValueError):
        source_ms = None
    return {
        "base": base,
        "symbol": symbol,
        "price": _num(raw.get("lastPrice")),
        "quoteVolume": _num(raw.get("quoteVolume")),
        "sourceTime": source_ms,
    }


def parse_kline_row(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) < 7:
        return None
    try:
        open_time = int(raw[0])
        close_time = int(raw[6])
    except (TypeError, ValueError):
        return None
    try:
        close = float(raw[4])
    except (TypeError, ValueError):
        close = float("nan")
    return {"openTime": open_time, "closeTime": close_time, "close": close}


class UrllibTransport:
    """Keyless GET. Host is whatever ``BINANCE_REST_URL`` already configures for the rest of AUU."""

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout

    def get(self, url: str) -> HttpResult:
        if not (url.startswith("https://") or url.startswith("http://")):
            return HttpResult.failure(0, "bad_url")
        req = Request(url, headers={"Accept": "application/json", "User-Agent": _UA}, method="GET")
        try:
            with urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — configured public REST host
                raw = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                status = getattr(resp, "status", 200) or 200
        except HTTPError as exc:
            headers = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                body = None
            return HttpResult.failure(exc.code, f"http_{exc.code}", body, headers)
        except (URLError, TimeoutError, OSError):
            return HttpResult.failure(0, "network")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return HttpResult.failure(status, "parse")
        return HttpResult(ok=status == 200, status=status, headers=headers, body=body, error=None if status == 200 else f"http_{status}")


def _binance_root() -> str:
    from app.marketdata.cex import venue_urls

    return venue_urls()["binance"].rstrip("/")


def read_mainstream_daily(base: str, now_ms: int) -> Optional[dict[str, Any]]:
    """Read pool daily bars already stored by AUU. Read-only: no refresh and no ticker overlay."""
    try:
        from app.marketdata.mainstream import get_service

        svc = get_service()
        if base not in svc.cfg.all_symbols():
            return None
        ex = svc.exchange_for_read()
        refreshed = int(getattr(svc, "last_refresh_ms", 0) or 0)
        if not ex or refreshed <= 0 or now_ms - refreshed > KLINE_FRESH_MS:
            return None
        rows = svc.store.candles(ex, base, "1d", limit=1000)
    except Exception:
        return None
    if not rows:
        return None
    bars = []
    for row in rows:
        try:
            open_time = int(row["ts"])
            close = float(row["close"])
        except (TypeError, ValueError, KeyError):
            close = float("nan")
            open_time = int(row.get("ts") or 0)
        bars.append({"openTime": open_time, "closeTime": open_time + DAY_MS - 1, "close": close})
    return {"bars": bars, "fetchedAt": refreshed, "source": f"auu-mainstream-store:{ex}"}


class ScreenerBook:
    def __init__(
        self,
        *,
        transport: Any,
        source_kind: str,
        store_reader: Optional[Callable[[str, int], Optional[dict[str, Any]]]] = None,
        now_ms: Optional[Callable[[], int]] = None,
        concurrency: int = 4,
        root: Optional[Callable[[], str]] = None,
    ) -> None:
        self.transport = transport
        self.source_kind = source_kind
        self.store_reader = store_reader
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.concurrency = max(1, concurrency)
        self._root = root or _binance_root
        self._lock = threading.RLock()
        self._flights: dict[str, dict[str, Any]] = {}
        self._limiter = _Limiter()
        self._net = threading.Semaphore(self.concurrency)
        self.info: dict[str, Any] = {"fetchedAt": 0, "symbols": [], "error": None, "rejected": 0}
        self.ticker: dict[str, Any] = {"fetchedAt": 0, "sourceTime": None, "rows": {}, "error": None}
        self.klines: dict[str, dict[str, Any]] = {}

    def meta(self) -> dict[str, Any]:
        return {
            "liveEnabled": LIVE_ENABLED,
            "mode": "read_only",
            "orders": False,
            "orderMode": "paper",
            "fixture": self.source_kind == "fixture",
            "sourceKind": self.source_kind,
            "universe": {
                "id": "binance-spot-usdt-trading",
                "venue": "binance",
                "quote": "USDT",
                "statusWhitelist": ["TRADING"],
                "spotOnly": True,
                "fullMarket": False,
                "priceNearOneExcluded": False,
                "topN": None,
            },
            "candlePolicy": {
                "indicatorTf": "1d",
                "indicatorsUse": "closed_daily_only",
                "latestPrice": "ticker_last",
                "quoteVolume": "ticker_quote_volume_24h",
                "formingExcluded": True,
                "flatRsi": 50,
                "closedStaleMs": 49 * 60 * 60 * 1000,
            },
            "fields": [
                {"field": "price"},
                {"field": "quoteVolume24h"},
                {"field": "ema", "period": 10, "tf": "1d"},
                {"field": "ema", "period": 20, "tf": "1d"},
                {"field": "ema", "period": 200, "tf": "1d"},
                {"field": "rsi", "period": 14, "tf": "1d"},
            ],
            "cmp": ["gt", "gte", "lt", "lte"],
            "unsupported": ["crossed_above", "crossed_below", "ema50"],
            "limits": {"maxLeaves": 8, "maxDepth": 3, "maxNodes": 16, "maxAbs": 1e15, "maxBodyBytes": 8000},
            "example": DEFAULT_QUERY,
        }

    def screen(self, payload: Any, *, budget: Optional[int] = None) -> dict[str, Any]:
        query = accept_query(payload)
        metrics = collect_metrics(query)
        now = self.now_ms()
        self._ensure_info(now)
        self._ensure_ticker(now)
        with self._lock:
            symbols = list(self.info["symbols"])
            info_state = self._info_state(now)
            ticker_state = self._ticker_state(now)
            ticker_rows = dict(self.ticker["rows"])
            snap = {
                "tickerFetchedAt": self.ticker["fetchedAt"] or None,
                "tickerSourceTime": self.ticker["sourceTime"],
                "tickerError": self.ticker["error"],
                "infoFetchedAt": self.info["fetchedAt"] or None,
                "infoError": self.info["error"],
                "rejected": self.info["rejected"],
            }
        k_metrics = [item for item in metrics if item["field"] in {"ema", "rsi"}]
        limit = history_limit(k_metrics)
        allow = budget if budget is not None else kline_budget()
        need: list[str] = []
        if k_metrics and limit:
            for item in symbols:
                readings = self._readings(item, metrics, now, ticker_state, ticker_rows)
                due = self._slot_due(item["base"], item["symbol"], now)
                if needs_klines(query, _plan_state(readings, due), _value(readings)):
                    need.append(item["symbol"])
            self._fetch_needed(need, limit, allow, now)
        rows = [self._row(item, query, metrics, now, ticker_state, ticker_rows, info_state) for item in symbols]
        rows.sort(key=lambda row: row["base"])
        counts = {"passes": 0, "fails": 0, "unknown": 0}
        for row in rows:
            counts[{"pass": "passes", "fail": "fails", "unknown": "unknown"}[row["verdict"]]] += 1
        ready = 0
        if need:
            with self._lock:
                for symbol in need:
                    slot = self.klines.get(symbol)
                    if slot and _slot_fresh(slot, now):
                        ready += 1
        ticker_ok = ticker_state == "ok"
        info_ok = info_state == "ok"
        # A stale or missing input on a listed coin is not a current book, even after the fetch
        # attempt. Short history and non-finite prices are known answers, so they do not.
        held_back = any(code in {"stale", "failed", "pending", "rate_limited", "missing"} for row in rows for code in row["reasons"])
        if not symbols and not info_ok:
            status = "unavailable"
        elif not ticker_ok or not info_ok or ready < len(need) or held_back:
            status = "degraded"
        else:
            status = "ok"
        return {
            "liveEnabled": LIVE_ENABLED,
            "mode": "read_only",
            "orders": False,
            "orderMode": "paper",
            "fixture": self.source_kind == "fixture",
            "status": status,
            "universe": {
                "id": "binance-spot-usdt-trading",
                "listed": len(symbols),
                "rejected": snap["rejected"],
                "fullMarket": False,
                "priceNearOneExcluded": False,
                "topN": None,
                "stale": not info_ok,
            },
            "candlePolicy": {
                "indicatorTf": "1d",
                "indicatorsUse": "closed_daily_only",
                "latestPrice": "ticker_last",
                "formingExcluded": True,
            },
            "asOf": {
                "serverNow": now,
                "sourceKind": self.source_kind,
                "venue": "binance",
                "tickerFetchedAt": snap["tickerFetchedAt"],
                "tickerSourceTime": snap["tickerSourceTime"],
                "tickerState": ticker_state,
                "tickerError": snap["tickerError"],
                "infoFetchedAt": snap["infoFetchedAt"],
                "infoState": info_state,
                "infoError": snap["infoError"],
            },
            "coverage": {"needed": len(need), "ready": ready, "pending": max(0, len(need) - ready)},
            "counts": counts,
            "rows": rows,
        }

    def csv_text(self, payload: Any, *, budget: Optional[int] = None) -> str:
        data = self.screen(payload, budget=budget)
        metrics = collect_metrics(accept_query(payload))
        columns = ["symbol", "verdict"]
        for metric in metrics:
            ident = metric_id(metric)
            columns.extend((ident, ident + "State"))
        columns.extend(["closedBars", "klineSource", "reasons"])
        table = []
        for row in data["rows"]:
            record: dict[str, Any] = {
                "symbol": row["symbol"],
                "verdict": row["verdict"],
                "closedBars": "" if row["closedBars"] is None else row["closedBars"],
                "klineSource": row.get("klineSource") or "",
                "reasons": "|".join(row["reasons"]),
            }
            for metric in metrics:
                ident = metric_id(metric)
                reading = row["values"].get(ident) or {}
                number = reading.get("value") if reading.get("usable") else reading.get("display")
                record[ident] = "" if number is None else json.dumps(number)
                record[ident + "State"] = reading.get("state") or ""
            table.append(record)
        return export_csv(columns, table)

    # ---- snapshots -------------------------------------------------------
    def _info_state(self, now: int) -> str:
        err = self.info["error"]
        fresh = self.info["fetchedAt"] and now - int(self.info["fetchedAt"]) <= INFO_FRESH_MS and not err
        if fresh:
            return "ok"
        if self.info["symbols"]:
            return "stale"
        return "failed" if err else "missing"

    def _ticker_state(self, now: int) -> str:
        err = self.ticker["error"]
        fresh = self.ticker["fetchedAt"] and now - int(self.ticker["fetchedAt"]) <= TICKER_FRESH_MS and not err
        if fresh and self.ticker["rows"]:
            return "ok"
        if self.ticker["rows"]:
            return "stale"
        return "failed" if err else "missing"

    def _ensure_info(self, now: int) -> None:
        with self._lock:
            if self._info_state(now) == "ok":
                return
        if not self._limiter.allow(time.monotonic()):
            with self._lock:
                if self.info["symbols"]:
                    self.info["error"] = self.info["error"] or "rate_limited"
            return
        self._single("info", self._load_info)

    def _ensure_ticker(self, now: int) -> None:
        with self._lock:
            if self._ticker_state(now) == "ok":
                return
        if not self._limiter.allow(time.monotonic()):
            with self._lock:
                if self.ticker["rows"]:
                    self.ticker["error"] = self.ticker["error"] or "rate_limited"
            return
        self._single("ticker", self._load_ticker)

    def _load_info(self) -> None:
        result = self.transport.get(self._root() + "/api/v3/exchangeInfo")
        with self._lock:
            self._apply_info(result)

    def _apply_info(self, result: HttpResult) -> None:
        if result.status in {429, 418}:
            self._limiter.penalize(parse_retry_after(result.headers), time.monotonic())
            self.info["error"] = "rate_limited"
            return
        symbols = result.body.get("symbols") if result.ok and isinstance(result.body, dict) else None
        if not isinstance(symbols, list) or not symbols:
            self.info["error"] = result.error or "empty"
            return
        accepted = []
        rejected = 0
        seen: set[str] = set()
        for raw in symbols:
            listing, _reason = classify_symbol(raw)
            if listing is None:
                rejected += 1
                continue
            if listing["symbol"] in seen:
                continue
            seen.add(listing["symbol"])
            accepted.append(listing)
        self.info["symbols"] = accepted
        self.info["rejected"] = rejected
        self.info["fetchedAt"] = self.now_ms()
        self.info["error"] = None

    def _load_ticker(self) -> None:
        result = self.transport.get(self._root() + "/api/v3/ticker/24hr")
        with self._lock:
            self._apply_ticker(result)

    def _apply_ticker(self, result: HttpResult) -> None:
        if result.status in {429, 418}:
            self._limiter.penalize(parse_retry_after(result.headers), time.monotonic())
            self.ticker["error"] = "rate_limited"
            return
        rows = result.body if result.ok and isinstance(result.body, list) else None
        if not rows:
            self.ticker["error"] = result.error or "empty"
            return
        parsed: dict[str, dict[str, Any]] = {}
        times = []
        for raw in rows:
            item = parse_ticker_row(raw)
            if item is None:
                continue
            parsed[item["symbol"]] = item
            if item["sourceTime"] is not None:
                times.append(item["sourceTime"])
        if not parsed:
            self.ticker["error"] = "empty"
            return
        self.ticker["rows"] = parsed
        self.ticker["sourceTime"] = min(times) if times else None
        self.ticker["fetchedAt"] = self.now_ms()
        self.ticker["error"] = None

    def _fetch_needed(self, symbols: list[str], limit: int, budget: int, now: int) -> None:
        batch = [symbol for symbol in symbols if not self._fresh_kline(symbol, now)][: max(0, budget)]
        if not batch:
            return
        if not self._limiter.allow(time.monotonic()):
            self._mark_limited(batch, now)
            return
        queue = list(batch)
        qlock = threading.Lock()

        def worker() -> None:
            while self._limiter.allow(time.monotonic()):
                with qlock:
                    if not queue:
                        return
                    symbol = queue.pop(0)
                self._fetch_one_kline(symbol, limit)
            with qlock:
                rest = list(queue)
                queue.clear()
            self._mark_limited(rest, now)

        workers = [threading.Thread(target=worker, name="screener-kline", daemon=True) for _ in range(min(self.concurrency, len(batch)))]
        for thread in workers:
            thread.start()
        for thread in workers:
            thread.join(20)

    def _mark_limited(self, symbols: list[str], now: int) -> None:
        for symbol in symbols:
            if not self._fresh_kline(symbol, now):
                self._note_kline(symbol, error="rate_limited")

    def _slot_due(self, base: str, symbol: str, now: int) -> bool:
        """True when another daily fetch could replace this slot. A recent fetch is not due,
        even if the last closed bar is too old to count as a hit."""
        slot = self._kline_slot(base, symbol, now)
        if not slot or slot.get("error") or not slot.get("bars"):
            return True
        return now - int(slot.get("fetchedAt") or 0) > KLINE_FRESH_MS

    def _fresh_kline(self, symbol: str, now: int) -> bool:
        slot = self._kline_slot(symbol[:-4], symbol, now)
        return bool(slot and _slot_fresh(slot, now))

    def _fetch_one_kline(self, symbol: str, limit: int) -> None:
        def go() -> None:
            if not self._limiter.allow(time.monotonic()):
                self._note_kline(symbol, error="rate_limited")
                return
            url = f"{self._root()}/api/v3/klines?symbol={symbol}&interval=1d&limit={int(limit)}"
            with self._net:
                if not self._limiter.allow(time.monotonic()):
                    self._note_kline(symbol, error="rate_limited")
                    return
                result = self.transport.get(url)
            self._apply_kline(symbol, result)

        self._single(f"k:{symbol}:{limit}", go)

    def _apply_kline(self, symbol: str, result: HttpResult) -> None:
        with self._lock:
            if result.status in {429, 418}:
                self._limiter.penalize(parse_retry_after(result.headers), time.monotonic())
                self._note_kline_locked(symbol, error="rate_limited")
                return
            rows = result.body if result.ok and isinstance(result.body, list) else None
            if not rows:
                self._note_kline_locked(symbol, error=result.error or "empty")
                return
            bars = [item for item in (parse_kline_row(row) for row in rows) if item]
            if not bars:
                self._note_kline_locked(symbol, error="empty")
                return
            self.klines[symbol] = {
                "fetchedAt": self.now_ms(),
                "bars": bars,
                "error": None,
                "source": self.source_kind,
            }

    def _note_kline(self, symbol: str, *, error: str) -> None:
        with self._lock:
            self._note_kline_locked(symbol, error=error)

    def _note_kline_locked(self, symbol: str, *, error: str) -> None:
        slot = self.klines.get(symbol) or {"fetchedAt": 0, "bars": [], "error": None, "source": self.source_kind}
        slot["error"] = error
        self.klines[symbol] = slot

    def _kline_slot(self, base: str, symbol: str, now: int) -> Optional[dict[str, Any]]:
        store = None
        if self.store_reader is not None:
            try:
                store = self.store_reader(base, now)
            except Exception:
                store = None
        if store and store.get("bars"):
            slot = {
                "fetchedAt": int(store.get("fetchedAt") or 0),
                "bars": list(store["bars"]),
                "error": None,
                "source": store.get("source") or "auu-mainstream-store",
            }
            if _slot_fresh(slot, now):
                return slot
        with self._lock:
            cached = self.klines.get(symbol)
            if cached:
                cached = {**cached, "bars": list(cached.get("bars") or [])}
        if cached and _slot_fresh(cached, now):
            return cached
        if store and store.get("bars"):
            return {
                "fetchedAt": int(store.get("fetchedAt") or 0),
                "bars": list(store["bars"]),
                "error": None,
                "source": store.get("source") or "auu-mainstream-store",
            }
        return cached

    def _readings(
        self,
        item: dict[str, str],
        metrics: list[dict[str, Any]],
        now: int,
        ticker_state: str,
        ticker_rows: dict[str, dict[str, Any]],
    ) -> dict[str, Reading]:
        quote = ticker_rows.get(item["symbol"])
        out: dict[str, Reading] = {}
        k_metrics = [metric for metric in metrics if metric["field"] in {"ema", "rsi"}]
        slot = self._kline_slot(item["base"], item["symbol"], now) if k_metrics else None
        closed: list[dict[str, Any]] = []
        fresh = False
        if slot and slot.get("bars"):
            closed, _forming = split_bars(slot["bars"], now)
            fresh = _slot_fresh(slot, now)
            closes = [bar["close"] for bar in closed]
        else:
            closes = []
        for metric in metrics:
            ident = metric_id(metric)
            if metric["field"] == "price":
                out[ident] = _quote_reading(None if quote is None else quote.get("price"), ticker_state, positive=True)
            elif metric["field"] == "quoteVolume24h":
                out[ident] = _quote_reading(None if quote is None else quote.get("quoteVolume"), ticker_state, positive=False)
            else:
                out[ident] = _kline_reading(metric, closes, fresh=fresh, slot=slot)
        return out

    def _row(
        self,
        item: dict[str, str],
        query: dict[str, Any],
        metrics: list[dict[str, Any]],
        now: int,
        ticker_state: str,
        ticker_rows: dict[str, dict[str, Any]],
        info_state: str,
    ) -> dict[str, Any]:
        readings = self._readings(item, metrics, now, ticker_state, ticker_rows)
        tri = eval_query(query, lambda metric: preview_value(readings, metric))
        reasons: list[str] = []
        for metric in metrics:
            state = readings[metric_id(metric)].state
            if state != "ok" and state not in reasons:
                reasons.append(state)
        if info_state != "ok":
            if "universe_stale" not in reasons:
                reasons.append("universe_stale")
            if tri == "true":
                tri = "unknown"
        slot = self._kline_slot(item["base"], item["symbol"], now)
        closed: list[dict[str, Any]] = []
        forming = None
        if slot and slot.get("bars"):
            closed, forming = split_bars(slot["bars"], now)
        quote = ticker_rows.get(item["symbol"]) or {}
        return {
            "base": item["base"],
            "symbol": item["symbol"],
            "chartPath": chart_path(item["base"]),
            "verdict": verdict_of(tri),
            "eval": tri,
            "values": {metric_id(metric): readings[metric_id(metric)].as_json() for metric in metrics},
            "closedBars": len(closed) if slot and slot.get("bars") else None,
            "lastClosedOpenTime": closed[-1]["openTime"] if closed else None,
            "lastClosedCloseTime": closed[-1]["closeTime"] if closed else None,
            "formingOpenTime": forming["openTime"] if forming else None,
            "formingExcluded": forming is not None,
            "klineSource": None if not slot else slot.get("source"),
            "klineFetchedAt": None if not slot or not slot.get("fetchedAt") else slot.get("fetchedAt"),
            "priceSourceTime": quote.get("sourceTime"),
            "reasons": reasons,
        }

    def _single(self, key: str, fn: Callable[[], Any]) -> Any:
        with self._lock:
            slot = self._flights.get(key)
            if slot is None:
                slot = {"event": threading.Event(), "result": None, "error": None}
                self._flights[key] = slot
                owner = True
            else:
                owner = False
        if not owner:
            slot["event"].wait(30)
            if slot["error"] is not None:
                raise slot["error"]
            return slot["result"]
        try:
            slot["result"] = fn()
            return slot["result"]
        except Exception as exc:
            slot["error"] = exc
            raise
        finally:
            slot["event"].set()
            with self._lock:
                if self._flights.get(key) is slot:
                    self._flights.pop(key, None)


def _state(readings: dict[str, Reading]) -> Callable[[dict[str, Any]], str]:
    def state_of(metric: dict[str, Any]) -> str:
        reading = readings.get(metric_id(metric))
        return reading.state if reading else "missing"

    return state_of


def _plan_state(readings: dict[str, Reading], due: bool) -> Callable[[dict[str, Any]], str]:
    """A due slot's unusable indicator is planned as pending so it can be fetched again.

    A fetch that just happened stays stale: unknown, and not fetched again until the TTL.
    """
    def state_of(metric: dict[str, Any]) -> str:
        reading = readings.get(metric_id(metric))
        state = reading.state if reading else "missing"
        if due and metric.get("field") in {"ema", "rsi"} and state != "ok":
            return "pending"
        return state

    return state_of


def _value(readings: dict[str, Reading]) -> Callable[[dict[str, Any]], Optional[float]]:
    return lambda metric: preview_value(readings, metric)


def _slot_fresh(slot: dict[str, Any], now: int) -> bool:
    if slot.get("error") or not slot.get("bars"):
        return False
    closed, _forming = split_bars(slot["bars"], now)
    return bars_are_fresh(closed, int(slot.get("fetchedAt") or 0), now, fresh_ms=KLINE_FRESH_MS)


def _quote_reading(number: Optional[float], ticker_state: str, *, positive: bool) -> Reading:
    if number is None:
        if ticker_state == "stale":
            return Reading("stale")
        if ticker_state == "failed":
            return Reading("failed")
        return Reading("missing")
    if not (number == number) or number in {float("inf"), float("-inf")} or (number <= 0 if positive else number < 0):
        return Reading("nan")
    if ticker_state != "ok":
        return Reading("stale", display=number)
    return Reading("ok", value=number)


def _kline_reading(metric: dict[str, Any], closes: list[Any], *, fresh: bool, slot: Optional[dict[str, Any]]) -> Reading:
    """Missing bars stay unknown (pending / failed / rate limited). They are never zero."""
    if slot and slot.get("bars"):
        return indicator_reading(metric, closes, fresh=fresh and not slot.get("error"))
    if slot and slot.get("error") == "rate_limited":
        return Reading("rate_limited")
    if slot and slot.get("error"):
        return Reading("failed")
    return Reading("pending")


class FixtureTransport:
    """In-memory public-REST stand-in. Used only when a book is constructed with it."""

    def get(self, url: str) -> HttpResult:
        now = int(time.time() * 1000)
        if url.endswith("/api/v3/exchangeInfo"):
            return HttpResult.success({"symbols": fixture_info()})
        if url.endswith("/api/v3/ticker/24hr"):
            return HttpResult.success(fixture_tickers(now))
        if "/api/v3/klines?" in url:
            symbol = url.split("symbol=", 1)[1].split("&", 1)[0]
            return HttpResult.success(fixture_klines(symbol, now))
        return HttpResult.failure(404, "not_found")


def fixture_info() -> list[dict[str, Any]]:
    def spot(base: str) -> dict[str, Any]:
        return {
            "symbol": base + "USDT",
            "status": "TRADING",
            "baseAsset": base,
            "quoteAsset": "USDT",
            "isSpotTradingAllowed": True,
            "permissions": ["SPOT"],
        }

    return [
        spot("BTC"),
        spot("ETH"),
        spot("SOL"),
        spot("NEW"),
        spot("USDC"),
        spot("THIN"),
        spot("NAN"),
        spot("OLD"),
        spot("BTCUP"),
        {"symbol": "FUTUSDT", "status": "TRADING", "baseAsset": "FUT", "quoteAsset": "USDT", "contractType": "PERPETUAL", "permissions": ["TRD_GRP"]},
        {"symbol": "HALTUSDT", "status": "HALT", "baseAsset": "HALT", "quoteAsset": "USDT", "isSpotTradingAllowed": True, "permissions": ["SPOT"]},
        {"symbol": "MRGNUSDT", "status": "TRADING", "baseAsset": "MRGN", "quoteAsset": "USDT", "isSpotTradingAllowed": False, "permissions": ["MARGIN"]},
    ]


_FIXTURE = {
    "BTCUSDT": {"n": 240, "start": 100.0, "step": 1.0, "volume": 2_000_000_000.0, "forming": 1.0},
    "ETHUSDT": {"n": 240, "start": 50.0, "step": 0.4, "volume": 800_000_000.0, "forming": None},
    "SOLUSDT": {"n": 240, "start": 300.0, "step": -0.5, "volume": 300_000_000.0, "forming": None},
    "NEWUSDT": {"n": 12, "start": 1.0, "step": 0.1, "volume": 50_000_000.0, "forming": None},
    "USDCUSDT": {"n": 40, "start": 0.9997, "step": 0.0, "volume": 2_000_000_000.0, "forming": None},
    "THINUSDT": {"n": 80, "start": 2.0, "step": 0.01, "volume": 1_000.0, "forming": None},
    "NANUSDT": {"n": 30, "start": 5.0, "step": 0.2, "volume": 50_000_000.0, "forming": None, "nan": True},
    "OLDUSDT": {"n": 30, "start": 8.0, "step": 0.1, "volume": 50_000_000.0, "forming": None, "shift": 10},
    "BTCUPUSDT": {"n": 40, "start": 2.0, "step": 0.05, "volume": 1_000.0, "forming": None},
}


def _fixture_closes(spec: dict[str, Any]) -> list[float]:
    count = int(spec["n"])
    return [float(spec["start"]) + float(spec["step"]) * index for index in range(count)]


def fixture_tickers(now: int) -> list[dict[str, Any]]:
    rows = []
    for symbol, spec in _FIXTURE.items():
        closes = _fixture_closes(spec)
        price = closes[-1]
        rows.append({
            "symbol": symbol,
            "lastPrice": price,
            "quoteVolume": spec["volume"],
            "closeTime": now,
        })
    rows.append({"symbol": "FUTUSDT", "lastPrice": "10", "quoteVolume": "99999999", "closeTime": now})
    rows.append({"symbol": "HALTUSDT", "lastPrice": "10", "quoteVolume": "99999999", "closeTime": now})
    return rows


def fixture_klines(symbol: str, now: int) -> list[list[Any]]:
    spec = _FIXTURE.get(symbol)
    if spec is None:
        return []
    day = now // DAY_MS * DAY_MS
    shift = int(spec.get("shift") or 0)
    last_open = day - DAY_MS * (1 + shift)
    count = int(spec["n"])
    first_open = last_open - DAY_MS * (count - 1)
    closes = _fixture_closes(spec)
    rows: list[list[Any]] = []
    for index, close in enumerate(closes):
        open_time = first_open + index * DAY_MS
        text = "NaN" if spec.get("nan") and index == count // 2 else repr(close)
        rows.append([open_time, text, text, text, text, "1", open_time + DAY_MS - 1, "1", 0, "0", "0", "0"])
    forming_open = day
    forming = spec["forming"]
    if forming is None:
        forming = closes[-1] + float(spec["step"])
    text = repr(float(forming))
    rows.append([forming_open, text, text, text, text, "1", forming_open + DAY_MS - 1, "1", 0, "0", "0", "0"])
    return rows


def build_default_book() -> ScreenerBook:
    if fixture_enabled():
        return ScreenerBook(transport=FixtureTransport(), source_kind="fixture", store_reader=lambda *_args, **_kwargs: None)
    return ScreenerBook(
        transport=UrllibTransport(),
        source_kind="binance-public-rest",
        store_reader=read_mainstream_daily,
    )
