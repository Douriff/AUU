"""One backtest run with the research protocol (plan §2.1).

- hold-out: last 30% of the evaluation window (train = first 70%), looked at once
- walk-forward: calendar-year out-of-sample segments; with a parameter grid each year uses
  only parameters chosen (by Sharpe) on data before that year
- block-bootstrap CI, remove-best-3-months, 2x cost sensitivity, +1 day execution lag
- benchmarks: BTC spot buy-and-hold, equal-weight universe buy-and-hold, T-bill
- look-ahead check: targets recomputed on truncated data must equal the full-run targets
"""
from __future__ import annotations

import random
from typing import Any, Callable, Optional, Sequence

from app.backtest import engine, stats
from app.backtest.costs import CostModel
from app.backtest.panel import Panel, day_ms, ms_day


def evaluate(days: Sequence[int], r: Sequence[float], *, name: str, holdout: float, tbill: float, rng_factory: Callable[[str], Any], turnover_ann: float = 0.0, key: str = "") -> dict:
    """full / train / hold summaries; one bootstrap RNG per evaluation, used in that order."""
    k = stats.split_holdout(len(r), holdout)
    rng = rng_factory(key or name)
    out = {
        "full": stats.summarize(days, r, label=f"{name} full", tbill=tbill, rng=rng),
        "train": stats.summarize(days[:k], r[:k], label=f"{name} train", tbill=tbill, rng=rng),
        "hold": stats.summarize(days[k:], r[k:], label=f"{name} HOLD", tbill=tbill, rng=rng),
        "by_year": stats.by_year(days, r),
        "turnover_ann": turnover_ann,
    }
    return out


def benchmarks(panel: Panel, *, start: str, end: str, eligible: dict[str, list[bool]]) -> dict[str, tuple[list[int], list[float]]]:
    s, e = day_ms(start), day_ms(end)
    rows = [i for i, d in enumerate(panel.days) if s <= d <= e]
    days = [panel.days[i] for i in rows]

    def sret(c, i):
        px = panel.spot_close[c]
        if i == 0 or px[i] is None or px[i - 1] is None:
            return None
        return px[i] / px[i - 1] - 1

    out = {}
    if "BTC" in panel.coins:
        out["BTC buy&hold"] = (days, [sret("BTC", i) or 0.0 for i in rows])
    ew = []
    for i in rows:
        xs = [x for c in panel.coins if eligible[c][i] and (x := sret(c, i)) is not None]
        ew.append(sum(xs) / len(xs) if xs else 0.0)
    out["EW universe buy&hold"] = (days, ew)
    return out


def lookahead_check(strategy, panel: Panel, *, samples: int = 6, seed: int = 11, start_row: int = 150) -> dict:
    """Recompute targets on data truncated at random rows; any difference means the
    strategy used information from the future (freqtrade 'lookahead-analysis' idea)."""
    full = strategy.targets(panel)
    rnd = random.Random(seed)
    rows = sorted(rnd.sample(range(start_row, len(panel)), min(samples, max(0, len(panel) - start_row))))
    worst = 0.0
    for n in rows:
        cut = strategy.decide(panel.truncate(n + 1))
        for c in panel.coins:
            worst = max(worst, abs(cut.get(c, 0.0) - full[c][n]))
    return {"rows_checked": [ms_day(panel.days[n]) for n in rows], "max_abs_diff": worst, "ok": worst < 1e-12}


def walk_forward(
    panel: Panel,
    make_targets: Callable[[Any], dict[str, list[float]]],
    grid: Sequence[Any],
    *,
    start: str,
    end: str,
    first_year: int,
    cost: CostModel,
    band: float,
    eligible: dict,
) -> dict:
    """Each calendar year >= first_year trades the grid point with the best Sharpe on all
    evaluation data before that year. A one-point grid gives plain yearly OOS segments."""
    runs = {}
    for g in grid:
        res = engine.run(panel, make_targets(g), start=start, end=end, cost=cost, band=band, eligible=eligible)
        runs[g if isinstance(g, str) else repr(g)] = (g, res)
    years = sorted({int(ms_day(d)[:4]) for d in next(iter(runs.values()))[1].days if int(ms_day(d)[:4]) >= first_year})
    days_all, rets_all, picks = [], [], {}
    for y in years:
        def sharpe_before(item):
            _, res = item
            xs = [x for d, x in zip(res.days, res.returns) if int(ms_day(d)[:4]) < y]
            if len(xs) < 30:
                return float("-inf")
            m = sum(xs) / len(xs)
            sd = (sum((v - m) ** 2 for v in xs) / (len(xs) - 1)) ** 0.5
            return m / sd if sd > 0 else float("-inf")

        key, (g, res) = max(runs.items(), key=lambda kv: sharpe_before(kv[1]))
        picks[y] = key
        for d, x in zip(res.days, res.returns):
            if int(ms_day(d)[:4]) == y:
                days_all.append(d)
                rets_all.append(x)
    return {"picks": picks, "days": days_all, "returns": rets_all}


