"""Top USDT/USD pairs from public CEX ticker batches. No keys, no orders.

One venue timing out marks only that venue unavailable. Results are cached
so the paper loop never waits on them.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import quote

from app.marketdata.cex import venue_urls
from app.marketdata.fetch import UpstreamError, get_json

TTL = 8.0
_VENUES = ("binance", "okx", "bybit", "coinbase")
_LABELS = {"binance": "Binance", "okx": "OKX", "bybit": "Bybit", "coinbase": "Coinbase"}
_SKIP_BASE = {"USDT", "USDC", "USD", "FDUSD", "TUSD", "DAI", "BUSD", "USDE", "EUR", "USD1"}

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def peek_board(venue: str) -> Optional[tuple[float, dict[str, Any]]]:
    """(age_sec, board) of the cached top-100 board, without any network call."""
    with _LOCK:
        hit = _CACHE.get(venue)
    return None if hit is None else (time.monotonic() - hit[0], hit[1])


def load_board(venue: str) -> dict[str, Any]:
    """Top-100 board for one venue (cached TTL seconds; one HTTP request when stale)."""
    return _load_one(venue) if venue in _VENUES else {"status": "unavailable", "rows": []}


def reset_ticker_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _flags() -> dict[str, Any]:
    return {"mode": "read_only", "liveEnabled": False, "liveDisabled": True}


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _coinbase_root() -> str:
    import os

    return os.getenv("COINBASE_MARKET_URL", "https://api.coinbase.com").rstrip("/")


def _skip_base(base: str) -> bool:
    text = base.upper()
    if text in _SKIP_BASE or len(text) < 2:
        return True
    return any(text.endswith(suffix) for suffix in ("UP", "DOWN", "BULL", "BEAR", "3L", "3S"))


def _row(base: str, symbol: str, last, change, volume, bid, ask) -> Optional[dict[str, Any]]:
    px = _num(last)
    if not base or _skip_base(base) or px is None or px <= 0:
        return None
    ch = _num(change)
    return {
        "base": base,
        "symbol": symbol,
        "last": px,
        "change_24h": ch,
        "volume_24h": _num(volume),
        "bid": _num(bid),
        "ask": _num(ask),
    }


def _parse_binance(payload: Any) -> list[dict[str, Any]]:
    rows = payload if isinstance(payload, list) else []
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or "")
        if not symbol.endswith("USDT"):
            continue
        change = _num(raw.get("priceChangePercent"))
        item = _row(
            symbol[: -len("USDT")],
            symbol,
            raw.get("lastPrice"),
            None if change is None else change / 100.0,
            raw.get("quoteVolume"),
            raw.get("bidPrice"),
            raw.get("askPrice"),
        )
        if item:
            out.append(item)
    return out


def _parse_okx(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data if isinstance(data, list) else []
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        inst = str(raw.get("instId") or "")
        if not inst.endswith("-USDT"):
            continue
        last = _num(raw.get("last"))
        open_px = _num(raw.get("open24h"))
        change = (last - open_px) / open_px if last and open_px else None
        item = _row(inst.split("-")[0], inst, last, change, raw.get("volCcy24h"), raw.get("bidPx"), raw.get("askPx"))
        if item:
            out.append(item)
    return out


def _parse_bybit(payload: Any) -> list[dict[str, Any]]:
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = result.get("list") if isinstance(result, dict) and isinstance(result.get("list"), list) else []
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or "")
        if not symbol.endswith("USDT"):
            continue
        item = _row(
            symbol[: -len("USDT")],
            symbol,
            raw.get("lastPrice"),
            raw.get("price24hPcnt"),
            raw.get("turnover24h"),
            raw.get("bid1Price"),
            raw.get("ask1Price"),
        )
        if item:
            out.append(item)
    return out


def _parse_coinbase(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        if str(raw.get("status") or "online").lower() not in {"online", "standard"}:
            continue
        if raw.get("is_disabled") or raw.get("trading_disabled"):
            continue
        product = str(raw.get("product_id") or "")
        quote = str(raw.get("quote_currency_id") or raw.get("quote_name") or "")
        if quote not in {"USD", "USDT"} and not product.endswith("-USD") and not product.endswith("-USDT"):
            continue
        base = str(raw.get("base_currency_id") or product.split("-")[0])
        last = _num(raw.get("price"))
        change = _num(raw.get("price_percentage_change_24h"))
        base_vol = _num(raw.get("volume_24h"))
        quote_vol = base_vol * last if base_vol is not None and last else None
        item = _row(
            base,
            product,
            last,
            None if change is None else change / 100.0,
            quote_vol,
            raw.get("bid"),
            raw.get("ask"),
        )
        if item:
            out.append(item)
    return out


def _fetch_coinbase(root: str) -> Any:
    rows: list[Any] = []
    cursor = ""
    merged: dict[str, Any] = {"products": rows}
    for _ in range(4):
        url = f"{root}/api/v3/brokerage/market/products?product_type=SPOT&limit=250"
        if cursor:
            url += "&cursor=" + quote(cursor)
        payload = get_json(url, timeout=6)
        if not isinstance(payload, dict):
            break
        chunk = payload.get("products") if isinstance(payload.get("products"), list) else []
        rows.extend(chunk)
        page = payload.get("pagination") if isinstance(payload.get("pagination"), dict) else {}
        cursor = str(page.get("next_cursor") or "")
        if not chunk or not page.get("has_next") or not cursor:
            break
    return merged


def _load_one(venue: str) -> dict[str, Any]:
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(venue)
        if hit and now - hit[0] < TTL:
            return hit[1]
    roots = venue_urls()
    try:
        if venue == "binance":
            payload = get_json(f"{roots['binance']}/api/v3/ticker/24hr", timeout=6)
            rows = _parse_binance(payload)
        elif venue == "okx":
            payload = get_json(f"{roots['okx']}/api/v5/market/tickers?instType=SPOT", timeout=6)
            rows = _parse_okx(payload)
        elif venue == "bybit":
            payload = get_json(f"{roots['bybit']}/v5/market/tickers?category=spot", timeout=6)
            rows = _parse_bybit(payload)
        else:
            payload = _fetch_coinbase(_coinbase_root())
            rows = _parse_coinbase(payload)
    except UpstreamError:
        board = {"status": "unavailable", "rows": []}
        with _LOCK:
            _CACHE[venue] = (time.monotonic(), board)
        return board
    except Exception:
        board = {"status": "unavailable", "rows": []}
        with _LOCK:
            _CACHE[venue] = (time.monotonic(), board)
        return board
    rows.sort(key=lambda row: float(row.get("volume_24h") or 0.0), reverse=True)
    board = {"status": "ok", "rows": rows[:100]}
    with _LOCK:
        _CACHE[venue] = (time.monotonic(), board)
    return board


def _boards() -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(_VENUES)) as pool:
        futs = {pool.submit(_load_one, venue): venue for venue in _VENUES}
        for fut, venue in ((fut, futs[fut]) for fut in futs):
            try:
                loaded[venue] = fut.result()
            except Exception:
                loaded[venue] = {"status": "unavailable", "rows": []}
    return loaded


def _health(boards: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for venue in _VENUES:
        status = boards.get(venue, {}).get("status") or "unavailable"
        out.append(
            {
                "id": venue,
                "label": _LABELS[venue],
                "status": "ok" if status == "ok" else "unavailable",
                "status_label": "可用" if status == "ok" else "不可用",
            }
        )
    return out


def _sort_rows(rows: list[dict[str, Any]], sort: str, direction: str) -> list[dict[str, Any]]:
    key = sort if sort in {"volume", "change", "price", "symbol", "spread"} else "volume"
    mult = 1 if direction == "asc" else -1

    def value(row: dict[str, Any]):
        if key == "symbol":
            return (0, str(row.get("base") or row.get("symbol") or ""))
        field = {"volume": "volume_24h", "change": "change_24h", "price": "last", "spread": "spread_bps"}[key]
        raw = row.get(field)
        if raw is None:
            return (1, 0.0)
        return (0, float(raw))

    ranked = sorted(rows, key=value)
    if mult < 0:
        present = [row for row in reversed(ranked) if value(row)[0] == 0]
        missing = [row for row in ranked if value(row)[0] != 0]
        return present + missing
    return ranked


def _filter_bucket(rows: list[dict[str, Any]], bucket: str, q: str) -> list[dict[str, Any]]:
    query = (q or "").strip().upper()
    out = []
    for row in rows:
        change = row.get("change_24h")
        if bucket == "gainers" and not (isinstance(change, (int, float)) and change > 0):
            continue
        if bucket == "losers" and not (isinstance(change, (int, float)) and change < 0):
            continue
        if query and query not in str(row.get("base") or "").upper() and query not in str(row.get("symbol") or "").upper():
            continue
        out.append(row)
    return out


def _cross_rows(boards: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by_base: dict[str, dict[str, dict[str, Any]]] = {}
    for venue in _VENUES:
        if boards.get(venue, {}).get("status") != "ok":
            continue
        for row in boards[venue].get("rows") or []:
            by_base.setdefault(row["base"], {})[venue] = row
    out = []
    for base, quotes in by_base.items():
        if len(quotes) < 2:
            continue
        bids = []
        asks = []
        for venue, row in quotes.items():
            bid = row.get("bid") if row.get("bid") else row.get("last")
            ask = row.get("ask") if row.get("ask") else row.get("last")
            if bid:
                bids.append((venue, float(bid)))
            if ask:
                asks.append((venue, float(ask)))
        if not bids or not asks:
            continue
        bid_venue, bid_px = max(bids, key=lambda item: item[1])
        ask_venue, ask_px = min(asks, key=lambda item: item[1])
        mid = min(bid_px, ask_px)
        spread = abs(ask_px - bid_px) / mid * 10_000.0 if mid > 0 else None
        volume = sum(float(row.get("volume_24h") or 0.0) for row in quotes.values())
        out.append(
            {
                "base": base,
                "symbol": base + "USDT",
                "last": quotes[next(iter(quotes))].get("last"),
                "change_24h": None,
                "volume_24h": volume,
                "bid": bid_px,
                "ask": ask_px,
                "bid_venue": _LABELS[bid_venue],
                "ask_venue": _LABELS[ask_venue],
                "spread_bps": spread,
                "venues": [_LABELS[venue] for venue in _VENUES if venue in quotes],
            }
        )
    return out


def list_tickers(
    venue: str = "binance",
    limit: int = 100,
    q: str = "",
    sort: str = "volume",
    direction: str = "desc",
    bucket: str = "all",
) -> dict[str, Any]:
    venue_id = venue if venue in {*_VENUES, "cross"} else "binance"
    limit_n = max(1, min(int(limit or 100), 100))
    boards = _boards()
    health = _health(boards)
    if venue_id == "cross":
        rows = _cross_rows(boards)
        status = "ok" if any(item["status"] == "ok" for item in health) else "unavailable"
    else:
        board = boards.get(venue_id) or {"status": "unavailable", "rows": []}
        rows = list(board.get("rows") or [])
        status = "ok" if board.get("status") == "ok" else "unavailable"
    rows = _filter_bucket(rows, bucket if bucket in {"all", "gainers", "losers"} else "all", q)
    rows = _sort_rows(rows, sort, direction)
    page = rows[:limit_n]
    return {
        **_flags(),
        "venue": venue_id,
        "status": status,
        "status_label": "可用" if status == "ok" else "不可用",
        "venues": health,
        "total": len(rows),
        "items": page,
    }
