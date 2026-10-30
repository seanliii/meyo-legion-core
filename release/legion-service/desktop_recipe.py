#!/usr/bin/env python3
"""把云电脑成功轨迹沉淀成可审计、可版本化、可重放的用户 Skill。"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

import desktop_fs
import desktop_workspace

SENSITIVE_KEYS = ("password", "otp", "captcha", "token", "secret", "cookie", "card", "cvv", "pin")
UNSAFE_ACTIONS = {"payment", "purchase", "send_message", "external_publish", "account_change"}


def _safe(value: Any, fallback: str) -> str:
    return desktop_fs._safe_id(str(value or fallback), fallback)


def _root(uid: str) -> Path:
    root = Path(desktop_fs.prepare_user_root(uid)["root"]) / "personal" / "recipes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _recipe_root(uid: str, recipe_id: str) -> Path:
    return _root(uid) / _safe(recipe_id, "recipe")


def _redact(value: Any, key: str = "") -> Any:
    if any(token in str(key).lower() for token in SENSITIVE_KEYS):
        return "{{USER_SECRET}}"
    if isinstance(value, dict):
        return {str(name)[:100]: _redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value[:100]]
    if isinstance(value, str):
        value = re.sub(r"(?i)(password|otp|token|captcha|cvv|pin)\s*[:=]\s*\S+", r"\1={{USER_SECRET}}", value)
        return value[:3000]
    return value


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _semantic_steps(events: list[dict]) -> list[dict]:
    steps = []
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        action = str(data.get("action") or event.get("type") or "").strip()
        if not action or action in {"task_created", "task_updated", "task_finalized"}:
            continue
        if action in UNSAFE_ACTIONS:
            action = "human_handoff"
        step = {
            "action": action[:100],
            "app": str(data.get("app") or "")[:100],
            "intent": str(data.get("intent") or data.get("phase") or data.get("title") or "")[:500],
            "inputs": _redact(data.get("inputs") or data.get("params") or {}),
            "completion_check": _redact(data.get("completion_check") or data.get("evidence") or {}),
        }
        # 旧坐标不进入配方；重放时由当前截图/语义重新定位，避免 UI 变化后盲点旧位置。
        if isinstance(step["inputs"], dict):
            for key in ("x", "y", "coordinates", "selector", "element_id"):
                step["inputs"].pop(key, None)
        steps.append(step)
    return steps


def create_from_task(uid: str, project_id: str, task_id: str, name: str,
                     recipe_id: str = "") -> dict:
    current = desktop_workspace.get_task(uid, project_id, task_id)
    if not current.get("ok"):
        return current
    task = current["task"]
    if task.get("state") != "completed":
        return {"ok": False, "error": "only completed tasks can become recipes", "task": task}
    steps = _semantic_steps(current.get("events") or [])
    if not steps:
        return {"ok": False, "error": "task has no reusable semantic actions"}
    recipe_id = _safe(recipe_id or f"recipe-{uuid.uuid4().hex[:10]}", "recipe")
    root = _recipe_root(uid, recipe_id)
    versions = root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    existing = list(versions.glob("v*.json"))
    version = len(existing) + 1
    recipe = {
        "recipe_id": recipe_id, "uid": _safe(uid, "default"), "name": str(name or "云电脑流程")[:200],
        "version": version, "source": {"project_id": _safe(project_id, "project"), "task_id": _safe(task_id, "task")},
        "steps": steps, "created_at": time.time(), "runs": 0, "successes": 0,
        "last_result": {}, "status": "draft",
        "replay_contract": "每步按当前截图和界面语义重新定位；失败回流模型，不使用历史坐标。",
    }
    path = versions / f"v{version}.json"
    _atomic(path, recipe)
    _atomic(root / "active.json", recipe)
    return {"ok": True, "recipe": recipe, "path": f"personal/recipes/{recipe_id}",
            "promotable": True}


def get(uid: str, recipe_id: str, version: int | None = None) -> dict:
    root = _recipe_root(uid, recipe_id)
    path = root / (f"versions/v{int(version)}.json" if version else "active.json")
    recipe = _load(path)
    return {"ok": bool(recipe), "recipe": recipe} if recipe else {"ok": False, "error": "recipe not found"}


def list_recipes(uid: str = "default") -> dict:
    rows = []
    for root in _root(uid).iterdir():
        if root.is_dir() and not root.is_symlink():
            recipe = _load(root / "active.json")
            if recipe:
                rows.append(recipe)
    rows.sort(key=lambda row: row.get("created_at", 0), reverse=True)
    return {"ok": True, "recipes": rows, "total": len(rows)}


def replay_context(uid: str, recipe_id: str, inputs: dict | None = None) -> dict:
    result = get(uid, recipe_id)
    if not result.get("ok"):
        return result
    recipe = result["recipe"]
    return {
        "ok": True, "recipe_id": recipe_id, "version": recipe["version"],
        "name": recipe["name"], "steps": recipe["steps"], "inputs": _redact(inputs or {}),
        "instruction": (
            "这是过去成功流程的结构化现场，只供判断。请在当前云电脑上逐步执行；"
            "每步先看当前截图/界面语义重新定位，绝不复用历史坐标。遇登录、验证码、付款、"
            "发送或隐私授权时创建人类待办；UI 变化或动作失败时读取新现场自行换招并记录证据。"
        ),
    }


def record_result(uid: str, recipe_id: str, ok: bool, task_id: str = "",
                  failure: str = "", learned_steps: list | None = None) -> dict:
    result = get(uid, recipe_id)
    if not result.get("ok"):
        return result
    recipe = result["recipe"]
    recipe["runs"] = int(recipe.get("runs", 0)) + 1
    if ok:
        recipe["successes"] = int(recipe.get("successes", 0)) + 1
    recipe["last_result"] = {"ok": bool(ok), "task_id": str(task_id or "")[:120],
                             "failure": str(failure or "")[:2000], "at": time.time()}
    if learned_steps:
        recipe["steps"] = _semantic_steps([{"type": "learned", "data": step} for step in learned_steps]) or recipe["steps"]
        recipe["version"] = int(recipe.get("version", 1)) + 1
        _atomic(_recipe_root(uid, recipe_id) / "versions" / f"v{recipe['version']}.json", recipe)
    _atomic(_recipe_root(uid, recipe_id) / "active.json", recipe)
    return {"ok": True, "recipe": recipe, "success_rate": recipe["successes"] / recipe["runs"]}


def export_skill(uid: str, recipe_id: str) -> dict:
    result = get(uid, recipe_id)
    if not result.get("ok"):
        return result
    recipe = result["recipe"]
    root = _recipe_root(uid, recipe_id) / "skill" / f"v{recipe['version']}"
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"name": recipe["name"], "recipe_id": recipe_id, "version": recipe["version"],
                "steps": recipe["steps"], "replay_contract": recipe["replay_contract"]}
    _atomic(root / "recipe.json", manifest)
    skill = (
        f"---\nname: {recipe_id}\ndescription: 可重放的云电脑流程：{recipe['name']}\n---\n\n"
        f"# {recipe['name']}\n\n"
        "读取同目录 `recipe.json` 作为过去成功流程的 context。根据当前截图和界面语义重新定位；"
        "不要复用历史坐标。涉及登录、验证码、付款、发送或隐私授权时停下并创建人类待办。"
        "每步保存状态与证据；UI 变化时自行重新观察、换招并记录。\n"
    )
    (root / "SKILL.md").write_text(skill, encoding="utf-8")
    return {"ok": True, "name": recipe_id, "version": recipe["version"],
            "source": str(root), "skill_md": str(root / "SKILL.md")}
