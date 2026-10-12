"""AUU proprietary strategy signal (auu_signal_v0) — read + shadow record, never trades.

GET /api/v1/mainstream/signal?symbol=&tf=&limit=
GET /api/v1/mainstream/signal/latest?symbol=&tf=
GET /api/v1/mainstream/signal/shadow?symbol=&tf=&limit=

Login required. Experimental; score ∈ [-1,1]; does not drive live/paper rebalance.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query, Request

from app.routes.envelope import err, ok
from app.routes.mainstream_paper import _uid
from app.strategies.auu_signal_v0 import VERSION

router = APIRouter(prefix="/api/v1/mainstream/signal", tags=["mainstream-signal"])

_ALLOWED_TF = {"1m", "5m", "15m", "1h", "4h", "1d"}


def _need_login(request: Request):
    if _uid(request) is None:
        return err("AUTH_REQUIRED", "请先登录", 401)
    return None


def _base(symbol: str) -> Optional[str]:
    base = "".join(ch for ch in symbol.split("/")[0].upper() if ch.isalnum())
    return base or None


@router.get("")
def signal_series(
    request: Request,
    symbol: str = Query(..., min_length=1, max_length=32),
    tf: str = Query("1d"),
    limit: int = Query(400, ge=30, le=2000),
):
    """Compute auu_signal_v0 series for a symbol (closed bars), persist shadow rows, return overlay."""
    denied = _need_login(request)
    if denied is not None:
        return denied
    if tf not in _ALLOWED_TF:
        return err("BAD_TF", f"tf must be one of {sorted(_ALLOWED_TF)}", 400)
    base = _base(symbol)
    if not base:
        return err("BAD_SYMBOL", "symbol format", 400)

    from app.marketdata.mainstream import get_service
    from app.paper import auu_signal_shadow as sh

    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return err("NO_EXCHANGE", "mainstream exchange not ready", 503)

    # Prefer pool store; fall back to chart_candles for display coins.
    candles = svc.store.candles(ex, base, tf, limit=limit)
    if not candles:
        packed = svc.chart_candles(base, tf, limit=limit)
        candles = packed.get("candles") or []
    fr = svc.store.funding(ex, base, limit=max(limit, 200))
    funding_rows = [(int(r["ts"]), float(r["rate"])) for r in fr]
    payload = sh.compute_and_record(
        base, candles, tf=tf, funding_rows=funding_rows, now_ms=svc.now_ms()
    )
    # Trim series to requested limit (compute used whatever we loaded).
    if len(payload["series"]) > limit:
        payload["series"] = payload["series"][-limit:]
        payload["latest"] = payload["series"][-1] if payload["series"] else None
    return ok(payload)


@router.get("/latest")
def signal_latest(
    request: Request,
    symbol: str = Query(..., min_length=1, max_length=32),
    tf: str = Query("1d"),
):
    denied = _need_login(request)
    if denied is not None:
        return denied
    if tf not in _ALLOWED_TF:
        return err("BAD_TF", f"tf must be one of {sorted(_ALLOWED_TF)}", 400)
    base = _base(symbol)
    if not base:
        return err("BAD_SYMBOL", "symbol format", 400)

    from app.marketdata.mainstream import get_service
    from app.paper import auu_signal_shadow as sh

    # Fresh compute (also shadows) then return latest only.
    svc = get_service()
    ex = svc.exchange_for_read()
    if not ex:
        return err("NO_EXCHANGE", "mainstream exchange not ready", 503)
    candles = svc.store.candles(ex, base, tf, limit=400) or (svc.chart_candles(base, tf, limit=400).get("candles") or [])
    fr = svc.store.funding(ex, base, limit=400)
    funding_rows = [(int(r["ts"]), float(r["rate"])) for r in fr]
    payload = sh.compute_and_record(base, candles, tf=tf, funding_rows=funding_rows, now_ms=svc.now_ms())
    return ok(
        {
            "version": VERSION,
            "experimental": True,
            "symbol": base,
            "tf": tf,
            "latest": payload.get("latest"),
            "label": payload.get("label"),
        }
    )


@router.get("/shadow")
def signal_shadow(
    request: Request,
    symbol: str = Query(..., min_length=1, max_length=32),
    tf: str = Query("1d"),
    limit: int = Query(100, ge=1, le=2000),
):
    """Read previously recorded shadow rows (no recompute)."""
    denied = _need_login(request)
    if denied is not None:
        return denied
    base = _base(symbol)
    if not base:
        return err("BAD_SYMBOL", "symbol format", 400)
    from app.paper.auu_signal_shadow import LABEL, get_store

    st = get_store()
    rows = st.series(base, tf, limit=limit)
    return ok(
        {
            "version": VERSION,
            "experimental": True,
            "label": LABEL,
            "symbol": base,
            "tf": tf,
            "rows": rows,
            "count": st.count(base),
        }
    )
