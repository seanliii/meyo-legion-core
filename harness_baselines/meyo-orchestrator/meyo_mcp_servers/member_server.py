#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单个军团成员的 Omnigent MCP 执行桥。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import orchestrator as O

_PROTO = "2024-11-05"
_MEMBER = os.environ.get("MEYO_MEMBER_KEY", "").strip()
_QHASH = os.environ.get("MEYO_FAIR_QID", "adhoc")
_SCHEMA = [{
    "name": "run_assigned_harness",
    "description": "调用该 AgentSpec 绑定的真实 Meyo harness，返回原始状态、输出和产物。",
    "inputSchema": {
        "type": "object",
        "properties": {"task": {"type": "string"}},
        "required": ["task"],
        "additionalProperties": False,
    },
}]


def _result(value):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)[:60000]}]}


def _handle(request):
    method, request_id = request.get("method"), request.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": _PROTO, "capabilities": {"tools": {}},
            "serverInfo": {"name": f"meyo-member-{_MEMBER}", "version": "1"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _SCHEMA}}
    if method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") != "run_assigned_harness":
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": "method_not_found"}}
        args = params.get("arguments") or {}
        return {"jsonrpc": "2.0", "id": request_id, "result": _result(
            O.run_harness(_MEMBER, str(args.get("task") or ""), qhash=_QHASH, gold=""))}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"method_not_found:{method}"}}


def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = _handle(request)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32000,
                                        "message": f"{type(exc).__name__}:{exc}"}},
                             ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
