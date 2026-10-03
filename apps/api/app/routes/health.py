import os

from fastapi import APIRouter, Request

from app.legacy import legacy_pump_enabled
from app.live.gate import evaluate
from app.risk import get_risk_gate
from app.routes.envelope import ok

router = APIRouter(prefix="/api/v1", tags=["health"])


def _stall(provider):
    """Stall alarm only for a strategy that actually runs (loop on + autopaper on)."""
    from app.legacy.pump.strategies.pump_paper_v1 import get_engine, loop_enabled

    try:
        engine = get_engine()
        status = engine.stall_status(provider)
    except Exception:
        return None
    active = bool(loop_enabled() and engine.params.auto_paper_orders)
    status["active"] = active
    if not active:
        status["stalled"] = False
        status["reason"] = "no_running_strategy"
    return status


def _live_fields() -> dict:
    payload = evaluate().as_dict()
    return {
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
    }


def _mainstream_fields() -> dict:
    try:
        from app.marketdata.mainstream import get_service

        svc = get_service()
        if not svc.cfg.enabled:
            return {"enabled": False, "stale": False}
        return svc.freshness()
    except Exception as exc:  # health must answer even if the store is broken
        return {"enabled": True, "stale": True, "lastError": f"{type(exc).__name__}: {exc}"[:200]}


def _strategy_status() -> dict:
    """M3 runner: stalled when the last rebalance is more than 26 h old."""
    try:
        from app.marketdata.mainstream import get_service
        from app.paper.strategy_runner import not_started_status, peek_runner, runner_enabled

        if not runner_enabled() or not get_service().cfg.enabled:
            return {"active": False, "stalled": False, "reason": "runner_off"}
        r = peek_runner()
        return r.status() if r else not_started_status()
    except Exception as exc:  # health must answer even if the ledger is broken
        return {"active": True, "stalled": True, "reason": f"status error: {type(exc).__name__}: {exc}"[:200]}


def _mainstream_health(gate) -> dict:
    """Health when AUU_LEGACY_PUMP is off: no pump provider is constructed."""
    md = _mainstream_fields()
    st = _strategy_status()
    running = [st["strategy"]] if st.get("active") and st.get("strategy") else []
    return {
        "status": "up",
        "provider": "cex_public",
        "mode": "paper",
        "venue": "CEX",
        "quote": "USDT",
        "marketData": "real",
        "marketDataLabel": "交易所公开行情",
        "legacyPump": False,
        "defaultSymbol": "BTC/USDT",
        "dataSourceOptions": [],
        "marketProviderOptions": ["cex_public"],
        "trading_state": gate.trading_state,
        "auto_paper_orders": False,
        "strategy_autopaper": False,
        "strategyId": running[0] if running else None,
        "runningStrategies": running,
        "liveFeed": None,
        # Paper strategy runner (M3). autopaperStall mirrors it so existing guards alarm too.
        "strategyRunner": st,
        "autopaperStall": {"stalled": bool(st.get("stalled")), "active": bool(st.get("active")),
                           "reason": st.get("reason") or "", "idleMin": st.get("idleMin"),
                           "hints": ["strategy_rebalance_overdue"] if st.get("stalled") else []},
        "mainstream": md,
        **_live_fields(),
        "copy_trade_enabled": False,
    }


@router.get("/health")
def health(request: Request):
    gate = get_risk_gate()
    legacy = getattr(request.app.state, "legacy_pump", None)
    if not (legacy_pump_enabled() if legacy is None else legacy):
        return ok(_mainstream_health(gate))
    from app.legacy.pump.discovery import discovery_health_fields
    from app.legacy.pump.strategies.pump_paper_v1 import get_engine, loop_enabled
    from app.legacy.pump.traders import COPY_TRADE_ENABLED
    from app.legacy.pump.traders.helius import helius_enabled, reader_mode
    from app.providers import AVAILABLE_PROVIDERS, default_symbol, get_provider, market_data_kind

    provider = get_provider()
    pump_venue = provider.name in {"pumpfun_paper", "pumpfun_live_paper"}
    venue = "Pump.fun" if pump_venue else "mock"
    kind = market_data_kind(provider.name)
    return ok(
        {
            "status": "up",
            "legacyPump": True,
            "mainstream": _mainstream_fields(),
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
            "runningStrategies": ["pump-paper-v1"] if (loop_enabled() and get_engine().params.auto_paper_orders) else [],
            "watch_mints": os.getenv("PUMPFUN_WATCH_MINTS", ""),
            **discovery_health_fields(),
            "liveFeed": provider.feed_health() if callable(getattr(provider, "feed_health", None)) else None,
            "autopaperStall": _stall(provider),
            **_live_fields(),
            "copy_trade_enabled": COPY_TRADE_ENABLED,
            "trader_watch_reader": reader_mode(),
            "helius_enabled": helius_enabled(),
        }
    )
