# -*- coding: utf-8 -*-
"""编排工具 stdio MCP server —— 把 orchestrator/mcp_bridge/skills 的客观工具统一暴露为 MCP。

用途:让【真 Omnigent supervisor】(captain.yaml)通过标准 MCP 拿到与 autoloop 完全同一套
工具,从而用生产级 supervisor 替代自研 tool-loop。工具语义/护栏与 autoloop 保持一致:
qhash/gold 不经模型(此 server 固定为 adhoc·真实解题时由上层注入环境变量),不含任何路由硬编码。
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent                       # meyo-orchestrator/
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "meyo_harnesses"))
_SHARED_MODE = "--shared-capabilities" in sys.argv
if _SHARED_MODE:
    # Reuse this stdio protocol, but never expose orchestration/judging tools
    # merely because an execution member needs a host-owned capability.
    sys.path.insert(0, str(_ROOT.parents[1] / "release" / "legion-service"))
    import native_capability_host
    _shared = native_capability_host.from_environment()
else:
    import orchestrator as O
    import mcp_bridge as MB
    import skills as SK
    import templates as ST
    import health as HE
    import cascade as CX

_PROTO = "2024-11-05"
# qhash/gold 护栏:与 autoloop 一致·不让模型自传(防串题/防作弊)。由环境固定注入。
_QHASH = os.environ.get("MEYO_FAIR_QID", "adhoc")
_GOLD = ""   # gold 永不进模型侧

# 工具定义(名/描述/schema)+ 实现映射。与 autoloop.TOOLS_SCHEMA 语义对齐。
_TOOLS = [
    ("list_harnesses", "列出可调 harness(客观·无评分)", {"type": "object", "properties": {}},
     lambda a: O.list_harnesses()),
    ("harness_stats", "各家客观聚合统计(命中率/中位耗时/中位搜索数)", {"type": "object", "properties": {}},
     lambda a: O.harness_stats()),
    ("read_harness_history", "读某家累计历史结果", {"type": "object", "properties": {
        "harness": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["harness"]},
     lambda a: O.read_harness_history(a["harness"], a.get("limit", 20))),
    ("probe_harness_latency", "对某家实时测速(2+2)", {"type": "object", "properties": {
        "harness": {"type": "string"}, "timeout_s": {"type": "integer"}}, "required": ["harness"]},
     lambda a: O.probe_harness_latency(a["harness"], a.get("timeout_s", 30))),
    ("read_trace", "深读一次完整 trace", {"type": "object", "properties": {
        "trace_path": {"type": "string"}}, "required": ["trace_path"]},
     lambda a: O.read_trace(a["trace_path"])),
    ("run_harness", "真调某家执行任务(qhash/gold外层固定)", {"type": "object", "properties": {
        "harness": {"type": "string"}, "task": {"type": "string"}}, "required": ["harness", "task"]},
     lambda a: O.run_harness(a["harness"], a["task"], qhash=_QHASH, gold=_GOLD)),
    ("judge_answer", "让裁判 harness 独立复核候选答案", {"type": "object", "properties": {
        "question": {"type": "string"}, "candidate_answer": {"type": "string"},
        "judge_harness": {"type": "string"}, "timeout_s": {"type": "integer"}},
        "required": ["question", "candidate_answer"]},
     lambda a: O.judge_answer(a["question"], a["candidate_answer"],
                              a.get("judge_harness", "cc"), a.get("timeout_s", 120))),
    ("list_mcp_tools", "列出所有已注册 MCP server 的工具", {"type": "object", "properties": {}},
     lambda a: MB.list_mcp_tools()),
    ("call_mcp_tool", "真调某 MCP 工具", {"type": "object", "properties": {
        "server": {"type": "string"}, "name": {"type": "string"}, "arguments": {"type": "object"}},
        "required": ["server", "name"]},
     lambda a: MB.call_mcp_tool(a["server"], a["name"], a.get("arguments") or {})),
    ("list_skills", "列出某 skill 源可用 SKILL.md", {"type": "object", "properties": {
        "source": {"type": "string"}}},
     lambda a: SK.list_skills(a.get("source", "pm-skills"))),
    ("read_skill", "读取一个 skill 的完整内容", {"type": "object", "properties": {
        "source": {"type": "string"}, "path": {"type": "string"}}, "required": ["source", "path"]},
     lambda a: SK.read_skill(a["source"], a["path"])),
    ("list_templates", "列出10个C端场景模板(目标/验收/可选工具·不绑定harness)",
     {"type": "object", "properties": {}}, lambda a: ST.list_templates()),
    ("instantiate_template", "实例化一个场景模板为清晰目标+验收标准",
     {"type": "object", "properties": {"key": {"type": "string"},
      "params": {"type": "object"}}, "required": ["key"]},
     lambda a: ST.instantiate_template(a["key"], a.get("params") or {})),
    ("health_snapshot", "查看当前harness熔断与活跃子进程健康状态",
     {"type": "object", "properties": {}},
     lambda a: HE.health_snapshot(_ROOT / "runs")),
    ("run_cascade", "置信级联:CC主执行,疑似失败自动升级DeepAgents(40题17→21)",
     {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]},
     lambda a: CX.run_cascade(a["task"], qhash=_QHASH, gold=_GOLD)),
]
if _SHARED_MODE:
    _TOOLS = _shared.tools
_IMPL = {name: fn for name, _d, _s, fn in _TOOLS}
_SCHEMA = [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in _TOOLS]


def _result(obj):
    text = json.dumps(obj, ensure_ascii=False)
    if _SHARED_MODE and isinstance(obj, native_capability_host.CapabilityImageResult):
        import base64
        return {"content": [
            {"type": "text", "text": text},
            {"type": "image", "mimeType": "image/png",
             "data": base64.b64encode(obj.png).decode("ascii")},
        ]}
    return {"content": [{"type": "text", "text": text if _SHARED_MODE else text[:60000]}],
            **({"isError": True} if _SHARED_MODE and obj.get("ok") is False else {})}


def _handle(req):
    m, rid = req.get("method"), req.get("id")
    if m == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": _PROTO, "capabilities": {"tools": {}},
            "serverInfo": {"name": "meyo-capabilities" if _SHARED_MODE else "meyo-orchestrator",
                           "version": "0.1"}}}
    if m == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _SCHEMA}}
    if m == "tools/call":
        p = req.get("params", {})
        name = p.get("name", "")
        try:
            out = _IMPL[name](p.get("arguments", {}) or {})
            return {"jsonrpc": "2.0", "id": rid, "result": _result(out)}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "isError": True, "content": [{"type": "text",
                 "text": f"{type(e).__name__}:{str(e)[:160]}"}]}}
    if rid is None:
        return None
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method_not_found:{m}"}}


def _shared_stdio_loop():
    """一个工具执行线程；主线程始终接收取消、目录查询和关闭信号。"""
    import select
    import signal
    import threading

    state_lock = threading.Lock()
    output_lock = threading.Lock()
    active = None
    worker = None
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        # signal handler 不取锁；即使打断主线程持锁区也不会自锁。
        stopping = True

    def emit(response):
        nonlocal stopping
        if response is not None:
            try:
                with output_lock:
                    print(json.dumps(response), flush=True)
            except (BrokenPipeError, OSError):
                stopping = True

    def tool_error(request_id, message):
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "isError": True, "content": [{"type": "text", "text": message}]}}

    def execute(request, cancel_event):
        nonlocal active
        try:
            with _shared.cancellation_scope(cancel_event.is_set):
                response = _handle(request)
        except BaseException as exc:
            response = tool_error(
                request.get("id"), f"{type(exc).__name__}:{str(exc)[:160]}")
        # 清理和 cancellation_scope 退出后才返回终态；客户端收到结果即可再调。
        with state_lock:
            active = None
            emit(response)

    def consume(line):
        nonlocal active, worker
        try:
            request = json.loads(line)
        except (ValueError, UnicodeError):
            return
        if not isinstance(request, dict):
            return
        method = request.get("method")
        if method == "notifications/cancelled":
            params = request.get("params")
            request_id = params.get("requestId") if isinstance(params, dict) else None
            with state_lock:
                if (active is not None and request_id is not None
                        and type(request_id) is type(active[0]) and request_id == active[0]):
                    active[1].set()
            return
        if method != "tools/call":
            emit(_handle(request))
            return
        with state_lock:
            if active is not None:
                emit(tool_error(request.get("id"),
                                "capability_call_busy: another tools/call is still running"))
                return
            cancel_event = threading.Event()
            active = (request.get("id"), cancel_event)
            worker = threading.Thread(
                target=execute, args=(request, cancel_event),
                name="meyo-shared-capability", daemon=False)
            try:
                worker.start()
            except BaseException:
                active = None
                worker = None
                raise

    previous_term = signal.signal(signal.SIGTERM, stop)
    try:
        # 不用 TextIO.readline：半行输入和内部预读都不能挡住 TERM/下一条消息。
        pending = b""
        stdin_fd = sys.stdin.fileno()
        while not stopping:
            readable, _, _ = select.select([stdin_fd], [], [], 0.1)
            if not readable:
                continue
            chunk = os.read(stdin_fd, 65536)
            if not chunk:
                if pending.strip():
                    consume(pending)
                break
            pending += chunk
            while b"\n" in pending and not stopping:
                line, pending = pending.split(b"\n", 1)
                consume(line)
    finally:
        try:
            with state_lock:
                if active is not None:
                    active[1].set()
            if worker is not None:
                worker.join()  # EOF/TERM 不得先注销 host 再把清理中的工具留在后台。
        finally:
            signal.signal(signal.SIGTERM, previous_term)


def main():
    if _SHARED_MODE:
        import legion_proc_registry
        parent = os.getppid()
        if parent <= 1:
            raise RuntimeError("native capability host has no living caller")
        if os.getpgrp() != os.getpid():
            os.setpgid(0, 0)
        if os.getppid() != parent or os.getpgrp() != os.getpid():
            raise RuntimeError("native capability process ownership changed")
        registered = legion_proc_registry.register(
            os.getpid(), pgid=os.getpgrp(), owner=parent,
            meta={"kind": "native-shared-capabilities",
                  "user_id": _shared.context["user_id"],
                  "session_id": _shared.context["session_id"]})
        if not registered:
            raise RuntimeError("native capability process registration failed")
    try:
        if _SHARED_MODE:
            _shared_stdio_loop()
            return
        for line in sys.stdin:
            try:
                req = json.loads(line)
            except Exception:
                continue
            resp = _handle(req)
            if resp is not None:
                print(json.dumps(resp), flush=True)
    finally:
        if _SHARED_MODE:
            legion_proc_registry.unregister(os.getpid())


if __name__ == "__main__":
    main()
