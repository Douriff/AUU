"""Email alerts + daily digest over the existing AUUTRADE SMTP channel (auth/email_codes).

Immediate alerts: strategy stall, runner errors, every new ``risk_events`` row, market data
stale / no exchange / risk data breaker. Each alert has a dedup key (same key is not resent within
the cooldown) and a global rate limit (per hour / per day); suppressed alerts are counted in the
digest. Failed sends are retried with backoff. The daily digest goes out at 08:30 Beijing time.

Paper data only: no credentials, no full addresses in logs (masked). Recipient from
``AUU_ALERT_TO`` (default olesaruga00@gmail.com).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.data_paths import data_dir

log = logging.getLogger("auu.alerts")

BJ = timezone(timedelta(hours=8))
DEFAULT_TO = "olesaruga00@gmail.com"
MAX_ATTEMPTS = 5
STALE_CONSECUTIVE = 2  # a data problem must be seen on 2 consecutive checks before it alerts
BAND_REPEAT_MS = 7 * 86_400_000  # while paper stays outside the expected band, repeat the alert weekly

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
  created_at INTEGER NOT NULL, status TEXT NOT NULL,      -- pending | sent | failed | suppressed | unconfigured
  attempts INTEGER NOT NULL DEFAULT 0, sent_at INTEGER, next_try INTEGER, error TEXT, to_masked TEXT
);
CREATE INDEX IF NOT EXISTS alerts_key ON alerts(key, created_at);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def alerts_enabled() -> bool:
    return os.getenv("AUU_ALERTS", "on").strip().lower() not in {"0", "false", "off", "no"}


def recipient() -> str:
    return (os.getenv("AUU_ALERT_TO") or DEFAULT_TO).strip()


def digest_time() -> tuple[int, int]:
    raw = (os.getenv("AUU_DIGEST_TIME_BJ") or "08:30").strip()
    try:
        h, m = (int(x) for x in raw.split(":"))
        return h, m
    except ValueError:
        return 8, 30


def bj(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, BJ).strftime("%Y-%m-%d %H:%M")


def _default_send(to: str, subject: str, body: str) -> None:
    from app.auth import email_codes

    email_codes._smtp_send(to, subject, body)


def _configured() -> bool:
    from app.auth.email_codes import smtp_configured

    return smtp_configured()


def record_hash(run: Optional[dict], fills: list[dict]) -> Optional[str]:
    """SHA-256 of one ledger day: the ``runs`` row and that day's fills as canonical JSON."""
    if run is None:
        return None
    doc = {"run": dict(run), "fills": sorted((dict(f) for f in fills), key=lambda f: f["coin"])}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class AlertCenter:
    def __init__(self, path: Optional[Path | str] = None, *, send: Optional[Callable[[str, str, str], None]] = None,
                 configured: Optional[Callable[[], bool]] = None, now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
                 to: Optional[str] = None):
        self.path = Path(path) if path else data_dir() / "alerts.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.RLock()
        self.send = send or _default_send
        self.configured = configured or _configured
        self.now_ms = now_ms
        self._to = to
        self.cooldown_ms = _int("AUU_ALERT_COOLDOWN_MIN", 360) * 60_000
        self.hourly_max = _int("AUU_ALERT_HOURLY_MAX", 6)
        self.daily_max = _int("AUU_ALERT_DAILY_MAX", 30)

    @property
    def to(self) -> str:
        return self._to or recipient()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- state ---------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM state WHERE key=?", (key,))
            else:
                self._db.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def rows(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---- raising -------------------------------------------------------------------
    def raise_alert(self, key: str, kind: str, subject: str, body: str, *, bypass_limits: bool = False) -> str:
        """Queue + send one alert. Returns sent | failed | duplicate | suppressed | unconfigured."""
        now = self.now_ms()
        with self._lock:
            db = self._db
            dup = db.execute("SELECT 1 FROM alerts WHERE key=? AND status IN ('sent','pending','failed','unconfigured') AND created_at>?",
                             (key, now - self.cooldown_ms)).fetchone()
            if dup:
                return "duplicate"
            if not bypass_limits:
                hour = db.execute("SELECT COUNT(*) FROM alerts WHERE kind!='digest' AND status IN ('sent','failed','pending') AND created_at>?",
                                  (now - 3_600_000,)).fetchone()[0]
                day = db.execute("SELECT COUNT(*) FROM alerts WHERE kind!='digest' AND status IN ('sent','failed','pending') AND created_at>?",
                                 (now - 86_400_000,)).fetchone()[0]
                if hour >= self.hourly_max or day >= self.daily_max:
                    db.execute("INSERT INTO alerts(key, kind, subject, body, created_at, status) VALUES (?,?,?,?,?, 'suppressed')",
                               (key, kind, subject, body, now))
                    log.warning("ALERT suppressed (rate limit %d/h, %d/day) %s: %s", self.hourly_max, self.daily_max, kind, subject)
                    return "suppressed"
            cur = db.execute("INSERT INTO alerts(key, kind, subject, body, created_at, status, to_masked) VALUES (?,?,?,?,?, 'pending', ?)",
                             (key, kind, subject, body, now, _mask(self.to)))
            aid = cur.lastrowid
        return self._deliver(aid)

    def _deliver(self, aid: int) -> str:
        with self._lock:
            row = self._db.execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
        now = self.now_ms()
        if not self.configured():
            self._update(aid, status="unconfigured", error="SMTP not configured")
            log.warning("ALERT not sent (SMTP not configured) %s: %s", row["kind"], row["subject"])
            return "unconfigured"
        attempts = int(row["attempts"]) + 1
        try:
            self.send(self.to, row["subject"], row["body"])
        except Exception as exc:  # never store exc text: SMTP errors can echo addresses
            self._update(aid, status="failed", attempts=attempts, error=type(exc).__name__,
                         next_try=now + 60_000 * (2 ** attempts))
            log.warning("ALERT send failed (%s, attempt %d) %s: %s", type(exc).__name__, attempts, row["kind"], row["subject"])
            return "failed"
        self._update(aid, status="sent", attempts=attempts, sent_at=now, error=None, next_try=None)
        log.warning("ALERT sent to %s %s: %s", _mask(self.to), row["kind"], row["subject"])
        return "sent"

    def _update(self, aid: int, **kv) -> None:
        cols = ", ".join(f"{k}=?" for k in kv)
        with self._lock:
            self._db.execute(f"UPDATE alerts SET {cols} WHERE id=?", (*kv.values(), aid))

    def retry_failed(self) -> int:
        now = self.now_ms()
        with self._lock:
            ids = [r["id"] for r in self._db.execute(
                "SELECT id FROM alerts WHERE status='failed' AND attempts<? AND next_try<=? AND created_at>?",
                (MAX_ATTEMPTS, now, now - 86_400_000))]
        return sum(1 for i in ids if self._deliver(i) == "sent")

    def status(self) -> dict:
        now = self.now_ms()
        with self._lock:
            db = self._db
            last = db.execute("SELECT sent_at, kind, subject FROM alerts WHERE status='sent' ORDER BY sent_at DESC LIMIT 1").fetchone()
            cnt = {r["status"]: r["n"] for r in db.execute(
                "SELECT status, COUNT(*) AS n FROM alerts WHERE created_at>? GROUP BY status", (now - 86_400_000,))}
            err = db.execute("SELECT error FROM alerts WHERE status IN ('failed','unconfigured') ORDER BY id DESC LIMIT 1").fetchone()
        return {"enabled": alerts_enabled(), "configured": bool(self.configured()), "to": _mask(self.to),
                "lastSentAt": int(last["sent_at"]) if last else None, "lastSentKind": last["kind"] if last else None,
                "last24h": cnt, "lastError": err["error"] if err else None,
                "digestTimeBJ": "%02d:%02d" % digest_time(), "lastDigestDay": self.get("digest_day")}


def _mask(addr: str) -> str:
    from app.auth.email_codes import mask_email

    return mask_email(addr)


# ---- checks ----------------------------------------------------------------------------
class AlertMonitor:
    """Collects alert conditions from the runner, risk ledger and market data each check."""

    def __init__(self, center: AlertCenter, *, runner_fn: Callable[[], Any], freshness_fn: Callable[[], dict],
                 extra_fn: Callable[[], dict] = lambda: {}, now_ms: Optional[Callable[[], int]] = None,
                 band_fn: Optional[Callable[[Any], dict]] = None):
        self.c = center
        self.band_fn = band_fn  # ledger -> expected-band evaluation (production: app.paper.expected_band.from_ledger)
        self.runner_fn, self.freshness_fn, self.extra_fn = runner_fn, freshness_fn, extra_fn
        self.now_ms = now_ms or center.now_ms

    def check(self) -> list[str]:
        out: list[str] = []
        r = self.runner_fn()
        if r is not None:
            out += self._runner(r)
            out += self._band(r)
        out += self._data(r)
        self.c.retry_failed()
        d = self._digest(r)
        if d:
            out.append(d)
        return out

    def _runner(self, r) -> list[str]:
        out = []
        st = r.status()
        if st.get("stalled"):
            key = f"stall:{st.get('lastDay')}"
            body = (f"趋势策略纸面调仓停滞：{st.get('reason')}\n上次调仓：{st.get('lastDay') or '无'} 收盘\n"
                    f"等待原因：{st.get('waiting') or '—'}\n最近错误：{st.get('lastError') or '—'}\n"
                    f"停滞阈值 {st.get('stallHours')}h。纸面账本，实盘锁定。")
            out.append(self.c.raise_alert(key, "stall", f"AUUTRADE 告警：策略停滞 {st.get('hoursSinceRebalance')}h", body))
        if st.get("lastError"):
            h = hashlib.sha256(st["lastError"].encode()).hexdigest()[:10]
            out.append(self.c.raise_alert(f"runner_error:{h}", "runner_error", "AUUTRADE 告警：策略 runner 异常",
                                          f"runner 最近错误：\n{st['lastError']}\n\n纸面账本，实盘锁定。"))
        led = r.ledger
        with led._lock:
            mx = led._db.execute("SELECT MAX(id) FROM risk_events").fetchone()[0] or 0
        cur = self.c.get("risk_event_id")
        if cur is None:  # first run: start after what already exists (no back-fill spam)
            self.c.set("risk_event_id", mx)
        elif mx > cur:
            with led._lock:
                ev = led._db.execute("SELECT * FROM risk_events WHERE id>? ORDER BY id", (cur,)).fetchall()
            from app.paper.strategy_risk import KIND_LABEL

            lines = [f"- {bj(int(e['ts']))} {KIND_LABEL.get(e['kind'], e['kind'])} · {e['action']} · 值 {_fmt(e['value'])} "
                     f"阈值 {_fmt(e['threshold'])} · {'收盘调仓' if e['at'] == 'close' else e['at']} {e['detail'] or ''}" for e in ev]
            kinds = sorted({KIND_LABEL.get(e["kind"], e["kind"]) for e in ev})
            out.append(self.c.raise_alert(f"risk:{mx}", "risk", f"AUUTRADE 告警：风控触发 {len(ev)} 条（{'、'.join(kinds)[:60]}）",
                                          "风控事件（北京时间）：\n" + "\n".join(lines) + "\n\n纸面账本，实盘锁定。"))
            self.c.set("risk_event_id", mx)
        return out

    def _band(self, r) -> list[str]:
        """Paper outside the pre-registered backtest band: alert on entering a new outside state, then weekly while it lasts."""
        from app.paper.expected_band import OUTSIDE

        if self.band_fn is None:
            return []
        try:
            ev = self.band_fn(r.ledger)
        except Exception:
            log.exception("expected band check failed")
            return []
        st, prev, now = ev.get("status"), self.c.get("band_alert"), self.now_ms()
        if st not in OUTSIDE:
            if prev is not None and st == "inside":
                self.c.set("band_alert", None)
            return []
        if prev is not None and prev.get("status") == st and now - int(prev.get("at", 0)) < BAND_REPEAT_MS:
            return []
        body = (f"纸面结果{ev['label']}。\n运行天数 N={ev['n']}（截至 {ev.get('day')} 收盘）\n"
                f"纸面累计收益 {_fmt(ev['cum'])}，区间 5%–95%：{_fmt(ev['p05'])} ~ {_fmt(ev['p95'])}（中位 {_fmt(ev['p50'])}）\n"
                f"纸面最大回撤 {_fmt(ev['mdd'])}，预期 5% 最差：{_fmt(ev['mddP05'])}\n"
                f"区间：{ev['name']} v{ev['version']}，seed {ev['registered']['seed']}，block {ev['registered']['block']}，"
                f"{ev['registered']['n_paths']} 条路径，来源 sha {ev['sourceSha']}\n\n{ev['note']}\n纸面账本，实盘锁定。")
        res = self.c.raise_alert(f"band:{st}:{ev.get('day')}", "band", f"AUUTRADE 告警：纸面{ev['label'][:12]}", body)
        if res in ("sent", "unconfigured", "duplicate", "failed"):
            self.c.set("band_alert", {"status": st, "at": now, "day": ev.get("day")})
        return [res]

    def _data(self, r) -> list[str]:
        out = []
        try:
            fr = self.freshness_fn() or {}
        except Exception as exc:
            fr = {"exchange": None, "lastError": f"freshness error {type(exc).__name__}"}
        problems = []
        if fr.get("enabled", True) and not fr.get("exchange"):
            problems.append(("no_exchange", f"没有可用交易所（{fr.get('lastError') or '—'}）"))
        if fr.get("stale"):
            series = sorted(fr.get("staleSeries") or [])
            problems.append((f"stale:{','.join(series)[:200]}", f"行情过期：{', '.join(series)}"))
        if r is not None:
            db = r.ledger.risk_get("data_bad") if hasattr(r.ledger, "risk_get") else None
            if db:
                problems.append((f"data_bad:{db.get('since')}", f"风控数据熔断（只减不开）：{db.get('reason')}"))
        seen = self.c.get("data_seen", {}) or {}
        now_keys = {k for k, _ in problems}
        new_seen = {k: int(seen.get(k, 0)) + 1 for k in now_keys}
        self.c.set("data_seen", new_seen)
        for k, msg in problems:
            if new_seen[k] >= STALE_CONSECUTIVE:  # then the dedup cooldown paces repeats
                out.append(self.c.raise_alert(k, "data", "AUUTRADE 告警：数据异常", f"{msg}\n交易所：{fr.get('exchange') or '—'}\n"
                                              f"上次刷新：{bj(fr['lastRefreshMs']) if fr.get('lastRefreshMs') else '—'}\n\n纸面账本，实盘锁定。"))
        return out

    # ---- digest --------------------------------------------------------------------
    def _digest(self, r) -> Optional[str]:
        now = self.now_ms()
        t = datetime.fromtimestamp(now / 1000, BJ)
        h, m = digest_time()
        if (t.hour, t.minute) < (h, m):
            if self.c.get("digest_armed") is None:
                self.c.set("digest_armed", now)
            return None
        day = t.strftime("%Y-%m-%d")
        prev = self.c.get("digest_day")
        if prev == day:
            return None
        if prev is None and self.c.get("digest_armed") is None:
            self.c.set("digest_armed", self.now_ms())  # first start after the day's slot: begin with the next 08:30
            self.c.set("digest_day", day)
            return None
        if r is not None and t.hour < 10:
            last = r.ledger.last_run()
            if last is not None and int(last["day"]) < r.due_day():
                return None  # today's rebalance not written yet: wait for it (until 10:00)
        subject, body = build_digest(r, self.c, now, extra=self.extra_fn())
        res = self.c.raise_alert(f"digest:{day}", "digest", subject, body, bypass_limits=True)
        if res in ("sent", "unconfigured", "duplicate"):
            self.c.set("digest_day", day)
        return f"digest:{res}"


def _fmt(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v) * 100:+.2f}%"
    except (TypeError, ValueError):
        return str(v)


