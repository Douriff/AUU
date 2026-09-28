"""Pump.fun universe search, with a DexScreener fallback.

Reads public HTTP only. Results are cached. A failure returns an empty list
instead of raising into the paper loop.
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import quote

from app.marketdata.fetch import UpstreamError, get_json
from app.models.contracts import PumpfunPaperSnapshot
from app.providers.pumpfun_curve_math import (
    TOKEN_DECIMALS,
    market_cap_lamports,
    price_sol,
    progress_bps,
)

SEARCH_TTL = 20.0
COIN_TTL = 8.0
CANDLE_TTL = 20.0
_DEX_OK = {"pumpfun", "pumpswap", "raydium", "raydium_clmm", "raydium_cp", "raydium_amm"}
_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,48}$")

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, Any]] = {}
_BY_MINT: dict[str, dict[str, Any]] = {}
_BY_SYMBOL: dict[str, dict[str, Any]] = {}


def reset_search_cache() -> None:
    with _LOCK:
        _CACHE.clear()
        _BY_MINT.clear()
        _BY_SYMBOL.clear()


def pump_frontend_base() -> str:
    return os.getenv("PUMPFUN_FRONTEND_API", "https://frontend-api-v3.pump.fun").rstrip("/")


def dexscreener_base() -> str:
    return os.getenv("DEXSCREENER_API", "https://api.dexscreener.com").rstrip("/")


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
    if out != out:  # NaN
        return None
    return out


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _safe_image(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if text.startswith("https://") or text.startswith("http://"):
        return text
    return None


def _flags() -> dict[str, Any]:
    return {"mode": "paper", "liveEnabled": False, "liveDisabled": True}


def amm_impact_bps(
    notional_sol: float,
    liquidity_usd: Optional[float],
    price_sol_px: Optional[float],
    price_usd: Optional[float],
) -> float:
    """Constant-product estimate: notional / (pool SOL / 2), in bps.

    Missing liquidity is a wide estimate so a buy stays inside the 150 bps cap
    only when the pool can actually be sized.
    """
    if (
        liquidity_usd is None
        or liquidity_usd <= 0
        or price_sol_px is None
        or price_sol_px <= 0
        or price_usd is None
        or price_usd <= 0
    ):
        return 10_000.0
    sol_usd = price_usd / price_sol_px
    if sol_usd <= 0:
        return 10_000.0
    reserve_sol = (liquidity_usd / sol_usd) / 2.0
    if reserve_sol <= 1e-12:
        return 10_000.0
    return abs(float(notional_sol)) / reserve_sol * 10_000.0


def _venue_label(dex: str, graduated: bool) -> str:
    key = (dex or "").lower()
    if not graduated and key in {"", "pumpfun", "bonding-curve"}:
        return "曲线"
    if "raydium" in key:
        return "Raydium"
    if key == "pumpswap":
        return "PumpSwap"
    if graduated:
        return "已毕业"
    return "曲线"


def _from_pump_coin(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    mint = str(raw.get("mint") or raw.get("address") or "").strip()
    if not mint:
        return None
    vs = _int(raw.get("virtual_sol_reserves"))
    vt = _int(raw.get("virtual_token_reserves"))
    rs = _int(raw.get("real_sol_reserves"))
    rt = _int(raw.get("real_token_reserves"))
    supply = _int(raw.get("total_supply") or raw.get("token_total_supply"))
    base_dec = _int(raw.get("base_decimals")) or TOKEN_DECIMALS
    complete = bool(raw.get("complete") or raw.get("raydium_pool") or raw.get("pump_swap_pool"))
    inverted = bool(raw.get("inverted"))
    usd_mcap = _num(raw.get("usd_market_cap") or raw.get("market_cap_usd"))
    raw_mcap = _num(raw.get("market_cap"))
    # `market_cap` is SOL when it is far below `usd_market_cap`.
    mcap_sol = raw_mcap if raw_mcap is not None and (usd_mcap is None or usd_mcap > raw_mcap * 2) else None
    ui_supply = supply / (10**base_dec) if supply else 0.0
    on_curve = vs > 0 and vt > 0 and not complete and not inverted
    # vs/vt is lamports per raw token. Human SOL per token scales by 10^(decimals-9).
    human_scale = 10 ** (base_dec - 9)
    if on_curve:
        px = price_sol(vs, vt) * human_scale
        if mcap_sol is None and supply > 0:
            mcap_sol = market_cap_lamports(vs, vt, supply) / 1_000_000_000
    elif mcap_sol is not None and ui_supply > 0:
        # Graduated and inverted coins keep frozen virtual reserves. Spot is mcap / supply.
        px = mcap_sol / ui_supply
    elif vs > 0 and vt > 0:
        px = price_sol(vs, vt) * human_scale
    else:
        px = _num(raw.get("price_sol"))
    price_usd = usd_mcap / ui_supply if usd_mcap and ui_supply > 0 else None
    progress = None if complete or vs <= 0 or inverted else progress_bps(rt) / 100.0
    dex = "pumpswap" if raw.get("pump_swap_pool") else "raydium" if raw.get("raydium_pool") else "pumpfun"
    graduated = not on_curve
    return {
        "mint": mint,
        "name": str(raw.get("name") or raw.get("symbol") or mint[:6]),
        "symbol": str(raw.get("symbol") or raw.get("name") or "TOKEN")[:16],
        "image": _safe_image(raw.get("image_uri") or raw.get("image") or raw.get("imageUrl")),
        "price_sol": px,
        "price_usd": price_usd,
        "market_cap_sol": mcap_sol,
        "market_cap_usd": usd_mcap if usd_mcap is not None else raw_mcap,
        "change_24h": _num(raw.get("price_change_24h") or raw.get("change_24h")),
        "volume_24h_usd": _num(raw.get("volume_24h") or raw.get("volume")),
        "progress_pct": progress,
        "graduated": graduated,
        "venue": _venue_label(dex, graduated),
        "dex": dex,
        "liquidity_usd": _num(raw.get("liquidity_usd")),
        "virtual_sol_reserves": str(vs),
        "virtual_token_reserves": str(vt),
        "real_sol_reserves": str(rs),
        "real_token_reserves": str(rt),
        "token_total_supply": str(supply or 0),
        "has_curve": vs > 0 and vt > 0 and not graduated,
        "source": "pumpfun",
    }


def _from_dex_pair(pair: dict[str, Any]) -> Optional[dict[str, Any]]:
    if str(pair.get("chainId") or "").lower() != "solana":
        return None
    dex = str(pair.get("dexId") or "").lower()
    if dex not in _DEX_OK:
        return None
    base = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    mint = str(base.get("address") or "").strip()
    if not mint:
        return None
    px_sol = _num(pair.get("priceNative"))
    px_usd = _num(pair.get("priceUsd"))
    change = pair.get("priceChange") if isinstance(pair.get("priceChange"), dict) else {}
    volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    liq = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
    info = pair.get("info") if isinstance(pair.get("info"), dict) else {}
    change_pct = _num(change.get("h24"))
    graduated = dex != "pumpfun"
    return {
        "mint": mint,
        "name": str(base.get("name") or base.get("symbol") or mint[:6]),
        "symbol": str(base.get("symbol") or "TOKEN")[:16],
        "image": _safe_image(info.get("imageUrl") or info.get("image")),
        "price_sol": px_sol,
        "price_usd": px_usd,
        "market_cap_sol": (float(pair["marketCap"]) / (px_usd / px_sol)) if (
            _num(pair.get("marketCap")) and px_usd and px_sol
        ) else None,
        "market_cap_usd": _num(pair.get("marketCap") or pair.get("fdv")),
        "change_24h": None if change_pct is None else change_pct / 100.0,
        "volume_24h_usd": _num(volume.get("h24")),
        "progress_pct": None if graduated else _num(pair.get("progress")),
        "graduated": graduated,
        "venue": _venue_label(dex, graduated),
        "dex": dex,
        "liquidity_usd": _num(liq.get("usd")),
        "virtual_sol_reserves": "0",
        "virtual_token_reserves": "0",
        "real_sol_reserves": "0",
        "real_token_reserves": "0",
        "token_total_supply": "0",
        "has_curve": False,
        "source": "dexscreener",
    }


def _merge(primary: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(primary)
    for key, value in extra.items():
        if out.get(key) in {None, "", "0", 0, False} and value not in {None, "", "0", 0}:
            out[key] = value
    if extra.get("graduated") and not primary.get("has_curve"):
        out["graduated"] = True
        out["venue"] = extra.get("venue") or out.get("venue")
        out["has_curve"] = False
    if not out.get("has_curve"):
        out["graduated"] = True
    return out


def _as_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("coins", "results", "data", "pairs", "items"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        if payload.get("mint") or payload.get("baseToken"):
            return [payload]
    return []


def _pump_search(q: str, limit: int) -> list[dict[str, Any]]:
    base = pump_frontend_base()
    term = quote(q)
    urls = [
        f"{base}/coins/search?offset=0&limit={limit}&sort=market_cap&includeNsfw=false&order=DESC&searchTerm={term}",
        f"{base}/coins?offset=0&limit={limit}&sort=market_cap&order=DESC&includeNsfw=false&searchTerm={term}",
    ]
    last: Optional[UpstreamError] = None
    for url in urls:
        try:
            payload = get_json(url)
        except UpstreamError as exc:
            last = exc
            continue
        rows = []
        for raw in _as_list(payload):
            coin = _from_pump_coin(raw)
            if coin:
                rows.append(coin)
        return rows[:limit]
    if last:
        raise last
    return []


def _dex_search(q: str, limit: int) -> list[dict[str, Any]]:
    url = f"{dexscreener_base()}/latest/dex/search?q={quote(q)}"
    payload = get_json(url)
    ranked: list[dict[str, Any]] = []
    for raw in _as_list(payload):
        coin = _from_dex_pair(raw)
        if coin:
            ranked.append(coin)
    ranked.sort(key=lambda row: float(row.get("liquidity_usd") or 0.0), reverse=True)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in ranked:
        if row["mint"] in seen:
            continue
        seen.add(row["mint"])
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _overlay_dex(items: list[dict[str, Any]], q: str, limit: int) -> list[dict[str, Any]]:
    """Fill 24h change, volume, and liquidity from DexScreener. Prices stay on pump.fun."""
    try:
        extra_rows = _dex_search(q, max(limit, 25))
    except UpstreamError:
        return items
    by_mint = {row["mint"]: row for row in extra_rows}
    merged: list[dict[str, Any]] = []
    for coin in items:
        extra = by_mint.get(coin["mint"])
        merged.append(_merge(coin, extra) if extra else coin)
    return merged


def _try_pump_coin(mint: str) -> Optional[dict[str, Any]]:
    try:
        payload = get_json(f"{pump_frontend_base()}/coins/{quote(mint)}")
    except UpstreamError:
        return None
    rows = _as_list(payload)
    if not rows and isinstance(payload, dict):
        rows = [payload]
    for raw in rows:
        coin = _from_pump_coin(raw)
        if coin and coin["mint"] == mint:
            return coin
    return _from_pump_coin(rows[0]) if rows else None


def _try_dex_token(mint: str) -> Optional[dict[str, Any]]:
    urls = [
        f"{dexscreener_base()}/tokens/v1/solana/{quote(mint)}",
        f"{dexscreener_base()}/latest/dex/tokens/{quote(mint)}",
    ]
    for url in urls:
        try:
            payload = get_json(url)
        except UpstreamError:
            continue
        coins = [c for raw in _as_list(payload) if (c := _from_dex_pair(raw))]
        coins = [c for c in coins if c["mint"] == mint] or coins
        if coins:
            coins.sort(key=lambda row: float(row.get("liquidity_usd") or 0.0), reverse=True)
            return coins[0]
    return None


def _load_coin(mint: str) -> Optional[dict[str, Any]]:
    pump = _try_pump_coin(mint)
    dex = None
    if pump is None or pump.get("graduated") or not pump.get("price_sol"):
        dex = _try_dex_token(mint)
    if pump and dex:
        return _merge(pump, dex)
    return pump or dex


def _bind(coin: dict[str, Any]) -> str:
    symbol = trading_symbol(str(coin.get("symbol") or ""), str(coin["mint"]))
    stored = dict(coin)
    stored["trade_symbol"] = symbol
    with _LOCK:
        _BY_MINT[str(coin["mint"])] = stored
        _BY_SYMBOL[symbol] = stored
    return symbol


def trading_symbol(base: str, mint: str) -> str:
    safe = "".join(ch for ch in (base or "").upper() if ch.isalnum())[:12] or "TOKEN"
    candidate = f"{safe}/SOL"
    try:
        from app.providers import get_provider

        for info in get_provider().list_symbols():
            if (getattr(info, "mint", None) or "") == mint:
                return info.symbol
            if info.symbol == candidate:
                return f"{safe}.{mint[:4]}/SOL"
    except Exception:
        pass
    return candidate


def search_coins(q: str, limit: int = 20) -> dict[str, Any]:
    text = (q or "").strip()
    limit_n = max(1, min(int(limit or 20), 25))
    body: dict[str, Any] = {**_flags(), "q": text, "items": [], "error": None}
    if len(text) < 1:
        return body

    def load() -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        error = None
        try:
            items = _pump_search(text, limit_n)
        except UpstreamError:
            error = "pump.fun 搜索不可用"
        if not items and _MINT_RE.match(text):
            coin = _load_coin(text)
            if coin:
                items = [coin]
                error = None
        if items:
            items = _overlay_dex(items, text, limit_n)
        if not items:
            try:
                items = _dex_search(text, limit_n)
                if items:
                    error = None
            except UpstreamError:
                error = error or "搜索暂时不可用"
        for coin in items:
            _bind(coin)
        return {"items": [_public(coin) for coin in items], "error": None if items else error}

    try:
        loaded = _cached(f"search:{text.lower()}:{limit_n}", SEARCH_TTL, load)
    except Exception:
        loaded = {"items": [], "error": "搜索暂时不可用"}
    body.update(loaded)
    return body


def get_coin(mint: str) -> Optional[dict[str, Any]]:
    text = (mint or "").strip()
    if not text:
        return None
    with _LOCK:
        hit = _BY_MINT.get(text)
    if hit:
        return hit

    def load() -> Optional[dict[str, Any]]:
        return _load_coin(text)

    try:
        coin = _cached(f"coin:{text}", COIN_TTL, load)
    except Exception:
        return None
    if not coin:
        return None
    _bind(coin)
    with _LOCK:
        return _BY_MINT.get(text)


def coin_by_symbol(symbol: str) -> Optional[dict[str, Any]]:
    with _LOCK:
        coin = _BY_SYMBOL.get(symbol)
        return dict(coin) if coin else None


def estimate_impact_bps(symbol: str, notional_sol: float) -> Optional[float]:
    """AMM estimate for a graduated external coin. None when the local curve applies."""
    coin = coin_by_symbol(symbol)
    if not coin or coin.get("has_curve"):
        return None
    return amm_impact_bps(
        notional_sol,
        _num(coin.get("liquidity_usd")),
        _num(coin.get("price_sol")),
        _num(coin.get("price_usd")),
    )


def _public(coin: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mint",
        "name",
        "symbol",
        "image",
        "price_sol",
        "price_usd",
        "market_cap_sol",
        "market_cap_usd",
        "change_24h",
        "volume_24h_usd",
        "progress_pct",
        "graduated",
        "venue",
        "source",
        "liquidity_usd",
    )
    return {key: coin.get(key) for key in keys}


def _parse_candles(payload: Any, symbol: str) -> list[dict[str, Any]]:
    rows = payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        rows = payload.get("candles") or payload.get("data") or payload.get("items") or []
    out: list[dict[str, Any]] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        t = _int(row.get("t") or row.get("timestamp") or row.get("time") or row.get("ts"))
        if t and t < 10_000_000_000:
            t *= 1000
        o = _num(row.get("o") if "o" in row else row.get("open"))
        h = _num(row.get("h") if "h" in row else row.get("high"))
        l = _num(row.get("l") if "l" in row else row.get("low"))
        c = _num(row.get("c") if "c" in row else row.get("close"))
        v = _num(row.get("v") if "v" in row else row.get("volume")) or 0.0
        if not t or o is None or h is None or l is None or c is None:
            continue
        out.append(
            {
                "symbol": symbol,
                "interval": "1m",
                "t": t,
                "o": o,
                "h": h,
                "l": l,
                "c": c,
                "v": v,
            }
        )
    return out


def candles_for(mint: str, price: Optional[float], symbol: str) -> tuple[list[dict[str, Any]], bool]:
    def load() -> list[dict[str, Any]]:
        try:
            payload = get_json(
                f"{pump_frontend_base()}/candles/{quote(mint)}?offset=0&limit=240&timeframe=1"
            )
        except UpstreamError:
            return []
        return _parse_candles(payload, symbol)

    try:
        rows = _cached(f"candles:{mint}", CANDLE_TTL, load)
    except Exception:
        rows = []
    if rows:
        return rows, False
    if price and price > 0:
        now = int(time.time() * 1000)
        return (
            [
                {
                    "symbol": symbol,
                    "interval": "1m",
                    "t": now,
                    "o": price,
                    "h": price,
                    "l": price,
                    "c": price,
                    "v": 0.0,
                }
            ],
            True,
        )
    return [], True


def coin_detail(mint: str) -> Optional[dict[str, Any]]:
    coin = get_coin(mint)
    if not coin:
        return None
    symbol = str(coin.get("trade_symbol") or trading_symbol(str(coin.get("symbol") or ""), mint))
    candles, estimated = candles_for(mint, _num(coin.get("price_sol")), symbol)
    return {
        **_flags(),
        **_public(coin),
        "trade_symbol": symbol,
        "impact_kind": "curve" if coin.get("has_curve") else "estimate",
        "candles": candles,
        "candles_estimated": estimated,
    }


def snapshot_for_symbol(symbol: str) -> Optional[PumpfunPaperSnapshot]:
    coin = coin_by_symbol(symbol)
    if not coin or not coin.get("price_sol"):
        return None
    graduated = not coin.get("has_curve")
    now = int(time.time() * 1000)
    return PumpfunPaperSnapshot(
        mint=str(coin["mint"]),
        symbol=symbol,
        phase="amm" if graduated else "curve",
        progress_bps=10_000 if graduated else int(float(coin.get("progress_pct") or 0) * 100),
        complete=bool(graduated),
        migrated=bool(graduated),
        virtual_sol_reserves=str(coin.get("virtual_sol_reserves") or "0"),
        virtual_token_reserves=str(coin.get("virtual_token_reserves") or "0"),
        real_sol_reserves=str(coin.get("real_sol_reserves") or "0"),
        real_token_reserves=str(coin.get("real_token_reserves") or "0"),
        token_total_supply=str(coin.get("token_total_supply") or "0"),
        price_sol=float(coin["price_sol"]),
        market_cap_sol=_num(coin.get("market_cap_sol")),
        updated_ts=now,
        synthetic=True,
    )
