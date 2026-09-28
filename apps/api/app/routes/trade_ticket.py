"""Paper ticket: preview, position, and manual orders. liveEnabled stays false."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from app.paper.manual_trade import ManualTradeError, book_view, position_view, preview_manual, submit_manual
from app.routes.auth import close_book, open_book
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
    request: Request,
    symbol: str = Query(""),
    mint: str = Query(""),
    side: Literal["buy", "sell"] = Query("buy"),
    notional_sol: Optional[float] = Query(None),
    sell_pct: Optional[float] = Query(None),
):
    bound = open_book(request)
    if bound.error is not None:
        return bound.error
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
    finally:
        close_book(bound)


@router.get("/trade/position")
def trade_position(request: Request, symbol: str = Query(""), mint: str = Query("")):
    bound = open_book(request)
    if bound.error is not None:
        return bound.error
    try:
        return ok(position_view(symbol=symbol, mint=mint))
    except ManualTradeError as exc:
        return _fail(exc)
    finally:
        close_book(bound)


@router.get("/trade/book")
def trade_book(request: Request, user_id: str = Query("")):
    bound = open_book(request, user_id=user_id)
    if bound.error is not None:
        return bound.error
    try:
        data = book_view()
        if bound.user is not None:
            data["user"] = {
                "id": bound.user["id"],
                "name": bound.user["name"],
                "is_admin": bool(bound.user.get("is_admin")),
            }
        else:
            data["user"] = None
        return ok(data)
    finally:
        close_book(bound)


@router.post("/trade/orders")
async def trade_orders(request: Request, body: TradeOrderBody):
    bound = open_book(request)
    if bound.error is not None:
        return bound.error
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
    finally:
        close_book(bound)
    return ok(data)
