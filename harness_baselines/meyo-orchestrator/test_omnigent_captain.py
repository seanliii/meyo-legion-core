# -*- coding: utf-8 -*-
"""真 Omnigent supervisor 接线测试。

1) orchestrator_server 暴露的工具名 == autoloop 的工具集(同一套·不分叉)。
2) captain 生产 bundle 经 Omnigent 自己的 parser+validator 校验通过(spec 合法)。
   omnigent 不可用时 skip(诚实·不假绿)。
"""
import os, sys, json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "meyo_harnesses"))


def test_server_tools_match_autoloop():
    import autoloop as A
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "orchestrator_server", _HERE / "meyo_mcp_servers" / "orchestrator_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    server_tools = {t["name"] for t in mod._SCHEMA}
    autoloop_tools = set(A.TOOLS_IMPL)
    assert server_tools == autoloop_tools, (server_tools ^ autoloop_tools)


def test_captain_bundle_valid_via_omnigent():
    omnigent_src = _HERE.parent / "ecosystem" / "omnigent"
    if not omnigent_src.is_dir():
        import pytest
        pytest.skip("omnigent 源缺失·跳过")
    sys.path.insert(0, str(omnigent_src))
    try:
        from omnigent.spec.parser import parse
        from omnigent.spec.validator import validate
    except Exception as e:
        import pytest
        pytest.skip(f"omnigent import 失败·跳过: {type(e).__name__}")
    os.environ.setdefault("MEYO_ORCHESTRATOR_ROOT", str(_HERE))
    os.environ.setdefault("MEYO_ORCHESTRATOR_SERVER",
                          str(_HERE / "meyo_mcp_servers" / "orchestrator_server.py"))
    os.environ.setdefault("MEYO_FAIR_QID", "adhoc")
    spec = parse(_HERE / "agents" / "captain")
    assert spec.name == "meyo-captain"
    assert [s.name for s in (spec.mcp_servers or [])] == ["orchestrator"]
    assert {skill.name for skill in spec.skills} == {
        "delivery-design", "fanout", "investigate", "synthesis-delivery"
    }
    assert "禁止再创建或派发任何子 Agent/fork" in next(
        skill.content for skill in spec.skills if skill.name == "investigate"
    )
    assert "成员不得继续创建 Agent 或 fork" in next(
        skill.content for skill in spec.skills if skill.name == "fanout"
    )
    assert "没有最终产物就没有完成" in next(
        skill.content for skill in spec.skills if skill.name == "synthesis-delivery"
    )
    delivery = next(skill.content for skill in spec.skills if skill.name == "delivery-design")
    assert "manifest_version" in delivery and "逐个检查文件存在" in delivery
    res = validate(spec)
    assert res.valid, getattr(res, "errors", None)


def test_real_captain_calls_our_mcp_tool_live():
    """真 Claude LLM 经 --mcp-config 直挂我方 orchestrator server,自主调 list_templates。

    需真实 Claude 订阅·默认跳过(MEYO_LIVE_CAPTAIN=1 才跑·避免烧额度)。
    这是 Omnigent native-Claude relay 未 materialize 外部 MCP 时的已验证可用路径。
    """
    if os.environ.get("MEYO_LIVE_CAPTAIN") != "1":
        import pytest
        pytest.skip("需 MEYO_LIVE_CAPTAIN=1 + Claude 订阅·默认跳过")
    import subprocess
    r = subprocess.run(["bash", str(_HERE / "run_captain_mcp.sh"),
                        "调用 list_templates 工具,只用一句话回答模板数量。",
                        "mcp__meyo-orchestrator__list_templates"],
                       capture_output=True, text=True, timeout=200, cwd=str(_HERE))
    out = json.loads(r.stdout)
    assert out.get("is_error") is False, out
    assert "10" in str(out.get("result", "")), out


