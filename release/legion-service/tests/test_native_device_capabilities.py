"""Native device grants and visual transport; no real devices, models or network."""
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
import struct
import subprocess
import sys
from types import SimpleNamespace
import zlib

import pytest


SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
SERVER = ORCH / "meyo_mcp_servers/orchestrator_server.py"
COMPUTER = "local_mcp:cua-local-desktop:computer_command"
STATUS = "local_mcp:cua-local-desktop:desktop_status"
EXTERNAL = "external_capability:fixture:inspect"


def test_native_desktop_descriptions_explain_the_installed_linux_input_protocol(device):
    context = copy.deepcopy(device.context)
    control = "local_mcp:cua-local-desktop:desktop_control"
    context["allowed_ids"] = [COMPUTER, control]
    listing = device.host.CapabilitySession(context).list_capabilities({})
    descriptions = {row["id"]: row["description"] for row in listing["actions"]}
    for tool_id in (COMPUTER, control):
        description = descriptions[tool_id]
        assert 'press_key' in description and '"key":"enter"' in description
        assert '"key":"esc"' in description
        assert 'hotkey' in description and '"keys":["ctrl","s"]' in description
        assert "set_clipboard" in description and "copy_to_clipboard" in description
        assert "读取" in description and "当前云桌面" in description
        assert "占位" in description and "截图" in description


def _png(width=2, height=1, *, compressed=None, color=6):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))

    header = struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0)
    pixels = b"\x00" + b"\x00\xff\x00\xff" * 2
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(pixels) if compressed is None else compressed)
            + chunk(b"IEND", b""))


def _cua_receipt(data=None):
    return {
        "server": "cua-local-desktop", "tool": "computer_command", "status": "ok",
        "result": {
            "ok": True, "sandbox": "meyo-desktop-alice", "command": "screenshot",
            "result": {
                "success": True, "image_data": base64.b64encode(_png() if data is None else data).decode(),
                "format": "png",
            },
            "transport": "managed_local_cua_computer_server",
        },
    }


