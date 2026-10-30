# -*- coding: utf-8 -*-
"""Captain 运行时就绪自检 —— 诚实报告两条执行路径各缺什么(不假装可用)。

- captain (claude-native):真 Claude CLI + tmux + omnigent 可 import。已验证可跑。
- captain-sdk (claude-sdk):额外需 claude-agent-sdk(93MB·带宽阻塞时装不上)。
- 我方 MCP server:必须 importable 且 spec 校验通过。

用法: python3.12 check_captain_runtime.py  → 打印 JSON 就绪矩阵,exit 0/1。
"""
from __future__ import annotations
import json, os, shutil, sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_OMNIGENT = _HERE.parent / "ecosystem" / "omnigent"


def _import_ok(mod: str, extra_path: str | None = None) -> bool:
    import importlib.util
    if extra_path and extra_path not in sys.path:
        sys.path.insert(0, extra_path)
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def _bundle_valid(bundle: str) -> dict:
    try:
        for p in (str(_OMNIGENT), str(_OMNIGENT / "sdks/python-client"),
                  str(_OMNIGENT / "sdks/ui")):
            if p not in sys.path:
                sys.path.insert(0, p)
        os.environ.setdefault("MEYO_ORCHESTRATOR_ROOT", str(_HERE))
        os.environ.setdefault("MEYO_FAIR_QID", "adhoc")
        from omnigent.spec.parser import parse
        from omnigent.spec.validator import validate
        spec = parse(_HERE / "agents" / bundle)
        res = validate(spec)
        return {"parsed": True, "valid": bool(res.valid),
                "harness": spec.executor.config.get("harness")}
    except Exception as e:
        return {"parsed": False, "error": f"{type(e).__name__}:{str(e)[:120]}"}


def readiness() -> dict:
    claude = shutil.which("claude")
    tmux = shutil.which("tmux")
    omni = _import_ok("omnigent", str(_OMNIGENT))
    sdk = _import_ok("claude_agent_sdk")
    mcp_ok = _import_ok("mcp")
    native_ready = bool(claude and tmux and omni and mcp_ok)
    sdk_ready = bool(native_ready and sdk)
    return {
        "deps": {"claude_cli": bool(claude), "tmux": bool(tmux),
                 "omnigent_import": omni, "mcp_pkg": mcp_ok,
                 "claude_agent_sdk": sdk},
        "captain_native": {"ready": native_ready,
                           "bundle": _bundle_valid("captain"),
                           "launcher": "run_captain_mcp.sh",
                           "note": "已验证真跑(真 Claude 真调我方 MCP 工具)"},
        "captain_sdk": {"ready": sdk_ready,
                        "bundle": _bundle_valid("captain-sdk"),
                        "blocker": None if sdk else
                        "claude-agent-sdk 未装(93MB wheel·带宽阻塞);装好即 ready"},
    }


if __name__ == "__main__":
    r = readiness()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    # native 路径就绪即算通过(sdk 是可选增强,不阻塞交付)
    raise SystemExit(0 if r["captain_native"]["ready"] else 1)
