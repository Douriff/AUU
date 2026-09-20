from fastapi import APIRouter, Query

from app.providers import get_provider
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["signals"])


@router.get("/signals")
def get_signals(
    symbol: str = Query(...),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
):
    provider = get_provider()
    events = provider.get_signals(symbol, from_ts, to_ts)
    # Flatten to SignalOut time series with anchor fields
    data = []
    for ev in events:
        row = ev.signal.model_dump()
        row["strategyId"] = ev.strategyId
        row["symbol"] = ev.symbol
        row["t"] = ev.t
        data.append(row)
    return ok(data)
