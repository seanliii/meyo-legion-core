"""Snapshot preview and fact handoff through the real Handler, without a socket.

Only synthetic files/identities in per-test temporary roots. No service, browser,
model, subprocess or original order data is used.
"""
from copy import deepcopy
from email.message import Message
import importlib
import io
import json
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from test_project_artifact_export import case, export, write, OWNER, SESSION, TEAM


CREATE = "/api/legion/artifact/preview"
OBSERVE = CREATE + "-observation"
PUBLIC = "https://legion.example.invalid"
OTHER = "another-owner"


@pytest.fixture(autouse=True)
def offline(case):
    def forbidden(*args, **kwargs):
        raise AssertionError("preview regression must not start services, threads or network")

    case.mp.setattr(socket.socket, "bind", forbidden)
    case.mp.setattr(socket.socket, "connect", forbidden)
    case.mp.setattr(threading.Thread, "start", forbidden)
    case.mp.setenv("LEGION_API_TOKEN", "fixture-instance-not-personal")
    case.mp.setenv("LEGION_PERSONAL_ACCOUNTS", "0")
    server = importlib.import_module("server")
    case.mp.setattr(server, "_PUBLIC_ORIGIN", None)
    case.mp.setattr(server, "_REMOTE_BIND_OK", False)
    if hasattr(case.ad, "_PREVIEW_GRANTS"):
        case.mp.setattr(case.ad, "_PREVIEW_GRANTS", {})
    case.server = server
    case.sessions = importlib.import_module("sessions")
    tokens = json.loads(case.tokens.read_text())
    tokens["fixture-other-personal-token"] = {"user": OTHER}
    case.tokens.write_text(json.dumps(tokens))
    return case


def request(case, method, path, *, body=None, authenticated=True,
            host="127.0.0.1:60929", origin="http://127.0.0.1:60929",
            token="fixture-personal-token", cookie=False, extra=(),
            remote="127.0.0.1", bound="127.0.0.1", content_type="application/json"):
    """Exercise dispatch/auth/body parsing/response methods, only replacing I/O."""
    handler = case.server.Handler.__new__(case.server.Handler)
    handler.server = SimpleNamespace(server_address=(bound, 60929),
                                     api_token="fixture-instance-not-personal")
    handler.client_address = (remote, 32100)
    handler.command, handler.path = method, path
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.raw_requestline = (handler.requestline + "\r\n").encode()
    handler.close_connection = False
    handler.headers = Message()
    handler.headers["Host"] = host
    if origin is not None:
        handler.headers["Origin"] = origin
    if authenticated:
        if cookie:
            handler.headers["Cookie"] = "legion_personal=" + token
        else:
            handler.headers["Authorization"] = "Bearer " + token
    raw = b"" if body is None else json.dumps(body).encode()
    if body is not None:
        handler.headers["Content-Type"] = content_type
        handler.headers["Content-Length"] = str(len(raw))
    for key, value in extra:
        handler.headers[key] = value
    handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
    handler.connection = SimpleNamespace(gettimeout=lambda: None, settimeout=lambda _: None)
    getattr(handler, "do_" + method)()
    head, data = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = dict(line.split(": ", 1) for line in lines[1:])
    result = json.loads(data) if data and "application/json" in headers.get("Content-Type", "") else data
    return SimpleNamespace(status=status, headers=headers, body=result, raw=data)


def save(case, rows, *, team=TEAM, sid=SESSION, user=OWNER):
    case.sessions.save_turn(
        sid, "synthetic original request",
        {"team_id": team, "status": "done", "results": [{"artifacts": rows}]},
        mode="single", user_id=user)


@pytest.fixture
def site(offline):
    case = offline
    write(case, "index.html", b'<!doctype html><html><head></head><body>'
          b'<script type="module" src="assets/app.js"></script></body></html>')
    write(case, "assets/app.js", b'import "./dep.js";\nexport const ready = true;\n')
    write(case, "assets/dep.js", b'export const dependency = 1;\n')
    rows = export(case, ["."])["artifacts"]
    assert {row["name"] for row in rows} == {"index.html", "app.js", "dep.js"}
    save(case, rows)
    case.rows = {row["name"]: row for row in rows}
    case.entry = case.rows["index.html"]
    return case


