#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""agent_adapters:全部 agent 的【适配状态】统一总览(用户2026-08-30:adapter要全·适配所有agent).

用户诉求:一定要适配所有 agent(每家都能派/当首脑/被编排)。这里如实汇总每个 agent/能力项目的
真实适配状态·一眼看全"谁能派、谁装一下就能派、谁是能力库走函数调用"——让"适配所有"可见可操作。

三类(诚实区分·不把能力库假装成通用Agent):
- ready       : 有adapter+已就绪·现在就能派(当首脑/被编排)。
- installable : 有adapter声明(transport+install)但没装/没构建·一键 install 后即 ready。
- capability_lib: 不是通用Agent(cua桌面SDK/orca worktree/OpenMontage视频库…)·
                  走 func_invoke 函数调用·不套 agent adapter(套了是假家)。

通用 adapter 机制已全(external_agent_transports: run_cli/run_http/run_jsonrpc/run_sdk)——
任何 agent 只要在 external_agents.json 声明 transport 就能被派·不用为每家写新 adapter。
"""
from __future__ import annotations
import os


def overview() -> dict:
    """全 agent 适配状态总览。ready/installable/capability_lib 三类如实分。"""
    ready, installable, capability_lib, platform_role = [], [], [], []
    # 各家 execution_role(诚实区分:只有真通用 Agent 才该作通用Agent派;
    # orchestrator/ui-platform/control-plane/capability 是平台/编排面·装了也不作通用Agent)。
    _roles = {}
    try:
        import external_agent_registry as _er
        for _it in _er.load_catalog():
            _roles[_it["key"]] = _it.get("execution_role")
    except Exception:
        pass

    def _is_answer_agent(key: str) -> bool:
        return _roles.get(key) not in ("control-plane", "ui-platform", "capability")

    # 1) 内置可派单家(legion_core adapter·军团核心成员)
    try:
        import legion_core
        for key in sorted(legion_core._harness_keys()):
            ready.append({"agent": key, "class": "builtin_harness", "how": "直接派/可当首脑"})
    except Exception:
        pass

    # 2) external 声明家(通用 transport adapter·probe 决定 ready/需装)。
    #    去重:已在 builtin ready 的(cline/goose/openclaw 等两处都登记)不重复列。
    _ready_names = {r["agent"] for r in ready}
    try:
        import external_agent_registry as ext
        for row in ext.probe().get("agents", []):
            key = row["key"]
            if key in _ready_names:
                continue  # builtin 已 ready·不重复
            if row.get("execution_ready"):
                ready.append({"agent": key, "class": "external_agent",
                              "transport": row.get("transport") or "cli/http/jsonrpc",
                              "how": "已就绪·可派(agent_tool_provider/dispatch)"})
                _ready_names.add(key)
            elif _is_answer_agent(key):
                # 真通用 Agent 但没装 → 装了就能派(pi 已验证:install→execution_ready=True)
                installable.append({"agent": key, "class": "external_agent",
                                    "reason": row.get("status") or "未装/未构建/未探到",
                                    "how": f"一键装: external_agent_registry.install('{key}', confirm=True) → 装完自动 ready 进编排军团"})
            else:
                # orchestrator/ui-platform/control-plane → 不是被派活的家·而是【端/平台】。
                # 正确适配(读真实README验证):paperclip=管理AI agent的app·tutti=多人多agent协作空间·
                # grok=桌面UI·aeon=运维控制台+定时跑skill。它们该【作为端反过来调军团当首脑】
                # (legion_client 一行 ask/HTTP /api/legion/dispatch)·不是被军团派活。
                platform_role.append({"agent": key, "class": "platform_client",
                                      "role": _roles.get(key),
                                      "how": "平台/端·正确适配=作为端调军团当首脑(legion_client.ask 或 HTTP "
                                             "/api/legion/dispatch)·不是被军团派活;若要借它某函数走 func_call"})
    except Exception:
        pass

    # 3) ecosystem 能力库(不是通用Agent·走函数调用)
    try:
        import func_index
        idx = func_index.build()
        ready_keys = {r["agent"] for r in ready} | {r["agent"] for r in installable}
        libs = {}
        for e in idx["entries"]:
            fam = e["family"]
            if not fam.startswith("eco:"):
                continue
            name = fam.split(":", 1)[-1]
            if name in ready_keys:
                continue
            libs.setdefault(name, {"funcs": 0})
            libs[name]["funcs"] += 1
        for name, info in sorted(libs.items()):
            capability_lib.append({
                "agent": f"eco:{name}", "class": "capability_lib",
                "functions": info["funcs"],
                "how": "非通用Agent·走 func_search 发现 + func_call 调函数(纯本地直调/装依赖/派对应家)"})
    except Exception:
        pass

    return {
        "ok": True,
        "summary": {
            "ready": len(ready), "installable": len(installable),
            "platform_role": len(platform_role),
            "capability_lib": len(capability_lib),
            "total_addressable": len(ready) + len(installable) + len(capability_lib) + len(platform_role),
        },
        "note": ("适配所有agent=每类都有正确适配路径(不是每家都'被派活'·那对平台/库是错的):"
                 "①通用Agent→被军团派/编排竞选首脑(通用adapter cli/http/jsonrpc/sdk已全·新家声明transport即可);"
                 "②平台/端(paperclip/tutti/grok/aeon)→作为端调军团(legion_client/HTTP dispatch)·读真实README"
                 "验证它们是管理面/协作空间/UI/控制台·不是通用Agent;③能力库(cua/orca…)→func_call调函数。"),
        "ready": ready,
        "installable": installable,
        "platform_role": platform_role,
        "capability_lib": capability_lib,
    }


if __name__ == "__main__":
    import json
    os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
    o = overview()
    s = o["summary"]
    print(f"=== 全 agent 适配状态 ===")
    print(f"能派(ready): {s['ready']} · 装了就能派(installable): {s['installable']} · "
          f"能力库(走函数调用): {s['capability_lib']} · 共 {s['total_addressable']}")
    print(f"\n[ready 能派/可当首脑]")
    for r in o["ready"]:
        print(f"  {r['agent']} ({r['class']})")
    print(f"\n[installable 一键装后能派]")
    for r in o["installable"]:
        print(f"  {r['agent']} — {r['how']}")
    print(f"\n[capability_lib 走函数调用·非通用Agent]")
    for r in o["capability_lib"]:
        print(f"  {r['agent']} ({r['functions']}函数)")
