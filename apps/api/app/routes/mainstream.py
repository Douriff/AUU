"""Read-only mainstream market data: /api/v1/mainstream/*.

GET only. Login is enforced by AuthGateMiddleware like every other /api path.
Data comes from the local SQLite store; requests never call the exchange.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from app.marketdata.mainstream import get_service
from app.marketdata.mainstream.config import TIMEFRAMES
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1/mainstream", tags=["mainstream"])


def _base(symbol: str) -> Optional[str]:
    svc = get_service()
    base = "".join(ch for ch in symbol.split("/")[0].upper() if ch.isalnum())
    return base if base in svc.cfg.symbols else None


@router.get("/status")
def status():
    return ok(get_service().freshness())


@router.get("/overview")
def overview():
    return ok(get_service().overview())


@router.get("/candles")
def candles(
    symbol: str = Query(..., description="BTC | ETH | SOL (configured symbols)"),
    tf: str = Query("1d", description="1d | 1h"),
    limit: int = Query(200, ge=1, le=2000),
    since: Optional[int] = Query(None, description="ms epoch, inclusive"),
):
    base = _base(symbol)
    if base is None:
        return err("UNKNOWN_SYMBOL", f"symbol not configured: {symbol}", 404)
    if tf not in TIMEFRAMES:
        return err("BAD_TIMEFRAME", f"tf must be one of {', '.join(TIMEFRAMES)}", 400)
    svc = get_service()
    ex = svc.exchange_for_read()
    rows = svc.store.candles(ex, base, tf, since=since, limit=limit) if ex else []
    return ok({"exchange": ex, "symbol": base, "pair": svc.cfg.spot(base), "tf": tf, "candles": rows})


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
