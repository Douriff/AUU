"""Session paper fill ledger → closed trades + performance stats (simulation only)."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Optional

from app.models.contracts import Fill

DEFAULT_MC_PATHS = 1000
DEFAULT_MC_SEED = 42
MIN_SHARPE_N = 5
DISCLAIMER = (
    "simulation from paper history, not a promise — 纸面历史重抽样，非实盘承诺"
)


@dataclass
class OpenLot:
    symbol: str
    qty: float  # signed: +long / -short remaining
    price: float
    ts: int
    fee: float
    tag: str = ""


@dataclass
class ClosedTrade:
    symbol: str
    side: str  # long | short
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: int
    exit_ts: int
    fee: float
    pnl: float
    pnl_pct: float
    tag: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "fee": self.fee,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "tag": self.tag,
            "reason": self.reason,
        }


class PaperLedger:
    """FIFO round-trip matcher for PaperBroker fills (session memory)."""

    def __init__(self) -> None:
        self.fills: list[dict[str, Any]] = []
        self.lots: dict[str, list[OpenLot]] = {}
        self.closed: list[ClosedTrade] = []

    def record_fill(
        self,
        symbol: str,
        fill: Fill,
        *,
        reason: str = "",
    ) -> list[ClosedTrade]:
        qty = float(fill.qty)
        px = float(fill.price)
        fee = float(fill.fee or 0.0)
        ts = int(fill.ts)
        tag = fill.tag or ""
        if qty == 0 or px <= 0:
            return []
        dumped = fill.model_dump()
        dumped["symbol"] = symbol
        self.fills.append(dumped)
        opened = self.lots.setdefault(symbol, [])
        remaining = qty
        new_closed: list[ClosedTrade] = []

        # Cover opposite lots first (FIFO).
        i = 0
        while remaining != 0 and i < len(opened):
            lot = opened[i]
            if lot.qty * remaining > 0:
                i += 1
                continue
            take = min(abs(lot.qty), abs(remaining))
            lot_sign = 1.0 if lot.qty > 0 else -1.0
            close_qty = take * lot_sign  # signed qty closed of the open lot
            fee_share = 0.0
            lot_notional = abs(lot.qty) * lot.price
            if lot_notional > 0:
                fee_share += lot.fee * (take / abs(lot.qty))
            fill_notional = abs(qty) * px
            if fill_notional > 0:
                fee_share += fee * (take / abs(qty))
            if lot.qty > 0:
                side = "long"
                pnl = (px - lot.price) * take - fee_share
                pnl_pct = (px - lot.price) / lot.price
            else:
                side = "short"
                pnl = (lot.price - px) * take - fee_share
                pnl_pct = (lot.price - px) / lot.price
            if lot.price * take > 0:
                pnl_pct -= fee_share / (lot.price * take)
            trade = ClosedTrade(
                symbol=symbol,
                side=side,
                qty=take,
                entry_price=lot.price,
                exit_price=px,
                entry_ts=lot.ts,
                exit_ts=ts,
                fee=fee_share,
                pnl=pnl,
                pnl_pct=pnl_pct,
                tag=tag or lot.tag,
                reason=reason,
            )
            self.closed.append(trade)
            new_closed.append(trade)
            lot.qty -= close_qty
            remaining -= -close_qty  # remaining is the fill remainder
            if abs(lot.qty) <= 1e-12:
                lot.fee = 0.0
                opened.pop(i)
            else:
                lot.fee = max(0.0, lot.fee - fee_share)
                i += 1

        if abs(remaining) > 1e-12:
            leftover_fee = fee * (abs(remaining) / abs(qty)) if qty else 0.0
            opened.append(
                OpenLot(
                    symbol=symbol,
                    qty=remaining,
                    price=px,
                    ts=ts,
                    fee=leftover_fee,
                    tag=tag,
                )
            )
        if not opened:
            self.lots.pop(symbol, None)
        return new_closed

    def closed_in_window(
        self,
        *,
        window: Optional[int] = None,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> list[ClosedTrade]:
        rows = list(self.closed)
        if from_ts is not None:
            rows = [t for t in rows if t.exit_ts >= from_ts]
        if to_ts is not None:
            rows = [t for t in rows if t.exit_ts <= to_ts]
        if window is not None and window > 0:
            rows = rows[-window:]
        return rows


def _max_drawdown_pct(returns: list[float]) -> Optional[float]:
    if not returns:
        return None
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in returns:
        equity *= 1.0 + r
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd * 100.0


def _sharpe_like(returns: list[float]) -> Optional[float]:
    n = len(returns)
    if n < MIN_SHARPE_N:
        return None
    mean = sum(returns) / n
    var = sum((x - mean) ** 2 for x in returns) / (n - 1)
    std = math.sqrt(var)
    if std <= 1e-18:
        return None
    return mean / std


def monte_carlo(
    returns: list[float],
    *,
    n_paths: int = DEFAULT_MC_PATHS,
    seed: int = DEFAULT_MC_SEED,
    day_loss_pct: float = 0.05,
) -> dict[str, Any]:
    """Resample closed-trade pnl_pct with replacement. Label as paper simulation."""
    n = len(returns)
    empty = {
        "n_paths": n_paths,
        "trade_count": n,
        "seed": seed,
        "p_equity_positive": None,
        "p_equity_above_start": None,
        "p_hit_day_loss": None,
        "final_equity_pct_p5": None,
        "final_equity_pct_p50": None,
        "final_equity_pct_p95": None,
        "label": DISCLAIMER,
    }
    if n == 0:
        return empty
    rng = random.Random(seed)
    n_paths = max(1, int(n_paths))
    finals: list[float] = []
    n_pos = 0
    n_above = 0
    n_dd = 0
    threshold = 1.0 - float(day_loss_pct)
    for _ in range(n_paths):
        eq = 1.0
        hit = False
        for _j in range(n):
            r = returns[rng.randrange(n)]
            eq *= 1.0 + r
            if eq <= threshold:
                hit = True
        finals.append(eq)
        if eq > 0:
            n_pos += 1
        if eq > 1.0:
            n_above += 1
        if hit:
            n_dd += 1
    finals.sort()

    def pctile(p: float) -> float:
        if not finals:
            return 0.0
        idx = min(len(finals) - 1, max(0, int(round((p / 100.0) * (len(finals) - 1)))))
        return (finals[idx] - 1.0) * 100.0

    return {
        "n_paths": n_paths,
        "trade_count": n,
        "seed": seed,
        "p_equity_positive": n_pos / n_paths,
        "p_equity_above_start": n_above / n_paths,
        "p_hit_day_loss": n_dd / n_paths,
        "final_equity_pct_p5": pctile(5),
        "final_equity_pct_p50": pctile(50),
        "final_equity_pct_p95": pctile(95),
        "day_loss_pct": day_loss_pct,
        "label": DISCLAIMER,
    }


def summarize(
    trades: list[ClosedTrade],
    *,
    stop_loss_pct: float = 0.12,
    day_loss_pct: float = 0.05,
    n_paths: int = DEFAULT_MC_PATHS,
    seed: int = DEFAULT_MC_SEED,
    window: str | int = "session",
) -> dict[str, Any]:
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl < 0)
    flats = n - wins - losses
    returns = [t.pnl_pct for t in trades]
    win_rate = (wins / n) if n else None
    exp = (sum(returns) / n) if n else None
    sl = max(float(stop_loss_pct), 1e-9)
    exp_r = (sum(r / sl for r in returns) / n) if n else None
    mc = monte_carlo(returns, n_paths=n_paths, seed=seed, day_loss_pct=day_loss_pct) if n else None
    return {
        "mode": "paper",
        "liveDisabled": True,
        "window": window,
        "trade_count": n,
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "win_rate": win_rate,
        "expectancy_pnl_pct": exp * 100.0 if exp is not None else None,
        "expectancy_r": exp_r,
        "max_drawdown_pct": _max_drawdown_pct(returns),
        "sharpe_like": _sharpe_like(returns),
        "open_lots": None,
        "monte_carlo": mc,
        "disclaimer": DISCLAIMER,
        "empty": n == 0,
        "recent": [t.as_dict() for t in trades[-8:]],
    }


_ledger: Optional[PaperLedger] = None


def get_paper_ledger() -> PaperLedger:
    global _ledger
    if _ledger is None:
        _ledger = PaperLedger()
    return _ledger


def reset_paper_ledger() -> None:
    global _ledger
    _ledger = None
