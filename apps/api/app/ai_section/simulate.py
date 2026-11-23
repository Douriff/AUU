"""Bounded, request-time paper simulation on stored candles.

Independent of any third-party evaluate() implementation. This runner does
not start the site-wide M3 ledger, does not place orders, and does not run
in the background.

Execution convention (disclosed, not a live fill):
- timestamps are millisecond bar-open times; a bar is closed when ts+tf_ms <= now
- only unique, strictly regular, already-closed OHLC bars are used for signals and fills
- the full parsed window is checked for uniqueness, OHLC, and exact period spacing before any exclusion
- only one legitimate still-forming last bar (ts <= now < ts+tf_ms, already on that grid) may be excluded and disclosed; it is not invented
- a true future bar (ts > now), a gap, an offset or overlapping tail, a duplicate, or illegal OHLC blocks the run
- if the remaining closed history is shorter than required, the run is blocked and no bars are invented
- signals use only completed bars; entries and non-stop exits fill at the
  next bar's open (no same-bar close fill)
- Donchian uses the prior period's high/low channel, not a close-only channel
- a gap through a stop or take fills at the gapped open, not the ideal price
- if stop and take are both reachable in the same bar, the stop is used
- each bar is processed in a fixed, causal order: (1) open-gap risk on an
  already-open position, using only the open; (2) the previous bar's pending
  exit, reverse, or entry at this open; (3) high/low stop and take on whatever
  position remains or was just opened; (4) this close decides the next pending.
  Later extrema in the same bar cannot revise an already-filled open order
- fee_bps and slippage_bps are configurable; zero means a gross, zero-cost
  paper fraction, not an investment return
- fill *details* cap at MAX_FILLS; the window is still walked and totals
  are reported separately when the cap is hit
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.ai_section.schema import KIND_SPECS, TF_MS

MAX_BARS = 300
MAX_FILLS = 40
TS_MIN = 946_684_800_000  # 2000-01-01T00:00:00Z
TS_MAX = 4_102_444_800_000  # 2100-01-01T00:00:00Z


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    acc = 0.0
    for i, price in enumerate(values):
        acc += price
        if i >= period:
            acc -= values[i - period]
        if i >= period - 1:
            out[i] = acc / period
    return out


def rsi(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period < 1 or len(values) < period + 1:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    return out


def _finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number)


def _ms_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):
        return None
    try:
        ts = int(value)
    except (OverflowError, ValueError):
        return None
    if ts < TS_MIN or ts > TS_MAX:
        return None
    return ts


@dataclass
class PreparedBars:
    """Closed bars ready to walk, plus disclosure about a dropped forming tail."""

    bars: list[dict[str, float]]
    reason: str | None
    excluded_unclosed_ts: list[int]
    data_cutoff_ts: int | None
    data_cutoff_close_at: int | None

    def disclosure(self) -> dict[str, Any]:
        return {
            "bars_excluded_unclosed": len(self.excluded_unclosed_ts),
            "excluded_unclosed_ts": list(self.excluded_unclosed_ts),
            "data_cutoff_ts": self.data_cutoff_ts,
            "data_cutoff_close_at": self.data_cutoff_close_at,
        }


def _empty_prepared(reason: str) -> PreparedBars:
    return PreparedBars([], reason, [], None, None)


def prepare_bars(rows: list[dict[str, Any]], timeframe: str, now_ms: int) -> PreparedBars:
    """Accept only already-closed bars; drop a single legitimate forming tail.

    The full parsed window is checked first: uniqueness, OHLC, and adjacent
    period spacing, including any still-forming last bar. Only after that
    grid is valid may a single trailing bar with ts <= now < ts+tf_ms be
    excluded and disclosed. A bar with ts > now is a true future open and
    still blocks; it is not dropped to force a success. A regular
    single-timeframe series can have at most one forming bar; overlapping
    or offset tails fail the grid check and are not silently removed.
    """
    if timeframe not in TF_MS:
        return _empty_prepared("bad_timeframe")
    period = TF_MS[timeframe]
    if not isinstance(rows, list):
        return _empty_prepared("bad_rows")
    bars: list[dict[str, float]] = []
    seen: set[int] = set()
    for row in rows[:MAX_BARS]:
        if not isinstance(row, dict):
            return _empty_prepared("bad_row")
        ts = _ms_int(row.get("ts"))
        if ts is None:
            return _empty_prepared("bad_ts")
        if ts in seen:
            return _empty_prepared("duplicate_ts")
        seen.add(ts)
        try:
            open_px = float(row["open"])
            high_px = float(row["high"])
            low_px = float(row["low"])
            close_px = float(row["close"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return _empty_prepared("bad_ohlc")
        if not all(_finite(v) and v > 0 for v in (open_px, high_px, low_px, close_px)):
            return _empty_prepared("bad_ohlc")
        if not (low_px <= min(open_px, close_px) and max(open_px, close_px) <= high_px):
            return _empty_prepared("bad_ohlc_envelope")
        close_at = ts + period
        bars.append({"ts": float(ts), "open": open_px, "high": high_px, "low": low_px, "close": close_px, "close_at": float(close_at)})
    bars.sort(key=lambda row: row["ts"])
    if len(bars) >= 2:
        for prev, cur in zip(bars, bars[1:]):
            delta = int(cur["ts"] - prev["ts"])
            if delta != period:
                return _empty_prepared("gap" if delta > period else "non_regular_ts")
    for bar in bars:
        if int(bar["ts"]) > now_ms:
            return _empty_prepared("future_or_unclosed")
    excluded: list[int] = []
    if bars:
        last = bars[-1]
        ts = int(last["ts"])
        close_at = int(last["close_at"])
        if ts <= now_ms < close_at:
            bars = bars[:-1]
            excluded = [ts]
    cutoff_ts = int(bars[-1]["ts"]) if bars else None
    cutoff_close = int(bars[-1]["close_at"]) if bars else None
    for bar in bars:
        if int(bar["ts"]) > now_ms or int(bar["close_at"]) > now_ms:
            return PreparedBars([], "future_or_unclosed", excluded, cutoff_ts, cutoff_close)
    return PreparedBars(bars[:MAX_BARS], None, excluded, cutoff_ts, cutoff_close)


def validate_bars(rows: list[dict[str, Any]], timeframe: str, now_ms: int) -> tuple[list[dict[str, float]], str | None]:
    """Return accepted closed bars, or an empty list with a reason to block."""
    prepared = prepare_bars(rows, timeframe, now_ms)
    return prepared.bars, prepared.reason


def load_stored_candles(symbol: str, timeframe: str, limit: int = MAX_BARS) -> list[dict[str, Any]]:
    """Read already-stored mainstream candles only. Do not fetch or invent bars."""
    from app.marketdata.mainstream import get_service

    service = get_service()
    exchange = service.exchange_for_read()
    if not exchange:
        return []
    rows = service.store.candles(exchange, symbol, timeframe, limit=min(limit, MAX_BARS))
    return rows if isinstance(rows, list) else []


def _signal(kind: str, params: dict[str, Any], bars: list[dict[str, float]], index: int, series: dict[str, Any]) -> str:
    """Return long, short, or flat for a completed bar ``index`` (no look-ahead)."""
    if kind == "sma_cross":
        fast = series["fast"]
        slow = series["slow"]
        if index < 1 or fast[index] is None or slow[index] is None or fast[index - 1] is None or slow[index - 1] is None:
            return "flat"
        prev = fast[index - 1] - slow[index - 1]
        now = fast[index] - slow[index]
        if prev <= 0 and now > 0:
            return "long"
        if prev >= 0 and now < 0:
            return "short"
        return "long" if now > 0 else "short" if now < 0 else "flat"
    if kind == "rsi_threshold":
        current = series["rsi"][index]
        if current is None:
            return "flat"
        if current <= params["oversold"]:
            return "long"
        if current >= params["overbought"]:
            return "short"
        return "flat"
    if kind == "donchian_break":
        period = int(params["period"])
        start = index - period
        if start < 0:
            return "flat"
        window = bars[start:index]
        if len(window) < period:
            return "flat"
        upper = max(bar["high"] for bar in window)
        lower = min(bar["low"] for bar in window)
        price = bars[index]["close"]
        if price > upper:
            return "long"
        if price < lower:
            return "short"
        return "flat"
    return "flat"


def _slip_price(price: float, action: str, side: str, slip_bps: float) -> float:
    bump = price * (float(slip_bps) / 10_000.0)
    buying = (action == "enter" and side == "long") or (action == "exit" and side == "short")
    out = price + bump if buying else price - bump
    return out


def _fee(weight: float, fee_bps: float) -> float:
    return abs(weight) * (float(fee_bps) / 10_000.0)


def _blocked(
    status: str,
    reason: str,
    request: dict[str, Any],
    bars_used: int = 0,
    *,
    disclosure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    extra = disclosure or {
        "bars_excluded_unclosed": 0,
        "excluded_unclosed_ts": [],
        "data_cutoff_ts": None,
        "data_cutoff_close_at": None,
    }
    return {
        "status": status,
        "reason": reason,
        "bars_used": bars_used,
        "bars_required": request.get("bars_requested"),
        "fills": [],
        "fill_count": 0,
        "fills_truncated": False,
        "fills_omitted": 0,
        "complete_fills": False,
        "open_position": None,
        "paper_pnl": None,
        "realized_paper_pnl": None,
        "unrealized_paper_pnl": None,
        "returns_published": False,
        "is_investment_return": False,
        "used_model": False,
        "live": False,
        "site_runner": False,
        "request": request,
        **extra,
    }


def simulate_bars(
    strategy: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    now_ms: int | None = None,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
) -> dict[str, Any]:
    need = int(strategy.get("need_bars") or KIND_SPECS[strategy["kind"]]["need_bars"](strategy["params"]))
    clock = int(now_ms if now_ms is not None else time.time() * 1000)
    risk = strategy["risk"]
    fee = float(risk.get("fee_bps") or 0.0) if fee_bps is None else float(fee_bps)
    slip = float(risk.get("slippage_bps") or 0.0) if slippage_bps is None else float(slippage_bps)
    request = {
        "strategy_id": strategy.get("id"),
        "kind": strategy["kind"],
        "symbol": strategy["symbol"],
        "timeframe": strategy["timeframe"],
        "params": strategy["params"],
        "risk": risk,
        "bars_requested": need,
        "ts_unit": "ms",
        "ts_meaning": "bar_open",
        "source": "stored_candles",
        "fee_bps": fee,
        "slippage_bps": slip,
        "execution": "signal_on_closed_bar_fill_next_open",
        "event_order": "open_gap_then_pending_open_orders_then_intrabar_then_close_signal",
        "stop_priority": "conservative_stop_if_both",
        "gap_rule": "fill_at_open_when_gapped_through_stop_or_take",
        "donchian_channel": "prior_high_low" if strategy["kind"] == "donchian_break" else None,
        "now_ms": clock,
    }
    if not rows:
        return _blocked("blocked_no_data", "stored_candles_missing_or_short", request, 0)
    prepared = prepare_bars(rows, strategy["timeframe"], clock)
    request.update(prepared.disclosure())
    bars = prepared.bars
    if prepared.reason:
        return _blocked("blocked_bad_data", prepared.reason, request, 0, disclosure=prepared.disclosure())
    if len(bars) < need:
        return _blocked("blocked_no_data", "stored_candles_missing_or_short", request, len(bars), disclosure=prepared.disclosure())

    closes = [bar["close"] for bar in bars]
    kind = strategy["kind"]
    params = strategy["params"]
    long_only = bool(risk.get("long_only", True))
    weight = float(risk.get("max_weight") or 0.2)
    stop = risk.get("stop_loss_pct")
    take = risk.get("take_profit_pct")
    series: dict[str, Any] = {}
    if kind == "sma_cross":
        series["fast"] = sma(closes, int(params["fast"]))
        series["slow"] = sma(closes, int(params["slow"]))
    elif kind == "rsi_threshold":
        series["rsi"] = rsi(closes, int(params["period"]))

    side: str | None = None
    entry = 0.0
    entry_ts = 0
    realized = 0.0
    pending: str | None = None
    fills: list[dict[str, Any]] = []

    def record(bar: dict[str, float], action: str, fill_side: str, raw_price: float, why: str, pnl: float) -> None:
        price = _slip_price(raw_price, action, fill_side, slip)
        if not _finite(price) or price <= 0:
            price = raw_price
        fills.append({
            "ts": int(bar["ts"]),
            "action": action,
            "side": fill_side,
            "price": price,
            "raw_price": raw_price,
            "reason": why,
            "fee": _fee(weight, fee),
            "paper_pnl": pnl,
        })

    def close_pos(bar: dict[str, float], raw_price: float, why: str) -> None:
        nonlocal side, realized
        if side is None:
            return
        exit_px = _slip_price(raw_price, "exit", side, slip)
        if not _finite(exit_px) or exit_px <= 0:
            exit_px = raw_price
        move = (exit_px / entry - 1.0) if side == "long" else (1.0 - exit_px / entry)
        change = weight * move - _fee(weight, fee)
        realized += change
        record(bar, "exit", side, raw_price, why, change)
        side = None

    def enter_pos(bar: dict[str, float], wanted: str) -> None:
        nonlocal side, entry, entry_ts, realized
        raw = bar["open"]
        entry = _slip_price(raw, "enter", wanted, slip)
        if not _finite(entry) or entry <= 0:
            return
        side = wanted
        entry_ts = int(bar["ts"])
        realized -= _fee(weight, fee)
        record(bar, "enter", side, raw, "signal_enter_next_open", 0.0)

    def stop_take_price(current_side: str, kind_name: str) -> float | None:
        if current_side == "long":
            return entry * (1.0 - float(stop)) if kind_name == "stop" and stop else (
                entry * (1.0 + float(take)) if kind_name == "take" and take else None
            )
        return entry * (1.0 + float(stop)) if kind_name == "stop" and stop else (
            entry * (1.0 - float(take)) if kind_name == "take" and take else None
        )

    def manage_open_gap(bar: dict[str, float]) -> None:
        """Use only the open, which is visible at the start of the bar."""
        if side is None:
            return
        stop_px = stop_take_price(side, "stop")
        take_px = stop_take_price(side, "take")
        open_px = bar["open"]
        if side == "long":
            gapped_stop = stop_px is not None and open_px <= stop_px
            gapped_take = take_px is not None and open_px >= take_px
        else:
            gapped_stop = stop_px is not None and open_px >= stop_px
            gapped_take = take_px is not None and open_px <= take_px
        if gapped_stop:
            close_pos(bar, open_px, "stop_loss_gap_open")
            return
        if gapped_take:
            close_pos(bar, open_px, "take_profit_gap_open")

    def apply_pending_open(bar: dict[str, float]) -> None:
        """Fill the previous bar's pending decision at this open only."""
        nonlocal pending
        if not pending:
            return
        wanted = pending
        pending = None
        if long_only and wanted == "short":
            wanted = "flat"
        if side is not None:
            if wanted == "flat":
                close_pos(bar, bar["open"], "signal_exit_next_open")
            elif wanted != side:
                close_pos(bar, bar["open"], "reverse_next_open")
                if wanted in {"long", "short"}:
                    enter_pos(bar, wanted)
            return
        if wanted in {"long", "short"}:
            enter_pos(bar, wanted)

    def manage_intrabar(bar: dict[str, float]) -> None:
        """High/low risk after all open-time orders for this bar are complete."""
        if side is None:
            return
        stop_px = stop_take_price(side, "stop")
        take_px = stop_take_price(side, "take")
        hit_stop = False
        hit_take = False
        if side == "long":
            hit_stop = stop_px is not None and bar["low"] <= stop_px
            hit_take = take_px is not None and bar["high"] >= take_px
        else:
            hit_stop = stop_px is not None and bar["high"] >= stop_px
            hit_take = take_px is not None and bar["low"] <= take_px
        if hit_stop and hit_take:
            close_pos(bar, stop_px, "stop_loss_priority")
        elif hit_stop:
            close_pos(bar, stop_px, "stop_loss")
        elif hit_take:
            close_pos(bar, take_px, "take_profit")

    for index, bar in enumerate(bars):
        # Causal order: open-gap on an existing position, then pending open
        # orders, then intrabar extrema, then this close for the next pending.
        manage_open_gap(bar)
        apply_pending_open(bar)
        manage_intrabar(bar)
        wanted = _signal(kind, params, bars, index, series)
        if long_only and wanted == "short":
            wanted = "flat"
        if wanted == side or (wanted == "flat" and side is None):
            pending = None
        else:
            pending = wanted

    open_position = None
    unrealized = 0.0
    if side is not None:
        last = bars[-1]
        move = (last["close"] / entry - 1.0) if side == "long" else (1.0 - last["close"] / entry)
        unrealized = weight * move
        open_position = {
            "side": side,
            "entry": entry,
            "entry_ts": entry_ts,
            "mark": last["close"],
            "unrealized": unrealized,
            "mark_is_last_close": True,
            "exit_fee_not_applied": True,
        }

    details = fills[:MAX_FILLS]
    truncated = len(fills) > MAX_FILLS
    zero_cost = fee == 0.0 and slip == 0.0
    return {
        "status": "simulated",
        "reason": "fills_capped" if truncated else None,
        "bars_used": len(bars),
        "bars_required": need,
        "processed_bars": len(bars),
        "processed_last_ts": int(bars[-1]["ts"]),
        "first_ts": int(bars[0]["ts"]),
        "last_ts": int(bars[-1]["ts"]),
        **prepared.disclosure(),
        "fills": details,
        "fill_count": len(fills),
        "fills_truncated": truncated,
        "fills_omitted": max(0, len(fills) - MAX_FILLS),
        "complete_fills": not truncated,
        "open_position": open_position,
        "paper_pnl": realized + unrealized,
        "realized_paper_pnl": realized,
        "unrealized_paper_pnl": unrealized,
        "returns_published": False,
        "is_investment_return": False,
        "gross_zero_cost": zero_cost,
        "unit": "paper_fraction",
        "cost_model": {
            "fee_bps": fee,
            "slippage_bps": slip,
            "applied": True,
            "gross_zero_cost": zero_cost,
        },
        "assumptions": {
            "execution": "signal_on_closed_bar_fill_next_open",
            "forming_tail": "exclude_trailing_unclosed_disclose_cutoff",
            "event_order": "open_gap_then_pending_open_orders_then_intrabar_then_close_signal",
            "gap_rule": "fill_at_open_when_gapped_through_stop_or_take",
            "dual_trigger": "stop_has_priority",
            "donchian_channel": "prior_high_low" if kind == "donchian_break" else None,
            "not_investment_return": True,
        },
        "used_model": False,
        "live": False,
        "site_runner": False,
        "request": request,
    }


def run_simulation(
    strategy: dict[str, Any],
    loader: Callable[[str, str, int], list[dict[str, Any]]] | None = None,
    *,
    now_ms: int | None = None,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
) -> dict[str, Any]:
    load = loader or load_stored_candles
    rows = load(strategy["symbol"], strategy["timeframe"], MAX_BARS)
    return simulate_bars(strategy, rows, now_ms=now_ms, fee_bps=fee_bps, slippage_bps=slippage_bps)
