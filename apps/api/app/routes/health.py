import os

from fastapi import APIRouter

from app.discovery import discovery_health_fields
from app.live.gate import evaluate
from app.providers import AVAILABLE_PROVIDERS, default_symbol, get_provider, market_data_kind
from app.risk import get_risk_gate
from app.routes.envelope import ok
from app.strategies.pump_paper_v1 import get_engine
from app.traders import COPY_TRADE_ENABLED
from app.traders.helius import helius_enabled, reader_mode

router = APIRouter(prefix="/api/v1", tags=["health"])


@router.get("/health")
def health():
    gate = get_risk_gate()
    provider = get_provider()
    pump_venue = provider.name in {"pumpfun_paper", "pumpfun_live_paper"}
    venue = "Pump.fun" if pump_venue else "mock"
    live = evaluate()
    payload = live.as_dict()
    kind = market_data_kind(provider.name)
    return ok(
        {
            "status": "up",
            "provider": provider.name,
            "mode": "paper",
            "venue": venue,
            "quote": "SOL" if pump_venue else None,
            "marketData": kind,
            "marketDataLabel": {"real": "真实链上", "synthetic": "合成行情", "mock": "模拟"}.get(kind, kind),
            "defaultSymbol": default_symbol(),
            "dataSourceOptions": ["mock", "paper", "pumpfun_paper"],
            "marketProviderOptions": list(AVAILABLE_PROVIDERS),
            "trading_state": gate.trading_state,
            "auto_paper_orders": get_engine().params.auto_paper_orders,
            "strategy_autopaper": get_engine().params.auto_paper_orders,
            "strategyId": "pump-paper-v1",
            "watch_mints": os.getenv("PUMPFUN_WATCH_MINTS", ""),
            **discovery_health_fields(),
            "liveEnabled": payload["liveEnabled"],
            "liveConfirmed": payload["liveConfirmed"],
            "liveDisabled": payload["liveDisabled"],
            "liveArmed": payload["liveArmed"],
            "liveSendWired": payload["liveSendWired"],
            "liveReasons": payload["reasons"],
            "liveLimits": payload["limits"],
            "keypairConfigured": payload["keypairConfigured"],
            "keypairMounted": bool(payload["keypairMounted"]),
            "pubkey": payload.get("pubkey"),
            "keypairRelpath": payload.get("keypairRelpath"),
            "keypairEnv": payload["keypairEnv"],
            "copy_trade_enabled": COPY_TRADE_ENABLED,
            "trader_watch_reader": reader_mode(),
            "helius_enabled": helius_enabled(),
        }
    )
