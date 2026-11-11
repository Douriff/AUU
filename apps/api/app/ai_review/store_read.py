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
        "truncated_nav": [],
        "truncated_trades": [],
        "truncated_bars": [],
        "truncated_v0": [],
        "nav_counts": {},
        "trade_counts": {},
        "bar_counts": {},
        "v0_counts": {},
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
            out["nav"], out["bad_nav"], out["truncated_nav"], out["nav_counts"] = _nav_rows(conn)
        if trade_state == "ok":
            out["trades"], out["bad_trades"], out["truncated_trades"], out["trade_counts"] = _trade_rows(conn)
        if bar_state == "ok":
            out["bars"], out["bad_bars"], out["truncated_bars"], out["bar_counts"] = _cand_bar_rows(conn)
        if v0_state == "ok":
            out["v0_bars"], out["bad_v0"], out["truncated_v0"], out["v0_counts"] = _v0_bar_rows(conn)
        if state == "ok" and all(item == "schema" for item in out["tables"].values()):
            out["state"] = "schema"
        return out
    except sqlite3.Error:
        out["state"] = "unreadable"
        return out
    finally:
        conn.close()


def _counts(conn: sqlite3.Connection, table: str, key: str) -> dict[str, int]:
    if key not in {"version"} or table not in {"cand_nav", "cand_trades", "cand_bars", "bars"}:
        raise ValueError("unexpected ledger query")
    return {str(row[0]): int(row[1]) for row in conn.execute(f"SELECT {key}, COUNT(*) FROM {table} GROUP BY {key}")}


