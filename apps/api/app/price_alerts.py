"""User price alerts on the mainstream chart.

Table ``price_alerts`` (+ the in-site notification table ``price_alert_events``) lives in its own
``price_alerts.sqlite``: never read by trading, ledger, exchange or points code.

* One alert = (user, symbol, venue, target price, direction above|below|cross, note, status).
* A background checker runs on the **primary only** (``price_alerts_enabled()`` is False on a standby, so
  the HK node never evaluates alerts and never sends mail). It reads the market-data service's cached
  ticker prices (no extra exchange load beyond the shared ticker cache, whose TTL alone sets the request
  rate) every ``CHECK_INTERVAL_SEC`` and only when at least one alert is active.
* Fire-once: every alert keeps ``side`` (price below target = -1, at/above = +1) from the last fresh price.
  It fires when the side flips in the alert's direction, and the row is moved ``active -> triggered`` with a
  compare-and-set UPDATE, so a restart, a second checker or a price that wobbles around the target can
  never fire it twice. The first price seen after creation only records the side (never fires).
* A trigger creates an in-site notification (badge) and, if the user has a registered email, one mail via
  the existing AUUTRADE SMTP channel (auth.email_codes), in the user's interface language, with retry for 24 h.

Live ticker prices older than ``MAX_PRICE_AGE_MS`` are ignored (no firing on stale data). When the ticker feed is
unavailable the market list falls back to its store rows (``priceSource == "store"``, the last stored candle); those keep
the previous, looser ``MAX_STORE_PRICE_AGE_MS`` rule, so a feed outage degrades to the old behaviour instead of stopping alerts.
"""
from __future__ import annotations

import asyncio
import html
import logging
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.data_paths import data_dir

log = logging.getLogger("auu.price_alerts")

DB_NAME = "price_alerts.sqlite"
DIRECTIONS = ("above", "below", "cross")
MAX_ACTIVE = 20  # per user
MAX_HISTORY = 100  # triggered / cancelled rows kept per user
MAX_EVENTS = 200  # in-site notifications kept per user
MAX_NOTE = 80
PRICE_MAX = 1e9
MAX_PRICE_AGE_MS = 15_000  # live ticker price (was 10 min)
MAX_STORE_PRICE_AGE_MS = 10 * 60_000  # fallback rows built from the stored candles (the previous rule)
CHECK_INTERVAL_SEC = 5  # was 15
MAIL_WINDOW_MS = 15_000  # at most MAIL_PER_TICK mails per this window: the pace of the old 15 s tick, kept now that the checker ticks 3x as often
MAIL_RETRY_MAX_MS = 10 * 60_000
MAIL_QUEUE_MS = 86_400_000
MAIL_MAX_ATTEMPTS = 200
MAIL_PER_USER_HOUR = 30  # beyond this, the trigger is in-site only (mail flood guard)
MAIL_PER_TICK = 5

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,20}$")
_UID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f\u2028\u2029]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  venue TEXT NOT NULL DEFAULT '',
  price REAL NOT NULL,
  direction TEXT NOT NULL,              -- above | below | cross
  created_at INTEGER NOT NULL,
  triggered_at INTEGER,
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,                 -- active | triggered | cancelled
  side INTEGER,                         -- last seen side of the price vs target (-1 below, +1 at/above)
  trigger_price REAL
);
CREATE INDEX IF NOT EXISTS price_alerts_user ON price_alerts (user_id, status);
CREATE INDEX IF NOT EXISTS price_alerts_active ON price_alerts (status, symbol);
CREATE TABLE IF NOT EXISTS price_alert_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  alert_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  venue TEXT NOT NULL DEFAULT '',
  target REAL NOT NULL,
  direction TEXT NOT NULL,
  fired_dir TEXT NOT NULL,              -- up | down
  trigger_price REAL NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  read_at INTEGER,
  email_status TEXT NOT NULL DEFAULT 'none',  -- none | pending | sent | failed | suppressed | no_email | unconfigured
  email_attempts INTEGER NOT NULL DEFAULT 0,
  email_next_try INTEGER,
  email_sent_at INTEGER
);
CREATE INDEX IF NOT EXISTS price_alert_events_user ON price_alert_events (user_id, id);
CREATE INDEX IF NOT EXISTS price_alert_events_mail ON price_alert_events (email_status, email_next_try);
"""


class AlertError(ValueError):
    """Invalid request (``code`` is the API error code, ``status`` the HTTP status)."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def price_alerts_enabled() -> bool:
    """Primary only: a standby never evaluates alerts or sends their mail (whatever the env says)."""
    from app.role import is_standby

    if is_standby():
        return False
    return os.getenv("AUU_PRICE_ALERTS", "on").strip().lower() not in {"0", "false", "off", "no"}


