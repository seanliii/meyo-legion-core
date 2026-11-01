"""Compact discovery catalog for capabilities every legion member can inspect on demand."""
from __future__ import annotations

import json
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def _skill_finder() -> dict:
    script = HERE / "legion_tool.py"
    ready = script.is_file() and (HERE / "skill_factory.py").is_file()
    params = json.dumps({"query": "<任务描述>", "scope": "local"}, ensure_ascii=False)
    return {
        "id": "skill_finder", "name": "SkillFinder", "ready": ready,
        "description": ("共享 Skill 工厂：query 默认 scope=local，搜索本仓和已装本地技能；"
                        "显式 scope=external/all 才查询外部源。action=list 仅列当前用户已启用的技能。"),
        "invoke": f"python3.12 {str(script)!r} run runnable:skill_finder {params!r}" if ready else None,
        "path": str(script) if ready else None,
        "needs": None if ready else "共享入口缺少随程序提供的 legion_tool.py 或 skill_factory.py",
    }


def search_skills(query: str, agent_type: str = "legion", timeout: int = 120,
                  *, scope: str = "local") -> dict:
    """Reuse the shared tool contract; external discovery remains explicit."""
    skill = _skill_finder()
    if not skill.get("ready") or not skill.get("path"):
        return {"ok": False, "error": skill.get("needs") or "SkillFinder unavailable"}
    started = time.time()
    try:
        import legion_tool
        data = legion_tool.run_tool("runnable:skill_finder", {
            "query": query, "agent_type": agent_type or "legion", "scope": scope,
        }, timeout_s=timeout)
    except Exception as exc:
        return {"ok": False, "error": f"SkillFinder:{type(exc).__name__}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "SkillFinder returned an invalid result"}
    rows = data.get("results")
    count = len(rows) if isinstance(rows, list) else 0
    return {"ok": bool(data.get("ok")), "count": count, "elapsed_s": round(time.time() - started, 2),
            "backend": skill["name"], "results": data}


def inventory() -> dict:
    """Return facts and invocation paths; the caller decides which capability to use."""
    import external_agent_registry
    # Use the same live declarations as execution. Presence is not an install,
    # health check, instance binding or permission to run an action.
    actions = [{
        **row, "name": row["id"], "runnable": True, "live_verified": False,
        "installed": None, "health": "unknown",
        "invoke": "legion_tool.py run " + row["id"] + " "
                  + repr(json.dumps({name: "<填写>" for name in row.get("params", [])},
                                    ensure_ascii=False)),
    } for row in external_agent_registry.capability_actions()["actions"]]
    import media_model_cli
    script = HERE / "legion_tool.py"
    decision_example = json.dumps({
        "profile_id": "<已绑定且端点为/api/alpha的候选ID>", "state": "<上下文>",
        "questions": {"q": {"type": "noul", "instructions": "<调用者定义的判断题>"}},
    }, ensure_ascii=False, separators=(",", ":"))
    model_entries = [{
        **row, "name": row["id"], "ready": True, "live_verified": False,
        "invoke": (f"python3.12 {str(script)!r} run runnable:{row['id']} "
                   + (repr(decision_example) if row["id"] == "model_decisions" else
                      "'{\"domain\":\"programming\"}'" if row["id"] == "model_catalog" else
                      "'{\"prompt\":\"<问题>\",\"domain\":\"programming\",\"model\":\"<目录中的候选>\"}'")),
        "profile_invoke": (f"python3.12 {str(script)!r} run runnable:{row['id']} "
                           + (repr(decision_example) if row["id"] == "model_decisions" else
                              "'{\"profile_id\":\"<已绑定的候选ID>\"}'" if row["id"] == "model_catalog" else
                              "'{\"prompt\":\"<问题>\",\"profile_id\":\"<已绑定的候选ID>\"}'")),
        "profile_requires": "可信父层的本实例私有 profile 与 owner/workspace 绑定；按次调用，不切默认",
    } for row in media_model_cli.model_tool_specs()]
    return {
        "ok": True,
        "discovery": [
            *model_entries,
            *actions,
            {
                "id": "research_capabilities", "name": "统一研究能力层", "ready": True,
                "description": "搜索、抓取、结构化抽取、证据压缩、实体关系、多跳路径、文档和多媒体处理。",
                "invoke": "legion_tool.py run runnable:research_capabilities '{}'",
            },
            {
                "id": "search_sources", "name": "搜索源目录", "ready": True,
                "description": "查看全部搜索体系及 YouTube/X/Reddit/搜狗微信等专线的健康状态。",
                "invoke": "legion_tool.py run runnable:search_sources '{}'",
            },
            _skill_finder(),
            {
                "id": "github_mcp", "name": "GitHub 官方 MCP", "ready": True,
                "description": "搜索仓库、代码、Issue、PR、Commit 与 Release；先列真实 schema 再调用。",
                "invoke": "legion_tool.py run runnable:mcp_list '{}'；随后 mcp_call server=github",
                "fallback": "legion_tool.py run auto_tool:search_github",
            },
            {
                "id": "internal_tools", "name": "内部与共享工具目录", "ready": True,
                "description": "检索各 harness 内置工具、可执行工具、源码参考和军团自产能力。",
                "invoke": "legion_tool.py search \"<能力描述>\"；或 legion_tool.py list [cap]",
            },
            {
                "id": "source_audit", "name": "源码审核、历史复用与自优化", "ready": True,
                "description": ("开工前检索当前源码、已有证据、Git全历史和交接点，明确reuse/extend/replace；"
                                "再审核自己或同伴的真实源码并核验file:line与运行证据。"),
                "invoke": ("legion_tool.py audit-source '{\"action\":\"prior_art\","
                           "\"queries\":[\"功能名\",\"历史术语\"]}'；"
                           "再用 action=prepare 建立源码审核"),
                "skill": str(REPO / ".claude" / "skills" / "source-audit-evolve" / "SKILL.md"),
            },
            {
                "id": "local_code", "name": "本地代码搜索", "ready": True,
                "description": "通过 shell/grep 或 filesystem MCP 搜索当前仓库与已授权目录。",
                "invoke": "优先使用 harness 原生代码搜索；MCP 路径先 mcp_list，再调用 filesystem.search_files",
            },
            {
                "id": "remote_code", "name": "远程代码搜索", "ready": True,
                "description": "通过 GitHub 官方 MCP 的 search_code 搜索 GitHub 代码。",
                "invoke": "legion_tool.py run runnable:mcp_call '{\"server\":\"github\",\"name\":\"search_code\",\"arguments\":{...}}'",
            },
            {
                "id": "mcp_catalog", "name": "MCP 动态目录", "ready": True,
                "description": "列出所有已注册 MCP 及真实工具 schema，不将上百个 schema 常驻上下文。",
                "invoke": "legion_tool.py run runnable:mcp_list '{}'",
            },
        ],
        "principle": "需要能力时先查看目录；目录只提供事实与入口，由 agent 自己判断调用哪个。",
    }
