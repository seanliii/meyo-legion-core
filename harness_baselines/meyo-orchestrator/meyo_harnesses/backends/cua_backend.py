# -*- coding: utf-8 -*-
"""Cua Computer Use 后端 adapter(经 cua-mcp-server)。

安全边界:本 adapter 默认只做可用性/工具发现。真实会改变桌面状态的动作必须由 captain
明确通过 call_mcp_tool 发起;不会在 probe/run 中擅自点击、输入或启动 VM。
"""
from __future__ import annotations
import shutil, subprocess, time
from pathlib import Path

_CUA_SRC = Path(__file__).resolve().parents[3] / "ecosystem" / "cua"


def _server_bin() -> str | None:
    return shutil.which("cua-mcp-server")


def probe() -> dict:
    """诚实探测:不仅看 entrypoint 是否在 PATH,还确认其运行时依赖(computer/agent)可导入。

    仅装 entrypoint 而缺 cua-computer/cua-agent 时,server 一启动就 ImportError——那不算可用。
    """
    b = _server_bin()
    if not b:
        return {"backend": "cua", "available": False, "mcp_server": None,
                "source_present": _CUA_SRC.is_dir(),
                "reason": "cua-mcp-server_not_installed",
                "hint": f"python3.12 -m pip install '{_CUA_SRC / 'libs/python/mcp-server'}'"}
    # 探运行时依赖:computer/agent 是否可导入(server.py 顶层就 import)。
    # 健康探测必须快:--help 若卡(cua-mcp-server 常在打印 help 前尝试初始化 Computer/Agent
    # 运行时或连 VM/云,能挂满 30s)会拖垮整个 capabilities 端点(每次缓存过期都付 30s)。
    # 探测超时收窄到 3s——3s 内不返回 = 运行时明显不健康,当不可用处理,不阻塞总线。
    try:
        r = subprocess.run([b, "--help"], capture_output=True, text=True, timeout=3)
        blob = (r.stdout or "") + (r.stderr or "")
        if "Failed to import Computer/Agent" in blob or "ModuleNotFoundError" in blob:
            return {"backend": "cua", "available": False, "mcp_server": b,
                    "source_present": _CUA_SRC.is_dir(),
                    "reason": "cua_runtime_deps_missing(cua-computer/cua-agent)",
                    "hint": f"python3.12 -m pip install '{_CUA_SRC / 'libs/python/mcp-server'}' (full deps + VM/cloud)"}
        return {"backend": "cua", "available": True, "mcp_server": b,
                "source_present": _CUA_SRC.is_dir()}
    except Exception as e:
        return {"backend": "cua", "available": False, "mcp_server": b,
                "source_present": _CUA_SRC.is_dir(),
                "reason": f"probe_err:{type(e).__name__}:{str(e)[:100]}"}


def run(qhash: str, task: str, gold: str = "") -> dict:
    """客观返回 Cua MCP 可用状态;实际 tool 调用走共享 call_mcp_tool。

    不把自然语言 task 自动映射成桌面动作——那会把模型的自主决策偷偷写死在 adapter。
    """
    t0 = time.time(); p = probe()
    if not p["available"]:
        return {"harness": "cua", "status": "unavailable", "final": "",
                **p, "elapsed": round(time.time() - t0, 1)}
    return {"harness": "cua", "status": "ok",
            "final": "Cua MCP server 可用;请用 list_mcp_tools/call_mcp_tool 自主选择桌面工具。",
            **p, "elapsed": round(time.time() - t0, 1)}
