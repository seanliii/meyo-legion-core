"""CPU-only HTTP context regression for the personal server boundary."""
from __future__ import annotations

import ast
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading

import pytest

SERVICE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SERVICE), str(SERVICE.parents[1] / "harness_baselines/meyo-orchestrator")]
TOKENS = {"alice": "__ut_http_alice_0123456789", "bob": "__ut_http_bob_0123456789"}


@pytest.fixture(autouse=True)
def endpoint(tmp_path, monkeypatch):
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_SESSIONS_DIR",
                "LEGION_TEAM_DIR", "LEGION_TASK_MEMORY_DIR", "LEGION_PROC_REGISTRY_DIR"):
        directory = tmp_path / key
        directory.mkdir()
        monkeypatch.setenv(key, str(directory))
    for key in ("MEYO_USER_ID", "MEYO_SESSION_ID", "MEYO_PROJECT_CONTEXT_JSON",
                "MEYO_CAPABILITY_POLICY_JSON", "LEGION_PERSONAL_STATE_DIR",
                "MEYO_PERSONAL_STATE_DIR", "LEGION_PERSONAL_OWNER", "LEGION_PUBLIC_ORIGIN"):
        monkeypatch.delenv(key, raising=False)
    tokens = tmp_path / "personal-tokens.json"
    tokens.write_text(json.dumps({token: {"user": user} for user, token in TOKENS.items()}))
    tokens.chmod(0o600)
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tokens))
    import server
    import sessions
    import agent_tool_provider as provider
    monkeypatch.setattr(server, "_PUBLIC_ORIGIN", None)
    monkeypatch.setattr(server, "_REMOTE_BIND_OK", True)
    monkeypatch.setattr(server, "_LEGACY_NO_ORIGIN", False)
    captures = []

    def receive(tool, params):
        _, env, _ = provider._runtime_binding()
        captures.append((tool, dict(params), dict(env), provider._bound_personal_token()))
        return {"ok": True}

    original_tool = server.LEGION_TOOL.run_tool
    monkeypatch.setattr(server.LEGION_TOOL, "run_tool", receive)
    for user in TOKENS:
        sessions.save_turn(user + "-existing", "original", {"results": [], "status": "done"},
                           user_id=user, input_snapshot={"capability_policy": {"install": False}})

    class Handler(server.Handler):
        def log_message(self, *_args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.api_token = "__ut_instance_not_personal"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield {"port": httpd.server_port, "captures": captures, "run_tool": original_tool}
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=3)


def call(endpoint, *, path="/api/legion/tool", user="alice", **fields):
    body = {"tool": "__ut_stateless", "params": {}, **fields}
    client = http.client.HTTPConnection("127.0.0.1", endpoint["port"], timeout=5)
    try:
        client.request("POST", path, json.dumps(body), {
            "Content-Type": "application/json", "Authorization": "Bearer " + TOKENS[user],
            "Origin": f"http://127.0.0.1:{endpoint['port']}",
        })
        response = client.getresponse()
        return response.status, json.loads(response.read())
    finally:
        client.close()


def test_tool_owner_and_private_token_do_not_inherit_process_state(endpoint, monkeypatch):
    monkeypatch.setenv("MEYO_USER_ID", "stale-owner")
    monkeypatch.setenv("MEYO_SESSION_ID", "stale-session")
    for user in TOKENS:
        status, result = call(endpoint, user=user)
        assert status == 200 and result["ok"]
        _, params, context, token = endpoint["captures"][-1]
        assert context == {"MEYO_USER_ID": user}
        assert token == TOKENS[user]
        assert token not in json.dumps([params, context, result])
    assert os.environ["MEYO_USER_ID"] == "stale-owner"


@pytest.mark.parametrize("fields,code", [
    ({"uid": "bob"}, 403), ({"session_id": "bob-existing"}, 403),
    ({"session_id": "unknown"}, 404), ({"params": {"personal_token": TOKENS["bob"]}}, 400),
])
def test_tool_rejects_untrusted_identity_before_execution(endpoint, fields, code):
    assert call(endpoint, **fields)[0] == code
    assert endpoint["captures"] == []


def test_onboard_policy_uses_latest_persisted_parent_not_request(endpoint):
    status, result = call(endpoint, tool="runnable:harness_onboard", session_id="alice-existing",
                          capability_policy={"install": True},
                          params={"_capability_policy": {"install": True}})
    assert status == 200 and result["ok"]
    _, params, context, _ = endpoint["captures"][-1]
    assert params["_capability_policy"]["install"] is False
    assert json.loads(context["MEYO_CAPABILITY_POLICY_JSON"])["install"] is False


def test_onboard_without_parent_cannot_elevate_default_policy(endpoint, monkeypatch):
    import legion_permissions
    monkeypatch.setattr(legion_permissions, "capability_policy", lambda: {"install": False})
    assert call(endpoint, tool="runnable:harness_onboard", capability_policy={"install": True})[0] == 200
    assert endpoint["captures"][-1][1]["_capability_policy"]["install"] is False


