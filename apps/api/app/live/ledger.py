"""Live fill ledger — source=live only. Never mixed into paper win-rate.

This scaffold's LiveBroker does not produce fills. The ledger exists so that if
a later PR records live fills they stay off PaperTradeJournal / paper stats.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from app.models.contracts import Fill

LIVE_SOURCE = "live"
DISCLAIMER = "live ledger is isolated from paper win-rate — 实盘账本不计入纸面胜率"


@dataclass
class LiveRoundTrip:
    id: str
    strategy_id: str
    symbol: str
    mint: Optional[str]
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    fees: float
    tags: list[str] = field(default_factory=list)
    source: str = LIVE_SOURCE
    side: str = "long"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "mint": self.mint,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "qty": self.qty,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "fees": self.fees,
            "tags": list(self.tags),
            "source": LIVE_SOURCE,
            "side": self.side,
        }


@dataclass
class LiveOpenLot:
    symbol: str
    qty: float
    price: float
    ts: int
    fees: float
    tag: str = ""
    mint: Optional[str] = None
    strategy_id: str = "live"


class LiveTradeJournal:
    """FIFO matcher for venue=live fills. Isolated from PaperTradeJournal."""

    def __init__(self) -> None:
        self.fills: list[dict[str, Any]] = []
        self.lots: dict[str, list[LiveOpenLot]] = {}
        self.closed: list[LiveRoundTrip] = []

    def reset(self) -> None:
        self.fills.clear()
        self.lots.clear()
        self.closed.clear()

    def record_fill(
        self,
        symbol: str,
        fill: Fill,
        *,
        reason: str = "",
        mint: Optional[str] = None,
    ) -> list[LiveRoundTrip]:
        qty = float(fill.qty)
        px = float(fill.price)
        fee = float(fill.fee or 0.0)
        ts = int(fill.ts)
        tag = fill.tag or ""
        if qty == 0 or px <= 0:
            return []
        dumped = fill.model_dump()
        dumped["symbol"] = symbol
        dumped["source"] = LIVE_SOURCE
        dumped["venue"] = "live"
        self.fills.append(dumped)
        opened = self.lots.setdefault(symbol, [])
        remaining = qty
        new_closed: list[LiveRoundTrip] = []

        i = 0
        while remaining != 0 and i < len(opened):
            lot = opened[i]
            if lot.qty * remaining > 0:
                i += 1
                continue
            take = min(abs(lot.qty), abs(remaining))
            lot_sign = 1.0 if lot.qty > 0 else -1.0
            close_qty = take * lot_sign
            fee_share = 0.0
            if abs(lot.qty) > 0:
                fee_share += lot.fees * (take / abs(lot.qty))
            if abs(qty) > 0:
                fee_share += fee * (take / abs(qty))
            if lot.qty > 0:
                side = "long"
                pnl = (px - lot.price) * take - fee_share
                pnl_pct = (px - lot.price) / lot.price
            else:
                side = "short"
                pnl = (lot.price - px) * take - fee_share
                pnl_pct = (lot.price - px) / lot.price
            tags = [reason] if reason else []
            if tag and tag not in tags:
                tags.append(tag)
            trade = LiveRoundTrip(
                id=str(uuid.uuid4()),
                strategy_id="live",
                symbol=symbol,
                mint=mint or lot.mint,
                entry_ts=lot.ts,
                exit_ts=ts,
                entry_price=lot.price,
                exit_price=px,
                qty=take,
                pnl=pnl,
                pnl_pct=pnl_pct,
                fees=fee_share,
                tags=tags,
                source=LIVE_SOURCE,
                side=side,
            )
            self.closed.append(trade)
            new_closed.append(trade)
            lot.qty -= close_qty
            remaining -= -close_qty
            if abs(lot.qty) <= 1e-12:
                lot.fees = 0.0
                opened.pop(i)
            else:
                lot.fees = max(0.0, lot.fees - fee_share)
                i += 1

        if abs(remaining) > 1e-12:
            leftover_fee = fee * (abs(remaining) / abs(qty)) if qty else 0.0
            opened.append(
                LiveOpenLot(
                    symbol=symbol,
                    qty=remaining,
                    price=px,
                    ts=ts,
                    fees=leftover_fee,
                    tag=tag,
                    mint=mint,
                    strategy_id="live",
                )
            )
        if not opened:
            self.lots.pop(symbol, None)
        return new_closed


def summarize_live(trades: list[LiveRoundTrip]) -> dict[str, Any]:
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    return {
        "mode": "live",
        "source": LIVE_SOURCE,
        "n_trades": n,
        "wins": wins,
        "win_rate": (wins / n) if n else None,
        "journal": [t.as_dict() for t in trades],
        "disclaimer": DISCLAIMER,
        "empty": n == 0,
        "mixedIntoPaper": False,
    }


_ledger: Optional[LiveTradeJournal] = None


def get_live_ledger() -> LiveTradeJournal:
    global _ledger
    if _ledger is None:
        _ledger = LiveTradeJournal()
    return _ledger


def reset_live_ledger() -> None:
    global _ledger
    _ledger = None


def build_live_ledger() -> dict[str, Any]:
    journal = get_live_ledger()
    data = summarize_live(list(journal.closed))
    data["fill_count"] = len(journal.fills)
    data["open_lots"] = sum(len(v) for v in journal.lots.values())
    return data
