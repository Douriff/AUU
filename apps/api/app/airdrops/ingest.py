"""空投雷达 ingest: keyword-filtered public sources -> airdrops.sqlite (info only).

Two tiers (``FEED_SOURCES[...]["tier"]``):

* ``official`` – exchange announcement APIs (OKX ``/api/v5/support/announcements``, Bybit
  ``/v5/announcements/index``: documented, public, key-less endpoints) and project / exchange
  blog RSS feeds that the sites themselves advertise. Shown as 已确认. Binance is NOT fetched
  (its only key-less endpoint is disallowed by robots.txt, the official one needs an API key).
* ``media`` – CoinDesk / Cointelegraph public RSS (reuses ``app.news`` parse/fetch).

Only title, a short summary (<= 220 chars) and the article link are kept; article bodies are never
fetched or stored. Failures are per-source (logged, backed off, old rows kept) and never reach the
API. Curated seed rows are never wiped. Standby nodes (``AUU_ROLE=standby``) never fetch.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from app.airdrops import CATEGORIES, AirdropStore, get_store, quality

log = logging.getLogger("auu.airdrops.ingest")

EXCHANGE_MIN_INTERVAL = 1800  # exchange APIs: 1 request / 30 min / source (OKX allows 5 req / 2 s)
BLOG_MIN_INTERVAL = 1800

# id -> definition. ``hosts``: link host suffixes accepted for official sources (a feed cannot
# smuggle a foreign link into an "official" card). ``min_interval``: seconds between requests.
FEED_SOURCES: dict[str, dict[str, Any]] = {
    # --- media (existing) ---
    "coindesk": {
        "name": "CoinDesk", "home": "https://www.coindesk.com/", "url": "https://www.coindesk.com/arc/outboundfeeds/rss",
        "kind": "rss", "tier": "media", "min_interval": 0,
    },
    "cointelegraph": {
        "name": "Cointelegraph", "home": "https://cointelegraph.com/", "url": "https://cointelegraph.com/rss",
        "kind": "rss", "tier": "media", "min_interval": 0,
    },
    # --- official: exchange announcement APIs ---
    "okx": {
        "name": "OKX 公告", "project": "OKX", "home": "https://www.okx.com/help/category/announcements",
        "url": "https://www.okx.com/api/v5/support/announcements?page=1",
        "kind": "okx", "tier": "official", "min_interval": EXCHANGE_MIN_INTERVAL, "hosts": ("okx.com",),
        "keywords": "exchange",
    },
    "bybit": {
        "name": "Bybit 公告", "project": "Bybit", "home": "https://announcements.bybit.com/",
        "url": "https://api.bybit.com/v5/announcements/index?locale=en-US&limit=50&page=1",
        "kind": "bybit", "tier": "official", "min_interval": EXCHANGE_MIN_INTERVAL, "hosts": ("bybit.com",),
        "keywords": "exchange",
    },
    # --- official: project / exchange blogs (RSS the sites advertise) ---
    "ethereum": {
        "name": "Ethereum Foundation Blog", "project": "Ethereum", "home": "https://blog.ethereum.org/",
        "url": "https://blog.ethereum.org/en/feed.xml", "kind": "rss", "tier": "official",
        "min_interval": BLOG_MIN_INTERVAL, "hosts": ("ethereum.org",),
    },
    "solana": {
        "name": "Solana News", "project": "Solana", "home": "https://solana.com/news",
        "url": "https://solana.com/news/rss.xml", "kind": "rss", "tier": "official",
        "min_interval": BLOG_MIN_INTERVAL, "hosts": ("solana.com",),
    },
    "arbitrum": {
        "name": "Arbitrum Blog", "project": "Arbitrum", "home": "https://blog.arbitrum.io/",
        "url": "https://blog.arbitrum.io/rss/", "kind": "rss", "tier": "official",
        "min_interval": BLOG_MIN_INTERVAL, "hosts": ("arbitrum.io",),
    },
    "optimism": {
        "name": "Optimism Blog", "project": "Optimism", "home": "https://optimism.mirror.xyz/",
        "url": "https://optimism.mirror.xyz/feed/atom", "kind": "rss", "tier": "official",
        "min_interval": BLOG_MIN_INTERVAL, "hosts": ("mirror.xyz", "optimism.io"),
    },
    "eigen": {
        "name": "Eigen Labs Blog", "project": "EigenLayer", "home": "https://www.eigenlabs.org/blog/",
        "url": "https://www.eigenlabs.org/blog/rss/", "kind": "rss", "tier": "official",
        "min_interval": BLOG_MIN_INTERVAL, "hosts": ("eigenlabs.org", "eigencloud.xyz", "eigenlayer.xyz"),
    },
    "lido": {
        "name": "Lido Blog", "project": "Lido", "home": "https://blog.lido.fi/",
        "url": "https://blog.lido.fi/rss/", "kind": "rss", "tier": "official",
        "min_interval": BLOG_MIN_INTERVAL, "hosts": ("lido.fi",),
    },
    "sui": {
        "name": "Sui Blog", "project": "Sui", "home": "https://www.sui.io/blog",
        "url": "https://www.sui.io/blog/rss.xml", "kind": "rss", "tier": "official",
        "min_interval": BLOG_MIN_INTERVAL, "hosts": ("sui.io",),
    },
    "layerzero": {
        "name": "LayerZero (Medium)", "project": "LayerZero", "home": "https://medium.com/layerzero-official",
        "url": "https://medium.com/feed/layerzero-official", "kind": "rss", "tier": "official",
        "min_interval": BLOG_MIN_INTERVAL, "hosts": ("medium.com", "layerzero.network", "layerzero.foundation"),
    },
    "kraken": {
        "name": "Kraken Blog", "project": "Kraken", "home": "https://blog.kraken.com/",
        "url": "https://blog.kraken.com/feed", "kind": "rss", "tier": "official",
        "min_interval": BLOG_MIN_INTERVAL, "hosts": ("kraken.com",),
    },
}

# Title/summary must match at least one (case-insensitive) to be kept.
AIRDROP_KEYWORDS = re.compile(
    r"\bairdrops?\b|空投|\bstakedrops?\b|\bretrodrops?\b|\btoken\s+claim\b|\bclaim\s+portal\b|"
    r"\bpoints?\s+program\b|\bholder\s+rewards?\b|\btoken\s+generation\b|\bTGE\b|"
    r"\bcommunity\s+(?:rewards?|allocation|drop)\b|\bair\s*drop\b",
    re.I,
)

# Exchange announcement streams are mostly listings/maintenance; keep launch-style campaigns too.
EXCHANGE_KEYWORDS = re.compile(
    AIRDROP_KEYWORDS.pattern + r"|\bjumpstart\b|\blaunchpool\b|\blaunchpad\b|\btoken\s+splash\b|\bmegadrops?\b|\bhodler\b",
    re.I,
)
SUMMARY_SHORT = 220
MAX_AGE_DAYS = 90

MEME_HINT = re.compile(
    r"\bpump\.?fun\b|\bbonk\b|\bwif\b|\bdoge(?:coin)?\b|\bpepe\b|\bmeme(?:coin)?s?\b|"
    r"\bshib\b|\bfartcoin\b|\btrump\b.*\b(?:meme|token)\b|\bletsbonk\b",
    re.I,
)
MAJOR_HINT = re.compile(
    r"\blayerzero\b|\beigen(?:layer)?\b|\bmonad\b|\bzk(?:sync)?\b|\bstarknet\b|\boptimism\b|"
    r"\barbitrum\b|\bbase\b|\bpolygon\b|\bsolana\b.*\b(?:foundation|labs)\b|\betf\b|"
    r"\bblackrock\b|\bsec\b|\bjupiter\b",
    re.I,
)

USER_AGENT = "AUUTRADE-airdrops/2.0 (+https://auutrade.com; headlines + links only, low frequency)"
RETAIN_INGESTED_DAYS = 90
RETAIN_INGESTED_ROWS = 2000

FetchFn = Callable[[str], bytes]


def ingest_enabled() -> bool:
    """Ingest follows AUU_AIRDROPS unless AUU_AIRDROPS_INGEST is set explicitly; always off on a standby."""
    from app.role import is_standby

    if is_standby():  # the primary fetches; a standby never duplicates the requests (AUU_ROLE=standby)
        return False
    raw = os.getenv("AUU_AIRDROPS_INGEST")
    if raw is None:
        from app.airdrops import airdrops_enabled

        if os.getenv("AUU_TEST", "") == "1":
            return False
        return airdrops_enabled()
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def interval_sec() -> int:
    try:
        v = int(os.getenv("AUU_AIRDROPS_INTERVAL_SEC", "") or 900)
    except ValueError:
        v = 900
    return max(300, min(3600, v))


def enabled_feeds() -> list[str]:
    raw = (os.getenv("AUU_AIRDROPS_FEEDS") or "").strip()
    if not raw:
        return list(FEED_SOURCES)
    return [s for s in (x.strip().lower() for x in raw.split(",")) if s in FEED_SOURCES]


def matches_airdrop(title: str, summary: str = "", *, exchange: bool = False) -> bool:
    text = f"{title or ''} {summary or ''}"
    return bool((EXCHANGE_KEYWORDS if exchange else AIRDROP_KEYWORDS).search(text))


def guess_category(title: str, summary: str = "") -> str:
    text = f"{title or ''} {summary or ''}"
    if MEME_HINT.search(text):
        return "meme"
    if MAJOR_HINT.search(text):
        return "major"
    return "community"


def guess_project(title: str) -> str:
    """Best-effort project label; falls back to 行业资讯."""
    t = (title or "").strip()
    if not t:
        return "行业资讯"
    # "Project: rest" / "Project — rest" / "Project - rest"
    for sep in (":", " — ", " – ", " - "):
        if sep in t:
            left = t.split(sep, 1)[0].strip()
            if 1 < len(left) <= 60 and not AIRDROP_KEYWORDS.fullmatch(left):
                return left[:120]
    # Leading $TICKER or ALLCAPS token
    m = re.match(r"^\$?([A-Z]{2,12})\b", t)
    if m:
        return m.group(1)
    # First 1–3 capitalized words before airdrop/etc.
    m = re.match(
        r"^((?:[A-Z][\w&.]*(?:\s+[A-Z][\w&.]*){0,2}))\b.{0,40}?\b(?:airdrop|stakedrop|TGE|points)\b",
        t,
    )
    if m:
        return m.group(1)[:120]
    return "行业资讯"


def ingested_id(source_id: str, url: str, guid: str = "") -> str:
    raw = (guid or url or "").strip() or url
    h = hashlib.sha256(f"{source_id}|{raw}".encode("utf-8")).hexdigest()[:20]
    return f"ing-{source_id[:12]}-{h}"


def article_link(item: dict[str, Any]) -> str:
    """The item's own article URL: <link>, else a http(s) <guid> permalink. Never the
    feed endpoint itself (that opens raw XML for 查看原文)."""
    from app.airdrops import is_feed_url

    for cand in (item.get("url"), item.get("guid")):
        c = (str(cand or "")).strip()
        if c.startswith(("http://", "https://")) and not is_feed_url(c):
            return c
    return ""


_TRACKING_PARAMS = ("utm_", "ref", "source", "fbclid", "gclid", "mc_", "_hs")


def strip_tracking(url: str) -> str:
    """Drop tracking query params (Medium adds ?source=rss-...) but keep the path and other params."""
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith(_TRACKING_PARAMS)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(h == d or h.endswith("." + d) for d in hosts)


def short_summary(text: str, limit: int = SUMMARY_SHORT) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    if len(t) <= limit:
        return t
    return t[: limit - 1].rstrip() + "…"


def feed_item_to_row(
    item: dict[str, Any],
    *,
    source_id: str,
    source_name: str,
    now_ms: int,
    meta: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Feed/API item -> store row, or None (no link, off-topic, scam wording, too old, foreign host)."""
    meta = meta or FEED_SOURCES.get(source_id) or {}
    tier = meta.get("tier", "media")
    title = (item.get("title") or "").strip()
    summary = short_summary(item.get("summary") or "")
    url = article_link(item)
    if not title or not url:
        return None
    if tier == "official":
        url = strip_tracking(url)
        if not url.startswith("https://") or not host_allowed(url, tuple(meta.get("hosts") or ())):
            return None
    if not matches_airdrop(title, summary, exchange=meta.get("keywords") == "exchange"):
        return None
    if quality.blocked_reason(title, summary):
        return None
    pub = int(item.get("published_at") or now_ms)
    if now_ms - pub > MAX_AGE_DAYS * 86_400_000:
        return None
    cat = guess_category(title, summary)
    if cat not in CATEGORIES:
        cat = "community"
    tags = ["ingested", "rss" if meta.get("kind", "rss") == "rss" else "api", source_id]
    if tier == "official":
        tags.append("official")
    if cat == "meme":
        tags.append("meme")
    project = (meta.get("project") if tier == "official" else None) or guess_project(title)
    status = item.get("status") if item.get("status") in {"active", "upcoming", "ended"} else "active"
    return {
        "id": ingested_id(source_id, url, str(item.get("guid") or "")),
        "title": title[:240],
        "project_name": project,
        "category": cat,
        "source_name": source_name[:120],
        "source_url": url[:1000],
        "summary": summary,
        "chains": "",
        "status": status,
        "published_at": pub,
        "updated_at": now_ms,
        "external_url": url[:1000],
        "tags": ",".join(tags),
        "origin": "ingested",
        "source_id": source_id,
        "kind": "official" if tier == "official" else "media",
    }


