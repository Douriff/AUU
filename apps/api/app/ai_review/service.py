"""Assemble the read-only review payload.

Fixture mode never falls back to a ledger. Ledger mode never invents rows.
"""
from __future__ import annotations

import json
import os
import time
from itertools import combinations
from pathlib import Path
from typing import Any, Optional

from app.ai_review.catalog import identities, identity as find_identity
from app.ai_review.explain import explain_bar, previous_bar
from app.ai_review.metrics import (
    attach_trade_costs,
    comparable,
    from_unit_nav,
    paper_performance,
    sum_recorded_costs,
)
from app.ai_review.model_adapter import facts_for_model, model_gate, run_model
from app.ai_review.schema import DAY_MS, HOUR_MS, MAX_EVENTS, scrub
from app.ai_review.store_read import read_ledgers

_FIXTURE = Path(__file__).with_name("fixtures") / "dev_sample.json"
_TIMING = {
    "late_entry": None,
    "early_exit": None,
    "status": "not_measured",
    "reason": "no_preregistered_breakout_detector",
}
_NOTICE = {
    "profit_promise": False,
    "orders_enabled": False,
    "live_enabled": False,
    "read_only": True,
    "wallet": False,
}


def fixture_enabled() -> bool:
    return os.getenv("AUU_AI_REVIEW_FIXTURE", "").strip().lower() in {"1", "true", "on", "yes"}


def load_fixture() -> Optional[dict[str, Any]]:
    try:
        data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict) or data.get("explicit") is not True or data.get("id") != "dev-sample-v1":
        return None
    return data


def _blank_book(status: str, reason: str, book: str) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "book": book,
        "basis": None,
        "principal": None,
        "principal_ccy": None,
        "total_return": None,
        "max_drawdown": None,
        "cost": None,
        "cost_status": "unknown",
        "closed_trades": None,
        "open_trades": None,
        "nav_points": None,
        "signal_rows": None,
        "as_of_ts": None,
        "recorded_at": None,
        "stale": None,
        "warnings": [],
        "curve": [],
        "method": None,
    }


def _count_signals(perf: dict[str, Any], ident: dict[str, Any], signal: dict[str, Any]) -> dict[str, Any]:
    bar_key = "v0_bars" if ident["id"] == "auu_signal_v0" else "bars"
    table_key = "bars" if ident["id"] == "auu_signal_v0" else "cand_bars"
    tables = signal.get("tables") or {}
    if tables.get(table_key) == "ok":
        grouped = (signal.get(bar_key) or {}).get(ident["id"]) or {}
        perf["signal_rows"] = sum(len(rows) for rows in grouped.values())
    return perf


def _signal_perf(ident: dict[str, Any], signal: dict[str, Any], now_ms: int) -> dict[str, Any]:
    from app.strategies.auu_signal_candidates import COST_PER_SIDE

    if signal.get("state") == "missing":
        return _blank_book("not_connected", "file_missing", "shadow")
    tables = signal.get("tables") or {}
    if tables.get("cand_nav") != "ok":
        return _count_signals(_blank_book("not_connected", "schema_missing", "shadow"), ident, signal)
    if ident["id"] in set(signal.get("bad_nav") or []):
        perf = _blank_book("non_finite", "non_finite_nav", "shadow")
        perf["warnings"] = ["non_finite"]
        return _count_signals(perf, ident, signal)
    points = list((signal.get("nav") or {}).get(ident["id"]) or [])
    perf = from_unit_nav(points, now_ms=now_ms, tf_ms=DAY_MS, book="shadow", start=1.0, basis="unit_nav")
    if ident["id"] in set(signal.get("bad_trades") or []):
        perf["cost"] = None
        perf["cost_status"] = "non_finite"
        perf["closed_trades"] = None
    elif tables.get("cand_trades") == "ok":
        flat = [row for rows in (signal.get("trades") or {}).get(ident["id"], {}).values() for row in rows]
        perf = attach_trade_costs(perf, flat, cost_per_side=COST_PER_SIDE)
    return _count_signals(perf, ident, signal)


def _paper_perf(paper: dict[str, Any], now_ms: int) -> dict[str, Any]:
    if paper.get("state") == "missing":
        return _blank_book("not_connected", "file_missing", "paper")
    if (paper.get("tables") or {}).get("runs") != "ok":
        return _blank_book("not_connected", "schema_missing", "paper")
    if "trend_tsmom_v1" in set(paper.get("bad_runs") or []):
        return _blank_book("non_finite", "non_finite_nav", "paper")
    points = list((paper.get("runs") or {}).get("trend_tsmom_v1") or [])
    if not points:
        return _blank_book("empty", "no_nav_rows", "paper")
    return paper_performance(points, start_nav=paper.get("start_nav"), now_ms=now_ms)


