"""Offline replay of stored paper-trade evidence with different exit rules.

    python -m app.paper.replay [--evidence FILE ...] [--stop 0.05] [--tp 0.06]
        [--exit-delay-ms 300] [--entry-delay-ms N] [--max-hold 120]
        [--tick-ms 1000] [--fee-bps 125] [--notional SOL] [--per-trade] [--json]
        [--where "top10_pct<=30" ...] [--keep-null] [--split "dev_pct>10" ...]
        [--bucket "top10_pct:10,20,30,50" ...]

Reads the JSONL written by :mod:`app.paper.evidence` (the active file plus its
rotations by default) and re-simulates each trade on its stored tape of real
prints. Model, mirroring the paper engine:

* exits are checked on a tick grid (``--tick-ms``; 0 = after every print)
  anchored on the recorded exit signal, using the mark after the latest print;
  take-profit / stop-loss compare the mark with the entry fill price, then
  ``--max-hold`` seconds after the entry fill;
* an order fills on the first print at or after ``signal + delay`` against the
  reserves left by that print (constant-product curve, fees on top), like the
  engine's deferred real fills. No print before the tape ends → the trade is
  closed at the last print and flagged ``tape_end``;
* ``--entry-delay-ms`` re-fills the entry the same way; by default the recorded
  entry fill is reused.

Entry-factor filters (``entry_factors`` of evidence v2, see
:mod:`app.paper.entry_factors`): ``--where KEY OP VALUE`` keeps only matching
trades (all ``--where`` must hold; a null factor fails unless ``--keep-null``),
``--split EXPR`` reports expectancy for matching / not matching / null, and
``--bucket KEY:E1,E2,..`` reports it per value range. KEY is a factor name
(``dev_pct``, ``top10_pct``, ``holder_count``, ``bundle_pct``, ``sniper_pct``,
``mint_authority_revoked`` ...) or a dotted path such as ``tape.top10_pct`` /
``rpc.curve_pct``. OP is one of ``> >= < <= == !=``; VALUE a number,
``true`` / ``false`` or ``null``.

Not modelled: sell-pressure / graduation / impact-split exits, competition for
block space, failed transactions. Paper only; never sends anything.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from app.paper.real_fill import quote_curve_fill
from app.providers.pumpfun_curve_math import (
    INITIAL_REAL_TOKEN_RESERVES,
    INITIAL_VIRTUAL_TOKEN_RESERVES,
    price_sol,
)

_VT_OFFSET = INITIAL_VIRTUAL_TOKEN_RESERVES - INITIAL_REAL_TOKEN_RESERVES


def iter_records(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    for path in paths:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") == "paper_trade_evidence":
                    yield rec


def _prints(rec: Mapping[str, Any]) -> list[tuple[int, int, int]]:
    """Absolute (ts, vs, vt) per stored print, time-ordered."""
    tape = rec.get("tape") or {}
    t0 = int(tape.get("t0") or 0)
    out = [(t0 + int(r[0]), int(r[3]), int(r[4])) for r in tape.get("rows") or [] if len(r) >= 5]
    out.sort(key=lambda x: x[0])
    return out


def _first_at_or_after(prints: Sequence[tuple[int, int, int]], ts: int) -> Optional[tuple[int, int, int]]:
    for p in prints:
        if p[0] >= ts:
            return p
    return None


def _mark_at(prints: Sequence[tuple[int, int, int]], ts: int) -> Optional[float]:
    last = None
    for p in prints:
        if p[0] > ts:
            break
        last = p
    return price_sol(last[1], last[2]) if last else None


def _fill(side: str, p: tuple[int, int, int], *, notional: float = 0.0, qty: float = 0.0, fee_bps: int) -> Optional[tuple[float, float, float]]:
    _ts, vs, vt = p
    q = quote_curve_fill(
        side=side,
        notional_sol=notional,
        virtual_sol_reserves=vs,
        virtual_token_reserves=vt,
        real_token_reserves=max(1, vt - _VT_OFFSET),
        flatten_qty=qty if side == "sell" else None,
        protocol_fee_bps=int(fee_bps),
        creator_fee_bps=0,
        mid=price_sol(vs, vt),
    )
    if q is None:
        return None
    px, abs_qty, fee, _slip = q
    return px, abs_qty, fee


def simulate(
    rec: Mapping[str, Any],
    *,
    stop: float,
    tp: float,
    exit_delay_ms: int = 300,
    entry_delay_ms: Optional[int] = None,
    max_hold_s: float = 120.0,
    tick_ms: int = 1000,
    fee_bps: int = 125,
    notional: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Re-simulate one evidence record. None when the record cannot be replayed."""
    prints = _prints(rec)
    entry = rec.get("entry") or {}
    if not prints or not entry.get("fill_ts"):
        return None
    # ---- entry
    if entry_delay_ms is None and notional is None:
        e_ts = int(entry["fill_ts"])
        e_px = float(entry["fill_price"])
        e_qty = float(entry["fill_qty"])
        e_fee = float(entry.get("fill_fee") or 0.0)
    else:
        sig = int(entry.get("signal_ts") or entry["fill_ts"])
        delay = int(entry_delay_ms if entry_delay_ms is not None else (int(entry["fill_ts"]) - sig))
        p = _first_at_or_after(prints, sig + delay)
        size = float(notional if notional is not None else entry.get("notional_sol") or 0.0)
        got = _fill("buy", p, notional=size, fee_bps=fee_bps) if p and size > 0 else None
        if got is None:
            return {"id": _rid(rec), "status": "no_entry"}
        e_px, e_qty, e_fee = got
        e_ts = p[0]
    cost = e_px * e_qty + e_fee
    # ---- exit trigger on a tick grid
    anchor = int((rec.get("exit") or {}).get("signal_ts") or e_ts)
    last_ts = prints[-1][0]
    if tick_ms > 0:
        k = -((anchor - e_ts) // tick_ms)  # first grid point at/after the entry fill
        checks: Iterable[int] = (anchor + (k + i) * tick_ms for i in range(0, max(1, (last_ts - e_ts) // tick_ms + 2)))
    else:
        checks = (p[0] for p in prints if p[0] >= e_ts)
    reason = None
    sig_ts = None
    trig = None
    for t in checks:
        if t < e_ts:
            continue
        if t > last_ts:
            break
        mark = _mark_at(prints, t)
        if mark is None:
            continue
        ret = mark / e_px - 1.0
        if ret >= tp:
            reason = "take_profit"
        elif ret <= -stop:
            reason = "stop_loss"
        elif (t - e_ts) / 1000.0 >= max_hold_s:
            reason = "max_hold"
        if reason:
            sig_ts, trig = t, mark
            break
    flag = None
    if reason is None:
        reason, sig_ts, trig, flag = "tape_end", last_ts, _mark_at(prints, last_ts), "tape_end"
    p = _first_at_or_after(prints, int(sig_ts) + int(exit_delay_ms))
    if p is None:
        p, flag = prints[-1], "tape_end"
    got = _fill("sell", p, qty=e_qty, fee_bps=fee_bps)
    if got is None:
        return {"id": _rid(rec), "status": "no_exit"}
    x_px, _q, x_fee = got
    net = x_px * e_qty - x_fee - cost
    return {
        "id": _rid(rec),
        "status": "ok",
        "symbol": rec.get("symbol"),
        "mint": rec.get("mint"),
        "entry_ts": e_ts,
        "exit_signal_ts": sig_ts,
        "exit_fill_ts": p[0],
        "reason": reason,
        "flag": flag,
        "trigger_price": trig,
        "exit_price": x_px,
        "fill_vs_trigger_bps": (x_px / trig - 1.0) * 1e4 if trig else None,
        "cost_sol": cost,
        "net_sol": net,
        "net_bps": net / cost * 1e4 if cost else None,
        "recorded_net_bps": (rec.get("result") or {}).get("net_bps"),
        "recorded_reason": (rec.get("exit") or {}).get("reason"),
    }


def _rid(rec: Mapping[str, Any]) -> str:
    return f"{rec.get('mint')}@{(rec.get('entry') or {}).get('fill_ts')}"


def summarize(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ok = [r for r in results if r.get("status") == "ok"]
    bps = [float(r["net_bps"]) for r in ok if r.get("net_bps") is not None]
    rec_bps = [float(r["recorded_net_bps"]) for r in ok if r.get("recorded_net_bps") is not None]
    by: dict[str, dict[str, Any]] = {}
    for r in ok:
        b = by.setdefault(str(r["reason"]), {"n": 0, "net_sol": 0.0, "bps": []})
        b["n"] += 1
        b["net_sol"] += float(r["net_sol"])
        b["bps"].append(float(r["net_bps"] or 0.0))
    return {
        "n_records": len(results),
        "n_replayed": len(ok),
        "n_skipped": len(results) - len(ok),
        "n_tape_end": sum(1 for r in ok if r.get("flag") == "tape_end"),
        "win_rate": (sum(1 for x in bps if x > 0) / len(bps)) if bps else None,
        "mean_net_bps": statistics.fmean(bps) if bps else None,
        "median_net_bps": statistics.median(bps) if bps else None,
        "total_net_sol": sum(float(r["net_sol"]) for r in ok),
        "recorded_mean_net_bps": statistics.fmean(rec_bps) if rec_bps else None,
        "by_reason": {
            k: {"n": v["n"], "net_sol": v["net_sol"], "mean_net_bps": statistics.fmean(v["bps"])}
            for k, v in sorted(by.items())
        },
    }


_OPS = (">=", "<=", "!=", "==", ">", "<")


def parse_expr(text: str) -> tuple[str, str, Any]:
    """``"top10_pct>=30"`` → ``("top10_pct", ">=", 30.0)``."""
    raw = str(text).strip()
    for op in _OPS:
        if op in raw:
            key, val = raw.split(op, 1)
            key, val = key.strip(), val.strip()
            if not key or not val:
                break
            low = val.lower()
            value: Any
            if low in {"true", "false"}:
                value = low == "true"
            elif low in {"null", "none"}:
                value = None
            else:
                value = float(val)
            return key, op, value
    raise ValueError(f"bad factor expression: {text!r} (use KEY OP VALUE, OP in {' '.join(_OPS)})")


def factor_value(factors: Optional[Mapping[str, Any]], key: str) -> Any:
    cur: Any = factors or {}
    for part in key.split("."):
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(part)
    return cur


def match_expr(factors: Optional[Mapping[str, Any]], expr: tuple[str, str, Any]) -> Optional[bool]:
    """True / False, or None when the factor is null (unknown)."""
    key, op, want = expr
    got = factor_value(factors, key)
    if want is None:
        return (got is None) if op == "==" else (got is not None) if op == "!=" else None
    if got is None:
        return None
    if isinstance(want, bool) or isinstance(got, bool):
        if op not in {"==", "!="}:
            return None
        same = bool(got) == bool(want)
        return same if op == "==" else not same
    try:
        g, w = float(got), float(want)
    except (TypeError, ValueError):
        return None
    return {">": g > w, ">=": g >= w, "<": g < w, "<=": g <= w, "==": g == w, "!=": g != w}[op]


def parse_bucket(text: str) -> tuple[str, list[float]]:
    key, _, edges = str(text).partition(":")
    vals = sorted(float(x) for x in edges.split(",") if x.strip())
    if not key.strip() or not vals:
        raise ValueError(f"bad bucket spec: {text!r} (use KEY:E1,E2,...)")
    return key.strip(), vals


def bucket_label(value: Any, edges: Sequence[float]) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "null"
    if v < edges[0]:
        return f"<{edges[0]:g}"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return f"[{lo:g},{hi:g})"
    return f">={edges[-1]:g}"


def _stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r.get("status") == "ok" and r.get("net_bps") is not None]
    bps = [float(r["net_bps"]) for r in ok]
    return {
        "n": len(bps),
        "win_rate": (sum(1 for x in bps if x > 0) / len(bps)) if bps else None,
        "mean_net_bps": statistics.fmean(bps) if bps else None,
        "median_net_bps": statistics.median(bps) if bps else None,
        "se_bps": (statistics.stdev(bps) / len(bps) ** 0.5) if len(bps) > 1 else None,
        "total_net_sol": sum(float(r.get("net_sol") or 0.0) for r in ok),
    }


def filter_results(
    results: Sequence[Mapping[str, Any]], wheres: Sequence[tuple[str, str, Any]], *, keep_null: bool = False
) -> list[Mapping[str, Any]]:
    out = []
    for r in results:
        verdicts = [match_expr(r.get("factors"), w) for w in wheres]
        if all(v is True or (v is None and keep_null) for v in verdicts):
            out.append(r)
    return out


def split_report(results: Sequence[Mapping[str, Any]], expr: tuple[str, str, Any]) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = {"match": [], "no_match": [], "null": []}
    for r in results:
        v = match_expr(r.get("factors"), expr)
        groups["null" if v is None else "match" if v else "no_match"].append(r)
    return {k: _stats(v) for k, v in groups.items()}


def bucket_report(results: Sequence[Mapping[str, Any]], key: str, edges: Sequence[float]) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    order = [f"<{edges[0]:g}"] + [f"[{a:g},{b:g})" for a, b in zip(edges, edges[1:])] + [f">={edges[-1]:g}"]
    for r in results:
        groups.setdefault(bucket_label(factor_value(r.get("factors"), key), edges), []).append(r)
    keys = [k for k in order if k in groups] + sorted(k for k in groups if k not in order)
    return {k: _stats(groups[k]) for k in keys}


def main(argv: Optional[Sequence[str]] = None) -> int:
    from app.paper.evidence import evidence_files

    ap = argparse.ArgumentParser(prog="python -m app.paper.replay", description=__doc__.split("\n\n")[0])
    ap.add_argument("--evidence", nargs="*", type=Path, help="JSONL files (default: active + rotated evidence files)")
    ap.add_argument("--stop", type=float, default=0.05, help="stop-loss fraction (0.05 = 5%%)")
    ap.add_argument("--tp", type=float, default=0.06, help="take-profit fraction")
    ap.add_argument("--exit-delay-ms", type=int, default=300)
    ap.add_argument("--entry-delay-ms", type=int, default=None, help="re-fill the entry after this delay")
    ap.add_argument("--max-hold", type=float, default=120.0, help="seconds")
    ap.add_argument("--tick-ms", type=int, default=1000, help="exit check period; 0 = every print")
    ap.add_argument("--fee-bps", type=int, default=125, help="curve fee per side (protocol+creator)")
    ap.add_argument("--notional", type=float, default=None, help="re-size entries (SOL)")
    ap.add_argument("--per-trade", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--where", action="append", default=[], help="keep trades whose entry factor matches, e.g. top10_pct<=30")
    ap.add_argument("--keep-null", action="store_true", help="--where keeps trades whose factor is null")
    ap.add_argument("--split", action="append", default=[], help="report match / no-match / null for EXPR")
    ap.add_argument("--bucket", action="append", default=[], help="report per range, e.g. dev_pct:1,5,10")
    args = ap.parse_args(argv)
    try:
        wheres = [parse_expr(w) for w in args.where]
        splits = [(w, parse_expr(w)) for w in args.split]
        buckets = [(b, *parse_bucket(b)) for b in args.bucket]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    paths = list(args.evidence) if args.evidence else evidence_files()
    if not paths:
        print("no evidence files found", file=sys.stderr)
        return 1
    results = []
    for rec in iter_records(paths):
        out = simulate(
            rec,
            stop=args.stop,
            tp=args.tp,
            exit_delay_ms=args.exit_delay_ms,
            entry_delay_ms=args.entry_delay_ms,
            max_hold_s=args.max_hold,
            tick_ms=args.tick_ms,
            fee_bps=args.fee_bps,
            notional=args.notional,
        )
        row = dict(out) if out else {"id": _rid(rec), "status": "no_tape"}
        row["factors"] = rec.get("entry_factors")
        results.append(row)
    n_all = len(results)
    if wheres:
        results = filter_results(results, wheres, keep_null=args.keep_null)
    summary = summarize(results)
    summary["n_before_filter"] = n_all
    summary["splits"] = {text: split_report(results, expr) for text, expr in splits}
    summary["buckets"] = {text: bucket_report(results, key, edges) for text, key, edges in buckets}
    summary["config"] = {
        k: v for k, v in vars(args).items() if k not in {"evidence", "per_trade", "json", "split", "bucket"}
    }
    summary["files"] = [str(p) for p in paths]
    if args.json:
        payload: dict[str, Any] = {"summary": summary}
        if args.per_trade:
            payload["trades"] = results
        print(json.dumps(payload, ensure_ascii=False, indent=1, default=float))
        return 0
    s = summary
    def fmt(x: Any, nd: int = 1) -> str:
        return "n/a" if x is None else f"{x:.{nd}f}"
    if wheres:
        print(f"filter  {' AND '.join(args.where)}{' (null kept)' if args.keep_null else ''}: {s['n_records']} of {n_all} records")
    print(f"records {s['n_records']}  replayed {s['n_replayed']}  skipped {s['n_skipped']}  tape_end {s['n_tape_end']}")
    print(f"config  {json.dumps(s['config'])}")
    print(
        f"win {fmt((s['win_rate'] or 0) * 100 if s['win_rate'] is not None else None)}%  "
        f"mean {fmt(s['mean_net_bps'])} bps  median {fmt(s['median_net_bps'])} bps  "
        f"total {s['total_net_sol']:+.6f} SOL  (recorded mean {fmt(s['recorded_mean_net_bps'])} bps)"
    )
    for k, v in s["by_reason"].items():
        print(f"  {k:<12} n={v['n']:<4} mean {v['mean_net_bps']:.1f} bps  net {v['net_sol']:+.6f} SOL")
    def line(label: str, st: Mapping[str, Any]) -> str:
        win = f"{st['win_rate'] * 100:.0f}%" if st["win_rate"] is not None else "n/a"
        return (
            f"    {label:<14} n={st['n']:<4} win {win:>4}  mean {fmt(st['mean_net_bps'])} bps"
            f"  (se {fmt(st['se_bps'])})  median {fmt(st['median_net_bps'])}  net {st['total_net_sol']:+.6f} SOL"
        )
    for text, groups in s["splits"].items():
        print(f"split {text}")
        for label, st in groups.items():
            print(line(label, st))
    for text, groups in s["buckets"].items():
        print(f"bucket {text}")
        for label, st in groups.items():
            print(line(label, st))
    if args.per_trade:
        for r in results:
            if r.get("status") != "ok":
                print(f"  {r['id']}  {r.get('status')}")
                continue
            print(
                f"  {r['symbol']:<14} {r['reason']:<11} net {r['net_bps']:+8.1f} bps"
                f"  (recorded {fmt(r['recorded_net_bps'])} {r['recorded_reason']})"
                + (f"  [{r['flag']}]" if r.get("flag") else "")
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
