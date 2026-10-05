"""行业动态: crypto industry headlines from free public RSS feeds (title + short summary + link only).

- Sources (no key, no account, no cost): CoinDesk and Cointelegraph public RSS. Each item keeps the
  source name and the original link; the full article is never fetched or stored.
- A background loop fetches every ``AUU_NEWS_INTERVAL_SEC`` (default 720 s = 12 min, clamped to
  300..3600) and writes ``news.sqlite`` in the data dir. Every failure is caught and recorded in
  ``fetch_log``; nothing here is read by the strategy, the ledgers, health or the guard, so a dead
  feed can never affect the main site.
- Each item is tagged with the strategy's 19 coins it mentions (BTC/ETH/SOL first), plus a
  keyword-based ``important`` flag (ETF, SEC, hack, listing, upgrade ...).
- Off switch: ``AUU_NEWS=off``. Under the test runner (AUU_TEST=1) it is off unless set explicitly.
- Retention: 30 days, at most 3000 rows.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from app.data_paths import data_dir

log = logging.getLogger("auu.news")

LEDGER_NAME = "news.sqlite"
SOURCES: dict[str, dict[str, str]] = {
    "coindesk": {"name": "CoinDesk", "home": "https://www.coindesk.com/", "url": "https://www.coindesk.com/arc/outboundfeeds/rss"},
    "cointelegraph": {"name": "Cointelegraph", "home": "https://cointelegraph.com/", "url": "https://cointelegraph.com/rss"},
}
USER_AGENT = "AUUTRADE-news/1.0 (+https://auutrade.com; RSS reader, headlines only)"
MAX_BYTES = 3_000_000
TITLE_MAX = 240
SUMMARY_MAX = 280
RETAIN_DAYS = 30
RETAIN_ROWS = 3000

# Strategy universe (UNIVERSE_19) in display priority: BTC/ETH/SOL first.
COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "LTC", "TRX", "DOT", "BCH", "ETC", "XLM", "ATOM", "FIL", "UNI", "NEAR"]
FOCUS = ("BTC", "ETH", "SOL")
# (case-insensitive names, case-sensitive tickers). Tickers that are also English words (SOL, LINK, DOT, NEAR,
# UNI, FIL, ATOM, ETC, ADA) only match in upper case.
_NAMES: dict[str, tuple[str, str]] = {
    "BTC": (r"bitcoin(?!\s+cash)", r"BTC"),
    "ETH": (r"ethereum(?!\s+classic)|\bether\b", r"ETH"),
    "SOL": (r"solana", r"SOL"),
    "XRP": (r"\bripple\b", r"XRP"),
    "DOGE": (r"dogecoin", r"DOGE"),
    "BNB": (r"bnb\s+chain|binance\s+coin", r"BNB"),
    "ADA": (r"cardano", r"ADA"),
    "AVAX": (r"avalanche", r"AVAX"),
    "LINK": (r"chainlink", r"LINK"),
    "LTC": (r"litecoin", r"LTC"),
    "TRX": (r"\btron\b", r"TRX"),
    "DOT": (r"polkadot", r"DOT"),
    "BCH": (r"bitcoin\s+cash", r"BCH"),
    "ETC": (r"ethereum\s+classic", r"ETC"),
    "XLM": (r"\bstellar\b", r"XLM"),
    "ATOM": (r"\bcosmos\b", r"ATOM"),
    "FIL": (r"filecoin", r"FIL"),
    "UNI": (r"uniswap", r"UNI"),
    "NEAR": (r"near\s+protocol", r"NEAR"),
}
_RX = {c: (re.compile(n, re.I), re.compile(r"(?<![A-Za-z0-9$])\$?" + t + r"(?![A-Za-z0-9])")) for c, (n, t) in _NAMES.items()}
_IMPORTANT = re.compile(
    r"\b(etfs?|sec|cftc|fed|federal reserve|rate (?:cut|hike)|lawsuit|sues?|charged|approv\w*|ban(?:s|ned)?|hack\w*|exploit\w*|"
    r"stolen|drain\w*|outage|halt\w*|delist\w*|hard fork|upgrade|mainnet|liquidat\w*|bankrupt\w*|"
    r"regulat\w*|sanction\w*|stablecoin bill)\b",
    re.I,
)


def news_enabled() -> bool:
    raw = os.getenv("AUU_NEWS")
    if raw is None:
        return os.getenv("AUU_TEST", "") != "1"
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def interval_sec() -> int:
    try:
        v = int(os.getenv("AUU_NEWS_INTERVAL_SEC", "") or 720)
    except ValueError:
        v = 720
    return max(300, min(3600, v))


def enabled_sources() -> list[str]:
    raw = (os.getenv("AUU_NEWS_SOURCES") or "").strip()
    if not raw:
        return list(SOURCES)
    return [s for s in (x.strip().lower() for x in raw.split(",")) if s in SOURCES]


# ---- parsing ---------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean_text(s: Optional[str], limit: int) -> str:
    """HTML/CDATA fragment -> plain single-line text, cut at ``limit`` chars on a word boundary."""
    if not s:
        return ""
    t = _TAG.sub(" ", s)
    t = html.unescape(html.unescape(t))
    t = _WS.sub(" ", t).strip()
    if len(t) > limit:
        cut = t[: limit - 1]
        sp = cut.rfind(" ")
        t = (cut[:sp] if sp > limit * 0.6 else cut).rstrip(" ,.;:-") + "…"
    return t


def clean_url(u: Optional[str]) -> Optional[str]:
    """http(s) only; drop utm_* tracking parameters."""
    u = (u or "").strip()
    try:
        p = urlsplit(u)
    except ValueError:
        return None
    if p.scheme not in {"http", "https"} or not p.netloc:
        return None
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def tag_coins(text: str) -> list[str]:
    out = []
    for c in COINS:
        name, tick = _RX[c]
        if name.search(text) or tick.search(text):
            out.append(c)
    return out


def is_important(title: str) -> bool:
    return bool(_IMPORTANT.search(title or ""))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> Optional[ET.Element]:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: ET.Element, name: str) -> str:
    c = _child(el, name)
    return (c.text or "") if c is not None else ""


def _ts(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp() * 1000)


def parse_feed(raw: bytes, source: str, now_ms: int) -> list[dict[str, Any]]:
    """RSS 2.0 (and Atom) -> item dicts. Refuses DTDs/entities; never keeps the article body."""
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in raw.lower():
        raise ValueError("feed declares a DTD/entity; refused")
    root = ET.fromstring(raw)
    nodes = [e for e in root.iter() if _local(e.tag) in {"item", "entry"}]
    out = []
    for it in nodes:
        title = clean_text(_text(it, "title"), TITLE_MAX)
        link = _text(it, "link").strip()
        if not link:  # Atom <link href=...>
            le = _child(it, "link")
            link = (le.get("href") or "") if le is not None else ""
        url = clean_url(link)
        if not title or not url:
            continue
        guid = (_text(it, "guid") or _text(it, "id")).strip() or url
        summary = clean_text(_text(it, "description") or _text(it, "summary"), SUMMARY_MAX)
        pub = _ts(_text(it, "pubDate") or _text(it, "published") or _text(it, "updated")) or now_ms
        pub = min(pub, now_ms + 5 * 60_000)  # a clock-skewed future date never pins an item to the top
        coins = tag_coins(f"{title} {summary}")
        out.append({"source": source, "guid": guid[:500], "url": url[:1000], "title": title, "summary": summary,
                    "published_at": pub, "coins": coins, "important": is_important(title)})
    return out


def http_fetch(url: str, timeout: float = 10.0) -> bytes:
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.8"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError(f"feed larger than {MAX_BYTES} bytes")
    return raw


# ---- store -----------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, guid TEXT NOT NULL, url TEXT NOT NULL,
  title TEXT NOT NULL, summary TEXT NOT NULL, published_at INTEGER NOT NULL, fetched_at INTEGER NOT NULL,
  coins TEXT NOT NULL DEFAULT '', important INTEGER NOT NULL DEFAULT 0, focus INTEGER NOT NULL DEFAULT 0,
  UNIQUE (source, guid)
);
CREATE INDEX IF NOT EXISTS items_pub ON items (published_at DESC);
CREATE TABLE IF NOT EXISTS fetch_log (
  source TEXT PRIMARY KEY, last_attempt INTEGER, last_ok INTEGER, last_error TEXT, last_added INTEGER,
  last_items INTEGER, fails INTEGER NOT NULL DEFAULT 0
);
"""


