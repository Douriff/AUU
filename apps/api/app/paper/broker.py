"""PaperBroker — limit cross / depth take / formula slippage (paper mode only)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.models.contracts import (
    BookCtx,
    Fill,
    OrderIntent,
    RejectOut,
    StrategyContext,
)


@dataclass
class PaperBroker:
    fee_bps: float = 15.0
    base_bps: float = 15.0
    k: float = 40.0
    alpha: float = 0.6
    latency_ms: int = 300
    open_orders: list[OrderIntent] = field(default_factory=list)
    last_reject: Optional[RejectOut] = None

    def submit(self, ctx: StrategyContext, intent: OrderIntent) -> list[Fill]:
        """Return fills or empty list on reject (never fabricates a fake fill on deny)."""
        self.last_reject = None
        fill_ts = ctx.ts + self.latency_ms

        if intent.order_type == "market":
            return self._fill_market(ctx, intent, fill_ts)
        if intent.order_type == "limit":
            return self._try_limit(ctx, intent, fill_ts)
        if intent.order_type == "twap_sim":
            return self._twap_slices(ctx, intent, fill_ts)
        self._reject(["MAX_NOTIONAL"], f"unknown order_type={intent.order_type}")
        return []

    def _slippage_bps(self, notional: float, adv_usd: float) -> float:
        adv = max(adv_usd, 1.0)
        return self.base_bps + self.k * ((abs(notional) / adv) ** self.alpha)

    def _reject(self, tags: list[str], notes: str = "") -> None:
        self.last_reject = RejectOut(tags=tags, notes=notes)

    def _mid(self, ctx: StrategyContext) -> float:
        if ctx.tick and ctx.tick.mid > 0:
            return ctx.tick.mid
        if ctx.book:
            bids = ctx.book.bids
            asks = ctx.book.asks
            if bids and asks:
                return (bids[0].price + asks[0].price) / 2.0
        return 1.0

    def _market_source(self, ctx: StrategyContext) -> str | None:
        raw = (ctx.meta or {}).get("market_source")
        if raw in {None, ""}:
            return None
        return str(raw)

    def _fill_ts(self, ctx: StrategyContext, fill_ts: int) -> int:
        raw = (ctx.meta or {}).get("fill_ts")
        if raw is None:
            return fill_ts
        try:
            return int(raw)
        except (TypeError, ValueError):
            return fill_ts

    def _fill_market(self, ctx: StrategyContext, intent: OrderIntent, fill_ts: int) -> list[Fill]:
        notional = abs(float(intent.qty_or_notional))
        meta = ctx.meta or {}
        flatten = meta.get("flatten_qty")
        held = abs(float(flatten)) if flatten is not None else 0.0
        if notional <= 0 and held <= 0:
            self._reject(["MAX_NOTIONAL"], "zero notional")
            return []
        fill_ts = self._fill_ts(ctx, fill_ts)
        source = self._market_source(ctx)

        if meta.get("curve_fill") and ctx.pump is not None:
            return self._fill_curve(ctx, intent, fill_ts, notional, held, source)

        if ctx.book and (ctx.book.bids or ctx.book.asks) and not (intent.side == "sell" and held > 0):
            legs = self._walk_book(ctx.book, intent.side, notional)
            if not legs:
                self._reject(["DEPTH_THIN"], "book empty for side")
                return []
        else:
            mid = self._mid(ctx)
            slip = self._slippage_bps(notional or held * mid, ctx.liquidity.adv_usd)
            if slip > intent.max_slippage_bps:
                self._reject(["SLIPPAGE_CAP"], f"slip={slip:.1f}>cap={intent.max_slippage_bps}")
                return []
            px = mid * (1 + slip / 1e4) if intent.side == "buy" else mid * (1 - slip / 1e4)
            qty = held if intent.side == "sell" and held > 0 else notional / px
            legs = [(px, qty, slip)]

        out: list[Fill] = []
        for px, qty, slip in legs:
            fee = abs(px * qty) * self.fee_bps / 1e4
            signed = qty if intent.side == "buy" else -qty
            out.append(
                Fill(
                    ts=fill_ts,
                    price=px,
                    qty=signed,
                    fee=fee,
                    slippage_bps=slip,
                    tag=intent.client_tag,
                    market_source=source,
                )
            )
        return out

    def _fill_curve(
        self,
        ctx: StrategyContext,
        intent: OrderIntent,
        fill_ts: int,
        notional: float,
        held: float,
        source: str | None,
    ) -> list[Fill]:
        from app.paper.real_fill import quote_curve_fill

        pump = ctx.pump
        assert pump is not None
        quoted = quote_curve_fill(
            side=intent.side,
            notional_sol=notional,
            virtual_sol_reserves=int(float(pump.virtual_sol_reserves)),
            virtual_token_reserves=int(float(pump.virtual_token_reserves)),
            real_token_reserves=int(float(pump.real_token_reserves)),
            flatten_qty=held if intent.side == "sell" else None,
            creator_fee_bps=int(pump.creator_fee_bps or 0),
            mid=self._mid(ctx),
        )
        if quoted is None:
            self._reject(["DEPTH_THIN"], "curve cannot fill")
            return []
        px, qty, fee, slip = quoted
        signed = qty if intent.side == "buy" else -qty
        return [
            Fill(
                ts=fill_ts,
                price=px,
                qty=signed,
                fee=fee,
                slippage_bps=slip,
                tag=intent.client_tag,
                market_source=source,
            )
        ]

    def _walk_book(
        self, book: BookCtx, side: str, notional: float
    ) -> list[tuple[float, float, float]]:
        levels = book.asks if side == "buy" else book.bids
        if not levels:
            return []
        remaining = notional
        mid = (book.bids[0].price + book.asks[0].price) / 2.0 if book.bids and book.asks else levels[0].price
        fills: list[tuple[float, float, float]] = []
        for lvl in levels:
            if remaining <= 0:
                break
            level_notional = abs(lvl.price * lvl.size)
            take = min(remaining, level_notional)
            qty = take / lvl.price if lvl.price else 0.0
            if qty <= 0:
                continue
            slip = abs(lvl.price - mid) / mid * 1e4 if mid else 0.0
            fills.append((lvl.price, qty, slip))
            remaining -= take
        return fills

    def _crossed(self, ctx: StrategyContext, intent: OrderIntent) -> bool:
        if intent.limit_price is None:
            return False
        if not ctx.book:
            mid = self._mid(ctx)
            # no book: cross if mid through limit
            if intent.side == "buy":
                return mid <= intent.limit_price
            return mid >= intent.limit_price
        if intent.side == "buy":
            return bool(ctx.book.asks) and ctx.book.asks[0].price <= intent.limit_price
        return bool(ctx.book.bids) and ctx.book.bids[0].price >= intent.limit_price

    def _try_limit(self, ctx: StrategyContext, intent: OrderIntent, fill_ts: int) -> list[Fill]:
        if intent.limit_price is None or intent.limit_price <= 0:
            self._reject(["MAX_NOTIONAL"], "limit_price required")
            return []
        if not self._crossed(ctx, intent):
            self.open_orders.append(intent)
            return []
        px = float(intent.limit_price)
        notional = abs(float(intent.qty_or_notional))
        qty = notional / px
        fee = abs(px * qty) * self.fee_bps / 1e4
        signed = qty if intent.side == "buy" else -qty
        return [
            Fill(
                ts=fill_ts,
                price=px,
                qty=signed,
                fee=fee,
                slippage_bps=0.0,
                tag=intent.client_tag,
            )
        ]

    def _twap_slices(self, ctx: StrategyContext, intent: OrderIntent, fill_ts: int) -> list[Fill]:
        n = 3
        slice_n = abs(float(intent.qty_or_notional)) / n
        out: list[Fill] = []
        for i in range(n):
            sub = OrderIntent(
                side=intent.side,
                order_type="market",
                qty_or_notional=slice_n,
                max_slippage_bps=intent.max_slippage_bps,
                client_tag=intent.client_tag,
            )
            part = self._fill_market(ctx, sub, fill_ts + i * 200)
            if not part:
                # partial TWAP abort — keep what we have; if none, reject already set
                break
            out.extend(part)
        return out

    def on_tick(self, ctx: StrategyContext) -> list[Fill]:
        done: list[Fill] = []
        rest: list[OrderIntent] = []
        for o in self.open_orders:
            if self._crossed(ctx, o):
                done.extend(self._try_limit(ctx, o, ctx.ts))
            else:
                if o.expire_ts and ctx.ts >= o.expire_ts:
                    continue
                rest.append(o)
        self.open_orders = rest
        return done


_broker: Optional[PaperBroker] = None


def get_paper_broker() -> PaperBroker:
    global _broker
    if _broker is None:
        _broker = PaperBroker()
    return _broker


def reset_paper_broker() -> None:
    global _broker
    _broker = None
