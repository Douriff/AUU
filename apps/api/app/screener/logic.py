"""Condition language and daily indicators for the spot screener.

This is the only indicator implementation. Comparisons use the raw floats
produced here; nothing rounds a value before a threshold test.

Three-valued results are the strings ``true``, ``false`` and ``unknown``.
``unknown`` is not a pass and not a fail. Negating ``unknown`` stays
``unknown`` — it never becomes a hit.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Optional

TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"
TRI = frozenset({TRUE, FALSE, UNKNOWN})

# The screener never arms live trading, whatever the process env says.
LIVE_ENABLED = False

EMA_PERIODS = frozenset({10, 20, 200})
RSI_PERIODS = frozenset({14})
TIMEFRAMES = frozenset({"1d"})
CMPS = frozenset({"gt", "gte", "lt", "lte"})
GROUP_OPS = frozenset({"all", "any"})
FIELDS = frozenset({"price", "quoteVolume24h", "ema", "rsi"})

MAX_NODES = 16
MAX_LEAVES = 8
MAX_DEPTH = 3
MAX_ABS = 1e15

# Rejected on purpose: not implemented, so they are not selectable.
UNSUPPORTED_CMPS = frozenset({
    "cross", "crossed", "crossed_above", "crossed_below", "crosses_above", "crosses_below",
    "cross_above", "cross_below",
})

DAY_MS = 86_400_000
# Same horizon the mainstream 1d series uses (49 hours).
CLOSED_STALE_MS = (2 * 24 * 60 + 60) * 60 * 1000


class SchemaError(ValueError):
    """The query is not in the whitelist. ``code`` is a stable client key."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def passes(tri: str) -> bool:
    """A row passes only when the predicate is definitely true."""
    return tri == TRUE


def fails(tri: str) -> bool:
    """A row fails only when the predicate is definitely false.

    ``unknown`` is not a failure. Callers must not treat ``not passes`` as ``fails``.
    """
    return tri == FALSE


def negate(tri: str) -> str:
    if tri == TRUE:
        return FALSE
    if tri == FALSE:
        return TRUE
    return UNKNOWN


def combine_all(values: list[str]) -> str:
    """Conjunction. A single ``false`` wins; otherwise any ``unknown`` blocks ``true``.

    Dropping ``unknown`` and then combining the rest would turn ``true + unknown``
    into ``true``. That is incorrect and this function does not do it.
    """
    if not values:
        return UNKNOWN
    if any(v == FALSE for v in values):
        return FALSE
    if any(v == UNKNOWN for v in values):
        return UNKNOWN
    return TRUE


def combine_any(values: list[str]) -> str:
    """Disjunction. A single ``true`` wins; otherwise any ``unknown`` blocks ``false``."""
    if not values:
        return UNKNOWN
    if any(v == TRUE for v in values):
        return TRUE
    if any(v == UNKNOWN for v in values):
        return UNKNOWN
    return FALSE


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError("BAD_NUMBER")
    number = float(value)
    if not math.isfinite(number) or abs(number) > MAX_ABS:
        raise SchemaError("BAD_NUMBER")
    return number


