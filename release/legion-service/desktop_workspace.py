#!/usr/bin/env python3
"""云电脑生产工作区：持久项目、任务、轨迹、恢复现场与交付包。"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import desktop_fs

TASK_STATES = {
    "queued", "running", "waiting_human", "blocked", "completed",
    "partial", "failed", "cancelled",
}
TERMINAL_STATES = {"completed", "partial", "failed", "cancelled"}
_LOCK = threading.RLock()


def _safe(value: Any, fallback: str) -> str:
    return desktop_fs._safe_id(str(value or fallback), fallback)


def _project_root(uid: str, project_id: str) -> Path:
    root = Path(desktop_fs.prepare_user_root(uid)["root"])
    target = (root / "projects" / _safe(project_id, "project")).resolve()
    projects = (root / "projects").resolve()
    if os.path.commonpath((str(projects), str(target))) != str(projects):
        raise ValueError("project escapes user workspace")
    return target


def _meta_root(uid: str, project_id: str) -> Path:
    return _project_root(uid, project_id) / ".meyo"


def _project_path(uid: str, project_id: str) -> Path:
    return _meta_root(uid, project_id) / "project.json"


def _task_path(uid: str, project_id: str, task_id: str) -> Path:
    return _meta_root(uid, project_id) / "tasks" / f"{_safe(task_id, 'task')}.json"


def _events_path(uid: str, project_id: str, task_id: str) -> Path:
    return _meta_root(uid, project_id) / "events" / f"{_safe(task_id, 'task')}.jsonl"


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _owned_project(uid: str, project_id: str, project: dict) -> bool:
    """Reject copied project metadata that does not belong to its directory owner."""
    return bool(
        project
        and str(project.get("uid") or "") == _safe(uid, "default")
        and str(project.get("project_id") or "") == _safe(project_id, "project")
    )


def _owned_task(uid: str, project_id: str, task_id: str, task: dict) -> bool:
    """Reject task metadata copied inside another task's deliverables/output tree."""
    return bool(
        task
        and str(task.get("uid") or "") == _safe(uid, "default")
        and str(task.get("project_id") or "") == _safe(project_id, "project")
        and str(task.get("task_id") or "") == _safe(task_id, "task")
    )


def _load_owned_task(uid: str, project_id: str, task_id: str) -> dict:
    task = _load(_task_path(uid, project_id, task_id))
    return task if _owned_task(uid, project_id, task_id, task) else {}


def _readme(title: str, brief: str) -> str:
    summary = brief.strip() or "尚未填写项目说明。"
    return (
        f"# {title}\n\n{summary}\n\n"
        "## 目录\n\n"
        "- `deliverables/`：最终可用产物\n"
        "- `evidence/`：截图与验收证据\n"
        "- `.meyo/`：任务状态与轨迹（由系统维护）\n\n"
        "## 状态\n\n任务进度与失败说明请在云电脑工作台查看。\n"
    )


def create_project(uid: str = "default", title: str = "新项目", brief: str = "",
                   project_id: str = "", objective: str = "",
                   success_criteria: list | None = None, owner: str = "") -> dict:
    uid = _safe(uid, "default")
    project_id = _safe(project_id or f"project-{int(time.time())}-{uuid.uuid4().hex[:6]}", "project")
    root = _project_root(uid, project_id)
    now = time.time()
    with _LOCK:
        for name in ("deliverables", "evidence", ".meyo/tasks", ".meyo/events", ".meyo/handoffs"):
            (root / name).mkdir(parents=True, exist_ok=True)
        path = _project_path(uid, project_id)
        if path.exists():
            return {"ok": False, "error": "project already exists", "project_id": project_id}
        project = {
            "project_id": project_id, "uid": uid, "title": str(title or "新项目")[:200],
            "brief": str(brief or "")[:8000], "status": "active", "version": 1,
            "objective_id": project_id, "objective": str(objective or title or "新项目")[:1000],
            "success_criteria": [str(item)[:1000] for item in (success_criteria or [])[:50]],
            "owner": str(owner or uid)[:200], "milestones": [], "next_action": "创建第一个任务",
            "created_at": now, "updated_at": now, "task_ids": [],
        }
        _atomic_json(path, project)
        readme = root / "README.md"
        if not readme.exists():
            readme.write_text(_readme(project["title"], project["brief"]), encoding="utf-8")
    return {"ok": True, "project": project, "path": f"projects/{project_id}"}


