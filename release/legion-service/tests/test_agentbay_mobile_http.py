"""Real local HTTP + personal identity; the mobile/provider boundary is offline.

These tests exercise only mobile wiring. They never initialize personal_runtime,
start a device, consume a real ticket, or read the operator's credentials.
"""
from __future__ import annotations

import base64
import http.client
import importlib
import json
import socket
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest


SERVICE = Path(__file__).resolve().parents[1]
OWNER = "__ut_mobile_owner"
OTHER = "__ut_mobile_other"
OWNER_TOKEN = "__ut_owner_personal_token_1234567890"
OTHER_TOKEN = "__ut_other_personal_token_1234567890"
INSTANCE_TOKEN = "__ut_instance_token_not_a_person_1234"
MOBILE = "/api/legion/mobile"

DIRECT_ROUTES = [
    ("GET", "/status", None),
    ("GET", "/screenshot", None),
    ("POST", "/ensure", {}),
    ("POST", "/action", {"action": "tap", "params": {"x": 23, "y": 45}}),
    ("POST", "/actions/batch", {"actions": [{"action": "press", "params": {"key": "HOME"}}]}),
    ("POST", "/stop", {}),
]
LEGACY_ROUTES = [
    ("GET", "/apps/search", None),
    ("GET", "/apps/inventory", None),
    ("GET", "/tasks", None),
    ("GET", "/task?id=__ut_task", None),
    ("GET", "/companion/inbox", None),
    ("GET", "/companion/summary", None),
    ("GET", "/companion/health", None),
    ("GET", "/companion/preferences", None),
    ("POST", "/apk/inspect", {"source": "/__ut_fixture.apk"}),
    ("POST", "/apk/install", {"token": "__ut_plan", "confirm": True}),
    ("POST", "/apps/plan", {"app": {}, "operation": "install"}),
    ("POST", "/apps/apply", {"token": "__ut_plan", "confirm": True}),
    ("POST", "/apps/uninstall", {"package": "invalid.fixture", "confirm": True}),
    ("POST", "/apps/rollback", {"package": "invalid.fixture", "confirm": True}),
    ("POST", "/tasks/create", {"goal": "fixture only"}),
    ("POST", "/tasks/confirm", {"task_id": "__ut_task"}),
    ("POST", "/tasks/resume", {"task_id": "__ut_task"}),
    ("POST", "/tasks/replay", {"task_id": "__ut_task"}),
    ("POST", "/companion/read", {}),
    ("POST", "/companion/preferences", {"preferences": {}}),
    ("POST", "/companion/control", {"task_id": "__ut_task", "action": "resume"}),
]


