"""Public REST tickers for a few CEXs. No API keys and no order routes.

Each venue is fetched on its own. A timeout or block marks that venue
不可用 and leaves the others in place. Results sit in a short cache so the
paper loop never waits on these calls.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from app.marketdata.fetch import UpstreamError, get_json

TTL = 5.0
_VENUES = ("binance", "okx", "bybit", "coinbase")
_LABELS = {
    "binance": "Binance",
    "okx": "OKX",
    "bybit": "Bybit",
    "coinbase": "Coinbase",
}

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def reset_cex_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def majors_bases() -> list[str]:
    raw = os.getenv("MAJORS_BASES", "SOL,BTC,ETH")
    out = []
    for part in raw.split(","):
        base = "".join(ch for ch in part.strip().upper() if ch.isalnum())
        if base and base not in out:
            out.append(base)
    return out or ["SOL", "BTC", "ETH"]


def _base_url(env: str, default: str) -> str:
    return os.getenv(env, default).rstrip("/")


def venue_urls() -> dict[str, str]:
    return {
        "binance": _base_url("BINANCE_REST_URL", "https://api.binance.com"),
        "okx": _base_url("OKX_REST_URL", "https://www.okx.com"),
        "bybit": _base_url("BYBIT_REST_URL", "https://api.bybit.com"),
        "coinbase": _base_url("COINBASE_REST_URL", "https://api.exchange.coinbase.com"),
    }


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _pair(venue: str, base: str) -> str:
    if venue == "okx":
        return f"{base}-USDT"
    if venue == "coinbase":
        return f"{base}-USD"
    return f"{base}USDT"


def _url(venue: str, base: str) -> str:
    roots = venue_urls()
    pair = _pair(venue, base)
    if venue == "binance":
        return f"{roots['binance']}/api/v3/ticker/24hr?symbol={pair}"
    if venue == "okx":
        return f"{roots['okx']}/api/v5/market/ticker?instId={pair}"
    if venue == "bybit":
        return f"{roots['bybit']}/v5/market/tickers?category=spot&symbol={pair}"
    return f"{roots['coinbase']}/products/{pair}/ticker"


def _parse(venue: str, payload: Any, base: str) -> Optional[dict[str, Any]]:
    if venue == "binance" and isinstance(payload, dict) and payload.get("lastPrice"):
        change = _num(payload.get("priceChangePercent"))
        return {
            "last": _num(payload.get("lastPrice")),
            "change_24h": None if change is None else change / 100.0,
            "volume_24h": _num(payload.get("quoteVolume")),
        }
    if venue == "okx" and isinstance(payload, dict):
        rows = payload.get("data") if isinstance(payload.get("data"), list) else []
        if not rows:
            return None
        row = rows[0]
        last = _num(row.get("last"))
        open_px = _num(row.get("open24h"))
        change = None
        if last is not None and open_px:
            change = (last - open_px) / open_px
        return {"last": last, "change_24h": change, "volume_24h": _num(row.get("volCcy24h"))}
    if venue == "bybit" and isinstance(payload, dict):
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        rows = result.get("list") if isinstance(result.get("list"), list) else []
        if not rows:
            return None
        row = rows[0]
        return {
            "last": _num(row.get("lastPrice")),
            "change_24h": _num(row.get("price24hPcnt")),
            "volume_24h": _num(row.get("turnover24h")),
        }
    if venue == "coinbase" and isinstance(payload, dict):
        last = _num(payload.get("price") or payload.get("last"))
        if last is None:
            return None
        stats = payload.get("_stats") if isinstance(payload.get("_stats"), dict) else {}
        open_px = _num(stats.get("open"))
        volume = _num(stats.get("volume"))
        change = None
        if open_px:
            change = (last - open_px) / open_px
        quote_vol = volume * last if volume is not None else None
        return {"last": last, "change_24h": change, "volume_24h": quote_vol}
    return None


def _fetch_venue_symbol(venue: str, base: str) -> dict[str, Any]:
    key = f"{venue}:{base}"
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < TTL:
            return dict(hit[1])
    try:
        payload = get_json(_url(venue, base))
        if venue == "coinbase" and isinstance(payload, dict):
            try:
                stats = get_json(f"{venue_urls()['coinbase']}/products/{_pair(venue, base)}/stats")
                if isinstance(stats, dict):
                    payload = dict(payload)
                    payload["_stats"] = stats
            except UpstreamError:
                pass
        parsed = _parse(venue, payload, base)
    except UpstreamError as exc:
        status = "not_listed" if exc.kind == "not_found" else "unavailable"
        parsed = None
        row = {"status": status, "last": None, "change_24h": None, "volume_24h": None}
        with _LOCK:
            _CACHE[key] = (time.monotonic(), row)
        return dict(row)
    except Exception:
        row = {"status": "unavailable", "last": None, "change_24h": None, "volume_24h": None}
        with _LOCK:
            _CACHE[key] = (time.monotonic(), row)
        return dict(row)
    if not parsed or parsed.get("last") is None:
        row = {"status": "not_listed", "last": None, "change_24h": None, "volume_24h": None}
    else:
        row = {"status": "ok", **parsed}
    with _LOCK:
        _CACHE[key] = (time.monotonic(), row)
    return dict(row)


def _load_venue(venue: str, bases: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    blocked = False
    for base in bases:
        if blocked:
            out[base] = {"status": "unavailable", "last": None, "change_24h": None, "volume_24h": None}
            continue
        row = _fetch_venue_symbol(venue, base)
        out[base] = row
        if row.get("status") == "unavailable":
            blocked = True
    return out


def _health(venue: str, rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    statuses = [row.get("status") for row in rows.values()]
    ok = any(status == "ok" for status in statuses)
    down = any(status == "unavailable" for status in statuses)
    status = "ok" if ok else "unavailable" if down or not statuses else "unavailable"
    return {
        "id": venue,
        "label": _LABELS[venue],
        "status": status,
        "status_label": "可用" if status == "ok" else "不可用",
    }


def _spread_bps(quotes: dict[str, dict[str, Any]]) -> Optional[float]:
    lasts = [float(row["last"]) for row in quotes.values() if row.get("status") == "ok" and row.get("last")]
    if len(lasts) < 2:
        return None
    low = min(lasts)
    if low <= 0:
        return None
    return (max(lasts) - low) / low * 10_000.0


def build_majors() -> dict[str, Any]:
    bases = majors_bases()
    loaded: dict[str, dict[str, dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=len(_VENUES)) as pool:
        futs = {pool.submit(_load_venue, venue, bases): venue for venue in _VENUES}
        for fut, venue in ((fut, futs[fut]) for fut in futs):
            try:
                loaded[venue] = fut.result()
            except Exception:
                loaded[venue] = {
                    base: {"status": "unavailable", "last": None, "change_24h": None, "volume_24h": None}
                    for base in bases
                }
    rows = []
    for base in bases:
        quotes = {venue: loaded.get(venue, {}).get(base) or {"status": "unavailable"} for venue in _VENUES}
        rows.append({"base": base, "quotes": quotes, "spread_bps": _spread_bps(quotes)})
    return {
        "mode": "read_only",
        "liveEnabled": False,
        "liveDisabled": True,
        "quote": "USDT",
        "bases": bases,
        "venues": [_health(venue, loaded.get(venue, {})) for venue in _VENUES],
        "rows": rows,
    }


def compare_symbol(base: str, onchain_usd: Optional[float] = None) -> dict[str, Any]:
    text = "".join(ch for ch in (base or "").upper() if ch.isalnum())[:16]
    loaded: dict[str, dict[str, dict[str, Any]]] = {}
    if text:
        with ThreadPoolExecutor(max_workers=len(_VENUES)) as pool:
            futs = {pool.submit(_load_venue, venue, [text]): venue for venue in _VENUES}
            for fut, venue in ((fut, futs[fut]) for fut in futs):
                try:
                    loaded[venue] = fut.result()
                except Exception:
                    loaded[venue] = {
                        text: {"status": "unavailable", "last": None, "change_24h": None, "volume_24h": None}
                    }
    quotes = []
    for venue in _VENUES:
        row = (loaded.get(venue) or {}).get(text) or {"status": "unavailable"}
        last = row.get("last")
        delta = None
        if last and onchain_usd and onchain_usd > 0 and row.get("status") == "ok":
            delta = (float(last) - float(onchain_usd)) / float(onchain_usd)
        quotes.append(
            {
                "id": venue,
                "label": _LABELS[venue],
                "status": row.get("status") or "unavailable",
                "status_label": "可用" if row.get("status") == "ok" else "未上架" if row.get("status") == "not_listed" else "不可用",
                "last": last,
                "change_24h": row.get("change_24h"),
                "vs_onchain": delta,
            }
        )
    listed = any(row["status"] == "ok" for row in quotes)
    return {
        "mode": "read_only",
        "liveEnabled": False,
        "liveDisabled": True,
        "base": text,
        "listed": listed,
        "onchain_usd": onchain_usd,
        "venues": quotes,
    }