# ---- exchange announcement APIs (documented public endpoints) -> item dicts like app.news.parse_feed ----
def parse_okx(raw: bytes, now_ms: int) -> list[dict[str, Any]]:
    d = json.loads(raw.decode("utf-8", "replace"))
    if str(d.get("code")) != "0":
        raise ValueError(f"okx code {d.get('code')}")
    out = []
    for page in d.get("data") or []:
        for a in page.get("details") or []:
            title = str(a.get("title") or "").strip()
            url = str(a.get("url") or "").strip()
            try:
                pub = int(a.get("pTime") or 0)
            except (TypeError, ValueError):
                pub = 0
            if not title or not url or pub <= 0:
                continue
            out.append({"title": title, "url": url, "guid": url, "summary": "", "published_at": min(pub, now_ms + 300_000),
                        "ann_type": str(a.get("annType") or "")})
    return out


def parse_bybit(raw: bytes, now_ms: int) -> list[dict[str, Any]]:
    d = json.loads(raw.decode("utf-8", "replace"))
    if int(d.get("retCode", -1)) != 0:
        raise ValueError(f"bybit retCode {d.get('retCode')}")
    out = []
    for a in (d.get("result") or {}).get("list") or []:
        title = str(a.get("title") or "").strip()
        url = str(a.get("url") or "").strip()
        pub = int(a.get("publishTime") or a.get("dateTimestamp") or 0)
        if not title or not url or pub <= 0:
            continue
        desc = str(a.get("description") or "").strip()
        if desc == title or desc.lower().startswith("disclaimer"):
            desc = ""
        status = "active"
        if (a.get("type") or {}).get("key") == "latest_activities":
            st, en = int(a.get("startDateTimestamp") or 0), int(a.get("endDateTimestamp") or 0)
            if st and st > now_ms:
                status = "upcoming"
            elif en and en < now_ms:
                status = "ended"
        out.append({"title": title, "url": url, "guid": url, "summary": desc, "published_at": min(pub, now_ms + 300_000),
                    "status": status})
    return out


