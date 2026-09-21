"""AUU Market Terminal API — FastAPI entrypoint (paper/mock only)."""
from __future__ import annotations

import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.routes import book, candles, curve, fills, health, paper, pipeline, pumpfun, risk, signals, symbols, ws
from app.routes.envelope import API_VERSION

load_dotenv()

app = FastAPI(
    title="AUU Market Terminal API",
    version="0.1.0",
    description="Paper/mock Pump.fun (Solana bonding curve) visualization backend. No live trading, no keys.",
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
app.include_router(book.router)
app.include_router(curve.router)
app.include_router(risk.router)
app.include_router(paper.router)
app.include_router(pipeline.router)
app.include_router(pumpfun.router)
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
                "postFill": "POST /api/v1/risk/post-fill",
                "decideAndFill": "POST /api/v1/pipeline/decide-and-fill",
                "book": "GET /api/v1/book?symbol=",
                "curve": "GET /api/v1/curve?symbol=",
                "pumpfunSnapshot": "GET /api/v1/pumpfun/snapshot?symbol=",
            },
        },
    }
