"""Durable local project/session history for AIFusion.

The store is deliberately local and append-oriented: events are immutable, while
snapshots make restarting the Palette cheap.  API keys and base64 image data are
never written here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_data_dir() -> Path:
    override = os.environ.get("AIFUSION_DATA_DIR")
    if override:
        return Path(override)
    # Keep project history beside the add-in so the project can be copied or
    # backed up as one unit.  This is intentionally not the OS user cache.
    return Path(__file__).resolve().parent.parent / ".aifusion"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class HistoryStore:
    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else default_data_dir() / "history.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
              title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
              event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS snapshots (
              session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
              event_id TEXT NOT NULL, state_json TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);
            """)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def create_project(self, name: str) -> dict[str, Any]:
        name = (name or "Untitled Fusion project").strip()[:120]
        project_id, now = uuid.uuid4().hex, _now()
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO projects VALUES (?, ?, ?, ?, 0)", (project_id, name, now, now))
        return {"id": project_id, "name": name, "created_at": now, "updated_at": now, "archived": 0}

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM projects WHERE archived=0 ORDER BY updated_at DESC")]

    def create_session(self, project_id: str, title: str = "New design session") -> dict[str, Any]:
        session_id, now = uuid.uuid4().hex, _now()
        with self._lock, self._connect() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=? AND archived=0", (project_id,)).fetchone():
                raise KeyError("project not found")
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, 'active')", (session_id, project_id, title[:120], now, now))
            db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, project_id))
        return {"id": session_id, "project_id": project_id, "title": title[:120], "created_at": now, "updated_at": now, "status": "active"}

    def list_sessions(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM sessions WHERE project_id=? ORDER BY updated_at DESC", (project_id,))]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                return None
            snap = db.execute("SELECT event_id, state_json, created_at FROM snapshots WHERE session_id=?", (session_id,)).fetchone()
            events = db.execute("SELECT event_id, event_type, payload_json, created_at FROM events WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        result = dict(row)
        result["state"] = json.loads(snap["state_json"]) if snap else {"conversation": []}
        result["events"] = [{"event_id": e["event_id"], "event_type": e["event_type"], "payload": json.loads(e["payload_json"]), "created_at": e["created_at"]} for e in events]
        return result

    def append_event(self, session_id: str, event_type: str, payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        event_id, now = uuid.uuid4().hex, _now()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT project_id FROM sessions WHERE id=?", (session_id,)).fetchone()
            if not row:
                raise KeyError("session not found")
            db.execute("INSERT INTO events(session_id,event_id,event_type,payload_json,created_at) VALUES (?,?,?,?,?)", (session_id, event_id, event_type[:80], _json(payload), now))
            db.execute("INSERT INTO snapshots(session_id,event_id,state_json,created_at) VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET event_id=excluded.event_id,state_json=excluded.state_json,created_at=excluded.created_at", (session_id, event_id, _json(state), now))
            db.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
            db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, row["project_id"]))
        return {"event_id": event_id, "created_at": now}

    def archive_session(self, session_id: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("UPDATE sessions SET status='archived', updated_at=? WHERE id=?", (_now(), session_id))