def get_project(uid: str, project_id: str) -> dict:
    project = _load(_project_path(uid, project_id))
    if not _owned_project(uid, project_id, project):
        return {"ok": False, "error": "project not found"}
    tasks = []
    for task_id in project.get("task_ids") or []:
        task = _load_owned_task(uid, project_id, task_id)
        if task:
            tasks.append(task)
    return {"ok": True, "project": project, "tasks": tasks,
            "path": f"projects/{project['project_id']}"}


def list_projects(uid: str = "default", limit: int = 50) -> dict:
    uid = _safe(uid, "default")
    base = Path(desktop_fs.prepare_user_root(uid)["root"]) / "projects"
    rows = []
    for child in base.iterdir() if base.is_dir() else []:
        if not child.is_dir() or child.is_symlink():
            continue
        project = _load(child / ".meyo" / "project.json")
        if _owned_project(uid, child.name, project):
            tasks = []
            for task_id in project.get("task_ids") or []:
                task = _load_owned_task(uid, child.name, task_id)
                if task:
                    tasks.append(task)
            project = dict(project)
            project["task_summary"] = {
                "total": len(tasks),
                "running": sum(task.get("state") == "running" for task in tasks),
                "waiting_human": sum(task.get("state") == "waiting_human" for task in tasks),
                "completed": sum(task.get("state") == "completed" for task in tasks),
            }
            rows.append(project)
    rows.sort(key=lambda row: row.get("updated_at", 0), reverse=True)
    return {"ok": True, "uid": uid, "projects": rows[:max(1, min(int(limit or 50), 200))],
            "total": len(rows)}


def create_task(uid: str, project_id: str, goal: str, task_id: str = "",
                team_id: str = "", session_id: str = "", mode: str = "auto",
                execution_spec: dict | None = None) -> dict:
    project_result = get_project(uid, project_id)
    if not project_result.get("ok"):
        return project_result
    task_id = _safe(task_id or f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}", "task")
    path = _task_path(uid, project_id, task_id)
    now = time.time()
    with _LOCK:
        if path.exists():
            return {"ok": False, "error": "task already exists", "task_id": task_id}
        task = {
            "task_id": task_id, "project_id": _safe(project_id, "project"),
            "uid": _safe(uid, "default"), "goal": str(goal or "")[:12000],
            "state": "queued", "phase": "准备", "version": 1, "mode": str(mode or "auto"),
            "team_id": str(team_id or "")[:120], "session_id": str(session_id or "")[:120],
            "completed_steps": [], "artifacts": [], "screenshots": [], "failures": [],
            "active_handoff_ids": [], "checkpoint": {}, "resume_requested_at": None,
            "execution_spec": {**dict(execution_spec or {}),
                               "kind": str((execution_spec or {}).get("kind") or "desktop_agent")[:80],
                               "goal": str((execution_spec or {}).get("goal") or goal or "")[:12000]},
            "attempt_seq": 0, "active_attempt": None, "attempt_history": [],
            "lease_owner": "", "lease_expires_at": 0, "heartbeat_at": 0,
            "next_run_at": now, "observe_before_resume": False,
            "delivery_notified_at": None,
            "created_at": now, "updated_at": now,
        }
        _atomic_json(path, task)
        project = project_result["project"]
        if task_id not in project["task_ids"]:
            project["task_ids"].append(task_id)
        project.update({"updated_at": now, "version": int(project.get("version", 1)) + 1})
        _atomic_json(_project_path(uid, project_id), project)
        append_event(uid, project_id, task_id, "task_created", {"goal": task["goal"]})
    return {"ok": True, "task": task}


def get_task(uid: str, project_id: str, task_id: str, include_events: bool = True) -> dict:
    task = _load_owned_task(uid, project_id, task_id)
    if not task:
        return {"ok": False, "error": "task not found"}
    result = {"ok": True, "task": task}
    if include_events:
        result["events"] = read_events(uid, project_id, task_id)
    return result


