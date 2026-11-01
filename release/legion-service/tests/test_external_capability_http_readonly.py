"""Read-only local capability calls use explicit caller-owned instance configuration."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

SERVICE = Path(__file__).resolve().parents[1]
KEY = "__ut_readonly_http"
ROUTES = {
    "status": "/orchestrator/api/state",
    "settings": "/orchestrator/api/settings",
    "sources": "/orchestrator/api/sources",
}


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    for name, directory in {
        "HOME": "home", "MEYO_LEGION_DIR": "state", "LEGION_HOME_DIR": "legion",
        "LEGION_PROC_REGISTRY_DIR": "processes",
    }.items():
        monkeypatch.setenv(name, str(tmp_path / directory))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.syspath_prepend(str(SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"))
    import external_agent_registry as registry
    monkeypatch.setattr(registry, "CATALOG", SERVICE / "external_agents.json")


def actions():
    return {
        action: {
            "description": f"Read native {action}; never start or configure it.",
            "params": [],
            "timeout_s": 5,
            "transport": {
                "type": "http", "method": "GET", "read_only_loopback": True,
                "url": "{instance_url}" + route,
                "headers": {"x-orchestrator-request": "1"},
            },
        }
        for action, route in ROUTES.items()
    }


@pytest.fixture
def capability(tmp_path, monkeypatch):
    import external_agent_registry as registry
    item = json.loads((SERVICE / "external_adapters/dsh-superagent.spec.json").read_text())
    item.update(key=KEY, capability_actions=actions())
    catalog = tmp_path / "catalog.json"

    def save():
        catalog.write_text(json.dumps({"agents": [item]}))

    save()
    monkeypatch.setattr(registry, "CATALOG", catalog)
    monkeypatch.setattr(
        registry, "_probe_one",
        lambda *args, **kwargs: pytest.fail("reading an existing API must not probe/install a harness"),
    )
    return registry, item, save


def configure(url, *, key=KEY, root=None):
    root = Path(root or os.environ["MEYO_LEGION_DIR"])
    file = root / "external_agents/instances.json"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps({key: {"url": url}}))
    file.chmod(0o600)
    return file


@pytest.fixture
def endpoint():
    state = {"requests": [], "mode": "json", "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append({
                "method": self.command, "path": self.path, "headers": dict(self.headers),
            })
            if state["mode"] == "slow":
                time.sleep(0.2)
            self.send_response(state["status"])
            if state["mode"] == "redirect":
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/must-not-read")
            self.send_header("Content-Type", "text/html" if state["mode"] == "html" else "application/json")
            self.end_headers()
            if state["mode"] == "oversize":
                body = b'{"value":"' + b"x" * 2_100_000 + b'"}'
            elif state["mode"] == "html":
                body = b"<html>sign in</html>"
            else:
                body = json.dumps(state.get("payload", {"native_path": self.path, "fixture": True})).encode()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            state["requests"].append({"method": self.command, "path": self.path})
            self.send_error(405)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_superagent_catalog_exposes_three_actions_without_becoming_an_answer_agent():
    import external_agent_registry as registry
    import legion_core
    import toolbox
    catalog = registry.load_catalog()
    expected = {f"external_capability:dsh-superagent:{name}" for name in ROUTES}
    assert {row["id"] for row in registry.capability_actions("dsh-superagent")["actions"]} == expected
    rows = [row for row in toolbox._scan_external_capabilities(catalog=catalog) if row["id"] in expected]
    assert {row["id"] for row in rows} == expected
    assert all(row["runnable"] and row["installed"] is None and row["health"] == "unknown" for row in rows)
    assert "dsh-superagent" not in legion_core._answer_keys_from_catalog(catalog)


def test_missing_instance_does_not_probe_install_or_guess_an_address(capability):
    registry, _, _ = capability
    result = registry.run_capability(KEY, "status", {})
    assert result["ok"] is False
    assert result.get("status") == "unconfigured", result
    assert result.get("code") == "instance_unconfigured", result


@pytest.mark.parametrize("action,path", list(ROUTES.items()))
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_reads_exact_native_route_with_same_origin_and_no_ambient_credentials(
    capability, endpoint, monkeypatch, action, path, host,
):
    registry, _, _ = capability
    url = endpoint["url"].replace("127.0.0.1", host)
    configure(url)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("GH_TOKEN", "unit-value-must-not-forward")
    monkeypatch.setenv("OPENAI_API_KEY", "another-unit-value-must-not-forward")
    result = registry.run_capability(KEY, action, {})
    assert result["ok"] is True
    assert result["data"] == {"native_path": path, "fixture": True}
    request, = endpoint["requests"]
    headers = {key.lower(): value for key, value in request["headers"].items()}
    assert request["method"] == "GET"
    assert request["path"] == path
    assert headers["host"] == url.removeprefix("http://")
    assert headers["origin"] == url
    assert headers["x-orchestrator-request"] == "1"
    assert not {"authorization", "proxy-authorization", "cookie", "content-length"} & headers.keys()
    assert "must-not-forward" not in json.dumps(result)


@pytest.mark.parametrize("url", [
    "http://example.invalid:6187", "http://127.0.0.2:6187", "http://127.1:6187",
    "http://2130706433:6187", "http://localhost.example.invalid:6187",
    "http://localhost.:6187", "http://[::1]:6187", "https://localhost:6187",
    "http://user:private-value@localhost:6187", "http://localhost:6187/?token=private-value",
    "http://localhost:6187/#private-value", "http://localhost:6187/orchestrator/api",
    "http://localhost", "http://localhost:0", "http://localhost:65536",
    "http://localhost:6187\nX-Secret: private-value",
])
def test_rejects_non_exact_or_credential_bearing_instance_origins(capability, url):
    registry, _, _ = capability
    configure(url)
    result = registry.run_capability(KEY, "status", {})
    assert result["ok"] is False
    assert result.get("code") == "invalid_instance_url", result
    assert "private-value" not in json.dumps(result)


def test_bound_instance_never_borrows_parent_configuration(capability, endpoint, tmp_path):
    registry, _, _ = capability
    configure(endpoint["url"])
    result = registry.run_capability(
        KEY, "status", {}, env={"MEYO_LEGION_DIR": str(tmp_path / "different-owner")},
        isolated_environment=True,
    )
    assert result.get("code") == "instance_unconfigured", result
    empty = registry.run_capability(KEY, "status", {}, env={}, isolated_environment=True)
    assert empty.get("code") == "instance_unconfigured", empty
    assert endpoint["requests"] == []


def test_unconfigured_caller_does_not_use_home_or_legacy_module_state(capability, endpoint, monkeypatch):
    registry, _, _ = capability
    configure(endpoint["url"])
    monkeypatch.delenv("MEYO_LEGION_DIR")
    result = registry.run_capability(KEY, "status", {})
    assert result.get("code") == "instance_unconfigured", result
    assert endpoint["requests"] == []


@pytest.mark.parametrize("kind", ["public-mode", "symlink", "parent-symlink", "invalid-json", "credential-field"])
def test_private_config_is_not_a_credential_or_cross_profile_transport(capability, endpoint, kind):
    registry, _, _ = capability
    file = configure(endpoint["url"])
    if kind == "public-mode":
        file.chmod(0o644)
    elif kind == "symlink":
        original = file.with_suffix(".private")
        file.rename(original)
        file.symlink_to(original)
    elif kind == "parent-symlink":
        original = file.parent.with_name("different-profile")
        file.parent.rename(original)
        file.parent.symlink_to(original, target_is_directory=True)
    elif kind == "invalid-json":
        file.write_text("{")
    else:
        file.write_text(json.dumps({KEY: {"url": endpoint["url"], "token": "private-value"}}))
    result = registry.run_capability(KEY, "status", {})
    assert result["ok"] is False
    assert result.get("code") == "instance_config_invalid", result
    assert "private-value" not in json.dumps(result)
    assert endpoint["requests"] == []


@pytest.mark.parametrize("override", [
    {"url": "http://localhost:1"}, {"method": "POST"}, {"headers": {"Authorization": "private-value"}},
])
def test_task_arguments_cannot_override_the_private_endpoint_or_method(capability, endpoint, override):
    registry, _, _ = capability
    configure(endpoint["url"])
    result = registry.run_capability(KEY, "status", override)
    assert result["ok"] is False
    assert result.get("code") == "readonly_parameters_not_allowed", result
    assert "private-value" not in json.dumps(result)
    assert endpoint["requests"] == []


@pytest.mark.parametrize("field,value", [
    ("method", "POST"), ("read_only_loopback", False),
    ("headers", {"Authorization": "private-value"}),
    ("headers", {"Origin": "http://localhost:1"}),
    ("url", "http://localhost:1/orchestrator/api/state"),
])
def test_malformed_readonly_contract_cannot_turn_into_another_http_action(
    capability, endpoint, field, value,
):
    registry, item, save = capability
    configure(endpoint["url"])
    item["capability_actions"]["status"]["transport"][field] = value
    save()
    result = registry.run_capability(KEY, "status", {})
    assert result["ok"] is False
    assert result.get("code") == "invalid_readonly_http_contract", result
    assert "private-value" not in json.dumps(result)
    assert endpoint["requests"] == []


def test_redirect_is_reported_without_following_even_on_same_host(capability, endpoint):
    registry, _, _ = capability
    configure(endpoint["url"])
    endpoint.update(mode="redirect", status=302)
    result = registry.run_capability(KEY, "status", {})
    assert result["ok"] is False
    assert result.get("http_status") == 302, result
    assert len(endpoint["requests"]) == 1


@pytest.mark.parametrize("status", [401, 403, 503])
def test_http_failures_remain_http_failures_not_setup_success(capability, endpoint, status):
    registry, _, _ = capability
    configure(endpoint["url"])
    endpoint["status"] = status
    result = registry.run_capability(KEY, "status", {})
    assert result["ok"] is False
    assert result.get("http_status") == status, result
    assert "data" not in result


@pytest.mark.parametrize("mode,code", [
    ("html", "invalid_json_response"), ("oversize", "response_too_large"),
])
def test_no_login_html_or_truncated_body_is_reported_as_native_data(capability, endpoint, mode, code):
    registry, _, _ = capability
    configure(endpoint["url"])
    endpoint["mode"] = mode
    result = registry.run_capability(KEY, "status", {})
    assert result["ok"] is False and result.get("code") == code, result
    assert "data" not in result


def test_read_is_bounded_and_does_not_retry(capability, endpoint):
    registry, _, _ = capability
    configure(endpoint["url"])
    endpoint["mode"] = "slow"
    result = registry.run_capability(KEY, "status", {}, timeout_s=0.03)
    assert result["ok"] is False and result.get("status") == "timeout", result
    assert len(endpoint["requests"]) == 1


def test_empty_runtime_scope_does_not_borrow_the_process_instance(capability, endpoint):
    import agent_tool_provider
    import legion_tool
    configure(endpoint["url"])
    with agent_tool_provider._runtime_scope({}, None, inherit_process_context=False):
        result = legion_tool.run_tool(f"external_capability:{KEY}:status", {})
    assert result.get("code") == "instance_unconfigured", result
    assert endpoint["requests"] == []


@pytest.mark.parametrize("configured", [True, False])
def test_native_host_preserves_only_its_explicit_instance_namespace(
    capability, endpoint, tmp_path, configured,
):
    import native_capability_host
    configure(endpoint["url"])
    caller_root = tmp_path / "bound-state"
    if configured:
        configure(endpoint["url"], root=caller_root)
    context = {
        "user_id": "unit-owner", "session_id": "unit-capability-session",
        "workdir": str(tmp_path), "task_facts": {}, "project_context": {},
        "capability_policy": {"execute": True, "publish": False},
        "allowed_ids": [f"external_capability:{KEY}:status"],
    }
    # An in-memory unit parent, not a GitHub identity/Trigger/Mandate or real order.
    session = native_capability_host.CapabilitySession(
        context, environment={"MEYO_LEGION_DIR": str(caller_root)})
    result = session.call_capability({"tool_id": context["allowed_ids"][0], "params": {}})
    if configured:
        assert result["ok"] is True, result
        assert result["data"]["native_path"] == ROUTES["status"]
    else:
        assert result.get("code") == "instance_unconfigured", result
        assert endpoint["requests"] == []


@pytest.mark.parametrize("action", list(ROUTES))
def test_real_superagent_actions_remove_private_native_fields_before_tool_logs(endpoint, action):
    import legion_tool
    secret = "DO_NOT_LOG_TOKEN_63972"
    problem = {"place": "github", "code": "agentCliUnset", "detail": secret,
               "values": {"output": secret, "token": secret}}
    source = {
        "id": "github", "kind": "github", "enabled": False, "installed": False,
        "state": "missing", "revision": 2, "problem": problem,
        "value": {"enabled": False, "config": {
            "repository": "seanliii/meyo-legion-core", "cli": "/private/" + secret,
            "pollSeconds": 60, "token": secret, "secret": secret,
        }},
        "health": [{"name": "github", "state": "paused", "detail": secret}],
        "stats": {"received": 4, "filtered": 2, "lastAt": ""},
        "filtered": [{"preview": secret}], "dropped": [{"preview": secret}],
        "facts": {"token": secret}, "labels": {"name": secret},
    }
    payloads = {
        "status": {
            "paused": True, "load": {"running": 0, "queued": 0, "maxWorkers": 3},
            "setup": {"problem": problem, "accounts": {"agent": secret},
                      "machine": {"stateDir": "/private/" + secret, "promptMaxChars": 100000}},
            "health": {"runtime": {"state": "paused", "detail": secret, "secret": secret}},
            "token": secret, "session_token": secret,
        },
        "settings": {
            "blocks": {
                "executor": {"revision": 7, "value": {
                    "maxWorkers": 3, "prepare": "TOKEN=" + secret + " run-private-command",
                    "directory": "/private/" + secret, "blockedCommands": [secret],
                    "model": {"api_key": secret}}},
                "policy": {"revision": 1, "value": {"rules": [{"instruction": secret}]}},
                "eval": {"revision": 1, "value": {"mode": "observe", "timeoutSeconds": 60}},
            },
            "sources": [source],
            "environment": {"root": "/private/" + secret, "variables": [{"value": secret}]},
            "access_token": secret,
        },
        "sources": [source],
    }
    endpoint["payload"] = payloads[action]
    configure(endpoint["url"], key="dsh-superagent")
    events = []
    result = legion_tool.run_tool(f"external_capability:dsh-superagent:{action}", {}, event_cb=events.append)
    assert result["ok"] is True, result
    assert secret not in json.dumps({"result": result, "events": events}), "native private data escaped into tool output/log events"
    assert result.get("data_view") == "redacted_observation"
    data = result["data"]
    if action == "status":
        assert data["setup"]["problem"] == {"place": "github", "code": "agentCliUnset"}
        assert data["paused"] is True and data["load"]["maxWorkers"] == 3
        assert data["health"]["runtime"]["state"] == "paused"
    elif action == "settings":
        assert data["blocks"]["executor"] == {"revision": 7, "value": {"maxWorkers": 3}}
        assert data["sources"][0]["value"]["config"]["repository"] == "seanliii/meyo-legion-core"
    else:
        assert data[0]["id"] == "github" and data[0]["stats"]["received"] == 4
        assert "filtered" not in data[0] and "dropped" not in data[0]


def test_standalone_cli_reads_without_loading_an_orchestrator_or_another_profile(endpoint, tmp_path):
    configure(endpoint["url"], key="dsh-superagent")
    endpoint["payload"] = {"paused": True, "load": {"running": 0, "queued": 0, "maxWorkers": 3}}
    env = {
        "PATH": os.environ["PATH"], "HOME": str(tmp_path / "cli-home"),
        "MEYO_LEGION_DIR": os.environ["MEYO_LEGION_DIR"],
        "LEGION_HOME_DIR": str(tmp_path / "cli-legion"), "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    process = subprocess.run(
        [sys.executable, "-B", str(SERVICE / "legion_tool.py"), "run",
         "external_capability:dsh-superagent:status", "{}"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert process.returncode == 0, process.stdout
    assert json.loads(process.stdout)["data"]["paused"] is True


def test_existing_command_capability_still_executes(capability, tmp_path, monkeypatch):
    registry, item, save = capability
    item.update(local_source=str(tmp_path), capability_actions={
        "version": {"command": [sys.executable, "-c", "print('legacy-command-ok')"], "params": []},
    })
    save()
    monkeypatch.setattr(registry, "_probe_one", lambda *args, **kwargs: {"available": True})
    result = registry.run_capability(
        KEY, "version", {}, env={"PATH": os.environ["PATH"]},
        workdir=str(tmp_path), isolated_environment=True,
    )
    assert result["ok"] is True
    assert result["stdout"].strip() == "legacy-command-ok"