def mint(site, **kwargs):
    response = request(site, "POST", CREATE, body={"sid": SESSION, "url": site.entry["url"]},
                       **kwargs)
    assert response.status == 200, response.body
    return response.body


def resource_url(grant, row):
    prefix = grant["preview_url"].split("/home-outputs/", 1)[0]
    return prefix + row["url"]


def observe(site, grant, observation=None, **kwargs):
    return request(site, "POST", OBSERVE, body={
        "sid": SESSION, "url": site.entry["url"],
        "observation_token": grant["observation_token"],
        "observation": observation or {
            "kind": "resource_error", "message": "Resource failed to load",
            "resource": resource_url(grant, site.rows["app.js"])},
    }, **kwargs)


def test_cookie_parent_mints_without_session_write_and_module_get_needs_no_cookie(site):
    session_before = Path(site.sessions._path(SESSION)).read_bytes()
    original = Path(site.entry["path"]).read_bytes()
    grant = mint(site, cookie=True)
    assert grant["ok"] is True
    assert grant["snapshot_id"] == site.entry["export_snapshot_id"]
    assert grant["preview_url"].startswith("/artifact-preview/")
    assert grant["preview_url"].endswith(site.entry["url"])
    assert grant["preview_nonce"] and grant["observation_token"]
    assert grant["observation_token"] not in grant["preview_url"]
    assert Path(site.sessions._path(SESSION)).read_bytes() == session_before
    for name in ("app.js", "dep.js"):
        response = request(site, "GET", resource_url(grant, site.rows[name]),
                           authenticated=False, origin="null")
        assert response.status == 200, response.body
        assert response.raw == Path(site.rows[name]["path"]).read_bytes()
        assert response.headers["Access-Control-Allow-Origin"] == "*"
        assert "Access-Control-Allow-Credentials" not in response.headers
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["Cache-Control"] == "no-store"
        assert "javascript" in response.headers["Content-Type"]
    assert Path(site.entry["path"]).read_bytes() == original


def test_real_personal_fetch_wrapper_pins_uid_on_preview_and_observation(site):
    # index_v2's shared fetch wrapper pins the current subject in both places.
    # The preview must retain that wrapper, not bypass its identity/epoch checks.
    response = request(site, "POST", CREATE + "?uid=" + OWNER, cookie=True,
                       body={"sid": SESSION, "url": site.entry["url"], "uid": OWNER})
    assert response.status == 200, response.body
    grant = response.body
    response = request(site, "POST", OBSERVE + "?uid=" + OWNER, cookie=True, body={
        "sid": SESSION, "url": site.entry["url"], "uid": OWNER,
        "observation_token": grant["observation_token"],
        "observation": {"kind": "http_error", "resource": "/api/generate", "status": 403},
    })
    assert response.status == 200, response.body
    spoofed = request(site, "POST", CREATE, cookie=True,
                      body={"sid": SESSION, "url": site.entry["url"], "uid": OTHER})
    assert spoofed.status == 403


def test_html_uses_nonce_and_parent_origin_but_download_keeps_original_bytes(site):
    grant = mint(site)
    original = Path(site.entry["path"]).read_bytes()
    calls = []
    original_prepare = site.ad.prepare_html_preview

    def prepare(raw, *, preview_nonce="", preview_parent_origin=""):
        calls.append((raw, preview_nonce, preview_parent_origin))
        return original_prepare(raw)

    site.mp.setattr(site.ad, "prepare_html_preview", prepare)
    preview = request(site, "GET", grant["preview_url"], authenticated=False, origin="null")
    assert preview.status == 200
    assert calls == [(original, grant["preview_nonce"], "http://127.0.0.1:60929")]
    assert "sandbox allow-scripts" in preview.headers["Content-Security-Policy"]
    assert "allow-same-origin" not in preview.headers["Content-Security-Policy"]
    assert grant["observation_token"].encode() not in preview.raw
    for url in (grant["preview_url"], site.entry["url"]):
        response = request(site, "GET", url + "?download=1",
                           authenticated=url == site.entry["url"], origin=None)
        assert response.status == 200
        assert response.raw == original
        assert response.headers["Content-Disposition"].startswith("attachment")
    assert len(calls) == 1
    assert Path(site.entry["path"]).read_bytes() == original


