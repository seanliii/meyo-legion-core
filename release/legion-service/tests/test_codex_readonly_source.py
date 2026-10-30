"""Actual adapter payload checks; no model, network, or native subprocess."""
import json
import importlib
import os
from pathlib import Path
import tomllib
from types import SimpleNamespace

import pytest

from test_codex_native_runtime_facts import isolated as native_isolation


@pytest.fixture
def payload_case(native_isolation, tmp_path, monkeypatch):
    registry = importlib.import_module("harness_registry")
    models = importlib.import_module("model_registry")
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    monkeypatch.setattr(
        registry._sh, "which", lambda name: "/fixture/codex" if name == "codex" else None
    )
    calls = []

    def capture(command, *_args, **kwargs):
        calls.append((command, kwargs))
        Path(command[command.index("-o") + 1]).write_text("offline fixture")
        return SimpleNamespace(
            returncode=0, stderr="",
            stdout=json.dumps({"type": "thread.started", "thread_id": "fixture-original-native"}),
        )

    monkeypatch.setattr(registry, "_run_task", capture)
    # Keep the real _legion_prompt: the shared prefix must agree with the actual grants.
    return SimpleNamespace(hr=registry, mr=models, calls=calls, root=tmp_path, monkeypatch=monkeypatch)


@pytest.mark.parametrize("source_writable", [False, True])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
def test_codex_writable_roots_match_source_permissions(payload_case, source_writable, resume, depth):
    case = payload_case
    source = Path(case.hr.__file__).resolve().parents[2]
    real_access = os.access

    def source_access(path, mode, *args, **kwargs):
        if Path(path) == source and mode == (os.W_OK | os.X_OK):
            return source_writable
        return real_access(path, mode, *args, **kwargs)

    case.monkeypatch.setattr(case.hr.os, "access", source_access)
    case.monkeypatch.delenv("MEYO_NATIVE_CAPABILITY_IDS", raising=False)
    key = "readonly-source-fixture-key"
    context = case.hr.HarnessCtx(
        model="fixture-model", appid=key, gateway="https://models.example.invalid/v1",
        workdir=str(case.root / "work"), execution_depth=depth,
        task_memory={"native_checkpoint": {
            "session_id": "fixture-original-native",
            "provider_binding": case.mr.ccr_binding_key(
                "fixture-model", key, "https://models.example.invalid/v1"),
        }} if resume else {},
    )
    result = case.hr._codex_adapter("__ut_readonly_source", "write the task file", ctx=context)
    assert len(case.calls) == 1, result
    command, options = case.calls[0]
    settings = tomllib.loads("\n".join(
        command[index + 1] for index, item in enumerate(command) if item == "-c"
    ))
    roots = settings["sandbox_workspace_write"]["writable_roots"]
    assert (str(source) in roots) is source_writable
    assert context.workdir in roots
    assert str(Path(os.environ["HOME"]) / ".meyo/legion") in roots
    assert command[command.index("-s") + 1] == "workspace-write"
    assert settings["sandbox_workspace_write"]["network_access"] is True
    assert options["cwd"] == context.workdir
    assert "no one restricts which paths you touch" not in options["input"]
    if source_writable:
        assert f"你对整个仓库 {source} 有读写权限" in options["input"]
    else:
        assert f"程序源码目录 {source} 当前不可写" in options["input"]
        assert "全仓库权限" not in options["input"]
    assert ("resume" in command) is resume
    assert key not in json.dumps(command) + options["input"]


@pytest.mark.parametrize("capabilities,depth,source_writable,expect_spec", [
    ("[]", "standard", False, False),
    ('["external_capability:fixture:inspect"]', "quick", False, False),
    ('["external_capability:fixture:inspect"]', "standard", True, True),
])
def test_optional_capabilities_cannot_reintroduce_source_write_access(
    payload_case, capabilities, depth, source_writable, expect_spec,
):
    case = payload_case
    source = Path(case.hr.__file__).resolve().parents[2]
    real_access = os.access
    case.monkeypatch.setattr(
        case.hr.os, "access",
        lambda path, mode, **kw: source_writable
        if Path(path) == source and mode == (os.W_OK | os.X_OK)
        else real_access(path, mode, **kw),
    )
    case.monkeypatch.setenv("MEYO_NATIVE_CAPABILITY_IDS", capabilities)
    context = case.hr.HarnessCtx(
        model="fixture-model", appid="fixture-key", gateway="https://models.example.invalid/v1",
        user_id="owner-a", session_id="source-scope-session",
        workdir=str(case.root / "work"), execution_depth=depth,
        task_facts={"team_id": "source-scope-team", "task_id": "codex"},
        capability_policy={"execute": True, "publish": False},
    )
    result = case.hr._codex_adapter("__ut_source_capabilities", "write the task file", ctx=context)
    assert len(case.calls) == 1, result
    command, _ = case.calls[0]
    settings = tomllib.loads("\n".join(
        command[index + 1] for index, item in enumerate(command) if item == "-c"
    ))
    assert str(source) not in settings["sandbox_workspace_write"]["writable_roots"]
    assert ("meyo_capabilities" in settings.get("mcp_servers", {})) is expect_spec
