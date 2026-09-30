"""Offline launch-strategy study on record-only launch tapes (shadow candidates).

    python -m app.paper.launch_study [--tapes FILE ...] [--split 0.7] [--json OUT]

Reads ``launch_tapes.jsonl`` (:mod:`app.paper.launch_tape`) and simulates
launch-time entry rules the live strategy does not use, without touching it:

* **H2 liquidity speed**: enter when virtual SOL first reaches ``vsol`` (50 /
  80 SOL) within at most ``max_trades`` prints since Create; exit before
  graduation (virtual SOL >= ``exit_vsol``, 105 / 110) or after ``timeout_s``;
* **H4 creator buy**: enter after the creator's buy of >= ``min_creator_sol``
  (1 SOL); +30 % TP, -30 % stop, 120 s max hold;
* **H5 vetoes**: skip launches with non-creator buys in the Create slot, or a
  low share of non-bot prints before entry (bots = wallets buying within 1
  slot of Create in >= 3 launches of the fit split);
* **H1 creator quality (partial)**: creator buy >= 2 % of supply and <= 4 SOL,
  no bundled launch block, not an ALL-CAPS name, no prior launch by the same
  creator in the data. Wallet age and funding-source checks are not computed
  (need per-creator history/funding traces; not reliable on the free RPC at
  ~50k launches/day) and are reported as such.

Fill model: an order fills on the first print at or after decision +
``latency_ms`` (1 s / 2 s) at that print's curve reserves (constant-product,
0.03 SOL, 125 bps fee per side) plus ``prio_fee_sol`` per transaction; no
fills after graduation. Tapes end at 180 s, graduation or watch-list
eviction; a position still open then is closed at the last print and counted
as ``tape_end``. Paper research only.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from app.paper.exit_study import bootstrap_ci, _mean_ex_top
from app.paper.replay import _fill
from app.providers.pumpfun_curve_math import INITIAL_VIRTUAL_TOKEN_RESERVES, TOKEN_TOTAL_SUPPLY, price_sol

LAMPORTS = 1_000_000_000
_VT_OFFSET = INITIAL_VIRTUAL_TOKEN_RESERVES - 793_100_000_000_000


def iter_tapes(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") == "launch_tape" and rec.get("rows"):
                    yield rec


@dataclass(frozen=True)
class LaunchPolicy:
    name: str
    entry: str = "speed"  # "speed" | "creator_buy"
    vsol: float = 50.0
    max_trades: Optional[int] = None
    min_creator_sol: float = 1.0
    exit_vsol: Optional[float] = 105.0
    timeout_s: float = 120.0
    tp: Optional[float] = None
    stop: Optional[float] = None
    latency_ms: int = 1000
    tick_ms: int = 1000
    notional: float = 0.03
    fee_bps: int = 125
    prio_fee_sol: float = 0.0
    veto_bundle: bool = False
    veto_bot_share: Optional[float] = None
    creator_quality: bool = False


def _rows(rec: Mapping[str, Any]) -> list[tuple[int, int, float, int, int, Optional[int], Optional[str]]]:
    t0 = int(rec.get("t0") or 0)
    return [(t0 + int(r[0]), int(r[1]), float(r[2] or 0), int(r[3]), int(r[4]), r[5], r[6]) for r in rec["rows"]]


def _live(r) -> bool:
    return r[4] - _VT_OFFSET > 0


def _first_live_ge(rows, ts: int):
    for r in rows:
        if r[0] >= ts and _live(r):
            return r
    return None


def launch_features(rec: Mapping[str, Any]) -> dict[str, Any]:
    """Creator buy (SOL, % supply), launch-block buys, name caps. Pure."""
    rows = _rows(rec)
    c8 = rec.get("creator8")
    prev_vt = INITIAL_VIRTUAL_TOKEN_RESERVES
    creator_sol = 0.0
    creator_tokens = 0
    block_foreign = 0
    for r in rows:
        tokens = abs(prev_vt - r[4])
        prev_vt = r[4]
        if r[1] > 0 and c8 and r[6] == c8 and r[5] in (0, None) and creator_tokens == 0:
            creator_sol, creator_tokens = r[2], tokens
        if r[1] > 0 and r[5] == 0 and r[6] != c8:
            block_foreign += 1
    name = str(rec.get("name") or "")
    letters = [ch for ch in name if ch.isalpha()]
    return {
        "creator_buy_sol": creator_sol,
        "creator_buy_pct": 100.0 * creator_tokens / TOKEN_TOTAL_SUPPLY,
        "launch_block_foreign_buys": block_foreign if rec.get("created_slot") is not None else None,
        "all_caps_name": bool(letters) and all(ch.isupper() for ch in letters),
    }


def bot_wallets(recs: Sequence[Mapping[str, Any]], min_launches: int = 3) -> set[str]:
    seen: dict[str, set[str]] = defaultdict(set)
    for rec in recs:
        for r in rec["rows"]:
            if r[1] > 0 and r[5] is not None and r[5] <= 1 and r[6] and r[6] != rec.get("creator8"):
                seen[r[6]].add(rec["mint"])
    return {w for w, mints in seen.items() if len(mints) >= min_launches}


def simulate_launch(
    rec: Mapping[str, Any],
    pol: LaunchPolicy,
    *,
    bots: Optional[set[str]] = None,
    prior_launches: int = 0,
) -> Optional[dict[str, Any]]:
    rows = _rows(rec)
    if not rows:
        return None
    feats = launch_features(rec)
    # ---- entry trigger
    trig = None
    if pol.entry == "speed":
        for i, r in enumerate(rows):
            if r[3] >= pol.vsol * LAMPORTS:
                if pol.max_trades is None or i + 1 <= pol.max_trades:
                    trig = r[0]
                break
    elif pol.entry == "creator_buy":
        if feats["creator_buy_sol"] >= pol.min_creator_sol:
            c8 = rec.get("creator8")
            trig = next((r[0] for r in rows if r[1] > 0 and r[6] == c8), None)
    if trig is None:
        return None
    # ---- vetoes / filters (only information available at the trigger)
    if pol.veto_bundle or pol.creator_quality:
        if feats["launch_block_foreign_buys"] is None:
            return {"status": "veto", "why": "no_slot"}
        if feats["launch_block_foreign_buys"] > 0:
            return {"status": "veto", "why": "bundled_launch"}
    if pol.veto_bot_share is not None and bots is not None:
        pre = [r for r in rows if r[0] <= trig and r[6]]
        share = (sum(1 for r in pre if r[6] not in bots) / len(pre)) if pre else 1.0
        if share < pol.veto_bot_share:
            return {"status": "veto", "why": "bot_share"}
    if pol.creator_quality:
        if not (2.0 <= feats["creator_buy_pct"] and feats["creator_buy_sol"] <= 4.0):
            return {"status": "veto", "why": "creator_buy_size"}
        if feats["all_caps_name"]:
            return {"status": "veto", "why": "all_caps"}
        if prior_launches > 0:
            return {"status": "veto", "why": "repeat_creator"}
    # ---- entry fill
    p = _first_live_ge(rows, trig + pol.latency_ms)
    if p is None:
        return {"status": "no_entry"}
    got = _fill("buy", (p[0], p[3], p[4]), notional=pol.notional, fee_bps=pol.fee_bps)
    if got is None:
        return {"status": "no_entry"}
    e_px, qty, e_fee = got
    e_ts = p[0]
    cost = e_px * qty + e_fee + pol.prio_fee_sol
    # ---- exit
    last_live = [r for r in rows if _live(r)]
    last_ts = rows[-1][0]
    reason = None
    t = e_ts
    while t <= last_ts:
        t += pol.tick_ms
        marks = [r for r in rows if r[0] <= t]
        if not marks:
            continue
        m = marks[-1]
        ret = price_sol(m[3], m[4]) / e_px - 1.0
        if not _live(m):
            reason = "graduated"
            break
        if pol.exit_vsol is not None and m[3] >= pol.exit_vsol * LAMPORTS:
            reason = "pre_graduation"
        elif pol.tp is not None and ret >= pol.tp:
            reason = "take_profit"
        elif pol.stop is not None and ret <= -pol.stop:
            reason = "stop_loss"
        elif (t - e_ts) / 1000.0 >= pol.timeout_s:
            reason = "timeout"
        if reason:
            break
    flag = None
    if reason == "graduated":
        x = last_live[-1] if last_live else p
        flag = "graduated_no_curve_fill"
    else:
        x = _first_live_ge(rows, (t if reason else last_ts) + pol.latency_ms) if reason else None
        if x is None:
            x = last_live[-1] if last_live else p
            flag = "tape_end"
            reason = reason or "tape_end"
    got = _fill("sell", (x[0], x[3], x[4]), qty=qty, fee_bps=pol.fee_bps)
    if got is None:
        return {"status": "no_exit"}
    x_px, _q, x_fee = got
    net = x_px * qty - x_fee - pol.prio_fee_sol - cost
    return {
        "status": "ok",
        "mint": rec.get("mint"),
        "t0": int(rec.get("t0") or 0),
        "entry_age_s": (e_ts - int(rec.get("t0") or 0)) / 1000.0,
        "hold_s": (x[0] - e_ts) / 1000.0,
        "reason": reason,
        "flag": flag,
        "cost_sol": cost,
        "net_sol": net,
        "net_bps": net / cost * 1e4,
    }


def lstats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r and r.get("status") == "ok"]
    bps = [float(r["net_bps"]) for r in ok]
    n = len(bps)
    out: dict[str, Any] = {"n": n, "vetoed": sum(1 for r in rows if r and r.get("status") == "veto")}
    if not n:
        return out
    se = statistics.stdev(bps) / math.sqrt(n) if n > 1 else float("nan")
    lo, hi = bootstrap_ci(bps)
    out.update(
        {
            "mean_bps": statistics.fmean(bps),
            "se_bps": se,
            "boot_lo_bps": lo,
            "boot_hi_bps": hi,
            "median_bps": statistics.median(bps),
            "win_rate": sum(1 for x in bps if x > 0) / n,
            "net_sol": sum(float(r["net_sol"]) for r in ok),
            "mean_bps_ex_top1pct": _mean_ex_top(bps, max(1, math.ceil(0.01 * n))),
            "mean_bps_ex_top3": _mean_ex_top(bps, 3),
            "tape_end_rate": sum(1 for r in ok if r.get("flag")) / n,
            "reasons": dict(Counter(r["reason"] for r in ok)),
        }
    )
    return out


def default_launch_policies() -> list[LaunchPolicy]:
    out = []
    for lat in (1000, 2000):
        for v in (50.0, 80.0):
            for mt in (None, 100, 300):
                for ex in (105.0, 110.0):
                    for to in (60.0, 120.0):
                        out.append(LaunchPolicy(name=f"H2_v{int(v)}_n{mt or 'any'}_x{int(ex)}_t{int(to)}_lat{lat}", vsol=v, max_trades=mt, exit_vsol=ex, timeout_s=to, latency_ms=lat))
        h4 = LaunchPolicy(name=f"H4_cb1_tp30_sl30_t120_lat{lat}", entry="creator_buy", min_creator_sol=1.0, exit_vsol=None, tp=0.30, stop=0.30, timeout_s=120.0, latency_ms=lat)
        out.append(h4)
        out.append(replace(h4, name=f"H4+H5_lat{lat}", veto_bundle=True, veto_bot_share=0.5))
        out.append(replace(h4, name=f"H4+H1_lat{lat}", creator_quality=True))
        h2 = LaunchPolicy(name=f"H2_v50_n100_x105_t120+H5_lat{lat}", vsol=50.0, max_trades=100, exit_vsol=105.0, timeout_s=120.0, latency_ms=lat, veto_bundle=True, veto_bot_share=0.5)
        out.append(h2)
        out.append(replace(h2, name=f"H2_v50_n100_x105_t120+H1_lat{lat}", veto_bot_share=None, creator_quality=True))
    return out


def run(recs: Sequence[Mapping[str, Any]], policies: Sequence[LaunchPolicy], *, split: float = 0.7, prio_fee_sol: float = 0.0) -> dict[str, Any]:
    recs = sorted(recs, key=lambda r: int(r.get("t0") or 0))
    cut = int(len(recs) * split)
    fit, val = recs[:cut], recs[cut:]
    bots = bot_wallets(fit)
    prior: dict[str, int] = {}
    counts: dict[str, int] = defaultdict(int)
    for r in recs:
        c = r.get("creator") or ""
        prior[r["mint"]] = counts[c] if c else 0
        if c:
            counts[c] += 1
    table = []
    for pol in policies:
        pol = replace(pol, prio_fee_sol=prio_fee_sol)
        a = [simulate_launch(r, pol, bots=bots, prior_launches=prior[r["mint"]]) for r in fit]
        b = [simulate_launch(r, pol, bots=bots, prior_launches=prior[r["mint"]]) for r in val]
        table.append({"policy": pol.name, "fit": lstats([x for x in a if x]), "validate": lstats([x for x in b if x])})
    return {
        "n_tapes": len(recs),
        "n_fit": len(fit),
        "n_validate": len(val),
        "bots": len(bots),
        "end_reasons": dict(Counter(r.get("end_reason") for r in recs)),
        "prio_fee_sol": prio_fee_sol,
        "table": table,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    from app.paper.launch_tape import launch_tape_path
    from app.paper.evidence import evidence_files

    ap = argparse.ArgumentParser(prog="python -m app.paper.launch_study", description=__doc__.split("\n\n")[0])
    ap.add_argument("--tapes", nargs="*", type=Path)
    ap.add_argument("--split", type=float, default=0.7)
    ap.add_argument("--prio-fee-sol", type=float, default=0.0)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    paths = list(args.tapes) if args.tapes else evidence_files(launch_tape_path())
    recs = list(iter_tapes(paths))
    if not recs:
        print("no launch tapes", file=sys.stderr)
        return 1
    res = run(recs, default_launch_policies(), split=args.split, prio_fee_sol=args.prio_fee_sol)
    if args.json:
        args.json.write_text(json.dumps(res, indent=1, default=float))
    print(f"tapes {res['n_tapes']} (fit {res['n_fit']} / validate {res['n_validate']})  bots {res['bots']}  ends {res['end_reasons']}")

    def f(s):
        if not s.get("n"):
            return f"n=0 veto={s.get('vetoed', 0)}"
        return (f"n={s['n']} {s['mean_bps']:+.0f} [{s['boot_lo_bps'] or 0:+.0f},{s['boot_hi_bps'] or 0:+.0f}] med {s['median_bps']:+.0f} "
                f"win {s['win_rate']*100:.0f}% net {s['net_sol']:+.4f} ex1% {s['mean_bps_ex_top1pct'] or 0:+.0f} te {s['tape_end_rate']*100:.0f}%")
    for row in res["table"]:
        print(f"{row['policy']:<40} fit {f(row['fit'])} | val {f(row['validate'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
