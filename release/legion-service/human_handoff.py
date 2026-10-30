#!/usr/bin/env python3
"""云电脑人类接管：登录、验证码与高后果外发动作的持久待办。"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import desktop_fs
import desktop_workspace

HANDOFF_KINDS = {
    "login", "register", "captcha", "mfa", "payment", "purchase",
    "send_message", "privacy", "external_publish", "account_change", "other",
}
HANDOFF_STATES = {"pending", "completed", "declined", "expired", "cancelled"}
SENSITIVE_KEYS = {
    "password", "passwd", "passcode", "otp", "code", "captcha", "token", "secret",
    "cookie", "authorization", "card", "cvv", "pin", "credential", "session_cookie",
}
_LOCK = threading.RLock()


def _safe(value: Any, fallback: str) -> str:
    return desktop_fs._safe_id(str(value or fallback), fallback)


def _root(uid: str, project_id: str) -> Path:
    return Path(desktop_fs.prepare_user_root(uid)["root"]) / "projects" / _safe(project_id, "project") / ".meyo" / "handoffs"


def _path(uid: str, project_id: str, handoff_id: str) -> Path:
    return _root(uid, project_id) / f"{_safe(handoff_id, 'handoff')}.json"


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _clean(value: Any, key: str = "") -> Any:
    lowered = str(key or "").lower()
    if any(token in lowered for token in SENSITIVE_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(name)[:100]: _clean(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value[:100]]
    text = str(value) if value is not None else ""
    return text[:2000]


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def request(uid: str, project_id: str, task_id: str, kind: str, title: str,
            instructions: str, *, app: str = "", target: str = "",
            consequence: str = "", checkpoint: dict | None = None,
            completion_check: dict | None = None, ttl_s: int = 86400) -> dict:
    kind = str(kind or "other").lower()
    if kind not in HANDOFF_KINDS:
        return {"ok": False, "error": "invalid handoff kind"}
    now = time.time()
    handoff_id = f"handoff-{uuid.uuid4().hex[:12]}"
    row = {
        "handoff_id": handoff_id, "uid": _safe(uid, "default"),
        "project_id": _safe(project_id, "project"), "task_id": _safe(task_id, "task"),
        "kind": kind, "title": str(title or "需要你处理")[:300],
        "instructions": str(instructions or "")[:4000], "app": str(app or "")[:200],
        "target": str(target or "")[:500], "consequence": str(consequence or "")[:2000],
        "checkpoint": _clean(checkpoint or {}), "completion_check": _clean(completion_check or {}),
        "state": "pending", "version": 1, "requested_at": now,
        "expires_at": now + max(60, min(int(ttl_s or 86400), 7 * 86400)),
        "resolved_at": None, "resolved_by": "", "resolution_note": "",
    }
    with _LOCK:
        # 乐观锁竞态修复(desktop_worker 与用户并发):get_task→update_task 之间 worker 会
        # claim/heartbeat 改 version，单次提交会撞 "task version conflict"。标准做法:
        # 冲突时重读最新 task 再试(小次数上限防活锁)，用户侧 handoff 不应因后台 worker
        # 正常推进而失败。最终仍冲突才如实返回让上层重试。
        for _lock_attempt in range(3):
            task_result = desktop_workspace.get_task(uid, project_id, task_id, include_events=False)
            if not task_result.get("ok"):
                return task_result
            task = task_result["task"]
            ids = list(task.get("active_handoff_ids") or [])
            candidate_ids = ids + [handoff_id]
            updated = desktop_workspace.update_task(
                uid, project_id, task_id, state="waiting_human", phase="等待你处理",
                checkpoint={**(task.get("checkpoint") or {}), **row["checkpoint"],
                            "active_handoff_ids": candidate_ids, "observe_before_resume": True},
                expected_version=task.get("version"),
            )
            if updated.get("ok"):
                break
            if str(updated.get("error") or "") != "task version conflict":
                return updated
        else:
            return updated
        _atomic(_path(uid, project_id, handoff_id), row)
        updated_task = updated["task"]
        updated_task["active_handoff_ids"] = candidate_ids
        desktop_workspace._atomic_json(desktop_workspace._task_path(uid, project_id, task_id), updated_task)
        desktop_workspace.append_event(uid, project_id, task_id, "human_handoff_requested", {
            "handoff_id": handoff_id, "kind": kind, "title": row["title"],
            "consequence": row["consequence"],
        })
    return {"ok": True, "handoff": row, "task": updated_task}


def get(uid: str, project_id: str, handoff_id: str) -> dict:
    expire(uid, project_id)
    row = _load(_path(uid, project_id, handoff_id))
    return {"ok": bool(row), "handoff": row} if row else {"ok": False, "error": "handoff not found"}


def list_handoffs(uid: str, project_id: str = "", task_id: str = "",
                  pending_only: bool = False) -> dict:
    rows = []
    projects = [project_id] if project_id else [item["project_id"] for item in desktop_workspace.list_projects(uid, 200).get("projects") or []]
    for pid in projects:
        expire(uid, pid)
        root = _root(uid, pid)
        for path in root.glob("*.json") if root.is_dir() else []:
            row = _load(path)
            if not row or (task_id and row.get("task_id") != _safe(task_id, "task")):
                continue
            if pending_only and row.get("state") != "pending":
                continue
            rows.append(row)
    rows.sort(key=lambda row: row.get("requested_at", 0), reverse=True)
    return {"ok": True, "handoffs": rows, "total": len(rows)}


def decide(uid: str, project_id: str, handoff_id: str, decision: str,
           *, actor: str = "user", note: str = "",
           expected_version: int | None = None) -> dict:
    decision = str(decision or "").lower()
    mapping = {"complete": "completed", "completed": "completed", "done": "completed",
               "decline": "declined", "declined": "declined", "cancel": "cancelled",
               "cancelled": "cancelled"}
    state = mapping.get(decision)
    if not state:
        return {"ok": False, "error": "decision must be complete/decline/cancel"}
    path = _path(uid, project_id, handoff_id)
    with _LOCK:
        row = _load(path)
        if not row:
            return {"ok": False, "error": "handoff not found"}
        if expected_version is not None and int(row.get("version", 1)) != int(expected_version):
            return {"ok": False, "error": "handoff version conflict", "handoff": row}
        if row.get("state") != "pending":
            return {"ok": row.get("state") == state, "status": "unchanged", "handoff": row}
        if time.time() >= float(row.get("expires_at") or 0):
            state = "expired"
        row.update({"state": state, "version": int(row.get("version", 1)) + 1,
                    "resolved_at": time.time(), "resolved_by": str(actor or "user")[:200],
                    "resolution_note": str(note or "")[:2000]})
        _atomic(path, row)
        task_result = desktop_workspace.get_task(uid, project_id, row["task_id"], include_events=False)
        task = task_result.get("task") or {}
        remaining = [value for value in task.get("active_handoff_ids") or [] if value != handoff_id]
        if state == "completed":
            next_state = "queued" if not remaining else "waiting_human"
            phase = "等待恢复" if not remaining else "仍有待办"
        elif state in {"declined", "cancelled", "expired"}:
            next_state, phase = "blocked", "人类待办未完成"
        else:
            next_state, phase = task.get("state", "waiting_human"), task.get("phase", "等待你处理")
        updated = desktop_workspace.update_task(
            uid, project_id, row["task_id"], state=next_state, phase=phase,
            checkpoint={**(task.get("checkpoint") or {}), "active_handoff_ids": remaining,
                        "last_handoff": {"id": handoff_id, "state": state},
                        "observe_before_resume": state == "completed" and not remaining},
            expected_version=task.get("version"), resume_requested=state == "completed" and not remaining,
        ) if task else {"ok": False, "error": "task not found"}
        if updated.get("ok"):
            updated_task = updated["task"]
            updated_task["active_handoff_ids"] = remaining
            if state == "completed" and not remaining:
                updated_task["observe_before_resume"] = True
            desktop_workspace._atomic_json(desktop_workspace._task_path(uid, project_id, row["task_id"]), updated_task)
        desktop_workspace.append_event(uid, project_id, row["task_id"], "human_handoff_resolved", {
            "handoff_id": handoff_id, "kind": row["kind"], "state": state,
        })
    return {"ok": state == mapping[decision], "handoff": row, "task": updated.get("task"),
            "resume_requested": bool(state == "completed" and not remaining)}


def expire(uid: str, project_id: str) -> int:
    changed = 0
    root = _root(uid, project_id)
    now = time.time()
    with _LOCK:
        for path in root.glob("*.json") if root.is_dir() else []:
            row = _load(path)
            if row.get("state") == "pending" and now >= float(row.get("expires_at") or 0):
                row.update({"state": "expired", "resolved_at": now,
                            "version": int(row.get("version", 1)) + 1})
                _atomic(path, row)
                desktop_workspace.append_event(uid, project_id, row.get("task_id", ""),
                                               "human_handoff_expired", {"handoff_id": row.get("handoff_id")})
                changed += 1
    return changed
