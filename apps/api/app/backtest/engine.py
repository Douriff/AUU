"""Daily portfolio backtest: weight drift, rebalance band, fees + slippage, funding.

Timing (no look-ahead): targets decided at the close of day i are traded at that close
and earn day i+1's close-to-close perp return. Funding for day i is charged on the
weights held through day i (longs pay positive funding). A coin is rebalanced only when
its weight drifts more than ``band`` (relative) from target, or to exit / flip.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.backtest.costs import CostModel
from app.backtest.panel import Panel, day_ms


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


@dataclass
class BacktestResult:
    days: list[int]
    returns: list[float]  # daily net return
    turnover: list[float]  # sum |trade weight| per day
    gross: list[float]  # gross exposure held into each day (before that day's trades)
    costs: list[float]
    funding: list[float]
    meta: dict = field(default_factory=dict)

    @property
    def turnover_ann(self) -> float:
        return sum(self.turnover) / len(self.days) * 365 if self.days else 0.0


@dataclass
class DayStep:
    """One day of the portfolio accounting (shared by the backtest and the paper runner)."""

    weights: list[float]  # after today's trades (fractions of NAV)
    drifted: list[float]  # after today's returns, before trades
    pnl: float  # price PnL minus funding (+ cash yield), fraction of yesterday's NAV
    funding: float
    cost: float  # fees + slippage, fraction of NAV
    turnover: float
    gross_held: float
    trades: list[tuple[int, float, float]]  # (coin index, weight before, weight after)

    @property
    def ret(self) -> float:
        return self.pnl - self.cost


def step(
    w: list[float],
    r: list[float],
    f: list[float],
    tg: list[float],
    cps: list[float],
    *,
    band: float = 0.2,
    min_trade: float = 0.01,
    cash_yield: float = 0.0,
) -> DayStep:
    """Mark yesterday's weights ``w`` to today's returns ``r`` / funding ``f``, then trade to ``tg``.

    Trades happen at today's close; a coin is traded only outside the band, on exit, or on a
    side change. Costs are |dw| * per-side cost (taker fee + slippage).
    """
    gross_held = sum(abs(x) for x in w)
    fund = sum(wi * fi for wi, fi in zip(w, f))
    pnl = sum(wi * ri for wi, ri in zip(w, r)) - fund
    if cash_yield:
        pnl += max(0.0, 1.0 - gross_held) * cash_yield / 365
    g = 1.0 + pnl
    w = [wi * (1.0 + ri) / g for wi, ri in zip(w, r)] if g > 0 else [0.0] * len(w)
    drifted = list(w)
    tr_sum, c_sum, trades = 0.0, 0.0, []
    for k in range(len(w)):
        t = tg[k]
        dw = t - w[k]
        # trade when outside the band, when exiting, or when entering/flipping side
        need = abs(dw) > max(band * abs(t), min_trade) or (t == 0 and w[k] != 0) or (t != 0 and _sign(t) != _sign(w[k]))
        if need and dw != 0:
            tr_sum += abs(dw)
            c_sum += abs(dw) * cps[k]
            trades.append((k, w[k], t))
            w[k] = t
    return DayStep(w, drifted, pnl, fund, c_sum, tr_sum, gross_held, trades)


def run(
    panel: Panel,
    targets: dict[str, list[float]],
    *,
    start: str,
    end: str,
    cost: Optional[CostModel] = None,
    band: float = 0.2,
    min_trade: float = 0.01,
    eligible: Optional[dict[str, list[bool]]] = None,
    cash_yield: float = 0.0,
) -> BacktestResult:
    cost = cost or CostModel()
    coins = panel.coins
    s_ms, e_ms = day_ms(start), day_ms(end)
    rows = [i for i, d in enumerate(panel.days) if s_ms <= d <= e_ms]
    # perp close-to-close return; missing -> 0 (no position can be priced)
    ret = {}
    for c in coins:
        px = panel.perp_close[c]
        ret[c] = [0.0] + [
            (px[i] / px[i - 1] - 1.0) if (px[i] is not None and px[i - 1] is not None and px[i - 1] != 0) else 0.0
            for i in range(1, len(px))
        ]
    cps = [cost.per_side(c) for c in coins]
    w = [0.0] * len(coins)
    out, turn, gross_l, cost_l, fund_l = [], [], [], [], []
    for i in rows:
        r = [ret[c][i] for c in coins]
        f = [panel.funding[c][i] for c in coins]
        tg = []
        for c in coins:
            t = targets.get(c, [0.0] * len(panel))[i] or 0.0
            if eligible is not None and not eligible[c][i]:
                t = 0.0
            tg.append(t)
        st = step(w, r, f, tg, cps, band=band, min_trade=min_trade, cash_yield=cash_yield)
        w = st.weights
        out.append(st.ret)
        turn.append(st.turnover)
        gross_l.append(st.gross_held)
        cost_l.append(st.cost)
        fund_l.append(st.funding)
    return BacktestResult([panel.days[i] for i in rows], out, turn, gross_l, cost_l, fund_l)
