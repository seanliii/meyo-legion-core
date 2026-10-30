"""One explicit HTTPS proxy origin; real local HTTP, no model or device startup."""
from __future__ import annotations

import http.client
import importlib
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
PUBLIC = "https://legion.example.invalid"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    for key, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_SESSIONS_DIR", "sessions"),
        ("LEGION_TASK_MEMORY_DIR", "memory"), ("LEGION_TEAM_DIR", "teams"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        path = tmp_path / leaf
        path.mkdir()
        monkeypatch.setenv(key, str(path))
    monkeypatch.delenv("LEGION_PUBLIC_ORIGIN", raising=False)
    monkeypatch.setenv("LEGION_ALLOW_REMOTE_BIND", "0")
    monkeypatch.setenv("LEGION_ALLOW_UNAUTHENTICATED_LOCAL_CLI", "0")
    monkeypatch.setenv("LEGION_EXT_AUTO_UPDATE", "0")
    runtime = importlib.import_module("personal_runtime")
    root = tmp_path / "instance"
    runtime.initialize(root, "invited-test", 18080)
    return SimpleNamespace(runtime=runtime, root=root, monkeypatch=monkeypatch)


def configure(fixture, origin=PUBLIC):
    function = getattr(fixture.runtime, "configure_public_origin", None)
    assert callable(function), "the existing instance has no explicit public HTTPS configuration"
    return function(fixture.root, origin)


def test_opt_in_persists_only_origin_without_remote_bind_or_secret_changes(isolated):
    before = (isolated.root / "login-token.txt").read_bytes()
    result = configure(isolated, "https://LEGION.example.invalid:443/")
    env, config = isolated.runtime.environment(isolated.root, {
        "LEGION_PUBLIC_ORIGIN": "https://attacker.example.invalid",
        "LEGION_ALLOW_REMOTE_BIND": "1",
    })
    assert config["public_origin"] == PUBLIC
    assert env["LEGION_PUBLIC_ORIGIN"] == PUBLIC
    assert env["LEGION_ALLOW_REMOTE_BIND"] == "0"
    assert config["host"] == "127.0.0.1"
    assert env["LEGION_ALLOW_UNAUTHENTICATED_LOCAL_CLI"] == "0"
    assert before == (isolated.root / "login-token.txt").read_bytes()
    assert before.decode().strip() not in repr(result)
    assert result["runtime_verified"] is False


@pytest.mark.parametrize("invalid", [
    "http://legion.example.invalid", "https://user:password@legion.example.invalid",
    "https://*.example.invalid", "https://legion.example.invalid/private",
    "https://legion.example.invalid?secret=value", "https://legion.example.invalid#fragment",
    "https://legion.example.invalid:0", "https://legion.example.invalid:99999",
    "https://legion.example.invalid:bad", " https://legion.example.invalid",
    "https://legion.example.invalid\n", "https://legion.example.invalid\\evil",
    "https://", "https://localhost", "https://127.0.0.1", "https://[::1]",
    "\x01https://legion.example.invalid", "https://legion.example.invalid\x7f",
    "https://[v1.legion.example.invalid]",
])
def test_bad_public_origin_cannot_replace_saved_configuration(isolated, invalid):
    configure(isolated)
    before = (isolated.root / "config.json").read_bytes()
    with pytest.raises(ValueError):
        isolated.runtime.configure_public_origin(isolated.root, invalid)
    assert (isolated.root / "config.json").read_bytes() == before


def test_remove_public_origin_cannot_inherit_it_from_old_shell(isolated):
    configure(isolated)
    old, _ = isolated.runtime.environment(isolated.root, {})
    isolated.runtime.configure_public_origin(isolated.root, None)
    env, config = isolated.runtime.environment(isolated.root, old)
    assert "public_origin" not in config
    assert "LEGION_PUBLIC_ORIGIN" not in env
    assert env["LEGION_ALLOW_REMOTE_BIND"] == "0"


def test_malformed_saved_origin_is_not_silently_ignored(isolated):
    config_path = isolated.root / "config.json"
    config = json.loads(config_path.read_text())
    config["public_origin"] = "https://legion.example.invalid/not-an-origin"
    config_path.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        isolated.runtime.environment(isolated.root, {})


def test_actual_configuration_cli_can_set_and_clear_without_replacing_environment(isolated, capsys):
    env_before = dict(os.environ)
    previous_mask = os.umask(0o077)
    try:
        assert isolated.runtime.main([
            "configure-public-origin", "--state-dir", str(isolated.root), "--origin", PUBLIC,
        ]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["public_origin"] == PUBLIC and result["runtime_verified"] is False
        assert isolated.runtime.main([
            "configure-public-origin", "--state-dir", str(isolated.root), "--clear",
        ]) == 0
        assert json.loads(capsys.readouterr().out)["public_origin"] is None
        assert os.environ == env_before
    finally:
        os.umask(previous_mask)


@pytest.fixture
def http_entry(isolated):
    server = importlib.import_module("server")
    isolated.monkeypatch.setattr(server, "_PUBLIC_ORIGIN", PUBLIC, raising=False)
    isolated.monkeypatch.setattr(server, "_REMOTE_BIND_OK", False)
    isolated.monkeypatch.setattr(server, "_LEGACY_NO_ORIGIN", False)
    isolated.monkeypatch.setenv(
        "LEGION_PERSONAL_TOKENS_FILE", str(isolated.root / "secrets/personal-tokens.json")
    )
    seen_device_subjects = []
    desktop = importlib.import_module("cloud_desktop")
    isolated.monkeypatch.setattr(
        desktop, "ensure_desktop",
        lambda uid: seen_device_subjects.append(uid) or {"ok": True, "device_stub": True},
    )
    listener = server.LegionServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    worker.start()
    token = (isolated.root / "login-token.txt").read_text().strip()

    def request(method, path, *, host="legion.example.invalid", origin=PUBLIC,
                authenticated=True, body=None, extra=None):
        headers = {"Host": host}
        if origin is not None:
            headers["Origin"] = origin
        if authenticated:
            headers["Authorization"] = "Bearer " + token
        duplicate_headers = extra if isinstance(extra, list) else []
        if extra and not duplicate_headers:
            headers.update(extra)
        content = None if body is None else json.dumps(body)
        if content is not None:
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", listener.server_port, timeout=3)
        try:
            if duplicate_headers:
                conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
                for name, value in [*headers.items(), *duplicate_headers]:
                    conn.putheader(name, value)
                if content is not None:
                    conn.putheader("Content-Length", str(len(content.encode())))
                conn.endheaders(content.encode() if content is not None else None)
            else:
                conn.request(method, path, body=content, headers=headers)
            response = conn.getresponse()
            data = response.read()
            decoded = (json.loads(data) if "application/json" in response.getheader("Content-Type", "")
                       else data)
            return response.status, dict(response.getheaders()), decoded if data else {}
        finally:
            conn.close()

    yield SimpleNamespace(server=server, request=request, seen=seen_device_subjects)
    listener.shutdown()
    listener.server_close()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_real_proxy_hop_logs_in_without_downgrading_cookie(http_entry):
    status, headers, body = http_entry.request("POST", "/api/legion/auth/session", body={})
    assert status == 200
    assert body["user_id"] == "invited-test"
    cookie = headers["Set-Cookie"]
    assert "; Secure" in cookie and "; HttpOnly" in cookie and "; SameSite=Strict" in cookie


def test_homepage_is_still_public_at_configured_host(http_entry):
    status, headers, body = http_entry.request("GET", "/", origin=None, authenticated=False)
    assert status == 200 and "text/html" in headers["Content-Type"]
    assert b"desktop_production.js" in body
    assert "Set-Cookie" not in headers


def test_real_get_and_post_still_bind_the_verified_owner(http_entry):
    assert http_entry.request("GET", "/api/legion/auth/status", origin=None)[2]["user_id"] == "invited-test"
    good = http_entry.request("POST", "/api/legion/cloud_desktop/ensure", body={})
    assert good[0] == 200 and http_entry.seen == ["invited-test"]
    forged = http_entry.request("POST", "/api/legion/cloud_desktop/ensure", body={"uid": "default"})
    assert forged[0] == 403 and forged[2]["error"] == "personal_owner_mismatch"
    assert http_entry.seen == ["invited-test"]


@pytest.mark.parametrize("host,origin", [
    ("other.example.invalid", "https://other.example.invalid"),
    ("legion.example.invalid", "https://attacker.example.invalid"),
    ("legion.example.invalid", "http://legion.example.invalid"),
    ("legion.example.invalid", "null"),
    ("legion.example.invalid:444", "https://legion.example.invalid:444"),
    ("legion.example.invalid", "https://legion.example.invalid/path"),
    ("legion.example.invalid", "https://user@legion.example.invalid"),
])
def test_real_proxy_hop_rejects_bad_host_and_origin_before_any_action(http_entry, host, origin):
    for method, path, body in (
        ("GET", "/api/legion/auth/status", None),
        ("POST", "/api/legion/cloud_desktop/ensure", {}),
        ("OPTIONS", "/api/legion/cloud_desktop/ensure", None),
    ):
        response = http_entry.request(method, path, host=host, origin=origin, body=body)
        assert response[0] == 403, (method, response)
    assert http_entry.seen == []


def test_forwarded_headers_do_not_grant_access_or_authentication(http_entry):
    response = http_entry.request(
        "POST", "/api/legion/cloud_desktop/ensure", body={},
        host="not-configured.invalid", origin=None,
        extra={"X-Forwarded-Host": "legion.example.invalid", "X-Forwarded-Proto": "https"},
    )
    assert response[0] == 403 and not http_entry.seen
    assert http_entry.request("GET", "/api/legion/auth/status", authenticated=False)[0] == 401
    assert http_entry.request("POST", "/api/legion/cloud_desktop/ensure",
                              authenticated=False, body={})[0] == 403


def test_public_configuration_never_grants_direct_remote_socket_access(http_entry):
    allows = http_entry.server.request_origin_allowed
    assert not allows("192.0.2.10", "127.0.0.1", "legion.example.invalid", PUBLIC)
    assert not allows("127.0.0.1", "0.0.0.0", "legion.example.invalid", PUBLIC)
    assert allows("127.0.0.1", "127.0.0.1", "127.0.0.1:18080", "http://127.0.0.1:18080")
    assert not allows("127.0.0.1", "127.0.0.1", "127.0.0.1:18080", PUBLIC)


def test_public_mode_uses_secure_cookies_even_for_local_operator(http_entry):
    status, headers, _ = http_entry.request(
        "POST", "/api/legion/auth/session", host="127.0.0.1:18080",
        origin="http://127.0.0.1:18080", body={},
    )
    assert status == 200
    assert "; Secure" in headers["Set-Cookie"]


@pytest.mark.parametrize("name,value", [
    ("Host", "legion.example.invalid"), ("Origin", PUBLIC),
    ("Host", "attacker.example.invalid"), ("Origin", "https://attacker.example.invalid"),
])
def test_duplicate_origin_or_host_is_rejected_in_public_mode(http_entry, name, value):
    response = http_entry.request(
        "GET", "/api/legion/auth/status", extra=[(name, value)],
    )
    assert response[0] == 403


def test_bad_runtime_origin_never_falls_back_to_legacy(isolated, http_entry):
    isolated.monkeypatch.setattr(http_entry.server, "_PUBLIC_ORIGIN", "http://bad.invalid")
    isolated.monkeypatch.setattr(http_entry.server, "_REMOTE_BIND_OK", True)
    response = http_entry.request("POST", "/api/legion/cloud_desktop/ensure", body={})
    assert response[0] == 403 and http_entry.seen == []


@pytest.mark.parametrize("path", [
    "/home-outputs/fixture-not-real.txt",
    "/shared/fixture-session/fixture-share/home-outputs/fixture.txt",
    "/api/legion/events?team_id=fixture&stream=1",
])
def test_download_share_and_event_gets_use_same_front_door(http_entry, path):
    response = http_entry.request("GET", path, host="unconfigured.example.invalid", origin=None)
    assert response[0] == 403


def test_no_public_configuration_preserves_legacy_local_and_remote_modes(isolated, http_entry):
    isolated.monkeypatch.setattr(http_entry.server, "_PUBLIC_ORIGIN", None)
    allow = http_entry.server.request_origin_allowed
    assert allow("127.0.0.1", "127.0.0.1", "localhost:18080", "http://localhost:18080")
    assert not allow("127.0.0.1", "127.0.0.1", "localhost:18080", "http://other.invalid")
    assert not allow("192.0.2.1", "0.0.0.0", "legacy.example.invalid", "https://legacy.example.invalid")
    isolated.monkeypatch.setattr(http_entry.server, "_REMOTE_BIND_OK", True)
    assert allow("192.0.2.1", "0.0.0.0", "legacy.example.invalid", "https://legacy.example.invalid")
    assert not allow("192.0.2.1", "0.0.0.0", "legacy.example.invalid", "https://other.invalid")


def test_origin_control_characters_are_rejected_before_urlsplit(http_entry):
    response = http_entry.request("GET", "/api/legion/auth/status", origin="\x01" + PUBLIC)
    assert response[0] == 403


def test_bracketed_ipvfuture_is_not_an_alias_for_dns_name(isolated, http_entry):
    origin = "https://v1.legion.example.invalid"
    isolated.monkeypatch.setattr(http_entry.server, "_PUBLIC_ORIGIN", origin)
    assert http_entry.request("GET", "/api/legion/auth/status",
                              host="v1.legion.example.invalid", origin=origin)[0] == 200
    assert http_entry.request("GET", "/api/legion/auth/status",
                              host="[v1.legion.example.invalid]", origin=None)[0] == 403
    assert http_entry.request("GET", "/api/legion/auth/status",
                              host="v1.legion.example.invalid",
                              origin="https://[v1.legion.example.invalid]")[0] == 403