def _h2_perf(ident: dict[str, Any], h2: dict[str, Any], now_ms: int) -> dict[str, Any]:
    if h2.get("state") == "missing":
        return _blank_book("not_connected", "file_missing", "shadow")
    if h2.get("table") != "ok":
        return _blank_book("not_connected", "schema_missing", "shadow")
    if h2.get("non_finite"):
        return _blank_book("non_finite", "non_finite_nav", "shadow")
    points = list(h2.get("days") or [])
    start = float(ident.get("accounting_unit") or 0.0)
    if start <= 0:
        return _blank_book("not_connected", "accounting_unit_unknown", "shadow")
    perf = from_unit_nav(points, now_ms=now_ms, tf_ms=DAY_MS, book="shadow", start=start, basis="shadow_unit")
    if perf["status"] != "ok":
        return perf
    stored = [row.get("drawdown") for row in points]
    if any(value is None for value in stored):
        perf["max_drawdown"] = None
        perf["warnings"] = list(perf.get("warnings") or []) + ["drawdown_not_cross_checked"]
    else:
        worst = min(float(value) for value in stored)
        if perf["max_drawdown"] is None or abs(worst - float(perf["max_drawdown"])) > 1e-4:
            perf["status"] = "inconsistent"
            perf["reason"] = "drawdown_mismatch"
            perf["total_return"] = None
            perf["max_drawdown"] = None
            perf["curve"] = []
            return perf
    cost, status = sum_recorded_costs(points)
    perf["cost"] = cost
    perf["cost_status"] = status
    perf["cost_unit"] = "shadow_unit" if status == "recorded" else None
    perf["principal"] = None
    perf["capital_kind"] = "shadow_unit"
    return perf


def _s3_view(s3: dict[str, Any]) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
    perf = _blank_book("not_connected", "no_portfolio_nav", "shadow")
    if s3.get("state") == "missing":
        perf["reason"] = "file_missing"
        return perf, None
    if s3.get("table") != "ok":
        perf["reason"] = "schema_missing"
        return perf, None
    if s3.get("non_finite"):
        perf["status"] = "non_finite"
        perf["reason"] = "non_finite_trade"
        return perf, None
    trades = list(s3.get("trades") or [])
    closed = [row for row in trades if row.get("status") == "closed"]
    costs = [row.get("cost") for row in closed]
    stats: dict[str, Any] = {
        "not_a_nav": True,
        "closed_trades": len(closed),
        "open_trades": sum(1 for row in trades if row.get("status") == "open"),
        "cost": None if any(cost is None for cost in costs) or not closed else sum(float(cost) for cost in costs),
        "cost_status": "unknown" if (not closed or any(cost is None for cost in costs)) else "recorded",
        "mean_net": None,
        "total_return": None,
        "max_drawdown": None,
    }
    perf["closed_trades"] = len(closed)
    perf["open_trades"] = stats["open_trades"]
    if not trades:
        perf["status"] = "empty"
        perf["reason"] = "no_trade_rows"
    return perf, stats


def _bars_for(ident: dict[str, Any], signal: dict[str, Any]) -> dict[str, list[dict]]:
    if ident["id"] == "auu_signal_v0":
        return (signal.get("v0_bars") or {}).get(ident["id"]) or {}
    return (signal.get("bars") or {}).get(ident["id"]) or {}


def _tf_ms(ident: dict[str, Any]) -> int:
    return HOUR_MS if ident.get("timeframe") == "1h" else DAY_MS


def _public_identity(ident: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": ident["id"],
        "version": ident["version"],
        "version_kind": ident["version_kind"],
        "style": ident["style"],
        "direction": ident["direction"],
        "timeframe": ident["timeframe"],
        "pool": ident["pool"],
        "hold": ident["hold"],
        "primary_book": ident["primary_book"],
        "registered_at": ident.get("registered_at"),
        "records": ident["records"],
        "orders": False,
        "params_hash": ident.get("params_hash"),
    }


def _public_perf(perf: dict[str, Any]) -> dict[str, Any]:
    hidden = {"ts", "accounting_unit"}
    return {key: value for key, value in perf.items() if key not in hidden}


