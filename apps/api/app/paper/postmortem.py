"""Paper postmortem + ExecReport (v0.1) — read-only aggregations.

Sources: PaperTradeJournal + DecisionLog + executability projection.
Never mutates journal/params/strategy state. liveEnabled stays false.
No LLM prose — findings are rule templates only.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Iterable, Mapping, Optional, Sequence

from app.paper.executability import MIN_CLOSED_TRADES, aggregate_executability

GO_WINDOW_LABEL = "round8b"
ADAPTER_REF = "docs/adapters/paper-postmortem-v0.md"
PANEL_REF = "docs/viz/postmortem-panel-v0.md"

_FINGERPRINT_KEYS = (
    "progress_bps_min",
    "progress_bps_max",
    "take_profit_pct",
    "stop_loss_pct",
    "max_hold_sec",
    "sell_pressure_sec",
    "sell_pressure_ratio",
    "min_trade_count_1m",
    "min_buy_sell_ratio_1m",
    "max_impact_bps",
    "max_notional_sol",
)

EXIT_REASONS = (
    "take_profit",
    "stop_loss",
    "max_hold",
    "sell_pressure",
    "graduation",
    "orphan",
    "other",
)

_TAG_TO_REASON = {
    "TAKE_PROFIT": "take_profit",
    "STOP_LOSS": "stop_loss",
    "MAX_HOLD": "max_hold",
    "SELL_PRESSURE": "sell_pressure",
    "CURVE_NEAR_GRADUATION": "graduation",
    "ORPHAN_EXIT": "orphan",
    "take_profit": "take_profit",
    "stop_loss": "stop_loss",
    "max_hold": "max_hold",
    "sell_pressure": "sell_pressure",
    "graduation": "graduation",
    "orphan": "orphan",
}

MH_DOMINANT_PCT = 0.40
TP_HEALTHY_PCT = 0.30
SL_HEAVY_PCT = 0.30
E_WEAK_MAX = 0.05
SP_SILENT_MIN_N = 10
FINDINGS_CAP = 8

NOTES_DEFAULT = [
    "paper only",
    "no param mutate",
    "no live prose",
    "does not mutate params",
]


class ScenarioProgressForbidden(Exception):
    """Request touched progress_* while Scenario is on — HTTP 400."""

    code = "SCENARIO_PROGRESS_FORBIDDEN"

    def __init__(self, touched: list[str]):
        self.touched = list(touched)
        super().__init__(f"scenario forbids progress fields: {', '.join(self.touched)}")


def _as_mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    if hasattr(row, "model_dump"):
        return row.model_dump()
    if hasattr(row, "as_dict"):
        return row.as_dict()
    return {
        k: getattr(row, k)
        for k in dir(row)
        if not k.startswith("_") and not callable(getattr(row, k, None))
    }


def _f(row: Mapping[str, Any], *keys: str) -> Optional[float]:
    for k in keys:
        if k in row and row[k] is not None:
            try:
                return float(row[k])
            except (TypeError, ValueError):
                continue
    return None


def _median(vals: Sequence[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(float(v) for v in vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def params_fingerprint(params: Mapping[str, Any] | None = None) -> str:
    """Stable short hash of Go-window params (read-only)."""
    raw: dict[str, Any] = {}
    src = dict(params or {})
    for k in _FINGERPRINT_KEYS:
        if k in src and src[k] is not None:
            raw[k] = src[k]
    blob = json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def exit_reason_of(row: Mapping[str, Any]) -> str:
    tags = [str(t) for t in (row.get("tags") or [])]
    for t in tags:
        if t.upper() == "ORPHAN_EXIT" or t.lower() == "orphan":
            return "orphan"
    for t in tags:
        mapped = _TAG_TO_REASON.get(t) or _TAG_TO_REASON.get(t.upper()) or _TAG_TO_REASON.get(t.lower())
        if mapped and mapped != "other":
            return mapped
    reason = str(row.get("exit_reason") or row.get("reason") or "").strip()
    if reason:
        mapped = _TAG_TO_REASON.get(reason) or _TAG_TO_REASON.get(reason.lower())
        if mapped:
            return mapped
    return "other"


def entry_impact_bps(row: Mapping[str, Any]) -> Optional[float]:
    return _f(
        row,
        "entry_estimated_impact_net_bps",
        "entry_estimated_impact_bps",
        "entry_estimated_impact_gross_bps",
        "estimated_impact_bps",
    )


def hold_sec(row: Mapping[str, Any]) -> Optional[float]:
    entry = _f(row, "entry_ts")
    exit_ = _f(row, "exit_ts")
    if entry is None or exit_ is None:
        return None
    return max(0.0, (float(exit_) - float(entry)) / 1000.0)


def parse_window(window: str = "session", n: Optional[int] = None) -> tuple[Optional[int], str | int]:
    """Return (n_window, window_label). Supports session | last_n | int."""
    raw = str(window or "session").strip().lower()
    if raw in {"", "session", "all"}:
        return None, "session"
    if raw in {"last_n", "last", "n"}:
        size = max(1, int(n if n is not None else 30))
        return size, size
    try:
        size = max(1, int(raw))
        return size, size
    except ValueError:
        return None, "session"


def parse_scenario(
    *,
    tag: str = "off",
    min_trade_count_1m: Optional[int] = None,
    min_buy_sell_ratio_1m: Optional[float] = None,
    progress_bps_min: Optional[int] = None,
    progress_bps_max: Optional[int] = None,
    extra_forbidden: Optional[Mapping[str, Any]] = None,
    parent_fingerprint: str = "",
    started_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Build ScenarioState. progress_* → ScenarioProgressForbidden."""
    forbidden: list[str] = []
    if progress_bps_min is not None:
        forbidden.append("progress_bps_min")
    if progress_bps_max is not None:
        forbidden.append("progress_bps_max")
    if extra_forbidden:
        for k, v in extra_forbidden.items():
            if v is None:
                continue
            if str(k).lower().startswith("progress"):
                forbidden.append(str(k))
    tag_n = str(tag or "off").strip().lower()
    if tag_n in {"", "off", "none", "false", "0"}:
        tag_n = "off"
    elif tag_n in {"momentum", "momentum_delta", "on", "true", "1"}:
        tag_n = "momentum_delta"
    else:
        tag_n = "off"

    if forbidden and tag_n != "off":
        raise ScenarioProgressForbidden(forbidden)
    if forbidden and (progress_bps_min is not None or progress_bps_max is not None):
        raise ScenarioProgressForbidden(forbidden)

    enabled = tag_n != "off"
    delta = {
        "min_trade_count_1m": int(min_trade_count_1m) if min_trade_count_1m is not None and enabled else None,
        "min_buy_sell_ratio_1m": (
            float(min_buy_sell_ratio_1m) if min_buy_sell_ratio_1m is not None and enabled else None
        ),
    }
    ts = int(started_ts if started_ts is not None else time.time() * 1000)
    debug_scenario = {
        "tag": tag_n,
        "parent_fingerprint": parent_fingerprint,
        "delta": dict(delta) if enabled else {"min_trade_count_1m": None, "min_buy_sell_ratio_1m": None},
        "started_ts": ts if enabled else None,
    }
    return {
        "tag": tag_n,
        "enabled": enabled,
        "delta": delta,
        "forbidden_touched": [],
        "debug": {"scenario": debug_scenario},
    }


