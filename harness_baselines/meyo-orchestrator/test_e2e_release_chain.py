# -*- coding: utf-8 -*-
"""端到端放行链路回测(不烧模型token,但工具均真执行)。

覆盖 stop-hook 要求的完整链:
1. 目标注入:goal + qhash 经 autonomous_solve 外层固定注入。
2. 连续工具执行:list_templates → instantiate_template → list_harnesses。
3. 失败:模型先调不存在的 MCP server,收到 unknown_server。
4. 自主修复:下一轮调真实 supervision-cv MCP,构造真实 Detections。
5. 最终放行:模型读到修复结果后停止并交最终答案;完整 trace 落盘。

仅模型回复用 deterministic mock(避免真实网关/token),工具桥/模板/harness/MCP/CV/trace 全是真链。
"""
import json, os, sys
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import autoloop as A


def _tool_response(calls):
    return {"choices": [{"message": {"content": None, "tool_calls": calls},
                         "finish_reason": "tool_calls"}]}


def _tc(i, name, args):
    return {"id": f"tc{i}", "function": {"name": name,
            "arguments": json.dumps(args, ensure_ascii=False)}}


def test_full_goal_to_release_chain(tmp_path):
    # 多轮 deterministic captain:发现→实例化→失败→修复→放行。
    responses = [
        _tool_response([_tc(1, "list_templates", {}),
                        _tc(2, "list_harnesses", {})]),
        _tool_response([_tc(3, "instantiate_template", {
            "key": "visual-inspection",
            "params": {"media": "test-frame", "objects_or_issues": "目标框"}})]),
        # 故意失败:不存在 server
        _tool_response([_tc(4, "call_mcp_tool", {
            "server": "missing-server", "name": "detect", "arguments": {}})]),
        # 模型根据失败返回自主修复:切真实 supervision server + 正确工具/schema
        _tool_response([_tc(5, "call_mcp_tool", {
            "server": "supervision-cv", "name": "detections_from_xyxy",
            "arguments": {"xyxy": [[1, 2, 20, 30]],
                          "confidence": [0.95], "class_id": [7]}})]),
        {"choices": [{"message": {"content":
            "已放行:真实CV工具返回1个检测框,class_id=7,confidence=0.95。"},
            "finish_reason": "stop"}]},
    ]
    old_trace = A.TRACE_DIR
    A.TRACE_DIR = tmp_path
    try:
        with mock.patch.object(A, "_post", side_effect=responses):
            out = A.autonomous_solve("检查test-frame中的目标框并验证后放行",
                                     max_steps=8, verbose=False,
                                     qhash="e2e-hook-injection", gold="SECRET_NOT_MODEL_VISIBLE")
    finally:
        A.TRACE_DIR = old_trace

    assert out["n_steps"] == 4, out
    assert "已放行" in out["final"] and "class_id=7" in out["final"]
    assert Path(out["trace_path"]).is_file()

    trace = json.loads(Path(out["trace_path"]).read_text())
    assert trace["status"] == "done" and trace["goal"].startswith("检查test-frame")
    assert len(trace["tool_log"]) == 5
    assert [x["tool"] for x in trace["tool_log"]] == [
        "list_templates", "list_harnesses", "instantiate_template",
        "call_mcp_tool", "call_mcp_tool"]
    # 失败真的发生并进入 trace
    assert "unknown_server" in trace["tool_log"][3]["result_preview"]
    # 修复后的真实 supervision MCP 结果也进入 trace
    assert '"status": "ok"' in trace["tool_log"][4]["result_preview"]
    # gold 不得出现在 trace/模型工具调用/最终输出(防作弊完整链)
    assert "SECRET_NOT_MODEL_VISIBLE" not in Path(out["trace_path"]).read_text()


def test_full_chain_retest_is_deterministic(tmp_path):
    """同目标重测:trace 命名稳定且结果覆盖为最新成功态,证明修复后可重复放行。"""
    stop = {"choices": [{"message": {"content": "重测放行"}, "finish_reason": "stop"}]}
    old_trace = A.TRACE_DIR
    A.TRACE_DIR = tmp_path
    try:
        with mock.patch.object(A, "_post", return_value=stop):
            a = A.autonomous_solve("same-goal", verbose=False, qhash="retest")
            b = A.autonomous_solve("same-goal", verbose=False, qhash="retest")
    finally:
        A.TRACE_DIR = old_trace
    assert a["trace_path"] == b["trace_path"]
    d = json.loads(Path(b["trace_path"]).read_text())
    assert d["status"] == "done" and d["final"] == "重测放行"
