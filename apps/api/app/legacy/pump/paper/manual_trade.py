"""Manual paper ticket. Buys go through run_pre_order then run_paper_order.

Hard caps (1 SOL, 10 open names, 4.5% day-loss) apply only on this path.
They are not the autopaper RiskGate limits and they do not arm live trading.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from app.models.contracts import (
    AccountCtx,
    LiquidityCtx,
    OrderIntent,
    RiskOut,
    SignalOut,
    SizeIn,
    StrategyContext,
    TickCtx,
)
from app.paper.books import current_book, user_day_pnl, using_user_book
from app.paper.broker import get_paper_broker
from app.paper.ledger import _ids_from_tag
from app.paper.pipeline import run_paper_order, run_pre_order
from app.legacy.pump.providers.pumpfun_curve_math import DEFAULT_PROTOCOL_FEE_BPS
from app.risk import get_risk_gate
from app.legacy.pump.strategies.pump_paper_v1 import curve_impact_bps

MAX_NOTIONAL_SOL = 1.0
MAX_OPEN_POSITIONS = 10
MAX_DAY_LOSS_PCT = 0.045
MANUAL_TAG = "paper:manual:source=manual"
BROKER_FEE_BPS = 15.0
ADV_USD = 100_000.0
# Broker formula slippage stays under this. RiskGate still denies impact > 150.
SLIP_CAP_BPS = 5_000.0
EQUITY_DEFAULT = 10_000.0
SELL_PCTS = (25, 50, 100)

_LIMIT_MESSAGES = {
    "MAX_NOTIONAL": "单笔上限 1 SOL",
    "MAX_OPEN_MINTS": "持仓上限 10",
    "DAY_LOSS_BREAKER": "日亏已达 4.5%，买入已停止",
    "NO_POSITION": "没有可卖的纸面仓位",
    "UNKNOWN_SYMBOL": "找不到这个代币",
    "BAD_SELL_PCT": "卖出比例只能是 25、50 或 100",
    "BAD_NOTIONAL": "买入金额必须大于 0",
}


class ManualTradeError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message or _LIMIT_MESSAGES.get(code, code)
        super().__init__(self.message)


def envelope_flags() -> dict[str, Any]:
    return {"mode": "paper", "liveEnabled": False, "liveDisabled": True, "source": "manual"}


def _equity() -> float:
    gate = get_risk_gate()
    raw = float(getattr(gate, "_equity_at_day_start", EQUITY_DEFAULT) or EQUITY_DEFAULT)
    return max(raw, 1e-9)


def day_loss_tripped() -> bool:
    if using_user_book():
        book = current_book()
        base = max(float(book.equity_0 or EQUITY_DEFAULT), 1e-9)
        return user_day_pnl(book) / base <= -MAX_DAY_LOSS_PCT
    gate = get_risk_gate()
    if str(gate.trading_state) == "halted":
        return True
    return float(gate.day_pnl) / _equity() <= -MAX_DAY_LOSS_PCT


def open_symbols() -> set[str]:
    symbols: set[str] = set()
    journal = current_book()
    for symbol, lots in journal.lots.items():
        if any(abs(float(lot.qty)) > 1e-12 for lot in lots):
            symbols.add(symbol)
    if using_user_book():
        return symbols
    try:
        from app.legacy.pump.strategies.pump_paper_v1 import get_engine

        for pos in get_engine().positions.values():
            if abs(float(pos.qty)) > 1e-12:
                symbols.add(pos.symbol)
    except Exception:
        pass
    return symbols


def position_qty(symbol: str) -> float:
    lots = current_book().lots.get(symbol) or []
    return sum(float(lot.qty) for lot in lots)


def limits_snapshot(symbol: str = "") -> dict[str, Any]:
    held = open_symbols()
    return {
        "max_notional_sol": MAX_NOTIONAL_SOL,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "max_day_loss_pct": MAX_DAY_LOSS_PCT,
        "open_positions": len(held),
        "held": bool(symbol) and symbol in held,
        "day_loss_tripped": day_loss_tripped(),
        "day_pnl": user_day_pnl(current_book()) if using_user_book() else float(get_risk_gate().day_pnl),
    }


def _local_symbol(symbol: str = "", mint: str = "") -> Optional[str]:
    from app.providers import get_provider

    infos = get_provider().list_symbols()
    raw = (symbol or "").strip()
    mint_s = (mint or "").strip()
    if mint_s:
        for info in infos:
            if (getattr(info, "mint", None) or "") == mint_s:
                return info.symbol
    if raw:
        for info in infos:
            if info.symbol == raw or info.base == raw or info.symbol.upper() == raw.upper():
                return info.symbol
            if (getattr(info, "mint", None) or "") == raw:
                return info.symbol
    return None


def _looks_like_mint(value: str) -> bool:
    text = (value or "").strip()
    return "/" not in text and len(text) >= 32


def resolve_symbol(symbol: str = "", mint: str = "") -> Optional[str]:
    local = _local_symbol(symbol, mint)
    if local:
        return local
    from app.legacy.pump.marketdata.pump_search import coin_by_symbol

    raw = (symbol or "").strip()
    if raw and coin_by_symbol(raw):
        return raw
    candidate = (mint or "").strip()
    if not candidate and _looks_like_mint(symbol):
        candidate = symbol.strip()
    if not candidate:
        return None
    from app.legacy.pump.marketdata.pump_search import get_coin

    coin = get_coin(candidate)
    if not coin:
        return None
    return str(coin.get("trade_symbol") or "") or None


def _uses_estimate(symbol: str) -> bool:
    from app.legacy.pump.marketdata.pump_search import coin_by_symbol

    coin = coin_by_symbol(symbol)
    return bool(coin) and not coin.get("has_curve")


def _snapshot(symbol: str):
    from app.providers import get_provider

    provider = get_provider()
    snap = provider.get_pumpfun_snapshot(symbol) if hasattr(provider, "get_pumpfun_snapshot") else None
    if snap is not None:
        return snap
    from app.legacy.pump.marketdata.pump_search import snapshot_for_symbol

    external = snapshot_for_symbol(symbol)
    if external is None:
        raise ManualTradeError("UNKNOWN_SYMBOL")
    return external


def _spread_bps(snap: Any) -> float:
    progress = float(getattr(snap, "progress_bps", 0) or 0)
    spread = 20.0 + (progress / 10_000.0) * 50.0
    if bool(getattr(snap, "complete", False)) and not bool(getattr(snap, "migrated", False)):
        spread += 25.0
    return spread


def _broker_slip(notional: float) -> float:
    return get_paper_broker()._slippage_bps(abs(notional), ADV_USD)


def _expected_px(mid: float, notional: float, side: str) -> tuple[float, float]:
    slip = _broker_slip(notional)
    px = mid * (1 + slip / 1e4) if side == "buy" else mid * (1 - slip / 1e4)
    return max(px, 1e-18), slip


def notional_for_qty(mid: float, qty: float, side: str) -> float:
    """Notional whose broker fill qty matches ``qty`` (no book walk)."""
    target = abs(float(qty))
    n = target * max(mid, 1e-18)
    for _ in range(8):
        px, _slip = _expected_px(mid, n, side)
        n = target * px
    return n


def _build_ctx(symbol: str, snap: Any, pos_qty: float) -> StrategyContext:
    now_ms = int(time.time() * 1000)
    gate = get_risk_gate()
    # Graduated external quotes have no curve reserves. A deep ADV keeps the
    # existing gate from inventing a second impact number; the ticket shows
    # the AMM estimate instead.
    if _uses_estimate(symbol):
        liquidity = LiquidityCtx(spread_bps=20.0, adv_usd=1_000_000_000_000.0)
        pump = None
    else:
        liquidity = LiquidityCtx(
            spread_bps=_spread_bps(snap),
            adv_usd=ADV_USD,
            virtual_sol_reserves=snap.virtual_sol_reserves,
            virtual_token_reserves=snap.virtual_token_reserves,
            real_sol_reserves=snap.real_sol_reserves,
            real_token_reserves=snap.real_token_reserves,
            creator_fee_bps=int(getattr(snap, "creator_fee_bps", 0) or 0),
        )
        pump = snap.to_pump_ctx()
    return StrategyContext(
        symbol=symbol,
        ts=now_ms,
        account=AccountCtx(equity=_equity(), day_pnl=float(gate.day_pnl)),
        liquidity=liquidity,
        position=float(pos_qty),
        tick=TickCtx(mid=max(float(snap.price_sol), 1e-18)),
        pump=pump,
        meta={"mint": snap.mint, "source": "manual"},
    )


def _impact_effective(snap: Any, notional: float, side: str) -> tuple[float, float]:
    raw = float(curve_impact_bps(snap, abs(notional), side))
    pump = snap.to_pump_ctx()
    near = pump.curve_progress_bps >= 9500 or pump.complete or pump.migrated
    return raw, raw * 1.5 if near else raw


def _fee_fields(snap: Any, notional: float, *, estimate: bool = False) -> dict[str, float]:
    if estimate:
        proto = 25
        creator = 0
    else:
        creator = int(getattr(snap, "creator_fee_bps", 0) or 0)
        proto = int(DEFAULT_PROTOCOL_FEE_BPS)
    fee_bps = float(proto + creator + BROKER_FEE_BPS)
    return {
        "protocol_fee_bps": float(proto),
        "creator_fee_bps": float(creator),
        "broker_fee_bps": BROKER_FEE_BPS,
        "fee_bps": fee_bps,
        "fee_sol": abs(notional) * fee_bps / 1e4,
    }


def _block_for(symbol: str, side: str, notional: float) -> Optional[tuple[str, str]]:
    if side == "buy" and notional > MAX_NOTIONAL_SOL + 1e-9:
        return "MAX_NOTIONAL", _LIMIT_MESSAGES["MAX_NOTIONAL"]
    if side == "buy":
        held = open_symbols()
        if symbol not in held and len(held) >= MAX_OPEN_POSITIONS:
            return "MAX_OPEN_MINTS", _LIMIT_MESSAGES["MAX_OPEN_MINTS"]
        if day_loss_tripped():
            return "DAY_LOSS_BREAKER", _LIMIT_MESSAGES["DAY_LOSS_BREAKER"]
    if side == "sell" and position_qty(symbol) <= 1e-12:
        return "NO_POSITION", _LIMIT_MESSAGES["NO_POSITION"]
    return None


def resolve_notional(
    symbol: str,
    side: str,
    *,
    notional_sol: Optional[float] = None,
    sell_pct: Optional[float] = None,
    snap: Any = None,
) -> float:
    if side == "sell":
        if sell_pct is None:
            raise ManualTradeError("BAD_SELL_PCT")
        pct = int(round(float(sell_pct)))
        if pct not in SELL_PCTS:
            raise ManualTradeError("BAD_SELL_PCT")
        qty = position_qty(symbol)
        if qty <= 1e-12:
            raise ManualTradeError("NO_POSITION")
        mid = float(snap.price_sol) if snap is not None else 0.0
        if mid <= 0:
            raise ManualTradeError("UNKNOWN_SYMBOL")
        return notional_for_qty(mid, qty * (pct / 100.0), "sell")
    if notional_sol is None or float(notional_sol) <= 0:
        raise ManualTradeError("BAD_NOTIONAL")
    return float(notional_sol)


def preview_manual(
    *,
    symbol: str = "",
    mint: str = "",
    side: str = "buy",
    notional_sol: Optional[float] = None,
    sell_pct: Optional[float] = None,
) -> dict[str, Any]:
    resolved = resolve_symbol(symbol, mint)
    if resolved is None:
        raise ManualTradeError("UNKNOWN_SYMBOL")
    side_n = "sell" if side == "sell" else "buy"
    snap = _snapshot(resolved)
    try:
        notional = resolve_notional(
            resolved, side_n, notional_sol=notional_sol, sell_pct=sell_pct, snap=snap
        )
    except ManualTradeError as exc:
        if exc.code not in {"NO_POSITION", "BAD_SELL_PCT", "BAD_NOTIONAL"}:
            raise
        notional = float(notional_sol or 0.0)
        blocked = (exc.code, exc.message)
    else:
        blocked = _block_for(resolved, side_n, notional)
    mid = max(float(snap.price_sol), 1e-18)
    use_n = notional if notional > 0 else 0.1
    from app.legacy.pump.marketdata.pump_search import estimate_impact_bps

    estimate = estimate_impact_bps(resolved, use_n)
    if estimate is None:
        raw, effective = _impact_effective(snap, use_n, side_n)
        impact_kind = "curve"
    else:
        raw, effective = estimate, estimate
        impact_kind = "estimate"
    impact_word = "估算冲击" if impact_kind == "estimate" else "冲击"
    if blocked is None and side_n == "buy" and effective > 150.0:
        blocked = ("SLIPPAGE_CAP", f"{impact_word} {effective:.0f} bps，超过 150")
    px, slip = _expected_px(mid, use_n, side_n)
    body = {
        **envelope_flags(),
        "symbol": resolved,
        "mint": snap.mint,
        "base": resolved.split("/")[0],
        "side": side_n,
        "notional_sol": use_n if notional > 0 else notional,
        "price_sol": mid,
        "impact_bps": effective,
        "curve_impact_bps": raw,
        "impact_kind": impact_kind,
        "impact_label": "估算" if impact_kind == "estimate" else "曲线",
        "slippage_bps": slip,
        "expected_price": px,
        "expected_qty": (use_n / px) if notional > 0 else 0.0,
        "blocked": blocked is not None,
        "block_code": blocked[0] if blocked else None,
        "block_message": blocked[1] if blocked else "",
        "limits": limits_snapshot(resolved),
    }
    body.update(_fee_fields(snap, use_n if notional > 0 else 0.0, estimate=impact_kind == "estimate"))
    return body


def position_view(*, symbol: str = "", mint: str = "") -> dict[str, Any]:
    resolved = resolve_symbol(symbol, mint)
    if resolved is None:
        raise ManualTradeError("UNKNOWN_SYMBOL")
    snap = _snapshot(resolved)
    journal = current_book()
    lots = [lot for lot in (journal.lots.get(resolved) or []) if abs(float(lot.qty)) > 1e-12]
    qty = sum(float(lot.qty) for lot in lots)
    notion = sum(abs(float(lot.qty)) * float(lot.price) for lot in lots)
    entry = (notion / abs(qty)) if abs(qty) > 1e-12 else None
    mark = float(snap.price_sol)
    upnl = ((mark - float(entry)) * qty) if entry is not None else 0.0
    recent: list[dict[str, Any]] = []
    for row in journal.fills:
        if str(row.get("symbol") or "") != resolved:
            continue
        tag = str(row.get("tag") or "")
        _sid, source = _ids_from_tag(tag)
        q = float(row.get("qty") or 0.0)
        recent.append(
            {
                "ts": int(row.get("ts") or 0),
                "side": "buy" if q > 0 else "sell",
                "price": float(row.get("price") or 0.0),
                "qty": abs(q),
                "fee": float(row.get("fee") or 0.0),
                "source": source,
                "tag": tag,
            }
        )
    return {
        **envelope_flags(),
        "symbol": resolved,
        "mint": snap.mint,
        "base": resolved.split("/")[0],
        "qty": qty,
        "entry_price": entry,
        "mark": mark,
        "notional_sol": abs(qty) * mark if abs(qty) > 1e-12 else 0.0,
        "upnl": upnl,
        "fills": recent[-20:],
        "limits": limits_snapshot(resolved),
    }


def book_view() -> dict[str, Any]:
    """Open lots on the active book. Marks use the local snapshot when one exists."""
    journal = current_book()
    items: list[dict[str, Any]] = []
    for symbol, lots in journal.lots.items():
        for lot in lots:
            qty = float(lot.qty)
            if abs(qty) <= 1e-12:
                continue
            mark = float(lot.price)
            try:
                mark = float(_snapshot(symbol).price_sol)
            except Exception:
                pass
            items.append(
                {
                    "symbol": symbol,
                    "mint": getattr(lot, "mint", None) or "",
                    "qty": qty,
                    "entry_price": float(lot.price),
                    "mark": mark,
                    "notional_sol": abs(qty) * mark,
                    "upnl": (mark - float(lot.price)) * qty,
                    "ts": int(lot.ts),
                }
            )
    items.sort(key=lambda row: int(row["ts"]), reverse=True)
    return {
        **envelope_flags(),
        "items": items,
        "n_closed": len(journal.closed),
        "limits": limits_snapshot(),
    }


async def submit_manual(
    *,
    symbol: str = "",
    mint: str = "",
    side: str = "buy",
    notional_sol: Optional[float] = None,
    sell_pct: Optional[float] = None,
) -> dict[str, Any]:
    resolved = resolve_symbol(symbol, mint)
    if resolved is None:
        raise ManualTradeError("UNKNOWN_SYMBOL")
    side_n = "sell" if side == "sell" else "buy"
    snap = _snapshot(resolved)
    notional = resolve_notional(
        resolved, side_n, notional_sol=notional_sol, sell_pct=sell_pct, snap=snap
    )
    blocked = _block_for(resolved, side_n, notional)
    if blocked is not None:
        raise ManualTradeError(blocked[0], blocked[1])

    from app.legacy.pump.marketdata.pump_search import estimate_impact_bps

    estimate = estimate_impact_bps(resolved, notional)
    if side_n == "buy" and estimate is not None and estimate > 150.0:
        view = position_view(symbol=resolved)
        view.update(
            {
                "submitted": [],
                "reject": {
                    "tags": ["SLIPPAGE_CAP"],
                    "notes": f"估算冲击 {estimate:.0f} bps，超过 150",
                },
                "side": side_n,
                "notional_sol": abs(notional),
                "impact_kind": "estimate",
            }
        )
        return view

    pos = position_qty(resolved)
    ctx = _build_ctx(resolved, snap, pos)
    signal = SignalOut(
        side="long" if side_n == "buy" else "short",
        strength=1.0,
        reason="manual",
        tags=["source=manual"],
    )
    size = SizeIn(target_notional=abs(notional), max_slippage_bps=150.0)
    user_book = using_user_book()
    if user_book:
        risk = RiskOut(allow=True, clipped_size=abs(notional), tags=["source=manual"], notes="user paper")
    else:
        risk = await run_pre_order(ctx, signal, size, strategy_id="manual-paper")
    reducing = side_n == "sell" and pos > 1e-12
    if not risk.allow and reducing:
        risk = RiskOut(
            allow=True,
            clipped_size=abs(notional),
            tags=["MANUAL_CLOSE", "source=manual"],
            notes="manual close",
        )
    if not risk.allow:
        view = position_view(symbol=resolved)
        view.update(
            {
                "submitted": [],
                "reject": {"tags": list(risk.tags or []), "notes": risk.notes or ""},
                "side": side_n,
                "notional_sol": abs(notional),
            }
        )
        return view

    clipped = float(risk.clipped_size or abs(notional))
    intent = OrderIntent(
        side=side_n,  # type: ignore[arg-type]
        order_type="market",
        qty_or_notional=clipped,
        max_slippage_bps=SLIP_CAP_BPS,
        client_tag=MANUAL_TAG,
    )
    data = await run_paper_order(
        ctx,
        intent,
        risk,
        auto_post_fill=True,
        close_reason="manual_close" if side_n == "sell" else "manual",
        ledger=current_book() if user_book else None,
        touch_gate=not user_book,
    )
    view = position_view(symbol=resolved)
    view.update(
        {
            "submitted": data.get("fills") or [],
            "reject": data.get("reject"),
            "side": side_n,
            "notional_sol": clipped,
        }
    )
    return view
