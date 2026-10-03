"""Pre-registered paper-trading expected band (report P0-3).

Moving-block bootstrap of the backtest hold-out daily returns -> distribution of
"cumulative return after N days" and "max drawdown within N days" for N = 1..HORIZON.
The parameters below are registered *before* paper results are compared to the band and are
written into the committed JSON (with a hash of the source returns), so they cannot be tuned
after the fact. The band is an early-warning check, NOT a Go verdict (Go/No-Go stays at 250 days).

    python -m app.backtest.expected_band --archive <binance-archive> [--out app/backtest/bands/trend_tsmom_v1.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Sequence

from app.backtest import engine, stats

# ---- registered parameters (change => new file version, never edit in place) -----------
SEED = 20261003
BLOCK = stats.BLOCK  # 20 days, same as the CI bootstrap
N_PATHS = 2000
HORIZON = 365
Q_LO, Q_MID, Q_HI = 5.0, 50.0, 95.0
VERSION = 1

DEFAULT_OUT = Path(__file__).resolve().parent / "bands" / "trend_tsmom_v1.json"


def returns_sha(r: Sequence[float]) -> str:
    return hashlib.sha256(json.dumps([round(float(x), 12) for x in r]).encode()).hexdigest()


def bootstrap_band(r: Sequence[float], *, seed: int = SEED, block: int = BLOCK, n_paths: int = N_PATHS,
                   horizon: int = HORIZON) -> dict:
    """Per N (1-based index = position + 1): quantiles of cum return and of max drawdown."""
    T = len(r)
    if T <= block:
        raise ValueError(f"need more than {block} returns, got {T}")
    rng = stats.StdRng(seed)
    nb = math.ceil(horizon / block)
    cum = [[0.0] * n_paths for _ in range(horizon)]
    mdd = [[0.0] * n_paths for _ in range(horizon)]
    for p in range(n_paths):
        nav, peak, dd = 1.0, 1.0, 0.0
        n = 0
        for st in rng.integers(0, T - block, nb):
            for j in range(int(st), int(st) + block):
                if n == horizon:
                    break
                nav *= 1.0 + r[j]
                peak = max(peak, nav)
                dd = min(dd, nav / peak - 1.0)
                cum[n][p] = nav - 1.0
                mdd[n][p] = dd
                n += 1
    q = lambda xs, k: round(stats.percentile(xs, k), 6)
    return {
        "cum_p05": [q(c, Q_LO) for c in cum], "cum_p50": [q(c, Q_MID) for c in cum], "cum_p95": [q(c, Q_HI) for c in cum],
        "mdd_p05": [q(m, Q_LO) for m in mdd], "mdd_p50": [q(m, Q_MID) for m in mdd],
    }


def holdout_returns(archive: str) -> tuple[list[int], list[float], dict]:
    from app.backtest.acceptance import END, START, WARMUP
    from app.backtest.costs import CostModel
    from app.backtest.panel import load_binance_archive
    from app.paper.strategy_runner import BAND
    from app.strategies.trend_tsmom import TrendTSMOM

    panel = load_binance_archive(archive, start=WARMUP, end=END)
    s = TrendTSMOM()
    cost = CostModel()
    full = engine.run(panel, s.targets(panel), start=START, end=END, band=BAND, cost=cost, eligible=s.eligibility(panel))
    k = stats.split_holdout(len(full.returns))
    src = {"strategy": s.describe(), "start": START, "end": END, "band": BAND, "holdout": 0.3,
           "data": panel.source, "coins": len(panel.coins), "risk_caps": "off (as in the backtest)"}
    return list(full.days[k:]), [float(x) for x in full.returns[k:]], src


def build(archive: str) -> dict:
    from app.backtest.panel import ms_day

    days, r, src = holdout_returns(archive)
    band = bootstrap_band(r)
    return {
        "name": "trend_tsmom_v1", "version": VERSION,
        "registered": {"seed": SEED, "block": BLOCK, "n_paths": N_PATHS, "horizon": HORIZON,
                       "quantiles": [Q_LO, Q_MID, Q_HI], "method": "moving-block bootstrap of hold-out daily returns",
                       "metrics": "cum = prod(1+r)-1 after N days; mdd = max drawdown within N days"},
        "source": {**src, "holdout_from": ms_day(days[0]), "holdout_to": ms_day(days[-1]), "n_days": len(r),
                   "returns_sha256": returns_sha(r), "mean_daily": sum(r) / len(r)},
        "note": "Early-warning band, not a Go verdict. Paper runs with risk caps on; the backtest has none.",
        **band,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--archive", required=True)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    a = ap.parse_args(argv)
    doc = build(a.archive)
    Path(a.out).write_text(json.dumps(doc, separators=(",", ":")) + "\n")
    s = doc["source"]
    print(f"band {doc['name']} v{doc['version']}: hold-out {s['holdout_from']}..{s['holdout_to']} n={s['n_days']} sha={s['returns_sha256'][:12]}")
    for n in (30, 90, 250, HORIZON):
        print(f"  N={n:3d} cum p05 {doc['cum_p05'][n-1]*100:+7.2f}% p50 {doc['cum_p50'][n-1]*100:+7.2f}% p95 {doc['cum_p95'][n-1]*100:+7.2f}%"
              f"  mdd p05 {doc['mdd_p05'][n-1]*100:+7.2f}%")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