def _nav_rows(conn: sqlite3.Connection) -> tuple[dict[str, list[dict]], list[str], list[str], dict[str, int]]:
    """Load each strategy on its own. A strategy over the cap is omitted, not silently shortened."""
    bad: set[str] = set()
    truncated: list[str] = []
    grouped: dict[str, list[dict]] = {}
    counts = _counts(conn, "cand_nav", "version")
    for version, count in counts.items():
        if count > NAV_READ_CAP:
            truncated.append(version)
            continue
        rows = conn.execute(
            "SELECT ts, ret, nav, n_long, n_short, n_sym, recorded_at FROM cand_nav WHERE version=? ORDER BY ts",
            (version,),
        )
        bucket: list[dict] = []
        spoiled = False
        for row in rows:
            if required_bad(row["ret"]) or required_bad(row["nav"]) or required_bad(row["ts"]):
                spoiled = True
                break
            bucket.append(
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
        if spoiled:
            bad.add(version)
            continue
        if len(bucket) != count:
            truncated.append(version)
            continue
        grouped[version] = bucket
    return grouped, sorted(bad), sorted(truncated), counts


def _trade_rows(conn: sqlite3.Connection) -> tuple[dict[str, dict[str, list[dict]]], list[str], list[str], dict[str, int]]:
    bad: set[str] = set()
    truncated: list[str] = []
    grouped: dict[str, dict[str, list[dict]]] = {}
    counts = _counts(conn, "cand_trades", "version")
    for version, count in counts.items():
        if count > TRADE_READ_CAP:
            truncated.append(version)
            continue
        rows = conn.execute(
            "SELECT symbol, side, entry_ts, entry_px, exit_ts, exit_px, gross, funding, net, recorded_at "
            "FROM cand_trades WHERE version=? ORDER BY symbol, entry_ts",
            (version,),
        )
        bucket: dict[str, list[dict]] = {}
        seen = 0
        spoiled = False
        for row in rows:
            seen += 1
            if required_bad(row["entry_ts"]) or required_bad(row["entry_px"]) or bad_number(row["exit_ts"]) or bad_number(row["exit_px"]) or bad_number(row["gross"]) or bad_number(row["funding"]) or bad_number(row["net"]):
                spoiled = True
                break
            symbol = str(row["symbol"] or "")
            bucket.setdefault(symbol, []).append(
                {
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
            )
        if spoiled:
            bad.add(version)
            continue
        if seen != count:
            truncated.append(version)
            continue
        grouped[version] = bucket
    return grouped, sorted(bad), sorted(truncated), counts


def _bar_bucket(row: sqlite3.Row, *, with_hash: bool) -> dict:
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
        "params_hash": str(row["params_hash"] or "") if with_hash else None,
        "recorded_at": int(row["recorded_at"]) if row["recorded_at"] is not None else None,
    }
    if not with_hash:
        tags = _loads(row["reason_tags"], None)
        item["reason_tags"] = tags if isinstance(tags, list) else None
    return item


def _bar_rows(conn: sqlite3.Connection, table: str, with_hash: bool):
    bad: set[str] = set()
    truncated: list[str] = []
    grouped: dict[str, dict[str, list[dict]]] = {}
    counts = _counts(conn, table, "version")
    extra = ", params_hash" if with_hash else ", reason_tags"
    for version, count in counts.items():
        if count > BAR_READ_CAP:
            truncated.append(version)
            continue
        rows = conn.execute(
            "SELECT symbol, tf, asof_ts, side, score, action, factors, recorded_at"
            f"{extra} FROM {table} WHERE version=? ORDER BY symbol, asof_ts",
            (version,),
        )
        bucket: dict[str, list[dict]] = {}
        seen = 0
        spoiled = False
        for row in rows:
            seen += 1
            if required_bad(row["score"]) or required_bad(row["asof_ts"]):
                spoiled = True
                break
            item = _bar_bucket(row, with_hash=with_hash)
            bucket.setdefault(item["symbol"], []).append(item)
        if spoiled:
            bad.add(version)
            continue
        if seen != count:
            truncated.append(version)
            continue
        grouped[version] = bucket
    return grouped, sorted(bad), sorted(truncated), counts


def _cand_bar_rows(conn: sqlite3.Connection):
    return _bar_rows(conn, "cand_bars", True)


def _v0_bar_rows(conn: sqlite3.Connection):
    return _bar_rows(conn, "bars", False)


def _read_paper(path: Path) -> dict[str, Any]:
    state, conn = _connect(path)
    out: dict[str, Any] = {
        "state": state,
        "basename": _PAPER,
        "runs": {},
        "fills": {},
        "start_nav": None,
        "bad_runs": [],
        "truncated_runs": [],
        "run_counts": {},
        "fills_attribution": "not_loaded",
        "fills_truncated": False,
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
            out["runs"], out["bad_runs"], out["truncated_runs"], out["run_counts"] = _paper_runs(conn)
        if fills_state == "ok":
            out["fills"], out["fills_attribution"], out["fills_truncated"] = _paper_fills(conn, runs_ready=runs_state == "ok")
        return out
    except sqlite3.Error:
        out["state"] = "unreadable"
        return out
    finally:
        conn.close()


def _paper_runs(conn: sqlite3.Connection):
    bad: set[str] = set()
    truncated: list[str] = []
    grouped: dict[str, list[dict]] = {}
    counts = {
        str(row[0]): int(row[1])
        for row in conn.execute("SELECT strategy, COUNT(*) FROM runs GROUP BY strategy")
    }
    for name, count in counts.items():
        if count > NAV_READ_CAP:
            truncated.append(name)
            continue
        rows = conn.execute(
            "SELECT day, ran_at, nav_close, ret, cost, funding FROM runs WHERE strategy=? ORDER BY day",
            (name,),
        )
        bucket = []
        spoiled = False
        for row in rows:
            if required_bad(row["day"]) or required_bad(row["nav_close"]) or required_bad(row["ret"]) or bad_number(row["cost"]) or bad_number(row["funding"]):
                spoiled = True
                break
            bucket.append(
                {
                    "ts": int(row["day"]),
                    "ret": float(row["ret"]),
                    "nav": float(row["nav_close"]),
                    "cost": None if row["cost"] is None else float(row["cost"]),
                    "funding": None if row["funding"] is None else float(row["funding"]),
                    "recorded_at": int(row["ran_at"]) if row["ran_at"] is not None else None,
                }
            )
        if spoiled:
            bad.add(name)
            continue
        if len(bucket) != count:
            truncated.append(name)
            continue
        grouped[name] = bucket
    return grouped, sorted(bad), sorted(truncated), counts


def _paper_fills(conn: sqlite3.Connection, *, runs_ready: bool) -> tuple[list[dict], str, bool]:
    """fills has no strategy column. A fill belongs to a strategy only via the one run on that day."""
    stored = int(conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0])
    if stored > TRADE_READ_CAP:
        return [], "truncated", True
    fill_cols = _columns(conn, "fills") or set()
    if "strategy" in fill_cols:
        sql = "SELECT day, coin, side, price, fill_price, fee, slippage, strategy FROM fills ORDER BY day"
        attribution = "fill_strategy_column"
    elif runs_ready:
        sql = (
            "SELECT f.day AS day, f.coin AS coin, f.side AS side, f.price AS price, f.fill_price AS fill_price, "
            "f.fee AS fee, f.slippage AS slippage, r.strategy AS strategy "
            "FROM fills f LEFT JOIN runs r ON r.day = f.day ORDER BY f.day"
        )
        attribution = "run_day"
    else:
        sql = "SELECT day, coin, side, price, fill_price, fee, slippage, NULL AS strategy FROM fills ORDER BY day"
        attribution = "no_run_table"
    out = []
    for row in conn.execute(sql):
        if any(bad_number(row[key]) for key in ("day", "price", "fill_price", "fee", "slippage")):
            continue
        strategy = row["strategy"]
        strategy_name = str(strategy) if strategy else None
        out.append(
            {
                "ts": int(row["day"]),
                "symbol": str(row["coin"] or ""),
                "side": str(row["side"] or ""),
                "price": None if row["fill_price"] is None else float(row["fill_price"]),
                "fee": None if row["fee"] is None else float(row["fee"]),
                "slippage": None if row["slippage"] is None else float(row["slippage"]),
                "strategy": strategy_name,
                "attribution": attribution if strategy_name else "no_matching_run",
            }
        )
    return out, attribution, False


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
        stored = int(conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
        out["stored"] = stored
        if stored > TRADE_READ_CAP:
            out["truncated"] = True
            out["trades"] = []
            return out
        rows = conn.execute(
            "SELECT coin, signal_ts, status, entry_px, exit_px, gross, funding, cost, net, entry_bar, exit_bar "
            "FROM trades ORDER BY signal_ts"
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
        stored = int(conn.execute("SELECT COUNT(*) FROM days").fetchone()[0])
        out["stored"] = stored
        if stored > NAV_READ_CAP:
            out["truncated"] = True
            return out
        rows = conn.execute(
            "SELECT day, computed_at, nav, ret, cost_sleeve, drawdown FROM days ORDER BY day"
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


def page_signal_bars(strategy: Optional[str], symbol: Optional[str], limit: int) -> dict[str, Any]:
    """Newest stored bars for one page. `truncated` means more rows exist than this page."""
    path = data_dir() / _SIGNAL
    state, conn = _connect(path)
    empty = {"rows": [], "stored": 0, "truncated": False, "state": state}
    if conn is None:
        return empty
    try:
        rows: list[dict] = []
        stored = 0
        jobs: list[tuple[str, Optional[str], bool]] = []
        if strategy in {None, "", "auu_signal_v0"}:
            jobs.append(("bars", "auu_signal_v0" if strategy else None, False))
        if strategy not in {None, "", "auu_signal_v0"}:
            jobs.append(("cand_bars", strategy, True))
        elif not strategy:
            jobs.append(("cand_bars", None, True))
        for table, version, with_hash in jobs:
            if _columns(conn, table) is None:
                continue
            where: list[str] = []
            args: list[Any] = []
            if version:
                where.append("version=?")
                args.append(version)
            if symbol:
                where.append("symbol=?")
                args.append(symbol)
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            stored += int(conn.execute(f"SELECT COUNT(*) FROM {table}{clause}", args).fetchone()[0])
            extra = ", params_hash" if with_hash else ", reason_tags"
            fetched = conn.execute(
                "SELECT version, symbol, tf, asof_ts, side, score, action, factors, recorded_at"
                f"{extra} FROM {table}{clause} ORDER BY asof_ts DESC LIMIT ?",
                [*args, int(limit)],
            )
            for row in fetched:
                if required_bad(row["score"]) or required_bad(row["asof_ts"]):
                    continue
                item = _bar_bucket(row, with_hash=with_hash)
                item["strategy_id"] = str(row["version"])
                rows.append(item)
        rows.sort(key=lambda item: int(item["asof_ts"]), reverse=True)
        page = rows[: int(limit)]
        return {"rows": page, "stored": stored, "truncated": stored > len(page), "state": "ok"}
    except sqlite3.Error:
        return {**empty, "state": "unreadable"}
    finally:
        conn.close()


def fetch_bar_context(strategy: str, symbol: str, asof_ts: int) -> dict[str, Any]:
    """The requested bar and the stored bar immediately before it."""
    path = data_dir() / _SIGNAL
    table = "bars" if strategy == "auu_signal_v0" else "cand_bars"
    with_hash = table == "cand_bars"
    state, conn = _connect(path)
    if conn is None:
        return {"state": state, "bar": None, "prev": None}
    try:
        if _columns(conn, table) is None:
            return {"state": "schema", "bar": None, "prev": None}
        extra = ", params_hash" if with_hash else ", reason_tags"
        current = conn.execute(
            "SELECT version, symbol, tf, asof_ts, side, score, action, factors, recorded_at"
            f"{extra} FROM {table} WHERE version=? AND symbol=? AND asof_ts=?",
            (strategy, symbol, int(asof_ts)),
        ).fetchone()
        prev = conn.execute(
            "SELECT version, symbol, tf, asof_ts, side, score, action, factors, recorded_at"
            f"{extra} FROM {table} WHERE version=? AND symbol=? AND asof_ts<? ORDER BY asof_ts DESC LIMIT 1",
            (strategy, symbol, int(asof_ts)),
        ).fetchone()

        def one(row: Any) -> Optional[dict]:
            if row is None or required_bad(row["score"]) or required_bad(row["asof_ts"]):
                return None
            return _bar_bucket(row, with_hash=with_hash)

        return {"state": "ok", "bar": one(current), "prev": one(prev)}
    except sqlite3.Error:
        return {"state": "unreadable", "bar": None, "prev": None}
    finally:
        conn.close()
