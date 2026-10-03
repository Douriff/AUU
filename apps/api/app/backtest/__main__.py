"""CLI:  python -m app.backtest --strategy trend_tsmom_v1 (--archive DIR | --store) [options]

Examples
  python -m app.backtest --archive ./binance-archive --start 2021-01-01 --end 2026-09-30
  python -m app.backtest --store --coins BTC,ETH,SOL --start 2025-01-01      # live data from AUU's SQLite
"""
from __future__ import annotations

import argparse
import json
import sys

from app.backtest import research, stats
from app.backtest.costs import CostModel
from app.backtest.panel import UNIVERSE_19, load_binance_archive, load_store, ms_day
from app.strategies import REGISTRY, TrendTSMOMParams


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.backtest", description="AUU backtest (research protocol, plan §2.1)")
    ap.add_argument("--strategy", default="trend_tsmom_v1", choices=sorted(REGISTRY))
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--archive", help="dir with Binance public-archive monthly zips")
    src.add_argument("--store", action="store_true", help="read AUU's mainstream.sqlite (AUU_DATA_DIR)")
    ap.add_argument("--exchange", default=None, help="store exchange (default: whichever has data)")
    ap.add_argument("--coins", default=",".join(UNIVERSE_19))
    ap.add_argument("--warmup-start", default="2020-01-01")
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-09-30")
    ap.add_argument("--holdout", type=float, default=0.3)
    ap.add_argument("--walk-forward-from", type=int, default=None, help="first calendar year of yearly OOS segments")
    ap.add_argument("--cost-mult", type=float, default=1.0)
    ap.add_argument("--band", type=float, default=0.2)
    ap.add_argument("--tbill", type=float, default=stats.TBILL)
    ap.add_argument("--long-short", action="store_true")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    coins = [c.strip().upper() for c in a.coins.split(",") if c.strip()]
    if a.archive:
        panel = load_binance_archive(a.archive, coins, start=a.warmup_start, end=a.end)
    else:
        from app.marketdata.mainstream.store import MarketStore

        st = MarketStore()
        ex = a.exchange
        if not ex:
            for name in ("binance", "okx"):
                if st.candle_bounds(name, coins[0], "1d")[2]:
                    ex = name
                    break
        panel = load_store(st, ex or "binance", coins, start=a.warmup_start if a.warmup_start != "2020-01-01" else None)
        if panel.days:
            a.end = min(a.end, ms_day(panel.days[-1]))
    if not panel.coins:
        print("no data for", coins, file=sys.stderr)
        return 2
    strat = REGISTRY[a.strategy](TrendTSMOMParams(long_only=not a.long_short))
    rep = research.run_research(strat, panel, start=a.start, end=a.end, holdout=a.holdout, band=a.band,
                                cost=CostModel().scaled(a.cost_mult), tbill=a.tbill, walk_forward_from=a.walk_forward_from)
    sr = rep["strategy_result"]
    print(f"{strat.name}  data={panel.source} coins={','.join(panel.coins)}  split train {rep['split']['train']} hold {rep['split']['hold']}")
    for k in ("train", "hold"):
        print("  " + stats.fmt(sr[k]))
    print(f"  turnover/yr {sr['turnover_ann']:.1f}  avg gross {rep['avg_gross']:.2f}")
    for s in rep["sensitivity"].values():
        print("  " + stats.fmt(s))
    for name, b in rep["benchmarks"].items():
        print("  " + (stats.fmt(b["hold"]) if "hold" in b else f"{name:<34} {b['ann']*100:.2f}%/yr"))
    if "walk_forward" in rep:
        print("  " + stats.fmt(rep["walk_forward"]["oos"]))
    h = sr["hold"]
    if "ann_mean" in h:
        print(f"  vs T-bill {a.tbill*100:.2f}%: excess {h['excess_vs_tbill']*100:+.2f}%/yr CI [{h['excess_ci'][0]*100:+.2f}, {h['excess_ci'][1]*100:+.2f}]")
    print(f"  look-ahead check: {'OK' if rep['lookahead']['ok'] else 'FAILED'} (max diff {rep['lookahead']['max_abs_diff']:.2e})")
    print(f"  verdict: hold-out CI lower bound > 0: {rep['verdict']['hold_ci_lo_gt_0']}; beats T-bill (CI): {rep['verdict']['hold_beats_tbill_ci']}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(rep, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
