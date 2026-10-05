"""Read-only mainstream market data: /api/v1/mainstream/*.

GET only. Login is enforced by AuthGateMiddleware like every other /api path.
Data comes from the local SQLite store; requests never wait on the exchange (tickers and the
display-only order book refresh small caches in the background).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.marketdata.mainstream import get_service
from app.marketdata.mainstream.config import CHART_TFS
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1/mainstream", tags=["mainstream"])


def _base(symbol: str) -> Optional[str]:
    svc = get_service()
    base = "".join(ch for ch in symbol.split("/")[0].upper() if ch.isalnum())
    return base if base in svc.cfg.all_symbols() else None


def _extra_venue(symbol: str, venue: Optional[str]):
    """None = normal pool path; (venue, base) = on-demand path; a response = rejected.

    The on-demand path is used for coins outside the pool (only if a venue's current top-100 lists
    them) and for pool coins when the user explicitly picked a venue other than the strategy source.
    """
    from app.marketdata.extra import BOARD_VENUES, clean_symbol, get_extra

    svc = get_service()
    base = clean_symbol(symbol)
    if base is None:
        return err("BAD_SYMBOL", "symbol format", 400)
    v = (venue or "").strip().lower() or None
    if v is not None and v not in BOARD_VENUES:
        return err("BAD_VENUE", "venue must be binance/okx/bybit/coinbase", 400)
    in_pool = base in svc.cfg.all_symbols()
    if in_pool and (v is None or v == svc.exchange_for_read() or v not in ("binance", "okx")):
        return None
    got, why = get_extra().resolve(base, v)
    if got is None:
        return err("VIEW_ONLY" if why == "VENUE_NO_DATA" else "UNKNOWN_SYMBOL",
                   "该币只在此交易所前 100，暂无 K 线数据" if why == "VENUE_NO_DATA" else f"不在各交易所成交额前 100：{base}", 404)
    return got, base


@router.get("/coin")
def coin(symbol: str = Query(...), venue: Optional[str] = Query(None)):
    """Header data for a coin opened from the 大盘 board: resolved data venue, ticker row, tradability."""
    from app.marketdata.extra import BOARD_VENUES, clean_symbol, get_extra

    base = clean_symbol(symbol)
    if base is None:
        return err("BAD_SYMBOL", "symbol format", 400)
    v = (venue or "").strip().lower() or None
    if v is not None and v not in BOARD_VENUES:
        return err("BAD_VENUE", "venue must be binance/okx/bybit/coinbase", 400)
    svc = get_service()
    ex = get_extra()
    in_pool = base in svc.cfg.all_symbols()
    data_venue, why = (svc.exchange_for_read(), None) if in_pool and v not in ("binance", "okx") else ex.resolve(base, v)
    row = (ex.board_row(v, base) if v else None) or (ex.board_row(data_venue, base) if data_venue else None) or {}
    tradable = data_venue is not None and ex.allowed_for_paper(base)
    price, price_ts = row.get("last"), None
    if data_venue in ("binance", "okx"):
        bars = ex.window(data_venue, base, "1m")["rows"]  # cached ~15s; same window the chart/paper use
        if bars:
            price, price_ts = bars[-1]["close"], bars[-1]["ts"]
    return ok({
        "symbol": base,
        "pair": f"{base}/USDT",
        "venue": v,
        "dataVenue": data_venue,
        "inPool": in_pool,
        "tradable": tradable,
        "viewOnly": not tradable,
        "reason": None if tradable else ("该币只在此交易所成交额前 100，暂无 K 线和纸面交易" if why == "VENUE_NO_DATA" else "不在可交易列表"),
        # stable id for the web UI's translated text (reason above stays for older clients)
        "reasonCode": None if tradable else ("VENUE_NO_DATA" if why == "VENUE_NO_DATA" else "NOT_LISTED"),
        "price": price,
        "priceTs": price_ts,
        "change24h": row.get("change_24h"),
        "quoteVolume24h": row.get("volume_24h"),
        "bid": row.get("bid"),
        "ask": row.get("ask"),
    })


@router.get("/status")
def status():
    return ok(get_service().freshness())


@router.get("/overview")
def overview():
    return ok(get_service().overview())


@router.get("/recon")
def recon():
    """Cross-source daily-close reconciliation (read-only; never switches the strategy's source)."""
    from app.marketdata.mainstream.recon import get_reconciler

    return ok(get_reconciler().summary())


@router.get("/orderbook")
def orderbook(symbol: str = Query(...), depth: int = Query(12, ge=1, le=20), venue: Optional[str] = Query(None)):
    """Spot order book snapshot for display only (paper fills use the market price ± simulated slippage).

    Served from a short per-coin cache; a stale cache triggers at most one background refresh.
    """
    from app.marketdata.mainstream.orderbook import get_book_cache

    ext = _extra_venue(symbol, venue)
    if isinstance(ext, tuple):
        return ok(get_book_cache().get(ext[1], depth, ex=ext[0]))
    if ext is not None:
        return ext
    base = _base(symbol)
    if base is None:
        return err("UNKNOWN_SYMBOL", f"unknown symbol {symbol}", 404)
    return ok(get_book_cache().get(base, depth))


@router.get("/markets")
def markets():
    """Market list: every display + strategy coin in one batched response (store + cached tickers)."""
    return ok(get_service().markets(holdings=_strategy_holdings()))


def _strategy_holdings() -> dict[str, float]:
    """Current strategy paper weights (read-only; empty when the runner has not started)."""
    try:
        import json

        from app.paper.strategy_runner import peek_runner

        r = peek_runner()
        last = r.ledger.last_run() if r is not None else None
        if last is None:
            return {}
        return {k: float(v) for k, v in json.loads(last["weights"]).items() if v}
    except Exception:
        return {}


@router.get("/candles")
def candles(
    symbol: str = Query(..., description="any configured display or strategy-universe symbol"),
    tf: str = Query("1d", description="1m | 5m | 15m | 1h | 4h | 1d"),
    limit: int = Query(200, ge=1, le=2000),
    since: Optional[int] = Query(None, description="ms epoch, inclusive (stored timeframes only)"),
    before: Optional[int] = Query(None, description="ms epoch, exclusive: page back in history"),
    venue: Optional[str] = Query(None, description="大盘 venue tab (binance/okx/bybit/coinbase); omit = strategy source"),
):
    """1h/4h/1d come from the stored history; 1m/5m/15m are fetched on demand
    (recent window only, see retentionDays) and cached in the same SQLite file."""
    if tf not in CHART_TFS:
        return err("BAD_TIMEFRAME", f"tf must be one of {', '.join(CHART_TFS)}", 400)
    svc = get_service()
    ext = _extra_venue(symbol, venue)
    if isinstance(ext, tuple):  # 大盘 coin (or a pool coin viewed on another venue): on-demand window
        from app.marketdata.extra import get_extra

        return ok(get_extra().candles(ext[0], ext[1], tf, limit=limit, before=before))
    if ext is not None:
        return ext
    base = _base(symbol)
    if base is None:
        return err("UNKNOWN_SYMBOL", f"symbol not configured: {symbol}", 404)
    if since is not None and before is None:
        ex = svc.exchange_for_read()
        rows = svc.store.candles(ex, base, tf, since=since, limit=limit) if ex else []
        return ok({"exchange": ex, "symbol": base, "pair": svc.cfg.spot(base), "tf": tf, "candles": rows})
    return ok(svc.chart_candles(base, tf, limit=limit, before=before))


@router.get("/funding")
def funding(
    symbol: str = Query(...),
    limit: int = Query(90, ge=1, le=2000),
    since: Optional[int] = Query(None),
):
    base = _base(symbol)
    if base is None:
        return err("UNKNOWN_SYMBOL", f"symbol not configured: {symbol}", 404)
    svc = get_service()
    ex = svc.exchange_for_read()
    rows = svc.store.funding(ex, base, since=since, limit=limit) if ex else []
    return ok({"exchange": ex, "symbol": base, "perp": svc.cfg.perp(base), "funding": rows})
