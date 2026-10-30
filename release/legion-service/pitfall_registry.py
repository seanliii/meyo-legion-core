#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨轮失败模式注册表：保存可核验 objection，不把整段答案当知识。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import legion_paths as _paths

_PATH = Path(os.environ.get("LEGION_PITFALL_FILE") or _paths.under("pitfalls.json"))
_LOCK = threading.RLock()


def _load() -> dict:
    try:
        value = json.loads(_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"version": 1, "items": {}}
    except Exception:
        return {"version": 1, "items": {}}


def _save(value: dict) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=_PATH.name + ".", dir=_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, _PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _id(category: str, finding: str) -> str:
    normalized = " ".join(str(finding or "").lower().split())
    return hashlib.sha256(f"{category}:{normalized}".encode()).hexdigest()[:24]


def record(*, category: str, finding: str, evidence: list | None = None,
           source: str = "verification", task_id: str = "", candidate_id: str = "",
           revision_request: str = "", scope: str = "global") -> dict:
    """记录与答案无关的失败模式；同类重复出现累计，不覆盖历史证据。"""
    category, finding = str(category or "verification"), str(finding or "").strip()
    if not finding:
        return {"ok": False, "error": "empty finding"}
    identity = _id(category, finding)
    now = time.time()
    with _LOCK:
        data = _load()
        row = data.setdefault("items", {}).setdefault(identity, {
            "id": identity, "category": category, "finding": finding[:2000],
            "scope": str(scope or "global"), "seen": 0, "resolved": 0,
            "first_seen_at": now, "evidence": [], "sources": [], "revisions": [],
        })
        row["seen"] = int(row.get("seen") or 0) + 1
        row["last_seen_at"] = now
        if source and source not in row["sources"]:
            row["sources"].append(str(source)[:200])
        for item in evidence or []:
            proof = str(item).strip()[:2000]
            if proof and proof not in row["evidence"]:
                row["evidence"].append(proof)
        row["evidence"] = row["evidence"][-50:]
        if revision_request:
            row["revisions"].append({"request": str(revision_request)[:4000],
                                     "task_id": str(task_id), "candidate_id": str(candidate_id),
                                     "ts": now, "status": "open"})
            row["revisions"] = row["revisions"][-50:]
        _save(data)
        return {"ok": True, "pitfall": dict(row)}


def resolve(pitfall_id: str, *, revision_task_id: str = "", evidence: list | None = None) -> dict:
    with _LOCK:
        data = _load()
        row = (data.get("items") or {}).get(str(pitfall_id))
        if not row:
            return {"ok": False, "error": "pitfall not found"}
        row["resolved"] = int(row.get("resolved") or 0) + 1
        row["last_resolved_at"] = time.time()
        row["resolved_by"] = str(revision_task_id)
        for item in evidence or []:
            proof = str(item).strip()[:2000]
            if proof and proof not in row["evidence"]:
                row["evidence"].append(proof)
        for revision in reversed(row.get("revisions") or []):
            if revision.get("status") == "open":
                revision["status"] = "resolved"
                revision["resolved_by"] = str(revision_task_id)
                break
        _save(data)
        return {"ok": True, "pitfall": dict(row)}


def search(categories: list[str] | None = None, *, unresolved_only: bool = True,
           limit: int = 20) -> list[dict]:
    allowed = {str(value) for value in categories or [] if str(value)}
    rows = list((_load().get("items") or {}).values())
    if allowed:
        rows = [row for row in rows if row.get("category") in allowed]
    if unresolved_only:
        rows = [row for row in rows if int(row.get("seen") or 0) > int(row.get("resolved") or 0)]
    rows.sort(key=lambda row: (-(int(row.get("seen") or 0) - int(row.get("resolved") or 0)),
                               -float(row.get("last_seen_at") or 0)))
    return rows[:max(1, min(int(limit or 20), 100))]