def _bucket_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pnls = [_f(r, "pnl") for r in rows]
    pnls_f = [p for p in pnls if p is not None]
    holds = [h for h in (hold_sec(r) for r in rows) if h is not None]
    expectancy = (sum(pnls_f) / len(pnls_f)) if pnls_f else None
    return {
        "count": len(rows),
        "expectancy": expectancy,
        "median_pnl": _median(pnls_f),
        "median_hold_sec": _median(holds),
    }


def by_exit_reason(trades: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {r: [] for r in EXIT_REASONS}
    for row in trades:
        groups[exit_reason_of(row)].append(row)
    n = len(trades)
    out: list[dict[str, Any]] = []
    for reason in EXIT_REASONS:
        st = _bucket_stats(groups[reason])
        out.append(
            {
                "reason": reason,
                "count": st["count"],
                "pct": (st["count"] / n) if n else 0.0,
                "expectancy": st["expectancy"],
                "median_pnl": st["median_pnl"],
                "median_hold_sec": st["median_hold_sec"],
            }
        )
    return out


def by_impact_quartile(trades: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    scored: list[tuple[float, Mapping[str, Any]]] = []
    for row in trades:
        impact = entry_impact_bps(row)
        if impact is None:
            continue
        scored.append((float(impact), row))
    scored.sort(key=lambda x: x[0])
    n = len(scored)
    labels = ("Q1", "Q2", "Q3", "Q4")
    if n == 0:
        return [
            {"q": lab, "count": 0, "expectancy": None, "median_impact_bps": None, "dominant_exit": None}
            for lab in labels
        ]
    out: list[dict[str, Any]] = []
    for qi, lab in enumerate(labels):
        lo = (qi * n) // 4
        hi = ((qi + 1) * n) // 4
        chunk = scored[lo:hi]
        rows = [r for _imp, r in chunk]
        impacts = [imp for imp, _r in chunk]
        st = _bucket_stats(rows)
        counts: dict[str, int] = {}
        for r in rows:
            reason = exit_reason_of(r)
            counts[reason] = counts.get(reason, 0) + 1
        dominant = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if counts else None
        out.append(
            {
                "q": lab,
                "count": st["count"],
                "expectancy": st["expectancy"],
                "median_impact_bps": _median(impacts),
                "dominant_exit": dominant,
            }
        )
    return out


def rolling_expectancy(trades: Sequence[Mapping[str, Any]], *, rolling: int = 10) -> list[dict[str, Any]]:
    win = max(1, int(rolling))
    out: list[dict[str, Any]] = []
    pnls: list[float] = []
    for i, row in enumerate(trades, start=1):
        p = _f(row, "pnl")
        pnls.append(0.0 if p is None else float(p))
        chunk = pnls[-win:]
        out.append({"i": i, "expectancy": sum(chunk) / len(chunk)})
    return out


def build_exec_report(
    exec_data: Mapping[str, Any],
    *,
    params_fp: str,
    asof_ts: int,
    win_rate: Optional[float] = None,
) -> dict[str, Any]:
    """Project executability → ExecReport field pack (no live prose)."""
    gates_src = exec_data.get("gates") or {}
    shadow = exec_data.get("shadow_slippage") or {}
    reject = exec_data.get("reject_rate") or {}
    impact_err = exec_data.get("impact_error") or {}
    overall_go = str(exec_data.get("verdict") or "") == "go"

    def _bucket_count(key: str) -> int:
        b = reject.get(key) or {}
        try:
            return int(b.get("count") or 0)
        except (TypeError, ValueError):
            return 0

    exp_gate = gates_src.get("expectancy") or {}
    impact_gate = gates_src.get("median_entry_impact") or {}
    reject_gate = gates_src.get("reject_rate") or {}
    shadow_n = int(shadow.get("n") or 0)
    shadow_need = int(shadow.get("min_n") or MIN_CLOSED_TRADES)

    return {
        "asof_ts": int(asof_ts),
        "go_window_label": GO_WINDOW_LABEL,
        "params_fingerprint": params_fp,
        "gates": {
            "n_closed": int(exec_data.get("n_closed") or exec_data.get("n_trades") or 0),
            "sample_ok": bool(exec_data.get("sample_ok")),
            "expectancy": exec_data.get("expectancy"),
            "expectancy_ok": bool(exp_gate.get("ok")),
            "median_entry_impact_net_bps": exec_data.get("median_entry_impact_net_bps"),
            "median_entry_impact_ok": bool(impact_gate.get("ok")),
            "shadow_p50_bps": shadow.get("p50_bps", shadow.get("median_bps")),
            "shadow_coverage": {"n": shadow_n, "need": shadow_need},
            "shadow_ok": bool(shadow.get("ok")),
            "reject_buckets": {
                "progress": _bucket_count("progress"),
                "impact": _bucket_count("impact"),
                "risk": _bucket_count("risk"),
            },
            "reject_ok": bool(reject_gate.get("ok")),
            "overall_go": bool(overall_go),
        },
        "live_language_allowed": bool(overall_go),
        "live_hint": None,
        "metrics": {
            "win_rate": win_rate,
            "shadow_p90_bps": shadow.get("p90_bps"),
            "impact_error_p50_bps": impact_err.get("p50_bps"),
        },
    }


def _finding(fid: str, severity: str, title: str, detail: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": fid, "severity": severity, "title": title, "detail": detail, "evidence": dict(evidence)}


def build_findings(
    *,
    summary: Mapping[str, Any],
    exit_buckets: Sequence[Mapping[str, Any]],
    impact_buckets: Sequence[Mapping[str, Any]],
    overall_go: bool,
) -> list[dict[str, Any]]:
    """Rule-templated findings. Chinese copy; never suggests live / follow-trade."""
    findings: list[dict[str, Any]] = []
    n = int(summary.get("n_closed") or 0)
    expectancy = summary.get("expectancy")
    sample_ok = bool(summary.get("sample_ok"))
    by_reason = {str(b["reason"]): b for b in exit_buckets}

    if n < MIN_CLOSED_TRADES:
        findings.append(
            _finding(
                "SAMPLE_THIN",
                "warn",
                "样本偏薄",
                f"已平仓 {n}/{MIN_CLOSED_TRADES}，推演结论仅供参考。",
                {"n_closed": n, "need": MIN_CLOSED_TRADES},
            )
        )

    mh = by_reason.get("max_hold") or {}
    mh_pct = float(mh.get("pct") or 0.0)
    if n > 0 and mh_pct >= MH_DOMINANT_PCT:
        findings.append(
            _finding(
                "MH_DOMINANT",
                "warn",
                "超时平仓占比偏高",
                f"max_hold 占比 {mh_pct * 100:.1f}%（{mh.get('count', 0)} 笔），持仓窗可能偏紧或动能衰减。",
                {"reason": "max_hold", "pct": mh_pct, "count": mh.get("count", 0)},
            )
        )

    tp = by_reason.get("take_profit") or {}
    tp_pct = float(tp.get("pct") or 0.0)
    tp_e = tp.get("expectancy")
    if n > 0 and tp_pct >= TP_HEALTHY_PCT and tp_e is not None and float(tp_e) > 0:
        findings.append(
            _finding(
                "TP_HEALTHY",
                "info",
                "止盈路径贡献健康",
                f"take_profit 占比 {tp_pct * 100:.1f}% ，该桶期望 {float(tp_e):.4g}。",
                {"reason": "take_profit", "pct": tp_pct, "expectancy": tp_e},
            )
        )

    sl = by_reason.get("stop_loss") or {}
    sl_pct = float(sl.get("pct") or 0.0)
    if n > 0 and sl_pct >= SL_HEAVY_PCT:
        findings.append(
            _finding(
                "SL_HEAVY",
                "warn",
                "止损占比偏重",
                f"stop_loss 占比 {sl_pct * 100:.1f}%（{sl.get('count', 0)} 笔）。",
                {"reason": "stop_loss", "pct": sl_pct, "count": sl.get("count", 0)},
            )
        )

    q4 = next((b for b in impact_buckets if b.get("q") == "Q4"), None)
    if q4 and q4.get("count", 0) > 0 and q4.get("expectancy") is not None:
        q4_e = float(q4["expectancy"])
        overall_e = float(expectancy) if expectancy is not None else 0.0
        if q4_e < overall_e and q4_e < 0:
            findings.append(
                _finding(
                    "IMPACT_Q4_DRAG",
                    "warn",
                    "高冲击分位拖累期望",
                    f"冲击 Q4 期望 {q4_e:.4g}（中位冲击 {q4.get('median_impact_bps')} bps），低于窗口整体。",
                    {
                        "q": "Q4",
                        "expectancy": q4_e,
                        "overall_expectancy": expectancy,
                        "median_impact_bps": q4.get("median_impact_bps"),
                        "dominant_exit": q4.get("dominant_exit"),
                        "count": q4.get("count"),
                    },
                )
            )

    if expectancy is not None and float(expectancy) < 0:
        findings.append(
            _finding(
                "E_NEG",
                "warn",
                "窗口期望为负",
                f"期望 {float(expectancy):.4g}（n={n}）。",
                {"expectancy": expectancy, "n_closed": n},
            )
        )
    elif sample_ok and expectancy is not None and 0 <= float(expectancy) < E_WEAK_MAX:
        findings.append(
            _finding(
                "E_WEAK",
                "info",
                "期望偏弱",
                f"期望 {float(expectancy):.4g}，接近零（n={n}）。",
                {"expectancy": expectancy, "n_closed": n, "weak_max": E_WEAK_MAX},
            )
        )

    sp = by_reason.get("sell_pressure") or {}
    if n >= SP_SILENT_MIN_N and int(sp.get("count") or 0) == 0:
        findings.append(
            _finding(
                "SP_SILENT",
                "info",
                "卖压出场未触发",
                f"已平仓 {n} 笔中 sell_pressure 为 0，规则可能未覆盖弱盘。",
                {"reason": "sell_pressure", "count": 0, "n_closed": n},
            )
        )

    if not overall_go:
        findings.append(
            _finding(
                "GO_BLOCKED",
                "info",
                "Go 闸未过",
                "综合 Go/NoGo 为 NoGo；本报告不含实盘开通文案。",
                {"overall_go": False},
            )
        )

    return findings[:FINDINGS_CAP]


def aggregate_postmortem(
    trades: Iterable[Any],
    *,
    fills: Optional[Iterable[Any]] = None,
    decision_log: Optional[Iterable[Any]] = None,
    window_label: str | int = "session",
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    rolling: int = 10,
    params: Optional[Mapping[str, Any]] = None,
    scenario: Optional[Mapping[str, Any]] = None,
    setup_seed_tags: Optional[Sequence[str]] = None,
    asof_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Pure aggregation for PostmortemReport (includes nested ExecReport)."""
    trade_rows = [_as_mapping(t) for t in trades]
    trade_rows = [t for t in trade_rows if str(t.get("source") or "") != "live"]
    asof = int(asof_ts if asof_ts is not None else time.time() * 1000)
    params_fp = params_fingerprint(params)

    n = len(trade_rows)
    pnls = [_f(t, "pnl") for t in trade_rows]
    pnls_f = [p for p in pnls if p is not None]
    wins = sum(1 for p in pnls_f if p > 0)
    win_rate = (wins / n) if n else None
    expectancy = (sum(pnls_f) / n) if n and len(pnls_f) == n else (
        (sum(pnls_f) / len(pnls_f)) if pnls_f else None
    )
    sample_ok = n >= MIN_CLOSED_TRADES

    ts_list = [int(t.get("exit_ts") or 0) for t in trade_rows]
    window_from = from_ts if from_ts is not None else (min(ts_list) if ts_list else asof)
    window_to = to_ts if to_ts is not None else (max(ts_list) if ts_list else asof)

    exit_buckets = by_exit_reason(trade_rows)
    impact_buckets = by_impact_quartile(trade_rows)
    rolling_rows = rolling_expectancy(trade_rows, rolling=rolling)

    exec_raw = aggregate_executability(
        trade_rows,
        fills=fills,
        decision_log=decision_log,
        window=window_label,
        params_max_impact_bps=_f(dict(params or {}), "max_impact_bps"),
    )
    exec_report = build_exec_report(exec_raw, params_fp=params_fp, asof_ts=asof, win_rate=win_rate)
    overall_go = bool(exec_report["gates"]["overall_go"])
    summary = {
        "n_closed": n,
        "expectancy": expectancy,
        "win_rate": win_rate,
        "sample_ok": sample_ok,
    }
    findings = build_findings(
        summary=summary,
        exit_buckets=exit_buckets,
        impact_buckets=impact_buckets,
        overall_go=overall_go,
    )
    scen = dict(scenario) if scenario else parse_scenario(parent_fingerprint=params_fp, started_ts=asof)
    notes = list(NOTES_DEFAULT)
    if setup_seed_tags:
        notes.append(f"setup_seed_tags={list(setup_seed_tags)} (display only; not HabitProfile)")

    return {
        "asof_ts": asof,
        "window": {"from_ts": int(window_from), "to_ts": int(window_to), "n_closed": n, "label": window_label},
        "params_fingerprint": params_fp,
        "go_window_label": GO_WINDOW_LABEL,
        "exec": exec_report,
        "summary": summary,
        "by_exit_reason": exit_buckets,
        "by_impact_quartile": impact_buckets,
        "rolling_expectancy": rolling_rows,
        "findings": findings,
        "scenario": scen,
        "notes": notes,
        "liveEnabled": False,
        "liveDisabled": True,
        "adapter_ref": ADAPTER_REF,
        "panel_ref": PANEL_REF,
        "debug": {
            "scenario": (scen.get("debug") or {}).get("scenario"),
            "setup_seed_tags": list(setup_seed_tags) if setup_seed_tags else [],
        },
    }


LOG_QUERY = 2_000


def build_postmortem(
    *,
    window: str = "session",
    n: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    rolling: int = 10,
    scenario_tag: str = "off",
    min_trade_count_1m: Optional[int] = None,
    min_buy_sell_ratio_1m: Optional[float] = None,
    progress_bps_min: Optional[int] = None,
    progress_bps_max: Optional[int] = None,
    setup_seed_tags: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Load journal + decision log and build PostmortemReport (read-only)."""
    from app.paper.decision_log import backfill_decision_shadows, get_decision_log
    from app.paper.ledger import get_paper_journal
    from app.strategies.pump_paper_v1 import get_engine

    engine = get_engine()
    params = engine.params.model_dump()
    params_fp = params_fingerprint(params)
    scenario = parse_scenario(
        tag=scenario_tag,
        min_trade_count_1m=min_trade_count_1m,
        min_buy_sell_ratio_1m=min_buy_sell_ratio_1m,
        progress_bps_min=progress_bps_min,
        progress_bps_max=progress_bps_max,
        parent_fingerprint=params_fp,
    )
    n_window, window_label = parse_window(window, n)
    journal = get_paper_journal()
    trades = journal.closed_in_window(window=n_window, from_ts=from_ts, to_ts=to_ts)
    log = get_decision_log()
    rows = log.all() if n_window is None else log.query(from_ts=from_ts, to_ts=to_ts, limit=LOG_QUERY)
    backfill_decision_shadows(rows)
    return aggregate_postmortem(
        trades,
        fills=journal.fills,
        decision_log=rows,
        window_label=window_label,
        from_ts=from_ts,
        to_ts=to_ts,
        rolling=rolling,
        params=params,
        scenario=scenario,
        setup_seed_tags=setup_seed_tags,
    )


def build_exec_report_endpoint(
    *,
    window: str = "session",
    n: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> dict[str, Any]:
    """Standalone ExecReport (same pack nested under postmortem.exec)."""
    from app.paper.decision_log import backfill_decision_shadows, get_decision_log
    from app.paper.ledger import get_paper_journal
    from app.strategies.pump_paper_v1 import get_engine

    engine = get_engine()
    params = engine.params.model_dump()
    params_fp = params_fingerprint(params)
    n_window, window_label = parse_window(window, n)
    journal = get_paper_journal()
    trades = journal.closed_in_window(window=n_window, from_ts=from_ts, to_ts=to_ts)
    log = get_decision_log()
    rows = log.all() if n_window is None else log.query(from_ts=from_ts, to_ts=to_ts, limit=LOG_QUERY)
    backfill_decision_shadows(rows)
    trade_rows = [_as_mapping(t) for t in trades]
    trade_rows = [t for t in trade_rows if str(t.get("source") or "") != "live"]
    n_closed = len(trade_rows)
    pnls = [_f(t, "pnl") for t in trade_rows]
    pnls_f = [p for p in pnls if p is not None]
    wins = sum(1 for p in pnls_f if p > 0)
    win_rate = (wins / n_closed) if n_closed else None
    exec_raw = aggregate_executability(
        trade_rows,
        fills=journal.fills,
        decision_log=rows,
        window=window_label,
        params_max_impact_bps=float(engine.params.max_impact_bps),
    )
    report = build_exec_report(
        exec_raw, params_fp=params_fp, asof_ts=int(time.time() * 1000), win_rate=win_rate
    )
    report["liveEnabled"] = False
    report["liveDisabled"] = True
    return report
