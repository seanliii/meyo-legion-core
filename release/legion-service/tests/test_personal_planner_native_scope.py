"""Planner boundary reproduction using the real Codex argument builder.

Only the native process is replaced. No model, network, server, project grant,
or change to the source project's contents is involved. Inspecting declared
roots does not claim an end-to-end operating-system sandbox acceptance.
"""
import importlib
import json
import os
from pathlib import Path
import socket
import sys
import tomllib
from types import SimpleNamespace

import pytest

from test_codex_readonly_source import payload_case
from test_codex_native_runtime_facts import isolated as native_isolation


@pytest.fixture(autouse=True)
def no_external_execution(native_isolation, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network, listeners and external tools are forbidden")

    for method in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, method, forbidden)
    monkeypatch.setenv("LEGION_MANAGED_BROWSER", "1")
    monkeypatch.setenv("MEYO_NATIVE_CAPABILITY_IDS", '["external_capability:fixture:write"]')
    monkeypatch.setenv("MEYO_DEVICE_DOCKER_HOST", "unix:///fixture/docker.sock")
    monkeypatch.delenv("LEGION_CODEX_RESPONSES_BINDING", raising=False)
    for module, method in (
        ("managed_browser", "open_managed_browser"),
        ("native_capability_host", "native_stdio_spec"),
        ("native_device_permissions", "restrict_codex_device_control"),
    ):
        monkeypatch.setattr(importlib.import_module(module), method, forbidden)


def scope(request_id="__ut_planner_separate_request"):
    return {"native_session_scope": "request",
            "native_session_request_id": request_id, "native_read_only": True}


def assert_read_only_payload(command, options):
    assert command.count("-s") == 1
    assert command[command.index("-s") + 1] == "read-only"
    assert "resume" not in command
    assert "--ignore-user-config" in command and "--ignore-rules" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    settings = tomllib.loads("\n".join(
        command[index + 1] for index, value in enumerate(command) if value == "-c"))
    assert "sandbox_workspace_write" not in settings
    assert settings["approval_policy"] == "never"
    assert settings["mcp_servers"] == {}
    for feature in ("apps", "plugins", "hooks", "multi_agent", "multi_agent_v2"):
        assert settings["features"][feature] is False
    assert "permissions" not in settings  # Device adapter must not replace read-only.
    assert "全仓库权限" not in options["input"]
    assert "一律写到这个目录" not in options["input"]
    assert "read-only" in options["input"]


@pytest.mark.parametrize("original_location", [
    "separate_personal_data",
    "legacy_media_root",
    "writable_core_source",
])
def test_auxiliary_request_does_not_grant_original_project(
        payload_case, monkeypatch, original_location):
    case = payload_case
    monkeypatch.setenv("MEYO_LEGION_DIR", str(case.root / "data"))
    monkeypatch.setenv("LEGION_HOME_DIR", str(case.root / "legion-home"))

    sandbox = importlib.import_module("legion_sandbox")
    request_id = "__ut_planner_separate_request"
    # This is exactly the existing default-workdir route requested for planning:
    # an empty ProjectContext, not a made-up owner/workspace or a new grant.
    planner_workdir = sandbox.workdir("codex", request_id, {}, "__ut_owner")
    source = Path(case.hr.__file__).resolve().parents[2]
    locations = {
        "separate_personal_data": case.root / "data/outputs/original",
        "legacy_media_root": Path(os.environ["HOME"]) / ".meyo/legion/outputs/original",
        "writable_core_source": source,
    }
    original = locations[original_location].resolve()
    if original_location != "writable_core_source":
        original.mkdir(parents=True)
    else:
        # Check the real local permission; do not forge os.access or a grant.
        assert os.access(source, os.W_OK | os.X_OK)
    assert not original.is_relative_to(Path(planner_workdir))

    context = case.hr.HarnessCtx(
        model="fixture-model", appid="fixture-not-a-real-key",
        gateway="https://models.example.invalid/v1",
        user_id="__ut_owner", session_id="request___ut_planner_slot",
        workdir=planner_workdir, execution_depth="standard",
        task_facts=scope(request_id),
    )
    assert context.project_context == {}
    assert context.capability_policy == {}
    result = case.hr._codex_adapter(
        request_id, f"只返回规划 JSON；原作品现场仅供参考：{original}", ctx=context)
    assert len(case.calls) == 1, result
    command, options = case.calls[0]
    assert options["cwd"] == planner_workdir
    assert command[command.index("-C") + 1] == planner_workdir
    # Read-only has no writable cwd, TMPDIR or declared extra roots, unlike
    # workspace-write with an empty writable_roots array.
    assert_read_only_payload(command, options)
    assert result["final"] == "offline fixture"  # Host output remains usable.