@pytest.mark.parametrize("route,method", [
    ("dispatch", "_handle_dispatch"), ("run_one", "_handle_run_one"),
])
def test_outer_post_scope_is_captured_before_dispatch_starts_thread(endpoint, monkeypatch, route, method):
    import server
    import agent_tool_provider as provider
    observed = []

    def dispatch(handler, payload):
        def initial_worker():
            observed.append((provider._bound_personal_token(), provider._runtime_binding()[1],
                             provider._bound_native_onboard()))
            child = threading.Thread(target=provider._bind_runtime_call(
                lambda: observed.append((provider._bound_personal_token(),
                                         provider._runtime_binding()[1],
                                         provider._bound_native_onboard()))))
            child.start()
            child.join(timeout=2)
        worker = threading.Thread(target=provider._bind_runtime_call(initial_worker))
        worker.start()
        worker.join(timeout=2)
        handler._send_json({"ok": True})

    monkeypatch.setattr(server.Handler, method, dispatch)
    for user in TOKENS:
        assert call(endpoint, path="/api/legion/" + route, user=user)[0] == 200
    assert observed == [(TOKENS[user], {}, None) for user in TOKENS for _ in range(2)]
    assert provider._bound_personal_token() == ""


@pytest.mark.parametrize("grant_state", ["live", "revoked", "child_root"])
def test_host_onboard_reuses_live_lease_without_exposing_credential(
        endpoint, tmp_path, monkeypatch, grant_state):
    import sessions
    import external_agent_registry as registry
    import harness_onboard
    import server
    import legion_tool
    source = tmp_path / "member-source"
    source.mkdir()
    catalog = source / "external_agents.json"
    catalog.write_text(json.dumps({"agents": []}))
    monkeypatch.setattr(registry, "CATALOG", catalog)
    store = Path(os.environ["LEGION_PERSONAL_TOKENS_FILE"])
    records = json.loads(store.read_text())
    records[TOKENS["alice"]]["project_grants"] = [
        {"root": str(source), "export_paths": [], "source_write": True}]
    store.write_text(json.dumps(records))
    independent = tmp_path / "independent-tmp"
    independent.mkdir()
    monkeypatch.setenv("TMPDIR", str(independent))
    sid = "alice-onboard"
    sessions.save_turn(sid, "install", {"results": [], "status": "done"}, user_id="alice")
    calls = []
    monkeypatch.setattr(harness_onboard, "onboard", lambda **kwargs:
                        calls.append(kwargs) or {"ok": True})
    monkeypatch.setattr(server.LEGION_TOOL, "run_tool", endpoint["run_tool"])
    monkeypatch.setattr(legion_tool, "_record_tool_usage", lambda *args: {})
    requested_root = source
    if grant_state == "revoked":
        records[TOKENS["alice"]]["project_grants"] = []
        store.write_text(json.dumps(records))
    elif grant_state == "child_root":
        requested_root = source / "child"
        requested_root.mkdir()
    body = {"uid": "alice", "session_id": sid, "workdir": str(requested_root),
            "params": {"key": "__ut_onboard"}}
    client = http.client.HTTPConnection("127.0.0.1", endpoint["port"], timeout=5)
    try:
        client.request("POST", "/api/legion/tool/onboard", json.dumps(body), {
            "Content-Type": "application/json", "Authorization": "Bearer " + TOKENS["alice"],
            "Origin": f"http://127.0.0.1:{endpoint['port']}",
        })
        response = client.getresponse()
        result = json.loads(response.read())
        if grant_state != "live":
            assert response.status == 403 and result["error"] == "source_write_not_granted", result
            assert calls == []
            return
        assert response.status == 200 and result["ok"], result
    finally:
        client.close()
    assert len(calls) == 1 and calls[0]["uid"] == "alice"
    assert calls[0]["personal_token"] == TOKENS["alice"]
    assert TOKENS["alice"] not in json.dumps(result)


def test_existing_dispatch_and_planning_threads_bind_runtime():
    tree = ast.parse((SERVICE / "server.py").read_text())
    targets = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Thread":
            target = next((kw.value for kw in node.keywords if kw.arg == "target"), None)
            if isinstance(target, ast.Call) and isinstance(target.func, ast.Attribute):
                if target.func.attr == "_bind_runtime_call":
                    targets.append(target.args[0].id)
    assert {"_worker", "_plan_worker", "work"} <= set(targets)


def test_personal_bootstrap_passes_host_references_not_secret_or_task_grants():
    import personal_runtime
    assert {"LEGION_EXTERNAL_CATALOG_PATH", "LEGION_BILLD_SELF_HOSTED_ORIGIN"} <= personal_runtime._PASSTHROUGH
    assert not {"LEGION_PERSONAL_TOKEN", "MEYO_CAPABILITY_POLICY_JSON",
                "MEYO_ONBOARD_BRIDGE_TOKEN"} & personal_runtime._PASSTHROUGH
