"""Pre-registered forward-shadow candidates derived from ``auu_signal_v0`` factors.

Record-only. No chart display, no trading, never read by any strategy/live path.
Parameters are FROZEN at registration; ``PARAMS_HASH`` must match docs/signal-candidates.md.
Changing anything here requires a new candidate id (never edit in place).

cand_A: long-only, volume imbalance direction = sign(close[t]-close[t-5]) (5-bar smoothed),
        weights momentum .5 / volume .3 / funding .2, enter >= 0.25, exit < 0.10.
cand_B: same but funding term dropped (weights .5/.3 renormalised).  IS(2021-2023)-selected.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from app.strategies.auu_signal_v0 import AuuSignalParams, AuuSignalV0

REGISTERED_AT = "2026-10-15"  # Asia/Shanghai; forward evaluation counts bars with asof_ts >= REG_TS
REG_TS = 1_791_936_000_000  # 2026-10-14 00:00 UTC daily bar = first bar that CLOSES after registration (2026-10-15 01:4x CST)

CANDIDATES: dict[str, dict] = {
    "cand_A": {"base": "auu_signal_v0", "long_only": True, "imb_dir_bars": 5,
               "weights": {"momentum": 0.5, "volume_imbalance": 0.3, "funding": 0.2},
               "enter_th": 0.25, "exit_th": 0.10, "tf": "1d"},
    "cand_B": {"base": "auu_signal_v0", "long_only": True, "imb_dir_bars": 5,
               "weights": {"momentum": 0.5, "volume_imbalance": 0.3},
               "enter_th": 0.25, "exit_th": 0.10, "tf": "1d"},
}
COST_PER_SIDE = 0.0007


def params_hash(cid: str) -> str:
    blob = json.dumps({"id": cid, **CANDIDATES[cid]}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


PARAMS_HASH = {cid: params_hash(cid) for cid in CANDIDATES}


def compute_candidate(
    cid: str,
    *,
    closes: list[Optional[float]],
    volumes: list[Optional[float]],
    ts: list[int],
    funding: Optional[list[Optional[float]]] = None,
) -> list[dict]:
    p = CANDIDATES[cid]
    base = AuuSignalV0(params=AuuSignalParams(long_only=True)).compute(
        closes=closes, volumes=volumes, ts=ts, funding=funding)
    w = p["weights"]; L = p["imb_dir_bars"]
    out: list[dict] = []
    prev = "flat"
    for t, row in enumerate(base):
        fa = row["factors"]
        mom, imb, fund = fa.get("momentum"), fa.get("volume_imbalance"), fa.get("funding")
        if imb is not None:
            if t >= L and closes[t] is not None and closes[t - L] is not None:
                d = closes[t] - closes[t - L]  # type: ignore[operator]
                imb = abs(imb) * (1.0 if d > 0 else -1.0 if d < 0 else 0.0)
            else:
                imb = None
        parts = [(w["momentum"], mom), (w["volume_imbalance"], imb)]
        if "funding" in w:
            parts.append((w["funding"], fund))
        parts = [(a, b) for a, b in parts if b is not None]
        score = sum(a * b for a, b in parts) / sum(a for a, _ in parts) if (parts and mom is not None) else 0.0
        if prev == "flat":
            side = "long" if score >= p["enter_th"] else "flat"
        else:
            side = "flat" if score < p["exit_th"] else "long"
        action = "enter" if side != prev and side == "long" else "exit" if side != prev else "none"
        out.append({"asof_ts": int(ts[t]), "side": side, "score": round(score, 6), "action": action,
                    "reason_tags": [], "factors": {"momentum": mom, "volume_imbalance": None if imb is None else round(imb, 6),
                                                    "funding": fund if "funding" in w else None}})
        prev = side
    return out


def hypothetical_trades(rows: list[dict], opens_by_ts: dict[int, float], *, bar_ms: int,
                        funding_rows: Optional[list[tuple[int, float]]] = None, since_ts: int = REG_TS) -> list[dict]:
    """Next-bar-open fills, 7bps/side, funding paid by longs (received by shorts). Only positions
    whose signal bar asof_ts >= since_ts (forward-only). Open trades are returned with exit_ts=None."""
    fr = sorted(funding_rows or [])
    def fsum(a: int, b: int) -> float:
        return sum(r for t, r in fr if a < t <= b)
    res: list[dict] = []
    pos, ent = "flat", None
    for r in rows:
        s = r["side"]
        if s == pos:
            continue
        fill_ts = int(r["asof_ts"]) + bar_ms
        px = opens_by_ts.get(fill_ts)
        if px is None:
            break
        if pos != "flat" and ent is not None:
            et, ep = ent
            g = px / ep - 1 if pos == "long" else 1 - px / ep
            f = fsum(et, fill_ts)
            res.append({"side": pos, "entry_ts": et, "entry_px": ep, "exit_ts": fill_ts, "exit_px": px,
                        "gross": g, "funding": f if pos == "long" else -f,
                        "net": g - 2 * COST_PER_SIDE - (f if pos == "long" else -f)})
        ent = (fill_ts, px) if (s != "flat" and int(r["asof_ts"]) >= since_ts) else None
        pos = s
    if pos != "flat" and ent is not None:
        res.append({"side": pos, "entry_ts": ent[0], "entry_px": ent[1], "exit_ts": None, "exit_px": None,
                    "gross": None, "funding": None, "net": None})
    return res
