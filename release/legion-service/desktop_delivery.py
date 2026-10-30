#!/usr/bin/env python3
"""把云电脑终态交付幂等写回创建任务的原始对话。"""
from __future__ import annotations

import os
from urllib.parse import quote

import desktop_workspace
import file_hub
import legion_paths
import sessions

_TERMINAL_STATES = {"completed", "partial", "failed", "cancelled"}


def _artifact(path: str) -> dict | None:
    root = os.path.realpath(legion_paths.under("users"))
    absolute = os.path.realpath(os.path.expanduser(str(path or "")))
    if not os.path.isfile(absolute) or not (absolute == root or absolute.startswith(root + os.sep)):
        return None
    relative = os.path.relpath(absolute, root)
    url = "/desktop-outputs/" + "/".join(quote(part) for part in relative.split(os.sep))
    return {
        "name": os.path.basename(absolute),
        "path": absolute,
        "url": url,
        "kind": file_hub.kind_of(absolute),
        "bytes": os.path.getsize(absolute),
    }


def _quality(task: dict) -> dict:
    checkpoint = task.get("checkpoint") if isinstance(task.get("checkpoint"), dict) else {}
    quality = checkpoint.get("quality") if isinstance(checkpoint.get("quality"), dict) else {}
    if not quality:
        try:
            import desktop_quality_run
            loaded = desktop_quality_run.get(
                str(task.get("uid") or "default"), str(task.get("project_id") or ""),
                str(task.get("task_id") or ""), include_events=False)
            quality = loaded.get("run") if loaded.get("ok") else {}
        except Exception:
            quality = {}
    return quality if isinstance(quality, dict) else {}


def _result(task: dict) -> dict:
    artifacts = []
    for row in task.get("artifacts") or []:
        item = _artifact(str(row.get("path") or "")) if isinstance(row, dict) else None
        if item:
            artifacts.append(item)
    state = str(task.get("state") or "failed")
    summary = str(task.get("phase") or "云电脑后台任务已结束")
    quality = _quality(task)
    verdict = str(quality.get("verdict") or "")
    verdict_label = {
        "VERIFIED_NARROW_WIN": "已用同题证据验证一项窄面胜出",
        "PARITY_OR_INCONCLUSIVE": "与当前参照持平，或证据尚不足以下胜出结论",
        "BELOW_FRONTIER": "仍低于当前参照",
    }.get(verdict, "")
    final = f"云电脑任务《{task.get('goal') or task.get('task_id')}》{summary}。"
    if verdict_label:
        final += f" 质量结论：{verdict_label}。"
    if artifacts:
        final += " 已附上可查看和下载的交付文件。"
    elif state == "completed":
        final += " 本次没有登记可下载文件。"
    gaps = [str(value) for value in quality.get("remaining_gaps") or [] if str(value).strip()]
    if gaps:
        final += " 尚待改进：" + "；".join(gaps[:5]) + "。"
    result = {
        "key": "cloud_desktop", "name": "云电脑",
        "status": "done" if state == "completed" else state,
        "final": final, "artifacts": artifacts,
        "task_id": task.get("task_id"), "project_id": task.get("project_id"),
        "quality": {
            "verdict": verdict, "state": quality.get("state"),
            "verified_strengths": list(quality.get("verified_strengths") or []),
            "remaining_gaps": gaps, "gap_plan": list(quality.get("gap_plan") or []),
            "journey_observations": list(quality.get("journey_observations") or []),
            "quality_context": dict(quality.get("quality_context") or {}),
            "metrics": dict(quality.get("metrics") or {}),
            "references": list(quality.get("references") or []),
        } if quality else {},
    }
    return {"results": [result], "final_result": final, "delivered_count": len(artifacts)}


def notify(task: dict, attempt: int) -> dict:
    state = str(task.get("state") or "")
    if state not in _TERMINAL_STATES:
        return {"ok": False, "error": "task is not terminal"}
    if task.get("delivery_notified_at"):
        return {"ok": True, "status": "unchanged"}
    sid = str(task.get("session_id") or "")
    if not sid:
        return {"ok": False, "error": "task has no original session"}
    uid = str(task.get("uid") or "default")
    saved = sessions.save_turn(
        sid,
        f"[云电脑后台任务完成] {task.get('goal') or task.get('task_id')}",
        _result(task),
        mode=str(task.get("mode") or "auto"),
        user_id=uid,
    )
    if saved.get("error"):
        return {"ok": False, "error": saved["error"]}
    marked = desktop_workspace.mark_delivery_notified(
        uid, str(task["project_id"]), str(task["task_id"]), int(attempt),
    )
    return {"ok": bool(marked.get("ok")), "session": saved, "marked": marked}
