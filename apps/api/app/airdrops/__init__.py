"""空投雷达: curated airdrop / points-program headlines (info only).

- P0 data lives in ``seed.json`` next to this module (title, summary, source attribution,
  original links). No scraping of sites that forbid it; no API keys.
- Rows are loaded into ``airdrops.sqlite`` on first open (or when the seed file is newer /
  has new ids). The route only reads the local store.
- Login required (same pattern as ``app.news``). Never touches points, trading, or withdraw.
- Off switch: ``AUU_AIRDROPS=off``. Under the test runner (AUU_TEST=1) the store still works
  when tests point it at a temp path; the env only gates optional background work (none in P0).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.data_paths import data_dir

log = logging.getLogger("auu.airdrops")

LEDGER_NAME = "airdrops.sqlite"
SEED_PATH = Path(__file__).resolve().parent / "seed.json"
CATEGORIES = ("major", "community")
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
  seed_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS airdrops_cat ON items (category);
CREATE INDEX IF NOT EXISTS airdrops_status ON items (status);
CREATE INDEX IF NOT EXISTS airdrops_pub ON items (published_at DESC);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
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
    source_url = _clean_url(raw.get("source_url"))
    external_url = _clean_url(raw.get("external_url")) or source_url
    summary = str(raw.get("summary") or "").strip()[:SUMMARY_MAX]
    pub = _parse_ts(raw.get("published_at"))
    upd = _parse_ts(raw.get("updated_at")) or pub
    if pub <= 0:
        return None
    return {
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


def _row_out(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "title": r["title"],
        "projectName": r["project_name"],
        "category": r["category"],
        "sourceName": r["source_name"],
        "sourceUrl": r["source_url"] or None,
        "summary": r["summary"],
        "chains": _split_csv(r["chains"]),
        "status": r["status"],
        "publishedAt": r["published_at"],
        "updatedAt": r["updated_at"],
        "externalUrl": r["external_url"] or None,
        "tags": _split_csv(r["tags"]),
    }


class AirdropStore:
    def __init__(self, path: Optional[Path | str] = None, *, seed_path: Optional[Path | str] = None):
        self.path = Path(path) if path else data_dir() / LEDGER_NAME
        self.seed_path = Path(seed_path) if seed_path else SEED_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._lock = threading.RLock()
        self.ensure_seeded()

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
                        "status, published_at, updated_at, external_url, tags, seed_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET title=excluded.title, project_name=excluded.project_name, "
                        "category=excluded.category, source_name=excluded.source_name, source_url=excluded.source_url, "
                        "summary=excluded.summary, chains=excluded.chains, status=excluded.status, "
                        "published_at=excluded.published_at, updated_at=excluded.updated_at, "
                        "external_url=excluded.external_url, tags=excluded.tags, seed_hash=excluded.seed_hash",
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
                        ),
                    )
                    n += 1
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
        w = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = int(self._db.execute(f"SELECT COUNT(*) FROM items{w}", args).fetchone()[0])
            rows = self._db.execute(
                f"SELECT * FROM items{w} ORDER BY published_at DESC, id ASC LIMIT ? OFFSET ?",
                (*args, size, (page - 1) * size),
            ).fetchall()
            seeded_at = self._meta_get("seeded_at")
        items = [_row_out(r) for r in rows]
        return {
            "items": items,
            "page": page,
            "size": size,
            "total": total,
            "pages": max(1, -(-total // size)) if total else 1,
            "seededAt": int(seeded_at) if seeded_at and seeded_at.isdigit() else None,
        }

    def get(self, item_id: str) -> Optional[dict[str, Any]]:
        iid = (item_id or "").strip()
        if not iid:
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        return _row_out(row) if row else None


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
