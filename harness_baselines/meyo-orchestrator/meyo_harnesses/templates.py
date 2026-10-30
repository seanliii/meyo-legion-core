# -*- coding: utf-8 -*-
"""场景模板 —— 治"小白不知道拿 agent 做啥"。只给目标/验收/建议工具,不指定 harness。

设计原则(ADR-005):模板【不】绑定执行家/编排流程/路由。它只把一个模糊诉求变成
"清晰目标 + 验收标准 + 可选工具",执行家与编排方式仍由 captain 模型自主决定。
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

_FILE = Path(__file__).resolve().parent / "scenario_templates.json"


def _load() -> list[dict]:
    try:
        return json.loads(_FILE.read_text()).get("templates", [])
    except Exception:
        return []


def list_templates() -> list[dict[str, str]]:
    """客观列出场景模板(key/name/目标模板/验收)。无排序偏好/无推荐。"""
    return [{"key": t.get("key"), "name": t.get("name"),
             "goal_template": t.get("goal_template", ""),
             "acceptance": t.get("acceptance", []),
             "suggested_tools": t.get("suggested_tools", [])}
            for t in _load()]


def instantiate_template(key: str, params: dict[str, Any] | None = None) -> dict:
    """把模板 + 参数拼成一条清晰目标(供 captain 拿去自主编排)。缺参数不猜·如实标出。"""
    params = params or {}
    tpl = next((t for t in _load() if t.get("key") == key), None)
    if not tpl:
        return {"error": "unknown_template", "available": [t["key"] for t in _load()]}
    raw = tpl.get("goal_template", "")
    import string
    fields = [f for _, f, _, _ in string.Formatter().parse(raw) if f]
    missing = [f for f in fields if f not in params]
    try:
        goal = raw.format(**{**{f: "{" + f + "}" for f in fields}, **params})
    except Exception:
        goal = raw
    return {"key": key, "name": tpl.get("name"), "goal": goal,
            "acceptance": tpl.get("acceptance", []),
            "suggested_tools": tpl.get("suggested_tools", []),
            "missing_params": missing}
