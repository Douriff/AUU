"""空投雷达 quality rules: scam-phrase blocklist, same-activity matching, verification label.

Pure functions (no I/O) so they are cheap to test:

* ``blocked_reason``   – titles/summaries with "guaranteed returns" / "send first" style wording are
                         never stored or shown.
* ``same_activity``    – decides whether two headlines describe the same campaign (merge them into one
                         card with several sources).
* ``verification``     – ``confirmed`` when an official (exchange API / project blog) source is present
                         or at least two independent publishers carry the item; else ``unverified``.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# Fixed risk notice shown on every card / detail page (also returned by the API).
RISK_NOTICE = (
    "风险提示：空投、积分与代币活动常被用于诈骗和钓鱼。请只通过项目官方渠道核实；"
    "任何要求「先转账 / 先授权 / 提供助记词或私钥」的活动都不要参与。本站仅作信息聚合，不构成投资建议。"
)

# ---- scam / guaranteed-return phrases (case-insensitive; matched on title + summary only) -------------
BLOCKED_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (name, re.compile(rx, re.I))
    for name, rx in (
        ("保证收益", r"保证\s*(?:收益|盈利|回报|赚钱|获利)"),
        ("稳赚", r"稳赚|零风险|无风险|包赚|必赚"),
        ("先转账", r"先\s*(?:转账|打款|转币|充值|付款|转入|支付)"),
        ("send first", r"\bsend\s+(?:\S+\s+){0,4}?first\b(?!\s+(?:transaction|tx|message|email|post|tweet))|\bsend\s+(?:me\s+)?(?:\w+\s+){0,3}to\s+(?:receive|get|claim)\b|\bpay\s+first\b"),
        ("guaranteed", r"\bguarantee[sd]?\b"),
        ("risk-free", r"\brisk[- ]free\b|\bdouble\s+your\b|\b100\s*%\s*(?:profit|return|roi)\b|\bsure[- ]?profit\b"),
    )
)


def blocked_reason(*texts: Optional[str]) -> Optional[str]:
    """Name of the first blocked phrase group found in the given texts, else None."""
    blob = " ".join(t for t in texts if t)
    if not blob:
        return None
    for name, rx in BLOCKED_PATTERNS:
        if rx.search(blob):
            return name
    return None


# ---- same-activity matching ----------------------------------------------------------------------------
_WORD = re.compile(r"[A-Za-z0-9$][A-Za-z0-9.$'’_-]*|[\u4e00-\u9fff]+")
_STOP = frozenset(
    """a an the and or of for to in on at by with from into over under about as is are was were be been being it its this that
    these those will now new today more all your our their you we they his her up out off via per vs not no yes get got
    announces announcement announced announce introducing introduces launches launch launched launching update updates
    updated live official notice details detail join joins joined free share shares win prize pool trade trading spot
    token tokens coin coins campaign campaigns event events program programs reward rewards season phase round
    on-chain onchain crypto digital asset assets user users community holders holder early claim claims eligible
    eligibility how what why when who here there new-user complete full latest
    monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september
    october november december jan feb mar apr jun jul aug sep sept oct nov dec utc""".split()
)
# Capitalised/upper-case words that are not project names.
_GENERIC_ENT = frozenset(
    """usdt usdc usd eur btc eth sol bnb tge etf api nft nfts dex cex tvl ama faq utc p2p sec kyc ai us uk eu defi dao dapp
    web3 ceo cto q1 q2 q3 q4 2025 2026 2027 okx bybit binance coinbase kraken kucoin bitget gate htx mexc upbit
    coindesk cointelegraph the""".split()
)
# Exchange / publisher names must not make two different posts look like the same project.
_PUBLISHERS = frozenset("okx bybit binance coinbase kraken kucoin bitget gate htx mexc upbit coindesk cointelegraph".split())

_EVENT_CLASSES: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("airdrop", re.compile(r"\bair\s*drops?\b|空投|\bstakedrops?\b|\bretrodrops?\b|\bmegadrops?\b|\bhodler\s+airdrops?\b", re.I)),
    ("launch", re.compile(r"\bjumpstart\b|\blaunchpool\b|\blaunchpad\b|\btoken\s+splash\b|\btoken\s+generation\b|\bTGE\b", re.I)),
    ("points", re.compile(r"\bpoints?\b|积分", re.I)),
    ("claim", re.compile(r"\bclaim\b|领取", re.I)),
)
_TICKER_PAREN = re.compile(r"\(\s*\$?([A-Z][A-Z0-9]{1,9})\s*\)")
_TICKER_DOLLAR = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")
_PAIR = re.compile(r"\b([A-Z][A-Z0-9]{1,9})/(?:USDT|USDC|USD|BTC|ETH)\b")

SAME_WINDOW_MS = 14 * 86_400_000
JACCARD_MIN = 0.6


def event_class(text: str) -> str:
    for name, rx in _EVENT_CLASSES:
        if rx.search(text or ""):
            return name
    return ""


def _norm_token(w: str) -> str:
    return w.strip(".'’-_").lower()


def title_tokens(text: str) -> frozenset[str]:
    out = set()
    for w in _WORD.findall(text or ""):
        n = _norm_token(w)
        if len(n) < 2 or n in _STOP or n.isdigit():
            continue
        out.add(n)
    return frozenset(out)


def entities(text: str) -> frozenset[str]:
    """Project / ticker-like tokens: (TICK), $TICK, TICK/USDT, ALLCAPS words, Capitalised words >=4 chars."""
    t = text or ""
    ents: set[str] = set()
    for rx in (_TICKER_PAREN, _TICKER_DOLLAR, _PAIR):
        for m in rx.finditer(t):
            ents.add(m.group(1).lower())
    for w in _WORD.findall(t):
        if w.startswith("$"):
            continue
        n = _norm_token(w)
        if not n or n in _GENERIC_ENT or n in _STOP:
            continue
        if re.fullmatch(r"[A-Z][A-Z0-9]{1,9}", w):
            ents.add(n)
        elif re.fullmatch(r"[A-Z][A-Za-z0-9]{3,}", w) and not w.islower():
            ents.add(n)
    return frozenset(e for e in ents if e not in _GENERIC_ENT and e not in _PUBLISHERS and e not in _STOP)


def canonical_url(u: Optional[str]) -> str:
    u = (u or "").strip().split("#", 1)[0]
    base, _, q = u.partition("?")
    # drop tracking params only
    keep = [p for p in q.split("&") if p and not p.lower().startswith(("utm_", "ref=", "source=", "fbclid", "gclid"))]
    return (base.rstrip("/").lower() + (("?" + "&".join(keep)) if keep else "")) if base else ""


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def same_activity(
    a: dict[str, Any],
    b: dict[str, Any],
    *,
    window_ms: int = SAME_WINDOW_MS,
) -> bool:
    """True when two items (keys: title, summary, url, published_at) are the same campaign.

    Rules (any one is enough, always within ``window_ms`` of each other except for identical URL):
      1. identical canonical URL;
      2. title word-sets overlap >= 0.6 (Jaccard);
      3. they share a project/ticker entity AND the same event class (airdrop / launch / points / claim).
    Two posts from the same publisher are only merged by rule 1 or 2 (a publisher regularly runs several
    different campaigns in one week).
    """
    ua, ub = canonical_url(a.get("url")), canonical_url(b.get("url"))
    if ua and ua == ub:
        return True
    pa, pb = int(a.get("published_at") or 0), int(b.get("published_at") or 0)
    if pa and pb and abs(pa - pb) > window_ms:
        return False
    ta, tb = str(a.get("title") or ""), str(b.get("title") or "")
    if jaccard(title_tokens(ta), title_tokens(tb)) >= JACCARD_MIN:
        return True
    if a.get("source_id") and a.get("source_id") == b.get("source_id"):
        return False
    ea, eb = entities(ta), entities(tb)
    if not (ea & eb):
        return False
    ca = event_class(f"{ta} {a.get('summary') or ''}")
    cb = event_class(f"{tb} {b.get('summary') or ''}")
    return bool(ca) and ca == cb


def verification(sightings: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """``{label, official, sourceCount}`` from sightings (keys: source_id, kind in official|media|curated)."""
    pubs: set[str] = set()
    official = False
    for s in sightings:
        pubs.add(str(s.get("source_id") or s.get("source_name") or ""))
        if s.get("kind") == "official":
            official = True
    pubs.discard("")
    n = len(pubs)
    return {"label": "confirmed" if (official or n >= 2) else "unverified", "official": official, "sourceCount": n}
