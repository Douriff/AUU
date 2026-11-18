"""Shared limits and JSON/CSV safety for the strategy-review lab."""
from __future__ import annotations

import csv
import io
import math
import re
from typing import Any, Optional

STRATEGY_ID = re.compile(r"^[A-Za-z0-9_]{1,32}$")
SYMBOL = re.compile(r"^[A-Z0-9]{1,12}$")
_FORMULA = ("=", "+", "-", "@", "\t", "\r")

MAX_LIMIT = 200
MAX_COMPARE = 6
MAX_CURVE = 400
MAX_EVENTS = 80
NAV_READ_CAP = 4000
TRADE_READ_CAP = 2000
BAR_READ_CAP = 2000

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
STALE_GRACE_MS = 36 * HOUR_MS
SHORT_NAV = 30
SHORT_TRADES = 30
CHAIN_ABS = 1e-6


def finite(value: Any) -> Optional[float]:
    """Return a finite float, or None when the value is missing or not finite."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def bad_number(value: Any) -> bool:
    """True when a present value is NaN, infinite, or not numeric. NULL is not bad."""
    if value is None:
        return False
    return finite(value) is None


def required_bad(value: Any) -> bool:
    """A required metric cell is bad when it is missing or not finite."""
    return value is None or bad_number(value)


def scrub(value: Any) -> Any:
    """Drop non-finite floats so responses stay strict JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    return value


def csv_cell(value: Any) -> str:
    """Neutralise spreadsheet formula injection. Missing stays empty, never 0."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_FORMULA):
        text = "'" + text
    if any(ch in text for ch in '",\n\r'):
        text = '"' + text.replace('"', '""') + '"'
    return text


def csv_document(header: list[str], rows: list[list[Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([csv_cell(cell) for cell in header])
    for row in rows:
        writer.writerow([csv_cell(cell) for cell in row])
    return buffer.getvalue()
