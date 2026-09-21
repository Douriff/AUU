import os

from fastapi import APIRouter

from app.discovery import portal_key_configured, resolve_discovery_mode
from app.live.gate import evaluate
from app.providers import AVAILABLE_PROVIDERS, default_symbol, get_provider
from app.risk import get_risk_gate
from app.routes.envelope import ok
from app.strategies.pump_paper_v1 import get_engine

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health")
def health():
    gate = get_risk_gate()
    provider = get_provider()
    venue = "Pump.fun" if provider.name == "pumpfun_paper" else "mock"
    live = evaluate()
    payload = live.as_dict()
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
            "auto_paper_orders": get_engine().params.auto_paper_orders,
            "strategy_autopaper": get_engine().params.auto_paper_orders,
            "strategyId": "pump-paper-v1",
            "watch_mints": os.getenv("PUMPFUN_WATCH_MINTS", ""),
            "discovery": resolve_discovery_mode(),
            "discoveryOptions": ["pumpportal", "logs", "off"],
            "portal_key_configured": portal_key_configured(),
            "liveEnabled": payload["liveEnabled"],
            "liveConfirmed": payload["liveConfirmed"],
            "liveDisabled": payload["liveDisabled"],
            "liveArmed": payload["liveArmed"],
            "liveSendWired": payload["liveSendWired"],
            "liveReasons": payload["reasons"],
            "liveLimits": payload["limits"],
            "keypairConfigured": payload["keypairConfigured"],
            "keypairMounted": payload["keypairMounted"],
            "keypairEnv": payload["keypairEnv"],
        }
    )
