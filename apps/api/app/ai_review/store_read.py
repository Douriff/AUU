"""Open existing SQLite ledgers in read-only mode.

A missing file or table is reported as not connected. This module never
creates a database, never runs CREATE, and never requests a network.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from app.ai_review.schema import BAR_READ_CAP, NAV_READ_CAP, TRADE_READ_CAP, bad_number, required_bad
from app.data_paths import data_dir

_SIGNAL = "auu_signal_shadow.sqlite"
_PAPER = "mainstream_strategy.sqlite"
_S3 = "shadow_s3.sqlite"
_H2 = "shadow_h2.sqlite"

_SIGNAL_TABLES = {
    "cand_nav": {"version", "ts", "ret", "nav", "n_long", "n_short", "n_sym", "recorded_at"},
    "cand_trades": {
        "version", "symbol", "side", "entry_ts", "entry_px", "exit_ts", "exit_px",
        "gross", "funding", "net", "recorded_at",
    },
    "cand_bars": {
        "version", "params_hash", "symbol", "tf", "asof_ts", "side", "score", "action",
        "factors", "recorded_at",
    },
    "bars": {
        "version", "symbol", "tf", "asof_ts", "side", "score", "action", "reason_tags",
        "factors", "recorded_at",
    },
}


def _connect(path: Path) -> tuple[str, Optional[sqlite3.Connection]]:
    if not path.is_file():
        return "missing", None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        return "unreadable", None
    return "ok", conn


def _columns(conn: sqlite3.Connection, table: str) -> Optional[set[str]]:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if found is None:
        return None
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ready(conn: sqlite3.Connection, table: str, need: set[str]) -> str:
    cols = _columns(conn, table)
    if cols is None:
        return "schema"
    if not need <= cols:
        return "schema"
    return "ok"


def _loads(raw: Any, fallback: Any) -> Any:
    if raw is None or raw == "":
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def read_ledgers() -> dict[str, Any]:
    """Snapshot the local ledgers. Keys are source names, never absolute paths."""
    root = data_dir()
    signal = _read_signal(root / _SIGNAL)
    paper = _read_paper(root / _PAPER)
    s3 = _read_s3(root / _S3)
    h2 = _read_h2(root / _H2)
    return {"signal": signal, "paper": paper, "s3": s3, "h2": h2}


def _read_signal(path: Path) -> dict[str, Any]:
    state, conn = _connect(path)
    out: dict[str, Any] = {
        "state": state,
        "basename": _SIGNAL,
        "nav": {},
        "trades": {},
        "bars": {},
        "v0_bars": {},
        "bad_nav": [],
        "bad_trades": [],
        "bad_bars": [],
        "bad_v0": [],
    }
    if conn is None:
        return out
    try:
        nav_state = _ready(conn, "cand_nav", _SIGNAL_TABLES["cand_nav"])
        trade_state = _ready(conn, "cand_trades", _SIGNAL_TABLES["cand_trades"])
        bar_state = _ready(conn, "cand_bars", _SIGNAL_TABLES["cand_bars"])
        v0_state = _ready(conn, "bars", _SIGNAL_TABLES["bars"])
        out["tables"] = {
            "cand_nav": nav_state,
            "cand_trades": trade_state,
            "cand_bars": bar_state,
            "bars": v0_state,
        }
        if nav_state == "ok":
            out["nav"], out["bad_nav"] = _nav_rows(conn)
        if trade_state == "ok":
            out["trades"], out["bad_trades"] = _trade_rows(conn)
        if bar_state == "ok":
            out["bars"], out["bad_bars"] = _cand_bar_rows(conn)
        if v0_state == "ok":
            out["v0_bars"], out["bad_v0"] = _v0_bar_rows(conn)
        if state == "ok" and all(item == "schema" for item in out["tables"].values()):
            out["state"] = "schema"
        return out
    except sqlite3.Error:
        out["state"] = "unreadable"
        return out
    finally:
        conn.close()


def _nav_rows(conn: sqlite3.Connection) -> tuple[dict[str, list[dict]], list[str]]:
    bad: set[str] = set()
    grouped: dict[str, list[dict]] = {}
    rows = conn.execute(
        "SELECT version, ts, ret, nav, n_long, n_short, n_sym, recorded_at "
        "FROM cand_nav ORDER BY version, ts LIMIT ?",
        (NAV_READ_CAP,),
    )
    for row in rows:
        version = str(row["version"])
        if required_bad(row["ret"]) or required_bad(row["nav"]) or required_bad(row["ts"]):
            bad.add(version)
            continue
        grouped.setdefault(str(row["version"]), []).append(
            {
                "ts": int(row["ts"]),
                "ret": float(row["ret"]),
                "nav": float(row["nav"]),
                "n_long": int(row["n_long"]),
                "n_short": int(row["n_short"]),
                "n_sym": int(row["n_sym"]),
                "recorded_at": int(row["recorded_at"]) if row["recorded_at"] is not None else None,
            }
        )
    return grouped, sorted(bad)


def _trade_rows(conn: sqlite3.Connection) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    bad: set[str] = set()
    grouped: dict[str, dict[str, list[dict]]] = {}
    rows = conn.execute(
        "SELECT version, symbol, side, entry_ts, entry_px, exit_ts, exit_px, gross, funding, net, recorded_at "
        "FROM cand_trades ORDER BY version, symbol, entry_ts LIMIT ?",
        (TRADE_READ_CAP,),
    )
    for row in rows:
        version = str(row["version"])
        if required_bad(row["entry_ts"]) or required_bad(row["entry_px"]) or bad_number(row["exit_ts"]) or bad_number(row["exit_px"]) or bad_number(row["gross"]) or bad_number(row["funding"]) or bad_number(row["net"]):
            bad.add(version)
            continue
        symbol = str(row["symbol"] or "")
        item = {
            "symbol": symbol,
            "side": str(row["side"] or ""),
            "entry_ts": int(row["entry_ts"]),
            "entry_px": float(row["entry_px"]),
            "exit_ts": None if row["exit_ts"] is None else int(row["exit_ts"]),
            "exit_px": None if row["exit_px"] is None else float(row["exit_px"]),
            "gross": None if row["gross"] is None else float(row["gross"]),
            "funding": None if row["funding"] is None else float(row["funding"]),
            "net": None if row["net"] is None else float(row["net"]),
            "recorded_at": int(row["recorded_at"]) if row["recorded_at"] is not None else None,
        }
        grouped.setdefault(version, {}).setdefault(symbol, []).append(item)
    return grouped, sorted(bad)


def _cand_bar_rows(conn: sqlite3.Connection) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    bad: set[str] = set()
    grouped: dict[str, dict[str, list[dict]]] = {}
    rows = conn.execute(
        "SELECT version, params_hash, symbol, tf, asof_ts, side, score, action, factors, recorded_at "
        "FROM cand_bars ORDER BY version, symbol, asof_ts LIMIT ?",
        (BAR_READ_CAP,),
    )
    for row in rows:
        version = str(row["version"])
        if required_bad(row["score"]) or required_bad(row["asof_ts"]):
            bad.add(version)
            continue
        symbol = str(row["symbol"] or "")
        item = {
            "symbol": symbol,
            "tf": str(row["tf"] or ""),
            "asof_ts": int(row["asof_ts"]),
            "side": str(row["side"] or ""),
            "score": float(row["score"]),
            "action": str(row["action"] or ""),
            "factors": _loads(row["factors"], None),
            "reason_tags": None,
            "params_hash": str(row["params_hash"] or ""),
            "recorded_at": int(row["recorded_at"]) if row["recorded_at"] is not None else None,
        }
        grouped.setdefault(version, {}).setdefault(symbol, []).append(item)
    return grouped, sorted(bad)


def _v0_bar_rows(conn: sqlite3.Connection) -> tuple[dict[str, dict[str, list[dict]]], list[str]]:
    bad: set[str] = set()
    grouped: dict[str, dict[str, list[dict]]] = {}
    rows = conn.execute(
        "SELECT version, symbol, tf, asof_ts, side, score, action, reason_tags, factors, recorded_at "
        "FROM bars ORDER BY version, symbol, asof_ts LIMIT ?",
        (BAR_READ_CAP,),
    )
    for row in rows:
        version = str(row["version"])
        if required_bad(row["score"]) or required_bad(row["asof_ts"]):
            bad.add(version)
            continue
        symbol = str(row["symbol"] or "")
        tags = _loads(row["reason_tags"], None)
        item = {
            "symbol": symbol,
            "tf": str(row["tf"] or ""),
            "asof_ts": int(row["asof_ts"]),
            "side": str(row["side"] or ""),
            "score": float(row["score"]),
            "action": str(row["action"] or ""),
            "factors": _loads(row["factors"], None),
            "reason_tags": tags if isinstance(tags, list) else None,
            "params_hash": None,
            "recorded_at": int(row["recorded_at"]) if row["recorded_at"] is not None else None,
        }
        grouped.setdefault(version, {}).setdefault(symbol, []).append(item)
    return grouped, sorted(bad)


def _read_paper(path: Path) -> dict[str, Any]:
    state, conn = _connect(path)
    out: dict[str, Any] = {
        "state": state,
        "basename": _PAPER,
        "runs": {},
        "fills": {},
        "start_nav": None,
        "bad_runs": [],
    }
    if conn is None:
        return out
    need_runs = {"day", "strategy", "ran_at", "nav_close", "ret", "cost", "funding"}
    need_fills = {"day", "coin", "side", "price", "fill_price", "fee", "slippage"}
    try:
        runs_state = _ready(conn, "runs", need_runs)
        fills_state = _ready(conn, "fills", need_fills)
        out["tables"] = {"runs": runs_state, "fills": fills_state}
        if _ready(conn, "meta", {"key", "value"}) == "ok":
            row = conn.execute("SELECT value FROM meta WHERE key='start_nav'").fetchone()
            if row is not None and not bad_number(row["value"]):
                out["start_nav"] = float(row["value"]) if row["value"] is not None else None
        if runs_state == "ok":
            out["runs"], out["bad_runs"] = _paper_runs(conn)
        if fills_state == "ok":
            out["fills"] = _paper_fills(conn)
        return out
    except sqlite3.Error:
        out["state"] = "unreadable"
        return out
    finally:
        conn.close()


def _paper_runs(conn: sqlite3.Connection) -> tuple[dict[str, list[dict]], list[str]]:
    bad: set[str] = set()
    grouped: dict[str, list[dict]] = {}
    rows = conn.execute(
        "SELECT day, strategy, ran_at, nav_close, ret, cost, funding FROM runs ORDER BY strategy, day LIMIT ?",
        (NAV_READ_CAP,),
    )
    for row in rows:
        name = str(row["strategy"])
        if required_bad(row["day"]) or required_bad(row["nav_close"]) or required_bad(row["ret"]) or bad_number(row["cost"]) or bad_number(row["funding"]):
            bad.add(name)
            continue
        grouped.setdefault(name, []).append(
            {
                "ts": int(row["day"]),
                "ret": float(row["ret"]),
                "nav": float(row["nav_close"]),
                "cost": None if row["cost"] is None else float(row["cost"]),
                "funding": None if row["funding"] is None else float(row["funding"]),
                "recorded_at": int(row["ran_at"]) if row["ran_at"] is not None else None,
            }
        )
    return grouped, sorted(bad)


def _paper_fills(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT day, coin, side, price, fill_price, fee, slippage FROM fills ORDER BY day LIMIT ?",
        (TRADE_READ_CAP,),
    )
    out = []
    for row in rows:
        if any(bad_number(row[key]) for key in ("day", "price", "fill_price", "fee", "slippage")):
            continue
        out.append(
            {
                "ts": int(row["day"]),
                "symbol": str(row["coin"] or ""),
                "side": str(row["side"] or ""),
                "price": None if row["fill_price"] is None else float(row["fill_price"]),
                "fee": None if row["fee"] is None else float(row["fee"]),
                "slippage": None if row["slippage"] is None else float(row["slippage"]),
            }
        )
    return out


def _read_s3(path: Path) -> dict[str, Any]:
    state, conn = _connect(path)
    out: dict[str, Any] = {"state": state, "basename": _S3, "trades": [], "non_finite": False, "table": "missing"}
    if conn is None:
        return out
    need = {"coin", "signal_ts", "status", "entry_px", "exit_px", "gross", "funding", "cost", "net", "entry_bar", "exit_bar"}
    try:
        ready = _ready(conn, "trades", need)
        out["table"] = ready
        if ready != "ok":
            out["state"] = "schema" if state == "ok" else state
            return out
        rows = conn.execute(
            "SELECT coin, signal_ts, status, entry_px, exit_px, gross, funding, cost, net, entry_bar, exit_bar "
            "FROM trades ORDER BY signal_ts LIMIT ?",
            (TRADE_READ_CAP,),
        )
        for row in rows:
            if required_bad(row["signal_ts"]) or bad_number(row["entry_px"]) or bad_number(row["exit_px"]) or bad_number(row["gross"]) or bad_number(row["funding"]) or bad_number(row["cost"]) or bad_number(row["net"]):
                out["non_finite"] = True
                continue
            out["trades"].append(
                {
                    "symbol": str(row["coin"] or ""),
                    "ts": int(row["signal_ts"]),
                    "status": str(row["status"] or ""),
                    "entry_ts": None if row["entry_bar"] is None else int(row["entry_bar"]),
                    "exit_ts": None if row["exit_bar"] is None else int(row["exit_bar"]),
                    "entry_px": None if row["entry_px"] is None else float(row["entry_px"]),
                    "exit_px": None if row["exit_px"] is None else float(row["exit_px"]),
                    "gross": None if row["gross"] is None else float(row["gross"]),
                    "funding": None if row["funding"] is None else float(row["funding"]),
                    "cost": None if row["cost"] is None else float(row["cost"]),
                    "net": None if row["net"] is None else float(row["net"]),
                }
            )
        return out
    except sqlite3.Error:
        out["state"] = "unreadable"
        return out
    finally:
        conn.close()


def _read_h2(path: Path) -> dict[str, Any]:
    state, conn = _connect(path)
    out: dict[str, Any] = {"state": state, "basename": _H2, "days": [], "non_finite": False, "table": "missing"}
    if conn is None:
        return out
    need = {"day", "computed_at", "nav", "ret", "cost_sleeve", "drawdown"}
    try:
        ready = _ready(conn, "days", need)
        out["table"] = ready
        if ready != "ok":
            out["state"] = "schema" if state == "ok" else state
            return out
        rows = conn.execute(
            "SELECT day, computed_at, nav, ret, cost_sleeve, drawdown FROM days ORDER BY day LIMIT ?",
            (NAV_READ_CAP,),
        )
        for row in rows:
            if required_bad(row["day"]) or required_bad(row["nav"]) or required_bad(row["ret"]) or bad_number(row["cost_sleeve"]) or bad_number(row["drawdown"]):
                out["non_finite"] = True
                continue
            out["days"].append(
                {
                    "ts": int(row["day"]),
                    "ret": float(row["ret"]),
                    "nav": float(row["nav"]),
                    "cost": None if row["cost_sleeve"] is None else float(row["cost_sleeve"]),
                    "drawdown": None if row["drawdown"] is None else float(row["drawdown"]),
                    "recorded_at": int(row["computed_at"]) if row["computed_at"] is not None else None,
                }
            )
        return out
    except sqlite3.Error:
        out["state"] = "unreadable"
        return out
    finally:
        conn.close()
