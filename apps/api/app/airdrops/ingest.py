"""空投雷达 RSS ingest: keyword-filtered public feeds -> airdrops.sqlite (info only).

Reuses ``app.news`` RSS parse/fetch (CoinDesk / Cointelegraph public outbound feeds already
used by 行业动态). Never fetches article HTML bodies. Failures are per-source and never
propagate to the API. Curated seed rows are never wiped; ingest upserts by id and skips
URLs already present.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from app.airdrops import CATEGORIES, AirdropStore, get_store

log = logging.getLogger("auu.airdrops.ingest")

# Same public feeds as app.news (official RSS; titles+summaries only).
FEED_SOURCES: dict[str, dict[str, str]] = {
    "coindesk": {
        "name": "CoinDesk",
        "home": "https://www.coindesk.com/",
        "url": "https://www.coindesk.com/arc/outboundfeeds/rss",
    },
    "cointelegraph": {
        "name": "Cointelegraph",
        "home": "https://cointelegraph.com/",
        "url": "https://cointelegraph.com/rss",
    },
}

# Title/summary must match at least one (case-insensitive) to be kept.
AIRDROP_KEYWORDS = re.compile(
    r"\bairdrops?\b|空投|\bstakedrops?\b|\bretrodrops?\b|\btoken\s+claim\b|\bclaim\s+portal\b|"
    r"\bpoints?\s+program\b|\bholder\s+rewards?\b|\btoken\s+generation\b|\bTGE\b|"
    r"\bcommunity\s+(?:rewards?|allocation|drop)\b|\bair\s*drop\b",
    re.I,
)

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

USER_AGENT = "AUUTRADE-airdrops/1.0 (+https://auutrade.com; RSS reader, headlines only)"
RETAIN_INGESTED_DAYS = 90
RETAIN_INGESTED_ROWS = 2000

FetchFn = Callable[[str], bytes]


def ingest_enabled() -> bool:
    """Ingest follows AUU_AIRDROPS unless AUU_AIRDROPS_INGEST is set explicitly."""
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


def matches_airdrop(title: str, summary: str = "") -> bool:
    text = f"{title or ''} {summary or ''}"
    return bool(AIRDROP_KEYWORDS.search(text))


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


def feed_item_to_row(item: dict[str, Any], *, source_id: str, source_name: str, now_ms: int) -> Optional[dict[str, Any]]:
    title = (item.get("title") or "").strip()
    summary = (item.get("summary") or "").strip()
    url = article_link(item)
    if not title or not url or not matches_airdrop(title, summary):
        return None
    cat = guess_category(title, summary)
    if cat not in CATEGORIES:
        cat = "community"
    pub = int(item.get("published_at") or now_ms)
    tags = ["ingested", "rss", source_id]
    if cat == "meme":
        tags.append("meme")
    return {
        "id": ingested_id(source_id, url, str(item.get("guid") or "")),
        "title": title[:240],
        "project_name": guess_project(title),
        "category": cat,
        "source_name": source_name[:120],
        "source_url": url[:1000],
        "summary": summary[:800],
        "chains": "",
        "status": "active",
        "published_at": pub,
        "updated_at": now_ms,
        "external_url": url[:1000],
        "tags": ",".join(tags),
        "origin": "ingested",
    }


class AirdropFetcher:
    def __init__(
        self,
        store: AirdropStore,
        *,
        fetch: Optional[FetchFn] = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        from app.news import http_fetch

        self.store = store
        self.fetch = fetch or http_fetch
        self.now_ms = now_ms
        self._lock = threading.Lock()

    def tick(self) -> dict[str, Any]:
        """Fetch every enabled feed once. Never raises."""
        if not self._lock.acquire(blocking=False):
            return {}
        out: dict[str, Any] = {}
        try:
            from app.news import parse_feed

            for sid in enabled_feeds():
                meta = FEED_SOURCES[sid]
                now = self.now_ms()
                try:
                    raw = self.fetch(meta["url"])
                    parsed = parse_feed(raw, sid, now)
                    rows = []
                    for it in parsed:
                        row = feed_item_to_row(it, source_id=sid, source_name=meta["name"], now_ms=now)
                        if row:
                            rows.append(row)
                    added, skipped = self.store.upsert_ingested(rows)
                    self.store.log_fetch(sid, now_ms=now, ok=True, added=added, items=len(rows))
                    out[sid] = {"ok": True, "matched": len(rows), "added": added, "skipped": skipped, "feedItems": len(parsed)}
                except Exception as exc:
                    msg = _reason(exc)
                    try:
                        self.store.log_fetch(sid, now_ms=now, ok=False, error=msg)
                    except Exception:
                        pass
                    out[sid] = {"ok": False, "error": msg}
                    log.warning("airdrops ingest %s failed: %s", sid, msg)
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
