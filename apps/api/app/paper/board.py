"""Read-only 盘面 snapshot.

Composes the paper journal, monitor rows, executability verdict, and shadow
compare. Does not mutate strategy params, journals, or live gates.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from app.paper.postmortem import exit_reason_of

_EXIT_LABEL = {
    "take_profit": "TP",
    "stop_loss": "SL",
    "max_hold": "timeout",
    "sell_pressure": "pressure",
    "graduation": "graduation",
    "orphan": "orphan",
    "other": "other",
}

_TAPE_N = 40


def _session_candles(provider: Any, symbol: str) -> list[Any]:
    """1m bars already in memory.

    pumpfun_paper.get_candles advances the curve. The monitor call already
    does that, so this reads the seeded buffer and only synthesizes candles
    for providers that have no buffer (mock).
    """
    store = getattr(provider, "_candles", None)
    if isinstance(store, dict):
        lock = getattr(provider, "_lock", None)

        def read() -> list[Any]:
            bucket = store.get(symbol) or {}
            return list(bucket.get("1m") or [])

        if lock is not None:
            with lock:
                return read()
        return read()
    getter = getattr(provider, "get_candles", None)
    if not callable(getter):
        return []
    try:
        return list(getter(symbol, "1m") or [])
    except Exception:
        return []


def _change_pct(candles: list[Any], spot: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Return (price, session change). Change is first-bar open → spot."""
    base: Optional[float] = None
    last: Optional[float] = None
    if candles:
        opened = float(getattr(candles[0], "o", 0) or 0)
        closed = float(getattr(candles[-1], "c", 0) or 0)
        if opened > 0:
            base = opened
        if closed > 0:
            last = closed
    price = spot if spot is not None and spot > 0 else last
    if price is None or base is None or base <= 0:
        return price, None
    return price, (price - base) / base


def _upnl(entry: float, qty: float, mark: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    if mark is None or entry <= 0 or qty == 0:
        return None, None
    if qty > 0:
        pnl = (mark - entry) * qty
        pct = (mark - entry) / entry
    else:
        pnl = (entry - mark) * abs(qty)
        pct = (entry - mark) / entry
    return pnl, pct


def _shadow_col(col: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(col.get("id") or ""),
        "label": str(col.get("label") or ""),
        "n": int(col.get("n") or 0),
        "win_rate": col.get("win_rate"),
        "expectancy": col.get("expectancy"),
        "sample_ok": bool(col.get("sample_ok")),
        "habit_tag": col.get("habit_tag"),
    }


def build_board() -> dict[str, Any]:
    """One poll payload for the 盘面 page. liveEnabled stays false."""
    from app.paper.executability import build_executability
    from app.paper.ledger import get_paper_journal
    from app.paper.shadow_compare import build_shadow_compare
    from app.providers import get_provider
    from app.strategies.pump_paper_v1 import get_engine

    engine = get_engine()
    provider = get_provider()
    rows = engine.monitor_rows()

    ticker: list[dict[str, Any]] = []
    price_by_symbol: dict[str, Optional[float]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        raw_px = row.get("price_sol")
        spot = float(raw_px) if isinstance(raw_px, (int, float)) and raw_px > 0 else None
        candles = _session_candles(provider, symbol)
        price, change = _change_pct(candles, spot)
        price_by_symbol[symbol] = price
        base = symbol.split("/")[0] if "/" in symbol else symbol
        ticker.append(
            {
                "symbol": symbol,
                "base": base,
                "mint": str(row.get("mint") or ""),
                "price": price,
                "change_pct": change,
                "phase": row.get("phase"),
            }
        )

    from app.paper.ledger import excluded_from_autopaper_stats

    journal = get_paper_journal()
    trades = [
        t
        for t in journal.closed
        if (t.source or "") != "live" and not excluded_from_autopaper_stats(t)
    ]
    equity: list[dict[str, Any]] = []
    cum = 0.0
    if trades:
        equity.append({"t": int(trades[0].entry_ts), "pnl": 0.0})
        for trade in trades:
            cum += float(trade.pnl)
            equity.append({"t": int(trade.exit_ts), "pnl": cum})

    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    win_rate = (wins / n) if n else None
    expectancy = (sum(float(t.pnl) for t in trades) / n) if n else None

    tape: list[dict[str, Any]] = []
    for trade in reversed(trades[-_TAPE_N:]):
        reason = exit_reason_of(trade.as_dict())
        tape.append(
            {
                "exit_ts": int(trade.exit_ts),
                "symbol": trade.symbol,
                "mint": trade.mint or "",
                "exit_reason": reason,
                "exit_label": _EXIT_LABEL.get(reason, reason or "other"),
                "net_bps": float(trade.pnl_pct) * 10_000.0,
                "pnl": float(trade.pnl),
            }
        )

    exec_data = build_executability(window="session")
    shadow = build_shadow_compare()

    positions: list[dict[str, Any]] = []
    journal_symbols: set[str] = set()
    for symbol, lots in journal.lots.items():
        for lot in lots:
            if abs(float(lot.qty)) <= 1e-12:
                continue
            journal_symbols.add(symbol)
            mark = price_by_symbol.get(symbol)
            upnl, upct = _upnl(float(lot.price), float(lot.qty), mark)
            positions.append(
                {
                    "symbol": symbol,
                    "mint": lot.mint or "",
                    "qty": float(lot.qty),
                    "entry_price": float(lot.price),
                    "entry_ts": int(lot.ts),
                    "mark": mark,
                    "upnl": upnl,
                    "upnl_pct": upct,
                    "source": "journal",
                }
            )
    for pos in engine.positions.values():
        if pos.symbol in journal_symbols:
            continue
        mark = price_by_symbol.get(pos.symbol)
        upnl, upct = _upnl(float(pos.entry_price), float(pos.qty), mark)
        positions.append(
            {
                "symbol": pos.symbol,
                "mint": pos.mint or "",
                "qty": float(pos.qty),
                "entry_price": float(pos.entry_price),
                "entry_ts": int(pos.entry_ts),
                "mark": mark,
                "upnl": upnl,
                "upnl_pct": upct,
                "source": "strategy",
            }
        )

    return {
        "mode": "paper",
        "liveEnabled": False,
        "liveDisabled": True,
        "asof_ts": int(time.time() * 1000),
        "strategyId": "pump-paper-v1",
        "ticker": ticker,
        "session_pnl": cum,
        "equity_0": float(journal.equity_0),
        "equity": equity,
        "tape": tape,
        "stats": {
            "win_rate": win_rate,
            "expectancy": expectancy,
            "wins": wins,
            "n_closed": n,
            "sample_ok": bool(exec_data.get("sample_ok")),
            "verdict": exec_data.get("verdict") or "no-go",
            "lamp": exec_data.get("lamp") or "gray",
            "nogo_reason": exec_data.get("nogo_reason") or "",
            "go_window_label": shadow.get("go_window_label") or "round8b",
        },
        "positions": positions,
        "shadow": {
            "enabled": bool(shadow.get("enabled")),
            "liveEnabled": False,
            "note": str(shadow.get("note") or ""),
            "go_window_label": shadow.get("go_window_label") or "round8b",
            "main": _shadow_col(shadow.get("main") or {}),
            "sets": [_shadow_col(s) for s in (shadow.get("sets") or [])],
            "asof_ts": int(shadow.get("asof_ts") or 0),
        },
        "empty": n == 0 and not positions,
    }
