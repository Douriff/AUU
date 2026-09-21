"""AUU Market Terminal API — FastAPI entrypoint (paper/mock only)."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager, suppress

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.routes import candles, fills, health, paper, pumpfun, risk, signals, strategy, symbols, ws
from app.routes.envelope import API_VERSION
from app.strategies.pump_paper_v1 import get_engine, loop_enabled
from app.discovery import get_discovery, resolve_discovery_mode

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio

    tasks: list[asyncio.Task] = []
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
    description="Paper/mock meme-coin quant visualization backend. No live trading.",
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
app.include_router(symbols.router)
app.include_router(candles.router)
app.include_router(signals.router)
app.include_router(fills.router)
app.include_router(risk.router)
app.include_router(paper.router)
app.include_router(pumpfun.router)
app.include_router(strategy.router)
app.include_router(ws.router)


@app.get("/")
def root():
    return {
        "ok": True,
        "data": {
            "service": "auu-api",
            "docs": "/docs",
            "health": "/api/v1/health",
            "ws": "/api/v1/ws",
            "provider": os.getenv("DATA_PROVIDER", "mock"),
            "orderMode": "paper",
            "venue": "Pump.fun" if os.getenv("DATA_PROVIDER", "mock") == "pumpfun_paper" else "mock",
            "endpoints": {
                "preOrder": "POST /api/v1/risk/pre-order",
                "paperOrders": "POST /api/v1/paper/orders",
                "postFill": "POST /api/v1/risk/post-fill",
                "strategy": "GET/PUT /api/v1/strategy/pump-paper-v1",
                "monitor": "GET /api/v1/pumpfun/monitor",
                "discovery": "env PUMPFUN_DISCOVERY=pumpportal|logs|off",
            },
        },
    }
