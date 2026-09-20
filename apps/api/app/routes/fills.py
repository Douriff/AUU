from fastapi import APIRouter, Query

from app.providers import get_provider
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["fills"])


@router.get("/fills")
def get_fills(
    symbol: str = Query(...),
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
):
    provider = get_provider()
    fills = provider.get_fills(symbol, from_ts, to_ts)
    return ok([f.model_dump() for f in fills])
