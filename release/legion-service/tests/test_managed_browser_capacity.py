"""Actual loopback HTTP admission, with dummy RPC work and temporary static bytes."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest


def wait_for(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, "HTTP fixture did not reach the expected state"
        time.sleep(0.005)


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MEYO_LEGION_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LEGION_HOME_DIR", str(tmp_path / "legion"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("no native/browser process allowed"))
    real_connect = socket.socket.connect

    def local_only(sock, address):
        assert isinstance(address, tuple) and address[0] == "127.0.0.1"
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    module = importlib.import_module("managed_browser")
    work = tmp_path / "work"
    work.mkdir()
    content = b"/* static fixture, not application code */\nvoid 0;\n"
    for name in ("app.js", *(f"hold-{i}.bin" for i in range(4))):
        (work / name).write_bytes(content)
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic outside data")
    (work / "outside-link").symlink_to(outside)
    releases = [threading.Event() for _ in range(4)]
    rpc_release = threading.Event()
    opened, calls = [], []

    class Lease:
        closed = False
        _token = "fixture-rpc"
        _mcp_session = "fixture-session"
        artifact_token = "fixture-static"
        _root_fd = None
        workspace = work
        _backend = SimpleNamespace(initialization_result={})
        tools = []
        _rpc_lock = threading.RLock()

        def call(self, params):
            calls.append(params["name"])
            with self._rpc_lock:
                assert rpc_release.wait(5), "dummy RPC gate timed out"
                return {"content": [{"type": "text", "text": "offline RPC complete"}]}

        def _open_artifact(self, parts):
            opened.append("/".join(parts))
            if len(parts) == 1 and parts[0].startswith("hold-"):
                assert releases[int(parts[0][5])].wait(5), "dummy static gate timed out"
            return module.ManagedBrowserLease._open_artifact(self, parts)

    lease = Lease()
    server = module._Server(lease)
    server._capacity_wait_s = 0.3
    limit = server._requests._value
    lease._authority = f"127.0.0.1:{server.server_port}"
    lease.base_url = "http://" + lease._authority
    listener = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.005})
    listener.start()
    pool = ThreadPoolExecutor(max_workers=8)
    idle_sockets = []

    def request(path="/app.js", *, rpc=False, headers=None, method="tools/call"):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        actual_headers = {"X-Meyo-Browser-Artifact": lease.artifact_token}
        body = None
        if rpc:
            actual_headers.update({"Authorization": "Bearer " + lease._token,
                                   "Mcp-Session-Id": lease._mcp_session,
                                   "Content-Type": "application/json"})
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                               "params": {"name": "browser_navigate", "arguments": {}}})
        actual_headers.update(headers or {})
        try:
            connection.request("POST" if rpc else "GET", "/mcp" if rpc else path,
                               body=body, headers=actual_headers)
            response = connection.getresponse()
            return {"status": response.status, "headers": dict(response.getheaders()),
                    "body": response.read()}
        except (OSError, http.client.HTTPException) as exc:
            return {"error": type(exc).__name__, "message": str(exc)}
        finally:
            connection.close()

    case = SimpleNamespace(
        server=server, lease=lease, request=request, pool=pool, releases=releases,
        rpc_release=rpc_release, opened=opened, calls=calls, content=content,
        idle_sockets=idle_sockets, limit=limit,
        busy=lambda: limit - server._requests._value)
    try:
        yield case
    finally:
        rpc_release.set()
        for release in releases:
            release.set()
        for connection in idle_sockets:
            connection.close()
        pool.shutdown(wait=True)
        server.shutdown()
        server.server_close()
        listener.join(3)
        wait_for(lambda: server._requests._value == limit)
        assert not listener.is_alive() and server.socket.fileno() == -1


def hold_static(case, count):
    futures = []
    for index in range(count):
        name = f"hold-{index}.bin"
        futures.append(case.pool.submit(case.request, "/" + name))
        wait_for(lambda name=name: name in case.opened)
    return futures


def test_navigation_plus_three_resources_does_not_drop_app_js(endpoint):
    c = endpoint
    before = c.request()
    navigation = c.pool.submit(c.request, rpc=True)
    wait_for(lambda: len(c.calls) == 1)
    resources = hold_static(c, 3)
    during = c.request()
    assert during.get("status") == 200, during
    assert before["body"] == during["body"] == c.content
    assert not navigation.done(), "the asset must load while navigation still owns its RPC"
    for release in c.releases:
        release.set()
    assert all(f.result()["status"] == 200 for f in resources)
    assert c.request()["body"] == c.content


def test_static_capacity_waits_for_a_slot_instead_of_resetting(endpoint):
    c = endpoint
    resources = hold_static(c, 4)
    waiting = c.pool.submit(c.request)
    wait_for(lambda: waiting.done() or c.busy() == 5)
    assert not waiting.done(), waiting.result()
    c.releases[0].set()
    assert resources[0].result()["status"] == 200
    assert waiting.result().get("status") == 200


def test_static_overload_is_explicit_503_and_does_not_block_rpc(endpoint):
    c = endpoint
    hold_static(c, 4)
    result = c.request()
    assert result.get("status") == 503, result
    assert result["headers"]["Retry-After"]
    assert json.loads(result["body"])["error"] == "browser_static_capacity_exceeded"
    assert c.request(rpc=True, method="ping")["status"] == 200


def test_rpc_overload_is_explicit_503_and_does_not_block_static(endpoint):
    c = endpoint
    pending = [c.pool.submit(c.request, rpc=True) for _ in range(4)]
    wait_for(lambda: len(c.calls) == 4)
    assert c.request().get("status") == 200
    result = c.request(rpc=True)
    assert result.get("status") == 503, result
    assert result["headers"]["Retry-After"]
    assert json.loads(result["body"])["error"] == "browser_rpc_capacity_exceeded"
    assert len(c.calls) == 4
    c.rpc_release.set()
    assert all(f.result()["status"] == 200 for f in pending)


def test_total_connections_stay_bounded_and_overload_has_http_response(endpoint):
    c = endpoint
    for index in range(c.limit):
        connection = socket.socket()
        connection.settimeout(3)
        connection.connect(("127.0.0.1", c.server.server_port))
        c.idle_sockets.append(connection)
        wait_for(lambda: c.busy() == index + 1)
    result = c.request()
    assert result.get("status") == 503, result
    assert json.loads(result["body"])["error"] == "browser_connection_capacity_exceeded"
    assert c.busy() == c.limit
    assert not c.opened and not c.calls


@pytest.mark.parametrize("path,headers,status", [
    ("/app.js", {"X-Meyo-Browser-Artifact": "wrong-fixture"}, 401),
    ("/app.js", {"Origin": "https://untrusted.invalid"}, 403),
    ("/../outside.txt", {}, 403),
    ("/%2e%2e/outside.txt", {}, 403),
    ("/outside-link", {}, 403),
    ("/missing.js", {}, 404),
])
def test_capacity_changes_keep_actual_static_auth_and_path_boundary(endpoint, path, headers, status):
    assert endpoint.request(path, headers=headers)["status"] == status
    assert endpoint.request()["status"] == 200, "a rejected request must release its permits"


def test_rpc_auth_and_session_rejections_do_not_call_lease(endpoint):
    c = endpoint
    assert c.request(rpc=True, headers={"Authorization": "wrong-fixture"})["status"] == 401
    assert c.request(rpc=True, headers={"Mcp-Session-Id": "wrong-fixture"})["status"] == 403
    assert not c.calls