def test_real_captain_dispatches_backend_via_run_harness_live():
    """真 captain 经我方 MCP 的 run_harness 真派 supervision 后端(证明可替代 autoloop 的编排动作)。

    需 MEYO_LIVE_CAPTAIN=1 + Claude 订阅·默认跳过。
    """
    if os.environ.get("MEYO_LIVE_CAPTAIN") != "1":
        import pytest
        pytest.skip("需 MEYO_LIVE_CAPTAIN=1·默认跳过")
    import subprocess
    r = subprocess.run(["bash", str(_HERE / "run_captain_mcp.sh"),
                        '用 run_harness 调 harness=supervision, task="check",'
                        '一句话报告返回的 status。'],
                       capture_output=True, text=True, timeout=240, cwd=str(_HERE))
    out = json.loads(r.stdout)
    assert out.get("is_error") is False, out
    assert "ok" in str(out.get("result", "")).lower(), out


def test_real_captain_failure_repair_release_live():
    """全真链:真实LLM→真实MCP失败→自主修复→真派后端→最终放行(无mock)。

    断言基于 stream-json 里【结构化 tool_use/tool_result 事实】,不靠 LLM 自由措辞(防脆)。
    """
    if os.environ.get("MEYO_LIVE_CAPTAIN") != "1":
        import pytest
        pytest.skip("需 MEYO_LIVE_CAPTAIN=1·默认跳过")
    import subprocess
    goal = ('严格按序、且【不要调用 judge_answer】:'
            '1) call_mcp_tool 调 server="does-not-exist" 触发失败;'
            '2) 失败后调 list_templates 修复;'
            '3) run_harness 调 harness=supervision task="check";'
            '4) 一句话汇报结束。')
    r = subprocess.run(["bash", str(_HERE / "run_captain_mcp.sh"), goal, "", "stream-json"],
                       capture_output=True, text=True, timeout=240, cwd=str(_HERE))
    tool_uses, unknown_seen, sup_ok = [], False, False
    for ln in r.stdout.splitlines():
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if ev.get("type") == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    tool_uses.append(c.get("name"))
        if ev.get("type") == "user":
            for c in ev.get("message", {}).get("content", []):
                txt = json.dumps(c, ensure_ascii=False)
                if "unknown_server" in txt:
                    unknown_seen = True
                if '\\"status\\": \\"ok\\"' in txt or '"status": "ok"' in txt:
                    sup_ok = True
    assert "mcp__meyo-orchestrator__call_mcp_tool" in tool_uses, tool_uses
    assert "mcp__meyo-orchestrator__list_templates" in tool_uses, tool_uses
    assert "mcp__meyo-orchestrator__run_harness" in tool_uses, tool_uses
    assert unknown_seen, "未观察到真实 unknown_server 失败"
    assert sup_ok, "未观察到 supervision status=ok"


def test_real_cascade_e2e_live():
    """真 Claude→MCP→run_cascade→CC→失败检测→放行(无mock)。默认跳过。"""
    if os.environ.get("MEYO_LIVE_CAPTAIN") != "1":
        import pytest
        pytest.skip("需 MEYO_LIVE_CAPTAIN=1·默认跳过")
    import subprocess
    r = subprocess.run(["bash", str(_HERE / "run_captain_mcp.sh"),
                        "必须调用 run_cascade 完成:HTTP 418 的标准短语是什么。报告 chosen 和最终答案。",
                        "mcp__meyo-orchestrator__run_cascade", "stream-json"],
                       capture_output=True, text=True, timeout=420, cwd=str(_HERE))
    blob = r.stdout
    assert "mcp__meyo-orchestrator__run_cascade" in blob
    assert "teapot" in blob.lower()


def test_orchestrator_server_live_list():
    """真起 orchestrator MCP server·经桥验证工具集及动态成员目录。"""
    import mcp_bridge as mb
    server = {"command": "python3.12",
              "args": ["meyo_mcp_servers/orchestrator_server.py"],
              "cwd": "__ORCH_ROOT__"}
    with mb._StdioMCP("orchestrator", server) as c:
        tools = c.list_tools()
        assert len(tools) == 15, [t["name"] for t in tools]
        out = c.call_tool("list_harnesses", {})
        harnesses = json.loads(out["content"][0]["text"])
        assert {row["key"] for row in harnesses} == {
            row["key"] for row in __import__("orchestrator").list_harnesses()
        }
        assert all("available" in row for row in harnesses)
