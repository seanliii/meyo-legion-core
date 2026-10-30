# -*- coding: utf-8 -*-
"""云 UI 持久化 run store(SQLite)—— 多用户隔离的唯一事实源。"""
from __future__ import annotations
import json, sqlite3, threading, time
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()


class RunStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                payload TEXT NOT NULL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, updated_at)")

    def _db(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def put(self, run: dict[str, Any]):
        now = time.time()
        run = dict(run)
        run.setdefault("created_at", now)
        run["updated_at"] = now
        with _LOCK, self._db() as db:
            db.execute("""INSERT INTO runs(run_id,user_id,status,created_at,updated_at,payload)
                VALUES(?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status, updated_at=excluded.updated_at, payload=excluded.payload""",
                (run["run_id"], run["user_id"], run["status"], run["created_at"],
                 run["updated_at"], json.dumps(run, ensure_ascii=False)))

    def get(self, run_id: str, user_id: str) -> dict | None:
        with _LOCK, self._db() as db:
            row = db.execute("SELECT payload FROM runs WHERE run_id=? AND user_id=?",
                             (run_id, user_id)).fetchone()
        return json.loads(row["payload"]) if row else None

    def list(self, user_id: str, limit: int = 100) -> list[dict]:
        with _LOCK, self._db() as db:
            rows = db.execute("SELECT payload FROM runs WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
                              (user_id, max(1, min(limit, 500)))).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def recover_interrupted(self):
        """进程重启后 queued/running 不可能仍在执行→标 interrupted,但保留完整 payload/steps。"""
        with _LOCK, self._db() as db:
            rows = db.execute("SELECT payload FROM runs WHERE status IN ('queued','running')").fetchall()
            for row in rows:
                run = json.loads(row["payload"])
                run["status"] = "interrupted"
                run["interrupt_reason"] = "server_restarted"
                run["updated_at"] = time.time()
                db.execute("UPDATE runs SET status=?,updated_at=?,payload=? WHERE run_id=?",
                           (run["status"], run["updated_at"], json.dumps(run, ensure_ascii=False),
                            run["run_id"]))
