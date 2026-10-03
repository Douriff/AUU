"""Read-only mainstream market data: /api/v1/mainstream/*.

GET only. Login is enforced by AuthGateMiddleware like every other /api path.
Data comes from the local SQLite store; requests never call the exchange.
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
):
    """1h/4h/1d come from the stored history; 1m/5m/15m are fetched on demand
    (recent window only, see retentionDays) and cached in the same SQLite file."""
    base = _base(symbol)
    if base is None:
        return err("UNKNOWN_SYMBOL", f"symbol not configured: {symbol}", 404)
    if tf not in CHART_TFS:
        return err("BAD_TIMEFRAME", f"tf must be one of {', '.join(CHART_TFS)}", 400)
    svc = get_service()
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
