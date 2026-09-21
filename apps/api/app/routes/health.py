import os

from fastapi import APIRouter

from app.providers import AVAILABLE_PROVIDERS, get_provider
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
            "provider": provider.name,
            "mode": "paper",
            "venue": "Pump.fun" if provider.name == "pumpfun_paper" else "mock",
            "dataSourceOptions": ["mock", "paper"],
            "marketProviderOptions": list(AVAILABLE_PROVIDERS),
            "trading_state": gate.trading_state,
            "watch_mints": os.getenv("PUMPFUN_WATCH_MINTS", ""),
        }
    )