@pytest.mark.parametrize("name,content,content_type", [
    ("active.svg", b'<svg xmlns="http://www.w3.org/2000/svg">'
     b'<script>window.previewFixture = 1;</script></svg>', "image/svg+xml"),
    ("active.xhtml", b'<html xmlns="http://www.w3.org/1999/xhtml">'
     b'<script>window.previewFixture = 1;</script></html>', "application/xhtml+xml"),
    ("document.xml", b'<?xml version="1.0"?><fixture/>', "application/xml"),
])
def test_other_snapshot_document_types_keep_opaque_sandbox(site, name, content, content_type):
    # These resources can be navigated as documents, not only loaded as assets.
    write(site, name, content)
    rows = export(site, ["."])["artifacts"]
    save(site, rows)
    site.rows = {row["name"]: row for row in rows}
    site.entry = site.rows["index.html"]
    grant = mint(site)
    url = resource_url(grant, site.rows[name])
    response = request(site, "GET", url, authenticated=False, origin=None)
    assert response.status == 200
    assert response.headers["Content-Type"] == content_type
    assert response.raw == content
    assert "sandbox allow-scripts" in response.headers.get("Content-Security-Policy", "")
    assert "allow-same-origin" not in response.headers["Content-Security-Policy"]
    download = request(site, "GET", url + "?download=1", authenticated=False, origin=None)
    assert download.status == 200 and download.raw == content
    assert download.headers["Content-Disposition"].startswith("attachment")


@pytest.mark.parametrize("path", [CREATE, OBSERVE])
@pytest.mark.parametrize("extra", [(), (("X-Forwarded-Proto", "https"),)])
def test_local_preview_parent_rejects_different_scheme(site, path, extra):
    grant = mint(site)
    body = {"sid": SESSION, "url": site.entry["url"]}
    if path == OBSERVE:
        body.update({
            "observation_token": grant["observation_token"],
            "observation": {"kind": "network_error", "resource": "/api/generate"},
        })
    response = request(site, "POST", path, body=body, cookie=True,
                       origin="https://127.0.0.1:60929", extra=extra)
    assert response.status == 403
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize("origin", [None, "null", "http://evil.invalid"])
def test_parent_post_requires_explicit_same_origin_even_with_cookie(site, origin):
    response = request(site, "POST", CREATE,
                       body={"sid": SESSION, "url": site.entry["url"]},
                       cookie=True, origin=origin)
    assert response.status == 403


@pytest.mark.parametrize("mutation", ["tampered", "expired", "restart", "observation_as_read"])
def test_capabilities_reject_tamper_expiry_restart_and_wrong_purpose(site, mutation):
    grant = mint(site)
    url = resource_url(grant, site.rows["app.js"])
    if mutation == "tampered":
        parts = url.split("/")
        parts[2] = ("0" if parts[2][0] != "0" else "1") + parts[2][1:]
        url = "/".join(parts)
    elif mutation == "expired":
        now = site.ad.time.monotonic()
        site.mp.setattr(site.ad.time, "monotonic", lambda: now + 3600)
    elif mutation == "restart":
        site.mp.setattr(site.ad, "_PREVIEW_SIGNING_KEY", b"new-process-key" * 3)
    else:
        parts = url.split("/")
        parts[2] = grant["observation_token"]
        url = "/".join(parts)
    response = request(site, "GET", url, authenticated=False, origin="null")
    assert response.status in {403, 404, 410}
    assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.parametrize("attack", [
    "other_snapshot", "other_scope", "other_root", "traversal", "encoded_traversal",
    "double_encoded_traversal", "encoded_slash", "absolute", "unregistered",
])
def test_grant_never_expands_to_other_snapshot_root_or_undocumented_files(site, attack):
    grant = mint(site)
    row = deepcopy(site.rows["app.js"])
    if attack == "other_snapshot":
        write(site, "index.html", b"<html>next version</html>")
        newer = export(site, ["."])["artifacts"]
        save(site, newer)
        row = next(r for r in newer if r["name"] == "app.js")
    elif attack == "other_scope":
        row["url"] = row["url"].replace(row["url"].split("/")[3], "0" * 64, 1)
    elif attack == "other_root":
        row["url"] = row["url"].replace("/home-outputs/", "/outputs/", 1)
    elif attack == "absolute":
        row["url"] = "//etc/passwd"
    elif attack == "unregistered":
        sibling = Path(row["path"]).with_name("not-registered.js")
        sibling.write_text("not registered")
        row["url"] = row["url"].rsplit("/", 1)[0] + "/not-registered.js"
    else:
        suffix = {"traversal": "../app.js", "encoded_traversal": "%2e%2e/app.js",
                  "double_encoded_traversal": "%252e%252e/app.js",
                  "encoded_slash": "nested%2f..%2fapp.js"}[attack]
        row["url"] = row["url"].rsplit("/", 1)[0] + "/" + suffix
    response = request(site, "GET", resource_url(grant, row), authenticated=False, origin="null")
    assert response.status in {400, 403, 404}
    assert "Access-Control-Allow-Origin" not in response.headers


