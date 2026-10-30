# -*- coding: utf-8 -*-
"""共享 skill 源 —— 客观发现/读取 markdown skills,不做路由或自动套用 (2026-08-25)。

首个源:ecosystem/pm-skills。模型用 list_skills/read_skill 自主决定是否把某 skill
交给哪个 harness 执行;本层不绑定执行家,避免把编排逻辑写死。
"""
from __future__ import annotations
from pathlib import Path
from typing import Any

BASELINES = Path(__file__).resolve().parents[2]
SKILL_SOURCES = {
    "pm-skills": BASELINES / "ecosystem" / "pm-skills",
}


def list_skills(source: str = "pm-skills") -> list[dict[str, str]]:
    """客观列出某源里的 SKILL.md。"""
    root = SKILL_SOURCES.get(source)
    if not root or not root.is_dir():
        return []
    out = []
    for p in sorted(root.rglob("SKILL.md")):
        try:
            rel = str(p.relative_to(root))
            # 首个 markdown 标题只作 display_name,不评价
            title = next((ln.lstrip("# ").strip() for ln in p.read_text().splitlines()
                          if ln.startswith("#")), p.parent.name)
            out.append({"source": source, "path": rel, "name": title})
        except Exception:
            continue
    return out


def read_skill(source: str, path: str) -> dict[str, Any]:
    """读取一个 skill。路径严格限制在声明源内,防任意文件读取。"""
    root = SKILL_SOURCES.get(source)
    if not root:
        return {"error": "unknown_skill_source", "available": list(SKILL_SOURCES)}
    try:
        p = (root / path).resolve()
        if root.resolve() not in p.parents or p.name != "SKILL.md" or not p.is_file():
            return {"error": "skill_path_not_allowed"}
        return {"source": source, "path": path, "content": p.read_text()[:30000]}
    except Exception as e:
        return {"error": f"{type(e).__name__}:{str(e)[:120]}"}
