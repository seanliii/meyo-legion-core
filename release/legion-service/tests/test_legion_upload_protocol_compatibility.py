"""Offline HTTP regression against this release tree, never the source checkout.

Run with PYTHONDONTWRITEBYTECODE=1 and pytest -p no:cacheprovider --basetemp=...
Only the real Handler and a local ticket-consume fixture may receive connections.
"""
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

import pytest


SERVICE = Path(__file__).resolve().parents[1]
ALICE = "fixture-personal-alice-0000000000"
BOB = "fixture-personal-bob-000000000000"
INSTANCE = "fixture-instance-not-personal-000"
TICKET_ALICE = "fixture-ticket-alice"
TICKET_BOB = "fixture-ticket-bob"
UPLOAD_LIMIT = 25 * 1024 * 1024
JSON_LIMIT = 2 * 1024 * 1024
BOUNDARY = "----OfflineBrowserFormDataBoundary"


def multipart(files=None, fields=(), boundary=BOUNDARY):
    """Browser-style UTF-8 text fields and unencoded binary file parts."""
    if files is None:
        files = [("files", "fixture.bin", b"\x00\xfffixture\r\nbytes")]
    chunks = []
    for name, value in fields:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            .encode() + str(value).encode() + b"\r\n")
    for field, name, content in files:
        chunks.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
             f'filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n')
            .encode() + content + b"\r\n")
    return b"".join(chunks) + f"--{boundary}--\r\n".encode()


def upload_body(protocol, content=b"fixture", **values):
    if protocol == "json":
        return (json.dumps({"filename": "fixture.bin",
                            "data_base64": base64.b64encode(content).decode(),
                            **values}).encode(), "application/json")
    return (multipart(files=[("files", "fixture.bin", content)],
                      fields=list(values.items())),
            f"multipart/form-data; boundary={BOUNDARY}")


