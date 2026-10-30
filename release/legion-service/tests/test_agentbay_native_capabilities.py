"""Actual native host/MCP functions against the phone API contract, entirely offline.

The provider implementation belongs to the main task. Only that dependency is
replaced here; native declarations, parent binding, RPC, image validation and
filesystem readback are the production functions.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace

import pytest

from test_native_device_capabilities import _png
from test_personal_codex_model_catalog import adapter


SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
SERVER = ORCH / "meyo_mcp_servers/orchestrator_server.py"
PREFIX = "local_mcp:meyo-mobile:"
NAMES = ("mobile_status", "mobile_start", "mobile_screenshot", "mobile_action", "mobile_stop")
IDS = [PREFIX + name for name in NAMES]


@pytest.fixture(autouse=True)
def phone(tmp_path, monkeypatch):
    # from_environment deliberately clears its process environment. Keep that
    # operation test-local too, including on startup/authorization failures.
    monkeypatch.setattr(os, "environ", {
        key: value for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "PYTHONDONTWRITEBYTECODE"}
    })
    monkeypatch.syspath_prepend(str(ORCH))
    monkeypatch.syspath_prepend(str(SERVICE))
    instance = tmp_path.resolve() / "instance"
    instance.mkdir(mode=0o700)
    for key, leaf in (
        ("HOME", "home"), ("TMPDIR", "tmp"), ("MEYO_LEGION_DIR", "data"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_SESSIONS_DIR", "sessions"),
        ("LEGION_TASK_MEMORY_DIR", "task-memory"), ("LEGION_TEAM_DIR", "teams"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_PROC_REGISTRY_DIR", "registry"),
        ("LEGION_EXTERNAL_SOURCE_ROOT", "sources"), ("LEGION_EXTERNAL_RUNTIME_ROOT", "runtimes"),
    ):
        directory = instance / leaf
        directory.mkdir(mode=0o700)
        monkeypatch.setenv(key, str(directory))
    monkeypatch.setattr(tempfile, "tempdir", str(instance / "tmp"))
    monkeypatch.setenv("LEGION_EXT_AUTO_UPDATE", "0")
    for key in tuple(os.environ):
        if key.startswith(("MEYO_DEVICE_", "MEYO_DESKTOP_", "MEYO_PHONE_", "MEYO_AGENTBAY_",
                           "AGENTBAY_", "MEYO_NATIVE_")):
            monkeypatch.delenv(key)

    def forbidden(*args, **kwargs):
        raise AssertionError("no real process, network, Docker, phone or model in these tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    private = instance / "secrets/agentbay"
    private.mkdir(mode=0o700, parents=True)
    config = private / "config.json"
    # A synthetic metadata-only file. No credential is created or read.
    config.write_text(json.dumps({
        "schema_version": 1, "owner": "alice",
        "api_key_file": str(private / "api-key.txt"), "control_dir": str(private / "control"),
        "session_seconds": 300, "total_seconds": 1200,
    }))
    config.chmod(0o600)
    monkeypatch.setenv("MEYO_AGENTBAY_CONFIG", str(config))
    mobile = instance / "data/users/alice/mobile"
    mobile.mkdir(mode=0o700, parents=True)
    work = instance / "data/outputs/task"
    work.mkdir(mode=0o700, parents=True)
    ctx = {
        "user_id": "alice", "session_id": "original-native-session", "workdir": str(work),
        "task_facts": {"team_id": "same-team", "task_id": "phone"},
        "project_context": {"workspace_root": str(work)},
        "capability_policy": {"execute": True, "publish": False}, "allowed_ids": IDS,
    }
    case = SimpleNamespace(
        root=instance, config=config, mobile=mobile, work=work, context=ctx,
        calls=[], auth_calls=[], configured_calls=[], auth_error=None, callback_error=None,
        reply={"ok": True, "provider": "agentbay", "ready": False, "phase": "stopped",
               "remaining_seconds": 0, "remaining_budget_seconds": 1200},
    )
    provider = ModuleType("mobile_device")

    def configured(*, environment=None):
        source = os.environ if environment is None else environment
        case.configured_calls.append(dict(source))
        return bool(source.get("MEYO_AGENTBAY_CONFIG"))

    def authorize(uid, *, environment=None):
        source = os.environ if environment is None else environment
        case.auth_calls.append((uid, dict(source)))
        if case.auth_error:
            raise case.auth_error
        if not configured(environment=source):
            return None  # The real facade is also a no-op without cloud config.
        if not uid or uid != "alice":
            raise PermissionError("phone caller is not the configured owner")
        if source.get("MEYO_AGENTBAY_CONFIG") != str(config):
            raise ValueError("invalid private phone configuration")
        return None

    def record(name, arguments):
        import agent_tool_provider
        _, bound, _ = agent_tool_provider._runtime_binding()
        case.calls.append((name, copy.deepcopy(arguments), dict(bound)))
        if case.callback_error:
            raise case.callback_error
        return copy.deepcopy(case.reply)

    def status(force=False, *, uid=""):
        return record("status", {"force": force, "uid": uid})

    def ensure(timeout=120, *, uid=""):
        return record("ensure", {"timeout": timeout, "uid": uid})

    def screenshot(*, uid=""):
        return record("screenshot", {"uid": uid})

    def action(name, params=None, *, uid=""):
        return record("action", {"name": name, "params": params, "uid": uid})

    def stop(*, uid=""):
        return record("stop", {"uid": uid})

    provider.cloud_configured = configured
    provider.authorize_cloud_owner = authorize
    provider.status, provider.ensure = status, ensure
    provider.screenshot, provider.action, provider.stop = screenshot, action, stop
    monkeypatch.setitem(sys.modules, "mobile_device", provider)
    case.provider = provider
    case.host = importlib.import_module("native_capability_host")
    external = importlib.import_module("external_agent_registry")
    monkeypatch.setattr(external, "load_catalog", lambda: [])
    cloud = importlib.import_module("cloud_desktop")
    monkeypatch.setattr(cloud, "_command", forbidden)
    monkeypatch.setattr(cloud, "local_mcp_call", forbidden)
    return case


def session(phone, ids=None, **context):
    return phone.host.CapabilitySession({
        **phone.context, "allowed_ids": IDS if ids is None else ids, **context,
    })


def call(bound, name="mobile_status", params=None):
    return bound.call_capability_json({
        "tool_id": PREFIX + name, "params_json": json.dumps({} if params is None else params),
    })


def parent_spec(phone, environment=None, **context):
    env = dict(os.environ) if environment is None else environment
    return phone.host.native_stdio_spec(
        SimpleNamespace(**{**phone.context, "execution_depth": "standard", **context}),
        {**env, "MEYO_NATIVE_CAPABILITY_IDS": json.dumps(IDS)}, SERVER,
    )


def server(phone, monkeypatch, *, spec=None):
    spec = parent_spec(phone) if spec is None else spec
    monkeypatch.setattr(os, "environ", dict(spec["env"]))
    monkeypatch.setattr(sys, "argv", [str(SERVER), "--shared-capabilities"])
    loader = importlib.util.spec_from_file_location("_agentbay_native_mcp_test", SERVER)
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)  # Real from_environment, not a replacement session.
    return module


def rpc(module, name="mobile_status", params=None):
    return module._handle({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "call_capability", "arguments": {
            "tool_id": PREFIX + name, "params_json": json.dumps({} if params is None else params),
        }},
    })["result"]


def shot(phone, data=None):
    data = _png() if data is None else data
    path = phone.mobile / "fixture.png"
    path.write_bytes(data)
    path.chmod(0o600)
    phone.reply = {
        "ok": True, "path": str(path), "bytes": len(data), "width_height": "2x1",
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    return path


def test_exact_mobile_declarations_are_static_not_probes_or_provider_registration(phone, monkeypatch):
    monkeypatch.delenv("MEYO_AGENTBAY_CONFIG")
    listing = phone.host.declared_capability_actions()
    rows = {row["id"]: row for row in listing["actions"]}
    assert {key for key in rows if key.startswith(PREFIX)} == set(IDS)
    for tool_id in IDS:
        assert rows[tool_id]["server"] == "meyo-mobile"
        assert rows[tool_id]["input_schema"]["additionalProperties"] is False
    assert not phone.calls and not phone.auth_calls and not phone.configured_calls
    assert listing["count"] == len(rows)


def test_native_action_declaration_preserves_scalar_json_parameters(phone):
    bound = session(phone, [PREFIX + "mobile_action"])
    row, = bound.list_capabilities({})["actions"]
    assert row["input_schema"]["required"] == ["name", "params_json"]
    assert row["input_schema"]["properties"]["params_json"]["type"] == "string"
    assert "JSON" in row["input_schema"]["properties"]["params_json"]["description"]
    assert set(row["input_schema"]["properties"]) == {"name", "params_json"}
    assert {tool[0] for tool in bound.tools} == {"list_capabilities", "call_capability"}
    assert not phone.calls


@pytest.mark.parametrize("name,params,callback,expected", [
    ("mobile_status", {}, "status", {"force": False, "uid": "alice"}),
    ("mobile_status", {"force": True}, "status", {"force": True, "uid": "alice"}),
    ("mobile_start", {}, "ensure", {"timeout": 120, "uid": "alice"}),
    ("mobile_start", {"timeout": 20}, "ensure", {"timeout": 20, "uid": "alice"}),
    ("mobile_action", {"name": "tap", "params_json": '{"x":12,"y":34}'},
     "action", {"name": "tap", "params": {"x": 12, "y": 34}, "uid": "alice"}),
    ("mobile_stop", {}, "stop", {"uid": "alice"}),
])
def test_production_native_function_calls_the_existing_phone_api_with_bound_context(
        phone, name, params, callback, expected):
    result = call(session(phone), name, params)
    assert result["ok"] is True
    actual, arguments, bound = phone.calls.pop()
    assert actual == callback and arguments == expected
    assert bound["MEYO_USER_ID"] == "alice"
    assert bound["MEYO_SESSION_ID"] == "original-native-session"
    assert bound["MEYO_WORKDIR"] == str(phone.work)
    assert json.loads(bound["MEYO_TASK_FACTS_JSON"]) == phone.context["task_facts"]
    assert json.loads(bound["MEYO_PROJECT_CONTEXT_JSON"]) == phone.context["project_context"]
    assert phone.auth_calls[-1][0] == "alice"


def test_ungranted_phone_operation_cannot_be_selected_through_the_generic_tool(phone):
    bound = session(phone, [PREFIX + "mobile_status"])
    assert [row["id"] for row in bound.list_capabilities({})["actions"]] == [PREFIX + "mobile_status"]
    with pytest.raises(ValueError):
        call(bound, "mobile_start")
    assert not phone.calls


@pytest.mark.parametrize("name", ["create_sandbox", "get_sandbox_url", "task", "install", "*"])
def test_no_other_phone_capability_is_registered(phone, name):
    with pytest.raises(ValueError):
        phone.host.validate_ids([PREFIX + name])
    assert not phone.calls


def test_parent_stdio_forwards_only_the_config_path_and_uses_supplied_environment(phone, monkeypatch):
    given = {
        **os.environ, "AGENTBAY_API_KEY": "fake-provider-key",
        "MEYO_AGENTBAY_API_KEY": "fake-provider-key",
        "MEYO_AGENTBAY_URL": "https://provider.invalid/?APIKEY=fake-provider-key",
        "OPENAI_API_KEY": "fake-model-key", "MEYO_AGENTBAY_OWNER": "mallory",
    }
    monkeypatch.setenv("MEYO_AGENTBAY_CONFIG", "/unrelated/parent/config.json")
    before = dict(os.environ)
    spec = parent_spec(phone, given)
    assert spec["env"]["MEYO_AGENTBAY_CONFIG"] == str(phone.config)
    assert not {
        "AGENTBAY_API_KEY", "MEYO_AGENTBAY_API_KEY", "MEYO_AGENTBAY_URL",
        "OPENAI_API_KEY", "MEYO_AGENTBAY_OWNER",
    }.intersection(spec["env"])
    assert "fake-provider-key" not in repr(spec)
    assert os.environ == before, "validation must not mutate the parent process environment"
    assert phone.auth_calls[-1][1]["MEYO_AGENTBAY_CONFIG"] == str(phone.config)
    assert spec["command"] and spec["args"] == ["-I", str(SERVER), "--shared-capabilities"]


def test_from_environment_cleans_inherited_credentials_and_rechecks_owner(phone, monkeypatch):
    spec = parent_spec(phone)
    monkeypatch.setattr(os, "environ", {
        **spec["env"], "AGENTBAY_API_KEY": "fake-inherited-key",
        "MEYO_AGENTBAY_URL": "https://provider.invalid/", "MEYO_USER_ID": "mallory",
    })
    bound = phone.host.from_environment()
    assert bound.context["user_id"] == "alice"
    assert os.environ["MEYO_AGENTBAY_CONFIG"] == str(phone.config)
    assert not {"AGENTBAY_API_KEY", "MEYO_AGENTBAY_URL", "MEYO_USER_ID"}.intersection(os.environ)
    assert phone.auth_calls[-1][0] == "alice"
    assert not phone.calls


def test_phone_config_is_not_delegated_to_a_task_without_phone_grants(phone):
    environment = {
        **os.environ, "MEYO_NATIVE_CAPABILITY_IDS": '["external_capability:fixture:inspect"]',
    }
    spec = phone.host.native_stdio_spec(
        SimpleNamespace(**{**phone.context, "user_id": "mallory", "execution_depth": "standard"}),
        environment, SERVER,
    )
    assert "MEYO_AGENTBAY_CONFIG" not in spec["env"]
    assert not phone.auth_calls and not phone.calls


def test_ungranted_phone_config_is_removed_again_when_loading_the_native_host(phone, monkeypatch):
    monkeypatch.setenv("MEYO_NATIVE_HOST_CONTEXT", json.dumps({
        **phone.context, "user_id": "mallory", "allowed_ids": [],
    }))
    bound = phone.host.from_environment()
    assert not bound.actions
    assert "MEYO_AGENTBAY_CONFIG" not in os.environ
    assert not phone.auth_calls and not phone.calls


@pytest.mark.parametrize("entry", ["spec", "host", "call"])
@pytest.mark.parametrize("uid", ["", "mallory", "../alice"])
def test_invalid_caller_is_rejected_before_any_phone_access(phone, monkeypatch, entry, uid):
    if entry == "call":
        bound = session(phone)
        bound.context["user_id"] = uid
    with pytest.raises((ValueError, PermissionError)):
        if entry == "spec":
            parent_spec(phone, user_id=uid)
        elif entry == "host":
            binding = {**phone.context, "user_id": uid}
            monkeypatch.setenv("MEYO_NATIVE_HOST_CONTEXT", json.dumps(binding))
            phone.host.from_environment()
        else:
            call(bound)
    assert not phone.calls


@pytest.mark.parametrize("entry", ["spec", "host", "call"])
def test_unconfigured_cloud_never_falls_through_to_the_legacy_shared_phone(
        phone, monkeypatch, entry):
    bound = session(phone) if entry == "call" else None
    monkeypatch.delenv("MEYO_AGENTBAY_CONFIG")
    with pytest.raises(ValueError):
        if entry == "spec":
            parent_spec(phone)
        elif entry == "host":
            monkeypatch.setenv("MEYO_NATIVE_HOST_CONTEXT", json.dumps(phone.context))
            phone.host.from_environment()
        else:
            call(bound)
    assert not phone.calls


@pytest.mark.parametrize("error", [
    ValueError("invalid private configuration"),
    PermissionError("owner changed"), OSError("unreadable private configuration"),
])
def test_native_reuses_strict_provider_validation_instead_of_skipping_it(phone, error):
    bound = session(phone)
    phone.auth_error = error
    with pytest.raises(type(error)):
        parent_spec(phone)
    with pytest.raises(type(error)):
        call(bound)
    assert not phone.calls


@pytest.mark.parametrize("name,params", [
    ("mobile_status", {"uid": "mallory"}),
    ("mobile_start", {"sandbox_id": "another-phone"}),
    ("mobile_stop", {"config": "/another/config.json"}),
    ("mobile_screenshot", {"path": "/another/owner.png"}),
    ("mobile_status", {"force": "yes"}),
    ("mobile_start", {"timeout": True}),
    ("mobile_action", {"name": "tap", "params": {"x": 1, "y": 2}}),
    ("mobile_action", {"name": "tap", "params_json": "[]"}),
    ("mobile_action", {"name": "tap", "params_json": "null"}),
    ("mobile_action", {"name": "tap", "params_json": "not JSON"}),
    ("mobile_action", {"name": "tap", "params_json": "{}", "uid": "mallory"}),
])
def test_model_parameters_cannot_override_host_identity_or_the_declared_shape(phone, name, params):
    with pytest.raises(ValueError):
        call(session(phone), name, params)
    assert not phone.calls


@pytest.mark.parametrize("field", [
    "uid", "user_id", "owner", "sid", "sandbox_id", "config", "config_path",
    "MEYO_AGENTBAY_CONFIG", "api_key", "api_key_file", "control_dir",
])
def test_action_json_cannot_smuggle_identity_or_configuration_overrides(phone, field):
    with pytest.raises(ValueError):
        call(session(phone), "mobile_action", {
            "name": "tap", "params_json": json.dumps({"x": 1, "y": 2, field: "forged"}),
        })
    assert not phone.calls


def test_existing_execute_policy_is_not_bypassed_for_phone_actions(phone):
    result = call(session(phone, capability_policy={"execute": False}), "mobile_start")
    assert result["ok"] is False and result["authorization"]["decision"] == "deny"
    assert not phone.calls


def test_actual_stdio_functions_report_remaining_time_and_preserve_failure(phone, monkeypatch):
    module = server(phone, monkeypatch)
    listed = module._handle({"id": 1, "method": "tools/list"})["result"]["tools"]
    assert {row["name"] for row in listed} == {"list_capabilities", "call_capability"}
    ok = rpc(module)
    text, = ok["content"]
    assert json.loads(text["text"])["remaining_budget_seconds"] == 1200
    phone.reply = {
        "ok": False, "error_code": "budget_exhausted", "error": "本次手机额度已耗尽",
        "provider": "agentbay", "phase": "blocked", "ready": False,
    }
    result = rpc(module, "mobile_start")
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"]) == phone.reply
    assert [row["type"] for row in result["content"]] == ["text"]


@pytest.mark.parametrize("resume", [False, True])
def test_actual_adapter_configuration_reaches_production_mcp_and_image_functions(
        phone, adapter, monkeypatch, resume):
    monkeypatch.setenv("MEYO_NATIVE_CAPABILITY_IDS", json.dumps(IDS))
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    model, key, gateway = "fixture-phone-model", "fake-model-key", "https://model.invalid/v1"
    checkpoint = {
        "session_id": "fixture-native-thread",
        "provider_binding": adapter.mr.ccr_binding_key(model, key, gateway),
    }
    ctx = adapter.hr.HarnessCtx(
        model=model, appid=key, gateway=gateway, model_protocol="responses",
        user_id="alice", session_id=phone.context["session_id"], workdir=str(phone.work),
        execution_depth="standard", capability_policy=phone.context["capability_policy"],
        task_facts=phone.context["task_facts"], project_context=phone.context["project_context"],
        task_memory={"native_checkpoint": checkpoint} if resume else {},
    )
    result = adapter.hr._codex_adapter("__ut_agentbay_stdio_chain", "original phone task", ctx=ctx)
    assert result["status"] == "ok", result
    captured, = adapter.calls
    config = captured["config"]
    stdio = config["mcp_servers"]["meyo_capabilities"]
    assert stdio["env"]["MEYO_AGENTBAY_CONFIG"] == str(phone.config)
    assert key not in repr(stdio) and "MEYO_DEVICE_DOCKER_HOST" not in stdio["env"]
    assert config["permissions"][config["default_permissions"]]["filesystem"][
        str(phone.config.parent)] == "none"
    assert ("resume" in captured["command"]) is resume
    shot(phone)
    image_result = rpc(server(phone, monkeypatch, spec=stdio), "mobile_screenshot")
    assert not image_result.get("isError")
    assert [row["type"] for row in image_result["content"]] == ["text", "image"]
    assert base64.b64decode(image_result["content"][1]["data"], validate=True) == _png()
    assert phone.calls[-1][0:2] == ("screenshot", {"uid": "alice"})


@pytest.mark.parametrize("reply", [
    {"ok": False, "ready": True, "error_code": "provider_failed"},
    {"ok": 1, "ready": True}, {"ready": True}, {"ok": "true"},
])
def test_action_results_without_explicit_success_are_not_upgraded(phone, monkeypatch, reply):
    module = server(phone, monkeypatch)
    phone.reply = reply
    result = rpc(module, "mobile_action", {"name": "tap", "params_json": '{"x":1,"y":2}'})
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["ok"] is False


def test_provider_connection_fields_are_not_exposed_in_native_output(phone, monkeypatch):
    module = server(phone, monkeypatch)
    phone.reply.update(
        api_key="fake-key-never-output",
        url="https://provider.invalid/?APIKEY=fake-key-never-output",
        connection={"url": "https://provider.invalid/"},
        config={"api_key_file": str(phone.config.parent / "api-key.txt")},
    )
    result = rpc(module)
    assert "fake-key-never-output" not in repr(result)
    assert "https://" not in repr(result) and "api-key.txt" not in repr(result)
    assert json.loads(result["content"][0]["text"])["ok"] is True


def test_unexpected_provider_exception_does_not_echo_private_connection_text(phone, monkeypatch):
    module = server(phone, monkeypatch)
    phone.callback_error = RuntimeError("https://provider.invalid/?APIKEY=fake-key-never-output")
    result = rpc(module, "mobile_start")
    assert result["isError"] is True
    assert "fake-key-never-output" not in repr(result) and "https://" not in repr(result)


def test_actual_screenshot_rpc_validates_bytes_and_emits_a_native_image(phone, monkeypatch):
    source = shot(phone)
    module = server(phone, monkeypatch)
    result = rpc(module, "mobile_screenshot")
    assert not result.get("isError")
    text, image = result["content"]
    receipt = json.loads(text["text"])
    assert text["type"] == "text" and image["type"] == "image"
    assert image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"], validate=True) == _png()
    target = Path(receipt["path"])
    assert target.parent == phone.work and target != source
    assert target.read_bytes() == source.read_bytes() == _png()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert receipt["sha256"] == hashlib.sha256(_png()).hexdigest()
    assert receipt["bytes"] == len(_png()) and receipt["width_height"] == "2x1"
    assert receipt["model_visual_understanding"] == "unverified"
    assert image["data"] not in text["text"]
    assert phone.calls[-1][0:2] == ("screenshot", {"uid": "alice"})
    assert "provider.invalid" not in repr(result)


def test_screenshot_through_the_existing_action_api_is_also_a_native_image(phone, monkeypatch):
    shot(phone)
    result = rpc(server(phone, monkeypatch), "mobile_action", {
        "name": "screenshot", "params_json": "{}",
    })
    assert not result.get("isError")
    assert [row["type"] for row in result["content"]] == ["text", "image"]
    assert base64.b64decode(result["content"][1]["data"], validate=True) == _png()
    assert phone.calls[-1][0:2] == (
        "action", {"name": "screenshot", "params": {}, "uid": "alice"},
    )


@pytest.mark.parametrize("data", [
    b"not a PNG", b"\x89PNG\r\n\x1a\n", _png()[:-7],
    _png(compressed=b"not-zlib"), _png(width=100_000, height=100_000),
    _png() + b"private trailing data",
])
def test_invalid_actual_png_bytes_never_become_native_images_or_workspace_artifacts(
        phone, monkeypatch, data):
    shot(phone, data)
    result = rpc(server(phone, monkeypatch), "mobile_screenshot")
    assert result["isError"] is True
    assert [row["type"] for row in result["content"]] == ["text"]
    assert not list(phone.work.iterdir())


@pytest.mark.parametrize("kind", [
    "other-owner", "outside-mobile", "relative", "parent-traversal",
    "symlink-file", "symlink-mobile", "symlink-owner", "symlink-users", "symlink-data",
    "directory", "oversized", "not-png-suffix",
])
def test_screenshot_file_readback_is_confined_to_the_managed_owner_without_symlinks(
        phone, monkeypatch, kind):
    path = shot(phone)
    if kind == "other-owner":
        target = phone.root / "data/users/mallory/mobile/fixture.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(_png())
        phone.reply["path"] = str(target)
    elif kind == "outside-mobile":
        target = phone.root / "outside.png"
        target.write_bytes(_png())
        phone.reply["path"] = str(target)
    elif kind == "relative":
        phone.reply["path"] = "fixture.png"
    elif kind == "parent-traversal":
        phone.reply["path"] = str(phone.mobile) + "/../mobile/fixture.png"
    elif kind == "symlink-file":
        target = phone.root / "outside.png"
        path.rename(target)
        path.symlink_to(target)
    elif kind.startswith("symlink-"):
        parent = {
            "symlink-mobile": phone.mobile, "symlink-owner": phone.mobile.parent,
            "symlink-users": phone.mobile.parent.parent, "symlink-data": phone.root / "data",
        }[kind]
        target = parent.with_name(parent.name + "-saved")
        parent.rename(target)
        parent.symlink_to(target, target_is_directory=True)
    elif kind == "directory":
        path.unlink()
        path.mkdir()
    elif kind == "oversized":
        with path.open("r+b") as stream:
            stream.truncate(phone.host._MAX_IMAGE_BYTES + 1)
    else:
        target = path.with_suffix(".txt")
        path.rename(target)
        phone.reply["path"] = str(target)
    result = rpc(server(phone, monkeypatch), "mobile_screenshot")
    assert result["isError"] is True
    assert [row["type"] for row in result["content"]] == ["text"]
    assert not list(phone.work.iterdir())


def test_failed_screenshot_receipt_cannot_be_promoted_by_a_valid_file(phone, monkeypatch):
    shot(phone)
    phone.reply.update(ok=False, error_code="capture_failed", error="截图失败")
    result = rpc(server(phone, monkeypatch), "mobile_screenshot")
    assert result["isError"] is True
    assert json.loads(result["content"][0]["text"])["error_code"] == "capture_failed"
    assert [row["type"] for row in result["content"]] == ["text"]
    assert not list(phone.work.iterdir())
