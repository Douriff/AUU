"""行业动态: crypto industry headlines from free public RSS feeds (title + short summary + link only).

- Sources (no key, no account, no cost): CoinDesk and Cointelegraph public RSS. Each item keeps the
  source name and the original link; the full article is never fetched or stored.
- A background loop fetches every ``AUU_NEWS_INTERVAL_SEC`` (default 720 s = 12 min, clamped to
  300..3600) and writes ``news.sqlite`` in the data dir. Every failure is caught and recorded in
  ``fetch_log``; nothing here is read by the strategy, the ledgers, health or the guard, so a dead
  feed can never affect the main site.
- Each item is tagged with the strategy's 19 coins it mentions (BTC/ETH/SOL first), plus a rule-based
  ``important`` (要闻) flag for major events only (see ``importance``). The flag is recomputed for stored rows
  whenever the store opens, so a rule change applies to old items too.
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
# ---- 要闻 (important) rules ---------------------------------------------------------------
# Title-only, rule-based, deterministic. Only truly major events qualify (target ≈10–15 % of items on a normal
# news day, less on a quiet one). A title is 要闻 when it is not a roundup/opinion/speculation piece AND it hits
# one of these categories (``importance`` returns the category id):
#   etf        spot-ETF approval/rejection/launch, or ETF flows of at least ETF_FLOW_MIN_USD
#   regulation a regulator / court / law-enforcement body *taking* a hard action (approves, sues, charges,
#              bans, sanctions, arrests, seizes, signs/passes a law ...), or a crypto bill passing/being signed
#   hack       hack/exploit/theft at a top exchange, or of at least HACK_MIN_USD; top-exchange outage/halt
#   macro      central-bank rate decisions (Fed/FOMC/ECB/BOJ), CPI, US jobs report / unemployment rate
#   move       BTC/ETH/SOL crash/surge/all-time high, a single-day move ≥ MOVE_MIN_PCT, or liquidations ≥ LIQ_MIN_USD
#   whale      a top institution / treasury / government buying or selling ≥ WHALE_MIN_USD (or ≥ 1,000 BTC /
#              ≥ 50,000 ETH) of BTC/ETH/SOL
IMPORTANT_RULES = "v2"
ETF_FLOW_MIN_USD = 300e6
HACK_MIN_USD = 50e6
LIQ_MIN_USD = 1e9
WHALE_MIN_USD = 100e6
ETF_AGG_MIN_USD = 1e9
MOVE_MIN_PCT = {"BTC": 7.0, "ETH": 10.0, "SOL": 10.0}

_I = re.I
# Digests, live blogs, explainers, opinion and questions are never 要闻; neither is "may/might/could/would"
# speculation (lower-case only, so the month "May" is still allowed).
_EXCLUDE = re.compile(
    r"hodler.?s digest|what happened in crypto today|state of crypto|live updates?|\brecap\b|\bweekly\b|newsletter|"
    r"podcast|things to know|price (?:prediction|analysis)|\bopinion\b|\bexplained\b|\bhow to\b|\bcompared\b|"
    r"\bvs\.?(?=\s)|\?\s*$|\bwhy\b",
    _I,
)
_MODAL = re.compile(r"\b(?:may|might|could|would)\b")

_AMOUNT = re.compile(r"\$\s?(\d+(?:[.,]\d+)*)\s*(trillion|billion|million|thousand|tn|bn|[tbmk])?\b", _I)
_MULT = {"t": 1e12, "tn": 1e12, "trillion": 1e12, "b": 1e9, "bn": 1e9, "billion": 1e9, "m": 1e6, "million": 1e6,
         "k": 1e3, "thousand": 1e3}


def usd_amounts(title: str) -> list[float]:
    """Every "$..." amount in a title, in USD ("$103M" -> 1.03e8, "$1.5 billion" -> 1.5e9, "$87,000" -> 87000)."""
    out = []
    for num, unit in _AMOUNT.findall(title or ""):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        out.append(v * _MULT.get((unit or "").lower(), 1.0))
    return out


def _max_usd(title: str) -> float:
    return max(usd_amounts(title), default=0.0)


_COIN_QTY = re.compile(r"(\d+(?:[.,]\d+)*)\s*(k|thousand|million|m)?\s*(btc|bitcoin|eth|ether)\b", _I)


def _coin_qty(title: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for num, unit, coin in _COIN_QTY.findall(title or ""):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        v *= {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get((unit or "").lower(), 1.0)
        c = "BTC" if coin.lower() in {"btc", "bitcoin"} else "ETH"
        out[c] = max(out.get(c, 0.0), v)
    return out


_GAP = r"[^,;:|]{0,60}?"  # same clause: no comma/semicolon/colon between subject and verb
_ETF = re.compile(r"\betfs?\b", _I)
_ETF_DECISION = re.compile(r"\b(?:approv\w*|reject\w*|den(?:y|ies|ied)|green.?light\w*|launch(?:es|ed)?|debut\w*|"
                           r"begins? trading|starts? trading)\b", _I)
_ETF_SPECULATIVE = re.compile(r"\b(?:files?|filed|filing|applications?|proposals?|seeks?|amend\w*)\b", _I)
_FLOW = re.compile(r"\b(?:inflows?|outflows?|flows?|draws?|drew|take[sn]? in|pull(?:s|ed)?|shed\w*|bleed\w*|"
                   r"add(?:s|ed)?|lose|lost)\b", _I)
_RECORD = re.compile(r"\brecord\b", _I)
# flow aggregates: week / month / streak totals need ETF_AGG_MIN_USD; quarter / half-year / year recaps never count
_ETF_AGG = re.compile(r"\b(?:weeks?|weekly|months?|monthly|streaks?)\b", _I)
_ETF_RECAP = re.compile(r"\b(?:quarter|Q[1-4]|H[12]|half|years?|yearly|annual|YTD)\b(?!\s+high)", _I)

# acronyms that are also English words / pronouns are matched case-sensitively via (?-i:...)
_REGULATOR = (r"(?:(?-i:\bSEC\b)|\bCFTC\b|\bDOJ\b|Justice Department|Department of Justice|\bTreasury\b|\bOFAC\b|\bFinCEN\b|"
              r"Federal Reserve|\bOCC\b|\bFCA\b|\bESMA\b|\bPBOC\b|People.s Bank of China|\bCongress\b|\bSenate\b|"
              r"(?-i:\bHouse\b)|White House|\bTrump\b|\bcourt\b|\bjudge\b|\bjury\b|\bpolice\b|prosecutors?|\bFBI\b|\bIRS\b|"
              r"(?-i:\bEU\b)|European Commission|(?-i:\bMAS\b)|\bSFC\b|\bFSA\b|\bregulators?\b|\bgovernment\b)")
_REG_ACTION = (r"(?:approv(?:es|ed)|reject(?:s|ed)|den(?:ies|ied)|sues|sued|charg(?:es|ed)|fin(?:es|ed)|settles?|settled|"
               r"bans?|banned|sanction(?:s|ed)|arrest(?:s|ed)|seiz(?:es|ed)|indict(?:s|ed)|"
               r"raid(?:s|ed)|orders?|ordered|freez(?:es)|froze|halts?|halted|sentenc(?:es|ed)|"
               r"convict(?:s|ed)|drops? (?:case|lawsuit|charges)|dropped)")
_REG = re.compile(_REGULATOR + _GAP + r"\b" + _REG_ACTION + r"\b", _I)
# a law / executive order being signed or passed (only counts together with a crypto word)
_LAW = re.compile(r"\b(?:signs?|signed|pass(?:es|ed)|enacts?|enacted)\b" + _GAP + r"\b(?:into law|bill|act|law|executive order)\b", _I)
# the primary crypto regulators publishing binding rules / guidance / exemptions
_RULEMAKER = re.compile(r"(?:(?-i:\bSEC\b)|\bCFTC\b|\bOCC\b|Federal Reserve|\bTreasury\b|\bFCA\b|\bESMA\b|(?-i:\bMAS\b)|\bSFC\b|\bPBOC\b)"
                        + _GAP + r"\b(?:guidance|rules?|rulemaking|propos(?:es|ed|al)|framework|exemptions?|no-action|"
                        r"clears?|cleared|unveils?|issues?|issued|finaliz\w*|adopts?|adopted|withdraws?|rescinds?)\b", _I)
_REG_PASSIVE = re.compile(r"\b(?:charged|sued|fined|arrested|sentenced|indicted|banned|sanctioned|convicted|extradited)\s+"
                          r"(?:by|in|for|over|after)\b", _I)
_BILL = re.compile(r"(?:\b(?:GENIUS|CLARITY|FIT21|stablecoin|crypto|market structure)\b[^,;:]{0,20}\b(?:act|bill|law)\b)"
                   + _GAP + r"\b(?:pass(?:es|ed)|signed|clears?|cleared|becomes law|enacted|fails|failed)\b", _I)
_EXEC_ORDER = re.compile(r"\bexecutive order\b", _I)
_CRYPTO_WORD = re.compile(r"\b(?:crypto\w*|bitcoin|stablecoins?|digital assets?|GENIUS|CLARITY|FIT21|MiCA)\b", _I)

_TOP_EXCHANGE = (r"(?:Binance|Coinbase|\bOKX\b|Bybit|Kraken|Bitget|Upbit|Bitfinex|\bHTX\b|Huobi|KuCoin|Gate\.io|Gemini|"
                 r"Robinhood|Crypto\.com|Bithumb|Hyperliquid)")
_TOP_EXCHANGE_RX = re.compile(_TOP_EXCHANGE, _I)
_HACK = re.compile(r"\b(?:hack(?:s|ed|er|ers)?|exploit(?:s|ed|er)?|stolen|steal(?:s)?|stole|drain(?:s|ed)?|breach(?:es|ed)?|heist)\b", _I)
# follow-up stories (traced, recovered, blocked, resumed, lawsuits ...) are not a new incident
_HACK_FOLLOWUP = re.compile(r"\b(?:trac(?:es|ed)|recover\w*|returns?|returned|refund\w*|blocks?|blocked|resum\w*|sues?|sued|"
                            r"lawsuits?|probe|analysis|linked|losses)\b", _I)
_OUTAGE = re.compile(r"\b(?:outage|goes down|went down|offline|halts?|halted|suspends?|suspended|pauses?|paused|freez(?:es)|froze)\b"
                     + _GAP + r"\b(?:withdrawals?|deposits?|trading|services?|platform)\b|\boutage\b", _I)

_MACRO = re.compile(
    r"(?:\b(?:(?-i:Fed)|FOMC|Federal Reserve|Powell|ECB|BOJ|Bank of Japan)\b" + _GAP +
    r"\b(?:cuts?|hikes?|raises?|holds?|keeps?|leaves?|pauses?|decision|rate)\b)|"
    r"\b(?:rate (?:cut|hike)s?)\b" + _GAP + r"\b(?:(?-i:Fed)|FOMC|ECB|BOJ)\b|"
    r"\bCPI\b|\bconsumer price index\b|\bnonfarm\b|\bpayrolls?\b|\bjobs report\b|\bunemployment rate\b",
    _I,
)

_FOCUS_NAME = r"(?:\bbitcoin\b(?!\s+cash)|\bBTC\b|\bether(?:eum)?\b(?!\s+classic)|\bETH\b|\bsolana\b|\bSOL\b|\bcrypto(?:\s+market)?\b)"
_FOCUS_RX = {"BTC": re.compile(r"\bbitcoin\b(?!\s+cash)|\bBTC\b", _I), "ETH": re.compile(r"\bether(?:eum)?\b(?!\s+classic)|\bETH\b", _I),
             "SOL": re.compile(r"\bsolana\b|\bSOL\b", _I)}
_BIG_MOVE = re.compile(_FOCUS_NAME + _GAP + r"\b(?:crash(?:es|ed)?|plung(?:es|ed)|plummet(?:s|ed)?|tumbl(?:es|ed)|nosediv(?:es|ed)|"
                       r"soar(?:s|ed)?|skyrocket(?:s|ed)?|all.time high|record high|new high|\bATH\b)", _I)
_MOVE_VERB = (r"(?:up|down|falls?|fell|drops?|dropped|jumps?|jumped|rises?|rose|gains?|sinks?|sank|slides?|slid|"
              r"rall(?:ies|ied)|surg(?:es|ed)|dives?|dived|spikes?|spiked|tank(?:s|ed)?|loses|lost|soars?|soared|plung(?:es|ed))")
_PCT_MOVE = {c: re.compile("(?:" + rx.pattern + ")" + _GAP + r"\b" + _MOVE_VERB + r"\b" + _GAP + r"(?P<pct>\d+(?:\.\d+)?)\s?%", _I)
             for c, rx in _FOCUS_RX.items()}
_LONG_WINDOW = re.compile(r"\b(?:week|weeks|weekly|month|months|monthly|quarter|Q[1-4]|year|years|YTD|since|H[12])\b", _I)
_LIQ = re.compile(r"\bliquidat\w*\b", _I)

_WHALE = re.compile(r"(?:\bStrategy\b|MicroStrategy|Saylor|BlackRock|Fidelity|Tesla|Metaplanet|BitMine|Trump Media|Mt\.?\s?Gox|"
                    r"\bgovernment\b|Grayscale|Tether|sovereign|GameStop|Coinbase|Bhutan|El Salvador|(?-i:\bUS\b|\bU\.S\.)|China|"
                    r"Germany|(?-i:\bUK\b)|Harvard|Mubadala)" + _GAP +
                    r"\b(?:buys?|bought|acquir(?:es|ed)|purchas(?:es|ed)|adds?|added|adding|sells?|sold|selling|dumps?|dumped|"
                    r"offloads?|offloaded|liquidat(?:es|ed)|transfers?|transferred|moves?|moved)\b", _I)
_WHALE_NEG = re.compile(r"\b(?:buys|bought|sells|sold) no\b|\bno (?:bitcoin|btc|ether|eth)\b", _I)
_CRYPTO_ASSET = re.compile(r"\bbitcoin\b(?!\s+cash)|\bBTC\b|\bether(?:eum)?\b|\bETH\b|\bsolana\b|\bSOL\b", _I)


def _excluded(title: str) -> bool:
    return bool(_EXCLUDE.search(title) or _MODAL.search(title))


def importance(title: str) -> Optional[str]:
    """Category id when ``title`` is 要闻 (see the rule table above), else None."""
    t = _WS.sub(" ", title or "").strip()
    if not t or _excluded(t):
        return None
    usd = _max_usd(t)
    # etf
    if _ETF.search(t):
        if _ETF_DECISION.search(t) and not _ETF_SPECULATIVE.search(t):
            return "etf"
        if _FLOW.search(t) and not _ETF_RECAP.search(t):
            need = ETF_AGG_MIN_USD if _ETF_AGG.search(t) else ETF_FLOW_MIN_USD
            if usd >= need or _RECORD.search(t):
                return "etf"
    # hack / outage
    if _HACK.search(t) and not _HACK_FOLLOWUP.search(t) and (usd >= HACK_MIN_USD or _TOP_EXCHANGE_RX.search(t)):
        return "hack"
    if _TOP_EXCHANGE_RX.search(t) and _OUTAGE.search(t):
        return "hack"
    # regulation / enforcement
    if _REG.search(t) or _RULEMAKER.search(t) or _REG_PASSIVE.search(t) or _BILL.search(t):
        return "regulation"
    if (_EXEC_ORDER.search(t) or _LAW.search(t)) and _CRYPTO_WORD.search(t):
        return "regulation"
    # macro
    if _MACRO.search(t):
        return "macro"
    # BTC/ETH/SOL big move
    if _BIG_MOVE.search(t):
        return "move"
    if not _LONG_WINDOW.search(t):
        for coin, rx in _PCT_MOVE.items():
            m = rx.search(t)
            if m and float(m.group("pct")) >= MOVE_MIN_PCT[coin]:
                return "move"
    if _LIQ.search(t) and usd >= LIQ_MIN_USD:
        return "move"
    # top institution buying / selling
    if _WHALE.search(t) and _CRYPTO_ASSET.search(t) and not _WHALE_NEG.search(t):
        qty = _coin_qty(t)
        if usd >= WHALE_MIN_USD or qty.get("BTC", 0) >= 1000 or qty.get("ETH", 0) >= 50_000:
            return "whale"
    return None


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
    return importance(title) is not None


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
        with _suppress():
            self.retag()

    def retag(self) -> int:
        """Re-apply the current 要闻 rules to every stored row (rows keep whatever flag they were inserted with
        otherwise). Idempotent; returns the number of rows whose flag changed."""
        with self._lock:
            rows = self._db.execute("SELECT id, title, important FROM items").fetchall()
            upd = [(1 if is_important(r["title"]) else 0, r["id"]) for r in rows]
            upd = [(v, i) for (v, i), r in zip(upd, rows) if v != r["important"]]
            if upd:
                self._db.execute("BEGIN")
                try:
                    self._db.executemany("UPDATE items SET important=? WHERE id=?", upd)
                    self._db.execute("COMMIT")
                except Exception:
                    self._db.execute("ROLLBACK")
                    raise
            return len(upd)

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
