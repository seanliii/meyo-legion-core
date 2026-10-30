# -*- coding: utf-8 -*-
"""架构图真实性回归 —— AUDIT_2026-08-26.md 第6节每个节点必须真实存在且连通。

任何一层被删/改名/断线,这里立即红,防止"图上有、代码里没有"。
"""
import importlib.util as _u
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "meyo_harnesses"))


def test_ui_layer():
    import cloud_ui.api as api
    assert set(api._EXECUTORS) == {"cascade", "autoloop", "captain", "omnigent"}
    paths = {getattr(r, "path", "") for r in api.app.routes}
    assert "/templates/{key}/instantiate" in paths
    assert "/runs/{run_id}/cancel" in paths and "/runs/{run_id}/events" in paths


def test_brain_layer_14_tools():
    import autoloop as A
    impl = set(A.TOOLS_IMPL)
    schema = {t["function"]["name"] for t in A.TOOLS_SCHEMA}
    assert impl == schema and len(impl) == 15


def test_pool_13_harnesses():
    import orchestrator as O
    assert len(O._load_harnesses()) == 13


def test_mcp_layer():
    import mcp_bridge as MB
    assert callable(MB.list_mcp_tools) and callable(MB.call_mcp_tool)
    spec = _u.spec_from_file_location(
        "orch_srv", _HERE / "meyo_mcp_servers" / "orchestrator_server.py")
    m = _u.module_from_spec(spec); spec.loader.exec_module(m)
    assert len(m._SCHEMA) == 15


def test_shared_layer():
    import health as H, skills as SK, templates as ST
    assert callable(H.health_snapshot) and callable(H.circuit_open)
    assert len(SK.list_skills("pm-skills")) >= 2
    assert len(ST.list_templates()) == 10


def test_cascade_layer_wired():
    """置信级联(唯一超单家架构)已作为生产工具接入,策略文件在位。"""
    import cascade as CX
    assert callable(CX.run_cascade) and callable(CX.predict_failure)
    pol = CX._load_policy()
    assert pol["primary"] == "cc" and pol["fallback"] == "deepagents"
    import autoloop as A
    assert "run_cascade" in A.TOOLS_IMPL


def test_backends_and_bundles():
    from backends import BACKEND_HARNESSES
    assert set(BACKEND_HARNESSES) == {"cua", "supervision", "herdr", "orca"}
    assert (_HERE / "agents/captain/config.yaml").is_file()
    assert (_HERE / "agents/captain-sdk/config.yaml").is_file()