def test_manifest_membership_is_not_enough_without_exact_session_registration(site):
    rows = list(site.rows.values())
    unregistered = site.rows["dep.js"]
    # Keep the real export manifest, but register only the other two files.
    session = site.sessions.load_session(SESSION, user_id=OWNER)
    session["turns"][0]["results"][0]["artifacts"] = [
        row for row in rows if row["name"] != "dep.js"]
    Path(site.sessions._path(SESSION)).write_text(json.dumps(session))
    grant = mint(site)
    response = request(site, "GET", resource_url(grant, unregistered),
                       authenticated=False, origin="null")
    assert response.status == 403
    assert "Access-Control-Allow-Origin" not in response.headers


def test_changed_registered_bytes_and_symlink_are_denied(site):
    grant = mint(site)
    row = site.rows["app.js"]
    file = Path(row["path"])
    original = file.read_bytes()
    file.write_bytes(b"mutated export bytes")
    response = request(site, "GET", resource_url(grant, row), authenticated=False, origin="null")
    assert response.status == 409
    assert "Access-Control-Allow-Origin" not in response.headers
    outside = site.root / "outside.js"
    outside.write_bytes(original)
    file.unlink()
    file.symlink_to(outside)
    response = request(site, "GET", resource_url(grant, row), authenticated=False, origin="null")
    assert response.status in {403, 404}
    assert "Access-Control-Allow-Origin" not in response.headers


def test_missing_identity_wrong_owner_session_and_capability_as_post_identity_are_denied(site):
    grant = mint(site)
    capability = grant["preview_url"].split("/")[2]
    for kwargs in ({"authenticated": False}, {"token": "fixture-other-personal-token"},
                   {"token": capability}, {"token": grant["observation_token"]}):
        response = request(site, "POST", CREATE,
                           body={"sid": SESSION, "url": site.entry["url"]}, **kwargs)
        assert response.status in {401, 403}
    save(site, [], team="", sid="__ut_other_session")
    response = request(site, "POST", CREATE,
                       body={"sid": "__ut_other_session", "url": site.entry["url"]})
    assert response.status == 403
    response = request(site, "POST", grant["preview_url"], body={}, authenticated=False,
                       origin="null")
    assert response.status == 403


def test_non_export_registration_is_explicitly_unsupported_without_expanding_directory(site):
    legacy = site.root / "data/outputs/legacy.html"
    legacy.write_text("<html>legacy</html>")
    save(site, [{"path": str(legacy), "url": "/home-outputs/legacy.html"}])
    response = request(site, "POST", CREATE, body={"sid": SESSION, "url": "/home-outputs/legacy.html"})
    assert response.status == 422
    assert response.body["unsupported"] is True
    assert response.body["error_code"] == "preview_unsupported"


