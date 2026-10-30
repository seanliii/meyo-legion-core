#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云手机持久任务：状态、确认、执行尝试和断点恢复的单一事实源。"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import tempfile
import threading
import time
import uuid

import legion_paths

_STATES = {
    "needs_software", "needs_account", "ready", "running", "needs_confirmation",
    "done", "partial", "blocked",
}
_TERMINAL = {"done", "partial", "blocked"}
_CONFIRMATIONS = {"login", "otp", "payment", "order", "message", "privacy"}
_TRANSITIONS = {
    "needs_software": {"needs_account", "ready", "blocked"},
    "needs_account": {"ready", "blocked"},
    "ready": {"running", "blocked"},
    "running": {"needs_software", "needs_account", "needs_confirmation", "done", "partial", "blocked"},
    "needs_confirmation": {"ready", "running", "partial", "blocked"},
    "done": set(), "partial": set(), "blocked": set(),
}
_LOCK = threading.RLock()


def _root() -> pathlib.Path:
    return pathlib.Path(legion_paths.under("mobile", "tasks"))


def _safe(value: str) -> str:
    value = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("invalid mobile task id")
    return value


def _path(task_id: str) -> pathlib.Path:
    return _root() / _safe(task_id) / "task.json"


def _read(task_id: str) -> dict | None:
    path = _path(task_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write(task: dict) -> None:
    path = _path(str(task["id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".task-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(task, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _event(task: dict, kind: str, data: dict | None = None) -> dict:
    task["event_seq"] = int(task.get("event_seq") or 0) + 1
    payload = copy.deepcopy(data or {})
    try:
        from performance_contract import classify_event
        contract = classify_event(str(kind), {"t": kind, **payload})
        payload.update({key: contract[key] for key in ("stage", "heartbeat", "meaningful")
                        if key not in payload})
    except Exception:
        pass
    row = {"seq": task["event_seq"], "type": str(kind), "at": time.time(),
           "data": payload}
    task.setdefault("events", []).append(row)
    task["events"] = task["events"][-500:]
    return row


def create(goal: str, *, uid: str = "default", required_apps: list[dict] | None = None,
           context: dict | None = None, task_id: str = "") -> dict:
    goal = str(goal or "").strip()
    if not goal:
        return {"ok": False, "error": "手机任务目标不能为空"}
    required_apps = [dict(item) for item in (required_apps or []) if isinstance(item, dict)]
    task_id = _safe(task_id) if task_id else "mobile-" + uuid.uuid4().hex[:16]
    with _LOCK:
        existing = _read(task_id)
        if existing:
            return {"ok": True, "created": False, "task": existing}
        now = time.time()
        state = "needs_software" if required_apps else "ready"
        task = {
            "id": task_id, "uid": str(uid or "default")[:120], "goal": goal,
            "state": state, "version": 1, "created_at": now, "updated_at": now,
            "required_apps": required_apps, "context": copy.deepcopy(context or {}),
            "checkpoint": {}, "confirmation": None, "attempts": [], "artifacts": [],
            "verification": {}, "event_seq": 0, "events": [],
        }
        _event(task, "task_created", {"state": state, "required_apps": required_apps})
        _write(task)
    return {"ok": True, "created": True, "task": task}


def get(task_id: str, *, uid: str = "") -> dict:
    with _LOCK:
        task = _read(task_id)
    if not task or (uid and str(task.get("uid")) != str(uid)):
        return {"ok": False, "error": "mobile task not found"}
    return {"ok": True, "task": task}


def list_tasks(*, uid: str = "default", state: str = "", limit: int = 100) -> dict:
    rows = []
    root = _root()
    if root.is_dir():
        for item in root.iterdir():
            if not item.is_dir():
                continue
            try:
                task = json.loads((item / "task.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if uid and str(task.get("uid")) != str(uid):
                continue
            if state and str(task.get("state")) != str(state):
                continue
            rows.append(task)
    rows.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    cap = max(1, min(int(limit or 100), 500))
    return {"ok": True, "tasks": rows[:cap], "total": len(rows)}


def transition(task_id: str, state: str, *, reason: str = "", checkpoint: dict | None = None,
               evidence: dict | None = None, expected_version: int | None = None) -> dict:
    state = str(state or "")
    if state not in _STATES:
        return {"ok": False, "error": "invalid mobile task state"}
    with _LOCK:
        task = _read(task_id)
        if not task:
            return {"ok": False, "error": "mobile task not found"}
        current = str(task.get("state") or "")
        if expected_version is not None and int(task.get("version") or 1) != int(expected_version):
            return {"ok": False, "error": "mobile task version conflict", "task": task}
        if current == state:
            return {"ok": True, "status": "unchanged", "task": task}
        if current in _TERMINAL or state not in _TRANSITIONS.get(current, set()):
            return {"ok": False, "error": f"illegal mobile task transition: {current}->{state}", "task": task}
        task["state"] = state
        task["version"] = int(task.get("version") or 1) + 1
        task["updated_at"] = time.time()
        if checkpoint is not None:
            task["checkpoint"] = copy.deepcopy(checkpoint)
        if evidence:
            task["verification"] = copy.deepcopy(evidence)
        if state != "needs_confirmation":
            task["confirmation"] = None
        _event(task, "state_changed", {"from": current, "to": state, "reason": str(reason)[:2000]})
        _write(task)
    return {"ok": True, "task": task}


def request_confirmation(task_id: str, kind: str, *, summary: str, details: dict | None = None,
                         resume_hint: str = "", expected_version: int | None = None) -> dict:
    kind = str(kind or "").lower()
    if kind not in _CONFIRMATIONS:
        return {"ok": False, "error": "invalid mobile confirmation kind"}
    with _LOCK:
        task = _read(task_id)
        if not task:
            return {"ok": False, "error": "mobile task not found"}
        if expected_version is not None and int(task.get("version") or 1) != int(expected_version):
            return {"ok": False, "error": "mobile task version conflict", "task": task}
        current = str(task.get("state") or "")
        active = task.get("confirmation") or {}
        if current == "needs_confirmation" and active.get("status") == "pending":
            return {"ok": True, "status": "unchanged", "task": task, "confirmation": active}
        # verifier 已把任务停在“动作已准备、等待确认”边界时，run() 会先收束
        # 当前 attempt 并回到 ready；这里允许从该稳定断点创建确认，避免为了展示
        # 对话框而伪造一个仍在运行的进程。其他 ready 任务不能越过这道门。
        prepared = current == "ready" and (task.get("verification") or {}).get("prepared") is True
        if current != "running" and not prepared:
            return {"ok": False, "error": f"confirmation requires running/prepared task, got {current}"}
        now = time.time()
        row = {"id": "confirm-" + uuid.uuid4().hex[:16], "kind": kind,
               "summary": str(summary or "")[:2000], "details": copy.deepcopy(details or {}),
               "resume_hint": str(resume_hint or "")[:2000], "status": "pending",
               "requested_at": now, "decided_at": None, "decision": "", "actor": ""}
        task["confirmation"] = row
        task["state"] = "needs_confirmation"
        task["version"] = int(task.get("version") or 1) + 1
        task["updated_at"] = now
        _event(task, "confirmation_required", row)
        _write(task)
    return {"ok": True, "task": task, "confirmation": row}


def decide(task_id: str, confirmation_id: str, decision: str, *, actor: str = "user",
           note: str = "", expected_version: int | None = None) -> dict:
    decision = str(decision or "").lower()
    aliases = {"approve": "approved", "approved": "approved", "complete": "completed",
               "completed": "completed", "deny": "denied", "denied": "denied"}
    decision = aliases.get(decision, "")
    if not decision:
        return {"ok": False, "error": "decision must be approve/complete/deny"}
    with _LOCK:
        task = _read(task_id)
        if not task:
            return {"ok": False, "error": "mobile task not found"}
        row = task.get("confirmation") or {}
        if str(row.get("id") or "") != str(confirmation_id or ""):
            return {"ok": False, "error": "mobile confirmation not found"}
        if expected_version is not None and int(task.get("version") or 1) != int(expected_version):
            return {"ok": False, "error": "mobile task version conflict", "task": task}
        if row.get("status") != "pending":
            return {"ok": row.get("decision") == decision, "status": "unchanged", "task": task}
        now = time.time()
        row.update({"status": "decided", "decision": decision, "decided_at": now,
                    "actor": str(actor or "user")[:200], "note": str(note or "")[:2000]})
        previous = task["state"]
        task["state"] = "ready" if decision in {"approved", "completed"} else "partial"
        task["version"] = int(task.get("version") or 1) + 1
        task["updated_at"] = now
        _event(task, "confirmation_decided", {"confirmation_id": confirmation_id,
                                               "decision": decision, "from": previous,
                                               "to": task["state"]})
        _write(task)
    return {"ok": True, "task": task, "resume_required": task["state"] == "ready"}


def start_attempt(task_id: str, *, runner: str = "mobilerun", checkpoint: dict | None = None) -> dict:
    with _LOCK:
        task = _read(task_id)
        if not task:
            return {"ok": False, "error": "mobile task not found"}
        if task.get("state") not in {"ready", "running"}:
            return {"ok": False, "error": f"task is not runnable: {task.get('state')}", "task": task}
        if task.get("state") == "running":
            live = next((item for item in reversed(task.get("attempts") or [])
                         if item.get("status") == "running"), None)
            return {"ok": True, "status": "unchanged", "task": task, "attempt": live}
        now = time.time()
        attempt = {"id": "attempt-" + uuid.uuid4().hex[:16], "runner": str(runner),
                   "status": "running", "started_at": now, "finished_at": None,
                   "owner_pid": os.getpid(), "result": {}}
        task.setdefault("attempts", []).append(attempt)
        task["state"] = "running"
        task["version"] = int(task.get("version") or 1) + 1
        task["updated_at"] = now
        if checkpoint is not None:
            task["checkpoint"] = copy.deepcopy(checkpoint)
        _event(task, "attempt_started", attempt)
        _write(task)
    return {"ok": True, "task": task, "attempt": attempt}


def finish_attempt(task_id: str, attempt_id: str, *, state: str, result: dict | None = None,
                   artifacts: list | None = None) -> dict:
    if state not in _TERMINAL | {"ready", "needs_account", "needs_software"}:
        return {"ok": False, "error": "invalid attempt outcome"}
    with _LOCK:
        task = _read(task_id)
        if not task:
            return {"ok": False, "error": "mobile task not found"}
        attempt = next((item for item in task.get("attempts") or [] if item.get("id") == attempt_id), None)
        if not attempt:
            return {"ok": False, "error": "mobile attempt not found"}
        if attempt.get("status") != "running":
            return {"ok": True, "status": "unchanged", "task": task, "attempt": attempt}
        now = time.time()
        attempt.update({"status": "completed" if state == "done" else state,
                        "finished_at": now, "result": copy.deepcopy(result or {})})
        task["state"] = state
        task["version"] = int(task.get("version") or 1) + 1
        task["updated_at"] = now
        verification = (result or {}).get("verification")
        if isinstance(verification, dict):
            task["verification"] = copy.deepcopy(verification)
        for item in artifacts or []:
            if item and item not in task["artifacts"]:
                task["artifacts"].append(item)
        _event(task, "attempt_finished", {"attempt_id": attempt_id, "state": state,
                                          "result": result or {}})
        _write(task)
    return {"ok": True, "task": task, "attempt": attempt}


def recover_interrupted(owner_alive=None) -> dict:
    """把已失去执行进程的 running 任务标为 ready，但留下副作用未知证据。"""
    owner_alive = owner_alive or _pid_alive
    recovered = []
    with _LOCK:
        for task in list_tasks(uid="", state="running", limit=500).get("tasks", []):
            attempt = next((item for item in reversed(task.get("attempts") or [])
                            if item.get("status") == "running"), None)
            if not attempt or owner_alive(int(attempt.get("owner_pid") or 0)):
                continue
            now = time.time()
            attempt.update({"status": "unknown", "finished_at": now,
                            "result": {"side_effects_unknown": True,
                                       "error": "执行进程中断；可能已产生外部副作用，恢复前必须重新观察业务状态"}})
            task["state"] = "ready"
            task["version"] = int(task.get("version") or 1) + 1
            task["updated_at"] = now
            task.setdefault("checkpoint", {})["observe_before_resume"] = True
            _event(task, "attempt_interrupted", {"attempt_id": attempt["id"],
                                                  "side_effects_unknown": True})
            _write(task)
            recovered.append(task["id"])
    return {"ok": True, "recovered": recovered, "count": len(recovered)}


def run(task_id: str, *, steps=20, timeout_s=600, model="", runner=None) -> dict:
    """执行或恢复同一个持久任务；成功只由业务 verifier 判定。"""
    started = start_attempt(task_id, runner="mobilerun")
    if not started.get("ok"):
        return started
    task = started["task"]
    control = task.get("control") or {}
    if control.get("paused") or control.get("cancel_requested"):
        attempt = started.get("attempt") or {}
        state = "partial" if control.get("cancel_requested") else "ready"
        result = {"ok": False, "cancelled": bool(control.get("cancel_requested")),
                  "paused": bool(control.get("paused")),
                  "error": "任务已撤销" if control.get("cancel_requested") else "任务已暂停"}
        if attempt:
            finish_attempt(task_id, attempt["id"], state=state, result=result)
        return {"ok": False, "state": state, "run": result,
                "task": get(task_id).get("task", task), "business_verified": False}
    attempt = started.get("attempt") or {}
    if not attempt:
        return {"ok": False, "error": "mobile attempt missing", "task": task}
    checkpoint = task.get("checkpoint") or {}
    context = []
    if checkpoint.get("observe_before_resume"):
        context.append("上次执行进程中断，外部副作用未知。先观察当前 App 和业务状态，禁止直接重放提交动作。")
    if checkpoint.get("completed_facts"):
        context.append("已确认完成的业务事实：" + json.dumps(checkpoint["completed_facts"], ensure_ascii=False))
    if task.get("confirmation") and (task["confirmation"] or {}).get("decision"):
        context.append("用户对上个确认点的决定：" + json.dumps(task["confirmation"], ensure_ascii=False))
    command = task["goal"] + ("\n\n恢复上下文：\n" + "\n".join(context) if context else "")
    runner = runner or _mobile_runner
    result = runner(command, steps=steps, timeout_s=timeout_s, model=model)
    latest = get(task_id).get("task") or task
    control = latest.get("control") or {}
    if control.get("cancel_requested"):
        result = {**result, "ok": False, "cancelled": True,
                  "error": "用户已撤销；执行器在安全停点结束"}
    elif control.get("paused"):
        result = {**result, "ok": False, "paused": True,
                  "error": "用户已暂停；执行器在安全停点结束"}
    deliveries = result.get("deliveries") or {}
    artifacts = [value for value in deliveries.values() if isinstance(value, str) and value]
    next_state = "ready"
    if result.get("cancelled"):
        next_state = "partial"
    elif result.get("paused"):
        next_state = "ready"
    elif result.get("blocked") or result.get("status") == "blocked_environment":
        next_state = "blocked"
    elif result.get("needs_account"):
        next_state = "needs_account"
    elif result.get("verification", {}).get("ok"):
        next_state = "done"
    elif result.get("verification", {}).get("prepared"):
        next_state = "ready"
    elif result.get("ok"):
        next_state = "partial"
    finished = finish_attempt(task_id, attempt["id"], state=next_state,
                              result=result, artifacts=artifacts)
    return {"ok": next_state == "done", "state": next_state, "run": result,
            "task": finished.get("task", task),
            "business_verified": next_state == "done"}


def resume(task_id: str, *, steps=20, timeout_s=600, model="", runner=None) -> dict:
    current = get(task_id)
    if not current.get("ok"):
        return current
    task = current["task"]
    if task.get("state") != "ready":
        return {"ok": False, "error": f"task is not resumable: {task.get('state')}", "task": task}
    return run(task_id, steps=steps, timeout_s=timeout_s, model=model, runner=runner)


def replay(task_id: str, *, on_mismatch="agent", start_from=1, max_steps=0) -> dict:
    task = get(task_id)
    if not task.get("ok"):
        return task
    macro = next((item for item in reversed(task["task"].get("artifacts") or [])
                  if str(item).endswith("macro.json")), "")
    if not macro:
        return {"ok": False, "error": "task has no replayable macro"}
    return _macro_runner(macro, on_mismatch=on_mismatch,
                         start_from=start_from, max_steps=max_steps)


def _mobile_runner(command: str, **kwargs) -> dict:
    import mobile_device
    return mobile_device.task(command, **kwargs)


def _macro_runner(path: str, **kwargs) -> dict:
    import mobile_device
    return mobile_device.replay_macro(path, **kwargs)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
