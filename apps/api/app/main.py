"""AUU Market Terminal API — FastAPI entrypoint (paper/mock only)."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager, suppress

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.routes import (
    auth,
    board,
    book,
    candles,
    curve,
    events,
    fills,
    health,
    live,
    majors,
    markets,
    paper,
    pipeline,
    pumpfun,
    risk,
    signals,
    search,
    stats,
    strategy,
    symbols,
    trade_ticket,
    universe,
    wallet,
    watch,
    ws,
)
from app.routes.envelope import API_VERSION
from app.strategies.pump_paper_v1 import get_engine, loop_enabled
from app.discovery import get_discovery, resolve_discovery_mode

# Tests set AUU_SKIP_DOTENV so a developer .env (AUTO_PAPER_ORDERS=true)
# cannot change strategy behavior for the suite. AUU_DOTENV_PATH selects
# a single file; the default search stays the process working directory.
if os.getenv("AUU_SKIP_DOTENV", "").strip().lower() not in {"1", "true", "on", "yes"}:
    dotenv_path = (os.getenv("AUU_DOTENV_PATH") or "").strip()
    if dotenv_path:
        load_dotenv(dotenv_path)
    else:
        load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    tasks: list[asyncio.Task] = []
    try:
        from app.paper.events import note_api_start

        note_api_start()
    except Exception:
        pass
    if loop_enabled():
        tasks.append(asyncio.create_task(get_engine().run_loop(), name="pump-paper-v1-loop"))
    if resolve_discovery_mode() != "off":
        tasks.append(asyncio.create_task(get_discovery().run_loop(), name="pumpfun-discovery"))
    try:
        yield
    finally:
        get_engine().stop()
        get_discovery().stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError):
                await t


app = FastAPI(
    title="AUU Market Terminal API",
    version="0.1.0",
    description="Paper/mock Pump.fun (Solana bonding curve) visualization backend. Live adapter is scaffolded but dark (no chain submit).",
    lifespan=lifespan,
)

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


app.include_router(health.router)
app.include_router(board.router)
app.include_router(markets.router)
app.include_router(universe.router)
app.include_router(events.router)
app.include_router(search.router)
app.include_router(majors.router)
app.include_router(auth.router)
app.include_router(trade_ticket.router)
app.include_router(symbols.router)
app.include_router(candles.router)
app.include_router(signals.router)
app.include_router(fills.router)
app.include_router(book.router)
app.include_router(curve.router)
app.include_router(risk.router)
app.include_router(paper.router)
app.include_router(live.router)
app.include_router(pipeline.router)
app.include_router(pumpfun.router)
app.include_router(strategy.router)
app.include_router(stats.router)
app.include_router(watch.router)
app.include_router(wallet.router)
app.include_router(ws.router)


@app.get("/")
def root():
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