def append_event(uid: str, project_id: str, task_id: str, event_type: str,
                 data: dict | None = None) -> dict:
    task = _load_owned_task(uid, project_id, task_id)
    if not task:
        return {"ok": False, "error": "task not found"}
    path = _events_path(uid, project_id, task_id)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        seq = 1
        try:
            with path.open("rb") as handle:
                seq = sum(1 for _ in handle) + 1
        except OSError:
            pass
        payload = dict(data or {})
        try:
            from performance_contract import classify_event
            contract = classify_event(str(event_type or "event"), {"t": event_type, **payload})
            payload.update({key: contract[key] for key in ("stage", "heartbeat", "meaningful")
                            if key not in payload})
        except Exception:
            pass
        row = {"event_id": seq, "type": str(event_type or "event")[:80],
               "data": payload, "at": time.time()}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return {"ok": True, "event": row}


def read_events(uid: str, project_id: str, task_id: str, after: int = 0,
                limit: int = 500) -> list[dict]:
    rows = []
    try:
        for line in _events_path(uid, project_id, task_id).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if int(row.get("event_id", 0)) > int(after or 0):
                rows.append(row)
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    return rows[:max(1, min(int(limit or 500), 1000))]


def update_task(uid: str, project_id: str, task_id: str, *, state: str | None = None,
                phase: str | None = None, checkpoint: dict | None = None,
                completed_steps: list | None = None, artifacts: list | None = None,
                screenshots: list | None = None, failure: dict | None = None,
                expected_version: int | None = None, resume_requested: bool = False) -> dict:
    path = _task_path(uid, project_id, task_id)
    with _LOCK:
        task = _load_owned_task(uid, project_id, task_id)
        if not task:
            return {"ok": False, "error": "task not found"}
        if expected_version is not None and int(task.get("version", 1)) != int(expected_version):
            return {"ok": False, "error": "task version conflict", "task": task}
        if state is not None:
            state = str(state)
            if state not in TASK_STATES:
                return {"ok": False, "error": "invalid task state"}
            task["state"] = state
            if state in TERMINAL_STATES:
                # A terminal transition invalidates any in-flight lease. Otherwise an old
                # worker can finish later with the same attempt id and resurrect a cancelled
                # or finalized task, restarting its desktop and harnesses indefinitely.
                task.update({"active_attempt": None, "lease_owner": "",
                             "lease_expires_at": 0, "heartbeat_at": 0,
                             "next_run_at": 0})
        if phase is not None:
            task["phase"] = str(phase)[:500]
        if checkpoint is not None:
            task["checkpoint"] = checkpoint
        if completed_steps is not None:
            task["completed_steps"] = list(completed_steps)[-500:]
        if artifacts is not None:
            task["artifacts"] = list(artifacts)[-500:]
        if screenshots is not None:
            task["screenshots"] = list(screenshots)[-200:]
        if failure:
            task.setdefault("failures", []).append(dict(failure))
            task["failures"] = task["failures"][-100:]
        if resume_requested:
            task["resume_requested_at"] = time.time()
        task.update({"version": int(task.get("version", 1)) + 1, "updated_at": time.time()})
        _atomic_json(path, task)
        project = _load(_project_path(uid, project_id))
        if project:
            project.update({"updated_at": task["updated_at"],
                            "version": int(project.get("version", 1)) + 1})
            _atomic_json(_project_path(uid, project_id), project)
        append_event(uid, project_id, task_id, "task_updated", {
            "state": task["state"], "phase": task["phase"], "version": task["version"],
        })
    return {"ok": True, "task": task}