def build_digest(r, center: AlertCenter, now: int, *, extra: Optional[dict] = None) -> tuple[str, str]:
    from app.version import git_commit

    extra = extra or {}
    lines = [f"AUUTRADE 每日摘要 · {bj(now)}（北京时间）", f"代码版本：{git_commit() or 'unknown'}", "模式：纸面（PAPER），实盘锁定", ""]
    subject = f"AUUTRADE 每日摘要 {datetime.fromtimestamp(now / 1000, BJ).strftime('%Y-%m-%d')}"
    if r is None:
        lines.append("策略 runner 尚未启动（没有账本）。")
    else:
        s = r.summary()
        st = s["status"]
        runs = r.ledger.runs()
        last = runs[-1] if runs else None
        g = s["goNoGo"]
        if last is not None:
            nav = float(last["nav_close"])
            subject += f"：NAV {nav:,.2f}（{float(last['ret']) * 100:+.2f}%）"
            lines += [f"策略：{s['strategy'].get('name')} · 上次调仓 {st.get('lastDay')} 收盘（执行于 {bj(int(last['ran_at']))}）",
                      f"NAV：{nav:,.2f} USDT · 当日 {float(last['ret']) * 100:+.3f}% · 累计 {(nav / float(s['startNav']) - 1) * 100:+.2f}%",
                      f"当日成本 {float(last['cost']) * 100:.4f}% · 资金费 {float(last['funding']) * 100:+.4f}% · 换手 {float(last['turnover']):.3f}"
                      f" · 总敞口 {float(last['gross_after']) * 100:.1f}%",
                      f"Go/No-Go：{g.get('message')}"]
            if st.get("stalled"):
                lines.append(f"⚠ 停滞：{st.get('reason')}")
            lines += ["", "持仓："]
            lines += [f"  {p['coin']:<5} {p['weight'] * 100:6.2f}%  {p['notional']:>10,.2f} USDT" for p in s["positions"]] or ["  空仓"]
            fills = [dict(f) for f in r.ledger.fills(200) if int(f["day"]) == int(last["day"])]
            lines += ["", f"当日调仓（{st.get('lastDay')} 收盘）：{len(fills)} 笔"]
            lines += [f"  {f['coin']:<5} {'买' if f['side'] == 'buy' else '卖'} {f['notional']:>10,.2f} USDT @ {f['fill_price']:.6g}"
                      f"  费+滑点 {f['fee'] + f['slippage']:.3f}" for f in sorted(fills, key=lambda x: x["coin"])]
            lines += ["", f"账本记录哈希（{st.get('lastDay')}，runs 行 + 当日 fills，SHA-256）：", f"  {record_hash(dict(last), fills)}"]
        else:
            lines.append("尚未调仓。")
        ev = []
        with r.ledger._lock:
            ev = r.ledger._db.execute("SELECT * FROM risk_events WHERE ts>? ORDER BY id", (now - 86_400_000,)).fetchall()
        from app.paper.strategy_risk import KIND_LABEL

        lines += ["", f"风控事件（近 24h）：{len(ev)} 条"]
        lines += [f"  {bj(int(e['ts']))} {KIND_LABEL.get(e['kind'], e['kind'])} · {e['action']}" for e in ev[:20]]
        eb = s.get("expectedBand") or {}
        if eb.get("status") in ("inside", "below", "above", "dd_breach"):
            lines += ["", f"回测预期区间（N={eb['n']} 天，非 Go 判定）：{eb['label']} · 累计 {_fmt(eb['cum'])}，"
                          f"5%–95% {_fmt(eb['p05'])} ~ {_fmt(eb['p95'])} · 回撤 {_fmt(eb['mdd'])}（5% 最差 {_fmt(eb['mddP05'])}）"]
        ver = (s.get("version") or {}).get("lastRun") or {}
        if ver.get("params_sha"):
            lines.append(f"本次运行版本：commit {str(ver.get('git_commit') or '—')[:12]} · 参数 {ver['params_sha'][:12]} · 成本模型 {str(ver.get('cost_model_sha'))[:12]}")
        risk = s.get("risk") or {}
        if risk.get("locked"):
            lines.append(f"  🔒 锁定中：{(risk.get('lock') or {}).get('reason')}")
    for k, v in extra.items():
        lines += ["", f"{k}：{v}"]
    st = center.status()
    sup = st["last24h"].get("suppressed", 0)
    lines += ["", f"告警（近 24h）：已发 {st['last24h'].get('sent', 0)}，失败 {st['last24h'].get('failed', 0)}，限流未发 {sup}",
              "", "本邮件只含纸面数据，不含任何凭证。"]
    return subject, "\n".join(lines)