def http_fetch_ex(url: str, *, accept: str = "*/*", timeout: float = 10.0, max_bytes: int = 3_000_000) -> bytes:
    """Plain GET with our own User-Agent (no cookies, no auth, no redirects to other schemes)."""
    from urllib.request import Request, urlopen

    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"response larger than {max_bytes} bytes")
    return raw


class AirdropFetcher:
    def __init__(
        self,
        store: AirdropStore,
        *,
        fetch: Optional[FetchFn] = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.store = store
        self._injected = fetch
        self.now_ms = now_ms
        self._lock = threading.Lock()

    def fetch(self, meta: dict[str, Any]) -> bytes:
        if self._injected is not None:
            return self._injected(meta["url"])
        accept = "application/json" if meta.get("kind") in {"okx", "bybit"} else "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8"
        return http_fetch_ex(meta["url"], accept=accept)

    def _parse(self, meta: dict[str, Any], sid: str, raw: bytes, now: int) -> list[dict[str, Any]]:
        kind = meta.get("kind", "rss")
        if kind == "okx":
            return parse_okx(raw, now)
        if kind == "bybit":
            return parse_bybit(raw, now)
        from app.news import parse_feed

        return parse_feed(raw, sid, now)

    def _due(self, sid: str, meta: dict[str, Any], now: int) -> bool:
        """Per-source politeness: min interval, doubling (up to x16, 4 h cap) after consecutive failures."""
        base = int(meta.get("min_interval") or 0)
        lg = self.store.fetch_state(sid)
        if not lg or not lg.get("last_attempt"):
            return True
        fails = int(lg.get("fails") or 0)
        wait = base * (2 ** min(fails, 4)) if fails else base
        if fails:
            wait = max(wait, 300)
        wait = min(wait, 4 * 3600) if base else wait
        return now - int(lg["last_attempt"]) >= wait * 1000

    def tick(self, *, force: bool = False) -> dict[str, Any]:
        """Fetch every enabled, due source once. Never raises. A standby node does nothing."""
        from app.role import is_standby

        if is_standby():
            return {"standby": True}
        if not self._lock.acquire(blocking=False):
            return {}
        out: dict[str, Any] = {}
        try:
            for sid in enabled_feeds():
                meta = FEED_SOURCES[sid]
                now = self.now_ms()
                try:
                    if not force and not self._due(sid, meta, now):
                        out[sid] = {"ok": True, "skipped": "interval"}
                        continue
                    raw = self.fetch(meta)
                    parsed = self._parse(meta, sid, raw, now)
                    rows = []
                    blocked_pre = 0
                    ex = meta.get("keywords") == "exchange"
                    for it in parsed:
                        if matches_airdrop(it.get("title") or "", it.get("summary") or "", exchange=ex) and quality.blocked_reason(
                            it.get("title"), it.get("summary")
                        ):
                            blocked_pre += 1
                        row = feed_item_to_row(it, source_id=sid, source_name=meta["name"], now_ms=now, meta=meta)
                        if row:
                            rows.append(row)
                    res = self.store.merge_ingested(rows)
                    self.store.log_fetch(sid, now_ms=now, ok=True, added=res["added"], items=len(rows))
                    out[sid] = {"ok": True, "matched": len(rows), "added": res["added"], "merged": res["merged"],
                                "skipped": res["skipped"] + res["merged"], "blocked": res["blocked"] + blocked_pre, "feedItems": len(parsed)}
                except Exception as exc:
                    msg = _reason(exc)
                    try:
                        self.store.log_fetch(sid, now_ms=now, ok=False, error=msg)
                    except Exception:
                        pass
                    out[sid] = {"ok": False, "error": msg}
                    log.warning("airdrops ingest %s failed (old rows kept, backing off): %s", sid, msg)
            try:
                out["pruned"] = self.store.prune_ingested(self.now_ms())
            except Exception as exc:
                log.warning("airdrops prune: %s", exc)
        finally:
            self._lock.release()
        return out


def _reason(exc: BaseException) -> str:
    from urllib.error import HTTPError, URLError
    import socket
    from xml.etree import ElementTree as ET

    if isinstance(exc, HTTPError):
        return f"http {exc.code}"
    if isinstance(exc, (URLError, socket.timeout, TimeoutError)):
        return "network: unreachable or timeout"
    if isinstance(exc, ET.ParseError):
        return "parse: invalid XML"
    if isinstance(exc, json.JSONDecodeError):
        return "parse: invalid JSON"
    return f"{type(exc).__name__}: {str(exc)[:120]}"


_FETCHER: Optional[AirdropFetcher] = None
_FG = threading.Lock()


def get_fetcher() -> AirdropFetcher:
    global _FETCHER
    st = get_store()
    with _FG:
        if _FETCHER is None or _FETCHER.store is not st:
            _FETCHER = AirdropFetcher(st)
        return _FETCHER


def reset_fetcher() -> None:
    global _FETCHER
    with _FG:
        _FETCHER = None


async def run_loop() -> None:
    import asyncio

    await asyncio.sleep(35)
    while True:
        try:
            await asyncio.to_thread(get_fetcher().tick)
        except Exception:
            log.exception("airdrops ingest loop")
        await asyncio.sleep(interval_sec())
