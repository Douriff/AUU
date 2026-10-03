"""Strategy performance report (report P1-1 + P1-2). Read-only on the paper ledger.

- monthly returns (compounded per UTC month), drawdown curve, max drawdown and its duration
- Sortino / Calmar / Sharpe, daily win rate and win/loss ratio ("day" instead of "trade")
- per-coin attribution in USD: price (w_prev * r), funding (-w_prev * f, longs pay a positive
  rate), cost (fee + slippage from fills and risk_fills). Days split by intraday risk
  adjustments are attributed segment by segment (``adjustments`` rows). Whatever the
  per-coin parts do not explain is shown as a residual instead of being hidden.
CI and Go/No-Go stay at the top of the page (from the runner); nothing here is a verdict.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Callable, Optional

from app.backtest import stats
from app.backtest.panel import DAY_MS, ms_day

FundingFn = Callable[[str, int, int], list[tuple[int, float]]]


def _month(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m")


def metrics(days: list[int], rets: list[float]) -> dict:
    n = len(rets)
    out: dict = {"days": n}
    if n == 0:
        return out
    nav, peak, dds = 1.0, 1.0, []
    for r in rets:
        nav *= 1.0 + r
        peak = max(peak, nav)
        dds.append(nav / peak - 1.0)
    mdd = min(dds)
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else 0.0
    dsd = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / n)
    cagr = nav ** (365.0 / n) - 1.0 if nav > 0 else -1.0
    wins, losses = [r for r in rets if r > 0], [r for r in rets if r < 0]
    # drawdown durations (days below the running peak)
    longest, cur, start, best = 0, 0, None, None
    for k, d in enumerate(dds):
        if d < 0:
            cur += 1
            start = k - cur + 1
            if cur > longest:
                longest, best = cur, (start, k)
        else:
            cur = 0
    k_mdd = dds.index(mdd)
    out.update(
        totalReturn=nav - 1.0, cagr=cagr, annMean=mean * 365, annVol=sd * math.sqrt(365),
        sharpe=(mean * 365) / (sd * math.sqrt(365)) if sd > 0 else None,
        sortino=(mean * 365) / (dsd * math.sqrt(365)) if dsd > 0 else None,
        calmar=cagr / abs(mdd) if mdd < 0 else None,
        maxDrawdown=mdd, maxDrawdownDay=ms_day(days[k_mdd]),
        longestDrawdownDays=longest,
        longestDrawdown={"from": ms_day(days[best[0]]), "to": ms_day(days[best[1]])} if best else None,
        currentDrawdownDays=cur, currentDrawdown=dds[-1],
        winRate=len(wins) / n, winLossRatio=(sum(wins) / len(wins)) / abs(sum(losses) / len(losses)) if wins and losses else None,
        bestDay=max(rets), worstDay=min(rets),
        shortSample=n < 30,
    )
    return out


def monthly(days: list[int], rets: list[float]) -> list[dict]:
    acc: dict[str, list] = {}
    for d, r in zip(days, rets):
        m = _month(d)
        a = acc.setdefault(m, [1.0, 0])
        a[0] *= 1.0 + r
        a[1] += 1
    return [{"month": m, "ret": v[0] - 1.0, "days": v[1]} for m, v in sorted(acc.items())]


def drawdown_curve(days: list[int], rets: list[float], start_nav: float) -> list[dict]:
    nav, peak, out = 1.0, 1.0, []
    for d, r in zip(days, rets):
        nav *= 1.0 + r
        peak = max(peak, nav)
        out.append({"day": ms_day(d), "ts": d, "nav": nav * start_nav, "dd": nav / peak - 1.0})
    return out


def attribution(runs: list, fills: list, adjustments: list, risk_fills: list, funding_fn: Optional[FundingFn]) -> dict:
    """USD contribution per coin. ``runs`` ordered by day."""
    coins: dict[str, dict] = {}

    def add(c: str, k: str, v: float) -> None:
        coins.setdefault(c, {"coin": c, "price": 0.0, "funding": 0.0, "cost": 0.0})[k] += v

    adj_by_day: dict[int, list] = {}
    for a in adjustments:
        adj_by_day.setdefault(int(a["day"]), []).append(a)
    for v in adj_by_day.values():
        v.sort(key=lambda a: int(a["ts"]))
    rf_by_ts: dict[int, list] = {}
    for f in risk_fills:
        rf_by_ts.setdefault(int(f["ts"]), []).append(f)
    total_actual, funding_known = 0.0, funding_fn is not None
    for f in fills:
        add(f["coin"], "cost", -(float(f["fee"]) + float(f["slippage"])))
    for f in risk_fills:
        add(f["coin"], "cost", -(float(f["fee"]) + float(f["slippage"])))
    daily = []
    for j in range(1, len(runs)):
        prev, row = runs[j - 1], runs[j]
        D = int(row["day"])
        total_actual += float(row["nav_close"]) - float(row["nav_open"])
        segs = []  # (nav_start, weights, px_start, px_end, f_lo, f_hi)
        w, px, nav0, lo = json.loads(prev["weights"]), json.loads(prev["closes"]), float(prev["nav_close"]), D
        for a in adj_by_day.get(int(prev["day"]), []):
            pa = json.loads(a["prices"])
            segs.append((nav0, w, px, pa, lo, int(a["ts"]) + 1))
            w, px, nav0, lo = json.loads(a["weights_after"]), pa, float(a["nav_after"]), int(a["ts"]) + 1
        segs.append((nav0, w, px, json.loads(row["closes"]), lo, D + DAY_MS))
        day_parts = 0.0
        for nav_s, ws, p0, p1, f_lo, f_hi in segs:
            for c, wc in ws.items():
                wc = float(wc)
                if not wc:
                    continue
                a0, a1 = p0.get(c), p1.get(c)
                r = (float(a1) / float(a0) - 1.0) if (a0 and a1 is not None) else 0.0
                add(c, "price", nav_s * wc * r)
                day_parts += nav_s * wc * r
                if funding_fn is not None:
                    try:
                        fsum = sum(x for _, x in funding_fn(c, f_lo, f_hi))
                    except Exception:
                        fsum, funding_known = 0.0, False
                    add(c, "funding", -nav_s * wc * fsum)
                    day_parts -= nav_s * wc * fsum
        daily.append({"day": ms_day(D), "pnlUsd": float(row["nav_close"]) - float(row["nav_open"]), "partsUsd": day_parts})
    if runs:  # day 1 starts flat; its change is cost only
        total_actual += float(runs[0]["nav_close"]) - float(runs[0]["nav_open"])
    rows = sorted(coins.values(), key=lambda x: -(x["price"] + x["funding"] + x["cost"]))
    for x in rows:
        x["total"] = x["price"] + x["funding"] + x["cost"]
    explained = sum(x["total"] for x in rows)
    if not funding_known:  # fall back: ledger funding total, not split by coin
        fund_total = -sum(float(r["funding"]) * float(r["nav_open"]) for r in runs)
        explained += fund_total
    return {"coins": rows, "totalUsd": total_actual, "explainedUsd": explained, "residualUsd": total_actual - explained,
            "fundingByCoin": funding_known, "segmentsFromAdjustments": sum(len(v) for v in adj_by_day.values())}


def build(runner, funding_fn: Optional[FundingFn] = None) -> dict:
    led = runner.ledger
    runs = led.runs()
    start_nav = float(led.meta("start_nav") or runner.start_nav)
    days, rets = [int(x["day"]) for x in runs], [float(x["ret"]) for x in runs]
    with led._lock:
        fills = led._db.execute("SELECT * FROM fills").fetchall()
        adjs = led._db.execute("SELECT * FROM adjustments ORDER BY ts").fetchall()
        rfills = led._db.execute("SELECT * FROM risk_fills").fetchall()
    ci = None
    if len(rets) >= 30:
        lo, hi = stats.boot_ci(rets, rng=stats.StdRng(7))  # same block bootstrap as Go/No-Go (annualized mean, 95%)
        ci = {"lo": lo, "hi": hi, "method": "moving-block bootstrap, block 20, 2000 resamples, annualized mean"}
    return {
        "strategy": runner.strategy.name, "startNav": start_nav, "asOf": ms_day(days[-1]) if days else None,
        "goNoGo": runner.go_no_go(runs), "ci": ci,
        "metrics": metrics(days, rets), "monthly": monthly(days, rets),
        "drawdown": drawdown_curve(days, rets, start_nav),
        "attribution": attribution(runs, fills, adjs, rfills, funding_fn),
        "note": "纸面账本（PAPER），实盘锁定。指标只是描述，Go/No-Go 以页顶判定为准（≥250 天且 CI 下限 > 0、跑赢国债）。样本少于 30 天时比率没有统计意义。",
    }
