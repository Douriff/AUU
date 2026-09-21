import os

from fastapi import APIRouter

from app.providers import AVAILABLE_PROVIDERS, default_symbol, get_provider
from app.risk import get_risk_gate
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health")
def health():
    gate = get_risk_gate()
    provider = get_provider()
    venue = "Pump.fun" if provider.name == "pumpfun_paper" else "mock"
    return ok(
        {
            "status": "up",
            "provider": provider.name,
            "mode": "paper",
            "venue": venue,
            "quote": "SOL" if provider.name == "pumpfun_paper" else None,
            "defaultSymbol": default_symbol(),
            "dataSourceOptions": ["mock", "paper", "pumpfun_paper"],
            "marketProviderOptions": list(AVAILABLE_PROVIDERS),
            "trading_state": gate.trading_state,
            "watch_mints": os.getenv("PUMPFUN_WATCH_MINTS", ""),
            "liveDisabled": True,
        }
    )