class NewsStore:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else data_dir() / LEDGER_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add(self, items: list[dict[str, Any]], now_ms: int) -> int:
        with self._lock:
            before = self._db.total_changes
            self._db.execute("BEGIN")
            try:
                self._db.executemany(
                    "INSERT OR IGNORE INTO items(source, guid, url, title, summary, published_at, fetched_at, coins, important, focus)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(i["source"], i["guid"], i["url"], i["title"], i["summary"], int(i["published_at"]), now_ms,
                      "," + ",".join(i["coins"]) + "," if i["coins"] else "", 1 if i["important"] else 0,
                      1 if any(c in FOCUS for c in i["coins"]) else 0) for i in items],
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return self._db.total_changes - before

    def prune(self, now_ms: int) -> int:
        with self._lock:
            before = self._db.total_changes
            self._db.execute("DELETE FROM items WHERE published_at < ?", (now_ms - RETAIN_DAYS * 86_400_000,))
            self._db.execute("DELETE FROM items WHERE id NOT IN (SELECT id FROM items ORDER BY published_at DESC LIMIT ?)", (RETAIN_ROWS,))
            return self._db.total_changes - before

    def log(self, source: str, *, now_ms: int, ok: bool, error: str = "", added: int = 0, items: int = 0) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO fetch_log(source, fails) VALUES (?, 0)", (source,))
            if ok:
                self._db.execute("UPDATE fetch_log SET last_attempt=?, last_ok=?, last_error=NULL, last_added=?, last_items=?, fails=0 WHERE source=?",
                                 (now_ms, now_ms, added, items, source))
            else:
                self._db.execute("UPDATE fetch_log SET last_attempt=?, last_error=?, fails=fails+1 WHERE source=?", (now_ms, error[:200], source))

    def page(self, *, page: int = 1, size: int = 20, coin: Optional[str] = None, focus: bool = False,
             important: bool = False, source: Optional[str] = None) -> dict[str, Any]:
        where, args = [], []
        if coin:
            where.append("coins LIKE ?")
            args.append(f"%,{coin},%")
        if focus:
            where.append("coins != ''")
        if important:
            where.append("important = 1")
        if source:
            where.append("source = ?")
            args.append(source)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = int(self._db.execute(f"SELECT COUNT(*) FROM items{w}", args).fetchone()[0])
            rows = self._db.execute(f"SELECT * FROM items{w} ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
                                    (*args, size, (page - 1) * size)).fetchall()
            logs = {r["source"]: dict(r) for r in self._db.execute("SELECT * FROM fetch_log")}
            newest = self._db.execute("SELECT MAX(fetched_at) FROM items").fetchone()[0]
        items = [{"id": r["id"], "source": r["source"], "sourceName": SOURCES.get(r["source"], {}).get("name", r["source"]),
                  "title": r["title"], "summary": r["summary"], "url": r["url"], "publishedAt": r["published_at"],
                  "coins": [c for c in r["coins"].split(",") if c], "important": bool(r["important"])} for r in rows]
        return {"items": items, "page": page, "size": size, "total": total, "pages": max(1, -(-total // size)),
                "sources": _source_status(logs), "lastFetchedAt": newest}


def _source_status(logs: dict[str, dict]) -> list[dict[str, Any]]:
    out = []
    for sid in enabled_sources():
        s, lg = SOURCES[sid], logs.get(sid) or {}
        out.append({"id": sid, "name": s["name"], "home": s["home"], "lastOkAt": lg.get("last_ok"), "lastAttemptAt": lg.get("last_attempt"),
                    "ok": bool(lg.get("last_ok")) and not lg.get("last_error"), "fails": lg.get("fails") or 0,
                    # coarse reason only (no upstream bodies or internal paths)
                    "error": (lg.get("last_error") or "").split(":", 1)[0][:40] or None})
    return out


# ---- fetcher ---------------------------------------------------------------------------
FetchFn = Callable[[str], bytes]


class NewsFetcher:
    def __init__(self, store: NewsStore, *, fetch: FetchFn = http_fetch, now_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.store, self.fetch, self.now_ms = store, fetch, now_ms
        self._lock = threading.Lock()

    def tick(self) -> dict[str, Any]:
        """Fetch every enabled source once. Never raises."""
        if not self._lock.acquire(blocking=False):
            return {}
        out: dict[str, Any] = {}
        try:
            for sid in enabled_sources():
                now = self.now_ms()
                try:
                    items = parse_feed(self.fetch(SOURCES[sid]["url"]), sid, now)
                    if not items:
                        raise ValueError("empty: feed had no items")
                    added = self.store.add(items, now)
                    self.store.log(sid, now_ms=now, ok=True, added=added, items=len(items))
                    out[sid] = {"ok": True, "items": len(items), "added": added}
                except Exception as exc:  # network, HTTP, XML, disk: recorded, never propagated
                    msg = _reason(exc)
                    with _suppress():
                        self.store.log(sid, now_ms=now, ok=False, error=msg)
                    out[sid] = {"ok": False, "error": msg}
                    log.warning("news fetch %s failed: %s", sid, msg)
            with _suppress():
                out["pruned"] = self.store.prune(self.now_ms())
        finally:
            self._lock.release()
        return out


def _reason(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"http {exc.code}"
    if isinstance(exc, (URLError, socket.timeout, TimeoutError)):
        return "network: unreachable or timeout"
    if isinstance(exc, ET.ParseError):
        return "parse: invalid XML"
    return f"{type(exc).__name__}: {str(exc)[:120]}"


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if ev is not None:
            log.warning("news: %s", _reason(ev))
        return True


# ---- singletons / loop -------------------------------------------------------------------
_STORE: Optional[NewsStore] = None
_FETCHER: Optional[NewsFetcher] = None
_GUARD = threading.Lock()


def get_store() -> NewsStore:
    global _STORE
    with _GUARD:
        if _STORE is None:
            _STORE = NewsStore()
        return _STORE


def peek_store() -> Optional[NewsStore]:
    """The store if its file exists (the route never creates it)."""
    if _STORE is not None:
        return _STORE
    if not (data_dir() / LEDGER_NAME).exists():
        return None
    return get_store()


def reset_store(store: Optional[NewsStore] = None) -> None:
    global _STORE, _FETCHER
    with _GUARD:
        _STORE, _FETCHER = store, None


def get_fetcher() -> NewsFetcher:
    global _FETCHER
    st = get_store()
    with _GUARD:
        if _FETCHER is None or _FETCHER.store is not st:
            _FETCHER = NewsFetcher(st)
        return _FETCHER


async def run_loop() -> None:
    import asyncio

    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.to_thread(get_fetcher().tick)
        except Exception:
            log.exception("news loop")
        await asyncio.sleep(interval_sec())


def main(argv: Optional[list[str]] = None) -> int:
    """python -m app.news fetch | list"""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "fetch":
        print(json.dumps(get_fetcher().tick(), ensure_ascii=False))
        return 0
    if cmd == "list":
        d = get_store().page(size=10)
        print(json.dumps({"total": d["total"], "sources": d["sources"], "top": [(i["sourceName"], i["coins"], i["title"]) for i in d["items"]]},
                         ensure_ascii=False, indent=1))
        return 0
    print("usage: python -m app.news fetch | list")
    return 2