class MobileBoundary:
    """The agreed provider API, with observable calls and no device operations."""

    def __init__(self, root):
        self.cloud = True
        self.ready = True
        self.calls = []
        self.authorized = []
        self.failure = ""
        self.picture = root / "phone fixture.png"
        self.picture.write_bytes(b"\x89PNG\r\n\x1a\n__ut_picture")

    def cloud_configured(self):
        return self.cloud

    def authorize_cloud_owner(self, uid):
        self.authorized.append(uid)
        if self.cloud and uid != OWNER:
            raise PermissionError("fixture owner denied")

    def state(self):
        return {
            "ok": True, "provider": "agentbay" if self.cloud else "redroid",
            "ready": self.ready, "phase": "ready" if self.ready else "idle",
            "expires_at": 1792231320 if self.ready else None,
            "remaining_seconds": 120 if self.ready else 0,
            "remaining_budget_seconds": 360,
            "session_seconds": 120, "screen_size": "1080x1920",
            "actions": ["screenshot", "tap", "swipe", "press", "type", "start", "ui", "apps"],
        }

    def status(self, force=False, *, uid=""):
        self.calls.append(("status", uid, force))
        return self.state()

    def ensure(self, timeout=120, *, uid=""):
        self.calls.append(("ensure", uid, timeout))
        if self.failure == "ensure":
            return {"ok": False, "error": "fixture_start_failed"}
        self.ready = True
        return {"ok": True, "status": self.state()}

    def picture_result(self):
        return {"ok": True, "path": str(self.picture), "bytes": self.picture.stat().st_size,
                "width_height": "1080x1920", "url": "https://untrusted.invalid/not-the-artifact"}

    def screenshot(self, *, uid=""):
        self.calls.append(("screenshot", uid))
        if not self.ready or self.failure == "screenshot":
            return {"ok": False, "error": "fixture_not_running"}
        return self.picture_result()

    def read_cloud_screenshot(self, uid, path):
        self.calls.append(("read_screenshot", uid, path))
        if uid != OWNER or path != str(self.picture) or self.failure == "read_screenshot":
            raise PermissionError("fixture_image_not_owned")
        return self.picture.read_bytes()

    def action(self, name, params=None, *, uid=""):
        self.calls.append(("action", uid, name, params))
        if self.failure == name:
            return {"ok": False, "error": "fixture_action_failed"}
        return self.picture_result() if name == "screenshot" else {"ok": True, "action": name}

    def stop(self, *, uid=""):
        self.calls.append(("stop", uid))
        if self.failure == "stop":
            return {"ok": False, "error": "fixture_release_failed"}
        self.ready = False
        return {"ok": True, "status": self.state()}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    for name in (
        "HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_SESSIONS_DIR",
        "LEGION_TASK_MEMORY_DIR", "LEGION_TEAM_DIR", "LEGION_PROJECT_CONTEXT_DIR",
        "LEGION_PROC_REGISTRY_DIR",
    ):
        folder = tmp_path / name
        folder.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(folder))
    tokens = tmp_path / "fixture-personal-tokens.json"
    tokens.write_text(json.dumps({OWNER_TOKEN: {"user": OWNER}, OTHER_TOKEN: {"user": OTHER}}))
    tokens.chmod(0o600)
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tokens))
    monkeypatch.setenv("LEGION_API_TOKEN", INSTANCE_TOKEN)
    monkeypatch.setenv("LEGION_EXT_AUTO_UPDATE", "0")
    monkeypatch.delenv("LEGION_PUBLIC_ORIGIN", raising=False)

    # The only permitted connection is the fixture's ephemeral HTTP listener.
    ports = set()
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def local_only(original):
        def guarded(sock, address):
            assert isinstance(address, tuple) and address[0] == "127.0.0.1" and address[1] in ports, \
                "unexpected network/device connection in mobile HTTP regression"
            return original(sock, address)
        return guarded

    def no_process(*_args, **_kwargs):
        raise AssertionError("mobile HTTP regression must not launch a process or device")

    monkeypatch.setattr(socket.socket, "connect", local_only(connect))
    monkeypatch.setattr(socket.socket, "connect_ex", local_only(connect_ex))
    monkeypatch.setattr(subprocess, "Popen", no_process)
    return SimpleNamespace(root=tmp_path, ports=ports, monkeypatch=monkeypatch)


