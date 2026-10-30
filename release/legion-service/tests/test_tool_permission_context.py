"""Use the real authorization and worker binding; no network or model calls."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def tool_context(tmp_path, monkeypatch):
    service = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(service))
    monkeypatch.syspath_prepend(str(service.parents[1] / "harness_baselines/meyo-orchestrator"))
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    provider = importlib.import_module("agent_tool_provider")
    for name in provider._ENV_FIELDS:
        if name not in ("MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
            monkeypatch.delenv(name, raising=False)
    registry, _, _ = provider._runtime_binding()
    monkeypatch.setattr(registry._INPROC_RUNTIME_ENV, "values", {}, raising=False)
    monkeypatch.setattr(registry._INPROC_RUNTIME_ENV, "inherit_process_context", True, raising=False)
    monkeypatch.setattr(registry._INPROC_CANCEL_CHECK, "check", None, raising=False)
    monkeypatch.setattr(registry._INPROC_CANCEL_CHECK, "cancelled", False, raising=False)
    tool = importlib.import_module("legion_tool")
    calls = []

    def leaf(tool_id, params):
        _, env, check = provider._runtime_binding()
        calls.append({"tool_id": tool_id, "params": dict(params),
                      "env": dict(env), "cancel_check": check})
        return {"ok": True, "text": "offline boundary reached"}

    monkeypatch.setattr(tool, "_run_tool_inner", leaf)
    monkeypatch.setattr(tool, "_tool_source_ref", lambda tool_id: {"name": tool_id})
    monkeypatch.setattr(tool, "_record_tool_usage", lambda *args: {})
    return SimpleNamespace(tool=tool, provider=provider, registry=registry, calls=calls)


@pytest.mark.parametrize("tool_id", [
    "runnable:research_search", "runnable:research_fetch", "runnable:search_source_probe",
    "auto_tool:fetch_url_smart", "auto_tool:fetch_outlinks",
])
def test_existing_network_entries_require_network_permission(tool_context, tool_id):
    result = tool_context.tool.run_tool(tool_id, {
        "_capability_policy": {"read": True, "network": False},
    })
    assert result["ok"] is False
    assert result["authorization"]["action"] == "network_read"
    assert result["error"] == "CAPABILITY_DENIED:network_read"
    assert tool_context.calls == []


@pytest.mark.parametrize("params", [
    {}, {"_capability_policy": {}}, {"_capability_policy": {"network": True}},
])
@pytest.mark.parametrize("tool_id", ["runnable:web_fetch", "auto_tool:search_github"])
def test_parent_policy_applies_before_worker_and_lifecycle(
        tool_context, monkeypatch, params, tool_id):
    monkeypatch.setenv("MEYO_CAPABILITY_POLICY_JSON", '{"read":true,"network":false}')
    events = []
    result = tool_context.tool.run_tool(tool_id, params, event_cb=events.append)
    assert result["ok"] is False
    assert result["error"] == "CAPABILITY_DENIED:network_read"
    assert tool_context.calls == []
    assert events == [], "A denied request must not announce that a tool started."


def test_child_can_narrow_parent(tool_context, monkeypatch):
    monkeypatch.setenv("MEYO_CAPABILITY_POLICY_JSON", '{"network":true}')
    result = tool_context.tool.run_tool(
        "runnable:web_fetch", {"_capability_policy": {"network": False}})
    assert result["error"] == "CAPABILITY_DENIED:network_read"
    assert tool_context.calls == []


def test_trusted_scope_wins_over_process_and_child(tool_context, monkeypatch):
    monkeypatch.setenv("MEYO_CAPABILITY_POLICY_JSON", '{"network":true}')
    scope = {"MEYO_USER_ID": "__ut_owner", "MEYO_SESSION_ID": "__ut_session",
             "MEYO_CAPABILITY_POLICY_JSON": '{"network":false}'}
    with tool_context.provider._runtime_scope(scope, None, inherit_process_context=False):
        result = tool_context.tool.run_tool(
            "runnable:web_fetch", {"_capability_policy": {"network": True}})
        assert tool_context.provider._runtime_binding()[1] == scope
    assert result["error"] == "CAPABILITY_DENIED:network_read"
    assert tool_context.calls == []


@pytest.mark.parametrize("parent", [
    '{"network":', "[]", '{"network":"false"}', '{"confirm_required":["release",2]}',
])
def test_invalid_parent_policy_fails_before_execution(tool_context, monkeypatch, parent):
    monkeypatch.setenv("MEYO_CAPABILITY_POLICY_JSON", parent)
    result = tool_context.tool.run_tool("runnable:web_fetch", {})
    assert result["ok"] is False
    assert result["error"].startswith("tool policy unavailable:")
    assert parent not in result["error"], "Do not echo raw policy or environment data."
    assert tool_context.calls == []


def test_discovery_remains_read_only(tool_context, monkeypatch):
    monkeypatch.setenv("MEYO_CAPABILITY_POLICY_JSON", '{"read":true,"network":false}')
    result = tool_context.tool.run_tool("runnable:research_capabilities", {})
    assert result["ok"] is True
    assert result["authorization"]["action"] == "local_fs"
    policy = json.loads(tool_context.calls[0]["env"]["MEYO_CAPABILITY_POLICY_JSON"])
    assert policy["network"] is False


def test_explicit_empty_scope_does_not_borrow_process_policy(tool_context, monkeypatch):
    monkeypatch.setenv("MEYO_USER_ID", "must-not-borrow")
    monkeypatch.setenv("MEYO_CAPABILITY_POLICY_JSON", '{"network":false}')
    with tool_context.provider._runtime_scope({}, None, inherit_process_context=False):
        result = tool_context.tool.run_tool("runnable:web_fetch", {})
        assert tool_context.provider._runtime_binding()[1] == {}
    assert result["ok"] is True
    assert "MEYO_USER_ID" not in tool_context.calls[0]["env"]


def test_authorization_and_execution_share_the_same_context_snapshot(tool_context, monkeypatch):
    scope = {"MEYO_USER_ID": "__ut_original_owner",
             "MEYO_CAPABILITY_POLICY_JSON": '{"network":true,"execute":false}'}
    cancelled = lambda: False

    def on_event(event):
        if event["t"] == "tool_started":
            tool_context.registry.set_inproc_runtime_env({
                "MEYO_USER_ID": "__ut_changed_after_authorization",
                "MEYO_CAPABILITY_POLICY_JSON": '{"network":false,"execute":true}',
            })

    with tool_context.provider._runtime_scope(
            scope, cancelled, inherit_process_context=False):
        result = tool_context.tool.run_tool("runnable:web_fetch", {}, event_cb=on_event)
    assert result["ok"] is True
    captured = tool_context.calls[0]
    assert captured["env"]["MEYO_USER_ID"] == "__ut_original_owner"
    policy = json.loads(captured["env"]["MEYO_CAPABILITY_POLICY_JSON"])
    assert policy["network"] is True and policy["execute"] is False
    assert captured["cancel_check"] is cancelled
    assert tool_context.registry._INPROC_RUNTIME_ENV.values == {}