# ---- validation ------------------------------------------------------------------------------
def normalize_symbol(raw: Any) -> str:
    base = "".join(ch for ch in str(raw or "").split("/")[0].upper() if ch.isalnum())
    if not _SYMBOL_RE.match(base):
        raise AlertError("BAD_SYMBOL", "币种格式不正确")
    return base


def normalize_price(raw: Any) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise AlertError("BAD_PRICE", "价格必须是数字")
    p = float(raw)
    if not math.isfinite(p) or p <= 0 or p > PRICE_MAX:
        raise AlertError("BAD_PRICE", "价格超出范围")
    return float(f"{p:.10g}")


def normalize_direction(raw: Any) -> str:
    d = str(raw or "").strip().lower()
    if d not in DIRECTIONS:
        raise AlertError("BAD_DIRECTION", "方向必须是 above / below / cross")
    return d


def normalize_note(raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise AlertError("BAD_NOTE", "备注必须是文字")
    return _CTRL_RE.sub(" ", raw).strip()[:MAX_NOTE]


def normalize_venue(raw: Any) -> str:
    v = str(raw or "").strip().lower()
    if v not in {"", "binance", "okx"}:
        raise AlertError("BAD_VENUE", "交易所只能是 binance 或 okx")
    return v


def side_of(price: float, target: float) -> int:
    return 1 if price >= target else -1


def _view(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(r["id"]), "symbol": r["symbol"], "venue": r["venue"], "price": float(r["price"]), "direction": r["direction"],
        "createdAt": int(r["created_at"]), "triggeredAt": int(r["triggered_at"]) if r["triggered_at"] is not None else None,
        "note": r["note"], "status": r["status"],
        "triggerPrice": float(r["trigger_price"]) if r["trigger_price"] is not None else None,
    }


def _event_view(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(r["id"]), "alertId": int(r["alert_id"]), "symbol": r["symbol"], "venue": r["venue"], "target": float(r["target"]),
        "direction": r["direction"], "firedDir": r["fired_dir"], "triggerPrice": float(r["trigger_price"]), "note": r["note"],
        "createdAt": int(r["created_at"]), "read": r["read_at"] is not None, "email": r["email_status"],
    }


class PriceAlertStore:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else data_dir() / DB_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- user-facing CRUD ----------------------------------------------------------------
    def create(self, user_id: str, symbol: str, price: float, direction: str, *, venue: str = "", note: str = "",
               side: Optional[int] = None, now_ms: Optional[int] = None, max_active: int = MAX_ACTIVE) -> dict[str, Any]:
        if not _UID_RE.match(user_id or ""):
            raise AlertError("BAD_USER", "user id")
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                n = self._db.execute("SELECT COUNT(*) FROM price_alerts WHERE user_id=? AND status='active'", (user_id,)).fetchone()[0]
                if n >= max_active:
                    raise AlertError("ALERT_LIMIT", f"最多同时保留 {max_active} 条生效中的价格提醒，请先删除不需要的", 409)
                dup = self._db.execute(
                    "SELECT 1 FROM price_alerts WHERE user_id=? AND status='active' AND symbol=? AND price=? AND direction=?",
                    (user_id, symbol, price, direction)).fetchone()
                if dup:
                    raise AlertError("ALERT_DUPLICATE", "已经有一条相同的价格提醒", 409)
                cur = self._db.execute(
                    "INSERT INTO price_alerts(user_id, symbol, venue, price, direction, created_at, note, status, side) VALUES (?,?,?,?,?,?,?,'active',?)",
                    (user_id, symbol, venue, price, direction, now, note, side))
                aid = cur.lastrowid
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            row = self._db.execute("SELECT * FROM price_alerts WHERE id=?", (aid,)).fetchone()
        return _view(row)

    def list(self, user_id: str, *, status: Optional[str] = None, symbol: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM price_alerts WHERE user_id=?", [user_id]
        if status:
            q += " AND status=?"
            args.append(status)
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        q += " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [_view(r) for r in self._db.execute(q, args)]

    def counts(self, user_id: str) -> dict[str, int]:
        with self._lock:
            return {r["status"]: int(r["n"]) for r in self._db.execute(
                "SELECT status, COUNT(*) AS n FROM price_alerts WHERE user_id=? GROUP BY status", (user_id,))}

    def delete(self, user_id: str, alert_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM price_alerts WHERE id=? AND user_id=?", (alert_id, user_id))
            return cur.rowcount > 0

    def clear_history(self, user_id: str) -> int:
        with self._lock:
            return self._db.execute("DELETE FROM price_alerts WHERE user_id=? AND status!='active'", (user_id,)).rowcount

    # ---- checker -------------------------------------------------------------------------
    def active_symbols(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT DISTINCT symbol FROM price_alerts WHERE status='active'")}

    def evaluate(self, prices: dict[str, float], *, venue: str = "", now_ms: Optional[int] = None) -> list[dict[str, Any]]:
        """Compare fresh prices with the active alerts; fire (once) those whose side flipped in their direction."""
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        fired: list[dict[str, Any]] = []
        with self._lock:
            rows = self._db.execute("SELECT * FROM price_alerts WHERE status='active'").fetchall()
            for r in rows:
                p = prices.get(r["symbol"])
                if p is None or not math.isfinite(p) or p <= 0:
                    continue
                target, s, prev = float(r["price"]), side_of(p, float(r["price"])), r["side"]
                if prev is None:
                    self._db.execute("UPDATE price_alerts SET side=? WHERE id=? AND status='active'", (s, r["id"]))
                    continue
                if s == prev:
                    continue
                up = s > prev
                hit = r["direction"] == "cross" or (r["direction"] == "above" and up) or (r["direction"] == "below" and not up)
                if not hit:  # price crossed the wrong way (e.g. an "above" alert fell further): just remember the side
                    self._db.execute("UPDATE price_alerts SET side=? WHERE id=? AND status='active'", (s, r["id"]))
                    continue
                cur = self._db.execute(
                    "UPDATE price_alerts SET status='triggered', triggered_at=?, trigger_price=?, side=? WHERE id=? AND status='active'",
                    (now, p, s, r["id"]))
                if cur.rowcount != 1:  # lost a race: someone else fired / deleted it
                    continue
                self._db.execute(
                    "INSERT INTO price_alert_events(user_id, alert_id, symbol, venue, target, direction, fired_dir, trigger_price, note, created_at, email_status)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?, 'pending')",
                    (r["user_id"], r["id"], r["symbol"], venue or r["venue"], target, r["direction"], "up" if up else "down", p, r["note"], now))
                fired.append({"alertId": int(r["id"]), "userId": r["user_id"], "symbol": r["symbol"], "target": target,
                              "direction": r["direction"], "firedDir": "up" if up else "down", "price": p})
                self._prune(r["user_id"])
        return fired

    def _prune(self, user_id: str) -> None:
        self._db.execute(
            "DELETE FROM price_alerts WHERE user_id=? AND status!='active' AND id NOT IN "
            "(SELECT id FROM price_alerts WHERE user_id=? AND status!='active' ORDER BY id DESC LIMIT ?)", (user_id, user_id, MAX_HISTORY))
        self._db.execute(
            "DELETE FROM price_alert_events WHERE user_id=? AND id NOT IN (SELECT id FROM price_alert_events WHERE user_id=? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_EVENTS))

    # ---- in-site notifications -----------------------------------------------------------
    def events(self, user_id: str, *, unread_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        q = "SELECT * FROM price_alert_events WHERE user_id=?" + (" AND read_at IS NULL" if unread_only else "") + " ORDER BY id DESC LIMIT ?"
        with self._lock:
            return [_event_view(r) for r in self._db.execute(q, (user_id, limit))]

    def unread(self, user_id: str) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM price_alert_events WHERE user_id=? AND read_at IS NULL", (user_id,)).fetchone()[0])

    def mark_read(self, user_id: str, ids: Optional[list[int]] = None, *, now_ms: Optional[int] = None) -> int:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        with self._lock:
            if ids is None:
                return self._db.execute("UPDATE price_alert_events SET read_at=? WHERE user_id=? AND read_at IS NULL", (now, user_id)).rowcount
            n = 0
            for i in ids[:200]:
                n += self._db.execute("UPDATE price_alert_events SET read_at=? WHERE id=? AND user_id=? AND read_at IS NULL", (now, i, user_id)).rowcount
            return n

    # ---- mail outbox ---------------------------------------------------------------------
    def mail_due(self, now_ms: int, limit: int = MAIL_PER_TICK) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM price_alert_events WHERE email_status IN ('pending','failed') AND email_attempts<? AND created_at>?"
                " AND (email_next_try IS NULL OR email_next_try<=?) ORDER BY id LIMIT ?",
                (MAIL_MAX_ATTEMPTS, now_ms - MAIL_QUEUE_MS, now_ms, limit)).fetchall()

    def mail_sent_last_hour(self, user_id: str, now_ms: int) -> int:
        with self._lock:
            return int(self._db.execute(
                "SELECT COUNT(*) FROM price_alert_events WHERE user_id=? AND email_status='sent' AND email_sent_at>?", (user_id, now_ms - 3_600_000)).fetchone()[0])

    def mail_update(self, event_id: int, **kv: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in kv)
        with self._lock:
            self._db.execute(f"UPDATE price_alert_events SET {cols} WHERE id=?", (*kv.values(), event_id))


# ---- mail text (11 languages; {brand} is never translated) ----------------------------------------
BRAND = "AUUTRADE"
MAIL_TEXTS: dict[str, dict[str, str]] = {
    "zh-CN": {"up": "{brand} 价格提醒：{sym} 涨到 {price}", "down": "{brand} 价格提醒：{sym} 跌到 {price}",
              "intro": "您设置的价格提醒已触发。", "sym": "标的", "target": "提醒价", "last": "触发时价格", "time": "时间（北京时间）",
              "note": "备注", "venue": "行情来源", "note_disc": "价格来自交易所公开行情，可能与您看到的 K 线有秒级差异。本提醒仅供参考，不构成投资建议。\n可在图表的“价格提醒”面板中管理或删除提醒。"},
    "en": {"up": "{brand} price alert: {sym} rose to {price}", "down": "{brand} price alert: {sym} fell to {price}",
           "intro": "Your price alert has been triggered.", "sym": "Symbol", "target": "Alert price", "last": "Price at trigger", "time": "Time (UTC)",
           "note": "Note", "venue": "Price source", "note_disc": "Prices come from public exchange market data and may differ by a few seconds from the candles you see. This alert is for information only, not investment advice.\nManage or delete alerts in the chart's Price alerts panel."},
    "de": {"up": "{brand} Preisalarm: {sym} ist auf {price} gestiegen", "down": "{brand} Preisalarm: {sym} ist auf {price} gefallen",
           "intro": "Ihr Preisalarm wurde ausgelöst.", "sym": "Symbol", "target": "Alarmpreis", "last": "Preis bei Auslösung", "time": "Zeit (UTC)",
           "note": "Notiz", "venue": "Preisquelle", "note_disc": "Die Preise stammen aus öffentlichen Börsendaten und können um einige Sekunden von den Kerzen abweichen. Dieser Alarm dient nur der Information und ist keine Anlageberatung.\nAlarme verwalten oder löschen Sie im Chart-Bereich „Preisalarme“."},
    "fr": {"up": "{brand} alerte de prix : {sym} est monté à {price}", "down": "{brand} alerte de prix : {sym} est tombé à {price}",
           "intro": "Votre alerte de prix a été déclenchée.", "sym": "Symbole", "target": "Prix de l'alerte", "last": "Prix au déclenchement", "time": "Heure (UTC)",
           "note": "Note", "venue": "Source du prix", "note_disc": "Les prix proviennent des données de marché publiques des plateformes et peuvent différer de quelques secondes des bougies affichées. Cette alerte est fournie à titre informatif et ne constitue pas un conseil en investissement.\nGérez ou supprimez les alertes dans le panneau « Alertes de prix » du graphique."},
    "es": {"up": "{brand} alerta de precio: {sym} subió a {price}", "down": "{brand} alerta de precio: {sym} bajó a {price}",
           "intro": "Se ha activado tu alerta de precio.", "sym": "Símbolo", "target": "Precio de la alerta", "last": "Precio al activarse", "time": "Hora (UTC)",
           "note": "Nota", "venue": "Fuente del precio", "note_disc": "Los precios provienen de datos públicos de mercado y pueden diferir unos segundos de las velas que ves. Esta alerta es solo informativa y no constituye asesoramiento de inversión.\nGestiona o elimina las alertas en el panel «Alertas de precio» del gráfico."},
    "pt": {"up": "{brand} alerta de preço: {sym} subiu para {price}", "down": "{brand} alerta de preço: {sym} caiu para {price}",
           "intro": "O seu alerta de preço foi acionado.", "sym": "Símbolo", "target": "Preço do alerta", "last": "Preço no acionamento", "time": "Hora (UTC)",
           "note": "Nota", "venue": "Fonte do preço", "note_disc": "Os preços vêm de dados públicos de mercado e podem diferir alguns segundos dos candles que você vê. Este alerta é apenas informativo e não é aconselhamento de investimento.\nGerencie ou exclua alertas no painel «Alertas de preço» do gráfico."},
    "tr": {"up": "{brand} fiyat alarmı: {sym} {price} seviyesine yükseldi", "down": "{brand} fiyat alarmı: {sym} {price} seviyesine düştü",
           "intro": "Fiyat alarmınız tetiklendi.", "sym": "Sembol", "target": "Alarm fiyatı", "last": "Tetiklenme fiyatı", "time": "Zaman (UTC)",
           "note": "Not", "venue": "Fiyat kaynağı", "note_disc": "Fiyatlar herkese açık borsa verilerinden gelir ve gördüğünüz mumlardan birkaç saniye farklı olabilir. Bu alarm yalnızca bilgilendirme amaçlıdır, yatırım tavsiyesi değildir.\nAlarmları grafikteki “Fiyat alarmları” panelinden yönetebilir veya silebilirsiniz."},
    "ru": {"up": "{brand}: ценовой алерт — {sym} вырос до {price}", "down": "{brand}: ценовой алерт — {sym} упал до {price}",
           "intro": "Ваш ценовой алерт сработал.", "sym": "Инструмент", "target": "Цена алерта", "last": "Цена при срабатывании", "time": "Время (UTC)",
           "note": "Заметка", "venue": "Источник цены", "note_disc": "Цены берутся из публичных рыночных данных бирж и могут на несколько секунд отличаться от свечей на графике. Алерт носит информационный характер и не является инвестиционной рекомендацией.\nУправлять алертами и удалять их можно на панели «Ценовые алерты» графика."},
    "ja": {"up": "{brand} 価格アラート：{sym} が {price} に上昇しました", "down": "{brand} 価格アラート：{sym} が {price} に下落しました",
           "intro": "設定した価格アラートが作動しました。", "sym": "銘柄", "target": "アラート価格", "last": "作動時の価格", "time": "時刻（UTC）",
           "note": "メモ", "venue": "価格ソース", "note_disc": "価格は取引所の公開市場データに基づくため、チャートのローソク足と数秒の差が出ることがあります。本アラートは情報提供のみを目的とし、投資助言ではありません。\nアラートはチャートの「価格アラート」パネルで管理・削除できます。"},
    "ko": {"up": "{brand} 가격 알림: {sym}이(가) {price}까지 상승했습니다", "down": "{brand} 가격 알림: {sym}이(가) {price}까지 하락했습니다",
           "intro": "설정한 가격 알림이 발동되었습니다.", "sym": "종목", "target": "알림 가격", "last": "발동 시 가격", "time": "시간 (UTC)",
           "note": "메모", "venue": "가격 출처", "note_disc": "가격은 거래소 공개 시세 데이터를 사용하므로 차트의 캔들과 몇 초 차이가 있을 수 있습니다. 이 알림은 정보 제공용이며 투자 조언이 아닙니다.\n알림은 차트의 '가격 알림' 패널에서 관리하거나 삭제할 수 있습니다."},
    "ar": {"up": "{brand} تنبيه سعر: ارتفع {sym} إلى {price}", "down": "{brand} تنبيه سعر: انخفض {sym} إلى {price}",
           "intro": "تم تفعيل تنبيه السعر الذي أنشأته.", "sym": "الرمز", "target": "سعر التنبيه", "last": "السعر عند التفعيل", "time": "الوقت (UTC)",
           "note": "ملاحظة", "venue": "مصدر السعر", "note_disc": "الأسعار مأخوذة من بيانات السوق العامة للمنصات وقد تختلف ببضع ثوانٍ عن الشموع التي تراها. هذا التنبيه للعلم فقط وليس نصيحة استثمارية.\nيمكنك إدارة التنبيهات أو حذفها من لوحة «تنبيهات السعر» في الرسم البياني."},
}


def fmt_price(p: float) -> str:
    s = f"{p:,.8f}".rstrip("0").rstrip(".")
    return s


def render_mail(lang: str, ev: dict[str, Any]) -> tuple[str, str, str]:
    """(subject, plain text, html) for one fired alert, in the user's language; signature from auth.email_i18n."""
    from app.auth.email_i18n import RTL, TEXTS, EMAIL_LANGS

    lang = lang if lang in MAIL_TEXTS and lang in EMAIL_LANGS else "zh-CN"
    T = {k: v.replace("{brand}", BRAND) for k, v in MAIL_TEXTS[lang].items()}
    sig = {k: TEXTS[lang][k].replace("{brand}", BRAND) for k in ("hello", "team", "auto")}
    price = fmt_price(float(ev["target"]))
    sym = str(ev["symbol"])
    subject = T[ev["firedDir"]].format(sym=sym, price=price)
    when = datetime.fromtimestamp(int(ev["createdAt"]) / 1000, timezone.utc)
    if lang == "zh-CN":
        when = when.astimezone(timezone(timedelta(hours=8)))
    rows = [(T["sym"], sym), (T["target"], price), (T["last"], fmt_price(float(ev["triggerPrice"]))), (T["time"], when.strftime("%Y-%m-%d %H:%M:%S"))]
    if ev.get("venue"):
        rows.append((T["venue"], str(ev["venue"]).upper()))
    if ev.get("note"):
        rows.append((T["note"], str(ev["note"])))
    text = f"{sig['hello']}\n\n{T['intro']}\n\n" + "\n".join(f"{k}: {v}" for k, v in rows) + f"\n\n{T['note_disc']}\n\n{sig['team']}\n\n{sig['auto']}\n"
    esc = html.escape
    trs = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#6b7280;">{esc(k)}</td><td dir="ltr" style="padding:6px 0;color:#111827;font-weight:600;">{esc(v)}</td></tr>'
        for k, v in rows)
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif"
    body = f"""<!DOCTYPE html>
<html lang="{lang}" dir="{'rtl' if lang in RTL else 'ltr'}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(subject)}</title></head>
<body style="margin:0;padding:0;background:#f3f4f6;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:32px 12px;font-family:{font};">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#ffffff;border-radius:8px;overflow:hidden;border:1px solid #e5e7eb;">
<tr><td style="background:#0f172a;padding:20px 32px;"><span style="color:#ffffff;font-size:22px;font-weight:700;letter-spacing:4px;">{BRAND}</span></td></tr>
<tr><td style="padding:32px;color:#111827;font-size:15px;line-height:1.7;">
<p style="margin:0 0 12px;">{esc(sig['hello'])}</p>
<p style="margin:0 0 16px;">{esc(T['intro'])}</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 20px;">{trs}</table>
<p style="margin:0 0 24px;color:#374151;font-size:13px;white-space:pre-line;">{esc(T['note_disc'])}</p>
<p style="margin:0;color:#111827;">{esc(sig['team'])}</p>
</td></tr>
<tr><td style="padding:16px 32px;background:#f9fafb;border-top:1px solid #e5e7eb;color:#9ca3af;font-size:12px;line-height:1.6;">{esc(sig['auto'])}<br>&copy; {BRAND}</td></tr>
</table></td></tr></table></body></html>
"""
    return subject, text, body


# ---- the checker -----------------------------------------------------------------------------------
UserLookup = Callable[[str], Optional[dict[str, Any]]]
Sender = Callable[[str, str, str, Optional[str]], Any]


def _default_user(uid: str) -> Optional[dict[str, Any]]:
    from app.auth import accounts

    return accounts.user_by_id(uid)


def _default_send(to: str, subject: str, body: str, html_body: Optional[str]) -> Any:
    from app.auth import email_codes

    return email_codes._smtp_send(to, subject, body, html_body)


def fresh_prices(markets: dict[str, Any], now_ms: int) -> dict[str, float]:
    """symbol -> last price from ``MainstreamService.markets()`` rows that are fresh enough to act on."""
    out: dict[str, float] = {}
    for it in markets.get("items") or []:
        if it.get("priceSource") == "store":
            p, ts, limit = it.get("price"), it.get("priceTs"), MAX_STORE_PRICE_AGE_MS
        else:  # live ticker: judge by when the server fetched it (priceAt), else the exchange's own stat time
            p, ts, limit = it.get("price"), it.get("priceAt") or it.get("priceTs"), MAX_PRICE_AGE_MS
        if p and ts and now_ms - int(ts) <= limit:
            out[str(it["symbol"])] = float(p)
    return out


class AlertChecker:
    def __init__(self, store: PriceAlertStore, *, markets_fn: Callable[[], dict[str, Any]], user_fn: UserLookup = _default_user,
                 send: Sender = _default_send, configured: Optional[Callable[[], bool]] = None,
                 now_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        from app.auth.email_codes import smtp_configured

        self.store, self.markets_fn, self.user_fn, self.send = store, markets_fn, user_fn, send
        self.configured = configured or smtp_configured
        self.now_ms = now_ms
        self._sent_at: list[int] = []
        self.last_attempts = 0

    def check(self) -> list[dict[str, Any]]:
        if not self.store.active_symbols():
            return []  # nothing to watch: do not even touch the market-data service
        now = self.now_ms()
        mk = self.markets_fn()
        return self.store.evaluate(fresh_prices(mk, now), venue=str(mk.get("exchange") or ""), now_ms=now)

    def deliver(self, limit: int = MAIL_PER_TICK) -> int:
        """Send due mails (retry with backoff for 24 h). Returns the number sent."""
        now = self.now_ms()
        sent = 0
        self.last_attempts = 0
        for row in self.store.mail_due(now, limit):
            eid, uid = int(row["id"]), row["user_id"]
            user = self.user_fn(uid) or {}
            to = str(user.get("email") or "").strip()
            if not to:
                self.store.mail_update(eid, email_status="no_email")
                continue
            if not self.configured():
                self.store.mail_update(eid, email_status="unconfigured")
                continue
            if self.store.mail_sent_last_hour(uid, now) >= MAIL_PER_USER_HOUR:
                self.store.mail_update(eid, email_status="suppressed")
                continue
            ev = _event_view(row)
            subject, text, body = render_mail(str(user.get("locale") or "zh-CN"), ev)
            attempts = int(row["email_attempts"]) + 1
            self.last_attempts += 1  # failed SMTP tries use the pacing budget too
            try:
                self.send(to, subject, text, body)
            except Exception as exc:  # never log exc text: SMTP errors can echo addresses
                self.store.mail_update(eid, email_status="failed", email_attempts=attempts,
                                       email_next_try=now + min(60_000 * (2 ** min(attempts, 10)), MAIL_RETRY_MAX_MS))
                log.warning("price alert mail failed (%s, attempt %d) event %d", type(exc).__name__, attempts, eid)
                continue
            self.store.mail_update(eid, email_status="sent", email_attempts=attempts, email_sent_at=self.now_ms(), email_next_try=None)
            sent += 1
            log.warning("price alert mail sent event %d", eid)
        return sent

    def tick(self) -> dict[str, int]:
        fired = self.check()
        for f in fired:
            log.warning("price alert fired: %s %s %s at %s", f["symbol"], f["direction"], f["target"], f["price"])
        now = self.now_ms()
        self._sent_at = [t for t in self._sent_at if now - t < MAIL_WINDOW_MS]
        room = MAIL_PER_TICK - len(self._sent_at)  # sliding window: never faster than MAIL_PER_TICK per 15 s
        mailed = 0
        if room > 0:
            mailed = self.deliver(room)
            self._sent_at += [now] * self.last_attempts
        return {"fired": len(fired), "mailed": mailed}


# ---- production wiring -----------------------------------------------------------------------------
_STORE: Optional[PriceAlertStore] = None
_STORE_LOCK = threading.Lock()


def get_store() -> PriceAlertStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = PriceAlertStore()
        return _STORE


def reset_store(store: Optional[PriceAlertStore] = None) -> None:
    global _STORE
    with _STORE_LOCK:
        _STORE = store


def production_checker() -> AlertChecker:
    from app.marketdata.mainstream import get_service

    return AlertChecker(get_store(), markets_fn=lambda: get_service().markets(sync=True))


async def run_loop(interval_sec: int = CHECK_INTERVAL_SEC) -> None:
    await asyncio.sleep(75)  # let the first market refresh happen
    checker = production_checker()
    while True:
        try:
            await asyncio.to_thread(checker.tick)
        except Exception:
            log.exception("price alert loop")
        await asyncio.sleep(interval_sec)
