"""Return statistics for daily series (plan §2.1), stdlib only.

- annualized mean (x365), CAGR, vol, Sharpe
- 95% CI of the annualized mean: moving-block bootstrap (block 20 days, 2000 resamples)
- max drawdown, worst calendar month, "remove the best 3 months" annualized mean
- excess vs a T-bill rate (constant; plan uses 3.99%)

``rng`` is anything with ``integers(low, high, size) -> sequence``; the default is a seeded
stdlib generator. Passing ``numpy.random.default_rng(seed)`` reproduces numpy-based research runs.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone
from typing import Optional, Sequence

TBILL = 0.0399
BLOCK = 20
N_BOOT = 2000


class StdRng:
    def __init__(self, seed: int = 7):
        self._r = random.Random(seed)

    def integers(self, low: int, high: int, size: int) -> list[int]:
        return [self._r.randrange(low, high) for _ in range(size)]


def percentile(xs: Sequence[float], q: float) -> float:
    """numpy 'linear' percentile."""
    s = sorted(xs)
    h = (len(s) - 1) * q / 100.0
    lo = math.floor(h)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (h - lo) * (s[hi] - s[lo])


def boot_ci(r: Sequence[float], *, block: int = BLOCK, n: int = N_BOOT, rng=None) -> tuple[float, float]:
    T = len(r)
    if T < 30:
        return (math.nan, math.nan)
    rng = rng or StdRng()
    nb = math.ceil(T / block)
    means = []
    for _ in range(n):
        starts = rng.integers(0, T - block, nb)
        acc, cnt = 0.0, 0
        for st in starts:
            st = int(st)
            for j in range(st, st + block):
                if cnt == T:
                    break
                acc += r[j]
                cnt += 1
        means.append(acc / T)
    return (percentile(means, 2.5) * 365, percentile(means, 97.5) * 365)


def max_drawdown(r: Sequence[float]) -> float:
    eq, peak, mdd = 1.0, 1.0, 0.0
    for x in r:
        eq *= 1 + x
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return mdd


def _month(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m")


def _year(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


def _mean(x: Sequence[float]) -> float:
    return sum(x) / len(x)


def _std(x: Sequence[float]) -> float:
    m = _mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - 1))


def monthly(days: Sequence[int], r: Sequence[float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for d, x in zip(days, r):
        k = _month(d)
        out[k] = (1 + out.get(k, 0.0)) * (1 + x) - 1
    return out


def by_year(days: Sequence[int], r: Sequence[float]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for d, x in zip(days, r):
        y = _year(d)
        e = out.setdefault(y, {"ret": 0.0, "days": 0})
        e["ret"] = (1 + e["ret"]) * (1 + x) - 1
        e["days"] += 1
    return out


def summarize(days: Sequence[int], r: Sequence[float], *, label: str = "", tbill: float = TBILL, rng=None, n_boot: int = N_BOOT) -> dict:
    if len(r) < 30:
        return {"label": label, "days": len(r)}
    m = monthly(days, r)
    best3 = sorted(m, key=lambda k: m[k], reverse=True)[:3]
    ex = [x for d, x in zip(days, r) if _month(d) not in best3]
    lo, hi = boot_ci(r, rng=rng, n=n_boot)
    mean, sd = _mean(r), _std(r)
    yrs = len(r) / 365
    growth = 1.0
    for x in r:
        growth *= 1 + x
    growth_ex = 1.0
    for x in ex:
        growth_ex *= 1 + x
    worst = min(m, key=lambda k: m[k])
    return {
        "label": label,
        "start": datetime.fromtimestamp(days[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        "end": datetime.fromtimestamp(days[-1] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
        "days": len(r),
        "ann_mean": mean * 365,
        "cagr": growth ** (1 / yrs) - 1,
        "vol": sd * math.sqrt(365),
        "sharpe": mean / sd * math.sqrt(365) if sd > 0 else math.nan,
        "ci_lo": lo,
        "ci_hi": hi,
        "mdd": max_drawdown(r),
        "worst_month": m[worst],
        "worst_month_at": worst,
        "ex_best3m_ann": _mean(ex) * 365,
        "ex_best3m_cagr": growth_ex ** (365 / len(ex)) - 1,
        "best3m": best3,
        "tbill": tbill,
        "excess_vs_tbill": mean * 365 - tbill,
        "excess_ci": (lo - tbill, hi - tbill),
    }


def split_holdout(n: int, holdout: float = 0.3) -> int:
    """Index of the first hold-out row (last ``holdout`` share of the sample)."""
    return int(n * (1 - holdout))


def fmt(s: dict) -> str:
    if "ann_mean" not in s:
        return f"{s.get('label', '')}: n={s.get('days')}"
    return (
        f"{s['label']:<34} {s['start']}..{s['end']} ann {s['ann_mean']*100:6.2f}% CAGR {s['cagr']*100:6.2f}% "
        f"CI[{s['ci_lo']*100:6.2f},{s['ci_hi']*100:6.2f}] Sh {s['sharpe']:.2f} MDD {s['mdd']*100:6.1f}% "
        f"worstM {s['worst_month']*100:5.1f}% ({s['worst_month_at']}) ex3 {s['ex_best3m_ann']*100:6.2f}%"
    )
