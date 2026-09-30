"""Offline exit / entry-gating variant study on stored paper-trade evidence.

    python -m app.paper.exit_study [--evidence FILE ...] [--json OUT] [--markdown OUT]

Re-simulates every evidence record (:mod:`app.paper.evidence`) under a grid
of exit policies and entry gates, with the same fill model as
:mod:`app.paper.replay`: an exit decided at check time ``t`` fills on the
first real print at or after ``t + exit_delay_ms`` (300 ms = the paper
latency) against that print's curve reserves, 125 bps fee per side; the entry
is the recorded fill (fees included).

Exit rules (checked on a ``tick_ms`` grid anchored like the engine's loop;
``tick_ms=0`` = after every print, i.e. an event-driven exit):

* ``stop`` / ``tp``: mark vs entry fill price;
* ``time_stop_s`` + ``time_stop_min_ret``: exit once held that long unless up
  at least that much;
* ``trail_activate`` + ``trail_dist``: after the peak print is up
  ``trail_activate``, exit when the mark is ``trail_dist`` below the peak;
* ``sp_window_s`` + ``sp_ratio``: sell SOL >= ratio x buy SOL over the last
  window (at least ``sp_min_sells`` sells) → exit ("tape weakening");
* ``partial_frac`` at ``partial_tp``: sell that fraction, then the remainder
  uses ``after_partial_stop`` (e.g. 0 = break-even) and ``tp2``;
* ``max_hold_s``.

Limits (reported per variant): the tape ends 30 s (evidence v1/v2) or
``AUU_EVIDENCE_POST_MS`` (v3, default 90 s) after the recorded exit and
starts 30 s before the entry signal. A policy that would
still hold at the tape end is closed at the last print and counted in
``tape_end``; results with many ``tape_end`` trades are not credible. Knock-on
effects (cooldown, open-mint cap, day-loss stop) of different holds are not
modelled. Paper research only; never sends anything.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from app.paper.replay import _VT_OFFSET, _fill, _prints, iter_records
from app.providers.pumpfun_curve_math import price_sol

BOOTSTRAP_N = 2000


@dataclass(frozen=True)
class ExitPolicy:
    name: str = "baseline"
    stop: float = 0.05
    tp: Optional[float] = 0.06
    tick_ms: int = 1000
    exit_delay_ms: int = 300
    max_hold_s: float = 120.0
    time_stop_s: Optional[float] = None
    time_stop_min_ret: float = 0.0
    trail_activate: Optional[float] = None
    trail_dist: Optional[float] = None
    sp_window_s: Optional[float] = None
    sp_ratio: float = 1.0
    sp_min_sells: int = 1
    partial_frac: Optional[float] = None
    partial_tp: Optional[float] = None
    after_partial_stop: Optional[float] = None
    tp2: Optional[float] = None
    fee_bps: int = 125
    # Fill model extras: priority fee per transaction (SOL), re-filled entry
    # after ``entry_delay_ms`` from the signal (None = recorded fill).
    prio_fee_sol: float = 0.0
    entry_delay_ms: Optional[int] = None
    # Event exits (checked on every print after the entry fill):
    # ``dump_k``: a print whose log-return is below median - k * MAD-sigma of
    # the pre-entry per-print log-returns; ``creator_sell``: the creator sells
    # (needs the ``who`` tape column, evidence v3+).
    dump_k: Optional[float] = None
    creator_sell: bool = False


class _Tape:
    """Time-indexed stored prints: (ts, vs, vt) + side / SOL per print."""

    def __init__(self, rec: Mapping[str, Any]) -> None:
        tape = rec.get("tape") or {}
        t0 = int(tape.get("t0") or 0)
        rows = sorted((r for r in tape.get("rows") or [] if len(r) >= 5), key=lambda r: r[0])
        self.ts = [t0 + int(r[0]) for r in rows]
        self.side = [int(r[1]) for r in rows]
        self.sol = [float(r[2] or 0.0) for r in rows]
        self.res = [(t0 + int(r[0]), int(r[3]), int(r[4])) for r in rows]
        self.px = [price_sol(int(r[3]), int(r[4])) for r in rows]
        fields = list(tape.get("fields") or [])
        wi = fields.index("who") if "who" in fields else None
        self.who = [(r[wi] if wi is not None and len(r) > wi else None) for r in rows]
        self.has_who = wi is not None
        # Curve still live at this print (no fills after graduation).
        self.live = [int(r[4]) - _VT_OFFSET > 0 for r in rows]
        # prefix sums for windowed buy / sell SOL and sell counts
        self._buy = [0.0]
        self._sell = [0.0]
        self._nsell = [0]
        for s, v in zip(self.side, self.sol):
            self._buy.append(self._buy[-1] + (v if s > 0 else 0.0))
            self._sell.append(self._sell[-1] + (v if s < 0 else 0.0))
            self._nsell.append(self._nsell[-1] + (1 if s < 0 else 0))

    def idx_le(self, t: int) -> int:
        return bisect.bisect_right(self.ts, t) - 1

    def first_ge(self, t: int) -> Optional[tuple[int, int, int]]:
        i = bisect.bisect_left(self.ts, t)
        while i < len(self.res) and not self.live[i]:
            i += 1
        return self.res[i] if i < len(self.res) else None

    def flow(self, lo: int, hi: int) -> tuple[float, float, int]:
        a = bisect.bisect_left(self.ts, lo)
        b = bisect.bisect_right(self.ts, hi)
        return self._buy[b] - self._buy[a], self._sell[b] - self._sell[a], self._nsell[b] - self._nsell[a]


def simulate_policy(rec: Mapping[str, Any], pol: ExitPolicy) -> Optional[dict[str, Any]]:
    """One record under one policy. None when not replayable (no tape / entry)."""
    entry = rec.get("entry") or {}
    if not entry.get("fill_ts") or not entry.get("fill_price") or not entry.get("fill_qty"):
        return None
    tape = _Tape(rec)
    if not tape.ts:
        return None
    if pol.entry_delay_ms is None:
        e_ts = int(entry["fill_ts"])
        e_px = float(entry["fill_price"])
        qty = float(entry["fill_qty"])
        cost = e_px * qty + float(entry.get("fill_fee") or 0.0)
    else:
        sig = int(entry.get("signal_ts") or entry["fill_ts"])
        p = tape.first_ge(sig + int(pol.entry_delay_ms))
        size = float(entry.get("notional_sol") or (float(entry["fill_price"]) * float(entry["fill_qty"])))
        got = _fill("buy", p, notional=size, fee_bps=pol.fee_bps) if p else None
        if got is None:
            return {"status": "no_entry"}
        e_px, qty, fee = got
        e_ts = p[0]
        cost = e_px * qty + fee
    cost += pol.prio_fee_sol
    last_ts = tape.ts[-1]
    anchor = int((rec.get("exit") or {}).get("signal_ts") or e_ts)
    first = bisect.bisect_right(tape.ts, e_ts)
    if pol.tick_ms > 0:
        k = -((anchor - e_ts) // pol.tick_ms)
        start = anchor + k * pol.tick_ms
        grid = set(range(start, last_ts + 1, pol.tick_ms))
    else:
        grid = set(tape.ts[first:])
    # Event exits fire on the print itself (event-driven), so add print times.
    event_idx: dict[int, str] = {}
    if pol.dump_k is not None:
        thr = _dump_threshold(tape, e_ts, pol.dump_k)
        if thr is not None:
            for i in range(max(first, 1), len(tape.ts)):
                if math.log(tape.px[i] / tape.px[i - 1]) < thr:
                    event_idx.setdefault(tape.ts[i], "dump")
    if pol.creator_sell:
        creator = str(((rec.get("features") or {}).get("token") or {}).get("creator") or "")
        if not tape.has_who or not creator:
            return {"status": "not_applicable"}
        c8 = creator[:8]
        for i in range(first, len(tape.ts)):
            if tape.side[i] < 0 and tape.who[i] == c8:
                event_idx.setdefault(tape.ts[i], "creator_sell")
    checks: Iterable[int] = sorted(grid | set(event_idx))
    remaining = qty
    proceeds = 0.0
    fees = 0.0
    legs: list[dict[str, Any]] = []
    stop = pol.stop
    tp = pol.tp
    partial_done = False
    peak = e_px
    reason = None
    flag = None

    def sell(q: float, t: int, why: str) -> bool:
        nonlocal proceeds, fees, flag
        p = tape.first_ge(int(t) + int(pol.exit_delay_ms))
        if p is None:
            p, flag = tape.res[-1], "tape_end"
        got = _fill("sell", p, qty=q, fee_bps=pol.fee_bps)
        if got is None:
            return False
        px, _q, fee = got
        proceeds += px * q
        fees += fee + pol.prio_fee_sol
        legs.append({"reason": why, "signal_ts": int(t), "fill_ts": p[0], "qty": q, "price": px})
        return True

    last_check = e_ts
    for t in checks:
        if t < e_ts:
            continue
        i = tape.idx_le(t)
        if i < 0:
            continue
        j0 = tape.idx_le(last_check)
        seg = tape.px[max(j0 + 1, 0) : i + 1]
        if seg:
            peak = max(peak, max(seg))
        last_check = t
        mark = tape.px[i]
        ret = mark / e_px - 1.0
        hold = (t - e_ts) / 1000.0
        why = event_idx.get(t)
        if why is None and (
            not partial_done
            and pol.partial_frac
            and pol.partial_tp is not None
            and ret >= pol.partial_tp
        ):
            q = remaining * float(pol.partial_frac)
            if sell(q, t, "partial_tp"):
                remaining -= q
                partial_done = True
                if pol.after_partial_stop is not None:
                    stop = -float(pol.after_partial_stop)  # e.g. 0.0 → break-even stop
                tp = pol.tp2
            continue
        if why is not None:
            pass
        elif tp is not None and ret >= tp:
            why = "take_profit"
        elif ret <= -stop:
            why = "stop_loss"
        elif (
            pol.trail_activate is not None
            and pol.trail_dist is not None
            and peak / e_px - 1.0 >= pol.trail_activate
            and mark <= peak * (1.0 - pol.trail_dist)
        ):
            why = "trail"
        elif pol.sp_window_s:
            b, s, ns = tape.flow(t - int(pol.sp_window_s * 1000), t)
            if ns >= pol.sp_min_sells and s >= pol.sp_ratio * max(b, 1e-18):
                why = "sell_pressure"
        if why is None and pol.time_stop_s is not None and hold >= pol.time_stop_s and ret < pol.time_stop_min_ret:
            why = "time_stop"
        if why is None and hold >= pol.max_hold_s:
            why = "max_hold"
        if why:
            reason = why
            if not sell(remaining, t, why):
                return {"status": "no_exit"}
            remaining = 0.0
            break
    if remaining > 0:
        reason = reason or "tape_end"
        flag = "tape_end"
        if not sell(remaining, last_ts, "tape_end"):
            return {"status": "no_exit"}
    net = proceeds - fees - cost
    return {
        "status": "ok",
        "id": f"{rec.get('mint')}@{e_ts}",
        "entry_ts": e_ts,
        "signal_ts": int(entry.get("signal_ts") or e_ts),
        "reason": reason,
        "flag": flag,
        "legs": len(legs),
        "hold_s": (max(l["fill_ts"] for l in legs) - e_ts) / 1000.0 if legs else None,
        "cost_sol": cost,
        "net_sol": net,
        "net_bps": net / cost * 1e4 if cost else None,
    }


def _dump_threshold(tape: "_Tape", e_ts: int, k: float, min_n: int = 8) -> Optional[float]:
    """median - k * 1.4826 * MAD of per-print log-returns before the entry fill."""
    idx = [i for i in range(1, len(tape.ts)) if tape.ts[i] <= e_ts]
    rets = [math.log(tape.px[i] / tape.px[i - 1]) for i in idx if tape.px[i - 1] > 0]
    if len(rets) < min_n:
        return None
    med = statistics.median(rets)
    mad = statistics.median(abs(x - med) for x in rets)
    sigma = 1.4826 * mad
    if sigma <= 0:
        sigma = statistics.pstdev(rets) or 1e-4
    return med - k * sigma


def bootstrap_ci(vals: Sequence[float], n: int = BOOTSTRAP_N, seed: int = 7) -> tuple[Optional[float], Optional[float]]:
    import random

    if len(vals) < 2:
        return None, None
    rng = random.Random(seed)
    m = len(vals)
    means = sorted(sum(vals[rng.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


# ----------------------------------------------------------------- statistics
def stats(rows: Sequence[Mapping[str, Any]], *, bootstrap: bool = False) -> dict[str, Any]:
    ok = [r for r in rows if r.get("status") == "ok" and r.get("net_bps") is not None]
    bps = [float(r["net_bps"]) for r in ok]
    sol = [float(r["net_sol"]) for r in ok]
    n = len(bps)
    out: dict[str, Any] = {"n": n}
    if not n:
        return out
    mean = statistics.fmean(bps)
    se = statistics.stdev(bps) / math.sqrt(n) if n > 1 else float("nan")
    order = sorted(range(n), key=lambda i: -sol[i])
    keep = [i for i in range(n) if i not in set(order[:3])]
    out.update(
        {
            "mean_bps": mean,
            "se_bps": se,
            "ci_lo_bps": mean - 1.96 * se,
            "ci_hi_bps": mean + 1.96 * se,
            "median_bps": statistics.median(bps),
            "win_rate": sum(1 for x in bps if x > 0) / n,
            "net_sol": sum(sol),
            "sol_per_trade": sum(sol) / n,
            "mean_bps_ex_top3": statistics.fmean([bps[i] for i in keep]) if keep else None,
            "net_sol_ex_top3": sum(sol[i] for i in keep),
            "tape_end_rate": sum(1 for r in ok if r.get("flag") == "tape_end") / n,
            "mean_bps_ex_top1pct": _mean_ex_top(bps, max(1, math.ceil(0.01 * n))),
        }
    )
    if bootstrap:
        out["boot_lo_bps"], out["boot_hi_bps"] = bootstrap_ci(bps)
    return out


def _mean_ex_top(vals: Sequence[float], k: int) -> Optional[float]:
    keep = sorted(vals)[: max(0, len(vals) - k)]
    return statistics.fmean(keep) if keep else None


def time_split(recs: Sequence[Mapping[str, Any]], frac: float = 0.5) -> tuple[list, list]:
    ordered = sorted(recs, key=lambda r: int((r.get("entry") or {}).get("signal_ts") or 0))
    half = int(len(ordered) * frac)
    return list(ordered[:half]), list(ordered[half:])


# ------------------------------------------------------------------ entry gates
def feat(rec: Mapping[str, Any], path: str) -> Any:
    cur: Any = rec
    for part in path.split("."):
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(part)
    return cur


@dataclass(frozen=True)
class Gate:
    name: str
    path: str = ""
    lo: Optional[float] = None
    hi: Optional[float] = None
    keep_null: bool = False

    def ok(self, rec: Mapping[str, Any]) -> bool:
        if not self.path:
            return True
        v = feat(rec, self.path)
        if v is None:
            return self.keep_null
        v = float(v)
        return (self.lo is None or v >= self.lo) and (self.hi is None or v < self.hi)


def default_policies() -> list[ExitPolicy]:
    b = ExitPolicy()
    pols = [b]
    for s in (0.03, 0.04):
        pols.append(replace(b, name=f"stop{int(s*100)}", stop=s))
    for tk in (500, 250, 0):
        pols.append(replace(b, name=f"tick{tk}", tick_ms=tk))
        pols.append(replace(b, name=f"stop3_tick{tk}", stop=0.03, tick_ms=tk))
    for n in (2, 3, 5, 10):
        for x in (0.0, 0.02):
            pols.append(replace(b, name=f"time{n}s_min{int(x*100)}", time_stop_s=n, time_stop_min_ret=x))
    for act, dist in ((0.03, 0.03), (0.04, 0.03), (0.06, 0.04), (0.06, 0.06), (0.10, 0.05)):
        pols.append(replace(b, name=f"trail{int(act*100)}_{int(dist*100)}_notp", tp=None, trail_activate=act, trail_dist=dist))
        pols.append(replace(b, name=f"trail{int(act*100)}_{int(dist*100)}_tp12", tp=0.12, trail_activate=act, trail_dist=dist))
    for w in (2, 3, 5):
        for r in (1.0, 2.0):
            pols.append(replace(b, name=f"sp{w}s_r{r:g}", sp_window_s=w, sp_ratio=r, sp_min_sells=2))
    for frac, ptp, tp2 in ((0.5, 0.04, 0.10), (0.5, 0.06, 0.15), (0.5, 0.03, None)):
        pols.append(
            replace(
                b, name=f"partial{int(frac*100)}@{int(ptp*100)}_tp2{'-' if tp2 is None else int(tp2*100)}_be",
                partial_frac=frac, partial_tp=ptp, tp2=tp2, after_partial_stop=0.0,
                trail_activate=ptp if tp2 is None else None, trail_dist=0.03 if tp2 is None else None,
            )
        )
    pols.append(replace(b, name="tp4", tp=0.04))
    pols.append(replace(b, name="tp4_stop3", tp=0.04, stop=0.03))
    pols.append(replace(b, name="tp10", tp=0.10))
    # combinations
    pols.append(replace(b, name="stop3_time3s_trail4_3", stop=0.03, time_stop_s=3, time_stop_min_ret=0.0, tp=0.12, trail_activate=0.04, trail_dist=0.03))
    pols.append(replace(b, name="stop4_sp3s_r1_tick250", stop=0.04, sp_window_s=3, sp_ratio=1.0, sp_min_sells=2, tick_ms=250))
    pols.append(replace(b, name="stop3_trail4_3_tick0", stop=0.03, tp=0.12, trail_activate=0.04, trail_dist=0.03, tick_ms=0))
    # H3 event exits (fire on the print, then + latency)
    for k in (3.0, 4.0):
        pols.append(replace(b, name=f"H3_dump{k:g}mad", dump_k=k))
    pols.append(replace(b, name="H3_creator_sell", creator_sell=True))
    pols.append(replace(b, name="H3_dump4mad+creator_sell", dump_k=4.0, creator_sell=True))
    # H4 exits on our entries (gate creator_buy>=1 applied via the gate grid)
    pols.append(replace(b, name="H4_tp30_sl30_t120", tp=0.30, stop=0.30, max_hold_s=120.0))
    # Latency stress: entry re-filled and exits filled 1 s / 2 s after the decision
    for lat in (1000, 2000):
        pols.append(replace(b, name=f"baseline_lat{lat // 1000}s", entry_delay_ms=lat, exit_delay_ms=lat))
        pols.append(replace(b, name=f"H3_dump4mad_lat{lat // 1000}s", dump_k=4.0, entry_delay_ms=lat, exit_delay_ms=lat))
        pols.append(replace(b, name=f"trail4_3_tp12_lat{lat // 1000}s", tp=0.12, trail_activate=0.04, trail_dist=0.03, entry_delay_ms=lat, exit_delay_ms=lat))
    return pols


def gate_grid(recs: Sequence[Mapping[str, Any]]) -> list[Gate]:
    """Liquidity / flow gates at in-sample terciles (fit on the records given)."""
    gates = [Gate("all")]
    specs = [
        ("progress", "features.curve.progress_bps"),
        ("sol_in_curve", "features.curve.real_sol_reserves"),
        ("trades5s", "features.tape.5s.trades"),
        ("trades30s", "features.tape.30s.trades"),
        ("buysol30s", "features.tape.30s.buy_sol"),
        ("netsol15s", "features.tape.15s.net_sol"),
        ("buyers30s", "features.tape.30s.unique_buyers"),
        ("mom5s", "features.momentum.5s_bps"),
        ("age", "features.token.age_s"),
    ]
    for label, path in specs:
        vals = sorted(float(v) for v in (feat(r, path) for r in recs) if v is not None)
        if len(vals) < 30:
            continue
        q1, q2 = vals[len(vals) // 3], vals[2 * len(vals) // 3]
        gates.append(Gate(f"{label}<{q1:.4g}", path, None, q1))
        gates.append(Gate(f"{label}[{q1:.4g},{q2:.4g})", path, q1, q2))
        gates.append(Gate(f"{label}>={q2:.4g}", path, q2, None))
    gates.append(Gate("creator_buy>=1SOL", "features.creator_buy.sol", 1.0, None))
    gates.append(Gate("bundle<40", "entry_factors.bundle_pct", None, 40.0))
    gates.append(Gate("top10<40", "entry_factors.top10_pct", None, 40.0))
    return gates


def run_grid(
    recs: Sequence[Mapping[str, Any]],
    policies: Sequence[ExitPolicy],
    gates: Sequence[Gate],
    *,
    split: float = 0.5,
    bootstrap: bool = False,
) -> list[dict[str, Any]]:
    is_recs, oos_recs = time_split(recs, split)
    out = []
    cache: dict[tuple[str, str], Optional[dict]] = {}

    def sim(rec, pol):
        key = (f"{rec.get('mint')}@{(rec.get('entry') or {}).get('fill_ts')}", pol.name)
        if key not in cache:
            cache[key] = simulate_policy(rec, pol)
        return cache[key]

    for gate in gates:
        g_is = [r for r in is_recs if gate.ok(r)]
        g_oos = [r for r in oos_recs if gate.ok(r)]
        for pol in policies:
            rows_is = [x for x in (sim(r, pol) for r in g_is) if x]
            rows_oos = [x for x in (sim(r, pol) for r in g_oos) if x]
            na = sum(1 for x in rows_is + rows_oos if x.get("status") == "not_applicable")
            out.append(
                {
                    "gate": gate.name,
                    "policy": pol.name,
                    "is": stats(rows_is, bootstrap=bootstrap),
                    "oos": stats(rows_oos, bootstrap=bootstrap),
                    "all": stats(rows_is + rows_oos, bootstrap=bootstrap),
                    "not_applicable": na,
                }
            )
    return out


def _fmt(s: Mapping[str, Any]) -> str:
    if not s.get("n"):
        return "n=0"
    return (
        f"n={s['n']} mean {s['mean_bps']:+.0f}±{s['se_bps']:.0f} [{s['ci_lo_bps']:+.0f},{s['ci_hi_bps']:+.0f}] "
        f"med {s['median_bps']:+.0f} win {s['win_rate']*100:.0f}% net {s['net_sol']:+.4f} ex3 {s['mean_bps_ex_top3']:+.0f}"
        f" te {s['tape_end_rate']*100:.0f}%"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    from app.paper.evidence import evidence_files

    ap = argparse.ArgumentParser(prog="python -m app.paper.exit_study", description=__doc__.split("\n\n")[0])
    ap.add_argument("--evidence", nargs="*", type=Path)
    ap.add_argument("--json", type=Path, help="write the full grid as JSON")
    ap.add_argument("--top", type=int, default=15, help="rows to print, ranked by in-sample mean")
    ap.add_argument("--min-n", type=int, default=30, help="minimum in-sample n to rank")
    ap.add_argument("--split", type=float, default=0.5, help="in-sample fraction (time ordered)")
    ap.add_argument("--prio-fee-sol", type=float, default=0.0, help="priority fee per transaction")
    ap.add_argument("--bootstrap", action="store_true", help="bootstrap 95%% CIs (slower)")
    args = ap.parse_args(argv)
    paths = list(args.evidence) if args.evidence else evidence_files()
    recs = [r for r in iter_records(paths) if r.get("market_source") == "real"]
    if not recs:
        print("no evidence records", file=sys.stderr)
        return 1
    is_recs, _ = time_split(recs, args.split)
    pols = [replace(p, prio_fee_sol=args.prio_fee_sol) for p in default_policies()]
    grid = run_grid(recs, pols, gate_grid(is_recs), split=args.split, bootstrap=args.bootstrap)
    if args.json:
        args.json.write_text(json.dumps({"n_records": len(recs), "grid": grid, "policies": [asdict(p) for p in default_policies()]}, indent=1, default=float))
    base = next(g for g in grid if g["gate"] == "all" and g["policy"] == "baseline")
    print(f"records {len(recs)}  (in-sample first {args.split:.0%} by signal time, out-of-sample the rest)")
    print(f"baseline  IS {_fmt(base['is'])}\n          OOS {_fmt(base['oos'])}")
    ranked = sorted((g for g in grid if g["is"].get("n", 0) >= args.min_n), key=lambda g: -g["is"]["mean_bps"])
    for g in ranked[: args.top]:
        print(f"{g['gate']:<28} {g['policy']:<26} IS {_fmt(g['is'])}\n{'':<55}OOS {_fmt(g['oos'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