def claim_task(uid: str, project_id: str, task_id: str, *, owner: str = "",
               lease_s: int = 120) -> dict:
    """原子领取待执行任务；同一用户只能有一个有效桌面租约。"""
    path = _task_path(uid, project_id, task_id)
    now = time.time()
    with _LOCK:
        task = _load_owned_task(uid, project_id, task_id)
        if not task:
            return {"ok": False, "error": "task not found"}
        if task.get("state") not in {"queued", "running"}:
            return {"ok": False, "error": "task is not runnable", "task": task}
        active = task.get("active_attempt")
        if active is not None and float(task.get("lease_expires_at") or 0) > now:
            return {"ok": False, "error": "task already leased", "task": task}
        for project in list_projects(uid, limit=200).get("projects") or []:
            detail = get_project(uid, project["project_id"])
            for other in detail.get("tasks") or []:
                if other.get("task_id") == task_id:
                    continue
                if (other.get("active_attempt") is not None
                        and float(other.get("lease_expires_at") or 0) > now):
                    return {"ok": False, "error": "desktop already leased", "task": task}
        attempt = int(task.get("attempt_seq") or 0) + 1
        task.update({"attempt_seq": attempt, "active_attempt": attempt,
                     "state": "running", "phase": "后台执行中",
                     "lease_owner": str(owner or os.getpid())[:200],
                     "lease_expires_at": now + max(1, int(lease_s)),
                     "heartbeat_at": now, "started_at": task.get("started_at") or now,
                     "version": int(task.get("version") or 1) + 1, "updated_at": now})
        _atomic_json(path, task)
        append_event(uid, project_id, task_id, "attempt_started", {
            "attempt": attempt, "owner": task["lease_owner"],
            "lease_expires_at": task["lease_expires_at"],
        })
    return {"ok": True, "attempt": attempt, "task": task}


def renew_lease(uid: str, project_id: str, task_id: str, attempt: int,
                lease_s: int = 120) -> dict:
    path = _task_path(uid, project_id, task_id)
    with _LOCK:
        task = _load_owned_task(uid, project_id, task_id)
        if (not task or task.get("state") != "running"
                or int(task.get("active_attempt") or 0) != int(attempt)):
            return {"ok": False, "error": "stale attempt"}
        now = time.time()
        task.update({"heartbeat_at": now, "lease_expires_at": now + max(1, int(lease_s)),
                     "version": int(task.get("version") or 1) + 1, "updated_at": now})
        _atomic_json(path, task)
    return {"ok": True, "task": task}


def finish_attempt(uid: str, project_id: str, task_id: str, attempt: int, *, state: str,
                   phase: str = "", checkpoint: dict | None = None,
                   failure: dict | None = None, next_run_at: float | None = None) -> dict:
    if state not in TASK_STATES:
        return {"ok": False, "error": "invalid task state"}
    path = _task_path(uid, project_id, task_id)
    with _LOCK:
        task = _load_owned_task(uid, project_id, task_id)
        if not task:
            return {"ok": False, "error": "task not found"}
        if int(task.get("active_attempt") or 0) != int(attempt):
            append_event(uid, project_id, task_id, "stale_result_ignored", {"attempt": attempt})
            return {"ok": False, "error": "stale attempt", "task": task}
        now = time.time()
        history = {"attempt": attempt, "owner": task.get("lease_owner") or "",
                   "started_at": task.get("started_at"), "finished_at": now, "state": state}
        task.setdefault("attempt_history", []).append(history)
        task["attempt_history"] = task["attempt_history"][-20:]
        task.update({"state": state, "phase": str(phase or task.get("phase") or "")[:500],
                     "active_attempt": None, "lease_owner": "", "lease_expires_at": 0,
                     "heartbeat_at": 0, "next_run_at": float(next_run_at or 0),
                     "version": int(task.get("version") or 1) + 1, "updated_at": now})
        if checkpoint is not None:
            task["checkpoint"] = checkpoint
        if failure:
            task.setdefault("failures", []).append(dict(failure))
            task["failures"] = task["failures"][-100:]
        _atomic_json(path, task)
        append_event(uid, project_id, task_id, "attempt_finished", {
            "attempt": attempt, "state": state, "phase": task["phase"],
        })
    return {"ok": True, "task": task}