@pytest.fixture
def http_mobile(isolated):
    patch = isolated.monkeypatch
    server = importlib.import_module("server")
    patch.setattr(server, "_PUBLIC_ORIGIN", None)
    patch.setattr(server, "_REMOTE_BIND_OK", False)
    patch.setattr(server, "_LEGACY_NO_ORIGIN", False)
    device = importlib.import_module("mobile_device")
    boundary = MobileBoundary(isolated.root)
    for method in ("cloud_configured", "authorize_cloud_owner", "status",
                   "ensure", "screenshot", "read_cloud_screenshot", "action", "stop"):
        patch.setattr(device, method, getattr(boundary, method), raising=False)

    legacy_calls = []
    for module, methods in (
        (device, ("inspect_apk", "install_apk")),
        (server.MOBILE_APPS, ("search", "inventory", "plan", "apply_plan", "uninstall", "rollback")),
        (server.MOBILE_TASKS, ("list_tasks", "get", "create", "decide", "resume", "replay")),
        (server.MOBILE_COMPANION, ("inbox", "daily_summary", "health", "preferences",
                                   "mark_read", "save_preferences", "control")),
    ):
        for name in methods:
            def legacy(*args, _name=f"{module.__name__}.{name}", **kwargs):
                legacy_calls.append((_name, args, kwargs))
                return {"ok": True, "tasks": [], "items": [], "apps": []}
            patch.setattr(module, name, legacy)

    ticket_calls = []
    tickets = importlib.import_module("meyo_ticket_identity")

    def fixture_ticket(ticket):
        ticket_calls.append(ticket)
        if ticket != "__ut_valid_ticket":
            raise tickets.TicketAuthError("meyo_ticket_rejected")
        return OWNER

    patch.setattr(tickets, "consume_ticket", fixture_ticket)
    listener = server.LegionServer(("127.0.0.1", 0), server.Handler)
    isolated.ports.add(listener.server_port)
    thread = threading.Thread(target=listener.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    def request(method, path, *, body=None, token=OWNER_TOKEN, extra=None):
        headers = {"Origin": f"http://127.0.0.1:{listener.server_port}"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        headers.update(extra or {})
        content = None if body is None else json.dumps(body)
        if content is not None:
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", listener.server_port, timeout=3)
        try:
            conn.request(method, path, body=content, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            data = json.loads(raw) if raw and "application/json" in response.getheader("Content-Type", "") else raw
            return response.status, data
        finally:
            conn.close()

    try:
        yield SimpleNamespace(request=request, device=boundary, legacy=legacy_calls,
                              tickets=ticket_calls, root=isolated.root)
    finally:
        listener.shutdown()
        listener.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("method,path,body", DIRECT_ROUTES)
@pytest.mark.parametrize("token", [None, "__ut_invalid", INSTANCE_TOKEN])
def test_unverified_credentials_never_reach_a_phone(http_mobile, method, path, body, token):
    status, _ = http_mobile.request(method, MOBILE + path, body=body, token=token)
    assert status in (401, 403)
    assert http_mobile.device.calls == []
    assert http_mobile.device.authorized == []
    assert http_mobile.legacy == []


@pytest.mark.parametrize("method,path,body", DIRECT_ROUTES)
def test_forged_uid_is_not_a_personal_subject(http_mobile, method, path, body):
    if method == "GET":
        path += "?uid=" + OTHER
    else:
        body = {**body, "uid": OTHER}
    status, result = http_mobile.request(method, MOBILE + path, body=body)
    assert status == 403 and result["error"] == "personal_owner_mismatch"
    assert http_mobile.device.calls == []


@pytest.mark.parametrize("method,path,body", DIRECT_ROUTES)
def test_other_verified_user_cannot_use_configured_cloud_owner(http_mobile, method, path, body):
    status, _ = http_mobile.request(method, MOBILE + path, body=body, token=OTHER_TOKEN)
    assert status == 403
    assert http_mobile.device.authorized == [OTHER]
    assert http_mobile.device.calls == []


def test_status_is_read_only_and_binds_the_verified_owner(http_mobile):
    http_mobile.device.ready = False
    status, result = http_mobile.request("GET", MOBILE + "/status")
    assert status == 200 and result["provider"] == "agentbay" and not result["ready"]
    assert result["remaining_budget_seconds"] == 360
    assert http_mobile.device.calls == [("status", OWNER, False)]
    assert http_mobile.device.authorized == [OWNER]


def assert_artifact_url(result, picture):
    url = urlsplit(result["url"])
    assert not url.scheme and not url.netloc
    assert url.path == "/api/legion/artifact"
    assert parse_qs(url.query)["path"] == [str(picture)]
    assert result["width_height"] == "1080x1920"


def assert_inline_png(result, picture):
    prefix = "data:image/png;base64,"
    assert result["url"].startswith(prefix), "cloud PNG must stay inside the authenticated response"
    assert base64.b64decode(result["url"][len(prefix):], validate=True) == picture.read_bytes()
    assert result["width_height"] == "1080x1920"


def test_screenshot_carries_owner_and_provider_read_inline_png(http_mobile):
    status, result = http_mobile.request("GET", MOBILE + "/screenshot")
    assert status == 200
    assert_inline_png(result, http_mobile.device.picture)
    assert http_mobile.device.calls == [
        ("screenshot", OWNER), ("read_screenshot", OWNER, str(http_mobile.device.picture)),
    ]


def test_explicit_start_and_stop_preserve_timeout_and_owner(http_mobile):
    http_mobile.device.ready = False
    status, _ = http_mobile.request("POST", MOBILE + "/ensure", body={"timeout_s": 47})
    assert status == 200
    status, result = http_mobile.request("POST", MOBILE + "/stop", body={})
    assert status == 200 and result["ok"] and not http_mobile.device.ready
    assert http_mobile.device.calls == [("ensure", OWNER, 47), ("stop", OWNER)]


def test_action_and_requested_visual_confirmation_use_same_owner(http_mobile):
    status, result = http_mobile.request("POST", MOBILE + "/action", body={
        "action": "type", "params": {"text": "中文 fixture"}, "verify_visual": True,
    })
    assert status == 200 and result["ok"]
    assert_inline_png(result["screenshot"], http_mobile.device.picture)
    assert http_mobile.device.calls == [
        ("action", OWNER, "type", {"text": "中文 fixture"}), ("screenshot", OWNER),
        ("read_screenshot", OWNER, str(http_mobile.device.picture)),
    ]


def test_screenshot_action_also_gets_a_handler_url(http_mobile):
    status, result = http_mobile.request("POST", MOBILE + "/action",
                                       body={"action": "screenshot", "verify_visual": True})
    assert status == 200
    assert_inline_png(result, http_mobile.device.picture)
    assert http_mobile.device.calls == [
        ("action", OWNER, "screenshot", {}), ("read_screenshot", OWNER, str(http_mobile.device.picture)),
    ]


def test_batch_carries_owner_to_every_action_and_final_screenshot(http_mobile):
    status, result = http_mobile.request("POST", MOBILE + "/actions/batch", body={
        "actions": [{"action": "press", "params": {"key": "BACK"}},
                    {"action": "tap", "params": {"x": 8, "y": 9}}],
        "verify_visual": True,
    })
    assert status == 200 and result["steps_completed"] == 2
    assert_inline_png(result["screenshot"], http_mobile.device.picture)
    assert http_mobile.device.calls == [
        ("action", OWNER, "press", {"key": "BACK"}),
        ("action", OWNER, "tap", {"x": 8, "y": 9}), ("screenshot", OWNER),
        ("read_screenshot", OWNER, str(http_mobile.device.picture)),
    ]


def test_batch_screenshot_action_also_gets_a_handler_url(http_mobile):
    status, result = http_mobile.request("POST", MOBILE + "/actions/batch", body={
        "actions": [{"action": "screenshot"}],
    })
    assert status == 200
    assert_inline_png(result["results"][0], http_mobile.device.picture)
    assert http_mobile.device.calls == [
        ("action", OWNER, "screenshot", {}), ("read_screenshot", OWNER, str(http_mobile.device.picture)),
    ]


@pytest.mark.parametrize("path,body", [
    ("/screenshot", None),
    ("/action", {"action": "screenshot"}),
    ("/action", {"action": "tap", "params": {"x": 8, "y": 9}, "verify_visual": True}),
    ("/actions/batch", {"actions": [{"action": "screenshot"}], "verify_visual": True}),
])
def test_legacy_screenshots_keep_their_artifact_url_without_cloud_file_reads(http_mobile, path, body):
    http_mobile.device.cloud = False
    method = "GET" if body is None else "POST"
    status, result = http_mobile.request(method, MOBILE + path, body=body)
    assert status == 200
    if path == "/actions/batch":
        assert_artifact_url(result["results"][0], http_mobile.device.picture)
    assert_artifact_url(result.get("screenshot") or result, http_mobile.device.picture)
    assert not any(call[0] == "read_screenshot" for call in http_mobile.device.calls)


def test_provider_refused_image_read_cannot_return_a_url_or_fall_back_to_artifacts(http_mobile):
    http_mobile.device.failure = "read_screenshot"
    status, result = http_mobile.request("GET", MOBILE + "/screenshot")
    assert status >= 400
    assert not result["ok"] and "url" not in result
    assert "data:image/" not in json.dumps(result)
    assert http_mobile.device.calls == [
        ("screenshot", OWNER), ("read_screenshot", OWNER, str(http_mobile.device.picture)),
    ]


def test_nested_batch_owner_mismatch_rejects_whole_batch_before_first_effect(http_mobile):
    status, result = http_mobile.request("POST", MOBILE + "/actions/batch", body={
        "actions": [{"action": "press", "params": {"key": "HOME"}},
                    {"action": "tap", "params": {"x": 8, "y": 9, "uid": OTHER}}],
        "verify_visual": True,
    })
    assert status == 403 and result["error"] == "personal_owner_mismatch"
    assert http_mobile.device.calls == []


@pytest.mark.parametrize("method,path,body", LEGACY_ROUTES)
def test_cloud_never_falls_through_to_old_app_or_task_routes(http_mobile, method, path, body):
    status, result = http_mobile.request(method, MOBILE + path, body=body)
    assert status == 409 and not result["ok"]
    assert result["error"] == "mobile_operation_not_supported"
    assert http_mobile.device.calls == []
    assert http_mobile.legacy == []


@pytest.mark.parametrize("path,body", [
    ("/action", {"action": "task", "params": {"task": "fixture"}}),
    ("/actions/batch", {"actions": [{"action": "press", "params": {"key": "HOME"}},
                                   {"action": "task", "params": {"task": "fixture"}}]}),
])
def test_old_task_action_cannot_start_even_part_of_a_cloud_batch(http_mobile, path, body):
    status, result = http_mobile.request("POST", MOBILE + path, body=body)
    assert status == 409 and result["error"] == "mobile_operation_not_supported"
    assert http_mobile.device.calls == []


@pytest.mark.parametrize("method,path,body", LEGACY_ROUTES)
def test_unconfigured_cloud_keeps_legacy_routes(http_mobile, method, path, body):
    http_mobile.device.cloud = False
    status, result = http_mobile.request(method, MOBILE + path, body=body)
    assert status == 200 and result["ok"]
    assert len(http_mobile.legacy) == 1


@pytest.mark.parametrize("operation,expected", [("ensure", 503), ("stop", 400), ("screenshot", 503)])
def test_provider_failure_is_not_reported_as_success(http_mobile, operation, expected):
    http_mobile.device.failure = operation
    method = "GET" if operation == "screenshot" else "POST"
    status, result = http_mobile.request(method, MOBILE + "/" + operation,
                                        body=None if method == "GET" else {})
    assert status == expected and not result["ok"]


def test_browser_foreign_origin_cannot_capture_or_create_phone(http_mobile):
    for method, path, body in DIRECT_ROUTES:
        status, _ = http_mobile.request(method, MOBILE + path, body=body,
                                       extra={"Origin": "https://other.invalid"})
        assert status == 403
    assert http_mobile.device.calls == []


def test_ticket_is_verified_once_and_not_replaced_by_client_uid(http_mobile):
    status, _ = http_mobile.request("POST", MOBILE + "/ensure", token=None,
                                   body={"ticket": "__ut_valid_ticket", "uid": OTHER})
    assert status == 200
    assert http_mobile.tickets == ["__ut_valid_ticket"]
    assert http_mobile.device.calls == [("ensure", OWNER, 120)]
    assert http_mobile.device.authorized == [OWNER]


def test_browser_personal_cookie_binds_the_same_owner_without_a_bearer_header(http_mobile):
    identity = importlib.import_module("personal_identity")
    status, _ = http_mobile.request("POST", MOBILE + "/ensure", token=None, body={},
                                   extra={"Cookie": f"{identity.COOKIE_NAME}={OWNER_TOKEN}"})
    assert status == 200
    assert http_mobile.device.calls == [("ensure", OWNER, 120)]
    assert http_mobile.device.authorized == [OWNER]


def test_rejected_ticket_and_conflicting_personal_subject_never_reach_phone(http_mobile):
    for token, ticket in ((None, "__ut_invalid_ticket"), (OWNER_TOKEN, "__ut_invalid_ticket"),
                          (OTHER_TOKEN, "__ut_valid_ticket")):
        status, _ = http_mobile.request("GET", MOBILE + "/screenshot", token=token,
                                       extra={"X-Legion-Ticket": ticket})
        assert status == 401
    assert http_mobile.device.calls == []
    assert http_mobile.device.authorized == []
