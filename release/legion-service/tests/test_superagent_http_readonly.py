"""Real authenticated HTTP -> tool worker -> local observation, using only temp instances.

The Legion handler, authentication, context binding and capability runner are real.
Only the Superagent management API is a loopback fixture; no installed Host is used.
"""
from __future__ import annotations

import http.client
import importlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from test_external_capability_http_readonly import configure, endpoint
from test_public_origin_entry import isolated


SERVICE = Path(__file__).resolve().parents[1]
OWNER = "invited-test"
OTHER = "other-invited-user"
OTHER_TOKEN = "unit-other-personal-token-not-a-real-credential"
PUBLIC = "https://legion.example.invalid"
TOOL_PREFIX = "external_capability:dsh-superagent:"


@pytest.fixture
def readonly_http(isolated, endpoint):
    patch = isolated.monkeypatch
    runtime = isolated.runtime
    environment, _ = runtime.environment(isolated.root, {"PATH": os.environ["PATH"]})
    for key, value in environment.items():
        patch.setenv(key, value)
    patch.setenv("PYTHONNOUSERSITE", "1")
    server = importlib.import_module("server")
    patch.setattr(server, "_PUBLIC_ORIGIN", None)
    patch.setattr(server, "_REMOTE_BIND_OK", False)
    patch.setattr(server, "_LEGACY_NO_ORIGIN", False)
    registry = importlib.import_module("external_agent_registry")
    patch.setattr(registry, "CATALOG", SERVICE / "external_agents.json")
    patch.delenv("LEGION_EXTERNAL_CATALOG_PATH", raising=False)

    # Both credentials are generated/stored only in this temporary test instance.
    token = (isolated.root / "login-token.txt").read_text().strip()
    tokens_file = isolated.root / "secrets/personal-tokens.json"
    tokens = json.loads(tokens_file.read_text())
    tokens[OTHER_TOKEN] = {"user": OTHER}
    runtime._write(tokens_file, runtime._json_bytes(tokens))
    private_file = configure(endpoint["url"], key="dsh-superagent",
                             root=environment["MEYO_LEGION_DIR"])
    private_before = private_file.read_bytes()
    endpoint["payload"] = {
        "paused": True,
        "load": {"running": 0, "queued": 0, "maxWorkers": 3},
        "private": "DO_NOT_RETURN_PRIVATE_HTTP_FIELD",
    }
    listener = server.LegionServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=listener.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()

    def request(action="status", *, credential=token, body=None, host=None, origin=None):
        host = host or f"127.0.0.1:{listener.server_port}"
        headers = {"Host": host, "Origin": origin or "http://" + host,
                   "Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = "Bearer " + credential
        payload = {"tool": TOOL_PREFIX + action, "params": {}, **(body or {})}
        connection = http.client.HTTPConnection("127.0.0.1", listener.server_port, timeout=5)
        try:
            connection.request("POST", "/api/legion/tool",
                               body=json.dumps(payload), headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        yield SimpleNamespace(request=request, endpoint=endpoint, root=isolated.root,
                              runtime=runtime, patch=patch, server=server,
                              environment=environment, listener=listener)
    finally:
        listener.shutdown()
        listener.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert private_file.read_bytes() == private_before
        assert private_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("action,path,payload,expected", [
    ("status", "/orchestrator/api/state",
     {"paused": True, "load": {"running": 0, "queued": 0, "maxWorkers": 3},
      "token": "DO_NOT_RETURN_PRIVATE_HTTP_FIELD"},
     {"paused": True, "load": {"running": 0, "queued": 0, "maxWorkers": 3}}),
    ("settings", "/orchestrator/api/settings",
     {"blocks": {"executor": {"revision": 2, "value": {
         "maxWorkers": 3, "prepare": "DO_NOT_RETURN_PRIVATE_HTTP_FIELD"}}}},
     {"blocks": {"executor": {"revision": 2, "value": {"maxWorkers": 3}}}}),
    ("sources", "/orchestrator/api/sources",
     [{"id": "github", "enabled": False, "state": "off",
       "token": "DO_NOT_RETURN_PRIVATE_HTTP_FIELD"}],
     [{"id": "github", "enabled": False, "state": "off"}]),
])
def test_authenticated_owner_http_reaches_exact_native_readonly_route(
    readonly_http, action, path, payload, expected,
):
    scene = readonly_http
    scene.endpoint["payload"] = payload
    status, result = scene.request(action)
    assert status == 200 and result["ok"] is True, result
    assert result["data"] == expected
    assert result["data_view"] == "redacted_observation"
    assert "DO_NOT_RETURN_PRIVATE_HTTP_FIELD" not in json.dumps(result)
    request, = scene.endpoint["requests"]
    assert request["method"] == "GET" and request["path"] == path
    assert not {"authorization", "cookie"} & {key.lower() for key in request["headers"]}


def test_other_authenticated_user_cannot_borrow_owner_namespace_between_owner_calls(readonly_http):
    scene = readonly_http
    status, result = scene.request()
    assert status == 200 and result["ok"] is True, result
    scene.endpoint["requests"].clear()
    status, result = scene.request(credential=OTHER_TOKEN)
    assert status == 200 and result.get("code") == "instance_unconfigured", result
    assert scene.endpoint["requests"] == []
    status, result = scene.request()
    assert status == 200 and result["ok"] is True, result
    assert len(scene.endpoint["requests"]) == 1


def test_anonymous_http_call_does_not_reach_the_instance(readonly_http):
    status, result = readonly_http.request(credential=None)
    assert status in {401, 403} and result.get("error"), result
    assert readonly_http.endpoint["requests"] == []


def test_forged_body_owner_is_rejected_before_private_binding(readonly_http):
    status, result = readonly_http.request(credential=OTHER_TOKEN, body={"uid": OWNER})
    assert status == 403 and result.get("error") == "personal_owner_mismatch", result
    assert readonly_http.endpoint["requests"] == []


def test_body_environment_cannot_retarget_the_authenticated_owner_instance(readonly_http):
    scene = readonly_http
    status, result = scene.request(body={
        "MEYO_LEGION_DIR": str(scene.root / "wrong-namespace"),
        "MEYO_PERSONAL_STATE_DIR": str(scene.root / "wrong-instance"),
        "env": {"MEYO_LEGION_DIR": str(scene.root / "wrong-namespace")},
    })
    assert status == 200 and result["ok"] is True, result
    assert len(scene.endpoint["requests"]) == 1


@pytest.mark.parametrize("params", [
    {"env": {"MEYO_LEGION_DIR": "/unit/must-not-read"}},
    {"url": "http://127.0.0.1:1"},
    {"headers": {"Authorization": "unit-must-not-forward"}},
])
def test_http_action_parameters_cannot_supply_namespace_or_credentials(readonly_http, params):
    status, result = readonly_http.request(body={"params": params})
    assert status == 200 and result.get("code") == "readonly_parameters_not_allowed", result
    assert readonly_http.endpoint["requests"] == []


@pytest.mark.parametrize("field,value", [
    ("MEYO_PERSONAL_STATE_DIR", None),
    ("LEGION_PERSONAL_OWNER", OTHER),
    ("MEYO_LEGION_DIR", "/unit/wrong-data"),
    ("LEGION_HOME_DIR", "/unit/wrong-home"),
    ("LEGION_ALLOW_REMOTE_BIND", "1"),
    ("LEGION_PUBLIC_ORIGIN", PUBLIC),
])
def test_incomplete_or_mismatched_runtime_mapping_is_not_a_private_grant(
    readonly_http, field, value,
):
    scene = readonly_http
    if value is None:
        scene.patch.delenv(field, raising=False)
    else:
        scene.patch.setenv(field, value)
    status, result = scene.request()
    assert status == 200 and result.get("code") == "instance_unconfigured", result
    assert scene.endpoint["requests"] == []


@pytest.mark.parametrize("kind", ["wrong-owner", "public-mode", "invalid-json", "symlink"])
def test_invalid_persisted_instance_cannot_lend_its_namespace(readonly_http, kind):
    scene = readonly_http
    path = scene.root / "config.json"
    if kind == "wrong-owner":
        config = json.loads(path.read_text())
        config["user"] = OTHER
        path.write_text(json.dumps(config))
    elif kind == "public-mode":
        path.chmod(0o644)
    elif kind == "invalid-json":
        path.write_text("{")
    else:
        saved = path.with_suffix(".saved")
        path.rename(saved)
        path.symlink_to(saved)
    status, result = scene.request()
    assert status == 200 and result.get("code") == "instance_unconfigured", result
    assert scene.endpoint["requests"] == []


def test_public_proxy_instance_does_not_implicitly_get_local_owner_observations(readonly_http):
    scene = readonly_http
    scene.runtime.configure_public_origin(scene.root, PUBLIC)
    scene.patch.setattr(scene.server, "_PUBLIC_ORIGIN", PUBLIC)
    scene.patch.setenv("LEGION_PUBLIC_ORIGIN", PUBLIC)
    status, result = scene.request(host="legion.example.invalid", origin=PUBLIC)
    assert status == 200 and result.get("code") == "instance_unconfigured", result
    assert scene.endpoint["requests"] == []


def test_saved_public_mode_cannot_be_downgraded_by_missing_process_flag(readonly_http):
    scene = readonly_http
    scene.runtime.configure_public_origin(scene.root, PUBLIC)
    status, result = scene.request()
    assert status == 200 and result.get("code") == "instance_unconfigured", result
    assert scene.endpoint["requests"] == []


def test_readonly_binding_does_not_reload_login_or_model_credentials(readonly_http):
    scene = readonly_http
    private = scene.runtime._private

    def configuration_only(path, **kwargs):
        assert Path(path) == scene.root / "config.json", "read-only binding tried to read credentials"
        return private(path, **kwargs)

    def no_environment_reload(*args, **kwargs):
        pytest.fail("HTTP read must not load the personal token/model environment again")

    scene.patch.setattr(scene.runtime, "_private", configuration_only)
    scene.patch.setattr(scene.runtime, "environment", no_environment_reload)
    status, result = scene.request()
    assert status == 200 and result["ok"] is True, result
    assert len(scene.endpoint["requests"]) == 1
