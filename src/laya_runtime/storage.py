from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import ExecutionResult, uid


class EventStore:
    """Durable admission journal + bounded live stream. Persist metadata, never form facts."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
              type TEXT NOT NULL, time TEXT NOT NULL, payload TEXT NOT NULL,
              correlation_id TEXT, causation_id TEXT, UNIQUE(session_id,seq));
            CREATE TABLE IF NOT EXISTS actions (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, result TEXT NOT NULL);
        """)
        self._db.commit()
        self._lock = threading.Lock()
        self._write_gate = asyncio.Lock()
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def _write_event(self, session_id, kind, payload, correlation_id, causation_id):
        with self._lock:
            seq = self._db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session_id=?", (session_id,)).fetchone()[0]
            event = {"id": uid("evt"), "session_id": session_id, "seq": seq, "type": kind,
                     "time": datetime.now(timezone.utc).isoformat(), "payload": payload,
                     "correlation_id": correlation_id, "causation_id": causation_id}
            self._db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (
                event["id"], session_id, seq, kind, event["time"], json.dumps(payload, allow_nan=False),
                correlation_id, causation_id))
            self._db.commit()
            return event

    async def emit(self, session_id: str, kind: str, payload: dict | None = None,
                   correlation_id: str | None = None, causation_id: str | None = None) -> dict:
        async with self._write_gate:
            event = await asyncio.to_thread(self._write_event, session_id, kind, payload or {}, correlation_id, causation_id)
            for queue in tuple(self._subscribers.get(session_id, ())):
                if queue.full():
                    queue.get_nowait()  # Consumers detect seq gaps and fetch persisted history.
                queue.put_nowait(event)
            return event

    def _events(self, session_id: str, after: int, limit: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id,session_id,seq,type,time,payload,correlation_id,causation_id "
                "FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
                (session_id, after, min(max(limit, 1), 500))).fetchall()
        return [dict(zip(("id", "session_id", "seq", "type", "time", "payload", "correlation_id", "causation_id"),
                         (*row[:5], json.loads(row[5]), *row[6:]))) for row in rows]

    async def events(self, session_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        return await asyncio.to_thread(self._events, session_id, after, limit)

    def _admit(self, action_id: str, session_id: str) -> ExecutionResult | None:
        with self._lock:
            row = self._db.execute("SELECT result FROM actions WHERE id=?", (action_id,)).fetchone()
            if row:
                return ExecutionResult.model_validate_json(row[0])
            unknown = ExecutionResult(action_id=action_id, status="unknown", code="DISPATCH_NOT_VERIFIED")
            self._db.execute("INSERT INTO actions VALUES (?,?,?)", (action_id, session_id, unknown.model_dump_json()))
            self._db.commit()
            return None

    async def admit(self, action_id: str, session_id: str) -> ExecutionResult | None:
        # Commit the unknown state BEFORE dispatch. A process crash never causes an automatic replay.
        return await asyncio.to_thread(self._admit, action_id, session_id)

    def _finish(self, result: ExecutionResult):
        with self._lock:
            self._db.execute("UPDATE actions SET result=? WHERE id=?", (result.model_dump_json(), result.action_id))
            self._db.commit()

    async def finish(self, result: ExecutionResult) -> None:
        await asyncio.to_thread(self._finish, result)

    def subscribe(self, session_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._subscribers.setdefault(session_id, set()).add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue) -> None:
        self._subscribers.get(session_id, set()).discard(queue)

    async def close(self) -> None:
        await asyncio.to_thread(self._db.close)
