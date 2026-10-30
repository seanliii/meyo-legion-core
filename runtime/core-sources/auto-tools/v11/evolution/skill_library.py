#!/usr/bin/env python3
"""
skill_library · Skills 即知识 (对齐 CatPaw/Claude Agent Skills 的 SKILL.md 理念)

用户抓正 (2026-08-06)：「把280个脚本里的经验型脚本转成可被动态加载的
SKILL.md，而不是全部当tool硬调」——挖真实差距:

    evolution_tool_registry.py 现有的"工具"全部是**可执行代码**：
    search → execute(name, params) → 直接跑一个 Python 函数，返回结构化结果。
    这适合"机械性、确定性"的能力（scan_text、offload、read_back...）。

    但 evolution/ 目录里还有大量脚本承载的其实是**经验性判断知识**——
    "什么算过度自信断言""什么场景算证据充分""什么时候该用 rubric 而不是
    自己写判断函数"——这些是给 LLM/agent **参考着自己判断**的规程，
    不是"调用了就自动出正确答案"的机械函数。硬塞进 tool registry 当
    execute() 目标，会把"读一段经验、照着做决策"矮化成"调一个函数拿
    一个 bool"，丢失了大量上下文和推理空间。

    CC/deepagents 的 Skills 机制解决的正是这个问题：SKILL.md 是纯文本
    知识文件（frontmatter 声明 name/description 供发现，正文是给模型
    读的完整规程），模型按需发现、读入上下文、自己决定怎么用，而不是
    被当成一个只返回 true/false 的黑盒。

本模块: 独立于 evolution_tool_registry 的 Skills 发现/加载机制。
    不是把 280 个脚本全部转换(工作量不现实、大部分脚本本来就该是纯代码
    工具)，而是:
    1. 建立机制: evolution/skills/{skill-name}/SKILL.md 目录规范 + loader
    2. 做 3 个示范迁移(overconfidence-detection / content-sufficiency-review
       / rubric-gate-usage)，展示"哪类脚本该转成 Skill、怎么转"
    3. 后续任何新的"经验判断类"能力，优先考虑写成 SKILL.md 而不是
       又加一个只返回 bool 的 tool function

用法:
    from skill_library import search_skills, load_skill, list_skills

    list_skills()                          # 全部已注册 skill 的元信息
    search_skills("过度自信")               # 关键词发现相关 skill
    load_skill("overconfidence-detection")  # 读入完整 SKILL.md 正文供参考

── 跟系统里另外两套同名"skill"机制的边界 (排查 g6 时发现，容易混淆) ──

系统里已经存在两套完全不同用途、只是命名撞了"skill"的机制，本模块是第三套，
互不重叠、不合并(合并涉及路径迁移和使用场景差异过大，收益不确定，本轮不做):

1. `skill_synthesizer.py` + `~/.meyo/skills/` + `skill_finder_bridge.py`
   (seed-users-system/skill_finder_bridge.py)
   —— 面向**觅游产品内容创作**场景，给"用户发的帖子该配什么工具/skill
   推荐"做语义检索，服务对象是内容社区用户，走远程 meyo-skill-finder API。

2. `skill_gate.py` (evolution/)
   —— 历史命名，实际是"4 步自检门禁"(演绎/进化/意愿/执行)，跟 Agent
   Skills 概念无关，只是恰好也叫 skill。

3. 本模块 `skill_library.py` + `evolution/skills/*/SKILL.md`
   —— 面向 **evolution 280+ 模块生态系统内部**，给 MetaGraph/auto agent
   在执行任务时按需发现"经验判断规程"用，走本地文件系统，不依赖任何
   远程 API，理念对齐 CC/deepagents 的 Agent Skills。
"""
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(_DIR, "skills")


@dataclass
class SkillSpec:
    name: str
    description: str
    category: str = "general"
    source_module: str = ""
    path: str = ""
    body: str = ""  # 正文(不含 frontmatter)，需要时才读，list/search 不返回


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 --- yaml --- frontmatter，不依赖 pyyaml
    (只支持简单的 key: value 平铺结构，够用，避免引入新依赖)。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    raw_meta, body = m.group(1), m.group(2)
    meta = {}
    for line in raw_meta.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    return meta, body.strip()