def build_cards(ledgers: dict[str, Any], now_ms: int) -> list[dict[str, Any]]:
    cards = []
    signal = ledgers["signal"]
    for ident in identities():
        trade_stats = None
        if ident["style"].startswith("momentum"):
            perf = _signal_perf(ident, signal, now_ms)
        elif ident["id"] == "trend_tsmom_v1":
            perf = _paper_perf(ledgers["paper"], now_ms)
        elif ident["id"] == "shadow_h2":
            perf = _h2_perf(ident, ledgers["h2"], now_ms)
        elif ident["id"] == "shadow_s3":
            perf, trade_stats = _s3_view(ledgers["s3"])
        else:
            perf = _blank_book("not_connected", "no_adapter", ident["primary_book"])
        backtest = _blank_book("not_connected", "no_backtest_store", "backtest")
        card = {
            **_public_identity(ident),
            "performance": _public_perf(perf),
            "backtest": _public_perf(backtest),
            "trade_stats": trade_stats,
            "_perf": perf,
            "_bars": _bars_for(ident, signal) if ident["primary_book"] == "shadow" and ident["id"] != "shadow_h2" and ident["id"] != "shadow_s3" else {},
        }
        cards.append(card)
    return cards


def _compare_block(cards: list[dict[str, Any]]) -> dict[str, Any]:
    groups = []
    used: set[str] = set()
    ranked = [card for card in cards if card["_perf"].get("status") == "ok"]
    for card in ranked:
        if card["id"] in used:
            continue
        bucket = [card]
        for other in ranked:
            if other["id"] == card["id"] or other["id"] in used:
                continue
            ok, _reason = comparable(card["_perf"], other["_perf"])
            if ok:
                bucket.append(other)
        for item in bucket:
            used.add(item["id"])
        if len(bucket) >= 2:
            window = bucket[0]["performance"].get("window")
            groups.append(
                {
                    "ids": [item["id"] for item in bucket],
                    "reason": "same_window",
                    "window": window,
                    "rows": [
                        {
                            "id": item["id"],
                            "total_return": item["performance"].get("total_return"),
                            "max_drawdown": item["performance"].get("max_drawdown"),
                            "cost": item["performance"].get("cost"),
                            "cost_status": item["performance"].get("cost_status"),
                            "closed_trades": item["performance"].get("closed_trades"),
                            "nav_points": item["performance"].get("nav_points"),
                            "stale": item["performance"].get("stale"),
                            "warnings": item["performance"].get("warnings") or [],
                        }
                        for item in bucket
                    ],
                }
            )
    refused = []
    by_id = {card["id"]: card for card in cards}
    family = [card["id"] for card in cards if str(card["style"]).startswith("momentum")]
    for left_id, right_id in combinations(family, 2):
        ok, reason = comparable(by_id[left_id]["_perf"], by_id[right_id]["_perf"])
        if not ok:
            refused.append({"ids": [left_id, right_id], "reason": reason})
    return {"groups": groups, "refused": refused}


