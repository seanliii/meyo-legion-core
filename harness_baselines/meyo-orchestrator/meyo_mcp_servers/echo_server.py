# -*- coding: utf-8 -*-
"""最小 stdio MCP echo server —— 用于给 mcp_bridge 做 live smoke(无外部依赖)。

严格实现 MCP over stdio(JSON-RPC 2.0):initialize / tools/list / tools/call。
只有一个工具 echo,把入参回显。证明桥的传输/握手/调用链真通,不 mock。
"""
from __future__ import annotations
import json, sys

_PROTO = "2024-11-05"
_TOOLS = [{
    "name": "echo",
    "description": "回显传入的 text(用于连通性验证)",
    "inputSchema": {"type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"]},
}]


def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _handle(req):
    m = req.get("method")
    rid = req.get("id")
    if m == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": _PROTO, "capabilities": {"tools": {}},
            "serverInfo": {"name": "meyo-echo", "version": "0.1"}}}
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _TOOLS}}
    if m == "tools/call":
        p = req.get("params", {})
        text = (p.get("arguments") or {}).get("text", "")
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": f"echo:{text}"}]}}
    if rid is None:  # notification(如 notifications/initialized)→ 不回
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method_not_found:{m}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        resp = _handle(req)
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()
