#!/usr/bin/env python3
"""研究交付工作流：默认高效产出，仅在任务需要时验证真实桌面办公。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import cloud_desktop
import desktop_fs
import desktop_workspace

REQUIRED_APPS = ("chromium",)
OFFICE_APPS = ("libreoffice",)


def _relative(uid: str, path: str) -> str:
    root = Path(desktop_fs.prepare_user_root(uid)["root"]).resolve()
    target = Path(path).resolve()
    if os.path.commonpath((str(root), str(target))) != str(root):
        raise ValueError("artifact escapes user workspace")
    return target.relative_to(root).as_posix()


def _app_versions(uid: str) -> dict:
    command = (
        "for x in chromium libreoffice; do "
        "if command -v $x >/dev/null 2>&1; then printf '%s=' $x; $x --version 2>/dev/null | head -1; fi; done"
    )
    result = desktop_fs.desktop_exec(uid, command, 30)
    found = {}
    for line in str(result.get("stdout") or "").splitlines():
        name, separator, version = line.partition("=")
        if separator and name in REQUIRED_APPS + OFFICE_APPS:
            found[name] = version.strip()
    return {"ok": result.get("ok") and all(name in found for name in REQUIRED_APPS),
            "apps": found, "probe": result}


def _requires_office_gui(task: dict) -> bool:
    spec = task.get("execution_spec") or {}
    return bool(spec.get("require_office_gui") or spec.get("office_gui"))


def prepare(task: dict) -> dict:
    uid = str(task.get("uid") or "default")
    ready = cloud_desktop.ensure_desktop(uid)
    if not ready.get("ok") and not (ready.get("status") or {}).get("desktop_ready"):
        return {"ok": False, "state": "queued", "phase": "云电脑启动失败",
                "error": str(ready.get("reason") or ready.get("error") or "desktop unavailable"),
                "next_run_at": time.time() + 30,
                "checkpoint": {"stage": "prepare", "observe_before_resume": True}}
    apps = _app_versions(uid)
    required = REQUIRED_APPS + (OFFICE_APPS if _requires_office_gui(task) else ())
    missing = [name for name in required if name not in apps.get("apps", {})]
    if not apps.get("probe", {}).get("ok") or missing:
        return {"ok": False, "state": "blocked", "phase": "缺少任务必需软件",
                "error": "云电脑缺少: " + ", ".join(missing or required),
                "checkpoint": {"stage": "prepare", "missing_apps": missing or list(required)}}
    observed = desktop_fs.desktop_screenshot(uid)
    if not observed.get("ok"):
        return {"ok": False, "state": "queued", "phase": "无法观察桌面",
                "error": str(observed.get("error") or "screenshot failed"),
                "next_run_at": time.time() + 20,
                "checkpoint": {"stage": "prepare", "observe_before_resume": True}}
    desktop_workspace.append_event(uid, task["project_id"], task["task_id"], "desktop_observed", {
        "stage": "prepare", "screenshot": observed.get("path"), "apps": apps["apps"],
    })
    return {"ok": True, "screenshot": observed["path"], "apps": apps["apps"]}


def prompt(task: dict, prepared: dict) -> str:
    uid = str(task.get("uid") or "default")
    project_id = str(task["project_id"])
    task_id = str(task["task_id"])
    project_root = f"/root/Meyo/projects/{project_id}"
    office = "；本任务明确要求 LibreOffice GUI" if _requires_office_gui(task) else "；本任务不要求 LibreOffice，优先直接产出"
    return f"""在用户 {uid} 的真实云电脑里完成研究办公任务：{task.get('goal') or ''}

这是后台持久任务 {task_id}，项目目录是 {project_root}。当前已确认 Chromium 真正安装{office}，初始桌面截图为 {prepared['screenshot']}。

必须遵守下面的工作方式：
1. 先用 computer_command screenshot 观察当前桌面，再决定每一步；不得凭旧坐标盲点。
2. 用 desktop_app 打开 Chromium，在图形界面中浏览至少两个独立网页来源。每个来源都记录标题、URL、关键事实和访问时间到 {project_root}/evidence/sources.json。
3. 浏览和下载动作必须真实发生在 Chromium；下载材料保存到 {project_root}/evidence/downloads/。不能用 curl、wget、requests 或脚本假装浏览器研究。
4. 成果优先：可直接用可靠的程序化文档工具生成 {project_root}/deliverables/report.pdf，不得为了证明会点软件而降低速度和质量。只有 execution_spec 明确 require_office_gui 时，才必须用 LibreOffice 图形界面编辑或导出。
5. 每完成一个阶段，用 desktop_screenshot 保存真机画面，并在任务事件中说明刚看见什么、做了什么、如何确认结果。默认至少保存两个不同来源的 Chromium 页面截图；要求 office GUI 时再额外保存编辑与导出截图。
6. 遇到登录、验证码、MFA、隐私授权、付款、发送消息、公开发布或账户变更，立即创建 human handoff 并停止自动操作；绝不保存密码、验证码、Cookie 或 token。
7. 若 checkpoint 标记 observe_before_resume，必须先截图并检查浏览器、应用和文件现状，再判断是否继续；不能重放可能已有外部副作用的点击。
8. 最终只有在 PDF 可解析、正文非空、至少两个独立来源可追溯，且浏览器研究证据齐全时才能标记 completed；否则返回 partial/blocked/failed 并如实列出缺项。

