"""Pump.fun market universe for the 市场 page.

Public HTTP only. Independent of PUMPFUN_DISCOVERY. A short
cache keeps these calls off the paper loop. Mock symbols appear only when
AUU_UNIVERSE_MOCK is set.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional
from urllib.parse import quote

from app.marketdata.fetch import UpstreamError, get_json
from app.marketdata.pump_search import (
    _from_dex_pair,
    _from_pump_coin,
    dexscreener_base,
    pump_frontend_base,
)

CATALOG_TTL = 20.0
TABS = ("hot", "new", "graduating", "graduated", "gainers", "losers", "watch")
_SORTS = {
    "price": "price_usd",
    "change5m": "change_5m",
    "change1h": "change_1h",
    "change24": "change_24h",
    "volume": "volume_24h_usd",
    "mcap": "market_cap_usd",
    "progress": "progress_pct",
    "age": "created_ts",
    "name": "symbol",
}

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, Any]] = {}
_SPARK: dict[str, list[tuple[float, float]]] = {}


def reset_universe_cache() -> None:
    with _LOCK:
        _CACHE.clear()
        _SPARK.clear()


def mock_enabled() -> bool:
    return os.getenv("AUU_UNIVERSE_MOCK", "").strip().lower() in {"1", "true", "on", "yes"}


def _flags() -> dict[str, Any]:
    return {"mode": "paper", "liveEnabled": False, "liveDisabled": True}


def _cached(key: str, ttl: float, loader):
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = loader()
    with _LOCK:
        _CACHE[key] = (time.monotonic(), value)
    return value


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _pump_page(sort: str, offset: int, limit: int) -> list[dict[str, Any]]:
    url = (
        f"{pump_frontend_base()}/coins?offset={offset}&limit={limit}"
        f"&sort={quote(sort)}&order=DESC&includeNsfw=false"
    )
    payload = get_json(url)
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _attach(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    coin = _from_pump_coin(raw)
    if not coin:
        return None
    created = _int(raw.get("created_timestamp"))
    holders = _num(raw.get("holder_count") if raw.get("holder_count") is not None else raw.get("holders"))
    coin["created_ts"] = created or None
    coin["holders"] = int(holders) if holders and holders > 0 else None
    coin["mock"] = False
    return coin


def _dex_changes(pair: dict[str, Any]) -> Optional[dict[str, Any]]:
    base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    mint = str(base.get("address") or "").strip()
    if not mint:
        return None
    chain = str(pair.get("chainId") or "solana").lower()
    if chain != "solana":
        return None
    change = pair.get("priceChange") if isinstance(pair.get("priceChange"), dict) else {}
    volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}

    def frac(key: str) -> Optional[float]:
        raw = _num(change.get(key))
        return None if raw is None else raw / 100.0

    return {
        "mint": mint,
        "change_5m": frac("m5"),
        "change_1h": frac("h1"),
        "change_24h": frac("h24"),
        "volume_24h_usd": _num(volume.get("h24")),
    }


def _enrich(coins: list[dict[str, Any]]) -> None:
    """Fill 5m/1h/24h and volume from DexScreener. Missing data stays blank."""
    targets = [c for c in coins if c.get("graduated") or c.get("market_cap_usd")]
    targets.sort(key=lambda row: float(row.get("market_cap_usd") or 0.0), reverse=True)
    mints = [str(c["mint"]) for c in targets[:90]]
    by_mint = {str(c["mint"]): c for c in coins}
    for start in range(0, len(mints), 30):
        chunk = mints[start : start + 30]
        url = f"{dexscreener_base()}/tokens/v1/solana/{','.join(chunk)}"
        try:
            payload = get_json(url)
        except UpstreamError:
            continue
        rows = payload if isinstance(payload, list) else []
        if isinstance(payload, dict):
            rows = payload.get("pairs") or []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            extra = _dex_changes(raw)
            if not extra:
                continue
            coin = by_mint.get(extra["mint"])
            if not coin:
                continue
            for key in ("change_5m", "change_1h", "change_24h", "volume_24h_usd"):
                if coin.get(key) is None and extra.get(key) is not None:
                    coin[key] = extra[key]


def _dex_fallback() -> list[dict[str, Any]]:
    url = f"{dexscreener_base()}/latest/dex/search?q=pump"
    payload = get_json(url)
    rows = payload.get("pairs") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    out: dict[str, dict[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        coin = _from_dex_pair(raw)
        if not coin:
            continue
        extra = _dex_changes(raw) or {}
        for key in ("change_5m", "change_1h", "change_24h", "volume_24h_usd"):
            if extra.get(key) is not None:
                coin[key] = extra[key]
        coin["created_ts"] = None
        coin["holders"] = None
        coin["mock"] = False
        out[coin["mint"]] = coin
    return list(out.values())


def _note_spark(coin: dict[str, Any]) -> list[float]:
    mint = str(coin.get("mint") or "")
    price = _num(coin.get("price_usd")) or _num(coin.get("price_sol"))
    hist: list[tuple[float, float]] = []
    if mint and price and price > 0:
        now = time.time()
        with _LOCK:
            hist = list(_SPARK.get(mint) or [])
            if not hist or now - hist[-1][0] >= 8:
                hist.append((now, float(price)))
                hist = hist[-24:]
                _SPARK[mint] = hist
    pts = [p for _, p in hist]
    if len(pts) >= 2:
        return pts
    change = _num(coin.get("change_24h"))
    if price and change is not None and change > -0.97:
        return [float(price) / (1.0 + change), float(price)]
    return pts


def _mock_rows() -> list[dict[str, Any]]:
    if not mock_enabled():
        return []
    from app.paper.markets import build_markets

    rows = []
    for item in build_markets().get("items") or []:
        mint = str(item.get("mint") or "")
        if not mint:
            continue
        progress = item.get("progress_pct")
        rows.append(
            {
                "mint": mint,
                "name": item.get("base") or item.get("symbol"),
                "symbol": item.get("base") or item.get("symbol"),
                "image": None,
                "created_ts": None,
                "price_sol": item.get("price_sol"),
                "price_usd": None,
                "market_cap_sol": item.get("market_cap_sol"),
                "market_cap_usd": None,
                "change_5m": None,
                "change_1h": None,
                "change_24h": item.get("change_pct"),
                "volume_24h_usd": None,
                "holders": None,
                "progress_pct": progress,
                "graduated": bool(progress is not None and float(progress) >= 100),
                "venue": "曲线",
                "spark": list(item.get("spark") or []),
                "mock": True,
                "source": "mock",
            }
        )
    return rows


def _load_catalog() -> dict[str, Any]:
    def load() -> dict[str, Any]:
        found: dict[str, dict[str, Any]] = {}
        failed = 0
        feeds = (
            ("last_trade_timestamp", 0),
            ("created_timestamp", 0),
            ("created_timestamp", 80),
            ("market_cap", 0),
        )
        for sort, offset in feeds:
            try:
                rows = _pump_page(sort, offset, 80)
            except UpstreamError:
                failed += 1
                continue
            for raw in rows:
                coin = _attach(raw)
                if coin:
                    found[str(coin["mint"])] = coin
        error = None
        if not found:
            try:
                for coin in _dex_fallback():
                    found[str(coin["mint"])] = coin
            except UpstreamError:
                error = "行情暂时不可用"
        else:
            _enrich(list(found.values()))
        for coin in _mock_rows():
            found.setdefault(str(coin["mint"]), coin)
        if not found and error is None and failed:
            error = "行情暂时不可用"
        for coin in found.values():
            if not coin.get("spark"):
                coin["spark"] = _note_spark(coin)
        return {"coins": list(found.values()), "error": None if found else error}

    try:
        return _cached("catalog", CATALOG_TTL, load)
    except Exception:
        return {"coins": [], "error": "行情暂时不可用"}


def _match_query(coin: dict[str, Any], q: str) -> bool:
    if not q:
        return True
    blob = " ".join(
        str(coin.get(key) or "")
        for key in ("symbol", "name", "mint")
    ).lower()
    return q in blob


def _tab_ok(coin: dict[str, Any], tab: str, wanted: set[str]) -> bool:
    if tab == "watch":
        return str(coin.get("mint") or "") in wanted
    if tab == "new":
        return not coin.get("mock")
    if tab == "graduating":
        progress = _num(coin.get("progress_pct"))
        return not coin.get("graduated") and progress is not None and progress >= 55
    if tab == "graduated":
        return bool(coin.get("graduated"))
    if tab == "gainers":
        change = _num(coin.get("change_24h"))
        return change is not None and change > 0
    if tab == "losers":
        change = _num(coin.get("change_24h"))
        return change is not None and change < 0
    return True


def _sort_value(coin: dict[str, Any], key: str) -> tuple[int, float | str]:
    if key == "name":
        return (0, str(coin.get("symbol") or "").lower())
    raw = coin.get(key)
    if raw is None or raw == "":
        return (1, 0.0)
    if isinstance(raw, str):
        return (0, raw.lower())
    num = _num(raw)
    if num is None:
        return (1, 0.0)
    return (0, num)


def _default_sort(tab: str) -> tuple[str, int]:
    if tab == "new":
        return "created_ts", -1
    if tab == "graduating":
        return "progress_pct", -1
    if tab == "gainers":
        return "change_24h", -1
    if tab == "losers":
        return "change_24h", 1
    if tab == "hot":
        return "volume_24h_usd", -1
    return "market_cap_usd", -1


def _public(coin: dict[str, Any], now_ms: int) -> dict[str, Any]:
    created = _int(coin.get("created_ts"))
    age = None
    if created > 0:
        created_ms = created if created > 10_000_000_000 else created * 1000
        age = max(0, int((now_ms - created_ms) / 1000))
    mcap_usd = _num(coin.get("market_cap_usd"))
    return {
        "mint": coin.get("mint"),
        "name": coin.get("name"),
        "symbol": coin.get("symbol"),
        "image": coin.get("image"),
        "created_ts": created or None,
        "age_sec": age,
        "price_sol": _num(coin.get("price_sol")),
        "price_usd": _num(coin.get("price_usd")),
        "change_5m": _num(coin.get("change_5m")),
        "change_1h": _num(coin.get("change_1h")),
        "change_24h": _num(coin.get("change_24h")),
        "volume_24h_usd": _num(coin.get("volume_24h_usd")),
        "market_cap_sol": _num(coin.get("market_cap_sol")),
        "market_cap_usd": mcap_usd,
        "holders": coin.get("holders"),
        "progress_pct": _num(coin.get("progress_pct")),
        "graduated": bool(coin.get("graduated")),
        "venue": coin.get("venue"),
        "spark": list(coin.get("spark") or []),
        "mock": bool(coin.get("mock")),
    }


def _movers(coins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = [c for c in coins if _num(c.get("change_24h")) is not None and not c.get("mock")]
    ranked.sort(key=lambda row: float(row.get("change_24h") or 0.0), reverse=True)
    out = []
    for coin in ranked[:12]:
        out.append(
            {
                "mint": coin.get("mint"),
                "symbol": coin.get("symbol"),
                "price_usd": _num(coin.get("price_usd")),
                "change_24h": _num(coin.get("change_24h")),
            }
        )
    return out


def list_universe(
    tab: str = "hot",
    offset: int = 0,
    limit: int = 60,
    q: str = "",
    sort: str = "",
    direction: str = "",
    mints: str = "",
) -> dict[str, Any]:
    tab_id = tab if tab in TABS else "hot"
    limit_n = max(1, min(int(limit or 60), 100))
    offset_n = max(0, int(offset or 0))
    query = (q or "").strip().lower()
    wanted = {part.strip() for part in (mints or "").split(",") if part.strip()}
    catalog = _load_catalog()
    coins = list(catalog.get("coins") or [])
    filtered = [
        coin
        for coin in coins
        if _match_query(coin, query) and _tab_ok(coin, tab_id, wanted)
    ]
    key, default_dir = _default_sort(tab_id)
    if sort in _SORTS:
        key = _SORTS[sort]
    mult = default_dir
    if direction in {"asc", "1"}:
        mult = 1
    elif direction in {"desc", "-1"}:
        mult = -1
    filtered.sort(key=lambda row: _sort_value(row, key))
    if mult < 0:
        filtered.reverse()
        # Keep missing values at the end after a descending sort.
        present = [row for row in filtered if _sort_value(row, key)[0] == 0]
        missing = [row for row in filtered if _sort_value(row, key)[0] != 0]
        filtered = present + missing
    now_ms = int(time.time() * 1000)
    page = filtered[offset_n : offset_n + limit_n]
    body = {
        **_flags(),
        "tab": tab_id,
        "offset": offset_n,
        "limit": limit_n,
        "total": len(filtered),
        "quote": "SOL",
        "mock_included": mock_enabled(),
        "error": None if filtered or not catalog.get("error") else catalog.get("error"),
        "asof_ts": now_ms,
        "movers": _movers(coins),
        "items": [_public(coin, now_ms) for coin in page],
    }
    if catalog.get("error") and not coins:
        body["error"] = catalog["error"]
    return body
