from fastapi import APIRouter, Query

from app.providers import get_provider
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1", tags=["candles"])


@router.get("/candles")
def get_candles(
    symbol: str = Query(...),
    interval: str = Query("1m"),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
):
    provider = get_provider()
    symbols = {s.symbol for s in provider.list_symbols()}
    if symbol not in symbols:
        return err("UNKNOWN_SYMBOL", f"symbol not found: {symbol}", 404)
    candles = provider.get_candles(symbol, interval, from_ts, to_ts)
    return ok([c.model_dump() for c in candles])
