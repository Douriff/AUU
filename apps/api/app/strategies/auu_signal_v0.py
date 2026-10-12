"""AUU proprietary strategy-signal indicator · frozen formula ``auu_signal_v0``.

Not a MA/MACD rebrand. Outputs the five-field overlay contract per closed bar:

  side ∈ {long, short, flat}
  score ∈ [-1, 1]
  action ∈ {enter, hold, exit, none}
  reason_tags[]
  asof_ts  (bar open ts; decision uses only data ≤ bar close)

Main score = vol-scaled momentum + volume imbalance (+ optional settled-funding z).
News/资讯 is a reserved ``risk_gate`` hook only — never enters the predictive score in v0.

Causal: every value at row t uses rows ≤ t. Forming (incomplete) bars are excluded by the caller.
This module never places orders or writes a trading ledger.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

from app.strategies.indicators import pct_change, rolling_std, sign

VERSION = "auu_signal_v0"
Side = Literal["long", "short", "flat"]
Action = Literal["enter", "hold", "exit", "none"]

# ---- frozen parameters (do not retune in place; bump VERSION for a new formula) ----
PARAMS: dict = {
    "version": VERSION,
    "mom_lookbacks": [5, 20],
    "vol_window": 20,
    "vol_short": 5,
    "vol_long": 20,
    "funding_z_window": 60,
    "weights": {"momentum": 0.50, "volume_imbalance": 0.30, "funding": 0.20},
    "enter_th": 0.25,
    "exit_th": 0.10,
    "tag_abs": 0.15,
    "eps": 1e-12,
    "long_only": False,  # mainstream perps allow short; spot UIs may ignore short markers
    "news": {"role": "risk_gate_only", "in_score": False},
}


@dataclass(frozen=True)
class AuuSignalParams:
    mom_lookbacks: tuple[int, ...] = (5, 20)
    vol_window: int = 20
    vol_short: int = 5
    vol_long: int = 20
    funding_z_window: int = 60
    w_mom: float = 0.50
    w_vol: float = 0.30
    w_fund: float = 0.20
    enter_th: float = 0.25
    exit_th: float = 0.10
    tag_abs: float = 0.15
    eps: float = 1e-12
    long_only: bool = False


def _tanh(x: float) -> float:
    # bounded, no overflow surprises
    if x > 20:
        return 1.0
    if x < -20:
        return -1.0
    return math.tanh(x)


def _rolling_mean(x: list[Optional[float]], n: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(x)
    for t in range(n - 1, len(x)):
        w = x[t - n + 1 : t + 1]
        if any(v is None for v in w):
            continue
        out[t] = sum(w) / n  # type: ignore[arg-type]
    return out


def _clip(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


@dataclass
class AuuSignalV0:
    """Pure compute: list of OHLCV (+ optional funding) → overlay series."""

    params: AuuSignalParams = field(default_factory=AuuSignalParams)
    name: str = VERSION

    def describe(self) -> dict:
        return {"name": self.name, **asdict(self.params), "frozen": PARAMS}

    def compute(
        self,
        *,
        closes: list[Optional[float]],
        volumes: list[Optional[float]],
        ts: list[int],
        funding: Optional[list[Optional[float]]] = None,
        risk_gates: Optional[list[Optional[list[str]]]] = None,
    ) -> list[dict]:
        """Return one overlay row per input bar (same length). Warm-up rows are flat/none/score=0.

        ``funding[t]`` = last **settled** funding rate known at or before bar t's close (or None).
        ``risk_gates[t]`` = optional news/event tags; when present they force flat + ``risk_gate``
        and never contribute to the predictive score.
        """
        p = self.params
        n = len(closes)
        if not (len(volumes) == len(ts) == n):
            raise ValueError("closes/volumes/ts length mismatch")
        if funding is not None and len(funding) != n:
            raise ValueError("funding length mismatch")
        if risk_gates is not None and len(risk_gates) != n:
            raise ValueError("risk_gates length mismatch")

        rets1 = pct_change(closes, 1)
        vol = rolling_std(rets1, p.vol_window)
        moms = [pct_change(closes, L) for L in p.mom_lookbacks]
        v_short = _rolling_mean(volumes, p.vol_short)
        v_long = _rolling_mean(volumes, p.vol_long)

        fund_z: list[Optional[float]] = [None] * n
        if funding is not None:
            f_mean = _rolling_mean(funding, p.funding_z_window)
            f_std = rolling_std(funding, p.funding_z_window)
            for t in range(n):
                if funding[t] is None or f_mean[t] is None or f_std[t] is None:
                    continue
                s = f_std[t]
                if s is None or s < p.eps:
                    fund_z[t] = 0.0
                else:
                    fund_z[t] = (funding[t] - f_mean[t]) / s  # type: ignore[operator]

        out: list[dict] = []
        prev: Side = "flat"
        for t in range(n):
            mom_parts: list[float] = []
            ok = True
            if vol[t] is None:
                ok = False
            else:
                for L, series in zip(p.mom_lookbacks, moms):
                    m = series[t]
                    if m is None:
                        ok = False
                        break
                    scale = (vol[t] * math.sqrt(L)) + p.eps  # type: ignore[operator]
                    mom_parts.append(_tanh(m / scale))
            mom_s = sum(mom_parts) / len(mom_parts) if ok and mom_parts else None

            imb_s: Optional[float] = None
            if v_short[t] is not None and v_long[t] is not None and v_long[t] > p.eps and rets1[t] is not None:
                imb = (v_short[t] - v_long[t]) / (v_long[t] + p.eps)  # type: ignore[operator]
                imb_s = sign(rets1[t]) * _tanh(imb)  # type: ignore[arg-type]

            fund_s: Optional[float] = None
            if fund_z[t] is not None:
                # Contrarian: extreme positive funding (longs pay) leans short.
                fund_s = -_tanh(fund_z[t])

            score = 0.0
            tags: list[str] = []
            if mom_s is not None or imb_s is not None or fund_s is not None:
                w_m, w_v, w_f = p.w_mom, p.w_vol, p.w_fund
                parts: list[tuple[float, float]] = []
                if mom_s is not None:
                    parts.append((w_m, mom_s))
                if imb_s is not None:
                    parts.append((w_v, imb_s))
                if fund_s is not None:
                    parts.append((w_f, fund_s))
                wsum = sum(w for w, _ in parts) or 1.0
                score = _clip(sum(w * v for w, v in parts) / wsum)
                if mom_s is not None and abs(mom_s) >= p.tag_abs:
                    tags.append("momentum")
                if imb_s is not None and abs(imb_s) >= p.tag_abs:
                    tags.append("volume_imbalance")
                if fund_s is not None and abs(fund_s) >= p.tag_abs:
                    tags.append("funding")
                if vol[t] is not None and vol[t] > 0:
                    # crude vol-regime tag: realized vol above ~2% daily
                    if vol[t] >= 0.02:  # type: ignore[operator]
                        tags.append("vol_regime")

            side = self._side_from(prev, score if (mom_s is not None or imb_s is not None) else 0.0)
            gates = (risk_gates[t] if risk_gates else None) or None
            if gates:
                side = "flat"
                score = 0.0
                for g in gates:
                    if g and g not in tags:
                        tags.append(g)
                if "risk_gate" not in tags:
                    tags.append("risk_gate")

            action = self._action(prev, side)
            # hold only when staying in a position (frontend usually hides it)
            if action == "none" and side != "flat" and prev == side:
                action = "hold"

            row = {
                "asof_ts": int(ts[t]),
                "side": side,
                "score": round(score, 6),
                "action": action if not (action == "hold" and t < n - 1) else "none",
                # emit hold only on the latest bar to avoid marker spam; history uses none while flat/continuing
                "reason_tags": tags,
                "factors": {
                    "momentum": None if mom_s is None else round(mom_s, 6),
                    "volume_imbalance": None if imb_s is None else round(imb_s, 6),
                    "funding": None if fund_s is None else round(fund_s, 6),
                },
            }
            # Fix hold policy cleanly: only enter/exit/none in history; hold on last if in position
            if t < n - 1:
                if row["action"] == "hold":
                    row["action"] = "none"
            out.append(row)
            prev = side
        return out

    def _side_from(self, prev: Side, score: float) -> Side:
        p = self.params
        if prev == "flat":
            if score >= p.enter_th:
                return "long"
            if (not p.long_only) and score <= -p.enter_th:
                return "short"
            return "flat"
        if prev == "long":
            if (not p.long_only) and score <= -p.enter_th:
                return "short"
            if score < p.exit_th:
                return "flat"
            return "long"
        # prev == short
        if score >= p.enter_th:
            return "long"
        if score > -p.exit_th:
            return "flat"
        return "short"

    @staticmethod
    def _action(prev: Side, curr: Side) -> Action:
        if prev == curr:
            return "hold" if curr != "flat" else "none"
        if curr == "flat":
            return "exit"
        return "enter"  # flat→pos or flip


def align_funding_to_bars(
    bar_ts: list[int],
    funding_rows: list[tuple[int, float]],
    *,
    bar_ms: int,
) -> list[Optional[float]]:
    """Map settled funding onto bars: last settlement with ts <= bar_close (= bar_ts + bar_ms).

    ``bar_ts`` is the candle open time (exchange convention). Settlements after the bar close
    are not visible to that bar (no look-ahead).
    """
    out: list[Optional[float]] = [None] * len(bar_ts)
    if not funding_rows:
        return out
    fr = sorted(funding_rows, key=lambda x: x[0])
    j = 0
    last: Optional[float] = None
    for i, open_ts in enumerate(bar_ts):
        close_ts = int(open_ts) + int(bar_ms)
        while j < len(fr) and fr[j][0] <= close_ts:
            last = float(fr[j][1])
            j += 1
        out[i] = last
    return out
