# -*- coding: utf-8 -*-
"""mcp_bridge live 测试 —— 真起 echo MCP server·真走 stdio·不 mock。

也做静态校验:桥内无路由/荐人硬编码(延续 ADR-005 守护)。
"""
import json, os, sys, importlib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_HERE = Path(__file__).resolve().parent
_MH = _HERE / "meyo_harnesses"
sys.path.insert(0, str(_MH))


def _bridge_with_echo(tmp_registry: Path):
    """指向临时 registry(只含 echo server)的 mcp_bridge 实例。"""
    import mcp_bridge
    importlib.reload(mcp_bridge)
    mcp_bridge.SERVERS_FILE = tmp_registry
    return mcp_bridge


def _write_echo_registry(tmp_path: Path) -> Path:
    echo = _HERE / "meyo_mcp_servers" / "echo_server.py"
    assert echo.is_file(), "echo_server.py 缺失"
    reg = tmp_path / "mcp_servers.json"
    reg.write_text(json.dumps({"servers": {"echo": {
        "command": sys.executable, "args": [str(echo)], "enabled": True}}}))
    return reg


def test_list_mcp_tools_live(tmp_path):
    b = _bridge_with_echo(_write_echo_registry(tmp_path))
    tools = b.list_mcp_tools()
    names = [(t.get("server"), t.get("name")) for t in tools]
    assert ("echo", "echo") in names, f"没发现 echo 工具: {tools}"


def test_call_mcp_tool_live(tmp_path):
    b = _bridge_with_echo(_write_echo_registry(tmp_path))
    out = b.call_mcp_tool("echo", "echo", {"text": "hi-meyo"})
    assert out["status"] == "ok", out
    blob = json.dumps(out.get("result", {}), ensure_ascii=False)
    assert "echo:hi-meyo" in blob, out


def test_unknown_server(tmp_path):
    b = _bridge_with_echo(_write_echo_registry(tmp_path))
    out = b.call_mcp_tool("nope", "x", {})
    assert out["status"] == "unknown_server"
    assert "echo" in out["available"]


def test_missing_registry_returns_empty(tmp_path):
    b = _bridge_with_echo(tmp_path / "does_not_exist.json")
    assert b._load_servers() == {}
    # Built-in desktop tools remain available independently of external registry.
    assert all(t.get("server") == "cua-local-desktop" for t in b.list_mcp_tools())


def test_supervision_mcp_live():
    """真调已注册 supervision MCP server,证明 CV 包→MCP→bridge 全链通。"""
    try:
        import supervision  # noqa: F401
    except Exception:
        import pytest
        pytest.skip("supervision 未装·跳过(诚实)")
    import mcp_bridge as b
    importlib.reload(b)  # 恢复默认 SERVERS_FILE(其他测试会重指到 tmp)
    tools = b.list_mcp_tools()
    names = {(t.get("server"), t.get("name")) for t in tools}
    assert ("supervision-cv", "detections_from_xyxy") in names, tools
    out = b.call_mcp_tool("supervision-cv", "detections_from_xyxy", {
        "xyxy": [[1, 2, 20, 30]], "confidence": [0.95], "class_id": [7]})
    assert out["status"] == "ok", out
    # result.content[0].text 是内层 JSON 字符串·解回来核对真实 CV 结果
    inner = json.loads(out["result"]["content"][0]["text"])
    assert inner["count"] == 1 and inner["detections"][0]["class_id"] == 7, inner


def test_runtime_registry_expands_shared_servers():
    import mcp_bridge as b
    importlib.reload(b)
    servers = b._load_servers()
    expected = {"context7", "github", "memory", "filesystem", "playwright", "git", "fetch", "time"}
    assert expected <= set(servers), set(servers)
    blob = json.dumps(servers, ensure_ascii=False)
    assert "__HOME__" not in blob and "__REPO_ROOT__" not in blob and "__LEGION_DIR__" not in blob
    assert servers["github"]["transport"] == "http"
    assert str(_HERE.parent.parent) in servers["filesystem"]["args"]


def test_no_routing_hardcode():
    """静态守护:桥里不得出现路由/荐人/能力评分【代码】(注释/文档里提到不算)。"""
    import ast
    tree = ast.parse((_MH / "mcp_bridge.py").read_text())
    banned_names = {"recommend", "cost_tier", "strong_on", "pick_reviewer", "_is_simple"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name not in banned_names, f"桥含被禁的编排函数: {node.name}"
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assert node.id not in banned_names, f"桥含被禁的编排变量: {node.id}"