def list_runnable(uid: str = "", now: float | None = None, limit: int = 100) -> dict:
    current, rows = float(now if now is not None else time.time()), []
    users_root = Path(desktop_fs._users_root())
    users = [uid] if uid else [entry.name for entry in users_root.iterdir()
                               if entry.is_dir() and not entry.is_symlink()] if users_root.is_dir() else []
    for owner in users:
        safe_owner = _safe(owner, "default")
        for project in list_projects(safe_owner, limit=200).get("projects") or []:
            project_id = str(project.get("project_id") or "")
            if not _owned_project(safe_owner, project_id, project):
                continue
            detail = get_project(safe_owner, project_id)
            for task in detail.get("tasks") or []:
                task_id = str(task.get("task_id") or "")
                if (not _owned_task(safe_owner, project_id, task_id, task)
                        or task.get("state") != "queued"
                        or float(task.get("next_run_at") or 0) > current):
                    continue
                rows.append(task)
    rows.sort(key=lambda row: (float(row.get("next_run_at") or 0), float(row.get("created_at") or 0)))
    return {"ok": True, "tasks": rows[:max(1, min(int(limit or 100), 500))], "total": len(rows)}


def recover_expired(now: float | None = None) -> dict:
    current, recovered = float(now if now is not None else time.time()), []
    users_root = Path(desktop_fs._users_root())
    users = [entry.name for entry in users_root.iterdir()
             if entry.is_dir() and not entry.is_symlink()] if users_root.is_dir() else []
    with _LOCK:
        for uid in users:
            safe_uid = _safe(uid, "default")
            for project in list_projects(safe_uid, limit=200).get("projects") or []:
                project_id = str(project.get("project_id") or "")
                if not _owned_project(safe_uid, project_id, project):
                    continue
                for task in get_project(safe_uid, project_id).get("tasks") or []:
                    task_id = str(task.get("task_id") or "")
                    if (not _owned_task(safe_uid, project_id, task_id, task)
                            or task.get("state") != "running" or task.get("active_attempt") is None
                            or float(task.get("lease_expires_at") or 0) > current):
                        continue
                    path = _task_path(safe_uid, project_id, task_id)
                    record = {"attempt": task["active_attempt"], "state": "interrupted",
                              "owner": task.get("lease_owner") or "", "recovered_at": current,
                              "side_effects_unknown": True}
                    task.setdefault("attempt_history", []).append(record)
                    task["attempt_history"] = task["attempt_history"][-20:]
                    checkpoint = dict(task.get("checkpoint") or {})
                    checkpoint["observe_before_resume"] = True
                    task.update({"state": "queued", "phase": "中断后等待恢复",
                                 "active_attempt": None, "lease_owner": "", "lease_expires_at": 0,
                                 "heartbeat_at": 0, "checkpoint": checkpoint,
                                 "observe_before_resume": True, "next_run_at": current,
                                 "version": int(task.get("version") or 1) + 1, "updated_at": current})
                    _atomic_json(path, task)
                    append_event(safe_uid, project_id, task_id, "attempt_recovered", record)
                    recovered.append({"uid": safe_uid, "project_id": project_id,
                                      "task_id": task_id, **record})
    return {"ok": True, "count": len(recovered), "recovered": recovered}


def mark_delivery_notified(uid: str, project_id: str, task_id: str, attempt: int) -> dict:
    path = _task_path(uid, project_id, task_id)
    with _LOCK:
        task = _load_owned_task(uid, project_id, task_id)
        if not task:
            return {"ok": False, "error": "task not found"}
        if task.get("delivery_notified_at"):
            return {"ok": True, "status": "unchanged", "task": task}
        task.update({"delivery_notified_at": time.time(), "delivery_attempt": int(attempt),
                     "version": int(task.get("version") or 1) + 1, "updated_at": time.time()})
        _atomic_json(path, task)
    return {"ok": True, "task": task}


def mark_running(uid: str, project_id: str, task_id: str, checkpoint: dict | None = None) -> dict:
    return update_task(uid, project_id, task_id, state="running", phase="执行中",
                       checkpoint=checkpoint or {})


def wake_task(uid: str, project_id: str, task_id: str, *, phase: str = "依赖完成，等待续跑",
              checkpoint_patch: dict | None = None) -> dict:
    current = get_task(uid, project_id, task_id, include_events=False)
    if not current.get("ok"):
        return current
    task = current["task"]
    if task.get("state") not in {"queued", "waiting_human", "blocked"}:
        return {"ok": False, "error": "task is not waiting", "task": task}
    checkpoint = {**dict(task.get("checkpoint") or {}), **dict(checkpoint_patch or {})}
    result = update_task(uid, project_id, task_id, state="queued", phase=phase,
                         checkpoint=checkpoint, expected_version=task.get("version"))
    if result.get("ok"):
        path = _task_path(uid, project_id, task_id)
        updated = result["task"]
        updated["next_run_at"] = time.time()
        updated["version"] = int(updated.get("version", 1)) + 1
        updated["updated_at"] = time.time()
        _atomic_json(path, updated)
        append_event(uid, project_id, task_id, "dependency_ready", checkpoint_patch or {})
        result["task"] = updated
    return result


