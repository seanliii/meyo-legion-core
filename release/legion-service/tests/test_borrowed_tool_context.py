"""Offline private runtime, lease and safe-build contracts for the public instance."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
TOKEN = "fixture-private-owner-token"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("MEYO_", "LEGION_")) or name == "CODEX_HOME":
            monkeypatch.delenv(name)
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "TMPDIR"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        raise AssertionError("offline lease tests cannot spawn or connect")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    registry = SimpleNamespace(_INPROC_RUNTIME_ENV=threading.local(),
        _INPROC_CANCEL_CHECK=threading.local(), _INPROC_EVENT_SINK=threading.local())
    registry.set_inproc_runtime_env = lambda values: setattr(registry._INPROC_RUNTIME_ENV, "values", dict(values))
    registry.set_inproc_cancel_check = lambda check: setattr(registry._INPROC_CANCEL_CHECK, "check", check)
    registry.set_inproc_event_sink = lambda cb: setattr(registry._INPROC_EVENT_SINK, "cb", cb)
    monkeypatch.setitem(sys.modules, "harness_registry", registry)
    monkeypatch.setitem(sys.modules, "legion_core", SimpleNamespace())
    provider = importlib.import_module("agent_tool_provider")
    monkeypatch.setattr(provider, "_PRIVATE_CALL", threading.local())
    return SimpleNamespace(provider=provider, registry=registry, mp=monkeypatch, root=tmp_path)


@pytest.fixture
def lease_scene(isolated):
    case = isolated
    work = case.root / "source"
    work.mkdir()
    catalog = work / "external_agents.json"
    catalog.write_text('{"agents": []}')
    control = case.root / "control"
    control.mkdir(mode=0o700)
    tokens = control / "tokens.json"
    tokens.write_text(json.dumps({TOKEN: {"user": "alice", "project_grants": [
        {"root": str(work), "export_paths": [], "source_write": True}]}}))
    tokens.chmod(0o600)
    case.mp.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tokens))
    case.mp.setattr(case.provider.external, "CATALOG", catalog)
    session = {"turns": [{"input_snapshot": {"capability_policy": {
        "install": True, "write": True, "execute": True, "network": False}}}]}
    case.mp.setitem(sys.modules, "sessions", SimpleNamespace(
        load_session=lambda sid, user_id="default": session if sid == "owner" and user_id == "alice" else None))
    policy = {"install": True, "write": True, "execute": True, "network": False}
    context = {"MEYO_USER_ID": "alice", "MEYO_SESSION_ID": "run",
               "MEYO_CAPABILITY_POLICY_JSON": json.dumps(policy),
               "MEYO_MODEL_WORKSPACE_ID": "public-profile-fixture",
               "MEYO_PERSONAL_STATE_DIR": str(case.root / "profile"),
               "MEYO_LEGION_DIR": str(case.root / "profile-data")}
    with case.provider._runtime_scope(context, None, inherit_process_context=False, personal_token=TOKEN):
        lease = case.provider._NativeOnboardLease(user_id="alice", session_id="run", owner_session="owner",
            workdir=str(work), policy=policy, cancel_check=None, project_context={}, personal_token=TOKEN)
    lease.authority = "127.0.0.1:12345"
    return SimpleNamespace(**vars(case), work=work, catalog=catalog, tokens=tokens,
                           lease=lease, session=session, context=context)


def test_private_nested_scope_clears_new_identity_and_restores_on_exception(isolated):
    p = isolated.provider
    marker = object()
    with p._runtime_scope({"MEYO_USER_ID": "alice"}, None,
                         personal_token=TOKEN, native_onboard=marker):
        assert p._bound_personal_token() == TOKEN
        with pytest.raises(RuntimeError):
            with p._runtime_scope({"MEYO_USER_ID": "bob"}, None, inherit_process_context=False):
                assert p._bound_personal_token() == ""
                assert p._bound_native_onboard() is None
                assert p._runtime_binding()[1]["MEYO_USER_ID"] == "bob"
                raise RuntimeError("fixture")
        assert p._bound_personal_token() == TOKEN
        assert p._bound_native_onboard() is marker
    assert p._bound_personal_token() == ""
    assert p._bound_native_onboard() is None


def test_private_target_projection_uses_real_tls_not_public_context(isolated):
    p = isolated.provider
    declaration = {"pi": {"model": "bound-target", "base_url": "https://target.example.invalid/v1",
                           "api_key_env": "TARGET_PRIVATE_SLOT", "protocol": "responses"}}
    isolated.mp.setenv("TARGET_PRIVATE_SLOT", "fake-host-default")
    isolated.mp.setenv("LEGION_MEMBER_MODEL_BINDINGS", json.dumps({"cc": {
        "model": "foreign", "base_url": "https://foreign.example.invalid/v1",
        "api_key_env": "FOREIGN_SLOT", "protocol": "responses"}}))
    isolated.mp.setenv("FOREIGN_SLOT", "fake-foreign-key")
    scope = {"MEYO_USER_ID": "alice", "LEGION_MEMBER_MODEL_BINDINGS": json.dumps(declaration),
             "TARGET_PRIVATE_SLOT": "fake-target-key", "LEGION_CODEX_TRANSPORT": "responses"}
    with p._runtime_scope(scope, None, inherit_process_context=False):
        public = p._runtime_binding()[1]
        assert not any(name in public for name in (
            "LEGION_MEMBER_MODEL_BINDINGS", "TARGET_PRIVATE_SLOT", "LEGION_CODEX_TRANSPORT"))
        binding, uid = p._private_member_model_binding("pi")
        assert binding["model"] == "bound-target" and binding["api_key"] == "fake-target-key"
        assert uid == "alice"
        with pytest.raises(ValueError):
            p._private_member_model_binding("cc")
        with p._runtime_scope({"MEYO_USER_ID": "bob"}, None, inherit_process_context=False):
            with pytest.raises(ValueError):
                p._private_member_model_binding("pi")
        assert p._private_member_model_binding("pi")[0] == binding
    assert not getattr(isolated.registry._INPROC_RUNTIME_ENV, "values", {})


@pytest.mark.parametrize("fault", ["slot_missing", "no_owner", "cancelled"])
def test_private_projection_cannot_recover_host_authority(isolated, fault):
    p = isolated.provider
    declaration = {"pi": {"model": "bound-target", "base_url": "https://target.example.invalid/v1",
                           "api_key_env": "TARGET_PRIVATE_SLOT", "protocol": "responses"}}
    isolated.mp.setenv("TARGET_PRIVATE_SLOT", "fake-host-key")
    env = {"LEGION_MEMBER_MODEL_BINDINGS": json.dumps(declaration)}
    if fault != "no_owner":
        env["MEYO_USER_ID"] = "alice"
    if fault != "slot_missing":
        env["TARGET_PRIVATE_SLOT"] = "fake-target-key"
    with p._runtime_scope(env, lambda: fault == "cancelled", inherit_process_context=False):
        with pytest.raises(PermissionError if fault == "cancelled" else ValueError):
            p._private_member_model_binding("pi")


def test_thread_capture_preserves_public_profile_and_caller_private_binding(isolated):
    p = isolated.provider
    marker = object()
    env = {"MEYO_USER_ID": "alice", "MEYO_MODEL_WORKSPACE_ID": "profile-a",
           "MEYO_LOCAL_MODEL_CONFIG": "/fixture/profile-a/model.json"}
    def observe():
        return p._runtime_binding()[1], p._bound_personal_token(), p._bound_native_onboard()
    with p._runtime_scope(env, None, inherit_process_context=False,
                         personal_token=TOKEN, native_onboard=marker):
        call = p._bind_runtime_call(observe, _binding=(dict(env), None, False))
    with ThreadPoolExecutor(max_workers=1) as pool:
        values, token, onboard = pool.submit(call).result(timeout=3)
        assert values == env and token == TOKEN and onboard is marker
        assert pool.submit(observe).result(timeout=3)[1:] == ("", None)
    assert p._bound_personal_token() == ""


def test_native_scope_binds_lease_and_closes_after_failure(lease_scene):
    case = lease_scene
    lifecycle = []

    class Server:
        server_port = 12345

        def __init__(self, lease):
            self.lease = lease

        def serve_forever(self, **kwargs):
            pass

        def shutdown(self):
            lifecycle.append("shutdown")

        def server_close(self):
            lifecycle.append("close")

    case.mp.setattr(case.provider, "_NativeOnboardServer", Server)
    with case.provider._runtime_scope(case.context, None, inherit_process_context=False, personal_token=TOKEN):
        with pytest.raises(RuntimeError):
            with case.provider.native_onboard_scope(user_id="alice", session_id="run", owner_session="owner",
                    workdir=str(case.work), capability_policy=case.lease.policy) as lease:
                assert lease is not None
                assert case.provider._bound_native_onboard() is lease
                assert case.provider._bound_native_onboard_environment()["MEYO_ONBOARD_BRIDGE_TOKEN"] == lease._token
                raise RuntimeError("fixture")
        assert case.provider._bound_native_onboard() is None
        assert case.provider._bound_personal_token() == TOKEN
    assert lease.cancelled()
    assert lifecycle == ["shutdown", "close"]
    with pytest.raises(PermissionError):
        lease.environment()


def test_ambient_personal_token_cannot_start_onboard(lease_scene):
    case = lease_scene
    case.mp.setenv("MEYO_PERSONAL_TOKEN", TOKEN)
    case.mp.setenv("MEYO_ONBOARD_BRIDGE_TOKEN", "fixture-untrusted")
    assert case.provider._bound_personal_token() == ""
    assert case.provider._bound_native_onboard_environment() == {}
    with case.provider.native_onboard_scope(user_id="alice", session_id="run", owner_session="owner",
            workdir=str(case.work), capability_policy={"install": True}) as lease:
        assert lease is None


@pytest.mark.parametrize("fault", ["token", "grant", "session-policy", "catalog-link", "cancel"])
def test_lease_rechecks_live_authority_and_redacts_secrets(lease_scene, fault):
    case = lease_scene
    lease = case.lease
    assert lease.environment()["MEYO_ONBOARD_BRIDGE_TOKEN"] != TOKEN
    assert TOKEN not in repr(lease)
    assert lease.redact({TOKEN: [lease._token, TOKEN]}) == {"[redacted]": ["[redacted]", "[redacted]"]}
    if fault == "token":
        case.tokens.write_text("{}")
    elif fault == "grant":
        records = json.loads(case.tokens.read_text())
        records[TOKEN]["project_grants"][0]["source_write"] = False
        case.tokens.write_text(json.dumps(records))
    elif fault == "session-policy":
        case.session["turns"][-1]["input_snapshot"]["capability_policy"]["install"] = False
    elif fault == "catalog-link":
        os.link(case.catalog, case.work / "linked-catalog")
    else:
        lease._closed.set()
    with pytest.raises((PermissionError, ValueError)):
        lease.environment()


def test_install_paths_reject_authority_catalog_and_unowned_storage(lease_scene):
    case = lease_scene
    assert case.lease._install_roots[1] == case.root / "profile-data/repos/external-agents"
    assert case.lease._install_roots[2] == case.root / "profile-data/external_agents/runtimes"
    case.lease.install_paths({"key": "fixture-member"}, case.work / "member", case.work / "runtime")
    for destination in (case.root / "unowned", case.catalog, case.tokens.parent):
        with pytest.raises(PermissionError):
            case.lease.install_paths({"key": "fixture-member"}, destination, case.work / "runtime")
    with pytest.raises(PermissionError):
        case.lease.install_paths({"key": "../escape"}, case.work / "member", case.work / "runtime")


def test_safe_build_uses_private_home_filters_credentials_and_rechecks_wrap(lease_scene):
    case = lease_scene
    host = case.root / "host-bin"
    host.mkdir()
    binary = host / "codex"
    binary.write_text("#!/bin/sh\nexit 97\n")
    binary.chmod(0o755)
    case.lease._host_env["PATH"] = str(host)
    env = {"PATH": "/fixture/build-path", "LANG": "C", "ANTHROPIC_AUTH_TOKEN": TOKEN,
           "MEYO_LOCAL_MODEL_CONFIG": "/fixture/private/model.json", "MEYO_ONBOARD_BRIDGE_TOKEN": "secret"}
    with pytest.raises(PermissionError):
        with case.lease.install_launch(["build"], str(case.work), env, "fixture-member") as launch:
            assert launch.env["HOME"] != os.environ["HOME"]
            assert not any(name in launch.env for name in (
                "ANTHROPIC_AUTH_TOKEN", "MEYO_LOCAL_MODEL_CONFIG", "MEYO_ONBOARD_BRIDGE_TOKEN"))
            assert "sandbox_workspace_write.network_access=false" in launch.wrap(["build"])
            assert "PATH=/fixture/build-path" in launch.wrap(["build"])
            case.session["turns"][-1]["input_snapshot"]["capability_policy"]["execute"] = False
            launch.wrap(["build"])
    assert not launch.config_dir.exists()


@pytest.mark.parametrize("fault", [None, "lease-user", "lease-session", "lease-workdir",
                                  "new-user", "requested-user", "owner-revoked"])
def test_run_tool_borrowed_member_private_binding_and_onboard(lease_scene, fault):
    case, p = lease_scene, lease_scene.provider
    tools = importlib.import_module("legion_tool")
    context = {**case.context, "MEYO_WORKDIR": str(case.work),
               "MEYO_PROJECT_CONTEXT_JSON": json.dumps({
                   "user_id": "alice", "workspace_root": str(case.work)})}
    case.mp.setenv("MEYO_RUNTIME_BRIEF", "process-context-must-not-leak")
    case.mp.setattr(p, "inventory", lambda: {"declarations": [{
        "provider": "codex", "execution_ready": True, "tools": [{"name": "shell"}]}]})
    import legion_sandbox
    case.mp.setattr(legion_sandbox, "workdir", lambda *args: str(case.work))
    core = sys.modules["legion_core"]
    core.is_cancelled = lambda *args: False
    installed, observed = [], []

    def onboard(**kwargs):
        installed.append(kwargs)
        return {"ok": True}

    case.mp.setitem(sys.modules, "harness_onboard", SimpleNamespace(onboard=onboard))

    def run_one(provider, task, **kwargs):
        env = p._runtime_binding()[1]
        observed.append((env, p._bound_personal_token(), p._bound_native_onboard()))
        assert case.registry._INPROC_RUNTIME_ENV.inherit_process_context is False
        assert "MEYO_RUNTIME_BRIEF" not in env
        result = tools.run_tool("runnable:harness_onboard", {"key": "fixture-member"}, timeout_s=2)
        return {"status": "done", "onboard": result}

    core.run_one = run_one
    if fault == "lease-user":
        case.lease.user_id = "bob"
    elif fault == "lease-session":
        case.lease.session_id = "other-run"
    elif fault == "lease-workdir":
        case.lease.workdir = case.root
    elif fault == "owner-revoked":
        case.session["turns"][-1]["input_snapshot"]["capability_policy"]["install"] = False
    params = {"arguments": {}}
    if fault == "requested-user":
        params["user_id"] = "bob"
    with p._runtime_scope(context, None, inherit_process_context=False,
                          personal_token=TOKEN, native_onboard=case.lease):
        if fault == "new-user":
            # A fresh authenticated request must not retain the prior user's lease/token.
            other = {**context, "MEYO_USER_ID": "bob", "MEYO_PROJECT_CONTEXT_JSON": json.dumps({
                "user_id": "bob", "workspace_root": str(case.work)})}
            with p._runtime_scope(other, None, inherit_process_context=False):
                result = tools.run_tool("agent_tool:codex:shell", params, timeout_s=2)
            assert p._bound_personal_token() == TOKEN
            assert p._bound_native_onboard() is case.lease
        else:
            result = tools.run_tool("agent_tool:codex:shell", params, timeout_s=2)
        assert p._bound_personal_token() == TOKEN
        assert p._bound_native_onboard() is case.lease
    assert p._bound_personal_token() == ""
    assert p._bound_native_onboard() is None
    if fault == "requested-user":
        assert not result["ok"] and not observed and not installed
        assert "conflicts with the bound caller" in result["error"]
        return
    assert result["ok"], result
    env, token, lease = observed[0]
    assert env["MEYO_MODEL_WORKSPACE_ID"] == "public-profile-fixture"
    assert token == ("" if fault == "new-user" else TOKEN)
    assert lease is (case.lease if fault in (None, "owner-revoked") else None)
    outcome = result["result"]["onboard"]
    if fault is None:
        assert outcome["ok"], outcome
        assert installed == [{"key": "fixture-member", "uid": "alice", "personal_token": TOKEN}]
    else:
        assert not outcome["ok"] and not installed


def request(case, *, body=None, origin=None, token=None):
    p, lease = case.provider, case.lease
    body = body if body is not None else {"uid": "alice", "session_id": "run", "workdir": str(case.work), "params": {}}
    raw = json.dumps(body).encode()
    handler = object.__new__(p._NativeOnboardHandler)
    handler.path = "/onboard"
    handler.server = SimpleNamespace(lease=lease)
    handler.headers = {"Host": lease.authority, "Authorization": "Bearer " + (token or lease._token),
                       "Content-Length": str(len(raw)), "Content-Type": "application/json"}
    if origin is not None:
        handler.headers["Origin"] = origin
    handler.rfile = io.BytesIO(raw)
    replies = []
    handler._reply = lambda code, data: replies.append((code, data))
    handler.do_POST()
    return replies[0]


@pytest.mark.parametrize("fault,code", [("origin", 403), ("token", 401), ("user", 400)])
def test_bridge_rejects_cross_origin_credential_or_identity_before_dispatch(lease_scene, fault, code):
    case = lease_scene
    case.mp.setitem(sys.modules, "legion_tool", SimpleNamespace(
        run_tool=lambda *args: pytest.fail("must reject before dispatch")))
    kwargs = {"origin": "https://untrusted.invalid"} if fault == "origin" else {"token": "wrong"}
    if fault == "user":
        kwargs = {"body": {"uid": "bob", "session_id": "run", "workdir": str(case.work), "params": {}}}
    assert request(case, **kwargs)[0] == code


def test_bridge_dispatch_uses_private_identity_and_public_profile(lease_scene):
    case = lease_scene
    def run_tool(name, params):
        assert name == "runnable:harness_onboard"
        values = case.provider._runtime_binding()[1]
        assert values["MEYO_MODEL_WORKSPACE_ID"] == "public-profile-fixture"
        assert values["MEYO_PERSONAL_STATE_DIR"] == case.context["MEYO_PERSONAL_STATE_DIR"]
        assert case.provider._bound_personal_token() == TOKEN
        assert case.provider._bound_native_onboard() is case.lease
        return {"ok": True}
    case.mp.setitem(sys.modules, "legion_tool", SimpleNamespace(run_tool=run_tool))
    assert request(case) == (200, {"ok": True})
    assert case.provider._bound_personal_token() == ""