def test_managed_linux_uses_real_read_only_launch(payload_case, monkeypatch):
    case = payload_case
    app = case.root / "app"
    anchor = app / "legion/orchestrator/harness_registry.py"
    anchor.parent.mkdir(parents=True)
    (app / "legion/legion-service").mkdir()
    anchor.write_text("# Published layout fixture, not copied product code.")
    runtime = case.root / "store/runtimes/frozen"
    executable = runtime / "node-tools/bin/codex"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture executable; never run")
    executable.chmod(0o700)
    monkeypatch.setattr(case.hr, "__file__", str(anchor))
    monkeypatch.setattr(case.hr, "_sys", SimpleNamespace(**{**vars(sys), "platform": "linux"}))
    monkeypatch.setenv("MEYO_RUNTIME_ROOT", str(runtime))
    monkeypatch.setenv("MEYO_RUNTIME_STORE", str(case.root / "store"))
    for name in ("HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        Path(os.environ[name]).mkdir(parents=True, exist_ok=True)
    context = case.hr.HarnessCtx(
        model="fixture-model", appid="fixture-key", gateway="https://models.example.invalid/v1",
        user_id="__ut_owner", session_id="request___ut_planner_slot",
        workdir=str(case.root / "work"), task_facts=scope(),
    )
    result = case.hr._codex_adapter("__ut_planner_managed", "只返回规划 JSON", ctx=context)
    assert len(case.calls) == 1, result
    command, options = case.calls[0]
    assert command[0] == str(executable)
    assert_read_only_payload(command, options)
    assert result["final"] == "offline fixture"
    assert result["native_checkpoint"]["session_id"] == "fixture-original-native"
    assert not Path(command[command.index("-o") + 1]).exists()  # Original launch cleanup.


@pytest.mark.parametrize("facts,memory", [
    ({"native_read_only": "true", "native_session_scope": "request"}, {}),
    ({"native_read_only": 1, "native_session_scope": "request"}, {}),
    ({"native_read_only": True, "native_session_scope": "conversation"}, {}),
    (scope(), {"native_checkpoint": {"session_id": "main-history"}}),
])
def test_invalid_or_history_bearing_read_only_scope_never_runs(payload_case, facts, memory):
    case = payload_case
    result = case.hr._codex_adapter(
        "__ut_bad_planner_scope", "规划",
        ctx=case.hr.HarnessCtx(
            model="fixture-model", appid="fixture-key", gateway="https://models.example.invalid/v1",
            workdir=str(case.root / "work"), task_facts=facts, task_memory=memory),
    )
    assert case.calls == []
    assert result["status"] in ("error", "unavailable")
    assert result["error_envelope"]["code"] == "invalid_read_only_scope"


def test_read_only_scope_survives_large_context_transport(payload_case, monkeypatch):
    core = importlib.import_module("legion_core")
    facts = {**scope(), "large_fact": "x" * 40000}
    values = core._runtime_context_env(
        "", facts, {}, {}, {}, [], key="codex", qhash="__ut_planner_projection")
    assert json.loads(values["MEYO_TASK_FACTS_JSON"])["native_read_only"] is True
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    context = payload_case.hr.HarnessCtx.from_env()
    assert context.task_facts["native_read_only"] is True