def request_resume(uid: str, project_id: str, task_id: str) -> dict:
    current = get_task(uid, project_id, task_id, include_events=False)
    if not current.get("ok"):
        return current
    task = current["task"]
    if task.get("state") not in {"waiting_human", "blocked", "running"}:
        return {"ok": False, "error": "task is not resumable", "task": task}
    result = update_task(uid, project_id, task_id, state="queued", phase="等待恢复",
                         expected_version=task.get("version"), resume_requested=True)
    if result.get("ok"):
        append_event(uid, project_id, task_id, "resume_requested", {
            "checkpoint": result["task"].get("checkpoint") or {},
        })
    return result


def recoverable_tasks(uid: str = "default") -> dict:
    rows = []
    for project in list_projects(uid, limit=200).get("projects") or []:
        detail = get_project(uid, project["project_id"])
        for task in detail.get("tasks") or []:
            if task.get("state") in {"running", "waiting_human", "blocked", "queued"}:
                rows.append(task)
    rows.sort(key=lambda row: row.get("updated_at", 0), reverse=True)
    return {"ok": True, "uid": _safe(uid, "default"), "tasks": rows, "total": len(rows)}


def _collect_project_files(root: Path) -> list[dict]:
    files = []
    for base, directories, names in os.walk(root):
        directories[:] = [name for name in directories if name != ".meyo"]
        for name in names:
            path = Path(base) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            files.append({"name": name, "path": relative, "bytes": path.stat().st_size})
    return sorted(files, key=lambda row: row["path"])


def finalize(uid: str, project_id: str, task_id: str, status: str = "completed",
             failure_summary: str = "") -> dict:
    current = get_task(uid, project_id, task_id, include_events=False)
    if not current.get("ok"):
        return current
    root = _project_root(uid, project_id)
    files = _collect_project_files(root)
    deliverables = [row for row in files if row["path"].startswith("deliverables/")]
    evidence = [row for row in files if row["path"].startswith("evidence/")]
    readme = next((row for row in files if row["path"] == "README.md"), None)
    has_screenshot = any(row["path"].lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
                         for row in evidence)
    requested = status if status in {"completed", "partial", "failed", "cancelled"} else "failed"
    checks = {
        "readme": bool(readme and readme["bytes"] > 0),
        "deliverables": bool(deliverables),
        "evidence": bool(evidence),
        "screenshot": has_screenshot,
    }
    final_state = requested
    if requested == "completed" and not all(checks.values()):
        final_state = "partial" if (deliverables or evidence) else "failed"
    failure = {"summary": str(failure_summary)[:4000], "checks": checks} if failure_summary or final_state != "completed" else None
    artifacts = [{"name": row["name"], "path": str(root / row["path"]),
                  "relative_path": f"projects/{_safe(project_id, 'project')}/{row['path']}",
                  "bytes": row["bytes"]} for row in deliverables + evidence]
    result = update_task(uid, project_id, task_id, state=final_state,
                         phase="已交付" if final_state == "completed" else "交付未完整",
                         artifacts=artifacts, failure=failure)
    manifest = {
        "project_id": _safe(project_id, "project"), "task_id": _safe(task_id, "task"),
        "state": final_state, "checks": checks, "files": files,
        "failure_summary": str(failure_summary or ""), "finalized_at": time.time(),
    }
    _atomic_json(_meta_root(uid, project_id) / "delivery.json", manifest)
    append_event(uid, project_id, task_id, "task_finalized", manifest)
    return {"ok": final_state == "completed", "state": final_state,
            "task": result.get("task"), "manifest": manifest,
            "project_path": f"projects/{_safe(project_id, 'project')}",
            "readme": f"projects/{_safe(project_id, 'project')}/README.md",
            "artifacts": artifacts}
