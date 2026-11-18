from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import audit


class Conversations:
    def __init__(self, path: Path | None = None):
        self.db = sqlite3.connect(path or audit.runs_dir() / "checkpoints.sqlite", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS conversations (thread TEXT PRIMARY KEY, tenant TEXT NOT NULL, "
                        "actor TEXT NOT NULL, title TEXT, created REAL, updated REAL)")
        self.lock = threading.Lock()

    def create(self, tenant: str, actor: str) -> str:
        thread = uuid.uuid4().hex[:12]
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO conversations VALUES (?, ?, ?, NULL, ?, ?)", (thread, tenant, actor, now, now))
        return thread

    def owns(self, thread: str, tenant: str, actor: str) -> bool:
        with self.lock:
            row = self.db.execute("SELECT tenant, actor FROM conversations WHERE thread = ?", (thread,)).fetchone()
        return row == (tenant, actor)

    def touch(self, thread: str, question: str) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE conversations SET title = COALESCE(title, ?), updated = ? WHERE thread = ?",
                            (question.strip()[:80], time.time(), thread))

    def list(self, tenant: str, actor: str) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT thread, title, created, updated FROM conversations WHERE tenant = ? AND "
                                   "actor = ? ORDER BY updated DESC LIMIT 200", (tenant, actor)).fetchall()
        return [{"thread": t, "title": title or "New conversation", "created": c, "updated": u}
                for t, title, c, u in rows]