def run_research(
    strategy,
    panel: Panel,
    *,
    start: str,
    end: str,
    holdout: float = 0.3,
    band: float = 0.2,
    cost: Optional[CostModel] = None,
    tbill: float = stats.TBILL,
    cost_mults: Sequence[float] = (2.0,),
    lag_check: bool = True,
    walk_forward_from: Optional[int] = None,
    rng_factory: Optional[Callable[[str], Any]] = None,
    lookahead_samples: int = 6,
) -> dict:
    cost = cost or CostModel()
    # One seeded bootstrap RNG per evaluation (keyed), so every number is reproducible.
    rng_factory = rng_factory or (lambda _key: stats.StdRng(7))
    elig = strategy.eligibility(panel)
    T = strategy.targets(panel)
    base = engine.run(panel, T, start=start, end=end, cost=cost, band=band, eligible=elig)
    report: dict[str, Any] = {
        "strategy": strategy.describe(),
        "data": {"source": panel.source, "coins": panel.coins, "first_day": ms_day(panel.days[0]), "last_day": ms_day(panel.days[-1])},
        "protocol": {"start": start, "end": end, "holdout": holdout, "band": band, "tbill": tbill,
                     "cost": {"taker": cost.taker, "slippage": cost.slippage, "slippage_default": cost.slippage_default, "mult": cost.mult},
                     "bootstrap": {"block": stats.BLOCK, "n": stats.N_BOOT}},
        "strategy_result": evaluate(base.days, base.returns, name=strategy.name, holdout=holdout, tbill=tbill, rng_factory=rng_factory, turnover_ann=base.turnover_ann, key="strategy"),
        "avg_gross": sum(base.gross) / len(base.gross) if base.gross else 0.0,
    }
    k = stats.split_holdout(len(base.returns), holdout)
    report["split"] = {"train": [ms_day(base.days[0]), ms_day(base.days[k - 1])], "hold": [ms_day(base.days[k]), ms_day(base.days[-1])]}
    sens = {}
    for m in cost_mults:
        r = engine.run(panel, T, start=start, end=end, cost=cost.scaled(m), band=band, eligible=elig)
        sens[f"cost_x{m:g}"] = stats.summarize(r.days[k:], r.returns[k:], label=f"{strategy.name} cost x{m:g} HOLD", tbill=tbill, rng=rng_factory(f"cost_x{m:g}"))
    if lag_check:
        lagged = {c: [0.0] + w[:-1] for c, w in T.items()}
        r = engine.run(panel, lagged, start=start, end=end, cost=cost, band=band, eligible=elig)
        sens["lag_1d"] = stats.summarize(r.days[k:], r.returns[k:], label=f"{strategy.name} lag+1d HOLD", tbill=tbill, rng=rng_factory("lag_1d"))
    report["sensitivity"] = sens
    report["benchmarks"] = {
        name: evaluate(d, r, name=name, holdout=holdout, tbill=tbill, rng_factory=rng_factory, key=f"bench:{name}")
        for name, (d, r) in benchmarks(panel, start=start, end=end, eligible=elig).items()
    }
    report["benchmarks"]["T-bill"] = {"ann": tbill}
    if walk_forward_from:
        wf = walk_forward(panel, lambda _g: T, [strategy.describe()["name"]], start=start, end=end,
                          first_year=walk_forward_from, cost=cost, band=band, eligible=elig)
        report["walk_forward"] = {
            "picks": wf["picks"],
            "oos": stats.summarize(wf["days"], wf["returns"], label=f"{strategy.name} WF OOS", tbill=tbill, rng=rng_factory("walk_forward")),
            "by_year": stats.by_year(wf["days"], wf["returns"]),
        }
    if lookahead_samples:
        report["lookahead"] = lookahead_check(strategy, panel, samples=lookahead_samples)
    h, b = report["strategy_result"]["hold"], report["benchmarks"].get("BTC buy&hold", {}).get("hold", {})
    report["verdict"] = {
        "hold_ci_lo_gt_0": bool(h.get("ci_lo", -1) > 0),
        "hold_beats_tbill_ci": bool(h.get("excess_ci", (-1, -1))[0] > 0),
        "hold_vs_btc_cagr": (h.get("cagr", 0) - b.get("cagr", 0)) if b else None,
        "note": "edge is proven only if the hold-out CI lower bound is > 0 after costs",
    }
    return report
