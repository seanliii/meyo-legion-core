"""Paid device plumbing only: isolated files, recorded protocol shapes, no cloud."""
from __future__ import annotations

import base64
import importlib
import json
import os
from pathlib import Path
import socket
import threading
import time

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def mobile(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        path = tmp_path / key
        path.mkdir(mode=0o700)
        monkeypatch.setenv(key, str(path))
    monkeypatch.delenv("MEYO_AGENTBAY_CONFIG", raising=False)
    runtime = importlib.import_module("personal_runtime")
    assert hasattr(runtime, "configure_mobile"), "no personal mobile provider configuration"
    root = tmp_path / "instance"
    runtime.initialize(root, "phone-owner")
    key = tmp_path / "fixture.key"
    key.write_text("akm-offline-test-key-not-real\n")
    key.chmod(0o600)
    runtime.configure_mobile(root, key, session_seconds=60, total_seconds=120)
    env, _ = runtime.environment(root, {})
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    provider = importlib.import_module("agentbay_mobile")
    calls = []
    def tool(config, name, arguments):
        calls.append((name, dict(arguments)))
        if name == "create_sandbox":
            return {"isError": False, "content": [{"type": "text", "text": "s-owned-fixture"}]}
        if name == "kill_sandbox":
            return {"isError": False, "content": [{"type": "text", "text": "release resource success"}]}
        return {"isError": False, "content": [{"type": "text", "text": "[]"}]}
    monkeypatch.setattr(provider, "_tool", tool)
    def offline(*_args, **_kwargs):
        raise AssertionError("unit tests must not contact cloud or start a device")
    monkeypatch.setattr(socket.socket, "connect", offline)
    provider.reap("phone-owner")
    return runtime, root, provider, calls, monkeypatch


def test_configuration_and_status_are_read_only_and_key_not_in_environment(mobile):
    runtime, root, provider, calls, _ = mobile
    before = (root / "login-token.txt").read_bytes()
    env, _ = runtime.environment(root, {"MEYO_AGENTBAY_CONFIG": "/wrong/instance"})
    assert env["MEYO_AGENTBAY_CONFIG"] == str(root / "secrets/agentbay/config.json")
    assert "akm-offline-test-key-not-real" not in repr(env)
    result = provider.status("phone-owner")
    assert result["ready"] is False and result["phase"] == "idle"
    assert result["remaining_budget_seconds"] == 120
    assert calls == [] and before == (root / "login-token.txt").read_bytes()


def test_only_owner_can_start_and_reuse_a_single_resource(mobile):
    _, _, provider, calls, _ = mobile
    for name in ("status", "ensure", "screenshot", "stop"):
        with pytest.raises(PermissionError):
            getattr(provider, name)("someone-else")
    assert calls == []
    first = provider.ensure("phone-owner")
    second = provider.ensure("phone-owner")
    assert first["ready"] and second["ready"]
    assert len([c for c in calls if c[0] == "create_sandbox"]) == 1
    assert first["provider"] == "agentbay"
    assert first["remaining_budget_seconds"] == 60
    assert "akm-" not in repr(first)


def test_actions_do_not_implicitly_start_or_accept_identity_overrides(mobile):
    _, _, provider, calls, _ = mobile
    assert not provider.action("phone-owner", "type", {"text": "电池"})["ok"]
    assert not provider.screenshot("phone-owner")["ok"]
    assert calls == []
    provider.ensure("phone-owner")
    count = len(calls)
    for args in ({"x": 1, "y": 2, "sandbox_id": "s-other"},
                 {"x": 1, "y": 2, "uid": "other"}, {"x": -1, "y": 2}):
        with pytest.raises(ValueError):
            provider.action("phone-owner", "tap", args)
    assert len(calls) == count
    provider.action("phone-owner", "swipe",
                    {"x1": 2, "y1": 3, "x2": 20, "y2": 30, "duration_ms": 350})
    assert calls[-1] == ("swipe", {
        "sandbox_id": "s-owned-fixture", "start_x": 2, "start_y": 3,
        "end_x": 20, "end_y": 30, "duration_ms": 350,
    })
    provider.action("phone-owner", "press", {"key": "HOME"})
    assert calls[-1][1]["key"] == 3


def test_failure_result_is_not_upgraded_and_same_id_is_released(mobile):
    _, _, provider, calls, monkeypatch = mobile
    provider.ensure("phone-owner")
    def fail(_config, name, arguments):
        calls.append((name, arguments))
        return {"isError": True, "content": [{"type": "text", "text":
            "Failed to set text to node; akm-offline-test-key-not-real"}]}
    monkeypatch.setattr(provider, "_tool", fail)
    result = provider.action("phone-owner", "type", {"text": "电池"})
    assert result["ok"] is False and "Failed to set text" in result["error"]
    assert "akm-offline" not in repr(result)
    assert not provider.stop("phone-owner")["ok"]
    assert provider.status("phone-owner")["phase"] == "release_failed"
    count = len(calls)
    assert not provider.ensure("phone-owner")["ok"]
    assert len(calls) == count  # A failed release is not permission to buy another.


def test_reaper_and_reconfiguration_preserve_spending_and_existing_identity(mobile):
    runtime, root, provider, calls, monkeypatch = mobile
    provider.ensure("phone-owner")
    now = provider.time.time()
    monkeypatch.setattr(provider.time, "time", lambda: now + 90)
    assert not provider.status("phone-owner")["ready"]
    assert calls[-1][0] == "create_sandbox"  # status never starts/release resources.
    assert provider.reap("phone-owner")["released"] is True
    assert calls[-1] == ("kill_sandbox", {"sandbox_id": "s-owned-fixture"})
    token = (root / "login-token.txt").read_bytes()
    key = root / "secrets/agentbay/api-key.txt"
    runtime.configure_mobile(root, key, session_seconds=60, total_seconds=120)
    assert provider.status("phone-owner")["remaining_budget_seconds"] == 60
    assert (root / "login-token.txt").read_bytes() == token
    provider.ensure("phone-owner")
    provider.stop("phone-owner")
    assert provider.ensure("phone-owner")["error_code"] == "mobile_budget_exhausted"
    assert len([c for c in calls if c[0] == "create_sandbox"]) == 2


def test_ambiguous_create_response_never_retries_or_forgets_reserved_budget(mobile):
    _, _, provider, calls, monkeypatch = mobile
    def unknown(_config, name, arguments):
        calls.append((name, arguments))
        raise TimeoutError("sensitive HTTP endpoint must not be echoed")
    monkeypatch.setattr(provider, "_tool", unknown)
    result = provider.ensure("phone-owner")
    assert not result["ok"] and result["phase"] == "creation_unknown"
    assert "sensitive" not in repr(result)
    again = provider.ensure("phone-owner")
    assert not again["ok"] and len(calls) == 1
    assert provider.status("phone-owner")["remaining_budget_seconds"] == 60


def test_configuration_rejects_symlink_and_unbounded_budget(mobile):
    runtime, root, provider, _, _ = mobile
    before = (root / "secrets/agentbay/config.json").read_bytes()
    with pytest.raises(ValueError):
        runtime.configure_mobile(root, root / "secrets/agentbay/api-key.txt",
                                 session_seconds=60, total_seconds=999999)
    assert (root / "secrets/agentbay/config.json").read_bytes() == before
    path = root / "secrets/agentbay/control"
    path.mkdir(mode=0o700, exist_ok=True)
    elsewhere = root / "tmp/other.json"
    elsewhere.write_text("{}")
    (path / "phone.json").unlink()
    (path / "phone.json").symlink_to(elsewhere)
    with pytest.raises((ValueError, OSError)):
        provider.ensure("phone-owner")


def test_no_start_without_independent_expiry_reaper(mobile):
    _, _, provider, calls, monkeypatch = mobile
    now = provider.time.time()
    monkeypatch.setattr(provider.time, "time", lambda: now + 100)
    result = provider.ensure("phone-owner")
    assert result["error_code"] == "mobile_reaper_unavailable" and calls == []
    assert result["remaining_budget_seconds"] == 120


def test_recorded_sse_and_json_wire_shapes_are_parsed_not_guessed(mobile):
    _, _, provider, _, _ = mobile
    value = {"isError": False, "content": [{"type": "text", "text": "s-owned-fixture"}]}
    message = json.dumps({"jsonrpc": "2.0", "id": 2, "result": value}).encode()
    assert provider._rpc_reply(message, "application/json", 2) == value
    assert provider._rpc_reply(b"event: message\r\ndata: " + message + b"\r\n\r\n",
                               "text/event-stream", 2) == value
    with pytest.raises(ValueError):
        provider._rpc_reply(message, "application/json", 3)


def test_recorded_text_png_is_validated_and_saved_under_owner(mobile):
    _, root, provider, _, monkeypatch = mobile
    import struct
    import zlib
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\0\xff\0")) + chunk(b"IEND", b""))
    provider.ensure("phone-owner")
    def screenshot(_config, name, arguments):
        assert name == "screenshot" and arguments["sandbox_id"] == "s-owned-fixture"
        return {"isError": False, "content": [{"type": "text", "text": json.dumps({
            "type": "image", "mime_type": "image/png", "width": 2, "height": 1,
            "data": base64.b64encode(png).decode(),
        })}]}
    monkeypatch.setattr(provider, "_tool", screenshot)
    result = provider.screenshot("phone-owner")
    assert result["ok"], result
    path = Path(result["path"])
    assert path.parent == root / "data/users/phone-owner/mobile"
    assert path.read_bytes() == png and result["width_height"] == "2x1"
    assert not path.stat().st_mode & 0o077
    assert provider.read_screenshot("phone-owner", str(path)) == png
    with pytest.raises(PermissionError):
        provider.read_screenshot("other-owner", str(path))
    with pytest.raises(PermissionError):
        provider.read_screenshot("phone-owner", str(root / "login-token.txt"))


def test_provider_wire_initializes_and_never_follows_redirect_or_retries(mobile):
    _, _, provider, _, monkeypatch = mobile
    requests = []
    class Response:
        def __init__(self, value):
            self.value = value
            self.headers = {"Content-Type": "application/json", "Mcp-Session-Id": "fixture-session"}
            self.read_done = False
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read1(self, _):
            if self.read_done: return b""
            self.read_done = True
            return json.dumps(self.value).encode()
    class Opener:
        def open(self, request, timeout):
            body = json.loads(request.data)
            requests.append(body)
            if body["method"] == "initialize":
                return Response({"id": 1, "result": {"protocolVersion": "2025-06-18"}})
            if body["method"] == "notifications/initialized":
                return Response({})
            assert request.get_header("Mcp-session-id") == "fixture-session"
            return Response({"id": 2, "result": {
                "isError": False, "content": [{"type": "text", "text": "s-owned-fixture"}]}})
    monkeypatch.setattr(provider.urllib.request, "build_opener", lambda *args: Opener())
    # Undo only the fixture's cloud-tool stub; all HTTP remains an explicit fake.
    original = importlib.reload(provider)
    reply = original._tool(original._configuration("phone-owner"), "create_sandbox", {})
    assert reply["content"][0]["text"] == "s-owned-fixture"
    assert [r["method"] for r in requests] == ["initialize", "notifications/initialized", "tools/call"]
    with pytest.raises(ValueError):
        provider._NoRedirect().redirect_request(None, None, None, None, None, None)


def test_slow_action_does_not_block_expiry_or_return_stale_results(mobile):
    _, _, provider, calls, monkeypatch = mobile
    provider.ensure("phone-owner")
    started, released, cleanup = threading.Event(), threading.Event(), threading.Event()
    output = {}
    def slow(_config, name, arguments):
        calls.append((name, arguments))
        if name == "kill_sandbox":
            released.set()
            return {"isError": False, "content": [{"type": "text", "text": "release resource success"}]}
        started.set()
        cleanup.wait(timeout=2)
        return {"isError": False, "content": [{"type": "text", "text": "late result"}]}
    monkeypatch.setattr(provider, "_tool", slow)
    worker = threading.Thread(target=lambda: output.update(
        provider.action("phone-owner", "type", {"text": "fixture"})))
    worker.start()
    assert started.wait(timeout=1)
    reaper = threading.Thread(target=lambda: provider.stop("phone-owner"))
    reaper.start()
    try:
        assert released.wait(timeout=.5), "action lock blocked the independent resource release"
    finally:
        cleanup.set()
        worker.join(timeout=2)
        reaper.join(timeout=2)
    assert not output["ok"] and output["error_code"] == "mobile_session_changed"


def test_configuration_and_lifecycle_use_the_same_lock(mobile):
    runtime, root, provider, _, _ = mobile
    config = provider._configuration("phone-owner")
    entered, finished = threading.Event(), threading.Event()
    def configure():
        entered.set()
        runtime.configure_mobile(root, root / "secrets/agentbay/api-key.txt",
                                 session_seconds=60, total_seconds=120)
        finished.set()
    with provider._locked(config):
        thread = threading.Thread(target=configure)
        thread.start()
        assert entered.wait(timeout=1)
        completed_while_locked = finished.wait(timeout=.15)
    thread.join(timeout=2)
    assert finished.is_set()
    assert not completed_while_locked, "configuration update raced the lifecycle lock"


def test_current_phone_authorization_cannot_expand_to_one_hour(mobile):
    runtime, root, _, _, _ = mobile
    with pytest.raises(ValueError):
        runtime.configure_mobile(root, root / "secrets/agentbay/api-key.txt",
                                 session_seconds=900, total_seconds=3600)