def test_public_proxy_allows_only_capability_get_opaque_origin_and_keeps_other_checks(site):
    site.mp.setattr(site.server, "_PUBLIC_ORIGIN", PUBLIC)
    grant = mint(site, host="legion.example.invalid", origin=PUBLIC)
    good = request(site, "GET", resource_url(grant, site.rows["app.js"]),
                   host="legion.example.invalid", origin="null", authenticated=False)
    assert good.status == 200 and good.headers["Access-Control-Allow-Origin"] == "*"
    for kwargs in (
        {"host": "attacker.invalid", "extra": [("X-Forwarded-Host", "legion.example.invalid")]},
        {"host": "legion.example.invalid", "remote": "192.0.2.30"},
        {"host": "legion.example.invalid", "bound": "0.0.0.0"},
        {"host": "legion.example.invalid", "extra": [("Host", "attacker.invalid")]},
        {"host": "legion.example.invalid", "extra": [("Origin", PUBLIC)]},
    ):
        denied = request(site, "GET", grant["preview_url"],
                         authenticated=False, origin="null", **kwargs)
        assert denied.status == 403
        assert "Access-Control-Allow-Origin" not in denied.headers
    for method, path, body in (("GET", "/api/legion/auth/status", None),
                               ("POST", CREATE, {"sid": SESSION, "url": site.entry["url"]}),
                               ("OPTIONS", CREATE, None)):
        response = request(site, method, path, body=body, host="legion.example.invalid", origin="null")
        assert response.status == 403


def test_preview_posts_do_not_exchange_tickets_or_accept_extra_identity_fields(site):
    import personal_identity

    def no_exchange(*args, **kwargs):
        raise AssertionError("preview must not consume an upstream ticket")

    site.mp.setattr(personal_identity, "ticket_user", no_exchange)
    body = {"sid": SESSION, "url": site.entry["url"]}
    response = request(site, "POST", CREATE + "?ticket=fixture-upstream-ticket", body=body,
                       extra=[("X-Legion-Ticket", "fixture-upstream-ticket")])
    assert response.status == 200, response.body
    for field in ("ticket", "team_id", "workspace_root"):
        response = request(site, "POST", CREATE, body=body | {field: "untrusted-override"})
        assert response.status == 400
    response = request(site, "POST", CREATE, body=body, authenticated=False,
                       extra=[("X-Legion-Ticket", "fixture-upstream-ticket")])
    assert response.status == 403


@pytest.mark.parametrize("kwargs,status", [
    ({"content_type": "text/plain"}, 415),
    ({"content_type": "application/x-www-form-urlencoded"}, 415),
    ({"extra": [("Content-Length", "1")]}, 413),
    ({"extra": [("Transfer-Encoding", "chunked")]}, 413),
    ({"extra": [("Origin", "http://127.0.0.1:60929")]}, 403),
    ({"extra": [("Host", "127.0.0.1:60929")]}, 403),
    ({"origin": "file://127.0.0.1:60929"}, 403),
])
def test_preview_posts_require_one_json_body_and_unambiguous_transport(site, kwargs, status):
    response = request(site, "POST", CREATE,
                       body={"sid": SESSION, "url": site.entry["url"]}, **kwargs)
    assert response.status == status


def test_preview_grant_is_bound_to_parent_host_and_failure_is_not_cors_enabled(site):
    grant = mint(site)
    denied = request(site, "GET", grant["preview_url"],
                     authenticated=False, host="localhost:60929", origin="null")
    assert denied.status == 403
    assert "Access-Control-Allow-Origin" not in denied.headers
    site.mp.setattr(site.ad, "read_preview_resource",
                    lambda *args: (_ for _ in ()).throw(RuntimeError("/private/secret fixture")))
    failed = request(site, "GET", grant["preview_url"], authenticated=False, origin="null")
    assert failed.status == 503 and failed.body["error_code"] == "preview_unavailable"
    assert b"/private/" not in failed.raw
    assert "Access-Control-Allow-Origin" not in failed.headers


