"""Read-only console event ring.

Observers append real paper / decision / discovery / shadow / system rows.
Nothing here submits orders, edits strategy params, or arms live trading.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.paper.postmortem import exit_reason_of

RING_CAP = 2_000
EVENT_TYPES = ("discovery", "entry", "exit", "reject", "shadow", "system")

_EXIT_PILL = {
    "take_profit": "TP",
    "stop_loss": "SL",
    "max_hold": "timeout",
    "sell_pressure": "weak-tape",
    "graduation": "graduation",
    "orphan": "orphan",
    "other": "平仓",
}

_LOCK = threading.Lock()
_events: list[dict[str, Any]] = []
_ids: set[str] = set()
_seq = 0
_restart_noted = False
_status_auto: Optional[bool] = None
_disc_key: Optional[tuple[str, str]] = None

try:
    _SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:
    _SHANGHAI = timezone(timedelta(hours=8))


def reset_events() -> None:
    """Drop the in-memory ring. Does not touch journals or shadow config."""
    global _seq, _restart_noted, _status_auto, _disc_key
    with _LOCK:
        _events.clear()
        _ids.clear()
        _seq = 0
        _restart_noted = False
        _status_auto = None
        _disc_key = None


def shanghai_day_start_ms(now_ms: Optional[int] = None) -> int:
    """Epoch ms at 00:00:00 Asia/Shanghai for the day containing now_ms."""
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    local = datetime.fromtimestamp(ms / 1000.0, tz=_SHANGHAI)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _push(
    *,
    id: str,
    ts: int,
    type: str,
    pill: str,
    message: str,
    pnl: Optional[float] = None,
    net_bps: Optional[float] = None,
    symbol: str = "",
    mint: str = "",
) -> None:
    global _seq
    if type not in EVENT_TYPES:
        return
    with _LOCK:
        if id in _ids:
            return
        _seq += 1
        _ids.add(id)
        _events.append(
            {
                "id": id,
                "cursor": _seq,
                "ts": int(ts),
                "type": type,
                "pill": pill,
                "message": message,
                "pnl": None if pnl is None else float(pnl),
                "net_bps": None if net_bps is None else float(net_bps),
                "symbol": symbol or "",
                "mint": mint or "",
            }
        )
        if len(_events) > RING_CAP:
            del _events[: len(_events) - RING_CAP]


def _base(symbol: str) -> str:
    raw = symbol or ""
    return raw.split("/")[0] if "/" in raw else raw


def _px(n: float) -> str:
    a = abs(float(n))
    if a >= 1:
        text = f"{n:.4f}"
    elif a >= 0.0001:
        text = f"{n:.6f}"
    else:
        text = f"{n:.8f}"
    return text.rstrip("0").rstrip(".") or "0"


def _qty(n: float) -> str:
    a = abs(float(n))
    if a >= 1:
        text = f"{n:.4f}"
    else:
        text = f"{n:.6f}"
    return text.rstrip("0").rstrip(".") or "0"


def note_api_start() -> None:
    """One system row per process (or per reset_events)."""
    global _restart_noted
    with _LOCK:
        if _restart_noted:
            return
        _restart_noted = True
    _push(
        id=f"system:restart:{int(time.time() * 1000)}",
        ts=int(time.time() * 1000),
        type="system",
        pill="系统",
        message="API 已重启",
    )


def note_autopaper(on: bool) -> None:
    global _status_auto
    flag = bool(on)
    _status_auto = flag
    ts = int(time.time() * 1000)
    _push(
        id=f"system:autopaper:{ts}:{int(flag)}",
        ts=ts,
        type="system",
        pill="系统",
        message="自动纸面 开" if flag else "自动纸面 关",
    )


def note_discovery_status(active: str, reason: str) -> None:
    global _disc_key
    key = (str(active or ""), str(reason or ""))
    _disc_key = key
    ts = int(time.time() * 1000)
    _push(
        id=f"system:discovery:{ts}:{key[0]}:{key[1]}",
        ts=ts,
        type="system",
        pill="系统",
        message=_discovery_message(key[0], key[1]),
    )


def _discovery_message(active: str, reason: str) -> str:
    if "reject" in reason or reason == "portal_auth_rejected":
        return "发现离线"
    if reason == "connecting":
        return f"发现连接中 {active}".strip()
    if active and active not in {"off", "idle"} and reason not in {"off", "idle"}:
        return f"发现在线 {active}"
    if active and active not in {"off", "idle"} and reason in {"", "ok", "live", "online"}:
        return f"发现在线 {active}"
    return "发现离线"


def note_discovery(*, mint: str, symbol: str, ts: int, creator: str = "") -> None:
    mint_s = str(mint or "").strip()
    if not mint_s:
        return
    base = _base(symbol) or mint_s[:6]
    short = mint_s if len(mint_s) <= 10 else f"{mint_s[:4]}…{mint_s[-4:]}"
    who = f" {creator[:6]}" if creator else ""
    _push(
        id=f"discovery:{mint_s}",
        ts=int(ts or time.time() * 1000),
        type="discovery",
        pill="发现",
        message=f"新币 {base} {short}{who}".strip(),
        symbol=base,
        mint=mint_s,
    )


def note_journal_fill(
    symbol: str,
    fill: Any,
    *,
    reason: str = "",
    mint: Optional[str] = None,
    closed: Optional[list[Any]] = None,
    opened: Any = None,
) -> None:
    """Entry for a new paper lot, exit for each closed round-trip. Live fills never arrive here."""
    ts = int(getattr(fill, "ts", 0) or 0)
    mint_s = str(mint or getattr(opened, "mint", "") or "")
    if opened is not None and abs(float(getattr(opened, "qty", 0) or 0)) > 1e-12:
        lot_ts = int(getattr(opened, "ts", ts) or ts)
        lot_px = float(getattr(opened, "price", 0) or 0)
        lot_qty = float(getattr(opened, "qty", 0) or 0)
        _push(
            id=f"entry:{symbol}:{lot_ts}:{lot_px:.8f}",
            ts=lot_ts,
            type="entry",
            pill="开仓",
            message=f"{_base(symbol)} 纸面开仓 {_qty(lot_qty)} @ {_px(lot_px)} SOL",
            symbol=symbol,
            mint=str(getattr(opened, "mint", "") or mint_s),
        )
    for trade in closed or []:
        dumped = trade.as_dict() if hasattr(trade, "as_dict") else dict(trade)
        why = exit_reason_of(dumped)
        if not why or why == "other":
            mapped = exit_reason_of({"tags": dumped.get("tags") or [], "reason": reason, "exit_reason": reason})
            if mapped and mapped != "other":
                why = mapped
        pill = _EXIT_PILL.get(why, "平仓")
        net_bps = float(getattr(trade, "pnl_pct", 0) or 0) * 10_000.0
        pnl = float(getattr(trade, "pnl", 0) or 0)
        sign = "+" if net_bps > 0 else ""
        _push(
            id=f"exit:{getattr(trade, 'id', '')}",
            ts=int(getattr(trade, "exit_ts", ts) or ts),
            type="exit",
            pill=pill,
            message=f"{_base(symbol)} {pill} 净 {sign}{net_bps:.0f} bps",
            pnl=pnl,
            net_bps=net_bps,
            symbol=str(getattr(trade, "symbol", symbol) or symbol),
            mint=str(getattr(trade, "mint", "") or mint_s),
        )


def note_decision(row: Any) -> None:
    outcome = str(getattr(row, "outcome", "") or "")
    bucket = str(getattr(row, "reject_bucket", "") or "")
    if outcome != "reject" or bucket not in {"impact", "risk"}:
        return
    ts = int(getattr(row, "ts", 0) or 0)
    symbol = str(getattr(row, "symbol", "") or "")
    reason = str(getattr(row, "signal_reason", "") or "")
    tags = [str(t) for t in (getattr(row, "risk_tags", None) or []) if t]
    ident = f"reject:{ts}:{symbol}:{bucket}:{reason}:{','.join(tags)}"
    _push(
        id=ident,
        ts=ts or int(time.time() * 1000),
        type="reject",
        pill="拒绝",
        message=_reject_message(row, bucket, reason, tags),
        symbol=symbol,
        mint=str(getattr(row, "mint", "") or ""),
    )


def _reject_message(row: Any, bucket: str, reason: str, tags: list[str]) -> str:
    base = _base(str(getattr(row, "symbol", "") or "")) or "—"
    if bucket == "impact":
        parts = [base, "冲击"]
        gross = getattr(row, "impact_gross_bps", None)
        if gross is None:
            gross = getattr(row, "impact_bps_est", None)
        if gross is None:
            gross = getattr(row, "estimated_impact_bps", None)
        if gross is not None:
            parts.append(f"毛 {float(gross):.0f} bps")
        cap = getattr(row, "impact_bps_cap", None)
        if cap is not None:
            parts.append(f"> 上限 {float(cap):.0f}")
        elif reason:
            parts.append(reason)
        return " ".join(parts)
    parts = [base, "限额"]
    if reason:
        parts.append(reason)
    elif tags:
        parts.append(",".join(tags[:3]))
    return " ".join(parts)


def note_shadow_decision(row: dict[str, Any]) -> None:
    action = str(row.get("action") or "")
    if action not in {"enter", "skip"}:
        return
    ts = int(row.get("ts") or 0)
    set_id = str(row.get("set_id") or "")
    symbol = str(row.get("symbol") or "")
    reason = str(row.get("reason") or "")
    verb = "虚拟开仓" if action == "enter" else "虚拟跳过"
    _push(
        id=f"shadow:{set_id}:{symbol}:{ts}:{action}:{reason}",
        ts=ts or int(time.time() * 1000),
        type="shadow",
        pill="影子",
        message=f"影子 {set_id} {_base(symbol)} {verb} {reason}".strip(),
        symbol=symbol,
        mint=str(row.get("mint") or ""),
    )


def note_shadow_close(row: dict[str, Any]) -> None:
    ts = int(row.get("exit_ts") or row.get("ts") or 0)
    set_id = str(row.get("set_id") or "")
    symbol = str(row.get("symbol") or "")
    reason = str(row.get("exit_reason") or row.get("reason") or "")
    pill = _EXIT_PILL.get(reason, "平仓")
    pnl = row.get("pnl")
    net = row.get("net_bps")
    try:
        pnl_f = None if pnl is None else float(pnl)
    except (TypeError, ValueError):
        pnl_f = None
    try:
        net_f = None if net is None else float(net)
    except (TypeError, ValueError):
        net_f = None
    _push(
        id=f"shadow:{set_id}:{symbol}:{ts}:exit:{reason}",
        ts=ts or int(time.time() * 1000),
        type="shadow",
        pill="影子",
        message=f"影子 {set_id} {_base(symbol)} 虚拟平仓 {pill}",
        pnl=pnl_f,
        net_bps=net_f,
        symbol=symbol,
        mint=str(row.get("mint") or ""),
    )


def _backfill() -> None:
    """Replay journals that were loaded from disk or written before this process hooked them."""
    try:
        from app.paper.ledger import get_paper_journal

        journal = get_paper_journal()
        for trade in list(journal.closed):
            if (getattr(trade, "source", "") or "") == "live":
                continue
            note_journal_fill(
                trade.symbol,
                type("F", (), {"ts": trade.exit_ts})(),
                reason="",
                mint=trade.mint,
                closed=[trade],
                opened=None,
            )
        for symbol, lots in list(journal.lots.items()):
            for lot in list(lots):
                if abs(float(lot.qty)) <= 1e-12:
                    continue
                note_journal_fill(
                    symbol,
                    type("F", (), {"ts": lot.ts})(),
                    mint=lot.mint,
                    closed=[],
                    opened=lot,
                )
    except Exception:
        pass
    try:
        from app.paper.decision_log import get_decision_log

        for row in get_decision_log().all():
            note_decision(row)
    except Exception:
        pass
    try:
        from app.paper.shadow_compare import shadow_closed, shadow_decisions

        for row in shadow_decisions():
            note_shadow_decision(row)
        for row in shadow_closed():
            note_shadow_close(row)
    except Exception:
        pass
    try:
        from app.providers import get_provider

        provider = get_provider()
        curves = getattr(provider, "_curves", None)
        lock = getattr(provider, "_lock", None)
        if isinstance(curves, dict):

            def read() -> list[tuple[str, str, str]]:
                found: list[tuple[str, str, str]] = []
                for curve in curves.values():
                    if not getattr(curve, "discovered", False):
                        continue
                    found.append(
                        (
                            str(getattr(curve, "base", "") or ""),
                            str(getattr(curve, "mint", "") or ""),
                            str(getattr(curve, "symbol", "") or ""),
                        )
                    )
                return found

            rows = read() if lock is None else _locked(lock, read)
            now = int(time.time() * 1000)
            for base, mint, symbol in rows:
                note_discovery(mint=mint, symbol=base or symbol, ts=now)
    except Exception:
        pass


def _locked(lock: Any, fn: Any) -> Any:
    with lock:
        return fn()


def _sample_status() -> None:
    global _status_auto, _disc_key
    try:
        from app.discovery import discovery_health_fields

        fields = discovery_health_fields()
        key = (str(fields.get("discoveryActive") or ""), str(fields.get("discoveryReason") or ""))
        if key != _disc_key:
            note_discovery_status(key[0], key[1])
    except Exception:
        pass
    try:
        from app.strategies.pump_paper_v1 import get_engine

        auto = bool(get_engine().params.auto_paper_orders)
        if auto != _status_auto:
            note_autopaper(auto)
    except Exception:
        pass


def _open_positions() -> int:
    from app.paper.ledger import get_paper_journal
    from app.strategies.pump_paper_v1 import get_engine

    journal = get_paper_journal()
    n = 0
    symbols: set[str] = set()
    for symbol, lots in journal.lots.items():
        for lot in lots:
            if abs(float(lot.qty)) <= 1e-12:
                continue
            n += 1
            symbols.add(symbol)
    for pos in get_engine().positions.values():
        if pos.symbol in symbols:
            continue
        if abs(float(pos.qty)) <= 1e-12:
            continue
        n += 1
    return n


def console_stats() -> dict[str, Any]:
    from app.paper.executability import build_executability
    from app.paper.ledger import get_paper_journal

    start = shanghai_day_start_ms()
    end = start + 86_400_000
    trades = [
        t
        for t in get_paper_journal().closed
        if (t.source or "") != "live" and start <= int(t.exit_ts) < end
    ]
    n = len(trades)
    pnl = sum(float(t.pnl) for t in trades) if n else 0.0
    avg_bps = (sum(float(t.pnl_pct) * 10_000.0 for t in trades) / n) if n else None
    try:
        exe = build_executability(window="session")
    except Exception:
        exe = {}
    return {
        "open_positions": _open_positions(),
        "closed_today": n,
        "pnl_today": pnl,
        "avg_net_bps": avg_bps,
        "day_start_ts": start,
        "verdict": exe.get("verdict") or "no-go",
        "lamp": exe.get("lamp") or "gray",
        "nogo_reason": exe.get("nogo_reason") or "",
        "go_window_label": "round8b",
        "mode": "paper",
        "liveEnabled": False,
        "liveDisabled": True,
    }


def _symbol_for_mint(mint: str) -> str:
    if not mint:
        return ""
    try:
        from app.providers import get_provider

        provider = get_provider()
        curves = getattr(provider, "_curves", None)
        lock = getattr(provider, "_lock", None)
        if not isinstance(curves, dict):
            return ""

        def read() -> str:
            for curve in curves.values():
                if str(getattr(curve, "mint", "") or "") == mint:
                    return str(getattr(curve, "base", "") or getattr(curve, "symbol", "") or "")
            return ""

        return read() if lock is None else _locked(lock, read)
    except Exception:
        return ""


def _on_bus_event(event: dict[str, Any]) -> None:
    if event.get("type") != "new_token":
        return
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return
    mint = str(payload.get("mint") or "")
    if not mint:
        return
    try:
        ts = int(payload.get("ts") or time.time() * 1000)
    except (TypeError, ValueError):
        ts = int(time.time() * 1000)
    note_discovery(
        mint=mint,
        symbol=_symbol_for_mint(mint) or mint,
        ts=ts,
        creator=str(payload.get("creator") or ""),
    )


def _install_bus_listener() -> None:
    try:
        from app.bus import get_hub

        get_hub().add_sync_listener(_on_bus_event)
    except Exception:
        return


_install_bus_listener()


def build_events(*, since: Optional[str] = None, limit: int = 300) -> dict[str, Any]:
    """Incremental read. `since` is a cursor (small int) or an epoch-ms timestamp."""
    _backfill()
    _sample_status()
    cap = max(1, min(int(limit or 300), 1000))
    with _LOCK:
        rows = list(_events)
        cursor = _seq
    raw = (since or "").strip()
    if raw:
        try:
            mark = int(raw)
        except ValueError:
            mark = None
        if mark is not None:
            if mark >= 100_000_000_000:
                rows = [row for row in rows if int(row["ts"]) > mark]
            else:
                rows = [row for row in rows if int(row["cursor"]) > mark]
    rows.sort(key=lambda row: (int(row["ts"]), int(row["cursor"])))
    if len(rows) > cap:
        rows = rows[-cap:]
    return {
        "mode": "paper",
        "liveEnabled": False,
        "liveDisabled": True,
        "events": rows,
        "cursor": str(cursor),
        "types": list(EVENT_TYPES),
        "stats": console_stats(),
    }
