#!/usr/bin/env python3
"""OmniAgent headless 单发 worker(军团 legion cli transport 用)。

omniagent 的 CLI 命令(serve/config/onboard/doctor/chat)里 chat 是纯交互 REPL(prompt "You>"),
没有 `omniagent run <task>` 单发子命令——旧 catalog 的 `omniagent run {task}` 命令根本不存在。
它真正的单发能力在 ReflexionAgent.execute(task) → AgentResult{success,response,error}。
本 worker 直接构造 config(provider=openai·api_url 指网关·openai_api_key=appid)+ ReflexionAgent,
异步 execute 一轮,把 response 打一行 JSON 到 stdout,让 cli transport 的 _final_from_json 提取。
参数走 env(OA_*)。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import pathlib


def main() -> int:
    source = pathlib.Path(os.environ.get("OA_SOURCE") or "")
    task = os.environ.get("OA_TASK") or ""
    model = os.environ.get("OA_MODEL") or "gpt-5.4"
    appid = os.environ.get("OA_APPID") or ""
    gateway = os.environ.get("OA_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native"
    workdir = os.environ.get("OA_WORKDIR") or os.getcwd()
    if not source.is_dir():
        print(json.dumps({"error": f"omniagent source missing:{source}"}))
        return 2
    # 清死代理(血泪:子进程继承死代理→请求卡死)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "aigc.sankuai.com,127.0.0.1,localhost"
    # omniagent 的 openai provider 会读 OPENAI_API_KEY/OPENAI_BASE_URL
    os.environ.setdefault("OPENAI_API_KEY", appid)
    os.environ.setdefault("OPENAI_BASE_URL", gateway)
    os.environ.setdefault("OPENAI_API_BASE", gateway)
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

    try:
        from omniagent.config.models import OmniAgentConfig, AgentConfig
        from omniagent.agents.reflexion import ReflexionAgent
    except Exception as exc:
        print(json.dumps({"error": f"import omniagent failed:{type(exc).__name__}:{exc}"}))
        return 1

    try:
        # temperature=1:omniagent 默认 0.7,但网关部分模型(kimi-k2.7-code)只允许 temperature=1
        # (HTTP 400)。设 1 对所有模型合法·避开该兼容坑(速度底线·少一次换模型)。
        cfg = OmniAgentConfig(
            agent=AgentConfig(model_provider="openai", model_id=model, api_url=gateway,
                              max_iterations=12, temperature=1.0),
            openai_api_key=appid,
            # Legion already authorizes local shell/files/tools through ProductPermissionPolicy.
            # This worker is headless, so OmniAgent's second approval queue has no consumer and
            # turns every bash discovery call into a 120-second timeout loop.
            enable_security_guard=False,
        )
    except Exception as exc:
        print(json.dumps({"error": f"config build failed:{type(exc).__name__}:{exc}"}))
        return 1

    async def _run() -> str:
        agent = ReflexionAgent(config=cfg, work_dir=pathlib.Path(workdir))
        result = await agent.execute(task)
        if getattr(result, "success", False):
            return str(getattr(result, "response", "") or "")
        raise RuntimeError(getattr(result, "error", None) or "omniagent execute failed")

    try:
        final = asyncio.run(_run())
    except Exception as exc:
        print(json.dumps({"error": f"execute failed:{type(exc).__name__}:{str(exc)[:400]}"}))
        return 1
    if not final.strip():
        print(json.dumps({"error": "omniagent empty response"}))
        return 1
    print(json.dumps({"final": final, "session_id": "omniagent"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