@pytest.fixture(autouse=True)
def device(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ORCH))
    monkeypatch.syspath_prepend(str(SERVICE))
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR",
                "LEGION_SESSIONS_DIR", "LEGION_TASK_MEMORY_DIR", "LEGION_TEAM_DIR",
                "LEGION_PROJECT_CONTEXT_DIR", "LEGION_EXTERNAL_SOURCE_ROOT",
                "LEGION_EXTERNAL_RUNTIME_ROOT"):
        folder = tmp_path / key
        folder.mkdir()
        monkeypatch.setenv(key, str(folder))
    monkeypatch.setenv("LEGION_EXT_AUTO_UPDATE", "0")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    for key in tuple(os.environ):
        if key.startswith(("MEYO_DEVICE_", "MEYO_DESKTOP_", "MEYO_PHONE_")):
            monkeypatch.delenv(key)
    environment = {
        "MEYO_DEVICE_DOCKER_HOST": f"unix:///run/user/{os.getuid()}/docker.sock",
        "MEYO_DEVICE_DOCKER_DATA_ROOT": str(tmp_path / "docker-data"),
        "MEYO_DESKTOP_PUBLISH_PORT": "32768",
        "MEYO_DESKTOP_PUBLIC_PATH": "/desktop",
        "MEYO_DESKTOP_OWNER": "alice",
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("real network, process, Docker or phone call in an offline test")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setitem(sys.modules, "mobile_device",
                        SimpleNamespace(ensure=forbidden, action=forbidden, status=forbidden))
    host = importlib.import_module("native_capability_host")
    cloud = importlib.import_module("cloud_desktop")
    external = importlib.import_module("external_agent_registry")
    monkeypatch.setattr(cloud, "_command", forbidden)
    monkeypatch.setattr(external, "load_catalog", lambda: [{
        "key": "fixture", "capability_actions": {
            "inspect": {"command": ["not-executed"], "params": ["request_file"],
                        "description": "Existing external file action"},
        },
    }])
    work = tmp_path / "work"
    work.mkdir()
    context = {
        "user_id": "alice", "session_id": "original-session", "workdir": str(work),
        "task_facts": {"team_id": "original-team", "task_id": "native-device"},
        "project_context": {"workspace_root": str(work)},
        "capability_policy": {"execute": True, "publish": False},
        "allowed_ids": [COMPUTER],
    }
    calls = []
    state = SimpleNamespace(host=host, cloud=cloud, context=context, work=work,
                            environment=environment, calls=calls, receipt=_cua_receipt(),
                            real_callback=cloud.local_mcp_call)

    def callback(name, arguments=None, *, bound_uid=None):
        import agent_tool_provider
        _, bound, _ = agent_tool_provider._runtime_binding()
        calls.append((name, copy.deepcopy(arguments), bound_uid, dict(bound)))
        return copy.deepcopy(state.receipt)

    monkeypatch.setattr(cloud, "local_mcp_call", callback)
    return state


def _session(device, ids=None, **changes):
    return device.host.CapabilitySession({
        **device.context, "allowed_ids": ids or [COMPUTER], **changes,
    })


def _call(session, params=None, tool_id=COMPUTER):
    return session.call_capability_json({
        "tool_id": tool_id,
        "params_json": json.dumps({"command": "screenshot", "params": {}} if params is None else params),
    })


def _server(device, monkeypatch, session=None):
    session = session or _session(device)
    monkeypatch.setattr(device.host, "from_environment", lambda: session)
    monkeypatch.setattr(sys, "argv", [str(SERVER), "--shared-capabilities"])
    spec = importlib.util.spec_from_file_location("_offline_device_mcp", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rpc(server, params=None):
    return server._handle({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {
            "name": "call_capability",
            "arguments": {"tool_id": COMPUTER, "params_json": json.dumps(
                {"command": "screenshot", "params": {}} if params is None else params)},
        },
    })["result"]


def test_declared_actions_reuse_local_schema_without_probing_or_mutating_it(device):
    original = copy.deepcopy(device.cloud.local_mcp_tools())
    declared = getattr(device.host, "declared_capability_actions", None)
    assert callable(declared), "configuration and native host need one declaration source"
    result = declared()
    rows = {row["id"]: row for row in result["actions"]}
    assert result["count"] == len(rows)
    assert {COMPUTER, STATUS, EXTERNAL} <= rows.keys()
    assert {key for key in rows if key.startswith("local_mcp:meyo-mobile:")} == {
        "local_mcp:meyo-mobile:" + name for name in
        ("mobile_status", "mobile_start", "mobile_screenshot", "mobile_action", "mobile_stop")
    }
    schema = rows[COMPUTER]["input_schema"]
    assert "uid" not in schema.get("properties", {})
    assert schema["required"] == ["command"]
    assert schema["properties"]["params"] == {"type": "object"}
    assert "run_command" in rows[COMPUTER]["description"]
    assert rows[EXTERNAL]["params"] == ["request_file"]
    assert device.cloud.local_mcp_tools() == original
    assert not device.calls


def test_local_ids_and_existing_external_ids_validate(device):
    ids = [COMPUTER, STATUS, EXTERNAL, "external_capability:not-installed:action"]
    assert device.host.validate_ids(ids) == ids


@pytest.mark.parametrize("ids", [
    ["local_mcp:cua-local-desktop:no_such_tool"],
    ["local_mcp:another-server:computer_command"],
    ["local_mcp:mobile-device:tap"],
    ["local_mcp:cua-local-desktop:*"],
    [COMPUTER, COMPUTER],
    COMPUTER,
])
def test_unknown_wildcard_duplicate_or_phone_grants_are_rejected(device, ids):
    with pytest.raises(ValueError):
        device.host.validate_ids(ids)
    assert not device.calls


def test_only_explicit_local_grants_are_exposed_through_the_two_existing_tools(device):
    session = _session(device)
    assert {tool[0] for tool in session.tools} == {"list_capabilities", "call_capability"}
    listing = session.list_capabilities({})
    assert [row["id"] for row in listing["actions"]] == [COMPUTER]
    assert listing["readiness"] == "not_probed"
    with pytest.raises(ValueError):
        _call(session, {}, STATUS)
    assert not device.calls


def test_parent_device_environment_survives_stdio_and_host_cleaning(device, monkeypatch):
    env = {
        **os.environ, "MEYO_NATIVE_CAPABILITY_IDS": json.dumps([COMPUTER]),
        "OPENAI_API_KEY": "fixture-not-a-real-secret",
        "MEYO_DESKTOP_TOKEN": "fixture-do-not-pass",
        "MEYO_DESKTOP_PASSWORD": "fixture-do-not-pass",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
    }
    spec = device.host.native_stdio_spec(SimpleNamespace(
        **device.context, execution_depth="standard"), env, SERVER)
    assert all(spec["env"].get(key) == value for key, value in device.environment.items())
    assert not {"OPENAI_API_KEY", "MEYO_DESKTOP_TOKEN", "MEYO_DESKTOP_PASSWORD", "DOCKER_HOST"} & spec["env"].keys()
    monkeypatch.setattr(os, "environ", {
        **spec["env"], "OPENAI_API_KEY": "fixture-remove-on-host",
        "MEYO_DESKTOP_TOKEN": "fixture-remove-on-host",
    })
    loaded = device.host.from_environment()
    assert loaded.context["user_id"] == "alice"
    assert all(os.environ.get(key) == value for key, value in device.environment.items())
    assert "OPENAI_API_KEY" not in os.environ and "MEYO_DESKTOP_TOKEN" not in os.environ
    assert not device.calls


@pytest.mark.parametrize("missing", [
    "MEYO_DEVICE_DOCKER_HOST", "MEYO_DEVICE_DOCKER_DATA_ROOT",
    "MEYO_DESKTOP_PUBLISH_PORT", "MEYO_DESKTOP_PUBLIC_PATH", "MEYO_DESKTOP_OWNER",
])
def test_missing_local_configuration_fails_before_callback_or_docker_discovery(device, monkeypatch, missing):
    session = _session(device)
    monkeypatch.delenv(missing)
    with pytest.raises(ValueError):
        _call(session)
    assert not device.calls


def test_parent_spec_rejects_unconfigured_local_grant(device):
    with pytest.raises(ValueError):
        device.host.native_stdio_spec(SimpleNamespace(
            **device.context, execution_depth="standard"),
            {"MEYO_NATIVE_CAPABILITY_IDS": json.dumps([COMPUTER])}, SERVER)


@pytest.mark.parametrize("field,value", [
    ("MEYO_DEVICE_DOCKER_HOST", "unix:///var/run/docker.sock"),
    ("MEYO_DEVICE_DOCKER_HOST", "unix:///run/user/99999999/docker.sock"),
    ("MEYO_DESKTOP_OWNER", "mallory"),
])
def test_existing_desktop_configuration_rejects_wrong_socket_or_owner(device, monkeypatch, field, value):
    session = _session(device)
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError):
        _call(session)
    assert not device.calls


def test_authorized_command_keeps_native_semantics_and_explicit_context(device):
    device.receipt = {"server": "cua-local-desktop", "status": "ok",
                      "result": {"ok": True, "result": {"success": True, "stdout": "ok"}}}
    params = {"command": "run_command", "params": {"command": "printf ok"}}
    result = _call(_session(device), params)
    assert result["ok"] is True
    name, arguments, uid, context = device.calls[0]
    assert name == "computer_command" and arguments == {**params, "uid": "alice"} and uid == "alice"
    assert context["MEYO_USER_ID"] == "alice"
    assert context["MEYO_SESSION_ID"] == "original-session"
    assert context["MEYO_WORKDIR"] == str(device.work)
    assert json.loads(context["MEYO_TASK_FACTS_JSON"]) == device.context["task_facts"]


def test_existing_file_callback_keeps_bound_owner_without_process_environment_identity(device, monkeypatch):
    import desktop_fs
    monkeypatch.delenv("MEYO_USER_ID", raising=False)
    monkeypatch.delenv("MEYO_LEGION_UID", raising=False)
    monkeypatch.setattr(device.cloud, "local_mcp_call", device.real_callback)
    monkeypatch.setattr(desktop_fs, "list_files", lambda uid, path: {
        "ok": True, "owner_seen_by_existing_file_callback": uid, "path": path,
    })
    tool = "local_mcp:cua-local-desktop:desktop_fs_list"
    result = _call(_session(device, [tool]), {"path": "projects"}, tool)
    assert result["result"]["owner_seen_by_existing_file_callback"] == "alice"


@pytest.mark.parametrize("params", [
    {}, {"command": 1}, {"command": "screenshot", "params": "not-an-object"},
    {"command": "screenshot", "uid": "mallory"},
    {"command": "screenshot", "uid": "alice"},
])
def test_schema_or_uid_override_fails_before_device_callback(device, params):
    with pytest.raises(ValueError):
        _call(_session(device), params)
    assert not device.calls


def test_local_schema_optional_fields_and_array_items_are_honored(device):
    tool = "local_mcp:cua-local-desktop:desktop_app"
    device.receipt = {"status": "ok", "result": {"ok": True}}
    session = _session(device, [tool])
    assert _call(session, {}, tool)["ok"] is True
    assert _call(session, {"action": "open", "args": ["example.txt"]}, tool)["ok"] is True
    with pytest.raises(ValueError):
        _call(session, {"args": [1]}, tool)
    assert len(device.calls) == 2


def test_execute_denial_uses_existing_permission_policy_and_does_not_call_device(device):
    result = _call(_session(device, capability_policy={"execute": False}))
    assert result["ok"] is False
    assert result["authorization"]["decision"] == "deny"
    assert result["authorization"]["action"] == "execute"
    assert not device.calls and not list(device.work.iterdir())


def test_screenshot_rpc_emits_image_and_real_workspace_png_without_text_base64(device, monkeypatch):
    server = _server(device, monkeypatch)
    result = _rpc(server)
    assert not result.get("isError")
    assert [item["type"] for item in result["content"]] == ["text", "image"]
    text, image = result["content"]
    receipt = json.loads(text["text"])
    path = Path(receipt["path"])
    assert path.parent == device.work and path.read_bytes() == _png()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert receipt["bytes"] == len(_png())
    assert receipt["sha256"] == hashlib.sha256(_png()).hexdigest()
    assert receipt["model_visual_understanding"] == "unverified"
    assert image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"], validate=True) == _png()
    assert image["data"] not in text["text"]
    assert len(text["text"]) < 1500
    second = json.loads(_rpc(server)["content"][0]["text"])
    assert second["path"] != str(path) and path.read_bytes() == _png()


@pytest.mark.parametrize("broken", [
    b"not a png", b"\x89PNG\r\n\x1a\n", _png()[:-5],
    _png()[:40] + b"broken" + _png()[46:],
    _png(compressed=b"invalid zlib stream"),
    _png(width=100_000, height=100_000),
])
def test_invalid_png_never_creates_image_content_or_artifact(device, monkeypatch, broken):
    device.receipt = _cua_receipt(broken)
    result = _rpc(_server(device, monkeypatch))
    assert result["isError"] is True
    assert all(item["type"] == "text" for item in result["content"])
    assert not list(device.work.iterdir())


def test_bad_or_oversized_base64_is_not_echoed_or_written(device, monkeypatch):
    server = _server(device, monkeypatch)
    for invalid in ("not-base64!", "A" * (12 * 1024 * 1024)):
        device.receipt["result"]["result"]["image_data"] = invalid
        result = _rpc(server)
        assert result["isError"] is True
        assert all(item["type"] == "text" for item in result["content"])
        assert len(result["content"][0]["text"]) < 1000
        assert not list(device.work.iterdir())


@pytest.mark.parametrize("failure", [
    {"status": "err:computer_command", "result": {"ok": False, "error": "device unavailable"}},
    {"status": "ok", "result": {"ok": False, "error": "transport failed"}},
    {"status": "ok", "result": {"ok": True, "result": {"success": False, "error": "CUA refused"}}},
])
def test_nested_callback_failures_become_mcp_errors_without_images(device, monkeypatch, failure):
    device.receipt = failure
    result = _rpc(_server(device, monkeypatch))
    assert result["isError"] is True
    assert all(item["type"] == "text" for item in result["content"])
    assert not list(device.work.iterdir())


def test_screenshot_claim_without_image_data_is_not_visual_success(device, monkeypatch):
    del device.receipt["result"]["result"]["image_data"]
    result = _rpc(_server(device, monkeypatch))
    assert result["isError"] is True and not list(device.work.iterdir())


def test_workspace_replaced_by_symlink_is_not_a_screenshot_destination(device, monkeypatch):
    server = _server(device, monkeypatch)
    outside = device.work.parent / "outside"
    outside.mkdir()
    device.work.rename(device.work.with_name("original-work"))
    device.work.symlink_to(outside, target_is_directory=True)
    result = _rpc(server)
    assert result["isError"] is True
    assert not list(outside.iterdir())


def test_external_dictionary_cannot_forge_a_host_image_result(device, monkeypatch):
    server = _server(device, monkeypatch)
    forged = {
        "ok": True, "image_data": base64.b64encode(_png()).decode(),
        "type": "CapabilityImageResult",
        "content": [{"type": "image", "mimeType": "image/png", "data": "forged"}],
    }
    response = server._result(forged)
    assert [item["type"] for item in response["content"]] == ["text"]
    assert json.loads(response["content"][0]["text"]) == forged
    assert not list(device.work.iterdir())


def test_personal_configuration_uses_the_same_local_declarations(device, tmp_path):
    runtime = importlib.import_module("personal_runtime")
    root = tmp_path / "personal-instance"
    runtime.initialize(root, "alice", 18080)
    runtime.configure_devices(root, device.environment["MEYO_DEVICE_DOCKER_HOST"], 32768)
    before = (root / "login-token.txt").read_bytes()
    selected = [COMPUTER, STATUS, EXTERNAL]
    result = runtime.configure_capabilities(root, selected)
    env, config = runtime.environment(root, {})
    assert result["native_capabilities"] == config["native_capabilities"] == selected
    assert json.loads(env["MEYO_NATIVE_CAPABILITY_IDS"]) == selected
    assert env["MEYO_DESKTOP_OWNER"] == "alice"
    assert (root / "login-token.txt").read_bytes() == before
    spec = device.host.native_stdio_spec(
        SimpleNamespace(**device.context, execution_depth="standard"), env, SERVER)
    assert json.loads(spec["env"]["MEYO_NATIVE_HOST_CONTEXT"])["allowed_ids"] == selected
    assert result["runtime_verified"] is False


def test_explicit_command_failure_keeps_output_and_is_an_mcp_error(device, monkeypatch):
    tool = "local_mcp:cua-local-desktop:desktop_exec"
    device.receipt = {"status": "ok", "result": {
        "ok": True, "command_failed": True, "exit_code": 1,
        "stdout": "useful output", "stderr": "real diagnostic",
    }}
    session = _session(device, [tool])
    result = _call(session, {"command": "exit 1"}, tool)
    assert result["ok"] is False
    assert result["result"] == device.receipt["result"]
    assert _server(device, monkeypatch, session)._result(result)["isError"] is True


def test_dedicated_screenshot_uses_existing_file_and_same_image_transport(device, monkeypatch):
    import desktop_fs
    tool = "local_mcp:cua-local-desktop:desktop_screenshot"
    root = Path(desktop_fs.prepare_user_root("alice")["root"])
    source = root / "personal/screenshots/one.png"
    source.parent.mkdir()
    source.write_bytes(_png())
    device.receipt = {"status": "ok", "result": {
        "ok": True, "uid": "alice", "path": "personal/screenshots/one.png", "bytes": len(_png()),
    }}
    session = _session(device, [tool])
    result = _call(session, {}, tool)
    assert Path(result["path"]).parent == device.work
    assert Path(result["path"]).read_bytes() == _png()
    assert source.read_bytes() == _png()
    assert len(device.calls) == 1 and device.calls[0][0] == "desktop_screenshot"
    assert [item["type"] for item in _server(device, monkeypatch, session)._result(result)["content"]] == ["text", "image"]


def test_dedicated_screenshot_cannot_read_an_escaped_file(device):
    tool = "local_mcp:cua-local-desktop:desktop_screenshot"
    device.receipt = {"status": "ok", "result": {"ok": True, "path": "../other-user.png"}}
    with pytest.raises(ValueError):
        _call(_session(device, [tool]), {}, tool)
    assert not list(device.work.iterdir())


def test_indexed_screenshot_without_palette_is_rejected(device, monkeypatch):
    invalid = _png(width=1, height=1, color=3, compressed=zlib.compress(b"\x00\x00"))
    device.receipt = _cua_receipt(invalid)
    result = _rpc(_server(device, monkeypatch))
    assert result["isError"] is True
    assert not list(device.work.iterdir())
