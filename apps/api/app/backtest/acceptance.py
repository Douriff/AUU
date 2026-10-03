"""M2 acceptance: reproduce plan §2.2 (trend T1b row + benchmarks) within 0.1 percentage point.

    python -m app.backtest.acceptance --archive DIR [--json out.json]

DIR holds the Binance public-archive monthly zips (``app.backtest.panel.ARCHIVE_URLS``).
Reference numbers below are the research run's output (full precision, not rounded).

Bootstrap CIs depend on the random stream. The research drew from one numpy
``default_rng(7)`` shared by every summary in call order; when numpy is installed the
replay below re-creates that stream (burning the draws of the 21 summaries that came
before T1b), so CIs are compared exactly too. Without numpy, CIs are reported with the
engine's own seeded RNG and checked against a looser Monte-Carlo tolerance.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time

from app.backtest import research, stats
from app.backtest.panel import load_binance_archive
from app.strategies import TrendTSMOM

START, END = "2021-01-01", "2026-09-30"
WARMUP = "2020-01-01"
PP = 0.001  # 0.1 percentage point
MC_TOL = 0.05  # CI tolerance when the bootstrap stream differs: 5% of the reference CI width

REF = {
    "strategy": {
        "train": {"ann_mean": 0.15188955824729172, "sharpe": 1.403419193539334, "ci_lo": 0.017421087538195545, "ci_hi": 0.2882416679509835},
        "hold": {"ann_mean": 0.0325482626995448, "cagr": 0.03001956215034407, "ci_lo": -0.06154482505401232, "ci_hi": 0.11598341501588841,
                 "mdd": -0.08130269814951108, "worst_month": -0.01330619176726433, "ex_best3m_ann": -0.036526452818908206},
        "turnover_ann": 6.030365547502501,
        "by_year": {2021: 0.30431798727317916, 2022: -0.0469069488418179, 2023: 0.1451754793045672, 2024: 0.25854321593356033,
                    2025: -0.0005647757084547012, 2026: 0.05772296651171649},
    },
    "cost_x2_hold_ann": 0.027897850783302813,
    "bench:BTC buy&hold": {"hold": {"ann_mean": 0.019725618934732573, "cagr": -0.07187774848705397, "mdd": -0.5297176591351063,
                                    "ci_lo": -0.6256307497734148, "ci_hi": 0.5958651243368283}},
    "bench:EW universe buy&hold": {"hold": {"cagr": -0.24362307316888965, "mdd": -0.6675019113886018,
                                            "ci_lo": -0.9553680925223015, "ci_hi": 0.6122559110072785}},
    "tbill": 0.0399,
    "split": {"train": ["2021-01-01", "2025-01-08"], "hold": ["2025-01-09", "2026-09-30"]},
}

# Research call order of summaries (bt.py "t1"): each report() = full, train, hold.
_LEN = {"full": 2099, "train": 1469, "hold": 630}
_ORDER_BEFORE = {"bench:BTC buy&hold": 0, "bench:EW universe buy&hold": 1, "strategy": 7}


def numpy_replay_factory():
    try:
        import numpy as np
    except ImportError:
        return None

    def burn(g, reports: int):
        for _ in range(reports):
            for part in ("full", "train", "hold"):
                T = _LEN[part]
                nb = math.ceil(T / stats.BLOCK)
                for _ in range(stats.N_BOOT):
                    g.integers(0, T - stats.BLOCK, nb)

    def factory(key: str):
        g = np.random.default_rng(7)
        if key in _ORDER_BEFORE:
            burn(g, _ORDER_BEFORE[key])
        return g

    return factory


def compare(rep: dict, exact_ci: bool) -> list[dict]:
    rows = []

    def chk(name, got, ref, tol):
        ok = got is not None and abs(got - ref) <= tol
        rows.append({"metric": name, "got": got, "ref": ref, "diff": None if got is None else got - ref, "tol": tol, "ok": ok})

    s = rep["strategy_result"]
    def ci_tol(ref: dict) -> float:
        return PP if exact_ci else max(PP, MC_TOL * (ref["ci_hi"] - ref["ci_lo"]))

    for part, ref in REF["strategy"].items():
        if part in ("train", "hold"):
            for k, v in ref.items():
                chk(f"T1b {part} {k}", s[part][k], v, (0.01 if k == "sharpe" else ci_tol(ref) if k.startswith("ci_") else PP))
    chk("T1b turnover/yr", s["turnover_ann"], REF["strategy"]["turnover_ann"], 0.1)
    for y, v in REF["strategy"]["by_year"].items():
        chk(f"T1b year {y}", s["by_year"][y]["ret"], v, PP)
    chk("T1b hold ann, 2x cost", rep["sensitivity"]["cost_x2"]["ann_mean"], REF["cost_x2_hold_ann"], PP)
    for key in ("bench:BTC buy&hold", "bench:EW universe buy&hold"):
        b = rep["benchmarks"][key.split(":", 1)[1]]["hold"]
        for k, v in REF[key]["hold"].items():
            chk(f"{key[6:]} hold {k}", b[k], v, ci_tol(REF[key]["hold"]) if k.startswith("ci_") else PP)
    chk("T-bill", rep["protocol"]["tbill"], REF["tbill"], 1e-9)
    rows.append({"metric": "split", "got": rep["split"], "ref": REF["split"], "ok": rep["split"] == REF["split"]})
    rows.append({"metric": "lookahead", "got": rep["lookahead"]["max_abs_diff"], "ref": 0.0, "ok": rep["lookahead"]["ok"]})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--archive", required=True)
    ap.add_argument("--json", default=None)
    ap.add_argument("--no-numpy", action="store_true", help="use the engine's stdlib RNG for CIs")
    a = ap.parse_args(argv)
    t0 = time.time()
    panel = load_binance_archive(a.archive, start=WARMUP, end=END)
    factory = None if a.no_numpy else numpy_replay_factory()
    rep = research.run_research(TrendTSMOM(), panel, start=START, end=END, rng_factory=factory, walk_forward_from=2023)
    rows = compare(rep, exact_ci=factory is not None)
    bad = [r for r in rows if not r["ok"]]
    print(f"M2 acceptance  data={panel.source} coins={len(panel.coins)}  CI stream={'numpy replay (exact)' if factory else 'stdlib seed 7 (MC tolerance)'}")
    for r in rows:
        if isinstance(r["got"], float):
            unit = 100 if r["metric"] not in ("T1b train sharpe", "T1b turnover/yr", "lookahead") else 1
            print(f"  {'OK ' if r['ok'] else 'BAD'} {r['metric']:<34} got {r['got']*unit:10.4f}  ref {r['ref']*unit:10.4f}  diff {r['diff']*unit if r.get('diff') is not None else 0:+.4f}" if "diff" in r else f"  {'OK ' if r['ok'] else 'BAD'} {r['metric']:<34} {r['got']}")
        else:
            print(f"  {'OK ' if r['ok'] else 'BAD'} {r['metric']:<34} {r['got']}")
    for k in ("train", "hold"):
        print("  " + stats.fmt(rep["strategy_result"][k]))
    print("  " + stats.fmt(rep["walk_forward"]["oos"]), rep["walk_forward"]["picks"])
    print(f"RESULT {'PASS' if not bad else 'FAIL'}  {len(rows) - len(bad)}/{len(rows)} within tolerance  ({time.time() - t0:.0f}s)")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"rows": rows, "report": rep}, fh, indent=1, default=str)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
