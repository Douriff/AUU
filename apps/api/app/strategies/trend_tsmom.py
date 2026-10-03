"""Trend following: time-series momentum ensemble (plan §2, "T1b").

Same code for backtest and paper: :meth:`TrendTSMOM.targets` gives the target-weight row
for every day (decided at that day's close, causal); :meth:`TrendTSMOM.decide` is the
latest row only, which is what a paper/live runner calls once per daily close.

Recipe (pre-registered, not tuned):
  signal_c(t) = mean_k sign(close_t / close_{t-L_k} - 1), L = 20/60/120 (long-only clips < 0 to 0)
  size_c(t)   = min(target_vol / vol90_c(t), cap) / N_eligible(t)
  weight      = signal * size; coins without 90 days of spot+perp history get 0.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Optional

from app.strategies.indicators import pct_change, rolling_all_present, rolling_std, sign


@dataclass(frozen=True)
class TrendTSMOMParams:
    lookbacks: tuple[int, ...] = (20, 60, 120)
    long_only: bool = True
    target_vol: float = 0.25  # per-coin annualized vol target before dividing by N
    cap: float = 2.0  # per-coin leverage cap on target_vol / vol
    vol_window: int = 90
    elig_window: int = 90
    universe: Optional[tuple[str, ...]] = None  # None = every coin in the panel


@dataclass
class TrendTSMOM:
    params: TrendTSMOMParams = field(default_factory=TrendTSMOMParams)
    name: str = "trend_tsmom_v1"

    def describe(self) -> dict:
        return {"name": self.name, **asdict(self.params)}

    def coins(self, panel) -> list[str]:
        u = self.params.universe
        return [c for c in panel.coins if u is None or c in u]

    def eligibility(self, panel) -> dict[str, list[bool]]:
        n = self.params.elig_window
        return {c: rolling_all_present(panel.perp_close[c], panel.spot_close[c], n=n) for c in panel.coins}

    def targets(self, panel) -> dict[str, list[float]]:
        """Target weight per coin per day (0.0 when not eligible / no signal)."""
        p = self.params
        T = len(panel)
        elig = self.eligibility(panel)
        coins = self.coins(panel)
        n_elig = [max(1, sum(1 for c in coins if elig[c][t])) for t in range(T)]
        out: dict[str, list[float]] = {c: [0.0] * T for c in panel.coins}
        ann = math.sqrt(365)
        for c in coins:
            close = panel.spot_close[c]
            vol = rolling_std(pct_change(close), p.vol_window)
            moms = [pct_change(close, L) for L in p.lookbacks]
            row = out[c]
            for t in range(T):
                if not elig[c][t] or vol[t] is None:
                    continue
                ms = [m[t] for m in moms]
                if any(m is None for m in ms):
                    continue
                s = sum(sign(m) for m in ms) / len(ms)
                if p.long_only and s < 0:
                    s = 0.0
                v = vol[t] * ann
                lev = p.cap if v == 0 else min(p.target_vol / v, p.cap)
                row[t] = s * lev / n_elig[t]
        return out

    def decide(self, panel) -> dict[str, float]:
        """Targets for the newest row only (paper runner entry point)."""
        if not len(panel):
            return {}
        full = self.targets(panel)
        return {c: w[-1] for c, w in full.items()}
