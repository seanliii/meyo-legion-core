"""Explicit native-to-host capability wiring; offline and isolated."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest

from test_explicit_responses_provider import isolated

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.syspath_prepend(str(ORCH))
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    spec = importlib.util.find_spec("native_capability_host")
    assert spec is not None, "native shell has no granted host capability transport"
    module = importlib.import_module("native_capability_host")
    work = tmp_path / "work"
    work.mkdir()
    context = {
        "user_id": "alice", "session_id": "same-session", "workdir": str(work),
        "task_facts": {"team_id": "same-team", "task_id": "codex"},
        "project_context": {"workspace_root": str(work)},
        "capability_policy": {"execute": True, "publish": False},
        "allowed_ids": ["external_capability:fixture:inspect"],
    }
    import external_agent_registry as external
    monkeypatch.setattr(external, "load_catalog", lambda: [{
        "key": "fixture", "capability_actions": {
            "inspect": {"command": ["python3.12", "--version"],
                        "params": ["request_file"], "description": "Read existing work"},
            "other": {"command": ["python3.12", "--version"], "params": []},
        },
    }])
    return SimpleNamespace(module=module, context=context, work=work)


def test_host_only_exposes_explicitly_granted_declarations(host):
    session = host.module.CapabilitySession(host.context)
    listed = session.list_capabilities({})
    assert [row["id"] for row in listed["actions"]] == host.context["allowed_ids"]
    assert "run_harness" not in repr(session.tools)
    assert "judge_answer" not in repr(session.tools)
    with pytest.raises(ValueError):
        session.call_capability({"tool_id": "external_capability:fixture:other", "params": {}})


def test_host_uses_existing_runner_with_parent_identity_and_policy(host, monkeypatch):
    import legion_tool
    import agent_tool_provider
    seen = {}
    request = host.work / "request.json"
    request.write_text("{}")

    def run(tool_id, params, **kwargs):
        _, bound, _ = agent_tool_provider._runtime_binding()
        seen.update(tool_id=tool_id, params=params, bound=dict(bound))
        return {"ok": True, "native": {"status": "observed"}}

    monkeypatch.setattr(legion_tool, "run_tool", run)
    result = host.module.CapabilitySession(host.context).call_capability({
        "tool_id": host.context["allowed_ids"][0], "params": {"request_file": str(request)},
    })
    assert result["ok"]
    assert seen["bound"]["MEYO_USER_ID"] == "alice"
    assert seen["bound"]["MEYO_SESSION_ID"] == "same-session"
    assert seen["bound"]["MEYO_WORKDIR"] == str(host.work)
    assert json.loads(seen["bound"]["MEYO_TASK_FACTS_JSON"]) == host.context["task_facts"]
    assert seen["params"]["_capability_policy"]["publish"] is False


@pytest.mark.parametrize("extra", [
    {"env": {"HOME": "/other"}}, {"uid": "mallory"},
    {"confirm": True}, {"_capability_policy": {"publish": True}},
])
def test_model_cannot_override_parent_fields(host, extra):
    request = host.work / "request.json"
    request.write_text("{}")
    with pytest.raises(ValueError):
        host.module.CapabilitySession(host.context).call_capability({
            "tool_id": host.context["allowed_ids"][0],
            "params": {"request_file": str(request), **extra},
        })


@pytest.mark.parametrize("kind", ["outside", "symlink", "directory"])
def test_request_file_stays_in_owned_workspace(host, kind):
    outside = host.work.parent / "outside.json"
    outside.write_text("{}")
    request = outside
    if kind == "symlink":
        request = host.work / "linked.json"
        request.symlink_to(outside)
    elif kind == "directory":
        request = host.work
    with pytest.raises((ValueError, OSError)):
        host.module.CapabilitySession(host.context).call_capability({
            "tool_id": host.context["allowed_ids"][0], "params": {"request_file": str(request)},
        })


def test_parent_policy_denies_without_executing(host, monkeypatch):
    import legion_tool
    monkeypatch.setattr(legion_tool, "_run_tool_inner",
                        lambda *a, **k: pytest.fail("denied host tool executed"))
    request = host.work / "request.json"
    request.write_text("{}")
    context = {**host.context, "capability_policy": {"execute": False}}
    result = host.module.CapabilitySession(context).call_capability({
        "tool_id": context["allowed_ids"][0], "params": {"request_file": str(request)},
    })
    assert result["ok"] is False and result["authorization"]["decision"] == "deny"


def test_external_child_receives_bound_context(host, monkeypatch):
    import legion_tool
    import agent_tool_provider
    import external_agent_registry
    seen = {}

    def run(key, action, params, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(external_agent_registry, "run_capability", run)
    with agent_tool_provider._runtime_scope({
        "MEYO_USER_ID": "alice", "MEYO_SESSION_ID": "same-session",
        "MEYO_WORKDIR": str(host.work),
    }, None, inherit_process_context=False):
        assert legion_tool.run_tool(host.context["allowed_ids"][0], {})["ok"]
    assert seen.get("env", {}).get("MEYO_USER_ID") == "alice"
    assert seen.get("workdir") == str(host.work)


@pytest.mark.parametrize("resume", [False, True])
def test_actual_codex_exec_and_resume_mount_host_without_personal_token(isolated, resume):
    isolated.monkeypatch.setenv(
        "MEYO_NATIVE_CAPABILITY_IDS", '["external_capability:fixture:inspect"]')
    isolated.monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", "/private/do-not-pass.json")
    work = isolated.root / "work"
    work.mkdir()
    ctx = isolated.hr.HarnessCtx(
        model="fixture-model", appid="provider-secret", gateway="https://models.example.invalid/v1",
        user_id="alice", session_id="same-session", workdir=str(work), execution_depth="standard",
        task_facts={"team_id": "same-team", "task_id": "codex"},
        capability_policy={"execute": True, "publish": False},
        task_memory={"native_checkpoint": {
            "session_id": "fixture-original-native",
            "provider_binding": isolated.mr.ccr_binding_key(
                "fixture-model", "provider-secret", "https://models.example.invalid/v1"),
        }} if resume else {},
    )
    result = isolated.hr._codex_adapter("__ut_native_host", "original user task", ctx=ctx)
    assert len(isolated.calls) == 1, result
    command, kwargs = isolated.calls[0]
    config = tomllib.loads("\n".join(
        command[i + 1] for i, value in enumerate(command) if value == "-c"))
    assert "meyo_capabilities" in config.get("mcp_servers", {}), (
        "capability commands still run only in restricted shell", config)
    spec = config["mcp_servers"]["meyo_capabilities"]
    assert "--shared-capabilities" in spec["args"]
    assert "--ignore-user-config" in command and command[command.index("-s") + 1] == "workspace-write"
    assert "provider-secret" not in repr(spec)
    assert "LEGION_PERSONAL_TOKENS_FILE" not in repr(spec)
    bound = json.loads(spec["env"]["MEYO_NATIVE_HOST_CONTEXT"])
    assert bound["user_id"] == "alice" and bound["workdir"] == str(work)
    assert bound["allowed_ids"] == ["external_capability:fixture:inspect"]
    assert ("resume" in command) is resume


@pytest.fixture
def local_adapter(isolated):
    """Use the real adapter/host schema, but never execute a native CLI or wrapper."""
    monkeypatch = isolated.monkeypatch
    for name in (
        "MEYO_NATIVE_CAPABILITY_IDS", "MEYO_NATIVE_HOST_CONTEXT",
        "LEGION_CODEX_RESPONSES_BINDING", "LEGION_MANAGED_BROWSER",
        "MEYO_DEVICE_DOCKER_HOST", "MEYO_AGENTBAY_CONFIG",
        "LEGION_EXTERNAL_SOURCE_ROOT", "LEGION_EXTERNAL_RUNTIME_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(isolated.hr._INPROC_RUNTIME_ENV, "values", {}, raising=False)
    for name in ("CODEX_HOME", "TMPDIR"):
        directory = isolated.root / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    work, trusted = isolated.root / "local-work", isolated.root / "trusted-local-model"
    work.mkdir()
    trusted.mkdir()
    for name in ("home", "state", "registry"):
        (trusted / name).mkdir()
    for name in ("coding.py", "shape.py"):
        (trusted / name).write_text(
            "raise AssertionError('adapter configuration test must not execute a wrapper')\n")
    config = {
        "version": 1, "python": str(Path(sys.executable).resolve()),
        "coding_script": str(trusted / "coding.py"), "shape_script": str(trusted / "shape.py"),
        "home": str(trusted / "home"), "state_dir": str(trusted / "state"),
        "registry_dir": str(trusted / "registry"),
        "timeout_s": 150, "wait_s": 0, "term_grace_s": 2,
    }
    monkeypatch.setenv("MEYO_LOCAL_MODEL_CONFIG", json.dumps(config))
    secrets = {
        "OPENAI_API_KEY": "synthetic-unrelated-model-key",
        "LEGION_PERSONAL_TOKENS_FILE": str(trusted / "must-not-read-personal-tokens.json"),
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    module = importlib.import_module("native_capability_host")
    import external_agent_registry
    monkeypatch.setattr(external_agent_registry, "capability_actions",
                        lambda: {"actions": [], "count": 0})
    return SimpleNamespace(
        **vars(isolated), module=module, work=work, trusted=trusted, config=config,
        secrets=secrets, grants=[
            "local_mcp:meyo-local-model:preflight",
            "local_mcp:meyo-local-model:coding_text",
            "local_mcp:meyo-local-model:shape_3d",
        ],
    )


def _local_adapter_context(case, resume):
    return case.hr.HarnessCtx(
        model="fixture-model", appid="synthetic-provider-key",
        gateway="https://models.example.invalid/v1",
        user_id="alice", session_id="same-session", workdir=str(case.work),
        execution_depth="standard",
        task_facts={"team_id": "same-team", "task_id": "codex"},
        project_context={"workspace_root": str(case.work), "project_id": "same-project"},
        capability_policy={"execute": True, "read": True, "write": True, "publish": False},
        task_memory={"native_checkpoint": {
            "session_id": "fixture-original-native",
            "provider_binding": case.mr.ccr_binding_key(
                "fixture-model", "synthetic-provider-key", "https://models.example.invalid/v1"),
        }} if resume else {},
    )


def _adapter_config(command):
    return tomllib.loads("\n".join(
        command[index + 1] for index, value in enumerate(command) if value == "-c"))


@pytest.mark.parametrize("resume", [False, True], ids=["exec", "resume"])
def test_actual_codex_local_model_config_reaches_host_with_identity(local_adapter, resume):
    case = local_adapter
    case.monkeypatch.setenv("MEYO_NATIVE_CAPABILITY_IDS", json.dumps(case.grants))
    ctx = _local_adapter_context(case, resume)
    result = case.hr._codex_adapter("__ut_local_model_host", "synthetic original task", ctx=ctx)
    assert result["status"] == "ok" and len(case.calls) == 1, result
    command, kwargs = case.calls[0]
    config = _adapter_config(command)
    spec = config["mcp_servers"]["meyo_capabilities"]
    assert command[:2] == ["/fixture/codex", "exec"]
    assert "--ignore-user-config" in command
    assert command[command.index("-s") + 1] == "workspace-write"
    assert command[command.index("-C") + 1] == kwargs["cwd"] == str(case.work)
    assert command[command.index("-m") + 1] == result["model_used"] == ctx.model
    assert command[-1] == "-" and kwargs["input"].startswith("synthetic original task")
    assert ("resume" in command) is resume
    if resume:
        assert command[-3:] == ["resume", "fixture-original-native", "-"]
    checkpoint = result["native_checkpoint"]
    assert checkpoint["kind"] == "codex_thread"
    assert checkpoint["session_id"] == "fixture-original-native"
    assert checkpoint["provider_binding"] == case.mr.ccr_binding_key(
        ctx.model, ctx.appid, ctx.gateway)
    assert result["resumed_native"] is resume
    assert config["model_provider"] == result["responses_transport"] == "meyo_responses"
    provider = config["model_providers"]["meyo_responses"]
    assert provider["base_url"] == ctx.gateway and provider["wire_api"] == "responses"
    assert kwargs["env"][provider["env_key"]] == ctx.appid
    assert spec["args"] == [
        "-I", str(ORCH / "meyo_mcp_servers/orchestrator_server.py"), "--shared-capabilities"]
    assert spec["env"]["MEYO_LOCAL_MODEL_CONFIG"] == json.dumps(case.config)
    assert spec["tool_timeout_sec"] == 157  # Synthetic wrapper budget + grace + transport margin.
    bound = json.loads(spec["env"]["MEYO_NATIVE_HOST_CONTEXT"])
    assert bound == {
        "user_id": ctx.user_id, "session_id": ctx.session_id, "workdir": ctx.workdir,
        "task_facts": ctx.task_facts, "project_context": ctx.project_context,
        "capability_policy": ctx.capability_policy, "allowed_ids": case.grants,
    }
    writable = [Path(path).resolve() for path in
                config["sandbox_workspace_write"]["writable_roots"]]
    assert case.work in writable
    assert all(not path.is_relative_to(root) for root in writable
               for path in (SERVICE, ORCH, case.trusted))
    forbidden_keys = {
        "AIGC_APPID", "MEYO_UNIFIED_APPID", "MEYO_UNIFIED_GATEWAY",
        "LEGION_CODEX_RESPONSES_BINDING", *case.secrets,
    }
    assert forbidden_keys.isdisjoint(spec["env"])
    assert forbidden_keys.isdisjoint(spec.get("env_vars", []))
    for secret in (ctx.appid, *case.secrets.values()):
        assert secret not in repr(spec)

    # Exercise the real host scrubber with synthetic inherited CLI environment.
    # Replacing the mapping avoids clearing the actual pytest process environment.
    inherited = {**kwargs["env"], **spec["env"]}
    with case.monkeypatch.context() as patch:
        patch.setattr(case.module.os, "environ", inherited)
        session = case.module.from_environment()
        assert session.local_config == case.config
        assert {row["id"] for row in session.list_capabilities({})["actions"]} == set(case.grants)
        assert forbidden_keys.isdisjoint(session.environment)
        assert forbidden_keys.isdisjoint(case.module.os.environ)


@pytest.mark.parametrize("resume", [False, True], ids=["exec", "resume"])
@pytest.mark.parametrize("grants", [None, [], ["external_capability:fixture:inspect"]],
                         ids=["absent", "explicit-empty", "external-only"])
def test_actual_codex_local_model_config_is_not_a_grant(local_adapter, resume, grants):
    case = local_adapter
    if grants is not None:
        case.monkeypatch.setenv("MEYO_NATIVE_CAPABILITY_IDS", json.dumps(grants))
    ctx = _local_adapter_context(case, resume)
    # Task data cannot promote itself to an operator grant, even with a valid
    # process-level local configuration already present.
    ctx.task_facts.update(
        MEYO_NATIVE_CAPABILITY_IDS=json.dumps(case.grants),
        MEYO_LOCAL_MODEL_CONFIG=json.dumps(case.config))
    result = case.hr._codex_adapter("__ut_ungranted_local_model", "synthetic task", ctx=ctx)
    assert result["status"] == "ok" and len(case.calls) == 1, result
    command, _ = case.calls[0]
    spec = _adapter_config(command).get("mcp_servers", {}).get("meyo_capabilities")
    if not grants:
        assert spec is None
        return
    assert "MEYO_LOCAL_MODEL_CONFIG" not in spec["env"]
    bound = json.loads(spec["env"]["MEYO_NATIVE_HOST_CONTEXT"])
    assert bound["allowed_ids"] == grants
    session = case.module.CapabilitySession(bound, environment=spec["env"])
    assert session.local_config is None
    with pytest.raises(ValueError):
        session.call_capability({"tool_id": case.grants[0], "params": {"kind": "coding"}})
    assert len(case.calls) == 1


@pytest.mark.parametrize("resume", [False, True], ids=["exec", "resume"])
def test_actual_codex_local_model_host_does_not_borrow_process_config(local_adapter, resume):
    case = local_adapter
    case.monkeypatch.setenv("MEYO_NATIVE_CAPABILITY_IDS", json.dumps(case.grants))
    result = case.hr._codex_adapter(
        "__ut_local_model_no_borrow", "synthetic task", ctx=_local_adapter_context(case, resume))
    assert result["status"] == "ok" and len(case.calls) == 1, result
    spec = _adapter_config(case.calls[0][0])["mcp_servers"]["meyo_capabilities"]
    environment = dict(spec["env"])
    environment.pop("MEYO_LOCAL_MODEL_CONFIG")
    assert json.loads(os.environ["MEYO_LOCAL_MODEL_CONFIG"]) == case.config
    with pytest.raises(ValueError):
        case.module.CapabilitySession(
            json.loads(environment["MEYO_NATIVE_HOST_CONTEXT"]), environment=environment)
    assert len(case.calls) == 1


def test_quick_keeps_existing_lightweight_payload(isolated):
    isolated.monkeypatch.setenv("MEYO_NATIVE_CAPABILITY_IDS", '["external_capability:fixture:inspect"]')
    ctx = isolated.hr.HarnessCtx(
        model="fixture-model", appid="provider-secret", gateway="https://models.example.invalid/v1",
        user_id="alice", session_id="same-session",
        workdir=str(isolated.root / "work"), execution_depth="quick",
    )
    isolated.hr._codex_adapter("__ut_quick_host", "short task", ctx=ctx)
    command, kwargs = isolated.calls[0]
    assert not any("meyo_capabilities" in str(x) for x in command)


def test_existing_mcp_stdio_shell_serves_only_granted_capabilities(host, monkeypatch):
    # Real child process and JSON-RPC; no model and no executed external action.
    context = {**host.context, "allowed_ids": ["external_capability:ohmygame:status"]}
    env = {key: value for key, value in os.environ.items() if key in host.module._ENV_KEYS}
    env.update(MEYO_NATIVE_HOST_CONTEXT=json.dumps(context),
               OPENAI_API_KEY="must-not-appear-in-tool-server",
               LEGION_PERSONAL_TOKENS_FILE="/must-not-be-read")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_capabilities", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "run_harness", "arguments": {"harness": "other"}}},
    ]
    run = subprocess.run(
        [sys.executable, "-I", str(ORCH / "meyo_mcp_servers/orchestrator_server.py"),
         "--shared-capabilities"],
        env=env, input="\n".join(json.dumps(row) for row in messages) + "\n",
        text=True, capture_output=True, timeout=15)
    assert run.returncode == 0, run.stderr
    rows = [json.loads(row) for row in run.stdout.splitlines()]
    # Shared tools run asynchronously; JSON-RPC replies are matched by id, not arrival order.
    replies = {row["id"]: row for row in rows}
    assert len(rows) == len(replies) == 4 and set(replies) == {1, 2, 3, 4}
    assert replies[1]["result"]["serverInfo"]["name"] == "meyo-capabilities"
    assert {t["name"] for t in replies[2]["result"]["tools"]} == {
        "list_capabilities", "call_capability"}
    listed = json.loads(replies[3]["result"]["content"][0]["text"])
    assert [row["id"] for row in listed["actions"]] == context["allowed_ids"]
    assert replies[4]["result"]["isError"] is True
    assert "must-not" not in run.stdout
    assert not list(Path(env["LEGION_PROC_REGISTRY_DIR"]).glob("*.ns-*.json"))


def test_personal_configuration_freezes_explicit_capability_ids(host, monkeypatch, tmp_path):
    import personal_runtime
    root = tmp_path / "personal"
    personal_runtime.initialize(root, "alice", 18080)
    method = getattr(personal_runtime, "configure_capabilities", None)
    assert callable(method), "no explicit persistent parent grant for native capabilities"
    result = method(root, ["external_capability:fixture:inspect"])
    assert result["native_capabilities"] == host.context["allowed_ids"]
    env, _ = personal_runtime.environment(root, {
        "MEYO_NATIVE_CAPABILITY_IDS": '["external_capability:fixture:other"]',
    })
    assert json.loads(env["MEYO_NATIVE_CAPABILITY_IDS"]) == host.context["allowed_ids"]
    with pytest.raises(ValueError):
        method(root, ["external_capability:fixture:unknown"])


def test_isolated_external_execution_does_not_refill_credentials(host, monkeypatch):
    import external_agent_registry as external
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-model-key")
    monkeypatch.setenv("MEYO_UNIFIED_APPID", "unrelated-company-key")
    captured = {}
    monkeypatch.setattr(external, "_probe_one",
                        lambda item, env, **kw: {"available": True})
    monkeypatch.setattr(external, "_strip_dead_proxy", lambda env: env)
    monkeypatch.setattr(external, "_format_argv", lambda *a, **kw: [sys.executable, "--version"])
    monkeypatch.setattr(external, "_safe_step", lambda command: True)

    def execute(command, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(external.subprocess, "run", execute)
    assert external.run_capability(
        "fixture", "inspect", {"request_file": str(host.work / "request.json")},
        env={"PATH": "/usr/bin:/bin", "MEYO_USER_ID": "alice"},
        workdir=str(host.work), isolated_environment=True)["ok"]
    assert captured["env"] == {"PATH": "/usr/bin:/bin", "MEYO_USER_ID": "alice"}
    assert captured["cwd"] == str(host.work)
    item = {"credentials": {"any_of": ["MEYO_UNIFIED_APPID"]}}
    assert external._credentials(item, {}, inherit_environment=False)["ready"] is False
    assert external._credentials(item, {})["ready"] is True  # old API unchanged


def test_request_payload_cannot_export_outside_workspace(host):
    request = host.work / "request.json"
    request.write_text(json.dumps({"output_file": str(host.work.parent / "outside.json")}))
    with pytest.raises(ValueError):
        host.module.CapabilitySession(host.context).call_capability({
            "tool_id": host.context["allowed_ids"][0], "params": {"request_file": str(request)},
        })


def test_parent_rejects_workspace_overlapping_host_implementation(host):
    ctx = SimpleNamespace(
        user_id="alice", session_id="same-session", workdir=str(SERVICE),
        execution_depth="standard", task_facts={}, project_context={}, capability_policy={},
    )
    with pytest.raises(ValueError, match="host"):
        host.module.native_stdio_spec(
            ctx, {"MEYO_NATIVE_CAPABILITY_IDS": json.dumps(host.context["allowed_ids"])},
            ORCH / "meyo_mcp_servers/orchestrator_server.py")


def test_advertised_tool_has_explicit_serializable_business_parameters(host, monkeypatch):
    import legion_tool
    request = host.work / "request.json"
    request.write_text("{}")
    session = host.module.CapabilitySession(host.context)
    _name, _description, schema, execute = session.tools[1]
    assert schema["properties"].get("params_json", {}).get("type") == "string", (
        "an unshaped params object does not tell the native model how to send request_file")
    assert "params_json" in schema["required"]
    monkeypatch.setattr(legion_tool, "run_tool", lambda tool, params: {
        "ok": True, "request_snapshot": Path(params["request_file"]).read_text()})
    result = execute({
        "tool_id": host.context["allowed_ids"][0],
        "params_json": json.dumps({"request_file": str(request)}),
    })
    assert result["ok"] and result["request_snapshot"] == "{}"
