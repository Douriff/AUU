"""Daily price/funding panel shared by the backtest engine and the paper runner.

Pure Python (stdlib only) so the API process and a small server need no numpy/pandas.
Missing values are ``None``. Rows are UTC days (``day_ms`` = 00:00 UTC in ms).
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

DAY_MS = 86_400_000

# Research universe (plan §2.1): 19 large caps alive today.
UNIVERSE_19 = "BTC ETH SOL XRP DOGE BNB ADA AVAX LINK LTC TRX DOT BCH ETC XLM ATOM FIL UNI NEAR".split()

Series = list  # list[Optional[float]]


def day_ms(d: str | date) -> int:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def ms_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Panel:
    """Aligned daily series per coin.

    spot_close / spot_high / spot_low drive signals; perp_close drives PnL; funding is the
    sum of that day's perpetual funding rates (longs pay a positive rate).
    """

    days: list[int]
    coins: list[str]
    spot_close: dict[str, Series]
    perp_close: dict[str, Series]
    funding: dict[str, list[float]]
    spot_high: dict[str, Series] = field(default_factory=dict)
    spot_low: dict[str, Series] = field(default_factory=dict)
    source: str = ""

    def __len__(self) -> int:
        return len(self.days)

    def index_of(self, d: str | int) -> int:
        ms = day_ms(d) if isinstance(d, str) else int(d)
        return self.days.index(ms)

    def truncate(self, n: int) -> "Panel":
        """First ``n`` rows only (what the strategy could have seen at the close of row n-1)."""
        cut = lambda m: {c: v[:n] for c, v in m.items()}  # noqa: E731
        return Panel(
            self.days[:n], list(self.coins), cut(self.spot_close), cut(self.perp_close), cut(self.funding),
            cut(self.spot_high), cut(self.spot_low), self.source,
        )

    def subset(self, coins: Sequence[str]) -> "Panel":
        pick = lambda m: {c: m[c] for c in coins if c in m}  # noqa: E731
        return Panel(
            list(self.days), [c for c in coins if c in self.coins], pick(self.spot_close), pick(self.perp_close),
            pick(self.funding), pick(self.spot_high), pick(self.spot_low), self.source,
        )


def _daily_index(start_ms: int, end_ms: int) -> list[int]:
    return list(range(start_ms, end_ms + 1, DAY_MS))


def build_panel(
    days: list[int],
    raw: dict[str, dict[str, dict[int, float]]],
    funding: dict[str, dict[int, float]],
    *,
    source: str,
) -> Panel:
    """``raw[coin]`` holds day->value maps for spot_c/spot_h/spot_l/perp_c.

    Signal prices fall back to perp prints where spot is missing (plan §2.1: the 2026-09
    spot archive was not published yet; basis is a few bp).
    """
    coins = [c for c in raw if raw[c].get("perp_c") and (raw[c].get("spot_c") or raw[c].get("perp_c"))]
    sc, sh, sl, pc, fu = {}, {}, {}, {}, {}
    for c in coins:
        r = raw[c]
        p_c, s_c, s_h, s_l = r.get("perp_c", {}), r.get("spot_c", {}), r.get("spot_h", {}), r.get("spot_l", {})
        p_h, p_l = r.get("perp_h", {}), r.get("perp_l", {})
        pc[c] = [p_c.get(d) for d in days]
        sc[c] = [s_c.get(d, p_c.get(d)) for d in days]
        sh[c] = [s_h.get(d, p_h.get(d)) for d in days]
        sl[c] = [s_l.get(d, p_l.get(d)) for d in days]
        f = funding.get(c, {})
        fu[c] = [float(f.get(d, 0.0)) for d in days]
    return Panel(days, coins, sc, pc, fu, sh, sl, source)


# ---- Binance public archive (data.binance.vision monthly zips) -------------------------
ARCHIVE_URLS = {
    "spot": "https://data.binance.vision/data/spot/monthly/klines/{c}USDT/1d/{c}USDT-1d-{ym}.zip",
    "perp": "https://data.binance.vision/data/futures/um/monthly/klines/{c}USDT/1d/{c}USDT-1d-{ym}.zip",
    "fund": "https://data.binance.vision/data/futures/um/monthly/fundingRate/{c}USDT/{c}USDT-fundingRate-{ym}.zip",
}


def _zip_rows(path: Path) -> list[list[str]]:
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(raw)))
    rows = [r for r in rows if r]
    if rows and not (rows[0][0][:1].isdigit() or rows[0][0][:1] == "-"):
        rows = rows[1:]  # header (newer files)
    return rows


def _ts_ms(x: str) -> int:
    v = int(float(x))
    return v // 1000 if v > 10**14 else v  # 2025+ spot files use microseconds


def _klines(files: Iterable[Path]) -> dict[str, dict[int, float]]:
    out: dict[str, dict[int, float]] = {"c": {}, "h": {}, "l": {}}
    for f in files:
        try:
            rows = _zip_rows(f)
        except Exception:
            continue
        for r in rows:
            d = _ts_ms(r[0]) // DAY_MS * DAY_MS
            if d in out["c"]:
                continue  # keep first (same as drop_duplicates)
            out["h"][d], out["l"][d], out["c"][d] = float(r[2]), float(r[3]), float(r[4])
    return out


def _funding(files: Iterable[Path]) -> dict[int, float]:
    seen: set[int] = set()
    daily: dict[int, float] = {}
    for f in files:
        try:
            rows = _zip_rows(f)
        except Exception:
            continue
        for r in rows:
            t = _ts_ms(r[0])
            if t in seen:
                continue
            seen.add(t)
            d = t // DAY_MS * DAY_MS
            daily[d] = daily.get(d, 0.0) + float(r[-1])
    return daily


def load_binance_archive(
    directory: str | Path,
    coins: Sequence[str] = UNIVERSE_19,
    *,
    start: str = "2020-01-01",
    end: str = "2026-09-30",
) -> Panel:
    """Files named ``{spot,perp,fund}_{COIN}_{YYYY-MM}.zip`` (see ``ARCHIVE_URLS``)."""
    root = Path(directory)
    days = _daily_index(day_ms(start), day_ms(end))
    raw: dict[str, dict[str, dict[int, float]]] = {}
    fund: dict[str, dict[int, float]] = {}
    for c in coins:
        s = _klines(sorted(root.glob(f"spot_{c}_*.zip")))
        p = _klines(sorted(root.glob(f"perp_{c}_*.zip")))
        if not s["c"] or not p["c"]:
            continue
        raw[c] = {"spot_c": s["c"], "spot_h": s["h"], "spot_l": s["l"], "perp_c": p["c"], "perp_h": p["h"], "perp_l": p["l"]}
        fund[c] = _funding(sorted(root.glob(f"fund_{c}_*.zip")))
    return build_panel(days, raw, fund, source=f"binance-archive:{root.name}")


# ---- AUU market store (live CEX data, M1) ----------------------------------------------
def load_store(
    store,
    exchange: str,
    coins: Sequence[str],
    *,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Panel:
    """Daily panel from :class:`app.marketdata.mainstream.store.MarketStore`.

    The store keeps spot candles and perpetual funding; perp closes are proxied by spot
    closes (basis is a few bp on BTC/ETH/SOL). The newest (still forming) day is dropped.
    """
    raw: dict[str, dict[str, dict[int, float]]] = {}
    fund: dict[str, dict[int, float]] = {}
    lo_all, hi_all = None, None
    for c in coins:
        rows = store.candles(exchange, c, "1d", limit=100_000)
        if not rows:
            continue
        m = {"spot_c": {}, "spot_h": {}, "spot_l": {}}
        for r in rows:
            d = int(r["ts"]) // DAY_MS * DAY_MS
            m["spot_c"][d], m["spot_h"][d], m["spot_l"][d] = r["close"], r["high"], r["low"]
        m["perp_c"] = dict(m["spot_c"])
        raw[c] = m
        f: dict[int, float] = {}
        for x in store.funding(exchange, c, limit=1_000_000):
            d = int(x["ts"]) // DAY_MS * DAY_MS
            f[d] = f.get(d, 0.0) + float(x["rate"])
        fund[c] = f
        lo, hi = min(m["spot_c"]), max(m["spot_c"])
        lo_all = lo if lo_all is None else min(lo_all, lo)
        hi_all = hi if hi_all is None else max(hi_all, hi)
    if lo_all is None:
        return Panel([], [], {}, {}, {}, {}, {}, f"store:{exchange}")
    s = day_ms(start) if start else lo_all
    e = day_ms(end) if end else hi_all - DAY_MS
    return build_panel(_daily_index(s, e), raw, fund, source=f"store:{exchange}")
