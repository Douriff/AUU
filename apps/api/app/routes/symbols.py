from fastapi import APIRouter

from app.providers import get_provider
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["symbols"])


@router.get("/symbols")
def list_symbols():
    provider = get_provider()
    data = [s.model_dump() for s in provider.list_symbols()]
    return ok(data)