def _events_for_card(card: dict[str, Any], ledgers: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    ident_id = card["id"]
    if ident_id in {"shadow_s3", "shadow_h2", "trend_tsmom_v1"}:
        return _book_events(ident_id, ledgers)
    signal = ledgers["signal"]
    trades = (signal.get("trades") or {}).get(ident_id) or {}
    bars = card.get("_bars") or {}
    for symbol, rows in trades.items():
        for row in rows:
            events.append(_trade_event(ident_id, symbol, row, "entry"))
            if row.get("exit_ts") is not None:
                events.append(_trade_event(ident_id, symbol, row, "exit"))
    for symbol, rows in bars.items():
        for row in rows:
            if row.get("action") not in {"enter", "exit"}:
                continue
            events.append(
                {
                    "id": f"{ident_id}:{symbol}:signal:{row.get('asof_ts')}:{row.get('action')}",
                    "ts": row.get("asof_ts"),
                    "book": "shadow",
                    "strategy_id": ident_id,
                    "kind": "signal",
                    "symbol": symbol,
                    "side": row.get("side"),
                    "price": None,
                    "cost": None,
                    "gross": None,
                    "net": None,
                    "unrealized": None,
                    "basis": "recorded_signal_bar",
                    "live_order": False,
                    "record_class": "stored_history",
                }
            )
    return events


def _trade_event(strategy_id: str, symbol: str, row: dict[str, Any], kind: str) -> dict[str, Any]:
    from app.strategies.auu_signal_candidates import COST_PER_SIDE

    closed = row.get("exit_ts") is not None
    cost = None
    if closed:
        from app.ai_review.metrics import _round_trip_cost

        checked = _round_trip_cost(row, COST_PER_SIDE)
        if checked is not None:
            cost = COST_PER_SIDE
    return {
        "id": f"{strategy_id}:{symbol}:{kind}:{row.get('entry_ts')}",
        "ts": row.get("entry_ts") if kind == "entry" else row.get("exit_ts"),
        "book": "shadow",
        "strategy_id": strategy_id,
        "kind": kind,
        "symbol": symbol,
        "side": row.get("side"),
        "price": row.get("entry_px") if kind == "entry" else row.get("exit_px"),
        "cost": cost,
        "cost_unit": None if cost is None else "fraction_of_notional",
        "gross": row.get("gross") if kind == "exit" else None,
        "net": row.get("net") if kind == "exit" else None,
        "unrealized": None,
        "basis": "recorded_hypothetical_trade",
        "live_order": False,
        "record_class": "stored_history",
        "open": not closed,
    }


def _book_events(strategy_id: str, ledgers: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    if strategy_id == "trend_tsmom_v1":
        for row in ledgers["paper"].get("fills") or []:
            fee = row.get("fee")
            slip = row.get("slippage")
            cost = None if fee is None or slip is None else float(fee) + float(slip)
            events.append(
                {
                    "id": f"paper:{row.get('symbol')}:{row.get('ts')}:{row.get('side')}",
                    "ts": row.get("ts"),
                    "book": "paper",
                    "strategy_id": strategy_id,
                    "kind": "fill",
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "price": row.get("price"),
                    "cost": cost,
                    "cost_unit": None if cost is None else "USDT",
                    "gross": None,
                    "net": None,
                    "unrealized": None,
                    "basis": "recorded_paper_fill",
                    "live_order": False,
                    "record_class": "stored_history",
                }
            )
    if strategy_id == "shadow_s3":
        for row in ledgers["s3"].get("trades") or []:
            events.append(
                {
                    "id": f"s3:{row.get('symbol')}:{row.get('ts')}:{row.get('status')}",
                    "ts": row.get("entry_ts") or row.get("ts"),
                    "book": "shadow",
                    "strategy_id": strategy_id,
                    "kind": "exit" if row.get("status") == "closed" else "entry",
                    "symbol": row.get("symbol"),
                    "side": "long",
                    "price": row.get("exit_px") if row.get("status") == "closed" else row.get("entry_px"),
                    "cost": row.get("cost"),
                    "gross": row.get("gross"),
                    "net": row.get("net") if row.get("status") == "closed" else None,
                    "unrealized": None,
                    "basis": "recorded_shadow_trade",
                    "live_order": False,
                    "record_class": "stored_history",
                }
            )
    return events


def _signal_preview(cards: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    rows = []
    for card in cards:
        for symbol, bars in (card.get("_bars") or {}).items():
            for bar in bars:
                rows.append(
                    {
                        "strategy_id": card["id"],
                        "symbol": symbol,
                        "asof_ts": bar.get("asof_ts"),
                        "side": bar.get("side"),
                        "score": bar.get("score"),
                        "action": bar.get("action"),
                        "recorded_at": bar.get("recorded_at"),
                    }
                )
    rows.sort(key=lambda item: int(item["asof_ts"] or 0), reverse=True)
    return rows[:limit]


def explain_stored(
    ident: dict[str, Any],
    bars: dict[str, list[dict]],
    symbol: str,
    asof_ts: int,
    *,
    model_client: Any = None,
) -> dict[str, Any]:
    rows = list(bars.get(symbol) or [])
    bar = next((row for row in rows if int(row.get("asof_ts") or -1) == int(asof_ts)), None)
    prev, gap = (None, False) if bar is None else previous_bar(rows, asof_ts, _tf_ms(ident))
    rules = explain_bar(ident, bar, prev, gap=gap)
    if rules["reconstructed"] != "yes":
        ai = model_gate() if model_client is None else {
            "status": "skipped",
            "reason": "no_rule_evidence",
            "live": False,
            "external": False,
            "text": None,
            "mock": bool(getattr(model_client, "is_mock", False)),
        }
    else:
        ai = run_model(facts_for_model(rules), model_client)
    return {"rules": rules, "ai": ai}


def _featured(cards: list[dict[str, Any]], ledgers: dict[str, Any], model_client: Any) -> Optional[dict[str, Any]]:
    preview = _signal_preview(cards, limit=40)
    if not preview:
        return None
    chosen = preview[0]
    for item in preview:
        if item.get("action") == "enter":
            chosen = item
            break
    ident = find_identity(chosen["strategy_id"])
    if ident is None:
        return None
    bars = next(card["_bars"] for card in cards if card["id"] == ident["id"])
    return explain_stored(ident, bars, str(chosen["symbol"]), int(chosen["asof_ts"]), model_client=model_client)


def _sources(ledgers: dict[str, Any], sample: bool) -> list[dict[str, str]]:
    rows = []
    for key in ("signal", "paper", "s3", "h2"):
        block = ledgers[key]
        rows.append({"name": str(block.get("basename") or key), "state": "explicit_fixture" if sample else str(block.get("state") or "missing")})
    rows.append({"name": "backtest", "state": "not_connected"})
    return rows


def _ledgers(now_box: dict[str, int]) -> tuple[dict[str, Any], dict[str, Any]]:
    if fixture_enabled():
        data = load_fixture()
        if data is None:
            empty = {
                "signal": {"state": "missing", "basename": "explicit_fixture", "tables": {}, "nav": {}, "trades": {}, "bars": {}, "v0_bars": {}, "bad_nav": [], "bad_trades": []},
                "paper": {"state": "missing", "basename": "explicit_fixture", "tables": {}, "runs": {}, "fills": [], "start_nav": None, "bad_runs": []},
                "s3": {"state": "missing", "basename": "explicit_fixture", "trades": [], "table": "missing", "non_finite": False},
                "h2": {"state": "missing", "basename": "explicit_fixture", "days": [], "table": "missing", "non_finite": False},
            }
            return empty, {"sample": True, "fixture_id": None, "fixture_error": "invalid", "now_ms": now_box["now"]}
        if isinstance(data.get("now_ms"), int):
            now_box["now"] = int(data["now_ms"])
        return _fixture_ledgers(data), {"sample": True, "fixture_id": data["id"], "fixture_error": None, "now_ms": now_box["now"]}
    return read_ledgers(), {"sample": False, "fixture_id": None, "fixture_error": None, "now_ms": now_box["now"]}


def _fixture_ledgers(data: dict[str, Any]) -> dict[str, Any]:
    signal = data.get("signal") or {}
    return {
        "signal": {
            "state": "ok",
            "basename": "dev_sample.json",
            "tables": {"cand_nav": "ok", "cand_trades": "ok", "cand_bars": "ok", "bars": "ok"},
            "nav": signal.get("nav") or {},
            "trades": signal.get("trades") or {},
            "bars": signal.get("bars") or {},
            "v0_bars": signal.get("v0_bars") or {},
            "bad_nav": [],
            "bad_trades": [],
            "bad_bars": [],
            "bad_v0": [],
        },
        "paper": {"state": "missing", "basename": "mainstream_strategy.sqlite", "tables": {}, "runs": {}, "fills": [], "start_nav": None, "bad_runs": []},
        "s3": {"state": "missing", "basename": "shadow_s3.sqlite", "trades": [], "table": "missing", "non_finite": False},
        "h2": {"state": "missing", "basename": "shadow_h2.sqlite", "days": [], "table": "missing", "non_finite": False},
    }


def plaza_payload(now_ms: Optional[int] = None, model_client: Any = None) -> dict[str, Any]:
    now_box = {"now": int(now_ms if now_ms is not None else time.time() * 1000)}
    ledgers, meta = _ledgers(now_box)
    now = now_box["now"]
    if meta.get("fixture_error"):
        payload = {
            "mode": "explicit_fixture",
            "sample": True,
            "fixture_id": None,
            "fixture_error": "invalid",
            "notice": _NOTICE,
            "timing": _TIMING,
            "ai": model_gate(),
            "strategies": [],
            "compare": {"groups": [], "refused": []},
            "signals": [],
            "featured": None,
            "decisions": [],
            "live": {"status": "not_connected", "reason": "live_orders_not_read"},
            "generated_at": now,
            "sources": [],
        }
        return scrub(payload)
    cards = build_cards(ledgers, now)
    compare = _compare_block(cards)
    decisions = []
    for card in cards:
        decisions.extend(_events_for_card(card, ledgers))
    decisions.sort(key=lambda item: int(item.get("ts") or 0))
    featured = _featured(cards, ledgers, model_client)
    public = []
    for card in cards:
        item = {key: value for key, value in card.items() if not key.startswith("_")}
        item["data_origin"] = "explicit_fixture" if meta["sample"] and item["performance"]["status"] in {"ok", "empty", "inconsistent", "non_finite"} else (
            "local_ledger" if item["performance"]["status"] in {"ok", "empty", "inconsistent", "non_finite"} else "not_connected"
        )
        if meta["sample"] and item["performance"]["status"] == "not_connected":
            item["data_origin"] = "not_connected"
        public.append(item)
    payload = {
        "mode": "explicit_fixture" if meta["sample"] else "local_ledger",
        "sample": meta["sample"],
        "fixture_id": meta["fixture_id"],
        "fixture_error": None,
        "notice": _NOTICE,
        "timing": _TIMING,
        "ai": model_gate(),
        "strategies": public,
        "compare": compare,
        "signals": _signal_preview(cards),
        "featured": featured,
        "decisions": decisions[:MAX_EVENTS],
        "live": {"status": "not_connected", "reason": "live_orders_not_read"},
        "generated_at": now,
        "sources": _sources(ledgers, meta["sample"]),
    }
    return scrub(payload)


def strategy_payload(strategy_id: str, now_ms: Optional[int] = None, model_client: Any = None) -> Optional[dict[str, Any]]:
    payload = plaza_payload(now_ms=now_ms, model_client=model_client)
    for card in payload["strategies"]:
        if card["id"] == strategy_id:
            return {"notice": payload["notice"], "sample": payload["sample"], "strategy": card, "timing": payload["timing"], "ai": model_gate()}
    return None


def explain_payload(strategy_id: str, symbol: str, asof_ts: int, model_client: Any = None, now_ms: Optional[int] = None) -> Optional[dict[str, Any]]:
    ident = find_identity(strategy_id)
    if ident is None:
        return None
    now_box = {"now": int(now_ms if now_ms is not None else time.time() * 1000)}
    ledgers, meta = _ledgers(now_box)
    cards = build_cards(ledgers, now_box["now"])
    bars = next((card["_bars"] for card in cards if card["id"] == strategy_id), {})
    body = explain_stored(ident, bars, symbol, asof_ts, model_client=model_client)
    body["sample"] = meta["sample"]
    body["notice"] = _NOTICE
    return scrub(body)


def decisions_payload(strategy_id: Optional[str] = None, now_ms: Optional[int] = None) -> dict[str, Any]:
    payload = plaza_payload(now_ms=now_ms)
    rows = payload["decisions"]
    if strategy_id:
        rows = [row for row in rows if row["strategy_id"] == strategy_id]
    return {
        "sample": payload["sample"],
        "notice": payload["notice"],
        "timing": payload["timing"],
        "live": payload["live"],
        "events": rows,
    }


def signals_payload(strategy_id: Optional[str] = None, symbol: Optional[str] = None, limit: int = 50, now_ms: Optional[int] = None) -> dict[str, Any]:
    payload = plaza_payload(now_ms=now_ms)
    rows = payload["signals"]
    if strategy_id:
        rows = [row for row in rows if row["strategy_id"] == strategy_id]
    if symbol:
        rows = [row for row in rows if row["symbol"] == symbol]
    return {"sample": payload["sample"], "notice": payload["notice"], "signals": rows[:limit]}


def plaza_csv(now_ms: Optional[int] = None) -> str:
    from app.ai_review.schema import csv_document

    payload = plaza_payload(now_ms=now_ms)
    header = [
        "strategy_id", "version", "style", "direction", "book", "status", "reason",
        "total_return", "max_drawdown", "cost", "cost_status", "closed_trades",
        "nav_points", "signal_rows", "window_start", "window_end", "as_of_ts", "stale", "sample",
    ]
    rows = []
    for card in payload["strategies"]:
        perf = card["performance"]
        window = perf.get("window") or {}
        rows.append(
            [
                card["id"],
                card["version"],
                card["style"],
                card["direction"],
                perf.get("book"),
                perf.get("status"),
                perf.get("reason"),
                perf.get("total_return"),
                perf.get("max_drawdown"),
                perf.get("cost"),
                perf.get("cost_status"),
                perf.get("closed_trades"),
                perf.get("nav_points"),
                perf.get("signal_rows"),
                window.get("start_ts"),
                window.get("end_ts"),
                perf.get("as_of_ts"),
                perf.get("stale"),
                payload["sample"],
            ]
        )
    return csv_document(header, rows)
