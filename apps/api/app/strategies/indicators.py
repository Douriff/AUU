"""Causal rolling helpers on ``list[Optional[float]]`` (stdlib only).

Every value at row t uses rows <= t only. Semantics follow pandas defaults
(``pct_change(fill_method=None)``, ``rolling(n).std()`` with min_periods=n, ddof=1).
"""
from __future__ import annotations

import math
from typing import Optional

Num = Optional[float]


def pct_change(x: list[Num], n: int = 1) -> list[Num]:
    out: list[Num] = [None] * len(x)
    for t in range(n, len(x)):
        a, b = x[t - n], x[t]
        if a is not None and b is not None and a != 0:
            out[t] = b / a - 1.0
    return out


def rolling_std(x: list[Num], n: int) -> list[Num]:
    """Sample std over the last n rows; None unless all n are present."""
    out: list[Num] = [None] * len(x)
    for t in range(n - 1, len(x)):
        w = x[t - n + 1 : t + 1]
        if any(v is None for v in w):
            continue
        m = sum(w) / n
        out[t] = math.sqrt(sum((v - m) ** 2 for v in w) / (n - 1))
    return out


def rolling_all_present(*series: list[Num], n: int) -> list[bool]:
    """True when every series has a value in each of the last n rows."""
    length = len(series[0])
    run = 0
    out = [False] * length
    for t in range(length):
        run = run + 1 if all(s[t] is not None for s in series) else 0
        out[t] = run >= n
    return out


def sign(v: float) -> float:
    return 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)
