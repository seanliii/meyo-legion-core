#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云手机生活伙伴：通知、每日摘要、健康与风险偏好的持久运营层。"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import tempfile
import threading
import time

import legion_paths
import mobile_apps
import mobile_device
import mobile_tasks

_LOCK = threading.RLock()
_RED_LINES = {"login", "otp", "payment", "order", "message", "privacy"}
_RISK_LEVELS = {"always_confirm", "notify", "allow_safe"}


def _root() -> pathlib.Path:
    return pathlib.Path(legion_paths.under("mobile", "companion"))


def _safe_uid(uid: str) -> str:
    value = str(uid or "default")[:120]
    return value if re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value) else "default"


def _path(uid: str) -> pathlib.Path:
    return _root() / _safe_uid(uid) / "state.json"


def _read(uid: str) -> dict:
    try:
        value = json.loads(_path(uid).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(uid: str, value: dict) -> None:
    path = _path(uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".companion-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def preferences(uid="default") -> dict:
    state = _read(uid)
    return {"ok": True, "preferences": state.get("preferences") or {
        "daily_summary": True, "long_task_minutes": 20, "storage_warn_percent": 85,
        "risk": {kind: "always_confirm" for kind in sorted(_RED_LINES)}, "apps": {},
    }}


def save_preferences(uid="default", value: dict | None = None) -> dict:
    value = dict(value or {})
    current = preferences(uid)["preferences"]
    merged = {**current}
    if "daily_summary" in value:
        merged["daily_summary"] = bool(value["daily_summary"])
    for name, low, high in (("long_task_minutes", 1, 1440), ("storage_warn_percent", 50, 99)):
        if name in value:
            try:
                merged[name] = max(low, min(high, int(value[name])))
            except (TypeError, ValueError):
                return {"ok": False, "error": f"invalid {name}"}
    risk = dict(current.get("risk") or {})
    for kind, level in (value.get("risk") or {}).items():
        kind, level = str(kind), str(level)
        if kind not in _RED_LINES or level not in _RISK_LEVELS:
            return {"ok": False, "error": "invalid confirmation preference"}
        # 红线动作永远不能被长期偏好自动放行；最多改变通知方式。
        risk[kind] = "always_confirm"
    merged["risk"] = risk
    apps = value.get("apps")
    if isinstance(apps, dict):
        merged["apps"] = {str(key)[:180]: dict(row) for key, row in apps.items()
                          if re.fullmatch(r"[A-Za-z0-9_.]{3,180}", str(key)) and isinstance(row, dict)}
    with _LOCK:
        state = _read(uid)
        state["preferences"] = merged
        state["updated_at"] = time.time()
        _write(uid, state)
    return {"ok": True, "preferences": merged}


def _notification(task: dict, now: float, long_seconds: int) -> dict | None:
    state = str(task.get("state") or "")
    kind, title, severity = "", "", "info"
    if state == "needs_confirmation":
        kind, title, severity = "confirmation", "等待你的确认", "action"
    elif state == "needs_account":
        kind, title, severity = "account", "需要你处理登录或账号", "action"
    elif state == "needs_software":
        kind, title, severity = "software", "需要先安装可信 App", "action"
    elif state == "blocked":
        kind, title, severity = "blocked", "任务被环境或前置条件阻塞", "warning"
    elif state == "partial":
        kind, title, severity = "partial", "任务只完成了一部分", "warning"
    elif state == "running" and now - float(task.get("updated_at") or now) >= long_seconds:
        kind, title, severity = "long-running", "任务执行时间较长", "info"
    if not kind:
        return None
    return {"id": f"{kind}:{task['id']}:{task.get('version', 1)}", "kind": kind,
            "title": title, "severity": severity, "task_id": task["id"],
            "goal": str(task.get("goal") or ""), "state": state,
            "updated_at": float(task.get("updated_at") or 0),
            "confirmation": task.get("confirmation") if kind == "confirmation" else None}


def inbox(uid="default", *, include_read=False, now: float | None = None) -> dict:
    now = float(now or time.time())
    prefs = preferences(uid)["preferences"]
    rows = []
    for task in mobile_tasks.list_tasks(uid=uid, limit=500).get("tasks", []):
        row = _notification(task, now, int(prefs["long_task_minutes"]) * 60)
        if row:
            rows.append(row)
    seen = set((_read(uid).get("seen") or []))
    for row in rows:
        row["read"] = row["id"] in seen
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    visible = rows if include_read else [row for row in rows if not row["read"]]
    return {"ok": True, "items": visible, "total": len(visible),
            "unread": sum(not row["read"] for row in rows), "all_total": len(rows)}


def mark_read(uid="default", ids: list[str] | None = None) -> dict:
    ids = [str(value)[:240] for value in (ids or []) if value]
    with _LOCK:
        state = _read(uid)
        seen = list(dict.fromkeys([*(state.get("seen") or []), *ids]))[-1000:]
        state.update({"seen": seen, "updated_at": time.time()})
        _write(uid, state)
    return {"ok": True, "read": len(ids)}


def daily_summary(uid="default", *, day="", now: float | None = None) -> dict:
    now = float(now or time.time())
    local = time.localtime(now)
    day = str(day or time.strftime("%Y-%m-%d", local))
    try:
        start = time.mktime(time.strptime(day, "%Y-%m-%d"))
    except ValueError:
        return {"ok": False, "error": "day must be YYYY-MM-DD"}
    end = start + 86400
    tasks = mobile_tasks.list_tasks(uid=uid, limit=500).get("tasks", [])
    touched = [task for task in tasks if start <= float(task.get("updated_at") or 0) < end]
    states = {name: sum(str(task.get("state")) == name for task in touched)
              for name in ("done", "partial", "blocked", "needs_confirmation", "needs_account", "running")}
    return {"ok": True, "day": day, "total": len(touched), "states": states,
            "completed": [task["id"] for task in touched if task.get("state") == "done"],
            "needs_attention": [task["id"] for task in touched if task.get("state") in {
                "partial", "blocked", "needs_confirmation", "needs_account", "needs_software"}],
            "generated_at": now}


def control(task_id: str, action: str, *, uid="default", reason="") -> dict:
    action = str(action or "").lower()
    if action not in {"pause", "resume", "cancel"}:
        return {"ok": False, "error": "action must be pause/resume/cancel"}
    with mobile_tasks._LOCK:
        task = mobile_tasks._read(task_id)
        if not task or str(task.get("uid")) != str(uid):
            return {"ok": False, "error": "mobile task not found"}
        state = str(task.get("state") or "")
        if action == "pause":
            if state not in {"ready", "running", "needs_account", "needs_software"}:
                return {"ok": False, "error": f"task cannot be paused from {state}", "task": task}
            task.setdefault("control", {}).update({"paused": True, "cancel_requested": False,
                                                    "reason": str(reason)[:1000], "at": time.time()})
            task["state_before_pause"] = state
            if state == "running":
                task["control"]["stop_at_safe_point"] = True
        elif action == "resume":
            control = task.get("control") or {}
            if not control.get("paused"):
                return {"ok": False, "error": "task is not paused", "task": task}
            task["control"] = {"paused": False, "cancel_requested": False, "at": time.time()}
        else:
            if state in {"done", "partial", "blocked"}:
                return {"ok": False, "error": f"terminal task cannot be cancelled: {state}", "task": task}
            task.setdefault("control", {}).update({"cancel_requested": True, "paused": False,
                                                    "stop_at_safe_point": state == "running",
                                                    "reason": str(reason)[:1000], "at": time.time()})
        task["version"] = int(task.get("version") or 1) + 1
        task["updated_at"] = time.time()
        mobile_tasks._event(task, "task_control_requested", {"action": action,
                            "reason": str(reason)[:1000], "state": state,
                            "stop_at_safe_point": bool((task.get("control") or {}).get("stop_at_safe_point"))})
        mobile_tasks._write(task)
    return {"ok": True, "action": action, "task": task}


def health(uid="default") -> dict:
    device = mobile_device.status(force=True)
    root = pathlib.Path(legion_paths.under("mobile"))
    root.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(root)
    used_percent = round((disk.used / disk.total) * 100, 1) if disk.total else 0
    inventory = mobile_apps.inventory() if device.get("ready") else {
        "ok": False, "apps": [], "error": "Android 尚未就绪"}
    threshold = preferences(uid)["preferences"]["storage_warn_percent"]
    blockers = []
    if not device.get("ready"):
        blockers.append("云手机未就绪")
    if used_percent >= threshold:
        blockers.append(f"存储已使用 {used_percent}%")
    return {"ok": not blockers, "device": device, "storage": {
        "total": disk.total, "used": disk.used, "free": disk.free, "used_percent": used_percent,
        "warning": used_percent >= threshold}, "apps": inventory.get("apps") or [],
        "app_inventory_ok": bool(inventory.get("ok")), "blockers": blockers,
        "checked_at": time.time()}
