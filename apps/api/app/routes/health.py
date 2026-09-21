import os

from fastapi import APIRouter

from app.risk import get_risk_gate
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health")
def health():
    gate = get_risk_gate()
    return ok(
        {
            "status": "up",
            "provider": os.getenv("DATA_PROVIDER", "mock"),
            "mode": "paper",
            "dataSourceOptions": ["mock", "paper"],
            "trading_state": gate.trading_state,
        }
    )
