"""taskapi store — tiny thread-safe in-memory database.

Lock-guarded dicts keep every mutation race-free on free-threaded builds
too. The method shapes mirror a repository interface, so swapping in
SQLite/Postgres later touches this file only.
"""

from __future__ import annotations

import datetime
import threading
from typing import Any, Optional


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._users: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, str] = {}
        self._tasks: dict[int, dict[str, Any]] = {}
        self._feed: list[dict[str, Any]] = []
        self._next_task_id = 1

    # -- users -----------------------------------------------------------
    def add_user(self, username: str, password_hash: str, salt: str) -> dict[str, Any]:
        with self._lock:
            if username in self._users:
                raise KeyError(username)
            user = {"username": username, "password_hash": password_hash,
                    "salt": salt, "created_at": _now()}
            self._users[username] = user
            return dict(user)

    def get_user(self, username: str) -> Optional[dict[str, Any]]:
        with self._lock:
            user = self._users.get(username)
            return dict(user) if user else None

    # -- tokens (demo-grade opaque bearer tokens) -------------------------
    def issue_token(self, username: str, token: str) -> None:
        with self._lock:
            self._tokens[token] = username

    def token_owner(self, token: str) -> Optional[str]:
        with self._lock:
            return self._tokens.get(token)

    # -- tasks -------------------------------------------------------------
    def create_task(self, owner: str, title: str, notes: str, priority: int) -> dict[str, Any]:
        with self._lock:
            task_id = self._next_task_id
            self._next_task_id += 1
            now = _now()
            task = {"id": task_id, "owner": owner, "title": title, "notes": notes,
                    "priority": priority, "status": "open",
                    "created_at": now, "updated_at": now}
            self._tasks[task_id] = task
            return dict(task)

    def list_tasks(self, owner: str, *, status: Optional[str] = None,
                   skip: int = 0, limit: int = 20) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = [t for t in self._tasks.values() if t["owner"] == owner]
            if status is not None:
                rows = [t for t in rows if t["status"] == status]
            rows.sort(key=lambda t: t["id"])
            return [dict(t) for t in rows[skip:skip + limit]], len(rows)

    def get_task(self, owner: str, task_id: int) -> Optional[dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["owner"] != owner:
                return None
            return dict(task)

    def update_task(self, owner: str, task_id: int, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["owner"] != owner:
                return None
            for key in ("title", "notes", "priority", "status"):
                if patch.get(key) is not None:
                    task[key] = patch[key]
            task["updated_at"] = _now()
            return dict(task)

    def delete_task(self, owner: str, task_id: int) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task["owner"] != owner:
                return False
            del self._tasks[task_id]
            return True

    # -- activity feed (background jobs append; SSE streams) ----------------
    def append_feed(self, kind: str, text: str) -> dict[str, Any]:
        with self._lock:
            event = {"id": len(self._feed) + 1, "kind": kind, "text": text, "at": _now()}
            self._feed.append(event)
            return dict(event)

    def read_feed(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._feed[-limit:]]


store = Store()