def test_observation_is_versioned_in_latest_legal_project_and_visible_to_continuation(site):
    grant = mint(site)
    old_before = deepcopy(site.pc.load(TEAM))
    current = "__ut_latest_project"
    site.pc.initialize(current, "current objective", {
        "tasks": {"maker": {"task": "continue original project"}},
        "project_context": {"repo_root": str(site.work)}},
        mode="single", user_id=OWNER, conversation_id=SESSION)
    site.pc.record_verification(current, {"report_id": "trusted-evidence", "role": "native_test",
                                         "message": "preserve original verification"})
    save(site, [], team=current)
    # A newer foreign packet must not be used just because the client named it.
    foreign = "__ut_foreign_project"
    site.pc.initialize(foreign, "other", {"tasks": {}}, mode="single",
                       user_id=OTHER, conversation_id=SESSION)
    save(site, [], team=foreign)
    before = deepcopy(site.pc.load(current))
    session_bytes = Path(site.sessions._path(SESSION)).read_bytes()
    result = observe(site, grant)
    assert result.status == 200, result.body
    assert result.body["team_id"] == current
    packet = site.pc.load(current)
    report = next(r for r in packet["verification_findings"]
                  if r["report_id"] == result.body["report_id"])
    assert report["role"] == "artifact_preview_observer"
    assert report["trust"] == "untrusted_browser_observation"
    observation, = report["observations"]
    assert observation["observed_snapshot_id"] == grant["snapshot_id"]
    assert observation["source_team_id"] == TEAM and observation["source_turn"] == 1
    assert observation["kind"] == "resource_error" and observation["resource"] == "assets/app.js"
    assert site.pc.load(TEAM) == old_before
    for key in ("quality_context", "current_stage", "decisions", "artifacts", "completed", "open_gaps"):
        assert packet.get(key) == before.get(key)
    assert Path(site.sessions._path(SESSION)).read_bytes() == session_bytes
    continuation = site.sessions.continuation_project_context(SESSION, user_id=OWNER)
    assert continuation["continued_from_team_id"] == current
    assert report in continuation["verification_findings"]


def test_observation_is_bounded_deduplicated_and_cannot_flush_other_facts(site):
    grant = mint(site)
    for i in range(5):
        site.pc.record_verification(TEAM, {"report_id": f"real-{i}", "role": "native_test"})
    first = observe(site, grant)
    assert first.status == 200
    after = deepcopy(site.pc.load(TEAM))
    for _ in range(5):
        assert observe(site, grant).body == first.body
    assert site.pc.load(TEAM) == after, "duplicates must not rewrite/promote reports"
    for index in range(100):
        response = observe(site, grant, {
            "kind": "http_error", "message": "HTTP request failed", "status": 503,
            "resource": f"assets/missing-{index}.js"})
        assert response.status in {200, 429}
    packet = site.pc.load(TEAM)
    report, = [r for r in packet["verification_findings"]
               if r.get("role") == "artifact_preview_observer"]
    assert len(report["observations"]) <= 24
    assert {f"real-{i}" for i in range(5)} <= {r["report_id"] for r in packet["verification_findings"]}
    # A new view must aggregate into the same bounded project report.
    result = observe(site, mint(site))
    assert result.status in {200, 429}
    assert len([r for r in site.pc.load(TEAM)["verification_findings"]
                if r.get("role") == "artifact_preview_observer"]) == 1


@pytest.mark.parametrize("kind", ["script_error", "resource_error", "unhandled_rejection",
                                 "network_error", "http_error"])
def test_observations_strip_capability_queries_fragments_secrets_and_host_paths(site, kind):
    grant = mint(site)
    capability = grant["preview_url"].split("/")[2]
    observation = {
        "kind": kind, "message": "Failure at /Users/private-owner/secret.py?credential=bad#fragment "
                                + grant["observation_token"] + " Authorization: Bearer hidden-test-secret",
        "resource": "http://127.0.0.1:60929" + resource_url(grant, site.rows["app.js"])
                    + "?credential=bad#fragment",
        "status": 503, "line": 3, "column": 8,
        "body": "do not persist", "headers": {"Authorization": "hidden-test-secret"},
        "stack": "/private/secret/path", "verdict": "overwrite latest quality",
    }
    response = observe(site, grant, observation)
    assert response.status == 200, response.body
    report = next(r for r in site.pc.load(TEAM)["verification_findings"]
                  if r["report_id"] == response.body["report_id"])
    encoded = json.dumps(report)
    for sensitive in (capability, grant["observation_token"], grant["preview_nonce"],
                      "/Users/", "/private/", "?credential", "#fragment",
                      "hidden-test-secret", "do not persist", "overwrite latest quality"):
        assert sensitive not in encoded
    fact, = report["observations"]
    assert fact["resource"] == "assets/app.js" and fact["status"] == 503
    assert len(fact["message"]) <= 400
    assert not any(key in fact for key in ("body", "headers", "stack", "verdict"))


