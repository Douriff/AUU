import os

from fastapi import APIRouter

from app.providers import get_provider
from app.providers.pump_mints import DEFAULT_SYMBOL, VENUE
from app.risk import get_risk_gate
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health")
def health():
    gate = get_risk_gate()
    provider = get_provider()
    return ok(
        {
            "status": "up",
            "provider": os.getenv("DATA_PROVIDER", "mock"),
            "providerName": getattr(provider, "name", "mock"),
            "mode": "paper",
            "venue": getattr(provider, "venue", VENUE),
            "quote": "SOL",
            "defaultSymbol": DEFAULT_SYMBOL,
            "dataSourceOptions": ["mock", "paper", "pumpfun_paper"],
            "trading_state": gate.trading_state,
            "liveDisabled": True,
        }
    )
