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
        gross_held = sum(abs(x) for x in w)
        fund = sum(wi * fi for wi, fi in zip(w, f))
        pnl = sum(wi * ri for wi, ri in zip(w, r)) - fund
        if cash_yield:
            pnl += max(0.0, 1.0 - gross_held) * cash_yield / 365
        g = 1.0 + pnl
        w = [wi * (1.0 + ri) / g for wi, ri in zip(w, r)] if g > 0 else [0.0] * len(coins)
        tr_sum, c_sum = 0.0, 0.0
        for k, c in enumerate(coins):
            tg = targets.get(c, [0.0] * len(panel))[i] or 0.0
            if eligible is not None and not eligible[c][i]:
                tg = 0.0
            dw = tg - w[k]
            # trade when outside the band, when exiting, or when entering/flipping side
            need = (
                abs(dw) > max(band * abs(tg), min_trade)
                or (tg == 0 and w[k] != 0)
                or (tg != 0 and _sign(tg) != _sign(w[k]))
            )
            if need and dw != 0:
                tr_sum += abs(dw)
                c_sum += abs(dw) * cps[k]
                w[k] = tg
        out.append(pnl - c_sum)
        turn.append(tr_sum)
        gross_l.append(gross_held)
        cost_l.append(c_sum)
        fund_l.append(fund)
    return BacktestResult([panel.days[i] for i in rows], out, turn, gross_l, cost_l, fund_l)
