"""Paper vs the pre-registered backtest expected band (report P0-3). Read-only on the ledger.

status: inside | below | above | dd_breach | beyond_horizon | no_data | no_band
"Outside" (below / above / dd_breach) means "does not look like the backtest" -> alert via app.alerts.
This is an early-warning check, NOT a Go verdict.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

BANDS_DIR = Path(__file__).resolve().parents[1] / "backtest" / "bands"
OUTSIDE = ("below", "above", "dd_breach")
LABEL = {"inside": "在预期区间内", "below": "低于预期区间（5% 分位以下）", "above": "高于预期区间（95% 分位以上）",
         "dd_breach": "回撤超出预期（比 5% 最差情形还深）", "beyond_horizon": "已超过区间期限", "no_data": "尚无纸面记录",
         "no_band": "区间文件缺失"}
NOTE = "预期区间来自回测 hold-out 日收益的 block bootstrap（参数事先登记），只用于提前发现异常，不是 Go 判定；Go/No-Go 仍需 ≥250 天。"


@lru_cache(maxsize=4)
def load_band(name: str = "trend_tsmom_v1") -> Optional[dict]:
    p = BANDS_DIR / f"{name}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def evaluate(rets: Sequence[float], days: Sequence[str] = (), band: Optional[dict] = None, *, curve_pad: int = 30) -> dict:
    """``rets`` = paper daily returns in ledger order (day 1 = first rebalance)."""
    band = band if band is not None else load_band()
    if band is None:
        return {"status": "no_band", "label": LABEL["no_band"], "note": NOTE}
    H = len(band["cum_p05"])
    n = len(rets)
    out = {"name": band["name"], "version": band["version"], "registered": band["registered"],
           "sourceSha": band["source"]["returns_sha256"][:12], "horizon": H, "n": n, "note": NOTE}
    if n == 0:
        return {**out, "status": "no_data", "label": LABEL["no_data"], "curve": _curve(band, [], [], curve_pad)}
    nav, peak, dd, cum = 1.0, 1.0, 0.0, []
    for r in rets:
        nav *= 1.0 + float(r)
        peak = max(peak, nav)
        dd = min(dd, nav / peak - 1.0)
        cum.append(nav - 1.0)
    out.update(cum=cum[-1], mdd=dd, day=days[-1] if days else None)
    if n > H:
        return {**out, "status": "beyond_horizon", "label": LABEL["beyond_horizon"], "curve": _curve(band, cum, days, curve_pad)}
    i = n - 1
    lo, mid, hi, dd_lo = band["cum_p05"][i], band["cum_p50"][i], band["cum_p95"][i], band["mdd_p05"][i]
    status = "below" if cum[-1] < lo else "above" if cum[-1] > hi else "dd_breach" if dd < dd_lo else "inside"
    return {**out, "status": status, "label": LABEL[status], "outside": status in OUTSIDE,
            "p05": lo, "p50": mid, "p95": hi, "mddP05": dd_lo, "curve": _curve(band, cum, days, curve_pad)}


def _curve(band: dict, cum: list[float], days: Sequence[str], pad: int) -> list[dict]:
    H = len(band["cum_p05"])
    m = min(H, max(len(cum) + pad, 60))
    return [{"n": k + 1, "p05": band["cum_p05"][k], "p50": band["cum_p50"][k], "p95": band["cum_p95"][k],
             "paper": cum[k] if k < len(cum) else None, "day": days[k] if k < len(days) else None} for k in range(m)]


def from_ledger(ledger) -> dict:
    from app.backtest.panel import ms_day

    runs = ledger.runs()
    return evaluate([float(x["ret"]) for x in runs], [ms_day(int(x["day"])) for x in runs])
