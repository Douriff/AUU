"""AUU API — FastAPI entrypoint (paper only; mainstream CEX data, legacy pump behind a flag)."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager, suppress

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.auth.gate import AuthGateMiddleware
from app.legacy import legacy_pump_enabled
from app.routes import auth, events, health, live, mainstream, mainstream_paper, mainstream_shadow, mainstream_strategy, majors, stats, status, ws
from app.routes.envelope import API_VERSION

# Tests set AUU_SKIP_DOTENV so a developer .env (AUTO_PAPER_ORDERS=true)
# cannot change strategy behavior for the suite. AUU_DOTENV_PATH selects
# a single file; the default search stays the process working directory.
if os.getenv("AUU_SKIP_DOTENV", "").strip().lower() not in {"1", "true", "on", "yes"}:
    dotenv_path = (os.getenv("AUU_DOTENV_PATH") or "").strip()
    if dotenv_path:
        load_dotenv(dotenv_path)
    else:
        load_dotenv()


def _legacy_routers():
    """pump.fun / Solana routes (app.legacy.pump + provider-backed paper routes)."""
    from app.legacy.pump.routes import (
        board,
        curve,
        markets,
        pumpfun,
        search,
        stats_pump,
        strategy,
        trade_ticket,
        universe,
        wallet,
        watch,
    )
    from app.routes import book, candles, fills, paper, pipeline, risk, signals, symbols

    return [
        board, markets, universe, search, trade_ticket, symbols, candles, signals, fills, book,
        curve, risk, paper, pipeline, pumpfun, strategy, stats_pump, watch, wallet,
    ]


def _make_lifespan(legacy: bool):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio

        tasks: list[asyncio.Task] = []
        try:
            from app.paper.events import note_api_start

            note_api_start()
        except Exception:
            pass
        stops = []
        if legacy:
            from app.legacy.pump.discovery import get_discovery, resolve_discovery_mode
            from app.legacy.pump.strategies.pump_paper_v1 import get_engine, loop_enabled
            from app.providers import get_provider

            if loop_enabled():
                tasks.append(asyncio.create_task(get_engine().run_loop(), name="pump-paper-v1-loop"))
            if resolve_discovery_mode() != "off":
                tasks.append(asyncio.create_task(get_discovery().run_loop(), name="pumpfun-discovery"))
            feed = getattr(get_provider(), "run_feed", None)
            if callable(feed):
                tasks.append(asyncio.create_task(feed(), name="pumpfun-live-paper-feed"))
            stops += [lambda: get_engine().stop(), lambda: get_discovery().stop()]
            stops.append(lambda: (getattr(get_provider(), "stop_feed", None) or (lambda: None))())
        from app.marketdata.mainstream import get_service

        svc = get_service()
        if svc.cfg.enabled and svc.cfg.refresh:
            tasks.append(asyncio.create_task(svc.run_loop(), name="mainstream-refresh"))
            stops.append(svc.stop)
        if not legacy and svc.cfg.enabled:
            from app.paper.strategy_runner import run_loop as strategy_loop, runner_enabled

            if runner_enabled():
                tasks.append(asyncio.create_task(strategy_loop(), name="mainstream-strategy"))
            from app.paper.shadow_s3 import run_loop as shadow_loop, shadow_enabled

            if shadow_enabled():
                tasks.append(asyncio.create_task(shadow_loop(), name="shadow-s3"))
            from app.alerts import alerts_enabled, run_loop as alerts_loop

            if alerts_enabled():
                tasks.append(asyncio.create_task(alerts_loop(), name="alerts"))
            from app.marketdata.mainstream.recon import recon_enabled, run_loop as recon_loop

            if recon_enabled():
                tasks.append(asyncio.create_task(recon_loop(), name="recon"))
            from app.status import run_loop as uptime_loop, uptime_enabled

            if uptime_enabled():
                tasks.append(asyncio.create_task(uptime_loop(), name="uptime"))
        try:
            yield
        finally:
            for stop in stops:
                with suppress(Exception):
                    stop()
            for t in tasks:
                t.cancel()
            for t in tasks:
                with suppress(asyncio.CancelledError):
                    await t

    return lifespan


def create_app(legacy: bool | None = None) -> FastAPI:
    """Build the API. ``legacy`` defaults to ``AUU_LEGACY_PUMP`` (off)."""
    legacy = legacy_pump_enabled() if legacy is None else bool(legacy)
    app = FastAPI(
        title="AUU Market Terminal API",
        version="0.2.0",
        description=(
            "Mainstream-coin quant platform (paper only): public CEX market data, paper ledger, "
            "Go/No-Go. Live trading stays locked. Legacy pump.fun stack behind AUU_LEGACY_PUMP."
        ),
        lifespan=_make_lifespan(legacy),
        # AUU_API_DOCS=off hides /docs, /redoc and /openapi.json (public deployments).
        **(
            {"docs_url": None, "redoc_url": None, "openapi_url": None}
            if os.getenv("AUU_API_DOCS", "on").strip().lower() in {"0", "false", "off", "no"}
            else {}
        ),
    )
    app.state.legacy_pump = legacy

    # Login gate first so CORS (added after, so outermost) still decorates 401/403s.
    app.add_middleware(AuthGateMiddleware)

    origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Api-Version"],
    )

    @app.middleware("http")
    async def api_version_header(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Api-Version"] = API_VERSION
        return response

    for mod in (health, status, events, majors, auth, live, stats, mainstream, mainstream_paper, mainstream_strategy, mainstream_shadow):
        app.include_router(mod.router)
    if legacy:
        for mod in _legacy_routers():
            app.include_router(mod.router)
    app.include_router(ws.router)
    app.add_api_route("/", lambda: root(legacy), methods=["GET"])
    return app


def root(legacy: bool = False):
    if not legacy:
        return {
            "ok": True,
            "data": {
                "service": "auu-api",
                "mode": "mainstream",
                "health": "/api/v1/health",
                "status": "/api/v1/status",
                "ws": "/api/v1/ws",
                "provider": "cex_public",
                "orderMode": "paper",
                "legacyPump": False,
                "endpoints": {
                    "mainstreamOverview": "GET /api/v1/mainstream/overview",
                    "mainstreamCandles": "GET /api/v1/mainstream/candles?symbol=&tf=1m|5m|15m|1h|4h|1d&before=",
                    "mainstreamFunding": "GET /api/v1/mainstream/funding?symbol=",
                    "mainstreamStatus": "GET /api/v1/mainstream/status",
                    "paperAccount": "GET /api/v1/mainstream/paper/account?symbol=",
                    "paperOrder": "POST /api/v1/mainstream/paper/orders (paper only; login required)",
                    "strategy": "GET /api/v1/mainstream/strategy (M3 daily paper runner: curve, positions, Go/No-Go, risk caps)",
                    "shadowS3": "GET /api/v1/mainstream/shadow/s3 (shadow hypothesis, no capital, not evidence)",
                    "majors": "GET /api/v1/majors",
                    "paperPerformance": "GET /api/v1/stats/paper-performance",
                    "events": "GET /api/v1/events",
                    "liveStatus": "GET /api/v1/live/status",
                    "auth": "POST /api/v1/auth/register|login|logout|password",
                    "leaderboard": "GET /api/v1/leaderboard",
                    "legacy": "env AUU_LEGACY_PUMP=on re-enables the pump.fun stack",
                },
            },
        }
    provider = os.getenv("DATA_PROVIDER", "mock")
    return {
        "ok": True,
        "data": {
            "service": "auu-api",
            "docs": "/docs",
            "health": "/api/v1/health",
            "ws": "/api/v1/ws",
            "provider": provider,
            "orderMode": "paper",
            "venue": "Pump.fun" if provider == "pumpfun_paper" else "mock",
            "endpoints": {
                "preOrder": "POST /api/v1/risk/pre-order",
                "paperOrders": "POST /api/v1/paper/orders",
                "liveStatus": "GET /api/v1/live/status",
                "liveOrders": "POST /api/v1/live/orders (403 unless armed; no chain submit)",
                "postFill": "POST /api/v1/risk/post-fill",
                "decideAndFill": "POST /api/v1/pipeline/decide-and-fill",
                "book": "GET /api/v1/book?symbol=",
                "curve": "GET /api/v1/curve?symbol=",
                "pumpfunSnapshot": "GET /api/v1/pumpfun/snapshot?symbol=",
                "strategy": "GET/PUT /api/v1/strategy/pump-paper-v1",
                "strategyStats": "GET /api/v1/strategy/pump-paper-v1/stats",
                "strategyPostmortem": "GET /api/v1/strategy/pump-paper-v1/postmortem",
                "watchTraders": "GET/PUT/DELETE /api/v1/watch/traders",
                "applyDistill": "POST /api/v1/strategy/pump-paper-v1/apply-distill",
                "board": "GET /api/v1/board",
                "markets": "GET /api/v1/markets",
                "universe": "GET /api/v1/universe?tab=",
                "search": "GET /api/v1/search?q=",
                "searchCoin": "GET /api/v1/search/coin?mint=",
                "majors": "GET /api/v1/majors",
                "majorsTickers": "GET /api/v1/majors/tickers?venue=",
                "majorsCompare": "GET /api/v1/majors/compare?base=",
                "events": "GET /api/v1/events",
                "tradePreview": "GET /api/v1/trade/preview",
                "tradePosition": "GET /api/v1/trade/position",
                "tradeOrders": "POST /api/v1/trade/orders",
                "tradeBook": "GET /api/v1/trade/book",
                "auth": "POST /api/v1/auth/register|login|logout|password",
                "leaderboard": "GET /api/v1/leaderboard",
                "wallet": "GET /api/v1/wallet/status (pubkey only; mainnet orders stay unsigned until the user signs)",
                "paperPerformance": "GET /api/v1/stats/paper-performance",
                "executability": "GET /api/v1/stats/executability",
                "postmortem": "GET /api/v1/stats/postmortem",
                "execReport": "GET /api/v1/stats/exec-report",
                "decisionLog": "GET /api/v1/strategy/pump-paper-v1/decision-log",
                "monitor": "GET /api/v1/pumpfun/monitor",
                "discovery": "env PUMPFUN_DISCOVERY=pumpportal|logs|off",
            },
        },
    }


app = create_app()
