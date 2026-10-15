"""空投雷达: curated seed + RSS-ingested airdrop / points-program headlines (info only).

- Curated cold-start: ``seed.json`` (manual attribution). Ongoing: keyword-filtered public RSS
  (see ``app.airdrops.ingest``), reusing ``app.news`` parse/fetch. No HTML scraping; no API keys.
- Rows live in ``airdrops.sqlite``. Ingest upserts without wiping curated rows; dedup by id and
  source_url. Login required. Never touches points, trading, or withdraw.
- Off switch: ``AUU_AIRDROPS=off``. Ingest: ``AUU_AIRDROPS_INGEST`` (default follows airdrops,
  off under AUU_TEST=1).
- v2 (official layer + quality rules): ``sightings`` table keeps every source that reported an
  item; same campaign from several sources is merged into one card (``app.airdrops.quality``);
  ``verification`` = confirmed (official source present or >=2 independent publishers) / unverified;
  scam-phrase items are never stored. Standby nodes never fetch (``app.role``).
"""
from __future__ import annotations

import json
import re
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.airdrops import quality
from app.data_paths import data_dir

log = logging.getLogger("auu.airdrops")

LEDGER_NAME = "airdrops.sqlite"
SEED_PATH = Path(__file__).resolve().parent / "seed.json"
CATEGORIES = ("major", "community", "meme")
STATUSES = ("active", "upcoming", "ended")
TITLE_MAX = 240
SUMMARY_MAX = 800
SOURCE_MAX = 120
URL_MAX = 1000
TAG_MAX = 32
CHAIN_MAX = 32

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  project_name TEXT NOT NULL,
  category TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_url TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  chains TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  published_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  external_url TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  seed_hash TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'curated'
);
CREATE INDEX IF NOT EXISTS airdrops_cat ON items (category);
CREATE INDEX IF NOT EXISTS airdrops_status ON items (status);
CREATE INDEX IF NOT EXISTS airdrops_pub ON items (published_at DESC);
CREATE TABLE IF NOT EXISTS sightings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'media',
  url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  published_at INTEGER NOT NULL DEFAULT 0,
  UNIQUE (item_id, source_id, url)
);
CREATE INDEX IF NOT EXISTS sightings_item ON sightings (item_id);
CREATE INDEX IF NOT EXISTS sightings_url ON sightings (url);
CREATE INDEX IF NOT EXISTS sightings_pub ON sightings (published_at);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fetch_log (
  source TEXT PRIMARY KEY, last_attempt INTEGER, last_ok INTEGER, last_error TEXT, last_added INTEGER,
  last_items INTEGER, fails INTEGER NOT NULL DEFAULT 0
);
"""


def airdrops_enabled() -> bool:
    raw = os.getenv("AUU_AIRDROPS")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _parse_ts(raw: Any) -> int:
    """ISO-8601 or epoch-ms -> epoch ms. Naive values are treated as UTC."""
    if isinstance(raw, (int, float)):
        v = int(raw)
        return v if v > 10_000_000_000 else v * 1000
    s = str(raw or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return _parse_ts(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _clean_url(u: Optional[str]) -> str:
    u = (u or "").strip()
    if not u.startswith(("http://", "https://")):
        return ""
    return u[:URL_MAX]


# Known RSS/Atom feed endpoints -> the publisher homepage. A feed URL is never a valid
# "查看原文" target (it opens raw XML), so these are rewritten/hidden everywhere.
FEED_URL_HOMES: dict[str, str] = {
    "https://www.coindesk.com/arc/outboundfeeds/rss": "https://www.coindesk.com/",
    "https://cointelegraph.com/rss": "https://cointelegraph.com/",
}
_FEED_PATH = re.compile(r"(?:/rss(?:\.xml)?|/feed(?:\.xml)?|/atom(?:\.xml)?|/outboundfeeds/[^?#]*|\.rss|\.xml)/?$", re.I)


def _norm_feed_key(u: str) -> str:
    return (u or "").strip().split("#", 1)[0].split("?", 1)[0].rstrip("/").lower()


def is_feed_url(u: Optional[str]) -> bool:
    """True for a known RSS feed URL or an obvious feed endpoint path."""
    k = _norm_feed_key(u or "")
    if not k:
        return False
    if k in {_norm_feed_key(x) for x in FEED_URL_HOMES}:
        return True
    try:
        from urllib.parse import urlsplit

        path = urlsplit(k).path
    except ValueError:
        return False
    return bool(_FEED_PATH.search(path))


def feed_home(u: Optional[str]) -> str:
    k = _norm_feed_key(u or "")
    for f, home in FEED_URL_HOMES.items():
        if _norm_feed_key(f) == k:
            return home
    return ""


def _article_url(u: Optional[str]) -> str:
    """Cleaned URL, or '' when it is a feed endpoint."""
    c = _clean_url(u)
    return "" if is_feed_url(c) else c


def _csv_list(vals: Any, *, limit: int, max_items: int = 12) -> str:
    if not vals:
        return ""
    if isinstance(vals, str):
        parts = [p.strip() for p in vals.split(",") if p.strip()]
    else:
        parts = [str(p).strip() for p in vals if str(p).strip()]
    out = []
    for p in parts[:max_items]:
        out.append(p[:limit])
    return ",".join(out)


def normalize_seed_item(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Validate one seed row; return None if unusable."""
    iid = str(raw.get("id") or "").strip()[:80]
    title = str(raw.get("title") or "").strip()[:TITLE_MAX]
    project = str(raw.get("project_name") or "").strip()[:120]
    category = str(raw.get("category") or "").strip().lower()
    source_name = str(raw.get("source_name") or "").strip()[:SOURCE_MAX]
    status = str(raw.get("status") or "active").strip().lower()
    if not iid or not title or not project or not source_name:
        return None
    if category not in CATEGORIES or status not in STATUSES:
        return None
    source_url = _article_url(raw.get("source_url")) or feed_home(raw.get("source_url"))
    external_url = _article_url(raw.get("external_url")) or source_url
    summary = str(raw.get("summary") or "").strip()[:SUMMARY_MAX]
    pub = _parse_ts(raw.get("published_at"))
    upd = _parse_ts(raw.get("updated_at")) or pub
    if pub <= 0:
        return None
    if quality.blocked_reason(title, summary):
        log.warning("airdrops seed %s dropped: blocked phrase (%s)", iid, quality.blocked_reason(title, summary))
        return None
    return {
        "official": bool(raw.get("official")),
        "id": iid,
        "title": title,
        "project_name": project,
        "category": category,
        "source_name": source_name,
        "source_url": source_url,
        "summary": summary,
        "chains": _csv_list(raw.get("chains"), limit=CHAIN_MAX),
        "status": status,
        "published_at": pub,
        "updated_at": upd,
        "external_url": external_url,
        "tags": _csv_list(raw.get("tags"), limit=TAG_MAX),
    }