# ---- production wiring -----------------------------------------------------------------
_center: Optional[AlertCenter] = None
_clock = threading.Lock()


def get_center() -> AlertCenter:
    global _center
    with _clock:
        if _center is None:
            _center = AlertCenter()
        return _center


def reset_center(c: Optional[AlertCenter] = None) -> None:
    global _center
    with _clock:
        _center = c


def _extra() -> dict:
    out = {}
    try:
        from app.paper.shadow_s3 import get_shadow

        s = get_shadow().summary()
        out["S3 影子假设（非证据）"] = f"已平 {s['counts']['closed']}/{s['evalAt']} 笔，持有 {s['counts']['open']}，待入场 {s['counts']['pending']}"
    except Exception:
        pass
    return out


def production_monitor() -> AlertMonitor:
    from app.marketdata.mainstream import get_service
    from app.paper.strategy_runner import peek_runner

    from app.paper.expected_band import from_ledger

    return AlertMonitor(get_center(), runner_fn=peek_runner, freshness_fn=lambda: get_service().freshness(), extra_fn=_extra,
                        band_fn=from_ledger)


async def run_loop(interval_sec: int = 120) -> None:
    import asyncio

    await asyncio.sleep(90)  # let the first market refresh and runner tick happen
    mon = production_monitor()
    while True:
        try:
            await asyncio.to_thread(mon.check)
        except Exception:
            log.exception("alert loop")
        await asyncio.sleep(interval_sec)