完成后把结构化结果写入 {project_root}/evidence/workflow-result.json，并在回答中列出所有产物的项目相对路径。"""


def _source_urls(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    rows = payload if isinstance(payload, list) else payload.get("sources", []) if isinstance(payload, dict) else []
    return [str(row.get("url") or "") for row in rows if isinstance(row, dict) and row.get("url")]


def _pdf_text_ok(uid: str, pdf: Path) -> bool:
    if not pdf.is_file() or pdf.stat().st_size <= 1000:
        return False
    try:
        remote_path = "/root/Meyo/" + _relative(uid, str(pdf))
    except ValueError:
        # 产物不在用户工作区内：既不能给它拼云桌面路径，也不应把它当可验证交付——
        # 如实当作无法验证(返回 False)，不拍异常炸断上层 verify 流程。
        return False
    result = desktop_fs.desktop_exec(
        uid,
        "pdftotext " + json.dumps(remote_path) + " - 2>/dev/null",
        30,
    )
    text = str(result.get("stdout") or "").strip()
    if result.get("ok") and not result.get("command_failed") and len(text) >= 80:
        return True
    try:
        from pypdf import PdfReader
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)
    except Exception:
        return False
    return len(text.strip()) >= 80


def verify(task: dict, result: dict) -> dict:
    uid = str(task.get("uid") or "default")
    root = Path(desktop_fs.prepare_user_root(uid)["root"]) / "projects" / str(task["project_id"])
    pdf = root / "deliverables" / "report.pdf"
    sources = root / "evidence" / "sources.json"
    screenshots = list((root / "evidence").glob("*.png")) if (root / "evidence").is_dir() else []
    urls = _source_urls(sources)
    domains = {urlparse(url).netloc.lower() for url in urls if urlparse(url).scheme in {"http", "https"}}
    required_screenshots = 3 if _requires_office_gui(task) else 2
    checks = {
        "pdf": _pdf_text_ok(uid, pdf),
        "sources": len(urls) >= 2 and len(domains) >= 2,
        "screenshots": len(screenshots) >= required_screenshots,
    }
    final_shot = desktop_fs.desktop_screenshot(uid)
    if final_shot.get("ok"):
        screenshots.append(Path(desktop_fs.prepare_user_root(uid)["root"]) / final_shot["path"])
    artifacts = []
    for path in (pdf, sources):
        if path.is_file():
            artifacts.append({"path": _relative(uid, str(path)), "bytes": path.stat().st_size})
    evidence = [_relative(uid, str(path)) for path in screenshots if path.is_file()]
    completed = all(checks.values())
    desktop_workspace.append_event(uid, task["project_id"], task["task_id"], "desktop_workflow_verified", {
        "stage": "verify", "checks": checks, "artifacts": artifacts, "screenshots": evidence,
    })
    return {"ok": completed, "state": "completed" if completed else "partial",
            "phase": "真实研究报告已验证" if completed else "研究办公产物证据不完整",
            "checkpoint": {"stage": "verified", "checks": checks,
                           "artifacts": artifacts, "screenshots": evidence},
            "error": "缺少真实 GUI 交付证据: " + ", ".join(k for k, v in checks.items() if not v)
                     if not completed else "", "result": result}


def run(task: dict, dispatch) -> dict:
    checkpoint = task.get("checkpoint") or {}
    if checkpoint.get("handoff_request") and not checkpoint.get("last_handoff"):
        import human_handoff
        request = checkpoint["handoff_request"]
        return human_handoff.request(
            str(task.get("uid") or "default"), str(task["project_id"]), str(task["task_id"]),
            str(request.get("kind") or "other"), str(request.get("title") or "需要你处理"),
            str(request.get("instructions") or "请在云电脑中完成后回来继续"),
            app=str(request.get("app") or ""), target=str(request.get("target") or ""),
            consequence=str(request.get("consequence") or ""),
            checkpoint={key: value for key, value in checkpoint.items() if key != "handoff_request"},
            completion_check=request.get("completion_check") if isinstance(request.get("completion_check"), dict) else None,
        )
    prepared = prepare(task)
    if not prepared.get("ok"):
        return prepared
    result = dispatch(prompt(task, prepared))
    return verify(task, result)
