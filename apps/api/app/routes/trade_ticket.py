"""Paper ticket: preview, position, and manual orders. liveEnabled stays false."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.paper.manual_trade import ManualTradeError, position_view, preview_manual, submit_manual
from app.routes.envelope import err, ok

router = APIRouter(prefix="/api/v1", tags=["trade"])


class TradeOrderBody(BaseModel):
    symbol: str = ""
    mint: str = ""
    side: Literal["buy", "sell"] = "buy"
    notional_sol: Optional[float] = None
    sell_pct: Optional[float] = None


def _fail(exc: ManualTradeError):
    status = 404 if exc.code == "UNKNOWN_SYMBOL" else 400
    return err(exc.code, exc.message, status)


@router.get("/trade/preview")
def trade_preview(
    symbol: str = Query(""),
    mint: str = Query(""),
    side: Literal["buy", "sell"] = Query("buy"),
    notional_sol: Optional[float] = Query(None),
    sell_pct: Optional[float] = Query(None),
):
    try:
        return ok(
            preview_manual(
                symbol=symbol,
                mint=mint,
                side=side,
                notional_sol=notional_sol,
                sell_pct=sell_pct,
            )
        )
    except ManualTradeError as exc:
        return _fail(exc)


@router.get("/trade/position")
def trade_position(symbol: str = Query(""), mint: str = Query("")):
    try:
        return ok(position_view(symbol=symbol, mint=mint))
    except ManualTradeError as exc:
        return _fail(exc)


@router.post("/trade/orders")
async def trade_orders(body: TradeOrderBody):
    try:
        data = await submit_manual(
            symbol=body.symbol,
            mint=body.mint,
            side=body.side,
            notional_sol=body.notional_sol,
            sell_pct=body.sell_pct,
        )
    except ManualTradeError as exc:
        return _fail(exc)
    return ok(data)
