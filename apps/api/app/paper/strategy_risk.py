"""Hard risk caps for the strategy's automatic paper trading (leverage report §3.2).

Only the paper runner applies these; the backtest engine (and so the 31/31 acceptance) is untouched.
On the 19-coin archive the caps never bind in the hold-out (max single coin 7%, max gross 0.42,
worst intraday mark -2.7%, max drawdown -8.1%, no held long saw funding > 0.1%/8h).

Limits (fractions of equity):
- gross notional <= 1.0x, single coin <= 25%;
- intraday loss on hourly marks: -3% no new exposure, -5% halve everything, -8% close all + 24h lock;
- drawdown from peak NAV: -15% halve everything, -20% close all and lock until a manual review clears it;
- funding: a long whose latest settlement is > 0.1%/8h (short: < -0.1%) is halved;
- data: a held coin's 1h mark more than 2 bars stale, or the exchange health check failing -> reduce only.
Every trigger is written to ``risk_events`` and shown on the console.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Callable, Optional

HOUR_MS = 3_600_000


@dataclass(frozen=True)
class RiskLimits:
    max_gross: float = 1.0
    max_coin: float = 0.25
    day_stop_new: float = -0.03
    day_halve: float = -0.05
    day_flat: float = -0.08
    day_lock_hours: float = 24.0
    dd_halve: float = -0.15
    dd_flat: float = -0.20
    funding_f8: float = 0.001
    stale_bars: int = 2

    def describe(self) -> dict:
        return asdict(self)


def risk_enabled() -> bool:
    return os.getenv("AUU_STRATEGY_RISK", "on").strip().lower() not in {"0", "false", "off", "no"}


def f8_of(settlements: list[tuple[int, float]]) -> Optional[tuple[int, float]]:
    """(ts, 8h-equivalent rate) of the newest settlement; interval from the spacing to the previous one (default 8h)."""
    if not settlements:
        return None
    ts, rate = settlements[-1]
    iv = 8.0
    if len(settlements) >= 2:
        gap = (settlements[-1][0] - settlements[-2][0]) / HOUR_MS
        if 0.5 <= gap <= 8.5:
            iv = float(round(gap))
    return int(ts), float(rate) * 8.0 / (iv or 8.0)


def cap_weights(w: list[float], lim: RiskLimits) -> tuple[list[float], list[str]]:
    """Clip each coin to +-max_coin, then scale the book so gross <= max_gross."""
    hits = []
    out = []
    for x in w:
        y = max(-lim.max_coin, min(lim.max_coin, x))
        if y != x and "coin" not in hits:
            hits.append("coin")
        out.append(y)
    g = sum(abs(x) for x in out)
    if g > lim.max_gross + 1e-12:
        out = [x * lim.max_gross / g for x in out]
        hits.append("gross")
    return out, hits


def no_increase(t: float, w: float) -> float:
    """Target limited so exposure can only shrink: same side and |t| <= |w|, else hold w (or exit a flip)."""
    if t == 0.0:
        return 0.0
    if w == 0.0 or (t > 0) != (w > 0):
        return 0.0
    return t if abs(t) <= abs(w) else w


def funding_breach(w_sign: int, f8: Optional[float], lim: RiskLimits) -> bool:
    if f8 is None or w_sign == 0:
        return False
    return (w_sign > 0 and f8 > lim.funding_f8) or (w_sign < 0 and f8 < -lim.funding_f8)


def _sgn(x: float) -> int:
    return (x > 0) - (x < 0)


def rebalance_targets(
    coins: list[str],
    tg: list[float],
    drifted: list[float],
    *,
    lim: RiskLimits,
    locked: bool,
    day_ret: float,
    dd: float,
    day_flags: dict,
    data_bad: bool,
    f8: dict[str, Optional[float]],
) -> tuple[list[float], list[dict], dict]:
    """Apply the caps to the strategy's targets at the daily close.

    Returns (targets, events, state changes). ``events`` are dicts with kind/action/value/threshold;
    ``state`` may hold ``lock`` = "24h" or "review".
    """
    ev: list[dict] = []
    state: dict = {}
    out, hits = cap_weights(list(tg), lim)
    if "coin" in hits:
        over = {c: round(t, 4) for c, t in zip(coins, tg) if abs(t) > lim.max_coin}
        ev.append({"kind": "cap_coin", "action": "clip", "value": max(abs(t) for t in tg), "threshold": lim.max_coin, "detail": over})
    if "gross" in hits:
        ev.append({"kind": "cap_gross", "action": "scale", "value": sum(abs(x) for x in cap_weights(list(tg), RiskLimits(max_gross=1e9, max_coin=lim.max_coin))[0]),
                   "threshold": lim.max_gross, "detail": {}})
    if dd <= lim.dd_flat:
        state["lock"] = "review"
        ev.append({"kind": "dd_flat", "action": "close_all_lock_review", "value": dd, "threshold": lim.dd_flat, "detail": {}})
        return [0.0] * len(out), ev, state
    if day_ret <= lim.day_flat and not day_flags.get("flat"):  # not already flattened intraday
        state["lock"] = "24h"
        ev.append({"kind": "day_flat", "action": "close_all_lock_24h", "value": day_ret, "threshold": lim.day_flat, "detail": {"at": "close"}})
        return [0.0] * len(out), ev, state
    if locked:
        ev.append({"kind": "lock_active", "action": "targets_zero", "value": None, "threshold": None, "detail": {}})
        return [0.0] * len(out), ev, state
    if dd <= lim.dd_halve:
        out = [0.5 * t for t in out]  # half the strategy's targets while in drawdown (relative, never compounds)
        ev.append({"kind": "dd_halve", "action": "halve", "value": dd, "threshold": lim.dd_halve, "detail": {"at": "close"}})
    if day_ret <= lim.day_halve and not day_flags.get("halve"):  # not yet halved intraday
        out = [no_increase(t, 0.5 * w) for t, w in zip(out, drifted)]
        ev.append({"kind": "day_halve", "action": "halve", "value": day_ret, "threshold": lim.day_halve, "detail": {"at": "close"}})
    stop_new = day_ret <= lim.day_stop_new or day_flags.get("stop_new") or day_flags.get("halve") or data_bad
    if stop_new:
        before = list(out)
        out = [no_increase(t, w) for t, w in zip(out, drifted)]
        if day_ret <= lim.day_stop_new and not day_flags.get("stop_new"):
            ev.append({"kind": "day_stop_new", "action": "no_new_exposure", "value": day_ret, "threshold": lim.day_stop_new, "detail": {"at": "close"}})
        if data_bad:
            blocked = {c: round(b, 4) for c, b, a in zip(coins, before, out) if b != a}
            ev.append({"kind": "data_breaker", "action": "reduce_only", "value": None, "threshold": None, "detail": {"blocked": blocked, "at": "close"}})
    for k, c in enumerate(coins):
        t, w = out[k], drifted[k]
        side = _sgn(t) or _sgn(w)
        if funding_breach(side, f8.get(c), lim):
            new = no_increase(0.5 * t, w)  # half the target, never above what is held (relative, never compounds)
            if new != t:
                ev.append({"kind": "funding", "action": "reduce", "value": f8.get(c), "threshold": lim.funding_f8 * side,
                           "detail": {"coin": c, "target": round(t, 4), "held": round(w, 4), "to": round(new, 4), "at": "close"}})
                out[k] = new
    return out, ev, state


def intraday_actions(
    coins: list[str],
    w: list[float],
    *,
    lim: RiskLimits,
    day_ret: float,
    dd: float,
    flags: dict,
    dd_halved: bool,
    f8_new: dict[str, Optional[float]],
    last_targets: Optional[list[float]] = None,
) -> tuple[list[float], list[dict], dict]:
    """Decide intraday adjustments from an hourly mark. Returns (new weights, events, flag/state updates)."""
    ev: list[dict] = []
    up: dict = {}
    out, hits = cap_weights(list(w), lim)  # a rally can push a held coin past 25% / the book past 1x
    if hits:
        ev.append({"kind": "cap_coin" if "coin" in hits else "cap_gross", "action": "trade_down_held",
                   "value": max(abs(x) for x in w) if "coin" in hits else sum(abs(x) for x in w),
                   "threshold": lim.max_coin if "coin" in hits else lim.max_gross,
                   "detail": {c: round(b, 4) for c, a, b in zip(coins, w, out) if a != b}})
    if dd <= lim.dd_flat:
        ev.append({"kind": "dd_flat", "action": "close_all_lock_review", "value": dd, "threshold": lim.dd_flat, "detail": {}})
        up["lock"] = "review"
        return [0.0] * len(w), ev, up
    if day_ret <= lim.day_flat and not flags.get("flat"):
        ev.append({"kind": "day_flat", "action": "close_all_lock_24h", "value": day_ret, "threshold": lim.day_flat, "detail": {}})
        up.update(lock="24h", flat=True, halve=True, stop_new=True)
        return [0.0] * len(w), ev, up
    if day_ret <= lim.day_halve and not flags.get("halve"):
        ev.append({"kind": "day_halve", "action": "halve", "value": day_ret, "threshold": lim.day_halve, "detail": {}})
        out = [0.5 * x for x in out]
        up.update(halve=True, stop_new=True)
    if day_ret <= lim.day_stop_new and not flags.get("stop_new") and not up.get("stop_new"):
        ev.append({"kind": "day_stop_new", "action": "no_new_exposure", "value": day_ret, "threshold": lim.day_stop_new, "detail": {}})
        up["stop_new"] = True
    if dd <= lim.dd_halve and not dd_halved:
        ev.append({"kind": "dd_halve", "action": "halve", "value": dd, "threshold": lim.dd_halve, "detail": {}})
        out = [0.5 * x for x in out]
        up["dd_halved"] = True
    lt = last_targets or [abs(x) for x in w]
    for k, c in enumerate(coins):
        if out[k] != 0 and funding_breach(_sgn(out[k]), f8_new.get(c), lim):
            to = _sgn(out[k]) * min(abs(out[k]), 0.5 * abs(lt[k]))  # half the strategy target: repeated settlements don't compound
            if to == out[k]:
                continue
            ev.append({"kind": "funding", "action": "halve_coin", "value": f8_new.get(c), "threshold": lim.funding_f8 * _sgn(out[k]),
                       "detail": {"coin": c, "from": round(out[k], 4), "to": round(to, 4)}})
            out[k] = to
    return out, ev, up


KIND_LABEL = {
    "cap_coin": "单币上限 25%", "cap_gross": "总敞口上限 1x", "day_stop_new": "当日 −3% 停止新开仓",
    "day_halve": "当日 −5% 全部减半", "day_flat": "当日 −8% 全部平仓并锁 24h", "dd_halve": "回撤 −15% 仓位减半",
    "dd_flat": "回撤 −20% 清仓复查", "funding": "资金费熔断（多头 > 0.1%/8h）", "data_breaker": "数据熔断（只减不开）",
    "lock_active": "锁定中（目标仓位 0）", "lock_cleared": "人工解除锁定",
}

FetchMarks = Callable[[list[str], int], dict]