def test_observation_token_cannot_cross_owner_sid_url_or_be_used_as_read_token(site):
    grant = mint(site)
    assert observe(site, grant, token="fixture-other-personal-token").status == 403
    assert observe(site, grant, authenticated=False).status == 403
    bad = {**grant, "observation_token": grant["preview_url"].split("/")[2]}
    assert observe(site, bad).status == 403
    good_url = site.entry["url"]
    site.entry = {**site.entry, "url": site.rows["app.js"]["url"]}
    assert observe(site, grant).status == 403
    site.entry = {**site.entry, "url": good_url}
    assert not site.pc.load(TEAM).get("verification_findings")


def _direct_preview_grant(site):
    """Exercise the service helpers while the HTTP routes are wired separately."""
    return site.ad.create_preview_grant(
        OWNER, SESSION, site.entry["url"], parent_origin="http://127.0.0.1:60929")


def _direct_preview_observe(site, grant, observation):
    return site.ad.record_preview_observation(
        OWNER, SESSION, site.entry["url"], grant["observation_token"], observation)


def test_preview_rolling_retention_full_old_report_accepts_new_version_in_continuation(site):
    # Keep the entire other-report budget occupied: observer rotation must never
    # evict or rewrite any of these independently recorded verification facts.
    for index in range(79):
        site.pc.record_verification(
            TEAM, {"report_id": f"native-{index}", "role": "native_test", "value": index})
    old_snapshot = site.entry["export_snapshot_id"]
    for view in range(2):
        grant = _direct_preview_grant(site)
        for index in range(12):
            _direct_preview_observe(site, grant, {
                "kind": "resource_error", "resource": f"assets/missing-{view}-{index}.js"})
        after = deepcopy(site.pc.load(TEAM))
        _direct_preview_observe(site, grant, {
            "kind": "resource_error", "resource": f"assets/missing-{view}-0.js"})
        assert site.pc.load(TEAM) == after, "a duplicate must not rewrite or refresh reports"
        with pytest.raises(site.ad.PreviewError) as limited:
            _direct_preview_observe(site, grant, {
                "kind": "resource_error", "resource": f"assets/over-limit-{view}.js"})
        assert limited.value.code == "preview_observation_limit"
    old_packet = deepcopy(site.pc.load(TEAM))
    old_report = next(row for row in old_packet["verification_findings"]
                      if row["role"] == "artifact_preview_observer")
    assert len(old_report["observations"]) == 24
    assert {row["observed_snapshot_id"] for row in old_report["observations"]} == {old_snapshot}

    current = "__ut_preview_next_version"
    site.pc.initialize(current, "continue original project", {
        "tasks": {"maker": {"task": "continue original project"}},
        "project_context": site.sessions.continuation_project_context(SESSION, user_id=OWNER),
    }, mode="single", user_id=OWNER, conversation_id=SESSION)
    write(site, "index.html", b"<!doctype html><html><body>new exported version</body></html>")
    newer = export(site, ["."], team_id=current)["artifacts"]
    save(site, newer, team=current)
    site.entry = next(row for row in newer if row["name"] == "index.html")
    grant = _direct_preview_grant(site)
    assert grant["snapshot_id"] != old_snapshot
    before = deepcopy(site.pc.load(current))
    session_bytes = Path(site.sessions._path(SESSION)).read_bytes()
    observation = {"kind": "http_error", "resource": "/api/generate?key=fixture#private", "status": 503}

    receipt = _direct_preview_observe(site, grant, observation)

    assert receipt["team_id"] == current
    continuation = site.sessions.continuation_project_context(SESSION, user_id=OWNER)
    report = next(row for row in continuation["verification_findings"]
                  if row["report_id"] == receipt["report_id"])
    assert len(report["observations"]) == 24
    assert report["observations"][:-1] == old_report["observations"][1:]
    fact = report["observations"][-1]
    assert fact["observed_snapshot_id"] == grant["snapshot_id"]
    assert fact["source_team_id"] == current and fact["source_turn"] == 2
    assert fact["resource"] == "/api/generate" and fact["status"] == 503
    assert continuation["continued_from_team_id"] == current
    packet = site.pc.load(current)
    assert [row for row in packet["verification_findings"]
            if row["report_id"] != receipt["report_id"]] == [
        row for row in before["verification_findings"] if row["report_id"] != receipt["report_id"]]
    for key in ("quality_context", "current_stage", "decisions", "artifacts", "completed", "open_gaps"):
        assert packet.get(key) == before.get(key)
    assert site.pc.load(TEAM) == old_packet
    assert Path(site.sessions._path(SESSION)).read_bytes() == session_bytes
    assert _direct_preview_observe(site, grant, observation) == receipt
    assert site.pc.load(current) == packet


