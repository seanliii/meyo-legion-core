#!/usr/bin/env python3
"""CowAgent 原生 Agent 工具循环的 headless 单发 worker。"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile


_TOOL_CALL_ONLY = re.compile(r"\s*<tool_calls(?:\s[^>]*)?>", re.DOTALL | re.IGNORECASE)


def _run(source: pathlib.Path, task: str, model: str, appid: str, gateway: str,
         data_dir: str) -> int:
    pathlib.Path(data_dir).mkdir(parents=True, exist_ok=True)
    pathlib.Path(data_dir).chmod(0o700)
    os.environ["COW_DATA_DIR"] = data_dir
    (pathlib.Path(data_dir) / "config.json").write_text(json.dumps({
        "model": model,
        "bot_type": "custom",
        "custom_api_key": appid,
        "custom_api_base": gateway,
        "open_ai_api_key": appid,
        "open_ai_api_base": gateway,
        "temperature": 1,
        "top_p": 0.95,
        "text_to_image": "",
        "expires_in_seconds": 3600,
        "character_desc": "You are a helpful assistant.",
        "channel_type": "terminal",
    }, ensure_ascii=False), encoding="utf-8")
    (pathlib.Path(data_dir) / "config.json").chmod(0o600)

    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    try:
        from config import load_config
        load_config()
        from bridge.bridge import Bridge
        from bridge.context import Context, ContextType
        from bridge.reply import ReplyType
        from agent.workspace import project_store
    except Exception as exc:
        print(json.dumps({"error": f"import cowagent failed:{type(exc).__name__}:{exc}"}))
        return 1

    try:
        session_id = os.environ.get("CW_SESSION_ID") or "legion"
        workdir = os.environ.get("MEYO_WORKDIR") or data_dir
        bridge = Bridge()
        agent_id = bridge.get_agent_bridge().agent_registry.default_agent_id
        project_store.set_project_dir(session_id, workdir, agent_id)
        ctx = Context(ContextType.TEXT, task, {
            "session_id": session_id,
            "receiver": "legion",
            "channel_type": "terminal",
            "agent_id": agent_id,
        })
        reply = bridge.fetch_agent_reply(task, ctx, clear_history=False)
        content = getattr(reply, "content", None) if reply is not None else None
        reply_type = getattr(reply, "type", None) if reply is not None else None
        final = str(content or "").strip()
    except Exception as exc:
        print(json.dumps({"error": f"cowagent agent loop failed:{type(exc).__name__}:{str(exc)[:400]}"}))
        return 1

    failure_fallbacks = ("我现在有点累了", "请再问我一次吧", "请再问我一次")
    if reply_type is not None and reply_type == ReplyType.ERROR:
        print(json.dumps({"error": f"cowagent ReplyType.ERROR:{final[:300]}"}))
        return 1
    if not final or any(text in final for text in failure_fallbacks):
        print(json.dumps({"error": f"cowagent failure fallback/empty:{final[:200] or 'empty'}"}))
        return 1
    if _TOOL_CALL_ONLY.match(final):
        print(json.dumps({"error": "cowagent native agent loop returned an unconsumed tool request",
                          "tool_request": final[:1000]}, ensure_ascii=False))
        return 1
    print(json.dumps({"final": final, "session_id": session_id,
                      "native_checkpoint": {"kind": "cowagent_session", "session_id": session_id}},
                     ensure_ascii=False))
    return 0


def main() -> int:
    source = pathlib.Path(os.environ.get("CW_SOURCE") or "")
    task = os.environ.get("CW_TASK") or ""
    model = os.environ.get("CW_MODEL") or "gpt-5.4"
    appid = os.environ.get("CW_APPID") or ""
    gateway = os.environ.get("CW_GATEWAY") or "https://aigc.sankuai.com/v1/openai/native"
    configured_data_dir = os.environ.get("CW_DATA_DIR") or ""
    if not source.is_dir():
        print(json.dumps({"error": f"cowagent source missing:{source}"}))
        return 2
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    os.environ["NO_PROXY"] = "aigc.sankuai.com,127.0.0.1,localhost"

    if configured_data_dir:
        return _run(source, task, model, appid, gateway, configured_data_dir)
    with tempfile.TemporaryDirectory(prefix="cow_data_") as data_dir:
        return _run(source, task, model, appid, gateway, data_dir)


if __name__ == "__main__":
    raise SystemExit(main())