def main(argv: Optional[list[str]] = None) -> int:
    """python -m app.alerts test | digest-preview | status"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    c = get_center()
    if cmd == "test":
        from app.version import git_commit

        now = c.now_ms()
        import secrets

        res = c.raise_alert(f"test:{now}:{secrets.token_hex(4)}", "test", "AUUTRADE 测试邮件：告警通道已接通",
                            f"这是一封测试邮件，确认 AUUTRADE 告警和每日摘要通道可用。\n时间：{bj(now)}（北京时间）\n"
                            f"代码版本：{git_commit() or 'unknown'}\n每日摘要时间：北京时间 %02d:%02d\n\n纸面账本，实盘锁定；邮件不含任何凭证。" % digest_time(),
                            bypass_limits=True)
        print(json.dumps({"result": res, "to": _mask(c.to), "at": bj(now)}, ensure_ascii=False))
        return 0 if res == "sent" else 1
    if cmd == "digest-preview":
        from app.paper.strategy_runner import peek_runner

        print("\n\n".join(build_digest(peek_runner(), c, c.now_ms(), extra=_extra())))
        return 0
    if cmd == "status":
        print(json.dumps(c.status(), ensure_ascii=False))
        return 0
    print("usage: python -m app.alerts test | digest-preview | status")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