def _metric(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise SchemaError("BAD_FIELD")
    if set(node) - {"field", "period", "tf"}:
        raise SchemaError("BAD_FIELD")
    field = node.get("field")
    if field not in FIELDS:
        raise SchemaError("BAD_FIELD")
    if field in {"price", "quoteVolume24h"}:
        if set(node) != {"field"}:
            raise SchemaError("BAD_FIELD")
        return {"field": field}
    period = node.get("period")
    tf = node.get("tf")
    if type(period) is not int or tf not in TIMEFRAMES:
        raise SchemaError("BAD_FIELD")
    if field == "ema" and period not in EMA_PERIODS:
        raise SchemaError("BAD_PERIOD")
    if field == "rsi" and period not in RSI_PERIODS:
        raise SchemaError("BAD_PERIOD")
    return {"field": field, "period": period, "tf": tf}


def metric_id(metric: dict[str, Any]) -> str:
    field = metric["field"]
    if field == "ema":
        return f"ema{metric['period']}"
    if field == "rsi":
        return f"rsi{metric['period']}"
    return field


def _right(node: Any) -> dict[str, Any]:
    if isinstance(node, dict) and set(node) == {"num"}:
        return {"num": _finite_number(node["num"])}
    return _metric(node)


def parse_query(node: Any, _depth: int = 1, _count: Optional[list[int]] = None) -> dict[str, Any]:
    """Parse a whitelist query. Unknown keys, periods, timeframes and crosses are errors."""
    count = _count if _count is not None else [0]
    count[0] += 1
    if count[0] > MAX_NODES or _depth > MAX_DEPTH:
        raise SchemaError("TOO_COMPLEX")
    if not isinstance(node, dict):
        raise SchemaError("BAD_QUERY")
    if "op" in node:
        extra = set(node) - {"op", "of"}
        if extra:
            raise SchemaError("BAD_QUERY")
        op = node.get("op")
        if op in UNSUPPORTED_CMPS or (isinstance(op, str) and "cross" in op):
            raise SchemaError("CROSS_UNSUPPORTED")
        if op == "not":
            child = node.get("of")
            if isinstance(child, list):
                raise SchemaError("BAD_QUERY")
            return {"type": "not", "of": parse_query(child, _depth + 1, count)}
        if op not in GROUP_OPS:
            raise SchemaError("BAD_OP")
        children = node.get("of")
        if not isinstance(children, list) or not children:
            raise SchemaError("BAD_QUERY")
        return {"type": "group", "op": op, "of": [parse_query(child, _depth + 1, count) for child in children]}
    if "cmp" in node:
        if set(node) - {"cmp", "left", "right"}:
            raise SchemaError("BAD_QUERY")
        cmp = node.get("cmp")
        if cmp in UNSUPPORTED_CMPS or (isinstance(cmp, str) and "cross" in cmp):
            raise SchemaError("CROSS_UNSUPPORTED")
        if cmp not in CMPS:
            raise SchemaError("BAD_OP")
        leaf = {"type": "leaf", "cmp": cmp, "left": _metric(node.get("left")), "right": _right(node.get("right"))}
        return leaf
    raise SchemaError("BAD_QUERY")


def _check_leaves(node: dict[str, Any], leaves: list[int]) -> None:
    if node["type"] == "leaf":
        leaves[0] += 1
        if leaves[0] > MAX_LEAVES:
            raise SchemaError("TOO_COMPLEX")
        return
    if node["type"] == "not":
        _check_leaves(node["of"], leaves)
        return
    for child in node["of"]:
        _check_leaves(child, leaves)


def accept_query(payload: Any) -> dict[str, Any]:
    parsed = parse_query(payload)
    _check_leaves(parsed, [0])
    return parsed


def collect_metrics(node: dict[str, Any]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def walk(current: dict[str, Any]) -> None:
        if current["type"] == "leaf":
            for side in (current["left"], current["right"]):
                if "field" in side:
                    found.setdefault(metric_id(side), side)
            return
        if current["type"] == "not":
            walk(current["of"])
            return
        for child in current["of"]:
            walk(child)

    walk(node)
    return list(found.values())


def min_closed_bars(metric: dict[str, Any]) -> int:
    """Closed daily bars required before the metric is defined. Fewer bars → unknown, not zero."""
    if metric["field"] == "ema":
        return int(metric["period"])
    if metric["field"] == "rsi":
        return int(metric["period"]) + 1
    return 0


def history_limit(metrics: list[dict[str, Any]]) -> int:
    """How many daily bars to request. Not a universe cap: a short listing still returns."""
    need = 0
    for metric in metrics:
        need = max(need, min_closed_bars(metric))
    if need <= 0:
        return 0
    return min(1000, max(need, need * 3))


def _usable_close(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def series_problem(closes: list[Any]) -> Optional[str]:
    """``nan`` if any close is unusable. Does not substitute zero."""
    if any(_usable_close(value) is None for value in closes):
        return "nan"
    return None


def ema(closes: list[Any], period: int) -> Optional[float]:
    """EMA seeded with the SMA of the first ``period`` closes, then the standard multiplier.

    Returns None when the series is shorter than ``period`` or any close is not a finite
    price above zero. Missing data is never replaced with 0.
    """
    if period not in EMA_PERIODS:
        raise SchemaError("BAD_PERIOD")
    if len(closes) < period or series_problem(closes):
        return None
    values = [float(value) for value in closes]
    seed = sum(values[:period]) / period
    k = 2.0 / (period + 1.0)
    current = seed
    for price in values[period:]:
        current = (price - current) * k + current
    return current


def rsi(closes: list[Any], period: int = 14) -> Optional[float]:
    """Wilder RSI.

    Needs ``period + 1`` closes. A flat series (every change is zero, so both smoothed
    gain and loss are zero) is defined as 50. A series that only rises is 100. A series
    that only falls is 0. Short, empty or non-finite input is None — never 0.
    """
    if period not in RSI_PERIODS:
        raise SchemaError("BAD_PERIOD")
    if len(closes) < period + 1 or series_problem(closes):
        return None
    values = [float(value) for value in closes]
    gains: list[float] = []
    losses: list[float] = []
    for prev, price in zip(values, values[1:]):
        delta = price - prev
        gains.append(delta if delta > 0 else 0.0)
        losses.append(-delta if delta < 0 else 0.0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for index in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[index]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index]) / period
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compare(op: str, left: Optional[float], right: Optional[float]) -> str:
    """Compare raw values. None or a non-finite number is unknown, not false and not zero."""
    if op not in CMPS:
        raise SchemaError("BAD_OP")
    if left is None or right is None:
        return UNKNOWN
    if not math.isfinite(left) or not math.isfinite(right):
        return UNKNOWN
    if op == "gt":
        return TRUE if left > right else FALSE
    if op == "gte":
        return TRUE if left >= right else FALSE
    if op == "lt":
        return TRUE if left < right else FALSE
    return TRUE if left <= right else FALSE


def eval_query(node: dict[str, Any], value_of: Callable[[dict[str, Any]], Optional[float]]) -> str:
    """Evaluate a parsed query. ``value_of`` returns None when that metric is not usable."""
    if node["type"] == "not":
        return negate(eval_query(node["of"], value_of))
    if node["type"] == "group":
        values = [eval_query(child, value_of) for child in node["of"]]
        return combine_all(values) if node["op"] == "all" else combine_any(values)
    right = node["right"]
    right_value = right["num"] if "num" in right else value_of(right)
    return compare(node["cmp"], value_of(node["left"]), right_value)


def needs_klines(
    node: dict[str, Any],
    state_of: Callable[[dict[str, Any]], str],
    value_of: Callable[[dict[str, Any]], Optional[float]],
) -> bool:
    """Whether a still-unfetched daily series could change the result.

    A branch that is already definitely true (any) or definitely false (all) does not
    pull klines. ``pending`` means the series has not been fetched yet. Decided
    branches are evaluated with ``value_of`` (None unless that reading is usable).
    """
    if node["type"] == "not":
        return needs_klines(node["of"], state_of, value_of)
    if node["type"] == "group":
        known: list[str] = []
        waiting = False
        for child in node["of"]:
            if needs_klines(child, state_of, value_of):
                waiting = True
            else:
                known.append(eval_query(child, value_of))
        if node["op"] == "all":
            if any(item == FALSE for item in known):
                return False
            return waiting
        if any(item == TRUE for item in known):
            return False
        return waiting
    for side in (node["left"], node["right"]):
        if "field" in side and side["field"] in {"ema", "rsi"} and state_of(side) == "pending":
            return True
    return False


def preview_value(readings: dict[str, "Reading"], metric: dict[str, Any]) -> Optional[float]:
    reading = readings.get(metric_id(metric))
    if reading is None or reading.state != "ok":
        return None
    return reading.value


class Reading:
    """One metric observation. ``value`` is set only when ``state == 'ok'``."""

    __slots__ = ("state", "value", "display")

    def __init__(self, state: str, value: Optional[float] = None, display: Optional[float] = None) -> None:
        self.state = state
        self.value = value if state == "ok" else None
        self.display = value if state == "ok" else display

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"state": self.state, "value": self.value, "usable": self.state == "ok"}
        if self.state != "ok" and self.display is not None and math.isfinite(self.display):
            payload["display"] = self.display
        return payload


def indicator_reading(metric: dict[str, Any], closes: list[Any], *, fresh: bool) -> Reading:
    """Compute one indicator. A stale series keeps the number for display but is not usable."""
    need = min_closed_bars(metric)
    if len(closes) < need:
        if series_problem(closes) == "nan" and closes:
            return Reading("nan")
        return Reading("short")
    if series_problem(closes):
        return Reading("nan")
    number = ema(closes, metric["period"]) if metric["field"] == "ema" else rsi(closes, metric["period"])
    if number is None:
        return Reading("nan")
    if not fresh:
        return Reading("stale", display=number)
    return Reading("ok", value=number)


def split_bars(bars: list[dict[str, Any]], now_ms: int) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
    """Split exchange bars into closed daily bars and the single forming bar, if any.

    A bar is closed only when its close time is strictly before ``now_ms``. The forming
    bar is never fed to an indicator (no future, and no in-progress day).
    """
    ordered = sorted(bars, key=lambda bar: int(bar["openTime"]))
    closed = [bar for bar in ordered if int(bar["closeTime"]) < now_ms]
    forming = next((bar for bar in reversed(ordered) if int(bar["closeTime"]) >= now_ms), None)
    return closed, forming


def bars_are_fresh(closed: list[dict[str, Any]], fetched_at: int, now_ms: int, *, fresh_ms: int) -> bool:
    if not closed or fetched_at <= 0 or now_ms - fetched_at > fresh_ms:
        return False
    last_close = int(closed[-1]["closeTime"])
    return now_ms - last_close <= CLOSED_STALE_MS


_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: Any) -> str:
    """Quote every cell and neutralize spreadsheet formulas.

    A leading ``=``, ``+``, ``-``, ``@``, tab or carriage return (also after leading
    whitespace) is prefixed with an apostrophe before quoting. Internal quotes are doubled.
    """
    text = "" if value is None else str(value)
    probe = text.lstrip(" \t\r\n")
    if text[:1] in _FORMULA or (probe[:1] in "=+-@"):
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def export_csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    lines = [",".join(csv_cell(column) for column in columns)]
    for row in rows:
        lines.append(",".join(csv_cell(row.get(column, "")) for column in columns))
    return "\ufeff" + "\r\n".join(lines) + "\r\n"


DEFAULT_QUERY: dict[str, Any] = {
    "op": "all",
    "of": [
        {"cmp": "gt", "left": {"field": "quoteVolume24h"}, "right": {"num": 1_000_000}},
        {
            "cmp": "gt",
            "left": {"field": "price"},
            "right": {"field": "ema", "period": 20, "tf": "1d"},
        },
    ],
}


def verdict_of(tri: str) -> str:
    if tri == TRUE:
        return "pass"
    if tri == FALSE:
        return "fail"
    return "unknown"


def chart_path(base: str) -> str:
    """Existing mainstream chart URL. The screener does not change that page."""
    return f"/?symbol={base}&venue=binance"
