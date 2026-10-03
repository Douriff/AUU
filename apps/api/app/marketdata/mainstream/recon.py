"""Cross-source daily-close reconciliation (P1-3): the strategy's data source vs a second public venue.

Once a day (after ``AUU_RECON_AFTER_MIN`` minutes past 00:00 UTC, i.e. after the daily close), the
last ``AUU_RECON_DAYS`` closed daily bars of every strategy coin in the local store (the venue the
strategy reads, normally Binance) are compared with the same UTC days fetched from the other venue's
public REST klines (OKX when the store is Binance, and vice versa). Read-only: one keyless GET per
coin, paced, no ccxt client (small memory). A close that differs by more than the coin's threshold,
or a day missing on one side, is flagged, stored in ``recon.sqlite`` and sent through the existing
alert email. The strategy data source is **never** switched automatically.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.data_paths import data_dir

log = logging.getLogger("auu.recon")

DAY_MS = 86_400_000
DEFAULT_THRESHOLD_PCT = 0.5
VENUES = ("binance", "okx")
_UA = "auutrade-recon/1 (read-only public klines)"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
  day INTEGER NOT NULL, coin TEXT NOT NULL, primary_ex TEXT NOT NULL, secondary_ex TEXT NOT NULL,
  c1 REAL, c2 REAL, dev REAL, threshold REAL NOT NULL, status TEXT NOT NULL,   -- ok | deviation | missing_primary | missing_secondary
  checked_at INTEGER NOT NULL, PRIMARY KEY (day, coin)
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at INTEGER NOT NULL, primary_ex TEXT, secondary_ex TEXT,
  status TEXT NOT NULL, checked INTEGER NOT NULL DEFAULT 0, flagged INTEGER NOT NULL DEFAULT 0,
  max_dev REAL, max_coin TEXT, error TEXT, alert TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def recon_enabled() -> bool:
    return os.getenv("AUU_RECON", "on").strip().lower() not in {"0", "false", "off", "no"}


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.getenv(name, "") or default)
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def thresholds() -> tuple[float, dict[str, float]]:
    """Default threshold (percent) and per-coin overrides: AUU_RECON_THRESHOLD_PCT=0.5,
    AUU_RECON_THRESHOLDS="DOGE:1.0,FIL:1.0"."""
    try:
        default = float(os.getenv("AUU_RECON_THRESHOLD_PCT", "") or DEFAULT_THRESHOLD_PCT)
    except ValueError:
        default = DEFAULT_THRESHOLD_PCT
    per: dict[str, float] = {}
    for part in (os.getenv("AUU_RECON_THRESHOLDS") or "").split(","):
        k, _, v = part.partition(":")
        try:
            if k.strip() and v.strip():
                per[k.strip().upper()] = float(v)
        except ValueError:
            continue
    return default, per


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


# ---- public REST klines (keyless GET) ------------------------------------------------------
def _get_json(url: str, timeout: float = 10.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https hosts)
        return json.loads(resp.read().decode())


def fetch_daily_closes(venue: str, coin: str, quote: str, limit: int, *, get: Callable[[str], Any] = _get_json) -> dict[int, float]:
    """{UTC day open ms: close} of the last ``limit`` daily bars (the forming bar included; callers drop it)."""
    if venue == "binance":
        rows = get(f"https://api.binance.com/api/v3/klines?symbol={coin}{quote}&interval=1d&limit={limit}")
        return {int(r[0]): float(r[4]) for r in rows or []}
    if venue == "okx":
        doc = get(f"https://www.okx.com/api/v5/market/history-candles?instId={coin}-{quote}&bar=1Dutc&limit={limit}")
        if str(doc.get("code", "0")) != "0":
            raise RuntimeError(f"okx code {doc.get('code')}: {str(doc.get('msg'))[:80]}")
        return {int(r[0]): float(r[4]) for r in doc.get("data") or []}
    raise ValueError(f"unsupported venue {venue}")


class Reconciler:
    def __init__(self, path: Optional[Path | str] = None, *, svc_fn: Optional[Callable[[], Any]] = None,
                 fetch: Callable[..., dict[int, float]] = fetch_daily_closes,
                 alert_fn: Optional[Callable[[str, str, str, str], str]] = None,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000), sleep: Callable[[float], None] = time.sleep):
        self.path = Path(path) if path else data_dir() / "recon.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.RLock()
        self.svc_fn = svc_fn or _default_svc
        self.fetch = fetch
        self.alert_fn = alert_fn if alert_fn is not None else _default_alert
        self.now_ms, self.sleep = now_ms, sleep
        self.days = _int("AUU_RECON_DAYS", 7, 2, 60)
        self.after_min = _int("AUU_RECON_AFTER_MIN", 20, 0, 23 * 60)
        self.retry_min = _int("AUU_RECON_RETRY_MIN", 30, 5, 24 * 60)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def _set(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # ---- scheduling ----------------------------------------------------------------------
    def due(self) -> bool:
        now = self.now_ms()
        today = now // DAY_MS * DAY_MS
        if now - today < self.after_min * 60_000:
            return False
        if int(self._get("done_day", 0) or 0) >= today:
            return False
        return now - int(self._get("try_at", 0) or 0) >= self.retry_min * 60_000

    def tick(self) -> Optional[dict]:
        return self.run_once() if self.due() else None

    # ---- one pass ------------------------------------------------------------------------
    def run_once(self) -> dict:
        now = self.now_ms()
        self._set("try_at", now)
        svc = self.svc_fn()
        cfg = svc.cfg
        primary = svc.exchange_for_read()
        coins = list(cfg.strategy_symbols or cfg.all_symbols())
        if primary not in VENUES:
            return self._finish(now, primary, None, "error", [], error="no primary exchange data")
        secondary = next(v for v in VENUES if v != primary)
        today = now // DAY_MS * DAY_MS
        days = [today - i * DAY_MS for i in range(self.days, 0, -1)]  # closed days only
        default, per = thresholds()
        out: list[dict] = []
        errors: list[str] = []
        for i, coin in enumerate(coins):
            thr = per.get(coin, default)
            local = {int(r["ts"]): float(r["close"]) for r in svc.store.candles(primary, coin, "1d", since=days[0], until=days[-1], limit=self.days + 2)}
            try:
                if i:
                    self.sleep(0.25)  # pace: ~19 GETs spread over ~5 s, once a day
                remote = self.fetch(secondary, coin, cfg.quote, self.days + 2)
            except Exception as exc:
                errors.append(f"{coin}: {type(exc).__name__}")
                remote = None
            for d in days:
                c1 = local.get(d)
                c2 = None if remote is None else remote.get(d)
                if remote is None:
                    continue  # venue error: not a data finding, the run is retried
                if c1 is None:
                    st, dev = "missing_primary", None
                elif c2 is None:
                    st, dev = "missing_secondary", None
                else:
                    dev = abs(c2 / c1 - 1) * 100 if c1 else None
                    st = "deviation" if dev is None or dev > thr else "ok"
                out.append({"day": d, "coin": coin, "c1": c1, "c2": c2, "dev": dev, "threshold": thr, "status": st})
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO checks(day, coin, primary_ex, secondary_ex, c1, c2, dev, threshold, status, checked_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(r["day"], r["coin"], primary, secondary, r["c1"], r["c2"], r["dev"], r["threshold"], r["status"], now) for r in out])
            self._db.execute("DELETE FROM checks WHERE day<?", (today - 400 * DAY_MS,))
            self._db.execute("DELETE FROM runs WHERE at<?", (now - 400 * DAY_MS,))
        if len(errors) == len(coins):
            return self._finish(now, primary, secondary, "error", out, error="; ".join(errors)[:300])
        status = "partial" if errors else "ok"
        return self._finish(now, primary, secondary, status, out, error="; ".join(errors)[:300] or None)

    def _finish(self, now: int, primary, secondary, status: str, rows: list[dict], *, error: Optional[str] = None) -> dict:
        flagged = [r for r in rows if r["status"] != "ok"]
        devs = [r for r in rows if r["dev"] is not None]
        worst = max(devs, key=lambda r: r["dev"]) if devs else None
        alert = None
        seen = {k: v for k, v in (self._get("alerted", {}) or {}).items() if v >= now - 60 * DAY_MS}
        fresh = [r for r in flagged if _fkey(r) not in seen]
        if fresh:  # each (coin, day, status) finding is mailed once; the dedup/rate limits of the alert center still apply
            alert = self._alert(primary, secondary, fresh)
            if alert in ("sent", "unconfigured", "duplicate", "failed", "alerts_off"):
                seen.update({_fkey(r): now for r in fresh})
        self._set("alerted", seen)
        today = now // DAY_MS * DAY_MS
        tries = self._get("tries", {}) or {}
        n = int(tries.get(str(today), 0)) + 1
        self._set("tries", {str(today): n})
        if status == "ok" or n >= 3:  # a venue error is retried (every retry_min) at most 3 times a day
            self._set("done_day", today)
        with self._lock:
            self._db.execute(
                "INSERT INTO runs(at, primary_ex, secondary_ex, status, checked, flagged, max_dev, max_coin, error, alert) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, primary, secondary, status, len(rows), len(flagged), worst["dev"] if worst else None,
                 worst["coin"] if worst else None, error, alert))
        log.warning("recon %s %s vs %s: %d checked, %d flagged, max %s %s", status, primary, secondary, len(rows), len(flagged),
                    worst["coin"] if worst else "-", f"{worst['dev']:.3f}%" if worst else "-")
        return {"status": status, "primary": primary, "secondary": secondary, "checked": len(rows), "flagged": len(flagged),
                "maxDevPct": worst["dev"] if worst else None, "maxCoin": worst["coin"] if worst else None, "error": error, "alert": alert}

    def _alert(self, primary, secondary, flagged: list[dict]) -> Optional[str]:
        key = "recon:" + ",".join(sorted(f"{r['coin']}@{utc_day(r['day'])}:{r['status']}" for r in flagged))[:240]
        label = {"deviation": "收盘价偏差超阈值", "missing_primary": "策略数据源缺这一天", "missing_secondary": "对照源缺这一天"}
        lines = [f"- {utc_day(r['day'])} {r['coin']:<5} {label.get(r['status'], r['status'])}：{primary} {_px(r['c1'])} / {secondary} {_px(r['c2'])}"
                 f"{_dev(r['dev'])}（阈值 {r['threshold']:.2f}%）" for r in flagged[:40]]
        coins = sorted({r["coin"] for r in flagged})
        body = (f"跨源日线收盘价对账发现 {len(flagged)} 处异常（{primary} 对照 {secondary}，UTC 日线）：\n" + "\n".join(lines) +
                f"\n\n策略数据源仍是 {primary}，没有自动切换；请人工核对（插针 / 缺数据 / 交易所维护）。\n纸面账本，实盘锁定。")
        try:
            return self.alert_fn(key, "recon", f"AUUTRADE 告警：跨源对账异常 {len(flagged)} 处（{'、'.join(coins)[:60]}）", body)
        except Exception:
            log.exception("recon alert failed")
            return "error"

    # ---- read models ---------------------------------------------------------------------
    def last_run(self) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def health(self) -> dict:
        """Coarse public summary (no prices)."""
        r = self.last_run()
        return {"enabled": recon_enabled(), "lastRunAt": r["at"] if r else None, "status": r["status"] if r else None,
                "primary": r["primary_ex"] if r else None, "secondary": r["secondary_ex"] if r else None,
                "checked": r["checked"] if r else 0, "flagged": r["flagged"] if r else 0,
                "maxDevPct": r["max_dev"] if r else None, "maxCoin": r["max_coin"] if r else None,
                "error": (r["error"] or None) if r else None, "autoSwitch": False}

    def summary(self) -> dict:
        default, per = thresholds()
        with self._lock:
            latest = self._db.execute("SELECT MAX(day) FROM checks").fetchone()[0]
            rows = [dict(x) for x in self._db.execute("SELECT * FROM checks WHERE day>=? ORDER BY day DESC, coin",
                                                         ((latest or 0) - (self.days - 1) * DAY_MS,))] if latest else []
            runs = [dict(x) for x in self._db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 10")]
        by_coin: dict[str, dict] = {}
        for r in rows:
            c = by_coin.setdefault(r["coin"], {"coin": r["coin"], "threshold": r["threshold"], "latestDay": None, "latestDev": None,
                                               "maxDev": None, "flagged": 0, "status": "ok", "c1": None, "c2": None})
            if c["latestDay"] is None:
                c.update(latestDay=utc_day(r["day"]), latestDev=r["dev"], c1=r["c1"], c2=r["c2"])
            if r["dev"] is not None and (c["maxDev"] is None or r["dev"] > c["maxDev"]):
                c["maxDev"] = r["dev"]
            if r["status"] != "ok":
                c["flagged"] += 1
                c["status"] = r["status"]
        flags = [{"day": utc_day(r["day"]), "coin": r["coin"], "status": r["status"], "c1": r["c1"], "c2": r["c2"], "dev": r["dev"],
                  "threshold": r["threshold"]} for r in rows if r["status"] != "ok"]
        return {**self.health(), "days": self.days, "thresholdPct": default, "thresholdOverrides": per,
                "coins": list(by_coin.values()), "flags": flags[:60],
                "runs": [{"at": x["at"], "status": x["status"], "checked": x["checked"], "flagged": x["flagged"],
                          "maxDevPct": x["max_dev"], "maxCoin": x["max_coin"], "error": x["error"]} for x in runs],
                "note": "只读对账：偏差只告警、不自动切换策略数据源。"}


def _fkey(r: dict) -> str:
    return f"{r['coin']}@{r['day']}:{r['status']}"


def _dev(v) -> str:
    return "" if v is None else f" · 偏差 {v:.3f}%"


def _px(v) -> str:
    return "—" if v is None else f"{v:.6g}"


def _default_svc():
    from app.marketdata.mainstream import get_service

    return get_service()


def _default_alert(key: str, kind: str, subject: str, body: str) -> str:
    from app.alerts import alerts_enabled, get_center

    if not alerts_enabled():
        return "alerts_off"
    return get_center().raise_alert(key, kind, subject, body)


_rec: Optional[Reconciler] = None
_rlock = threading.Lock()


def get_reconciler() -> Reconciler:
    global _rec
    with _rlock:
        if _rec is None:
            _rec = Reconciler()
        return _rec


def peek_health() -> dict:
    """Health without creating the db on a fresh box."""
    if _rec is None and not (data_dir() / "recon.sqlite").exists():
        return {"enabled": recon_enabled(), "lastRunAt": None, "status": None, "flagged": 0, "autoSwitch": False}
    return get_reconciler().health()


def reset_reconciler(r: Optional[Reconciler] = None) -> None:
    global _rec
    with _rlock:
        _rec = r


async def run_loop(interval_sec: int = 600) -> None:
    import asyncio

    await asyncio.sleep(120)  # after the first market refresh
    rec = get_reconciler()
    while True:
        try:
            await asyncio.to_thread(rec.tick)
        except Exception:
            log.exception("recon loop")
        await asyncio.sleep(interval_sec)


def main(argv: Optional[list[str]] = None) -> int:
    """python -m app.marketdata.mainstream.recon run | status"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "run":
        print(json.dumps(get_reconciler().run_once(), ensure_ascii=False))
        return 0
    if cmd == "status":
        print(json.dumps(get_reconciler().summary(), ensure_ascii=False))
        return 0
    print("usage: python -m app.marketdata.mainstream.recon run | status")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
