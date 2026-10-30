# -*- coding: utf-8 -*-
"""Supervision CV stdio MCP server(真实工具,非 mock)。

暴露两个安全/无副作用工具:
- supervision_info:版本与核心能力
- detections_from_xyxy:把 xyxy/confidence/class_id 构造成 supervision.Detections 并回传记录

后续可继续加 annotate/track/zone 等工具;统一 MCP 桥无需改。
"""
from __future__ import annotations
import json, sys

_PROTO = "2024-11-05"
_TOOLS = [
    {"name": "supervision_info", "description": "返回已安装supervision版本与核心CV能力",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "detections_from_xyxy",
     "description": "用真实supervision.Detections构造检测结果并返回记录",
     "inputSchema": {"type": "object", "properties": {
         "xyxy": {"type": "array", "items": {"type": "array",
                  "items": {"type": "number"}, "minItems": 4, "maxItems": 4}},
         "confidence": {"type": "array", "items": {"type": "number"}},
         "class_id": {"type": "array", "items": {"type": "integer"}}},
         "required": ["xyxy"]}},
]


def _result(text: str):
    return {"content": [{"type": "text", "text": text}]}


def _call(name, args):
    import supervision as sv
    if name == "supervision_info":
        caps = [x for x in ("Detections", "BoxAnnotator", "ByteTrack", "PolygonZone")
                if hasattr(sv, x)]
        return _result(json.dumps({"version": sv.__version__, "capabilities": caps}))
    if name == "detections_from_xyxy":
        import numpy as np
        xyxy = np.asarray(args.get("xyxy", []), dtype=float).reshape((-1, 4))
        conf = args.get("confidence")
        cid = args.get("class_id")
        det = sv.Detections(
            xyxy=xyxy,
            confidence=np.asarray(conf, dtype=float) if conf is not None else None,
            class_id=np.asarray(cid, dtype=int) if cid is not None else None,
        )
        rows = []
        for box, _, confidence, class_id, _, _ in det:
            rows.append({"xyxy": box.tolist(), "confidence": (None if confidence is None else float(confidence)),
                         "class_id": (None if class_id is None else int(class_id))})
        return _result(json.dumps({"count": len(det), "detections": rows}))
    raise ValueError(f"unknown_tool:{name}")


def _handle(req):
    m, rid = req.get("method"), req.get("id")
    if m == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": _PROTO, "capabilities": {"tools": {}},
            "serverInfo": {"name": "meyo-supervision", "version": "0.1"}}}
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _TOOLS}}
    if m == "tools/call":
        try:
            p = req.get("params", {})
            return {"jsonrpc": "2.0", "id": rid,
                    "result": _call(p.get("name", ""), p.get("arguments", {}))}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "isError": True, "content": [{"type": "text",
                 "text": f"{type(e).__name__}:{str(e)[:160]}"}]}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method_not_found:{m}"}}


def main():
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except Exception:
            continue
        resp = _handle(req)
        if resp is not None:
            print(json.dumps(resp), flush=True)


if __name__ == "__main__":
    main()
