"""Evidence logging for real-market paper trades (observation only).

For every real paper trade this records, in a sidecar JSONL next to the paper
journal, what is needed to re-check the result offline:

* signal time, the first real print after the paper latency, and the fill time,
  for both entry and exit;
* entry features at signal time (curve reserves/progress, token age, tape
  counts and SOL volume over 5/15/30/60 s, unique buyers, momentum, the
  creator's buy, market cap, discovery source);
* the exit trigger (trigger mark vs fill price) and the price path;
* a compact tape of the mint's real prints over [entry signal - 30 s, exit + post]
  (post = ``AUU_EVIDENCE_POST_MS``, default 90 s; 30 s before evidence v3);
* ``entry_factors``: holder structure / token safety at the signal, looked up
  off the execution path by ``app.paper.entry_factors``.

It never changes a decision, an order or a parameter: the strategy calls the
``on_*`` hooks after it has already acted, every hook swallows its own errors,
and nothing here is read back by the strategy. ``python -m app.paper.replay``
consumes the file. Paper only; liveEnabled is untouched.

Storage is bounded: one active file rotated at ``AUU_EVIDENCE_MAX_MB``
(default 16 MB) keeping ``AUU_EVIDENCE_KEEP`` old files (default 8), at most
``MAX_TAPE_ROWS`` prints per trade and ``MAX_EPISODES`` trades in memory.
``AUU_EVIDENCE=off`` disables it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from app.data_paths import data_dir, guarded_path
from app.providers.pumpfun_curve_math import (
    INITIAL_REAL_TOKEN_RESERVES,
    TOKEN_TOTAL_SUPPLY,
    market_cap_sol,
    price_sol,
    progress_bps,
)

log = logging.getLogger("auu.paper.evidence")

EVIDENCE_VERSION = 3  # v2: + entry_factors; v3: post-exit tape window configurable (default 90 s)
PRE_MS = 30_000
POST_MS = 30_000  # legacy default; the live value comes from post_ms()
DEFAULT_POST_MS = 90_000


def post_ms() -> int:
    """Tape kept after the exit fill (``AUU_EVIDENCE_POST_MS``, 5 s .. 10 min, default 90 s).

    Longer than the 30 s of v1/v2 so hold-longer exit variants (trailing,
    no-TP) can be replayed offline. Record only.
    """
    try:
        val = int(os.getenv("AUU_EVIDENCE_POST_MS") or DEFAULT_POST_MS)
    except ValueError:
        val = DEFAULT_POST_MS
    return max(5_000, min(600_000, val))
FEATURE_WINDOWS_S = (5, 15, 30, 60)
MAX_TAPE_ROWS = 6_000
MAX_EPISODES = 64
# Entries that never fill (no real print within the entry TTL, risk deny...) are
# dropped after this long. Open trades older than STALE_OPEN_MS (e.g. a position
# left over from before a restart) are dropped rather than kept forever.
PENDING_DROP_MS = 90_000
STALE_OPEN_MS = 30 * 60_000
TAPE_FIELDS = ("dt_ms", "side", "sol", "vs", "vt")


def evidence_enabled() -> bool:
    return (os.getenv("AUU_EVIDENCE") or "on").strip().lower() not in {"0", "off", "false", "no"}


def evidence_path() -> Path:
    raw = (os.getenv("PAPER_EVIDENCE_STORE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return data_dir() / "evidence" / "paper_evidence.jsonl"


def _env_int(name: str, default: int, lo: int) -> int:
    try:
        return max(lo, int(os.getenv(name) or default))
    except ValueError:
        return default


def max_file_bytes() -> int:
    return _env_int("AUU_EVIDENCE_MAX_MB", 16, 1) * 1024 * 1024


def keep_files() -> int:
    return _env_int("AUU_EVIDENCE_KEEP", 8, 0)


def evidence_files(path: Optional[Path] = None) -> list[Path]:
    """Active file plus rotated ones, oldest first (``.N`` … ``.1``, then active)."""
    base = path or evidence_path()
    rotated = sorted(
        (p for p in base.parent.glob(base.name + ".*") if p.suffix.lstrip(".").isdigit()),
        key=lambda p: -int(p.suffix.lstrip(".")),
    )
    return [*rotated, *([base] if base.exists() else [])]


class EvidenceWriter:
    """Append-only JSONL with size-based rotation (``file`` → ``file.1`` → … ``file.K``)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path or evidence_path()

    def _rotate(self, path: Path) -> None:
        keep = keep_files()
        if keep <= 0:
            path.unlink(missing_ok=True)
            return
        oldest = path.with_name(f"{path.name}.{keep}")
        oldest.unlink(missing_ok=True)
        for n in range(keep - 1, 0, -1):
            src = path.with_name(f"{path.name}.{n}")
            if src.exists():
                src.replace(path.with_name(f"{path.name}.{n + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))

    def write(self, record: Mapping[str, Any]) -> Path:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
        data = line.encode("utf-8")
        with self._lock:
            path = guarded_path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 0 and path.stat().st_size + len(data) > max_file_bytes():
                self._rotate(path)
            with open(path, "ab") as fh:
                fh.write(data)
        return path


def _num(val: Any) -> Optional[float]:
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _int(val: Any) -> Optional[int]:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _row_price(row: Mapping[str, Any]) -> Optional[float]:
    vs, vt = _int(row.get("virtual_sol_reserves")), _int(row.get("virtual_token_reserves"))
    if vs and vt:
        return price_sol(vs, vt)
    return _num(row.get("price"))


def _row_sol(row: Mapping[str, Any]) -> float:
    amt = _num(row.get("sol_amount")) or 0.0
    if amt <= 0:
        amt = abs((_num(row.get("price")) or 0.0) * (_num(row.get("qty")) or 0.0))
    return amt


def _sorted_prints(trades: Iterable[Mapping[str, Any]], mint: str) -> list[Mapping[str, Any]]:
    rows = [r for r in trades if not mint or not r.get("mint") or r.get("mint") == mint]
    rows.sort(key=lambda r: (int(r.get("ts") or 0), int(r.get("seq") or 0)))
    return rows


def entry_features(
    trades: Iterable[Mapping[str, Any]],
    *,
    mint: str,
    now_ms: int,
    snapshot: Any = None,
    curve: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Features known at ``now_ms`` (signal time). Pure; unknown values are None."""
    curve = dict(curve or {})
    rows = [r for r in _sorted_prints(trades, mint) if int(r.get("ts") or 0) <= now_ms]
    out: dict[str, Any] = {}
    # Curve state at signal time: the snapshot the strategy decided on.
    vs = _int(getattr(snapshot, "virtual_sol_reserves", None))
    vt = _int(getattr(snapshot, "virtual_token_reserves", None))
    rs = _int(getattr(snapshot, "real_sol_reserves", None))
    rt = _int(getattr(snapshot, "real_token_reserves", None))
    supply = _int(getattr(snapshot, "token_total_supply", None)) or TOKEN_TOTAL_SUPPLY
    px = _num(getattr(snapshot, "price_sol", None))
    out["curve"] = {
        "virtual_sol_reserves": vs,
        "virtual_token_reserves": vt,
        "real_sol_reserves": rs,
        "real_token_reserves": rt,
        "progress_bps": _int(getattr(snapshot, "progress_bps", None))
        if snapshot is not None
        else (progress_bps(rt, INITIAL_REAL_TOKEN_RESERVES) if rt else None),
        "price_sol": px,
        "market_cap_sol": (market_cap_sol(vs, vt, supply) if vs and vt else None),
        "phase": getattr(snapshot, "phase", None),
    }
    created = _int(curve.get("created_ts"))
    out["token"] = {
        "discovery_source": curve.get("source") or None,
        "creator": curve.get("creator") or None,
        "created_ts": created,
        "age_s": round((now_ms - created) / 1000.0, 3) if created else None,
        "age_basis": curve.get("created_basis") or None,
        "verified": curve.get("verified"),
    }
    windows: dict[str, Any] = {}
    for w in FEATURE_WINDOWS_S:
        lo = now_ms - w * 1000
        sub = [r for r in rows if int(r.get("ts") or 0) >= lo]
        buys = [r for r in sub if r.get("side") == "buy"]
        sells = [r for r in sub if r.get("side") == "sell"]
        buyers = {str(r.get("trader")) for r in buys if r.get("trader")}
        sellers = {str(r.get("trader")) for r in sells if r.get("trader")}
        traders_known = any(r.get("trader") for r in sub)
        buy_sol = sum(_row_sol(r) for r in buys)
        sell_sol = sum(_row_sol(r) for r in sells)
        windows[f"{w}s"] = {
            "trades": len(sub),
            "buys": len(buys),
            "sells": len(sells),
            "buy_sol": round(buy_sol, 9),
            "sell_sol": round(sell_sol, 9),
            "net_sol": round(buy_sol - sell_sol, 9),
            "unique_buyers": len(buyers) if traders_known else None,
            "unique_sellers": len(sellers) if traders_known else None,
        }
    out["tape"] = windows
    ref = px if px else (_row_price(rows[-1]) if rows else None)
    momentum: dict[str, Optional[float]] = {}
    for w in FEATURE_WINDOWS_S:
        cut = now_ms - w * 1000
        before = [r for r in rows if int(r.get("ts") or 0) <= cut]
        old = _row_price(before[-1]) if before else None
        momentum[f"{w}s_bps"] = round((ref / old - 1.0) * 1e4, 3) if ref and old else None
    out["momentum"] = momentum
    out["last_print_age_ms"] = (now_ms - int(rows[-1].get("ts") or 0)) if rows else None
    creator = curve.get("creator")
    creator_rows = [r for r in rows if creator and r.get("trader") == creator and r.get("side") == "buy"]
    out["creator_buy"] = {
        "observed": bool(creator_rows),
        "sol": round(sum(_row_sol(r) for r in creator_rows), 9) if creator_rows else None,
        "first_buy_sol": round(_row_sol(creator_rows[0]), 9) if creator_rows else None,
    }
    out["tape_prints_seen"] = len(rows)
    return out


def compact_print(row: Mapping[str, Any], t0: int) -> Optional[list[Any]]:
    vs, vt = _int(row.get("virtual_sol_reserves")), _int(row.get("virtual_token_reserves"))
    if not vs or not vt:
        return None
    side = 1 if row.get("side") == "buy" else -1 if row.get("side") == "sell" else 0
    return [int(row.get("ts") or 0) - t0, side, round(_row_sol(row), 9), vs, vt]


class _Episode:
    __slots__ = (
        "symbol", "mint", "created_ms", "phase", "entry", "exit", "features", "params",
        "prints", "pre_truncated", "capped", "exit_done_ts", "last_seen_ms", "gone_since",
    )

    def __init__(self, symbol: str, mint: str, now_ms: int) -> None:
        self.symbol = symbol
        self.mint = mint
        self.created_ms = now_ms
        self.phase = "entry_pending"  # entry_pending → open → exit_pending → closed
        self.entry: dict[str, Any] = {}
        self.exit: dict[str, Any] = {"fills": []}
        self.features: dict[str, Any] = {}
        self.params: dict[str, Any] = {}
        self.prints: dict[tuple[int, int, str], Mapping[str, Any]] = {}
        self.pre_truncated = False
        self.capped = False
        self.exit_done_ts: Optional[int] = None
        self.last_seen_ms = now_ms
        self.gone_since: Optional[int] = None


def _fill_row(fill: Any) -> dict[str, Any]:
    get = (lambda k: fill.get(k)) if isinstance(fill, Mapping) else (lambda k: getattr(fill, k, None))
    return {
        "ts": _int(get("ts")),
        "price": _num(get("price")),
        "qty": _num(get("qty")),
        "fee": _num(get("fee")),
        "slippage_bps": _num(get("slippage_bps")),
    }


class EvidenceRecorder:
    def __init__(self, writer: Optional[EvidenceWriter] = None, clock=None) -> None:
        self._writer = writer or EvidenceWriter()
        self._eps: dict[str, _Episode] = {}
        self._lock = threading.RLock()
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.counters = {"written": 0, "dropped_no_fill": 0, "dropped_stale": 0, "dropped_cap": 0, "errors": 0}

    # ------------------------------------------------------------------ hooks
    def on_signal(
        self,
        *,
        symbol: str,
        mint: str,
        side: str,
        now_ms: int,
        signal: Any,
        snapshot: Any,
        notional: float,
        entry_impact_bps: Optional[float],
        params: Mapping[str, Any],
        provider: Any,
    ) -> None:
        t_hook = time.perf_counter_ns()
        with self._lock:
            ep = self._eps.get(symbol)
            if ep is not None and ep.mint != mint:
                ep = None
            if side == "long":
                if ep is not None:
                    return
                self._make_room(now_ms)
                ep = _Episode(symbol, mint, now_ms)
                trades = _recent(provider, symbol)
                ep.features = entry_features(
                    trades, mint=mint, now_ms=now_ms, snapshot=snapshot, curve=_curve_info(provider, symbol)
                )
                ep.params = dict(params)
                ep.entry = {
                    "signal_ts": now_ms,
                    "signal_reason": getattr(signal, "reason", None),
                    "signal_tags": list(getattr(signal, "tags", []) or []),
                    "signal_strength": getattr(signal, "strength", None),
                    "decision_price": _num(getattr(snapshot, "price_sol", None)),
                    "notional_sol": _num(notional),
                    "entry_impact_bps": _num(entry_impact_bps) if (entry_impact_bps or 0) < 1e8 else None,
                }
                self._eps[symbol] = ep
                self._collect(ep, trades, now_ms)
                t_sub = time.perf_counter_ns()
                _submit_entry_factors(provider, symbol=symbol, mint=mint, signal_ts=now_ms)
                t_end = time.perf_counter_ns()
                # On-path cost of this hook (features + factor enqueue), for the record.
                ep.entry["evidence_hook_us"] = round((t_end - t_hook) / 1000.0, 1)
                ep.entry["factor_submit_us"] = round((t_end - t_sub) / 1000.0, 1)
            elif side == "flat" and ep is not None and ep.phase == "open":
                entry_px = _num(ep.entry.get("fill_price"))
                trig = _num(getattr(snapshot, "price_sol", None))
                ep.exit.update(
                    {
                        "signal_ts": now_ms,
                        "reason": getattr(signal, "reason", None),
                        "tags": list(getattr(signal, "tags", []) or []),
                        "trigger_price": trig,
                        "trigger_ret_bps": round((trig / entry_px - 1.0) * 1e4, 3) if trig and entry_px else None,
                        "hold_at_signal_s": round((now_ms - int(ep.entry.get("fill_ts") or now_ms)) / 1000.0, 3),
                    }
                )
                ep.phase = "exit_pending"
                self._collect(ep, _recent(provider, symbol), now_ms)

    def on_first_print(self, *, symbol: str, mint: str, side: str, pending_ts: int, ready_ts: int, trade: Mapping[str, Any]) -> None:
        with self._lock:
            ep = self._eps.get(symbol)
            if ep is None or ep.mint != mint:
                return
            block = ep.entry if side == "long" else ep.exit
            if side != "long" and ep.phase != "exit_pending":
                return
            block.update(
                {
                    "ready_ts": int(ready_ts),
                    "first_print_ts": _int(trade.get("ts")),
                    "first_print_chain_ts": _int(trade.get("chain_ts")),
                    "first_print_price": _row_price(trade),
                    "first_print_side": trade.get("side"),
                    "first_print_seq": _int(trade.get("seq")),
                }
            )

    def on_fills(self, *, symbol: str, mint: str, side: str, fills: Iterable[Any], reason: Optional[str], closed: bool, provider: Any = None, now_ms: Optional[int] = None) -> None:
        now = int(now_ms if now_ms is not None else self._clock())
        with self._lock:
            ep = self._eps.get(symbol)
            if ep is None or ep.mint != mint:
                return
            rows = [_fill_row(f) for f in fills]
            if not rows:
                return
            if side == "buy" and ep.phase == "entry_pending":
                f0 = rows[0]
                ep.entry.update(
                    {
                        "fill_ts": f0["ts"],
                        "fill_price": f0["price"],
                        "fill_qty": sum(r["qty"] or 0.0 for r in rows),
                        "fill_fee": sum(r["fee"] or 0.0 for r in rows),
                        "fill_slippage_bps": f0["slippage_bps"],
                        "fills": rows,
                    }
                )
                sig = int(ep.entry.get("signal_ts") or 0)
                if f0["ts"] and sig:
                    ep.entry["signal_to_fill_ms"] = int(f0["ts"]) - sig
                ep.phase = "open"
            elif side == "sell" and ep.phase in {"open", "exit_pending"}:
                if ep.phase == "open":
                    # Orphan / immediate exits have no deferred exit signal.
                    ep.exit.setdefault("signal_ts", now)
                    ep.exit.setdefault("reason", reason)
                ep.exit["fills"].extend(rows)
                if reason and not ep.exit.get("reason"):
                    ep.exit["reason"] = reason
                ep.phase = "exit_pending"
                if closed:
                    ep.phase = "closed"
                    ep.exit_done_ts = max(int(r["ts"] or now) for r in ep.exit["fills"])
            else:
                return
            if provider is not None:
                self._collect(ep, _recent(provider, symbol), now)

    def poll(self, provider: Any, now_ms: Optional[int] = None) -> int:
        """Grow tapes and write finished trades. Returns the number written."""
        now = int(now_ms if now_ms is not None else self._clock())
        done: list[_Episode] = []
        with self._lock:
            for sym, ep in list(self._eps.items()):
                trades = _recent(provider, sym)
                self._collect(ep, trades, now)
                if ep.phase == "entry_pending" and now - ep.created_ms > PENDING_DROP_MS:
                    self._eps.pop(sym, None)
                    self.counters["dropped_no_fill"] += 1
                elif ep.phase in {"open", "exit_pending"} and now - ep.created_ms > STALE_OPEN_MS:
                    self._eps.pop(sym, None)
                    self.counters["dropped_stale"] += 1
                elif ep.phase == "closed" and ep.exit_done_ts is not None:
                    gone = ep.gone_since is not None and now - ep.gone_since > 5_000
                    if now >= ep.exit_done_ts + post_ms() or gone:
                        self._eps.pop(sym, None)
                        done.append(ep)
        n = 0
        for ep in done:
            try:
                self._writer.write(self._record(ep, now))
                self.counters["written"] += 1
                n += 1
            except Exception:
                self.counters["errors"] += 1
                log.exception("evidence write failed for %s", ep.symbol)
        return n

    def active(self) -> int:
        with self._lock:
            return len(self._eps)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"enabled": evidence_enabled(), "active": len(self._eps), "path": str(self._writer.path), **self.counters}

    # -------------------------------------------------------------- internals
    def _make_room(self, now_ms: int) -> None:
        while len(self._eps) >= MAX_EPISODES:
            victim = min(self._eps.values(), key=lambda e: (e.phase != "entry_pending", e.created_ms))
            self._eps.pop(victim.symbol, None)
            self.counters["dropped_cap"] += 1

    def _window(self, ep: _Episode, now_ms: int) -> tuple[int, int]:
        lo = int(ep.entry.get("signal_ts") or ep.created_ms) - PRE_MS
        hi = (ep.exit_done_ts + post_ms()) if ep.exit_done_ts is not None else now_ms
        return lo, hi

    def _collect(self, ep: _Episode, trades: list[Mapping[str, Any]], now_ms: int) -> None:
        lo, hi = self._window(ep, now_ms)
        mine = [r for r in trades if r.get("mint") in (None, "", ep.mint)]
        if not mine:
            if ep.gone_since is None:
                ep.gone_since = now_ms
            return
        ep.gone_since = None
        if not ep.prints:
            oldest = min(int(r.get("ts") or 0) for r in mine)
            cap = _trade_cap()
            if oldest > lo and cap and len(trades) >= cap:
                ep.pre_truncated = True
        for r in mine:
            ts = int(r.get("ts") or 0)
            if ts < lo or ts > hi:
                continue
            key = (ts, int(r.get("seq") or 0), str(r.get("signature") or ""))
            if key in ep.prints:
                continue
            if len(ep.prints) >= MAX_TAPE_ROWS:
                ep.capped = True
                break
            ep.prints[key] = r

    def _record(self, ep: _Episode, now_ms: int) -> dict[str, Any]:
        t0 = int(ep.entry.get("signal_ts") or ep.created_ms)
        lo, hi = self._window(ep, now_ms)
        ordered = [ep.prints[k] for k in sorted(ep.prints)]
        rows = [c for c in (compact_print(r, t0) for r in ordered) if c is not None]
        exit_fills = ep.exit.get("fills") or []
        qty = sum(abs(f["qty"] or 0.0) for f in exit_fills)
        exit_px = (sum((f["price"] or 0.0) * abs(f["qty"] or 0.0) for f in exit_fills) / qty) if qty else None
        exit_fee = sum(f["fee"] or 0.0 for f in exit_fills)
        ex = dict(ep.exit)
        ex["fill_ts"] = ep.exit_done_ts
        ex["fill_price"] = exit_px
        ex["fill_fee"] = exit_fee
        trig = _num(ex.get("trigger_price"))
        ex["fill_vs_trigger_bps"] = round((exit_px / trig - 1.0) * 1e4, 3) if exit_px and trig else None
        if ex.get("signal_ts") and ep.exit_done_ts:
            ex["signal_to_fill_ms"] = int(ep.exit_done_ts) - int(ex["signal_ts"])
        entry_px = _num(ep.entry.get("fill_price"))
        e_qty = _num(ep.entry.get("fill_qty")) or 0.0
        e_fee = _num(ep.entry.get("fill_fee")) or 0.0
        cost = (entry_px or 0.0) * e_qty + e_fee
        gross = ((exit_px or 0.0) - (entry_px or 0.0)) * e_qty if exit_px and entry_px else None
        net = (gross - e_fee - exit_fee) if gross is not None else None
        # Price path while held (marks after each real print between entry and exit fill).
        e_ts = int(ep.entry.get("fill_ts") or t0)
        x_ts = int(ep.exit_done_ts or e_ts)
        held = [price_sol(r[3], r[4]) for r in rows if e_ts <= r[0] + t0 <= x_ts]
        path = {
            "prints_while_held": len(held),
            "max_price": max(held) if held else None,
            "min_price": min(held) if held else None,
            "mfe_bps": round((max(held) / entry_px - 1.0) * 1e4, 3) if held and entry_px else None,
            "mae_bps": round((min(held) / entry_px - 1.0) * 1e4, 3) if held and entry_px else None,
        }
        return {
            "v": EVIDENCE_VERSION,
            "kind": "paper_trade_evidence",
            "strategy_id": "pump-paper-v1",
            "market_source": "real",
            "symbol": ep.symbol,
            "mint": ep.mint,
            "recorded_ts": now_ms,
            "params": ep.params,
            "entry": ep.entry,
            "features": ep.features,
            "entry_factors": _entry_factor_result(ep.mint, int(ep.entry.get("signal_ts") or 0)),
            "exit": ex,
            "path": path,
            "result": {
                "cost_sol": cost or None,
                "gross_sol": gross,
                "fees_sol": e_fee + exit_fee,
                "net_sol": net,
                "net_bps": round(net / cost * 1e4, 3) if net is not None and cost else None,
                "hold_s": round((x_ts - e_ts) / 1000.0, 3),
            },
            "tape": {
                "t0": t0,
                "from_ts": lo,
                "to_ts": hi,
                "fields": list(TAPE_FIELDS),
                "rows": rows,
                "pre_truncated": ep.pre_truncated,
                "capped": ep.capped,
                "post_complete": now_ms >= hi,
            },
        }


def _recent(provider: Any, symbol: str) -> list[Mapping[str, Any]]:
    getter = getattr(provider, "get_recent_trades", None)
    if not callable(getter):
        return []
    try:
        return list(getter(symbol) or [])
    except Exception:
        return []


def _curve_info(provider: Any, symbol: str) -> dict[str, Any]:
    getter = getattr(provider, "curve_evidence", None)
    if callable(getter):
        try:
            return dict(getter(symbol) or {})
        except Exception:
            return {}
    return {}


def _submit_entry_factors(provider: Any, *, symbol: str, mint: str, signal_ts: int) -> None:
    """Enqueue the holder / safety lookup (non-blocking; see ``entry_factors``)."""
    try:
        from app.paper.entry_factors import get_entry_factor_service

        svc = get_entry_factor_service()
        if svc is not None:
            svc.submit(provider, symbol=symbol, mint=mint, signal_ts=signal_ts)
    except Exception:
        log.exception("entry factor submit failed")


def _entry_factor_result(mint: str, signal_ts: int) -> dict[str, Any]:
    try:
        from app.paper.entry_factors import get_entry_factor_service, pending_result

        svc = get_entry_factor_service()
        if svc is None:
            return pending_result(signal_ts, "disabled")
        return svc.result(mint, signal_ts)
    except Exception as exc:
        log.exception("entry factor result failed")
        return {"status": f"error:{type(exc).__name__}", "signal_ts": signal_ts}


def _trade_cap() -> int:
    try:
        from app.providers.pumpfun_live_paper import TRADE_CAP

        return int(TRADE_CAP)
    except Exception:
        return 0


_recorder: Optional[EvidenceRecorder] = None
_recorder_lock = threading.Lock()


def get_evidence_recorder() -> Optional[EvidenceRecorder]:
    """Process recorder, or None when ``AUU_EVIDENCE=off``."""
    global _recorder
    if not evidence_enabled():
        return None
    with _recorder_lock:
        if _recorder is None:
            _recorder = EvidenceRecorder()
        return _recorder


def reset_evidence_recorder() -> None:
    global _recorder
    with _recorder_lock:
        _recorder = None


def safe_hook(name: str, *args: Any, **kwargs: Any) -> None:
    """Call ``EvidenceRecorder.<name>``; any error is logged and swallowed."""
    rec = get_evidence_recorder()
    if rec is None:
        return
    try:
        getattr(rec, name)(*args, **kwargs)
    except Exception:
        rec.counters["errors"] += 1
        log.exception("evidence hook %s failed", name)