def load_seed(path: Optional[Path] = None) -> list[dict[str, Any]]:
    p = Path(path) if path else SEED_PATH
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("airdrops seed unreadable: %s", exc)
        return []
    if not isinstance(raw, list):
        return []
    out = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        item = normalize_seed_item(row)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        out.append(item)
    return out


def _seed_fingerprint(items: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        [(i["id"], i["updated_at"], i["title"], i["status"]) for i in items],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    import hashlib

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


def _split_csv(s: str) -> list[str]:
    return [p for p in (s or "").split(",") if p]


def _row_out(r: sqlite3.Row, sightings: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    keys = r.keys()
    origin = r["origin"] if "origin" in keys else ("ingested" if "ingested" in _split_csv(r["tags"]) else "curated")
    sources = [
        {
            "sourceId": x["source_id"],
            "name": x["source_name"],
            "url": _article_url(x["url"]) or None,
            "kind": x["kind"] if x["kind"] in {"official", "media", "curated"} else "media",
            "publishedAt": x["published_at"],
        }
        for x in (sightings or [])[:8]
    ]
    verif = r["verification"] if "verification" in keys else "unverified"
    return {
        "id": r["id"],
        "title": r["title"],
        "projectName": r["project_name"],
        "category": r["category"],
        "sourceName": r["source_name"],
        "sourceUrl": _article_url(r["source_url"]) or feed_home(r["source_url"]) or None,
        "summary": r["summary"],
        "chains": _split_csv(r["chains"]),
        "status": r["status"],
        "publishedAt": r["published_at"],
        "updatedAt": r["updated_at"],
        "externalUrl": _article_url(r["external_url"]) or _article_url(r["source_url"]) or feed_home(r["source_url"]) or None,
        "tags": _split_csv(r["tags"]),
        "origin": origin if origin in {"curated", "ingested"} else "curated",
        "verification": verif if verif in {"confirmed", "unverified"} else "unverified",
        "official": bool(r["official"]) if "official" in keys else False,
        "sourceCount": int(r["source_count"]) if "source_count" in keys and r["source_count"] else max(1, len(sources)),
        "sources": sources,
        "risk": quality.RISK_NOTICE,
    }


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "unknown"


def _legacy_source(r: sqlite3.Row) -> tuple[str, str]:
    """(source_id, kind) for a row that has no sighting yet (pre-v2 data or seed)."""
    tags = _split_csv(r["tags"])
    if (r["origin"] if "origin" in r.keys() else "") == "ingested" or "ingested" in tags:
        for t in tags:
            if t in FEED_URL_HOME_IDS:
                return t, "media"
        return _slug(r["source_name"]), "media"
    return "curated-" + _slug(r["source_name"]), "curated"


FEED_URL_HOME_IDS = frozenset({"coindesk", "cointelegraph"})


class AirdropStore:
    def __init__(self, path: Optional[Path | str] = None, *, seed_path: Optional[Path | str] = None):
        self.path = Path(path) if path else data_dir() / LEDGER_NAME
        self.seed_path = Path(seed_path) if seed_path else SEED_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._migrate()
        self._lock = threading.RLock()
        self._migrate_v2()
        self.ensure_seeded()
        self._migrate_v2()


    def _migrate(self) -> None:
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(items)").fetchall()}
        if "origin" not in cols:
            self._db.execute("ALTER TABLE items ADD COLUMN origin TEXT NOT NULL DEFAULT 'curated'")
        self._db.execute(
            "UPDATE items SET origin='curated' WHERE IFNULL(origin,'')='' AND IFNULL(seed_hash,'')!=''"
        )
        self._db.execute(
            "UPDATE items SET origin='ingested' WHERE IFNULL(origin,'')='' AND (','||IFNULL(tags,'')||',') LIKE '%,ingested,%'"
        )
        self.repair_feed_urls()
        self._db.execute("CREATE INDEX IF NOT EXISTS airdrops_origin ON items (origin)")
        self._db.execute("CREATE INDEX IF NOT EXISTS airdrops_source_url ON items (source_url)")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS fetch_log ("
            "source TEXT PRIMARY KEY, last_attempt INTEGER, last_ok INTEGER, last_error TEXT, last_added INTEGER, "
            "last_items INTEGER, fails INTEGER NOT NULL DEFAULT 0)"
        )

    def _migrate_v2(self) -> None:
        """v2 columns, sightings backfill, scam-phrase purge, verification recompute (idempotent)."""
        with self._lock:
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(items)").fetchall()}
            for col, ddl in (
                ("verification", "TEXT NOT NULL DEFAULT 'unverified'"),
                ("source_count", "INTEGER NOT NULL DEFAULT 1"),
                ("official", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in cols:
                    self._db.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
            self._db.execute("BEGIN")
            try:
                purged = 0
                for r in self._db.execute("SELECT id, title, summary FROM items").fetchall():
                    if quality.blocked_reason(r["title"], r["summary"]):
                        self._db.execute("DELETE FROM items WHERE id=?", (r["id"],))
                        self._db.execute("DELETE FROM sightings WHERE item_id=?", (r["id"],))
                        purged += 1
                if purged:
                    log.warning("airdrops: purged %d items with blocked phrases", purged)
                # every item has at least its own sighting
                for r in self._db.execute(
                    "SELECT * FROM items WHERE id NOT IN (SELECT item_id FROM sightings)"
                ).fetchall():
                    sid, kind = _legacy_source(r)
                    self._db.execute(
                        "INSERT OR IGNORE INTO sightings(item_id, source_id, source_name, kind, url, title, summary, published_at) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (r["id"], sid, r["source_name"], kind, r["source_url"] or "", r["title"], (r["summary"] or "")[:300], r["published_at"]),
                    )
                self._db.execute("DELETE FROM sightings WHERE item_id NOT IN (SELECT id FROM items)")
                for r in self._db.execute("SELECT id FROM items").fetchall():
                    self._recompute(r["id"])
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    def _recompute(self, item_id: str) -> None:
        """Refresh verification / source_count / official for one item from its sightings."""
        sx = self._db.execute(
            "SELECT source_id, source_name, kind FROM sightings WHERE item_id=?", (item_id,)
        ).fetchall()
        v = quality.verification([dict(x) for x in sx])
        self._db.execute(
            "UPDATE items SET verification=?, source_count=?, official=? WHERE id=?",
            (v["label"], max(1, v["sourceCount"]), 1 if v["official"] else 0, item_id),
        )

    def _sightings_for(self, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
        if not ids:
            return out
        q = ",".join("?" * len(ids))
        for r in self._db.execute(
            f"SELECT * FROM sightings WHERE item_id IN ({q}) "
            "ORDER BY CASE kind WHEN 'official' THEN 0 WHEN 'media' THEN 1 ELSE 2 END, published_at ASC, id ASC",
            ids,
        ).fetchall():
            out[r["item_id"]].append(dict(r))
        return out

    def repair_feed_urls(self) -> int:
        """Backfill: rows whose source/external URL is a feed endpoint get the publisher
        homepage (or the other, non-feed URL) instead. Ingested rows whose own URL is a
        feed and have no article link are dropped (they are re-ingested with the real
        <link> on the next tick). Returns rows changed."""
        n = 0
        rows = self._db.execute("SELECT id, origin, source_url, external_url FROM items").fetchall()
        for r in rows:
            su, eu = r["source_url"] or "", r["external_url"] or ""
            if not (is_feed_url(su) or is_feed_url(eu)):
                continue
            new_su = _article_url(su) or _article_url(eu) or feed_home(su) or feed_home(eu)
            new_eu = _article_url(eu) or _article_url(su) or new_su
            if r["origin"] == "ingested" and not (_article_url(su) or _article_url(eu)):
                self._db.execute("DELETE FROM items WHERE id=?", (r["id"],))
            else:
                self._db.execute(
                    "UPDATE items SET source_url=?, external_url=? WHERE id=?", (new_su, new_eu, r["id"])
                )
            n += 1
        if n:
            log.warning("airdrops: repaired %d feed-URL rows", n)
        return n

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _meta_get(self, key: str) -> Optional[str]:
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def ensure_seeded(self, *, force: bool = False) -> int:
        """Upsert curated seed rows. Returns number of rows written/updated."""
        items = load_seed(self.seed_path)
        fp = _seed_fingerprint(items)
        with self._lock:
            if not force and self._meta_get("seed_fp") == fp and self._db.execute("SELECT COUNT(*) FROM items").fetchone()[0]:
                return 0
            self._db.execute("BEGIN")
            try:
                n = 0
                for i in items:
                    self._db.execute(
                        "INSERT INTO items(id, title, project_name, category, source_name, source_url, summary, chains, "
                        "status, published_at, updated_at, external_url, tags, seed_hash, origin) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET title=excluded.title, project_name=excluded.project_name, "
                        "category=excluded.category, source_name=excluded.source_name, source_url=excluded.source_url, "
                        "summary=excluded.summary, chains=excluded.chains, status=excluded.status, "
                        "published_at=excluded.published_at, updated_at=excluded.updated_at, "
                        "external_url=excluded.external_url, tags=excluded.tags, seed_hash=excluded.seed_hash, "
                        "origin='curated' "
                        "WHERE items.origin='curated' OR items.seed_hash!='' OR items.origin IS NULL OR items.origin=''",
                        (
                            i["id"],
                            i["title"],
                            i["project_name"],
                            i["category"],
                            i["source_name"],
                            i["source_url"],
                            i["summary"],
                            i["chains"],
                            i["status"],
                            i["published_at"],
                            i["updated_at"],
                            i["external_url"],
                            i["tags"],
                            fp,
                            "curated",
                        ),
                    )
                    n += 1
                    cur = self._db.execute("SELECT origin FROM items WHERE id=?", (i["id"],)).fetchone()
                    if cur is not None and cur["origin"] == "curated":
                        self._db.execute("DELETE FROM sightings WHERE item_id=? AND kind IN ('curated','official')", (i["id"],))
                        self._db.execute(
                            "INSERT OR IGNORE INTO sightings(item_id, source_id, source_name, kind, url, title, summary, published_at) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (
                                i["id"], "curated-" + _slug(i["source_name"]), i["source_name"],
                                "official" if i.get("official") else "curated",
                                i["source_url"], i["title"], i["summary"][:300], i["published_at"],
                            ),
                        )
                        self._recompute(i["id"])
                self._meta_set("seed_fp", fp)
                self._meta_set("seeded_at", str(int(datetime.now().timestamp() * 1000)))
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
            return n

    def list(
        self,
        *,
        category: str = "all",
        status: Optional[str] = None,
        origin: Optional[str] = None,
        verification: Optional[str] = None,
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        where: list[str] = []
        args: list[Any] = []
        if category and category != "all":
            where.append("category = ?")
            args.append(category)
        if status:
            where.append("status = ?")
            args.append(status)
        if origin in {"curated", "ingested"}:
            where.append("origin = ?")
            args.append(origin)
        if verification in {"confirmed", "unverified"}:
            where.append("verification = ?")
            args.append(verification)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = int(self._db.execute(f"SELECT COUNT(*) FROM items{w}", args).fetchone()[0])
            rows = self._db.execute(
                f"SELECT * FROM items{w} ORDER BY published_at DESC, id ASC LIMIT ? OFFSET ?",
                (*args, size, (page - 1) * size),
            ).fetchall()
            seeded_at = self._meta_get("seeded_at")
            logs = {r["source"]: dict(r) for r in self._db.execute("SELECT * FROM fetch_log")}
            last_ingest = self._meta_get("last_ingest_ok")
            sx = self._sightings_for([r["id"] for r in rows])
            counts = {
                r["verification"]: int(r["n"])
                for r in self._db.execute("SELECT verification, COUNT(*) AS n FROM items GROUP BY verification")
            }
        items = [_row_out(r, sx.get(r["id"])) for r in rows]
        return {
            "items": items,
            "page": page,
            "size": size,
            "total": total,
            "pages": max(1, -(-total // size)) if total else 1,
            "seededAt": int(seeded_at) if seeded_at and seeded_at.isdigit() else None,
            "lastIngestAt": int(last_ingest) if last_ingest and last_ingest.isdigit() else None,
            "feeds": _feed_status(logs),
            "verificationCounts": {"confirmed": counts.get("confirmed", 0), "unverified": counts.get("unverified", 0)},
        }


    def upsert_ingested(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        """Back-compat wrapper over :meth:`merge_ingested`: (newly_inserted, everything_else)."""
        r = self.merge_ingested(rows)
        return r["added"], r["merged"] + r["skipped"]

    def merge_ingested(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        """Insert/merge ingested rows (each: item fields + ``source_id`` + ``kind`` official|media).

        * rows with a blocked phrase are dropped (``blocked``);
        * a URL we already hold (any source, curated included) is ``skipped`` (curated is never overwritten);
        * the same campaign reported by another source is added as a sighting of the existing card
          (``merged``) and may promote an ingested card's headline to the official wording;
        * anything else becomes a new card (``added``).
        """
        out = {"added": 0, "merged": 0, "skipped": 0, "blocked": 0}
        if not rows:
            return out
        with self._lock:
            self._db.execute("BEGIN")
            try:
                for i in rows:
                    if quality.blocked_reason(i.get("title"), i.get("summary")):
                        out["blocked"] += 1
                        continue
                    self._merge_one(i, out)
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return out

    def _merge_one(self, i: dict[str, Any], out: dict[str, int]) -> None:
        url = i.get("source_url") or ""
        source_id = str(i.get("source_id") or "") or _slug(i["source_name"])
        kind = i.get("kind") if i.get("kind") in {"official", "media"} else "media"
        pub = int(i["published_at"])
        if url:
            hit = self._db.execute("SELECT item_id FROM sightings WHERE url=? LIMIT 1", (url,)).fetchone()
            if hit is None:
                hit = self._db.execute("SELECT id AS item_id FROM items WHERE source_url=? LIMIT 1", (url,)).fetchone()
            if hit is not None:
                self._refresh_known(hit["item_id"], i, source_id, kind)
                out["skipped"] += 1
                return
        existing = self._db.execute("SELECT origin, seed_hash FROM items WHERE id=?", (i["id"],)).fetchone()
        if existing is not None:
            out["skipped"] += 1
            return
        cand = {
            "title": i["title"], "summary": i.get("summary") or "", "url": url,
            "published_at": pub, "source_id": source_id,
        }
        lo, hi = pub - quality.SAME_WINDOW_MS, pub + quality.SAME_WINDOW_MS
        target: Optional[str] = None
        for s in self._db.execute(
            "SELECT item_id, source_id, title, summary, url, published_at FROM sightings "
            "WHERE published_at BETWEEN ? AND ? ORDER BY published_at ASC, id ASC",
            (lo, hi),
        ).fetchall():
            if quality.same_activity(cand, dict(s)):
                target = s["item_id"]
                break
        if target is None:
            tags = i["tags"]
            if kind == "official" and "official" not in _split_csv(tags):
                tags = ",".join([*_split_csv(tags), "official"])
            self._db.execute(
                "INSERT INTO items(id, title, project_name, category, source_name, source_url, summary, chains, "
                "status, published_at, updated_at, external_url, tags, seed_hash, origin) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    i["id"], i["title"], i["project_name"], i["category"], i["source_name"], url,
                    i.get("summary") or "", i.get("chains") or "", i["status"], pub, int(i["updated_at"]),
                    i.get("external_url") or url, tags, "", "ingested",
                ),
            )
            self._add_sighting(i["id"], source_id, i["source_name"], kind, url, i["title"], i.get("summary") or "", pub)
            self._recompute(i["id"])
            out["added"] += 1
            return
        self._add_sighting(target, source_id, i["source_name"], kind, url, i["title"], i.get("summary") or "", pub)
        row = self._db.execute("SELECT origin, official, seed_hash FROM items WHERE id=?", (target,)).fetchone()
        if row is not None and row["origin"] == "ingested" and not (row["seed_hash"] or "") and kind == "official" and not row["official"]:
            # promote the card to the official wording / link (earliest publish time is kept)
            self._db.execute(
                "UPDATE items SET title=?, project_name=?, category=?, source_name=?, source_url=?, summary=?, "
                "external_url=?, published_at=MIN(published_at, ?), updated_at=? WHERE id=?",
                (
                    i["title"], i["project_name"], i["category"], i["source_name"], url, i.get("summary") or "",
                    i.get("external_url") or url, pub, int(i["updated_at"]), target,
                ),
            )
        self._recompute(target)
        out["merged"] += 1

    def _add_sighting(self, item_id: str, source_id: str, name: str, kind: str, url: str, title: str, summary: str, pub: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO sightings(item_id, source_id, source_name, kind, url, title, summary, published_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (item_id, source_id, name, kind, url, title, summary[:300], pub),
        )

    def _refresh_known(self, item_id: str, i: dict[str, Any], source_id: str, kind: str) -> None:
        """A URL we already have came around again: keep the sighting's title current; touch nothing else."""
        self._db.execute(
            "UPDATE sightings SET title=?, summary=? WHERE item_id=? AND url=? AND source_id=?",
            (i["title"], (i.get("summary") or "")[:300], item_id, i.get("source_url") or "", source_id),
        )
        row = self._db.execute("SELECT origin, seed_hash, source_name FROM items WHERE id=?", (item_id,)).fetchone()
        if row is not None and row["origin"] == "ingested" and not (row["seed_hash"] or "") and row["source_name"] == i["source_name"]:
            self._db.execute(
                "UPDATE items SET title=?, summary=?, status=?, updated_at=? WHERE id=?",
                (i["title"], i.get("summary") or "", i["status"], int(i["updated_at"]), item_id),
            )

    def prune_ingested(self, now_ms: int) -> int:
        """Drop old ingested rows only (curated kept)."""
        from app.airdrops.ingest import RETAIN_INGESTED_DAYS, RETAIN_INGESTED_ROWS

        with self._lock:
            before = self._db.total_changes
            cutoff = now_ms - RETAIN_INGESTED_DAYS * 86_400_000
            self._db.execute(
                "DELETE FROM items WHERE origin='ingested' AND IFNULL(seed_hash,'')='' AND published_at < ?",
                (cutoff,),
            )
            # cap ingested count
            self._db.execute(
                "DELETE FROM items WHERE origin='ingested' AND IFNULL(seed_hash,'')='' AND id NOT IN ("
                "SELECT id FROM items WHERE origin='ingested' AND IFNULL(seed_hash,'')='' "
                "ORDER BY published_at DESC LIMIT ?)",
                (RETAIN_INGESTED_ROWS,),
            )
            self._db.execute("DELETE FROM sightings WHERE item_id NOT IN (SELECT id FROM items)")
            return self._db.total_changes - before

    def log_fetch(self, source: str, *, now_ms: int, ok: bool, error: str = "", added: int = 0, items: int = 0) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO fetch_log(source, fails) VALUES (?, 0)", (source,))
            if ok:
                self._db.execute(
                    "UPDATE fetch_log SET last_attempt=?, last_ok=?, last_error=NULL, last_added=?, last_items=?, fails=0 WHERE source=?",
                    (now_ms, now_ms, added, items, source),
                )
                self._meta_set("last_ingest_ok", str(now_ms))
            else:
                self._db.execute(
                    "UPDATE fetch_log SET last_attempt=?, last_error=?, fails=fails+1 WHERE source=?",
                    (now_ms, error[:200], source),
                )

    def fetch_state(self, source: str) -> Optional[dict[str, Any]]:
        with self._lock:
            r = self._db.execute("SELECT * FROM fetch_log WHERE source=?", (source,)).fetchone()
        return dict(r) if r else None

    def get(self, item_id: str) -> Optional[dict[str, Any]]:
        iid = (item_id or "").strip()
        if not iid:
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            sx = self._sightings_for([iid]).get(iid) if row else None
        return _row_out(row, sx) if row else None



def _feed_status(logs: dict[str, dict]) -> list[dict[str, Any]]:
    try:
        from app.airdrops.ingest import FEED_SOURCES, enabled_feeds
    except Exception:
        return []
    out = []
    for sid in enabled_feeds():
        s, lg = FEED_SOURCES[sid], logs.get(sid) or {}
        out.append({
            "id": sid,
            "name": s["name"],
            "home": s["home"],
            "tier": s.get("tier", "media"),
            "lastOkAt": lg.get("last_ok"),
            "lastAttemptAt": lg.get("last_attempt"),
            "ok": bool(lg.get("last_ok")) and not lg.get("last_error"),
            "fails": lg.get("fails") or 0,
            "error": (lg.get("last_error") or "").split(":", 1)[0][:40] or None,
        })
    return out


_STORE: Optional[AirdropStore] = None
_GUARD = threading.Lock()


def get_store() -> AirdropStore:
    global _STORE
    with _GUARD:
        if _STORE is None:
            _STORE = AirdropStore()
        return _STORE


def peek_store() -> Optional[AirdropStore]:
    """Return the store, creating it (and seeding) if the seed file exists."""
    if _STORE is not None:
        return _STORE
    if not SEED_PATH.exists() and not (data_dir() / LEDGER_NAME).exists():
        return None
    return get_store()


def reset_store(store: Optional[AirdropStore] = None) -> None:
    global _STORE
    with _GUARD:
        _STORE = store