@pytest.mark.parametrize("kind,resource,expected", [
    ("network_error", "/api/generate?key=fixture#private", "/api/generate"),
    ("http_error", "/api/generate/private-id?token=fixture", "/api/generate"),
    ("network_error", "http://127.0.0.1:60929/api/generate?key=fixture", "/api/generate"),
    ("network_error", "//127.0.0.1:60929/api/generate", "/api/generate"),
    ("script_error", "/api/generate?key=fixture", ""),
    ("resource_error", "/api/generate?key=fixture", ""),
    ("unhandled_rejection", "/api/generate?key=fixture", ""),
    ("network_error", "/api/x", ""),
    ("network_error", "/api/Generate", ""),
    ("network_error", "/api/generate_more", ""),
    ("network_error", "/api/generate2", ""),
    ("network_error", "/api/%67enerate", ""),
    ("network_error", "/api/private-action/another-id", ""),
    ("network_error", "/api/get-token", ""),
    ("http_error", "/api/secret", ""),
    ("http_error", "/api/credential", ""),
    ("http_error", "/api/password", ""),
    ("http_error", "/api/bearer", ""),
    ("http_error", "/api/api-key", ""),
    ("http_error", "/api/sk-example", ""),
    ("network_error", "https://remote.example.invalid/api/generate?key=fixture",
     "https://remote.example.invalid/"),
    ("network_error", "http://127.0.0.1:60930/api/generate", "http://127.0.0.1/"),
    ("network_error", "https://127.0.0.1:60929/api/generate", "https://127.0.0.1/"),
    ("network_error", "/Users/private-owner/secret.py?key=fixture", ""),
    ("network_error", "/private/var/tmp/secret.py", ""),
    ("resource_error", "assets/app.js?key=fixture#private", "assets/app.js"),
    ("resource_error", "assets/../app.js", ""),
    ("resource_error", "file:///Users/private-owner/secret.py", ""),
])
def test_preview_resource_policy_preserves_only_safe_network_route_prefix(site, kind, resource, expected):
    grant = _direct_preview_grant(site)
    receipt = _direct_preview_observe(site, grant, {
        "kind": kind, "resource": resource, "message": "ignored private browser text"})
    report = next(row for row in site.pc.load(TEAM)["verification_findings"]
                  if row["report_id"] == receipt["report_id"])
    fact, = report["observations"]
    assert fact["resource"] == expected
    assert "fixture" not in json.dumps(report)
    assert "ignored private browser text" not in json.dumps(report)


@pytest.mark.parametrize("variant,expected", [
    ("same_origin", "assets/app.js"),
    ("external_origin", "https://remote.example.invalid/"),
    ("external_nonce_host", ""),
    ("external_capability_host", ""),
    ("bound_path_traversal", ""),
    ("other_snapshot", ""),
])
def test_preview_resource_policy_snapshot_path_and_credential_redaction(site, variant, expected):
    grant = _direct_preview_grant(site)
    url = resource_url(grant, site.rows["app.js"])
    if variant == "same_origin":
        resource = "http://127.0.0.1:60929" + url + "?token=fixture#private"
    elif variant == "external_origin":
        resource = "https://remote.example.invalid" + url
    elif variant == "external_nonce_host":
        resource = "https://" + grant["preview_nonce"] + ".invalid/api/generate"
    elif variant == "external_capability_host":
        resource = "https://" + url.split("/")[2] + ".invalid/api/generate"
    elif variant == "bound_path_traversal":
        resource = url.rsplit("/", 1)[0] + "/../app.js"
    else:
        resource = url.replace(grant["snapshot_id"], "0" * 64)
    receipt = _direct_preview_observe(site, grant, {"kind": "network_error", "resource": resource})
    report = next(row for row in site.pc.load(TEAM)["verification_findings"]
                  if row["report_id"] == receipt["report_id"])
    assert report["observations"][0]["resource"] == expected
