"""H2 trend leg (research2 "B058"), stdlib only, causal.

Port of research2/strat.py ``trend_targets(sig="agree3", pvt=0.15, ccap=0.10, regime="zero")``:

  hold_c(t)  = 1 if spot close is above its close 20, 60 AND 120 days ago, else 0 (long only)
  size_c(t)  = min(0.25 / vol90_c(t), 2.0) / N_eligible(t)      (same per-coin sizing as trend_tsmom_v1)
  regime     = 0 when BTC spot close <= its 200-day mean (all weights flat), else 1
  portfolio  = scale today's book to 15% ex-ante annual vol on the trailing-60-day sample covariance
               of daily spot returns (missing -> 0), scale capped at 10x, then gross capped at 2.0
  coin cap   = each weight clipped to at most 0.10

Every value at row t uses rows <= t only. Parameters come from ``app.paper.shadow_h2.PARAMS``;
they are frozen there (hash-checked) and must not be edited here.
"""
from __future__ import annotations

import math
from typing import Optional

from app.strategies.indicators import pct_change, rolling_all_present, rolling_std


def rolling_mean(x: list, n: int) -> list:
    out: list = [None] * len(x)
    for t in range(n - 1, len(x)):
        w = x[t - n + 1 : t + 1]
        if any(v is None for v in w):
            continue
        out[t] = sum(w) / n
    return out


def _cov_quad(R: list[list[float]], idx: list[int], w: list[float], t: int, n: int) -> float:
    """w' C w with C the ddof=1 sample covariance of R[t-n+1..t] restricted to columns idx."""
    rows = range(t - n + 1, t + 1)
    cols = [[R[k][j] for j in rows] for k in idx]
    means = [sum(c) / n for c in cols]
    dev = [[v - m for v in c] for c, m in zip(cols, means)]
    # portfolio deviation series p_j = sum_k w_k dev_k[j]; w'Cw = sum p_j^2 / (n-1)
    q = 0.0
    for j in range(n):
        p = 0.0
        for a, k in enumerate(idx):
            p += w[a] * dev[a][j]
        q += p * p
    return q / (n - 1)


def targets(panel, p: dict, *, from_index: int = 0) -> dict[str, list[float]]:
    """Target weights per coin per row (rows < from_index are left at 0 to save work)."""
    T = len(panel)
    coins = [c for c in panel.coins if c in p["universe"]]
    lbs, tv, cap = p["lookbacks"], p["per_coin_target_vol"], p["per_coin_lev_cap"]
    ann = math.sqrt(365)
    elig = {c: rolling_all_present(panel.spot_close[c], panel.perp_close[c], n=p["elig_window"]) for c in coins}
    n_elig = [max(1, sum(1 for c in coins if elig[c][t])) for t in range(T)]
    ret = {c: pct_change(panel.spot_close[c]) for c in coins}
    W = {c: [0.0] * T for c in panel.coins}
    for c in coins:
        vol = rolling_std(ret[c], p["vol_window"])
        moms = [pct_change(panel.spot_close[c], L) for L in lbs]
        for t in range(max(from_index, 0), T):
            if not elig[c][t] or vol[t] is None:
                continue
            ms = [m[t] for m in moms]
            if any(m is None for m in ms):
                continue
            s = 1.0 if all(m > 0 for m in ms) else 0.0  # agree3
            v = vol[t] * ann
            lev = cap if not v > 0 else min(tv / v, cap)
            W[c][t] = s * lev / n_elig[t]
    # regime filter: BTC spot close vs its N-day mean (strictly above = bull)
    rg = p["regime"]
    btc = panel.spot_close.get(rg["coin"]) or [None] * T
    ma = rolling_mean(btc, rg["ma_days"])
    for t in range(from_index, T):
        bull = btc[t] is not None and ma[t] is not None and btc[t] > ma[t]
        if not bull:
            for c in coins:
                W[c][t] *= rg["bear_mult"]
    # portfolio vol target on the trailing covariance of daily spot returns (missing -> 0)
    n = p["cov_window"]
    R = {c: [0.0 if x is None else x for x in ret[c]] for c in coins}
    Rl = [R[c] for c in coins]
    for t in range(from_index, T):
        idx = [k for k, c in enumerate(coins) if W[c][t] != 0.0]
        if t < n or not idx:
            sc = 0.0
        else:
            w = [W[coins[k]][t] for k in idx]
            pv = math.sqrt(max(_cov_quad(Rl, idx, w, t, n), 0.0) * 365)
            sc = min(p["portfolio_vol_target"] / pv, p["pvt_scale_cap"]) if pv > 0 else 0.0
        g = 0.0
        for c in coins:
            W[c][t] *= sc
            g += abs(W[c][t])
        if g > 0:
            k = min(1.0, p["gross_cap"] / g)
            for c in coins:
                W[c][t] *= k
        for c in coins:
            W[c][t] = min(W[c][t], p["coin_cap"])
    return W


def decide(panel, p: dict) -> dict[str, float]:
    if not len(panel):
        return {}
    full = targets(panel, p, from_index=len(panel) - 1)
    return {c: w[-1] for c, w in full.items()}
