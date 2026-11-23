"""Per-user persistence for studio drafts, paper agents, and simulations.

Follows the chart-drawings convention: a dedicated SQLite file under
``data_dir()``, keyed by ``user_id``, never mixed into the system paper
ledger. Association checks, quotas, and writes happen in one IMMEDIATE
transaction so a concurrent delete cannot leave a dangling agent.
"""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from app.data_paths import data_dir

log = logging.getLogger("auu.ai_section.store")

LEDGER_NAME = "ai_section_studio.sqlite"
MAX_STRATEGIES = 20
MAX_AGENTS = 10
MAX_SIMULATIONS = 50

_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS strategies (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS strategies_user ON strategies (user_id, updated_at);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  name TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS agents_user ON agents (user_id, updated_at);
CREATE TABLE IF NOT EXISTS simulations (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  status TEXT NOT NULL,
  request_json TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
  FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS simulations_user ON simulations (user_id, agent_id, created_at);
"""

_STORE: Optional["StudioStore"] = None
_STORE_LOCK = threading.Lock()


class StoreError(ValueError):
    def __init__(self, code: str, message: str = "bad_body", status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class StudioStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else data_dir() / LEDGER_NAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            if self._conn.in_transaction:
                self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _now(self) -> int:
        return int(time.time() * 1000)

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}_{secrets.token_hex(8)}"

    def list_strategies(self, uid: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT json, id, created_at, updated_at FROM strategies WHERE user_id=? ORDER BY updated_at DESC",
                (uid,),
            ).fetchall()
        return [self._strategy_row(row) for row in rows]

    def get_strategy(self, uid: str, sid: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT json, id, created_at, updated_at FROM strategies WHERE id=? AND user_id=?",
                (sid, uid),
            ).fetchone()
        if row is None:
            raise StoreError("NOT_FOUND", "not found", 404)
        return self._strategy_row(row)

    def _strategy_in_tx(self, conn: sqlite3.Connection, uid: str, sid: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT json, id, created_at, updated_at FROM strategies WHERE id=? AND user_id=?",
            (sid, uid),
        ).fetchone()
        if row is None:
            raise StoreError("NOT_FOUND", "not found", 404)
        return self._strategy_row(row)

    def create_strategy(self, uid: str, body: dict[str, Any]) -> dict[str, Any]:
        sid = self._new_id("s")
        now = self._now()
        payload = {**body, "id": sid, "created_at": now, "updated_at": now}
        with self._tx() as conn:
            n = conn.execute("SELECT COUNT(*) FROM strategies WHERE user_id=?", (uid,)).fetchone()[0]
            if n >= MAX_STRATEGIES:
                raise StoreError("LIMIT", "strategy limit", 400)
            conn.execute(
                "INSERT INTO strategies (id, user_id, name, kind, status, json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (sid, uid, body["name"], body["kind"], body["status"], json.dumps(payload, separators=(",", ":")), now, now),
            )
        return self.get_strategy(uid, sid)

    def update_strategy(self, uid: str, sid: str, body: dict[str, Any]) -> dict[str, Any]:
        with self._tx() as conn:
            current = self._strategy_in_tx(conn, uid, sid)
            payload = {**body, "id": sid, "created_at": current["created_at"], "updated_at": self._now()}
            cur = conn.execute(
                "UPDATE strategies SET name=?, kind=?, status=?, json=?, updated_at=? WHERE id=? AND user_id=?",
                (body["name"], body["kind"], body["status"], json.dumps(payload, separators=(",", ":")), payload["updated_at"], sid, uid),
            )
            if cur.rowcount == 0:
                raise StoreError("NOT_FOUND", "not found", 404)
        return self.get_strategy(uid, sid)

    def delete_strategy(self, uid: str, sid: str) -> dict[str, Any]:
        with self._tx() as conn:
            self._strategy_in_tx(conn, uid, sid)
            linked = conn.execute(
                "SELECT COUNT(*) FROM agents WHERE user_id=? AND strategy_id=?",
                (uid, sid),
            ).fetchone()[0]
            if linked:
                raise StoreError("IN_USE", "strategy is linked to a paper agent", 409)
            conn.execute("DELETE FROM strategies WHERE id=? AND user_id=?", (sid, uid))
        return {"deleted": sid}

    def list_agents(self, uid: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, strategy_id, name, status, created_at, updated_at FROM agents WHERE user_id=? ORDER BY updated_at DESC",
                (uid,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_agent(self, uid: str, aid: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, strategy_id, name, status, created_at, updated_at FROM agents WHERE id=? AND user_id=?",
                (aid, uid),
            ).fetchone()
        if row is None:
            raise StoreError("NOT_FOUND", "not found", 404)
        return dict(row)

    def create_agent(self, uid: str, name: str, strategy_id: str) -> dict[str, Any]:
        aid = self._new_id("a")
        now = self._now()
        with self._tx() as conn:
            strategy = self._strategy_in_tx(conn, uid, strategy_id)
            if strategy.get("status") != "verified":
                raise StoreError("UNVERIFIED", "strategy is not verified", 400)
            n = conn.execute("SELECT COUNT(*) FROM agents WHERE user_id=?", (uid,)).fetchone()[0]
            if n >= MAX_AGENTS:
                raise StoreError("LIMIT", "agent limit", 400)
            conn.execute(
                "INSERT INTO agents (id, user_id, strategy_id, name, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (aid, uid, strategy_id, name, "idle", now, now),
            )
        return self.get_agent(uid, aid)

    def delete_agent(self, uid: str, aid: str) -> dict[str, Any]:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id FROM agents WHERE id=? AND user_id=?",
                (aid, uid),
            ).fetchone()
            if row is None:
                raise StoreError("NOT_FOUND", "not found", 404)
            conn.execute("DELETE FROM simulations WHERE user_id=? AND agent_id=?", (uid, aid))
            conn.execute("DELETE FROM agents WHERE id=? AND user_id=?", (aid, uid))
        return {"deleted": aid}

    def add_simulation(self, uid: str, aid: str, strategy_id: str, status: str, request: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        mid = self._new_id("m")
        now = self._now()
        record = {"id": mid, "agent_id": aid, "strategy_id": strategy_id, "status": status, "request": request, "result": result, "created_at": now}
        with self._tx() as conn:
            agent = conn.execute(
                "SELECT id, strategy_id FROM agents WHERE id=? AND user_id=?",
                (aid, uid),
            ).fetchone()
            if agent is None:
                raise StoreError("NOT_FOUND", "not found", 404)
            if agent["strategy_id"] != strategy_id:
                raise StoreError("NOT_FOUND", "not found", 404)
            self._strategy_in_tx(conn, uid, strategy_id)
            conn.execute(
                "INSERT INTO simulations (id, user_id, agent_id, strategy_id, status, request_json, result_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (mid, uid, aid, strategy_id, status, json.dumps(request, separators=(",", ":")), json.dumps(result, separators=(",", ":")), now),
            )
            extra = conn.execute(
                "SELECT id FROM simulations WHERE user_id=? ORDER BY created_at DESC",
                (uid,),
            ).fetchall()
            if len(extra) > MAX_SIMULATIONS:
                drop = [row["id"] for row in extra[MAX_SIMULATIONS:]]
                conn.executemany("DELETE FROM simulations WHERE id=? AND user_id=?", [(item, uid) for item in drop])
            updated = conn.execute(
                "UPDATE agents SET status=?, updated_at=? WHERE id=? AND user_id=?",
                (status, now, aid, uid),
            )
            if updated.rowcount == 0:
                raise StoreError("NOT_FOUND", "not found", 404)
        return record

    def list_simulations(self, uid: str, aid: str, limit: int = 20) -> list[dict[str, Any]]:
        self.get_agent(uid, aid)
        cap = min(max(int(limit), 1), 20)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, agent_id, strategy_id, status, request_json, result_json, created_at FROM simulations WHERE user_id=? AND agent_id=? ORDER BY created_at DESC LIMIT ?",
                (uid, aid, cap),
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["request"] = json.loads(item.pop("request_json"))
            item["result"] = json.loads(item.pop("result_json"))
            out.append(item)
        return out

    def counts(self, uid: str | None = None) -> dict[str, int]:
        clause = " WHERE user_id=?" if uid else ""
        args = (uid,) if uid else ()
        with self._lock:
            strategies = self._conn.execute("SELECT COUNT(*) FROM strategies" + clause, args).fetchone()[0]
            agents = self._conn.execute("SELECT COUNT(*) FROM agents" + clause, args).fetchone()[0]
            simulations = self._conn.execute("SELECT COUNT(*) FROM simulations" + clause, args).fetchone()[0]
        return {"strategies": strategies, "agents": agents, "simulations": simulations}

    def _strategy_row(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["json"])
        payload["id"] = row["id"]
        payload["created_at"] = row["created_at"]
        payload["updated_at"] = row["updated_at"]
        return payload


def get_store() -> StudioStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = StudioStore()
        return _STORE


def reset_store(store: Optional[StudioStore] = None) -> None:
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None and store is not _STORE:
            try:
                _STORE.close()
            except sqlite3.Error:
                log.exception("ai_section store close failed")
        _STORE = store