@pytest.fixture
def scene(tmp_path, monkeypatch):
    # Pin both reads and writes before importing any product module. Cached
    # product modules must also originate in this very same release directory.
    for key in list(os.environ):
        if key.startswith(("LEGION_", "MEYO_", "MARS_")):
            monkeypatch.delenv(key)
    for key, leaf in {
        "HOME": "home", "USERPROFILE": "home", "CODEX_HOME": "codex",
        "CLAUDE_CONFIG_DIR": "claude", "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache", "XDG_DATA_HOME": "data",
        "MEYO_LEGION_DIR": "meyo", "LEGION_HOME_DIR": "legion",
    }.items():
        path = tmp_path / leaf
        path.mkdir(exist_ok=True)
        monkeypatch.setenv(key, str(path))
    monkeypatch.setenv("LEGION_API_TOKEN", INSTANCE)
    monkeypatch.setenv("LEGION_ALLOW_REMOTE_BIND", "0")
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(SERVICE))
    store = tmp_path / "personal-tokens.json"
    store.write_text(json.dumps({
        ALICE: {"user": "alice"}, BOB: {"user": "bob"},
        # Even a provisioned store entry must not promote an instance token.
        INSTANCE: {"user": "alice"},
    }))
    store.chmod(0o600)
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(store))

    allowed_ports, forbidden = set(), []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check_address(address):
        if (not isinstance(address, tuple) or len(address) < 2
                or address[0] not in ("127.0.0.1", "::1", "localhost")
                or int(address[1]) not in allowed_ports):
            forbidden.append("non-fixture connection")
            raise AssertionError("Only fixture loopback connections are allowed")

    def connect(sock, address):
        check_address(address)
        return real_connect(sock, address)

    def connect_ex(sock, address):
        check_address(address)
        return real_connect_ex(sock, address)

    def getaddrinfo(host, port, *args, **kwargs):
        check_address((host, port))
        return real_getaddrinfo(host, port, *args, **kwargs)

    def no_process(*args, **kwargs):
        forbidden.append("subprocess")
        raise AssertionError("Subprocesses, installers and models are forbidden")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "getfqdn", lambda *args: "localhost")
    monkeypatch.setattr(subprocess, "Popen", no_process)
    monkeypatch.setattr(os, "system", no_process)

    spec = importlib.util.spec_from_file_location(
        "release_upload_compatibility_server", SERVICE / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    import personal_identity
    import meyo_ticket_identity
    for module in (server, server.PATHS, server.SESSIONS, server.TEAMS,
                   personal_identity, meyo_ticket_identity):
        assert Path(module.__file__).resolve().parent == SERVICE
    meyo_ticket_identity._reset_for_tests()

    consume_calls = []

    class TicketFixture(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.path == "/api/v1/meyo-agent/internal/legion-ticket/consume"
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            consume_calls.append(value)
            uid = {TICKET_ALICE: "alice", TICKET_BOB: "bob"}.get(value.get("ticket"))
            body = json.dumps({"code": 200 if uid else 401,
                               "data": {"uid": uid}}).encode()
            self.send_response(200 if uid else 401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    issuer = ThreadingHTTPServer(("127.0.0.1", 0), TicketFixture)
    allowed_ports.add(issuer.server_port)
    monkeypatch.setenv("MARS_SERVER_BASE", f"http://127.0.0.1:{issuer.server_port}")
    reads, responses, cleared = [], [], []
    original_setup = server.Handler.setup
    original_send = server.Handler._send_json
    original_clear = server.Handler._clear_request_identity

    class ObserveBodyReads:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            reads.append(("read", size))
            return self.stream.read(size)

        def read1(self, size=-1):
            reads.append(("read1", size))
            return self.stream.read1(size)

    def setup(handler):
        original_setup(handler)
        handler.rfile = ObserveBodyReads(handler.rfile)

    def send(handler, obj, code=200):
        responses.append({
            "path": handler.path, "requestline": handler.requestline,
            "raw_requestline": handler.raw_requestline.decode("iso-8859-1"),
            "headers": dict(handler.headers), "body": obj,
            "payload": getattr(handler, "_request_json_payload", None),
        })
        return original_send(handler, obj, code)

    def clear(handler):
        original_clear(handler)
        cleared.append((handler._request_ticket_identity, handler._request_json_payload,
                        getattr(handler, "_request_upload_ticket", None)))

    monkeypatch.setattr(server.Handler, "setup", setup)
    monkeypatch.setattr(server.Handler, "_send_json", send)
    monkeypatch.setattr(server.Handler, "_clear_request_identity", clear)
    app = server.LegionServer(("127.0.0.1", 0), server.Handler)
    allowed_ports.add(app.server_port)
    threads = [threading.Thread(target=s.serve_forever,
                                kwargs={"poll_interval": 0.01}, daemon=True)
               for s in (issuer, app)]
    for thread in threads:
        thread.start()

    for uid in ("alice", "bob"):
        for turn in range(6 if uid == "alice" else 1):
            server.SESSIONS.save_turn(f"session-{uid}", f"original turn {turn}",
                                     {"results": []}, user_id=uid)
        server.TEAMS._write({"team_id": f"team-{uid}", "user_id": uid,
                            "conversation_id": f"session-{uid}"})
    original = Path(personal_identity.upload_root("alice")) / "original.txt"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"o" * 46)
    session_path = Path(server.SESSIONS._path("session-alice"))
    session_before, store_before = session_path.read_bytes(), store.read_bytes()

    def request(path="/api/legion/upload?uid=alice", *, body=None,
                content_type=None, token=ALICE, headers=None, method=None):
        outgoing = {"Origin": f"http://127.0.0.1:{app.server_port}"}
        if token:
            outgoing["Authorization"] = "Bearer " + token
        if content_type:
            outgoing["Content-Type"] = content_type
        outgoing.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", app.server_port, timeout=8)
        try:
            connection.request(method or ("POST" if body is not None else "GET"),
                               path, body, outgoing)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def raw_request(body, headers, *, path="/api/legion/upload?uid=alice",
                    half_close=False):
        sock = socket.create_connection(("127.0.0.1", app.server_port), timeout=8)
        raw_headers = [
            f"POST {path} HTTP/1.1", f"Host: 127.0.0.1:{app.server_port}",
            f"Origin: http://127.0.0.1:{app.server_port}",
            f"Authorization: Bearer {ALICE}",
            *[f"{key}: {value}" for key, value in headers],
        ]
        try:
            started = time.monotonic()
            sock.sendall(("\r\n".join(raw_headers) + "\r\n\r\n").encode() + body)
            if half_close:
                sock.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(sock)
            response.begin()
            result = response.status, response.read(), time.monotonic() - started
            # A malformed/unfinished body must not become another request.
            sock.settimeout(1)
            assert sock.recv(1) == b""
            return result
        finally:
            sock.close()

    def files_snapshot():
        return {str(p): p.read_bytes()
                for p in Path(server.PATHS.uploads_dir()).rglob("*") if p.is_file()}

    try:
        yield {
            "server": server, "request": request, "raw_request": raw_request,
            "reads": reads, "responses": responses, "cleared": cleared,
            "files": files_snapshot, "consume_calls": consume_calls,
            "port": app.server_port, "session_path": session_path,
            "session_before": session_before, "store": store,
            "original_url": "/media/" + original.relative_to(
                server.PATHS.uploads_dir()).as_posix(),
        }
    finally:
        for httpd in (app, issuer):
            httpd.shutdown()
            httpd.server_close()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
        meyo_ticket_identity._reset_for_tests()
        assert session_path.read_bytes() == session_before
        assert store.read_bytes() == store_before
        assert not forbidden
        assert all(value == (None, None, None) for value in cleared)


def assert_download(scene, entry, content):
    path = Path(entry["path"])
    assert path.parent == Path(scene["server"].PATHS.uploads_dir()) / "personal" / (
        hashlib.sha256(b"alice").hexdigest())
    assert path.read_bytes() == content
    assert scene["request"](entry["url"])[:2] == (200, content)
    for token in (BOB, INSTANCE, None):
        code, body, _ = scene["request"](entry["url"], token=token)
        assert code == 403 and body != content


def test_local_media_inspection_does_not_publish_or_advertise_download(scene, tmp_path):
    """Replay the real tool/GET failure using local bytes, not the user's media."""
    path = tmp_path / "unpublished.mp4"
    content = b"not a video container"
    path.write_bytes(content)
    before = scene["files"]()
    body = json.dumps({"tool": "runnable:media_inspect",
                       "params": {"path": str(path)}})

    code, raw, _ = scene["request"](
        "/api/legion/tool", body=body, content_type="application/json")
    result = json.loads(raw)

    assert code == 200 and result["ok"] is True
    assert result["url"] == ""
    assert result["downloadable"] is False and result["renderable"] is False
    assert result["path"] == str(path) and result["bytes"] == len(content)
    assert result["format_inference"]["basis"] == "filename_extension"
    assert result["content_processing"]["read"] == "not_performed"
    assert result["content_processing"]["media_decode"] == "not_performed"
    assert result["link"]["availability"] == "not_provided"
    assert scene["request"]("/media/unpublished.mp4")[0] == 404
    assert scene["files"]() == before
    assert path.read_bytes() == content


def test_inspection_keeps_supplied_private_url_and_existing_access_checks(scene):
    url = scene["original_url"]
    path = Path(scene["server"].PATHS.uploads_dir()) / url.removeprefix("/media/")
    body = json.dumps({"tool": "runnable:file_read",
                       "params": {"path": str(path), "url": url}})

    code, raw, _ = scene["request"](
        "/api/legion/tool", body=body, content_type="application/json")
    result = json.loads(raw)

    assert code == 200 and result["ok"] is True
    assert result["url"] == url and result["downloadable"] is True
    assert result["renderable"] is True and result["preview"]["ok"] is True
    assert result["link"] == {"source": "caller", "availability": "unverified"}
    assert result["content_processing"]["read"] == "performed"
    # Only this explicit GET proves access; describe must not confer ownership.
    assert_download(scene, result, b"o" * 46)


@pytest.mark.parametrize("data_url", [False, True])
def test_legacy_json_keeps_complete_single_object_response(scene, data_url):
    content = "原 JSON 上传兼容\n".encode()
    encoded = base64.b64encode(content).decode()
    body = json.dumps({"filename": "legacy.txt",
                       "data_base64": ("data:text/plain;base64," if data_url else "") + encoded})
    code, raw, _ = scene["request"](body=body, content_type="application/json")
    assert code == 200
    result = json.loads(raw)
    assert set(result) == {"ok", "url", "path", "kind", "bytes", "orig_name",
                           "preview", "renderable"}
    assert result["ok"] is True and result["bytes"] == len(content)
    assert result["orig_name"] == "legacy.txt" and result["kind"] == "text"
    assert result["preview"] == {"ok": True, "text": content.decode()}
    assert result["renderable"] is True
    assert_download(scene, result, content)


@pytest.mark.parametrize("auth", ["bearer", "cookie", "ticket-header", "ticket-query"])
def test_frontend_files_formdata_upload_and_real_private_download(scene, auth):
    content = b"\x00\xffbrowser-file\r\nnot-base64"
    body, content_type = upload_body("multipart", content)
    headers, token, query = {}, ALICE, {"uid": "alice"}
    if auth == "cookie":
        token, headers = None, {"Cookie": "legion_personal=" + ALICE}
    elif auth == "ticket-header":
        token, headers = None, {"X-Legion-Ticket": TICKET_ALICE}
    elif auth == "ticket-query":
        token, query["ticket"] = None, TICKET_ALICE
    code, raw, _ = scene["request"]("/api/legion/upload?" + urlencode(query),
                                  body=body, content_type=content_type,
                                  token=token, headers=headers)
    assert code == 200, raw
    result = json.loads(raw)
    assert result["ok"] is True and len(result["files"]) == 1
    entry = result["files"][0]
    assert entry["name"] == "fixture.bin" and entry["size"] == len(content)
    assert entry["kind"] == "file" and "error" not in entry
    assert_download(scene, entry, content)
    assert scene["request"](scene["original_url"])[:2] == (200, b"o" * 46)
    code, raw, _ = scene["request"]("/api/legion/session?sid=session-alice&uid=alice")
    assert code == 200 and len(json.loads(raw)["turns"]) == 6


@pytest.mark.parametrize("protocol", ["json", "multipart"])
def test_upload_over_ordinary_two_mib_limit_succeeds(scene, protocol):
    content = b"a" * (JSON_LIMIT + 23)
    body, content_type = upload_body(protocol, content)
    code, raw, _ = scene["request"](body=body, content_type=content_type)
    assert code == 200, raw
    result = json.loads(raw)
    entry = result if protocol == "json" else result["files"][0]
    assert_download(scene, entry, content)


@pytest.mark.parametrize("protocol", ["json", "multipart"])
def test_request_at_exact_25_mib_including_envelope_succeeds(scene, protocol):
    body, content_type = upload_body(protocol, b"one")
    body += b" " * (UPLOAD_LIMIT - len(body))
    code, raw, _ = scene["request"](body=body, content_type=content_type)
    assert code == 200, raw
    result = json.loads(raw)
    assert_download(scene, result if protocol == "json" else result["files"][0], b"one")


@pytest.mark.parametrize("protocol", ["json", "multipart"])
def test_over_limit_is_413_before_any_body_read_or_write(scene, protocol):
    _, content_type = upload_body(protocol)
    before, reads = scene["files"](), len(scene["reads"])
    code, _, elapsed = scene["raw_request"](b"", [
        ("Content-Type", content_type), ("Content-Length", UPLOAD_LIMIT + 1)])
    assert code == 413 and elapsed < 2
    assert scene["reads"][reads:] == [] and scene["files"]() == before


def test_ordinary_json_keeps_two_mib_limit(scene):
    body = b"{}" + b" " * (JSON_LIMIT - 2)
    assert scene["request"]("/api/legion/auth/session", body=body,
                            content_type="application/json")[0] == 200
    before, reads = scene["files"](), len(scene["reads"])
    code, _, _ = scene["raw_request"](b"", [
        ("Content-Type", "application/json"), ("Content-Length", JSON_LIMIT + 1)],
        path="/api/legion/auth/session")
    assert code == 413
    assert scene["reads"][reads:] == [] and scene["files"]() == before


@pytest.mark.parametrize("protocol", ["json", "multipart"])
@pytest.mark.parametrize("length", ["-1", "not-a-number", "+4", "1.5", "4,4"])
def test_invalid_content_length_is_rejected_before_read(scene, protocol, length):
    _, content_type = upload_body(protocol)
    before, reads = scene["files"](), len(scene["reads"])
    code, _, elapsed = scene["raw_request"](b"", [
        ("Content-Type", content_type), ("Content-Length", length)])
    assert code == 400 and elapsed < 2
    assert scene["reads"][reads:] == [] and scene["files"]() == before


@pytest.mark.parametrize("extra", [
    [("Content-Length", "4"), ("Content-Length", "4")],
    [("Content-Length", "4"), ("Content-Length", "5")],
    [("Content-Length", "4"), ("Transfer-Encoding", "chunked")],
])
def test_ambiguous_framing_is_rejected_before_read(scene, extra):
    before, reads = scene["files"](), len(scene["reads"])
    code, _, _ = scene["raw_request"](b"", [
        ("Content-Type", f"multipart/form-data; boundary={BOUNDARY}"), *extra])
    assert code == 400
    assert scene["reads"][reads:] == [] and scene["files"]() == before


@pytest.mark.parametrize("protocol", ["json", "multipart"])
def test_complete_payload_with_truncated_http_length_is_not_saved(scene, protocol):
    body, content_type = upload_body(protocol)
    before = scene["files"]()
    code, _, _ = scene["raw_request"](body, [
        ("Content-Type", content_type), ("Content-Length", len(body) + 7)],
        half_close=True)
    assert code == 400 and scene["files"]() == before


@pytest.mark.parametrize("protocol", ["json", "multipart"])
def test_stalled_body_times_out_in_five_seconds_and_closes(scene, protocol):
    body, content_type = upload_body(protocol)
    before = scene["files"]()
    code, _, elapsed = scene["raw_request"](body[:4], [
        ("Content-Type", content_type), ("Content-Length", len(body))])
    assert code == 400 and 4.5 <= elapsed < 7
    assert scene["files"]() == before


@pytest.mark.parametrize("damage", [
    "missing-boundary", "wrong-boundary", "missing-close", "invalid-part-header",
    "json-instead", "no-files", "empty-file", "nested-multipart",
])
def test_bad_multipart_is_rejected_without_partial_files(scene, damage):
    body, content_type = upload_body("multipart")
    if damage == "missing-boundary":
        content_type = "multipart/form-data"
    elif damage == "wrong-boundary":
        content_type = "multipart/form-data; boundary=wrong"
    elif damage == "missing-close":
        body = body[:body.rfind(f"--{BOUNDARY}--".encode())]
    elif damage == "invalid-part-header":
        body = body.replace(b"Content-Type: application/octet-stream", b"broken header")
    elif damage == "json-instead":
        body = json.dumps({"ticket": TICKET_ALICE}).encode()
    elif damage == "no-files":
        body = multipart(files=[], fields=[("uid", "alice")])
    elif damage == "empty-file":
        body = multipart(files=[("files", "first.bin", b"valid"),
                                ("files", "second.bin", b"")])
    elif damage == "nested-multipart":
        body = multipart(files=[("files", "nested.bin", multipart())])
        body = body.replace(b"Content-Type: application/octet-stream",
                            f"Content-Type: multipart/mixed; boundary={BOUNDARY}".encode(), 1)
    before = scene["files"]()
    code, _, _ = scene["request"](body=body, content_type=content_type)
    assert code == 400 and scene["files"]() == before


@pytest.mark.parametrize("protocol", ["json", "multipart"])
@pytest.mark.parametrize("token", [None, INSTANCE, "fixture-unknown-token"])
def test_no_personal_auth_never_writes(scene, protocol, token):
    body, content_type = upload_body(protocol)
    before = scene["files"]()
    code, _, _ = scene["request"](body=body, content_type=content_type, token=token)
    assert code == 403 and scene["files"]() == before


@pytest.mark.parametrize("protocol", ["json", "multipart"])
def test_bad_origin_rejects_before_body_and_cleans_ticket(scene, protocol):
    body, content_type = upload_body(protocol)
    before, reads = scene["files"](), len(scene["reads"])
    code, _, _ = scene["request"]("/api/legion/upload?ticket=" + TICKET_ALICE,
        body=body, content_type=content_type,
        headers={"Origin": "https://untrusted.invalid", "X-Legion-Ticket": TICKET_ALICE})
    assert code == 403
    assert scene["reads"][reads:] == [] and scene["files"]() == before
    assert not scene["consume_calls"]
    assert TICKET_ALICE not in repr(scene["responses"])


@pytest.mark.parametrize("field,value", [
    ("uid", "bob"), ("user_id", "bob"), ("sid", "session-bob"),
    ("session_id", "session-bob"), ("conversation_id", "session-bob"),
    ("team_id", "team-bob"), ("qhash", "team-bob"),
])
@pytest.mark.parametrize("where", ["query", "form"])
def test_multipart_owner_and_resource_fields_reuse_binding(scene, field, value, where):
    body = multipart(fields=[(field, value)] if where == "form" else [])
    query = {field: value} if where == "query" else {}
    before = scene["files"]()
    code, _, _ = scene["request"]("/api/legion/upload?" + urlencode(query),
        body=body, content_type=f"multipart/form-data; boundary={BOUNDARY}")
    assert code == 403 and scene["files"]() == before


def test_multipart_valid_text_fields_and_aliases_are_bound(scene):
    body = multipart(fields=[("uid", "alice"), ("session_id", "session-alice"),
                             ("qhash", "team-alice")])
    code, raw, _ = scene["request"]("/api/legion/upload?sid=session-alice",
        body=body, content_type=f'Multipart/Form-Data; boundary="{BOUNDARY}"')
    assert code == 200, raw
    assert json.loads(raw)["files"][0]["path"]


@pytest.mark.parametrize("fields", [
    [("uid", "alice"), ("uid", "bob")],
    [("session_id", "session-alice"), ("sid", "session-bob")],
    [("project_context", '{"user_id":"bob"}')],
    [("params", '{"session_id":"session-bob"}')],
    [("ticket", TICKET_ALICE)],
])
def test_multipart_ambiguous_or_unsupported_text_fields_never_write(scene, fields):
    before = scene["files"]()
    body = multipart(fields=fields)
    code, raw, _ = scene["request"](body=body,
        content_type=f"multipart/form-data; boundary={BOUNDARY}")
    assert code in (400, 403) and scene["files"]() == before
    assert TICKET_ALICE.encode() not in raw


def test_multipart_ticket_field_is_not_a_login_channel(scene):
    before = scene["files"]()
    code, _, _ = scene["request"](body=multipart(fields=[("ticket", TICKET_ALICE)]),
        token=None, content_type=f"multipart/form-data; boundary={BOUNDARY}")
    assert code == 403 and not scene["consume_calls"]
    assert scene["files"]() == before


@pytest.mark.parametrize("transport", ["header", "query", "body", "all"])
def test_json_ticket_transports_keep_legacy_response_and_cleanup(scene, transport):
    payload = {"filename": "legacy.bin", "data_base64": "bGVnYWN5", "uid": "placeholder"}
    headers, query = {}, {}
    if transport in ("header", "all"):
        headers["X-Legion-Ticket"] = TICKET_ALICE
    if transport in ("query", "all"):
        query["ticket"] = TICKET_ALICE
    if transport in ("body", "all"):
        payload["ticket"] = TICKET_ALICE
    code, raw, _ = scene["request"]("/api/legion/upload?" + urlencode(query),
        body=json.dumps(payload), content_type="application/json", token=None, headers=headers)
    assert code == 200, raw
    result = json.loads(raw)
    assert "files" not in result and result["bytes"] == 6
    assert_download(scene, result, b"legacy")
    assert TICKET_ALICE not in repr(scene["responses"])


@pytest.mark.parametrize("path,content_type", [
    ("/api/legion/auth/session", "multipart/form-data; boundary=looks-real"),
    ("/api/legion/auth/session", "multipart/form-data"),
    ("/api/legion/upload", "multipart/form-data-not-really"),
    ("/api/legion/upload", "application/json"),
])
def test_non_upload_or_fake_mime_cannot_hide_json_body_ticket_conflict(
        scene, path, content_type):
    body, _ = upload_body("json", ticket=TICKET_BOB)
    before = scene["files"]()
    code, raw, _ = scene["request"](path + "?ticket=" + TICKET_ALICE,
        body=body, content_type=content_type,
        headers={"X-Legion-Ticket": TICKET_ALICE})
    assert code == 401 and json.loads(raw)["error"] == "meyo_ticket_conflicting_credentials"
    assert not scene["consume_calls"] and scene["files"]() == before
    assert TICKET_ALICE not in repr(scene["responses"])
    assert TICKET_BOB not in repr(scene["responses"])


@pytest.mark.parametrize("conflict", ["query", "personal-token"])
def test_multipart_existing_ticket_conflicts_are_preserved(scene, conflict):
    body, content_type = upload_body("multipart")
    query = "?ticket=" + TICKET_BOB if conflict == "query" else ""
    before = scene["files"]()
    code, raw, _ = scene["request"]("/api/legion/upload" + query,
        body=body, content_type=content_type,
        token=BOB if conflict == "personal-token" else None,
        headers={"X-Legion-Ticket": TICKET_ALICE})
    assert code == 401, raw
    assert scene["files"]() == before
    assert TICKET_ALICE not in repr(scene["responses"])
    assert TICKET_BOB not in repr(scene["responses"])


def test_multiple_files_and_existing_acceptance_file_alias(scene):
    body = multipart(files=[("files", "one.txt", b"one"), ("file", "two.bin", b"two")])
    code, raw, _ = scene["request"](body=body,
        content_type=f"multipart/form-data; boundary={BOUNDARY}")
    assert code == 200, raw
    result = json.loads(raw)
    assert len(result["files"]) == 2
    for entry, content in zip(result["files"], (b"one", b"two")):
        assert entry["size"] == len(content) and "error" not in entry
        assert_download(scene, entry, content)


def test_browser_utf8_filename_is_not_an_email_syntax_error(scene):
    content = "旅行资料，完整读回。\n".encode()
    body = multipart(files=[("files", "旅行资料.txt", content)])
    code, raw, _ = scene["request"](body=body,
        content_type=f"multipart/form-data; boundary={BOUNDARY}")
    assert code == 200, raw
    entry = json.loads(raw)["files"][0]
    assert entry["name"] == "旅行资料.txt" and entry["kind"] == "text"
    assert_download(scene, entry, content)


@pytest.mark.parametrize("damage", ["invalid-utf8", "duplicate-name", "duplicate-boundary"])
def test_broken_or_ambiguous_multipart_headers_are_not_accepted(scene, damage):
    body, content_type = upload_body("multipart")
    if damage == "invalid-utf8":
        body = body.replace(b'filename="fixture.bin"', b'filename="\xff.bin"')
    elif damage == "duplicate-name":
        body = body.replace(b'name="files"', b'name="files"; name="ticket"')
    else:
        content_type += "; boundary=other"
    before = scene["files"]()
    code, _, _ = scene["request"](body=body, content_type=content_type)
    assert code == 400 and scene["files"]() == before


def test_trickle_body_cannot_extend_the_five_second_read_budget(scene):
    sock = socket.create_connection(("127.0.0.1", scene["port"]), timeout=8)
    before = scene["files"]()
    stop = threading.Event()

    def trickle():
        while not stop.wait(0.2):
            try:
                sock.sendall(b"x")
            except OSError:
                return

    sender = threading.Thread(target=trickle, daemon=True)
    try:
        header = (
            "POST /api/legion/upload HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{scene['port']}\r\n"
            f"Origin: http://127.0.0.1:{scene['port']}\r\n"
            f"Authorization: Bearer {ALICE}\r\n"
            f"Content-Type: multipart/form-data; boundary={BOUNDARY}\r\n"
            "Content-Length: 4096\r\n\r\n")
        started = time.monotonic()
        sock.sendall(header.encode() + b"x")
        sender.start()
        response = http.client.HTTPResponse(sock)
        response.begin()
        raw = response.read()
        elapsed = time.monotonic() - started
        assert response.status == 400, raw
        assert 4.5 <= elapsed < 7
        assert scene["files"]() == before
    finally:
        stop.set()
        if sender.ident is not None:
            sender.join(timeout=1)
            assert not sender.is_alive()
        sock.close()


@pytest.mark.parametrize("filename", [
    f"report-{TICKET_ALICE}.txt", f"report.{TICKET_ALICE}",
], ids=["ticket-in-basename", "ticket-in-extension"])
@pytest.mark.parametrize("protocol", ["json", "multipart"])
@pytest.mark.parametrize("transport", ["header", "query"])
def test_active_ticket_in_upload_filename_reuses_json_rejection(
        scene, filename, protocol, transport):
    if protocol == "json":
        body, content_type = upload_body("json", filename=filename)
    else:
        body = multipart(files=[("files", filename, b"fixture")])
        content_type = f"multipart/form-data; boundary={BOUNDARY}"
    headers, query = {}, {"uid": "alice"}
    if transport == "header":
        headers["X-Legion-Ticket"] = TICKET_ALICE
    else:
        query["ticket"] = TICKET_ALICE
    before = scene["files"]()
    code, raw, _ = scene["request"]("/api/legion/upload?" + urlencode(query),
        body=body, content_type=content_type, token=None, headers=headers)
    assert code == 400, raw
    assert json.loads(raw) == {"error": "meyo_ticket_in_request_content"}
    assert scene["files"]() == before
    # The rejected input cache is not response data; fixture teardown checks its cleanup.
    assert TICKET_ALICE.encode() not in raw
    assert all(TICKET_ALICE not in repr({key: value for key, value in event.items()
                                       if key != "payload"}) for event in scene["responses"])


@pytest.mark.parametrize("filename", [
    f"report-{TICKET_ALICE}.txt", f"report.{TICKET_ALICE}",
], ids=["ticket-in-basename", "ticket-in-extension"])
def test_ticket_in_later_filename_rejects_before_any_file_write(scene, filename):
    body = multipart(files=[("files", "clean-first.bin", b"first"),
                            ("files", filename, b"second")])
    before = scene["files"]()
    code, raw, _ = scene["request"](body=body,
        content_type=f"multipart/form-data; boundary={BOUNDARY}", token=None,
        headers={"X-Legion-Ticket": TICKET_ALICE})
    assert code == 400, raw
    assert json.loads(raw) == {"error": "meyo_ticket_in_request_content"}
    assert scene["files"]() == before
    assert TICKET_ALICE.encode() not in raw
    assert all(TICKET_ALICE not in repr({key: value for key, value in event.items()
                                       if key != "payload"}) for event in scene["responses"])
