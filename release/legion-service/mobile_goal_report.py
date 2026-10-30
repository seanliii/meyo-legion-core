#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云手机生活伙伴整体目标：只按真实业务事实生成 pass/partial/blocked/not_run 矩阵。"""
from __future__ import annotations

import hashlib
import json
import pathlib
import time

import legion_paths
import mobile_tasks
import verification_center

_STATUSES = {"pass", "partial", "blocked", "not_run"}


def _task(task_id: str, *, uid="") -> dict:
    if not task_id:
        return {}
    row = mobile_tasks.get(task_id, uid=uid)
    return row.get("task") if row.get("ok") else {}


def _artifact(path: str) -> dict:
    file = pathlib.Path(str(path or ""))
    if not file.is_file():
        return {"path": str(file), "exists": False}
    return {"path": str(file), "exists": True, "bytes": file.stat().st_size,
            "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}


def _row(key: str, title: str, status: str, *, facts=None, evidence=None,
         reason="", needs_user="", safety=True) -> dict:
    if status not in _STATUSES:
        raise ValueError("invalid goal status")
    return {"key": key, "title": title, "status": status,
            "facts": facts or {}, "evidence": evidence or [], "reason": reason,
            "needs_user": needs_user, "safety_boundary_respected": bool(safety)}


def _task_row(key: str, title: str, task_id: str, required=None, *, uid="") -> dict:
    task = _task(task_id, uid=uid)
    if not task:
        return _row(key, title, "not_run", reason="未找到真实持久任务账本")
    detail = verification_center.mobile_task_detail(task_id) or {}
    facts = (task.get("verification") or {}).get("facts") or {}
    required = required or []
    missing = [name for name in required if not facts.get(name)]
    ok = task.get("state") == "done" and (task.get("verification") or {}).get("ok") is True and not missing
    return _row(key, title, "pass" if ok else "partial", facts={
        **facts, "task_id": task_id, "state": task.get("state"),
        "attempts": len(task.get("attempts") or []), "events": len(task.get("events") or []),
        "confirmations": detail.get("counts", {}).get("confirmations", 0),
        "release_ready": detail.get("release_ready", False)},
        evidence=[_artifact(path) for path in task.get("artifacts") or []],
        reason="" if ok else "任务存在但业务事实或终态未完整通过")


def build(*, uid="default", output="", task_ids: dict[str, str] | None = None) -> dict:
    """按报告项 key 显式选择本 uid 的账本；未指定时仅保留原四项默认任务 ID。"""
    task_ids = task_ids or {}
    rows = [
        _task_row("confirmation-resume", "同任务人工确认与恢复",
                  task_ids.get("confirmation-resume", "mobile-confirm-resume-proof"),
                  ["privacy_decision_observed", "screen"], uid=uid),
        _task_row("trusted-app-lifecycle", "可信商店 App 全生命周期",
                  task_ids.get("trusted-app-lifecycle", "mobile-fdroid-klooni-lifecycle"),
                  ["installed", "launch_verified", "uninstall_verified", "rollback_verified"], uid=uid),
        _task_row("alarm", "真实闹钟与重启持久性",
                  task_ids.get("alarm", "mobile-real-alarm-0735"),
                  ["system_alarm_visible", "ui_alarm_visible", "persisted_after_reboot"], uid=uid),
        _task_row("files-gallery", "真实文件、MediaStore 与相册预览",
                  task_ids.get("files-gallery", "mobile-real-files-gallery"),
                  ["file_exists", "media_store_visible", "gallery_focus_visible"], uid=uid),
    ]

    for key, title in (
        ("osmand-route", "OsmAnd 真实路线规划"),
        ("amap-route", "高德地图真实路线规划"),
    ):
        rows.append(_task_row(key, title, task_ids.get(key, ""), uid=uid))

    for key, title, needs in (
        ("communication", "真实联系人消息发送前准备", "用户在真实通讯 App 登录，并提供测试联系人和消息"),
        ("shopping", "真实购物 App 加购物车", "用户登录真实购物 App，并提供无歧义商品与规格"),
        ("food", "真实外卖 App 结算前准备", "用户登录真实外卖 App，并提供门店、商品和地址"),
        ("content", "真实内容 App 收藏", "用户登录真实内容 App，并提供目标内容"),
    ):
        rows.append(_row(key, title, "not_run", reason="没有真实账号现场，未用测试桩冒充商业业务通过",
                         needs_user=needs))

    rows.append(_task_row("hardware", "相机、扫码、麦克风、定位、通知和蓝牙",
                          task_ids.get("hardware", ""), uid=uid))

    counts = {name: sum(row["status"] == name for row in rows) for name in sorted(_STATUSES)}
    report = {"schema": "meyo.mobile.goal-report.v1", "generated_at": time.time(),
              "uid": uid, "counts": counts, "items": rows,
              "stage_complete": all(row["status"] == "pass" for row in rows
                                    if row["key"] in {"confirmation-resume", "trusted-app-lifecycle",
                                                       "alarm", "files-gallery"}),
              "overall_complete": all(row["status"] == "pass" for row in rows)}
    target = pathlib.Path(output or legion_paths.under("mobile", "reports", "overall-goal.json"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["output"] = str(target)
    return report


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