def _scan_skills() -> dict[str, SkillSpec]:
    """扫描 evolution/skills/*/SKILL.md，解析出元信息(不读全文正文，
    正文延迟到 load_skill 才读，对齐 tool registry 的延迟加载理念)。"""
    out: dict[str, SkillSpec] = {}
    if not os.path.isdir(SKILLS_DIR):
        return out
    for entry in sorted(os.listdir(SKILLS_DIR)):
        skill_dir = os.path.join(SKILLS_DIR, entry)
        skill_file = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_file):
            continue
        try:
            text = open(skill_file, encoding="utf-8").read()
            meta, _body = _parse_frontmatter(text)
            name = meta.get("name", entry)
            out[name] = SkillSpec(
                name=name,
                description=meta.get("description", ""),
                category=meta.get("category", "general"),
                source_module=meta.get("source_module", ""),
                path=skill_file,
            )
        except Exception:
            continue
    return out


def list_skills() -> list[dict]:
    """列出所有已发现 skill 的元信息(不含正文，对齐 tool registry 的
    'delayed工具仅注入名称列表' 理念，避免一次性把所有 SKILL.md 全文
    塞进 prompt)。"""
    skills = _scan_skills()
    return [
        {"name": s.name, "description": s.description,
         "category": s.category, "source_module": s.source_module}
        for s in skills.values()
    ]


def _tokenize(text: str) -> list[str]:
    text = text.replace("__", " ").replace("_", " ").replace("-", " ")
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    # 中文按字切，英文按词切(够用的简易分词，不引入 jieba 依赖)
    tokens = re.findall(r"[a-zA-Z0-9]{2,}|[\u4e00-\u9fff]", text)
    return [t.lower() for t in tokens]


def search_skills(query: str, limit: int = 5) -> list[dict]:
    """关键词发现相关 skill (跟 evolution_tool_registry.search_tools 同构，
    保证 agent 用同一套心智模型去发现 tool 和 skill)。"""
    skills = _scan_skills()
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return list_skills()[:limit]

    scored = []
    for s in skills.values():
        name_tokens = set(_tokenize(s.name))
        desc_tokens = set(_tokenize(s.description))
        score = 0.0
        overlap_name = len(query_tokens & name_tokens)
        overlap_desc = len(query_tokens & desc_tokens)
        if overlap_name:
            score += 3.0 * overlap_name / max(len(query_tokens), 1)
        if overlap_desc:
            score += 1.0 * overlap_desc / max(len(query_tokens), 1)
        if score > 0:
            scored.append((score, s))

    scored.sort(key=lambda x: -x[0])
    return [
        {"name": s.name, "description": s.description,
         "category": s.category, "score": round(score, 3)}
        for score, s in scored[:limit]
    ]


def load_skill(name: str) -> dict:
    """按需读入某个 skill 的完整正文(供模型参考着做判断，不是执行代码)。

    Returns:
        {ok, name, description, body, path} · body 是 Markdown 正文全文
    """
    skills = _scan_skills()
    spec = skills.get(name)
    if not spec:
        return {"ok": False, "error": f"skill '{name}' not found",
                "available": list(skills.keys())}
    try:
        text = open(spec.path, encoding="utf-8").read()
        _meta, body = _parse_frontmatter(text)
        return {"ok": True, "name": spec.name, "description": spec.description,
                "category": spec.category, "source_module": spec.source_module,
                "body": body, "path": spec.path}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def new_skill_scaffold(name: str, description: str, category: str = "general",
                        source_module: str = "") -> dict:
    """给一个新 skill 生成骨架文件，方便后续继续把经验型脚本迁移成 Skill
    (对齐 create-skill 的思路，但是 evolution 系统内部专用的轻量版)。"""
    skill_dir = os.path.join(SKILLS_DIR, name)
    skill_file = os.path.join(skill_dir, "SKILL.md")
    if os.path.exists(skill_file):
        return {"ok": False, "error": f"skill '{name}' 已存在: {skill_file}"}
    try:
        os.makedirs(skill_dir, exist_ok=True)
        content = f"""---
name: {name}
description: {description}
source_module: {source_module}
category: {category}
---

# {name}

## 为什么需要这个

(说明这个 skill 解决什么问题，最好引用真实踩过的坑)

## 判断标准 / 应对流程

(具体的规程，越具体越好，避免空泛原则)

## 相关工具

(如果有对应的可执行工具，列出调用方式)
"""
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": skill_file}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ══════════════════════════════════════════════════════════════════
# 接入 evolution_tool_registry：让 agent 用同一套 search 入口就能同时
# 发现"可执行工具"和"经验知识 skill"，不用记两套 API
# ══════════════════════════════════════════════════════════════════

