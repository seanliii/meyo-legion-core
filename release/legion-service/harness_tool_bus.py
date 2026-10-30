#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""harness_tool_bus:全家 harness 工具函数互通总线(用户2026-08-30·真生态).

各家源码都在本仓库(harness_baselines/+ecosystem/)·不止CLI→能像auto一样抽工具直调。
本总线统一聚合各家工具·军团任何家可发现+调用:
- auto: capstack_bridge 42工具无状态直调(阶段0·已验证健康✅)。
- hermes/codex/ecosystem/新家: 按各家【工具声明约定】抽·分阶段接入(见 dev/harness-tool-bus-plan.md)。

调用分层:无状态→直调(不派家);有runtime依赖→agent_tool_provider.invoke 派该家执行。
判据:每接一家必端到端测(≥5工具真调不崩)·不达标不算接入(诚实不假装)。
"""
from __future__ import annotations
import os

# 各家工具声明【约定】:决定怎么从源码抽它声明的工具(不盲抽所有函数·避免噪音)。
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HB = os.path.join(_REPO, "harness_baselines")
_TOOL_CONVENTIONS = {
    # harness: {source, style, status}
    "auto": {"source": "seed-users-system/qa_system/agent", "style": "t_+register_tool",
             "status": "bridged", "bridge": "capstack_bridge"},
    "hermes": {"source": "harness_baselines/hermes-agent/tools", "style": "file_per_tool",
               "status": "bridged", "bridge": "hermes_tool_bridge"},  # 阶段1·12工具无状态直调✅(km 2783646281 判据6/6 PASS)
    "codex": {"source": "harness_baselines/codex", "style": "schema",
              "status": "provider_only",
              "note": "核心Rust·py(107)为devcontainer/build辅助·无独立无状态工具集·走 provider 派单借用"},
    "deepseek": {"source": "harness_baselines/deepseek-harness", "style": "register",
                 "status": "provider_only",
                 "note": "py(22)全是sitecustomize/build脚本·无工具函数·走 provider 派单借用"},
    # ecosystem真能力库(本就是工具集·阶段2)
    "dsh-vision": {"source": "harness_baselines/ecosystem/dsh-vision-toolkit", "style": "file_per_script",
                   "status": "bridged", "bridge": "vision_toolkit_bridge"},  # 阶段2首家·5工具直调✅
    "openmontage": {"source": "harness_baselines/ecosystem/OpenMontage", "style": "lib",
                    "status": "provider_only",
                    "note": "tools/都是BaseTool类·需GoogleAPI/Atlas凭证runtime·非无状态·走 provider 派单借用"},
    "qwen-audio": {"source": "harness_baselines/ecosystem/qwen-audio-agent", "style": "lib",
                   "status": "provider_only",
                   "note": "主体是TUI/demo·无独立工具库函数可抽·走 provider 派单借用"},
    # 阶段4:未来新装的 harness(不在 _TOOL_CONVENTIONS 里的家)通过 agent_tool_provider._all_providers 自动纳入
    # 详见 agent_tool_provider.inventory():set(_BUILTIN)|set(external_rows)·new_member=True 标记·端到端已验证
}


def all_tools() -> dict:
    """聚合全家可用工具·军团发现入口。当前:auto桥(已接)+ provider inventory(各家借用)。
    分阶段把 hermes/codex/ecosystem 从 planned→bridged/runnable。"""
    out = {"direct": [], "provider": [], "conventions": _TOOL_CONVENTIONS}
    # 1) auto 无状态直调(阶段0·已验证)
    try:
        import capstack_bridge as cb
        if cb.available():
            out["direct"] += [{"harness": "auto", "tool": t, "call": "direct",
                               "via": "capstack_bridge"} for t in cb.list_bridged_tools()]
    except Exception:
        pass
    # 1b) dsh-vision 脚本工具无状态直调(阶段2·已验证·源码在本仓库→直调不派家)
    try:
        import vision_toolkit_bridge as vt
        if vt.available():
            out["direct"] += vt.tool_meta()
    except Exception:
        pass
    # 1c) hermes tools 无状态直调(阶段1·km 2783646281 6/6 PASS·12工具)
    try:
        import hermes_tool_bridge as hb
        if hb.available():
            out["direct"] += hb.tool_meta()
    except Exception:
        pass
    # 2) 各家 provider 工具(有runtime依赖·派单借用·含新家自动纳入)
    try:
        import agent_tool_provider as atp
        inv = atp.inventory()
        for row in inv.get("providers", []):
            for tool in row.get("tools", []):
                out["provider"].append({"harness": row["provider"], "tool": tool["name"],
                                        "call": "invoke", "new_member": row.get("new_member", False)})
    except Exception:
        pass
    out["summary"] = {"direct": len(out["direct"]), "provider": len(out["provider"]),
                      "harnesses_bridged": sorted({d["harness"] for d in out["direct"]}),
                      "harnesses_provider": sorted({p["harness"] for p in out["provider"]})}
    return out


def call(harness: str, tool: str, params: dict | None = None, **kw) -> dict:
    """统一调用入口:无状态直调优先·否则派该家执行。军团任何家用这个借任意家的工具。"""
    params = params or {}
    # auto 工具走 capstack_bridge 直调
    if harness == "auto":
        try:
            import capstack_bridge as cb
            if cb.available() and cb._find_fn(tool):
                return cb.call(tool, path=params.get("path", ""), question=params.get("q", ""))
        except Exception as e:
            return {"ok": False, "error": f"auto直调失败: {e}"}
    # dsh-vision 脚本工具走 vision_toolkit_bridge 直调(源码在本仓库·无状态·不派家)
    if harness in ("dsh-vision", "vision"):
        try:
            import vision_toolkit_bridge as vt
            if vt.available() and tool in vt.list_bridged_tools():
                return vt.call(tool, params)
        except Exception as e:
            return {"ok": False, "error": f"dsh-vision直调失败: {e}"}
    # hermes tools 走 hermes_tool_bridge 直调(阶段1·纯函数无状态·不派家)
    if harness == "hermes":
        try:
            import hermes_tool_bridge as hb
            # A declared source function stays a direct call even if its source
            # is missing. Do not turn an installation error into a model task.
            if tool in hb.list_bridged_tools():
                return hb.call(tool, params)
        except Exception as e:
            return {"ok": False, "error": f"hermes直调失败: {e}"}
        # 未桥接的 hermes 工具 fallback 到 provider 派 hermes 执行(下面)
    # 其他家:派该家执行(有runtime依赖·阶段1+逐步把无状态的改直调)
    try:
        import agent_tool_provider as atp
        return atp.invoke(harness, tool, params, **atp._call_kwargs(kw))
    except Exception as e:
        return {"ok": False, "error": f"{harness}/{tool} 借用失败: {e}"}


if __name__ == "__main__":
    os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
    t = all_tools()
    print("=== harness工具总线 ===")
    print("直调工具:", t["summary"]["direct"], "· 家:", t["summary"]["harnesses_bridged"])
    print("派单借用工具:", t["summary"]["provider"], "· 家:", t["summary"]["harnesses_provider"])
    print("接入约定:", {k: v["status"] for k, v in t["conventions"].items()})
