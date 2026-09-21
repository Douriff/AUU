import os

from fastapi import APIRouter

from app.providers import AVAILABLE_PROVIDERS, get_provider
from app.risk import get_risk_gate
from app.routes.envelope import ok
from app.strategies.pump_paper_v1 import get_engine

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
            "auto_paper_orders": get_engine().params.auto_paper_orders,
            "strategyId": "pump-paper-v1",
            "watch_mints": os.getenv("PUMPFUN_WATCH_MINTS", ""),
        }
    )