def _register_self():
    try:
        from evolution_tool_registry import register_evolution_tool

        @register_evolution_tool(
            name="skill_search",
            description="按关键词发现经验知识型Skill(SKILL.md)，跟tool不同，"
                        "skill是给你参考着自己判断的规程，不是直接执行的函数",
            category="meta",
            cost_tier="cheap",
            search_hint="skill knowledge experience guideline judgment criteria",
            module="skill_library",
            is_deferred=True,
        )
        def _search_tool(params):
            query = params.get("query", "")
            limit = params.get("limit", 5)
            return {"ok": True, "result": search_skills(query, limit=limit)}

        @register_evolution_tool(
            name="skill_load",
            description="读入某个Skill的完整正文(经验规程)，供你参考着做判断",
            category="meta",
            cost_tier="cheap",
            search_hint="skill load read full content guideline",
            module="skill_library",
            is_deferred=True,
        )
        def _load_tool(params):
            name = params.get("name", "")
            if not name:
                return {"ok": False, "error": "缺少 name 参数"}
            return load_skill(name)
    except Exception:
        pass


# 模块级触发注册：让 evolution_tool_registry.search()/execute() 能直接
# 发现 skill_search / skill_load 这两个工具，agent 用同一套心智模型
# 就能同时找到"可执行工具"和"经验知识skill"。
_register_self()


if __name__ == "__main__":
    import sys
    import json as _json

    if "--list" in sys.argv:
        skills = list_skills()
        print(f"=== {len(skills)} skills registered ===")
        for s in skills:
            print(f"  [{s['category']:10}] {s['name']:28} {s['description'][:60]}")
    elif "--search" in sys.argv:
        idx = sys.argv.index("--search") + 1
        q = sys.argv[idx] if idx < len(sys.argv) else ""
        results = search_skills(q)
        print(f"=== search '{q}' → {len(results)} hits ===")
        for r in results:
            print(f"  [{r['score']:.2f}] {r['name']:28} {r['description'][:60]}")
    elif "--load" in sys.argv:
        idx = sys.argv.index("--load") + 1
        name = sys.argv[idx] if idx < len(sys.argv) else ""
        r = load_skill(name)
        print(_json.dumps(r, ensure_ascii=False, indent=2))
    elif "--test" in sys.argv:
        print("=== skill_library smoke test ===")
        skills = list_skills()
        assert len(skills) >= 3, f"应该至少发现3个示范skill, 实际: {len(skills)}"
        print(f"✅ 发现 {len(skills)} 个 skill: {[s['name'] for s in skills]}")

        hits = search_skills("过度自信 断言")
        assert hits, "搜索'过度自信'应该命中 overconfidence-detection"
        assert hits[0]["name"] == "overconfidence-detection", f"最佳匹配应是 overconfidence-detection, 实际: {hits[0]}"
        print(f"✅ 关键词搜索命中: {hits[0]['name']} (score={hits[0]['score']})")

        loaded = load_skill("rubric-gate-usage")
        assert loaded["ok"], f"应能成功加载: {loaded}"
        assert len(loaded["body"]) > 200, "正文应该有实质内容"
        assert "run_with_retry" in loaded["body"], "正文应包含关键 API 名"
        print(f"✅ 加载 skill 正文成功, {len(loaded['body'])} 字符")

        not_found = load_skill("nonexistent-skill-xyz")
        assert not not_found["ok"], "不存在的skill应该返回失败"
        print("✅ 不存在的skill正确返回失败")

        print("\n🎉 skill_library 自测通过")
    else:
        print("用法: --list | --search <kw> | --load <name> | --test")
