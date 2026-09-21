"""GET /api/v1/book?symbol= — mock depth snapshot (additive; paper/mock only)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.providers import get_provider
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1", tags=["book"])


@router.get("/book")
def get_book(symbol: str = Query(...)):
    provider = get_provider()
    snapshot_fn = getattr(provider, "snapshot_book", None)
    if not callable(snapshot_fn):
        return err("NOT_IMPLEMENTED", "book snapshot not available for this provider", 501)
    return ok(snapshot_fn(symbol))
