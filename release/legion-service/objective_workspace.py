#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Objective → Project → Task 跨来源统一投影；旧状态机继续保留各自事实源。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import desktop_fs
import desktop_workspace
import goal_runtime

_LOCK = threading.RLock()
_CANONICAL = {
    "queued": "queued", "ready": "queued", "registered": "queued",
    "running": "running", "fired": "running",
    "waiting_human": "waiting_user", "needs_confirmation": "waiting_user",
    "needs_account": "waiting_user", "needs_software": "waiting_user",
    "blocked": "blocked", "failed": "blocked", "error": "blocked", "timeout": "blocked",
    "partial": "blocked", "completed": "completed", "done": "completed",
    "cancelled": "cancelled", "expired": "cancelled",
}


def _project_root(uid: str, project_id: str) -> Path:
    base = Path(desktop_fs.prepare_user_root(uid)["root"]) / "projects"
    return base / desktop_workspace._safe(project_id, "project")


def _links_path(uid: str, project_id: str) -> Path:
    return _project_root(uid, project_id) / ".meyo" / "links.json"


def _read_links(uid: str, project_id: str) -> dict:
    try:
        value = json.loads(_links_path(uid, project_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "links": []}
    return value if isinstance(value, dict) else {"version": 1, "links": []}


def _write_links(uid: str, project_id: str, value: dict) -> None:
    path = _links_path(uid, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def canonical_state(state: str) -> str:
    return _CANONICAL.get(str(state or "").lower(), "queued")


def link(uid: str, project_id: str, task_id: str, source_type: str, source_id: str,
         *, metadata: dict | None = None) -> dict:
    if source_type not in {"team", "mobile", "cron", "subscription", "session", "device"}:
        return {"ok": False, "error": "unsupported source type"}
    project = desktop_workspace.get_project(uid, project_id)
    if not project.get("ok"):
        return project
    if task_id and not any(row.get("task_id") == task_id for row in project.get("tasks") or []):
        return {"ok": False, "error": "task not found"}
    row = {"task_id": str(task_id), "source_type": source_type, "source_id": str(source_id),
           "metadata": dict(metadata or {}), "linked_at": time.time()}
    with _LOCK:
        value = _read_links(uid, project_id)
        links = [item for item in value.get("links") or []
                 if not (item.get("source_type") == source_type and item.get("source_id") == str(source_id))]
        links.append(row)
        value.update({"version": int(value.get("version") or 0) + 1,
                      "updated_at": time.time(), "links": links[-2000:]})
        _write_links(uid, project_id, value)
    return {"ok": True, "link": row}


def link_team(uid: str, project_id: str, task_id: str, team_id: str) -> dict:
    return link(uid, project_id, task_id, "team", team_id)


def link_mobile_task(uid: str, project_id: str, task_id: str, mobile_task_id: str) -> dict:
    return link(uid, project_id, task_id, "mobile", mobile_task_id)


def link_cron(uid: str, project_id: str, task_id: str, cron_id: str) -> dict:
    return link(uid, project_id, task_id, "cron", cron_id)


def link_subscription(uid: str, project_id: str, task_id: str, sid: str, url: str = "") -> dict:
    return link(uid, project_id, task_id, "subscription", sid, metadata={"url": str(url)})


def _source_projection(link_row: dict) -> dict:
    source_type, source_id = link_row["source_type"], link_row["source_id"]
    state, title, next_action, evidence = "queued", source_id, "等待开始", []
    if source_type == "team":
        try:
            import team_state
            row = team_state.load(source_id) or {}
            title = str(row.get("question") or source_id)
            states = [str(task.get("status") or "pending") for task in (row.get("tasks") or {}).values()]
            state = "completed" if states and all(item == "done" for item in states) else \
                "blocked" if any(item in {"failed", "error", "blocked"} for item in states) else \
                "running" if any(item == "running" for item in states) else "queued"
            next_action = "查看交付" if state == "completed" else "处理阻塞" if state == "blocked" else "继续执行"
        except Exception:
            state, next_action = "blocked", "检查军团状态"
    elif source_type == "mobile":
        try:
            import mobile_tasks
            row = mobile_tasks.get(source_id).get("task") or {}
            title, state = str(row.get("goal") or source_id), canonical_state(row.get("state"))
            evidence = list(row.get("artifacts") or [])
            next_action = "处理手机确认" if state == "waiting_user" else "查看手机结果" if state == "completed" else "继续手机任务"
        except Exception:
            state, next_action = "blocked", "检查手机任务"
    elif source_type == "cron":
        try:
            import cron_jobs
            row = next((item for item in cron_jobs.list_jobs(include_expired=True) if item.get("id") == source_id), {})
            title, state = str(row.get("question") or source_id), canonical_state(row.get("status"))
            next_action = "查看运行历史" if row.get("last_fired_at") else "等待下次执行"
        except Exception:
            state, next_action = "blocked", "检查定时任务"
    elif source_type == "subscription":
        title = str((link_row.get("metadata") or {}).get("url") or source_id)
        state, next_action = "running", "等待网页变化"
    return {**link_row, "title": title, "state": state, "next_action": next_action,
            "evidence": evidence}


def build_project_view(uid: str, project_id: str) -> dict:
    result = desktop_workspace.get_project(uid, project_id)
    if not result.get("ok"):
        return result
    project, tasks = result["project"], result.get("tasks") or []
    links = [_source_projection(item) for item in (_read_links(uid, project_id).get("links") or [])]
    task_map = {task["task_id"]: task for task in tasks}
    task_views = []
    for task in tasks:
        linked = [row for row in links if row.get("task_id") == task["task_id"]]
        source_states = [row["state"] for row in linked]
        state = canonical_state(task.get("state"))
        if "waiting_user" in source_states:
            state = "waiting_user"
        elif "blocked" in source_states:
            state = "blocked"
        elif linked and all(item == "completed" for item in source_states):
            state = "completed"
        elif "running" in source_states:
            state = "running"
        task_views.append({**task, "canonical_state": state, "sources": linked,
                           "next_action": next((row["next_action"] for row in linked
                                                if row["state"] in {"waiting_user", "blocked"}),
                                               "查看交付" if state == "completed" else "继续执行")})
    counts = {name: sum(row["canonical_state"] == name for row in task_views)
              for name in ("queued", "running", "waiting_user", "blocked", "completed", "cancelled")}
    # 项目级变化源(2026-09-07 打通做项目/盯变化):不挂具体任务的 cron/订阅等 link
    # (如从项目页直接登记的定时盯变化)也要在项目详情可见可操作——不能静默丢失。
    _task_ids = {task["task_id"] for task in tasks}
    unassigned_sources = [row for row in links
                          if not row.get("task_id") or row.get("task_id") not in _task_ids]
    active = next((row for row in task_views if row["canonical_state"] in {"waiting_user", "blocked", "running"}),
                  task_views[0] if task_views else None)
    objective_title = project.get("objective") or project["title"]
    ledger_items = []
    for task in task_views:
        dependencies = list((task.get("execution_spec") or {}).get("depends_on") or [])
        ledger_items.append({"id": task["task_id"], "title": task.get("goal") or task["task_id"],
                             "state": "pending" if task["canonical_state"] == "queued" else
                                      "running" if task["canonical_state"] == "running" else
                                      "blocked" if task["canonical_state"] in {"blocked", "waiting_user"} else
                                      task["canonical_state"],
                             "depends_on": dependencies, "next_action": task.get("next_action")})
    ledger = goal_runtime.reconcile(objective_title, {"items": ledger_items},
                                    evidence=[item for task in task_views
                                              for item in (task.get("artifacts") or [])])
    return {"ok": True, "unassigned_sources": unassigned_sources, "objective": {"id": project.get("objective_id") or project["project_id"],
                                        "title": objective_title,
                                        "success_criteria": project.get("success_criteria") or [],
                                        "status": "completed" if task_views and counts["completed"] == len(task_views)
                                                  else "active"},
            "project": project, "tasks": task_views, "links": links, "counts": counts,
            "work_ledger": ledger,
            "progress": round(counts["completed"] / len(task_views), 3) if task_views else 0,
            "next_action": ledger.get("next_action") or
                           (active.get("next_action") if active else "创建第一个任务"),
            "updated_at": project.get("updated_at")}
