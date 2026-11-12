"""Files 图片复用：假授权、本机 HTTP、真 bridge；不访问云或真实模型。"""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import copy
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time

import httpx
import pytest

from test_codex_output_budget_transport import (
    runtime, MODEL, KEY, LOCAL_TOKEN, NATIVE, capture_native, context, declare,
)

OPTION = {"profile": "deepseek-files-v1", "ttl_seconds": 3600}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aR1EAAAAASUVORK5CYII=")
URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


def image(detail="high"):
    value = {"type": "input_image", "image_url": URL}
    if detail is not None:
        value["detail"] = detail
    return value


def payload():
    return {"model": MODEL, "stream": True, "store": False,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "保留文本与顺序"}, image(), image("low"), image(None),
                {"type": "input_image", "image_url": "https://example.invalid/a.png", "detail": "high"}]},
                {"type": "reasoning", "encrypted_content": "opaque-unmodified"},
                {"type": "function_call", "call_id": "one", "name": "screenshot", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "one", "output": [image("original")]},
                {"type": "custom_tool_call", "call_id": "two", "name": "apply_patch", "input": "unchanged"},
                {"type": "custom_tool_call_output", "call_id": "two", "output": [image("auto")]},
                {"role": "developer", "content": [image("high")]},
                {"role": "assistant", "content": [image("high")]}],
            "tools": [{"type": "function", "name": "screenshot", "parameters": {"type": "object"}}]}


@contextmanager
def provider():
    seen, files = [], {}
    state = {"upload_status": 200, "get_status": 200, "response_status": 200}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data, content_type="application/json"):
            raw = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            seen.append({"method": "GET", "path": self.path,
                         "authorization": self.headers.get("Authorization")})
            if state["get_status"] != 200:
                return self.reply(state["get_status"], {"secret": KEY, "url": URL})
            self.reply(200, files[self.path.rsplit("/", 1)[-1]])

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            entry = {"method": "POST", "path": self.path,
                     "authorization": self.headers.get("Authorization")}
            seen.append(entry)
            if self.path.endswith("/files"):
                message = BytesParser(policy=default).parsebytes(
                    ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode() + raw)
                entry["parts"] = {part.get_param("name", header="content-disposition"):
                                  part.get_payload(decode=True) for part in message.iter_parts()}
                if state["upload_status"] != 200:
                    return self.reply(state["upload_status"], {"secret": KEY, "url": URL})
                fid = "file-api-" + str(len(files) + 1)
                obj = {"id": fid, "object": "file", "purpose": "user_data",
                       "bytes": len(entry["parts"]["file"]), "created_at": int(time.time()),
                       "expires_at": int(time.time()) + 3600}
                files[fid] = obj
                return self.reply(200, obj)
            entry["body"] = json.loads(raw)
            entry["raw"] = raw
            entry["headers"] = dict(self.headers)
            if state["response_status"] != 200:
                return self.reply(state["response_status"], {"error": {"code": "original_provider_error"}})
            rid, mid = "resp_fixture", "msg_fixture"
            item = {"id": mid, "type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "fixture acknowledgment"}]}
            events = [
                {"type": "response.created", "response": {"id": rid, "status": "in_progress", "output": []}},
                {"type": "response.output_item.added", "output_index": 0,
                 "item": {"id": mid, "type": "message", "role": "assistant", "content": []}},
                {"type": "response.output_text.delta", "item_id": mid, "output_index": 0,
                 "content_index": 0, "delta": "fixture acknowledgment"},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": rid, "status": "completed", "output": [item],
                    "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}}}]
            data = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                           for event in events).encode()
            self.reply(200, data, "text/event-stream")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", seen, state, files
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def owned_binding(case, base, user="fixture", **options):
    return {"model": MODEL, "base_url": base, "api_key": KEY, "http_headers": {},
            "stamp": {"sha256": case.mr.ccr_binding_key(MODEL, KEY, base)},
            "image_file_cache": {**OPTION, **options}, "image_file_cache_user": user,
            "image_file_cache_workdir": str(case.work)}


def post(active, body):
    with httpx.Client(timeout=10, trust_env=False) as client:
        return client.post(active["client_base_url"] + "/responses", json=body,
                           headers={"Authorization": "Bearer " + active["client_api_key"],
                                    "Digest": "old-representation", "Content-MD5": "old-representation"})


def enable(case, base="https://provider.example.invalid/agentic/v1", option=OPTION):
    raw = declare(case, base=base, cap=None)
    raw["image_file_cache"] = option
    case.mp.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps(raw))
    return raw


@pytest.mark.parametrize("option", [None, True, False, {}, {**OPTION, "ttl_seconds": True},
    {**OPTION, "ttl_seconds": 3599}, {**OPTION, "ttl_seconds": 2592001},
    {**OPTION, "default_detail": "low"}, {**OPTION, "profile": "guess"}, {**OPTION, "url": "bad"}])
def test_invalid_explicit_options_never_start_native(runtime, option):
    enable(runtime, option=option)
    calls, bridges = capture_native(runtime)
    with pytest.raises(ValueError) as error:
        runtime.mr.explicit_responses_binding()
    assert error.value.binding_option == "image_file_cache"
    result = runtime.hr._codex_adapter("__ut_files_bad", "fixture", ctx=context(runtime))
    assert result["status"] == "unavailable" and not calls and not bridges


def test_enable_only_on_matching_binding_and_preserve_checkpoint(runtime):
    case = runtime
    declare(case, cap=None)
    calls, bridges = capture_native(case)
    first = case.hr._codex_adapter("__ut_files_direct", "fixture", ctx=context(case))
    assert not bridges
    checkpoint = copy.deepcopy(first["native_checkpoint"])
    enable(case)
    second = case.hr._codex_adapter("__ut_files_resume", "fixture", ctx=context(case,
        task_memory={"native_checkpoint": checkpoint}))
    assert len(bridges) == 1 and bridges[0]["image_file_cache"] == OPTION
    assert bridges[0]["image_file_cache_user"] == "fixture"
    assert "resume" in calls[-1][0] and checkpoint["session_id"] in calls[-1][0]
    assert second["native_checkpoint"] == checkpoint == first["native_checkpoint"]
    case.hr._codex_adapter("__ut_files_other", "fixture", ctx=context(case, model="different"))
    assert len(bridges) == 1
    result = case.hr._codex_adapter("__ut_files_unverified", "fixture", ctx=context(case,
        task_memory={"native_checkpoint": {"session_id": "old", "provider_binding": "wrong"}}))
    assert result["status"] == "unavailable" and len(bridges) == 1 and len(calls) == 3


def test_real_http_preserves_parts_bytes_and_cache_across_bridge_restart(runtime):
    case = runtime
    original = payload()
    with provider() as (base, seen, state, files):
        for _ in range(2):
            with case.owned.transparent_bridge(owned_binding(case, base), dict(os.environ)) as active:
                response = post(active, original)
                assert response.status_code == 200, response.text
                assert "response.completed" in response.text
        assert [row["path"] for row in seen] == ["/v1/files", "/v1/responses", "/v1/files/file-api-1", "/v1/responses"]
        upload = seen[0]["parts"]
        assert upload == {"file": PNG, "purpose": b"user_data", "expires_after[anchor]": b"created_at",
                          "expires_after[seconds]": b"3600"}
        expected = copy.deepcopy(original)
        for part in (expected["input"][0]["content"][1], expected["input"][3]["output"][0],
                     expected["input"][5]["output"][0], expected["input"][6]["content"][0]):
            part["file_id"] = "file-api-1"
            del part["image_url"]
        assert seen[1]["body"] == seen[3]["body"] == expected
        assert all(row["authorization"] == "Bearer " + KEY for row in seen)
        assert "Digest" not in seen[1]["headers"] and "Content-MD5" not in seen[1]["headers"]
    assert original == payload(), "caller/rollout source must not be changed"
    records = list((case.root / "codex-home/meyo-image-files").rglob("*.json"))
    assert len(records) == 1
    text = records[0].read_text()
    assert KEY not in text and base64.b64encode(PNG).decode() not in text
    assert (records[0].stat().st_mode & 0o777) == 0o600
    assert (records[0].parent.stat().st_mode & 0o777) == 0o700


def test_default_detail_is_explicit_and_low_never_changes(runtime):
    with provider() as (base, seen, *_):
        with runtime.owned.transparent_bridge(owned_binding(runtime, base, default_detail="auto"), dict(os.environ)) as active:
            body = {"model": MODEL, "input": [{"role": "user", "content": [image(None), image("low")]}]}
            assert post(active, body).status_code == 200
        content = seen[-1]["body"]["input"][0]["content"]
        assert content == [{"type": "input_image", "file_id": "file-api-1"}, image("low")]


@pytest.mark.parametrize("change", ["user", "key", "model", "base"])
def test_cache_never_crosses_user_or_provider_binding(runtime, change):
    with provider() as (base, seen, *_):
        binding = owned_binding(runtime, base)
        for i in range(2):
            current = copy.deepcopy(binding)
            body = {"model": MODEL, "input": [{"role": "user", "content": [image()]}]}
            if i:
                field = {"user": "image_file_cache_user", "key": "api_key", "model": "model", "base": "base_url"}[change]
                current[field] += "-other" if change != "base" else "/other"
                body["model"] = current["model"]
            with runtime.owned.transparent_bridge(current, dict(os.environ)) as active:
                assert post(active, body).status_code == 200
        assert sum(row["method"] == "POST" and row["path"].endswith("/files") for row in seen) == 2


def test_concurrent_requests_upload_same_bytes_once(runtime):
    with provider() as (base, seen, *_):
        with ExitStack() as stack:
            active = [stack.enter_context(runtime.owned.transparent_bridge(
                owned_binding(runtime, base), dict(os.environ))) for _ in range(2)]
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda bridge: post(bridge, payload()), active))
            assert [r.status_code for r in responses] == [200, 200]
        assert sum(row["path"] == "/v1/files" for row in seen) == 1


def test_ttl_refresh_once_and_deleted_file_fails_without_hidden_resend(runtime):
    with provider() as (base, seen, state, files):
        with runtime.owned.transparent_bridge(owned_binding(runtime, base), dict(os.environ)) as active:
            assert post(active, payload()).status_code == 200
            record = next((runtime.root / "codex-home/meyo-image-files").rglob("*.json"))
            cached = json.loads(record.read_text())
            cached["cache_until"] = int(time.time()) - 1
            record.write_text(json.dumps(cached))
            assert post(active, payload()).status_code == 200
            assert sum(r["path"] == "/v1/files" for r in seen) == 2
            state["get_status"] = 404
            before = len(seen)
            failed = post(active, payload())
            assert failed.status_code == 424
            assert failed.json()["error"]["stage"] == "files_validate"
            assert len(seen) == before + 1 and not record.exists()
            assert KEY not in failed.text and URL not in failed.text


def test_shorter_ttl_uses_local_upload_time_even_with_server_clock_skew(runtime):
    with provider() as (base, seen, *_):
        with runtime.owned.transparent_bridge(owned_binding(runtime, base, ttl_seconds=7200), dict(os.environ)) as active:
            assert post(active, payload()).status_code == 200
        record = next((runtime.root / "codex-home/meyo-image-files").rglob("*.json"))
        cached = json.loads(record.read_text())
        now = int(time.time())
        cached.update(uploaded_at=now - 4000, created_at=now + 100,
                      cache_until=now + 2000, expires_at=now + 2000)
        record.write_text(json.dumps(cached))
        with runtime.owned.transparent_bridge(owned_binding(runtime, base), dict(os.environ)) as active:
            assert post(active, payload()).status_code == 200
        assert [row["path"] for row in seen] == ["/v1/files", "/v1/responses", "/v1/files", "/v1/responses"]


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_upload_failure_has_sanitized_stage_and_no_responses_or_retry(runtime, status):
    with provider() as (base, seen, state, files):
        state["upload_status"] = status
        with runtime.owned.transparent_bridge(owned_binding(runtime, base), dict(os.environ)) as active:
            response = post(active, payload())
        assert response.status_code == 424 and len(seen) == 1
        assert response.json()["error"] == {"type": "image_file_transport_error", "code": "files_http_error",
            "stage": "files_upload", "upstream_status": status, "message": "Image Files transport failed at files_upload"}
        assert not list((runtime.root / "codex-home/meyo-image-files").rglob("*.json"))


def test_network_error_does_not_expose_exception_url_or_body(runtime):
    seen = []
    def failure(request):
        seen.append(request)
        raise httpx.ReadTimeout(KEY + URL, request=request)
    raw = {"binding_id": "fixture", "model": MODEL, "base_url": "https://fixture.invalid/v1", "api_key": KEY,
           "client_token": LOCAL_TOKEN, "wire_api": "responses", "image_file_cache": OPTION,
           "image_file_cache_root": str(runtime.root / "cache"), "image_file_cache_user": "fixture"}
    runtime.bridge._configure_mode(runtime.bridge.app, {"MEYO_RESPONSES_BRIDGE_MODE": "passthrough",
        "MEYO_RESPONSES_BRIDGE_BINDING": json.dumps(raw)})
    original = httpx.AsyncClient
    runtime.mp.setattr(runtime.bridge.httpx, "AsyncClient", lambda **kw:
        original(transport=httpx.MockTransport(failure), **kw))
    class Request:
        headers = {"authorization": "Bearer " + LOCAL_TOKEN}
        async def body(self):
            return json.dumps(payload()).encode()
    response = asyncio.run(runtime.bridge._transparent_responses(Request(), runtime.bridge.app.state.responses_binding))
    assert response.status_code == 424 and len(seen) == 1
    assert json.loads(response.body)["error"]["code"] == "files_timeout"
    assert KEY.encode() not in response.body and URL.encode() not in response.body


def test_project_state_is_rejected_and_old_bridge_guard_is_unchanged(runtime):
    from meyo_harnesses.backends.omnigent_request_binding import NativeBindingError
    value = owned_binding(runtime, "https://fixture.invalid")
    with pytest.raises(NativeBindingError) as error:
        with runtime.owned.transparent_bridge(value, dict(os.environ), {"session_id": "old"}):
            pytest.fail("unverified saved bridge must not start")
    assert error.value.code == "native_bridge_unverified"
    with pytest.raises(NativeBindingError) as error:
        with runtime.owned.transparent_bridge(value, {**os.environ, "CODEX_HOME": str(runtime.work / ".codex")}):
            pytest.fail("project-writable cache must not start")
    assert error.value.code == "image_files_state_invalid"


def test_real_native_direct_to_files_resume_keeps_original_thread(runtime):
    assert NATIVE.is_file(), "需要已安装原生 CLI，不能静默跳过"
    with provider() as (base, seen, *_):
        declare(runtime, base=base, cap=None)
        first = runtime.hr._codex_adapter("__ut_files_native_first", "Acknowledge the fixture; do not use tools.",
                                         ctx=context(runtime, base=base))
        assert first["status"] == "ok", first
        checkpoint = copy.deepcopy(first["native_checkpoint"])
        enable(runtime, base=base)
        second = runtime.hr._codex_adapter("__ut_files_native_resume", "Acknowledge the fixture again; no tools.",
            ctx=context(runtime, base=base, task_memory={"native_checkpoint": checkpoint}))
    assert second["status"] == "ok" and second["resumed_native"], second
    assert second["native_checkpoint"] == checkpoint == first["native_checkpoint"]
    assert len(seen) == 2 and all(r["path"] == "/v1/responses" for r in seen)
    assert all(r["authorization"] == "Bearer " + KEY for r in seen)


def test_no_eligible_image_does_not_touch_files_or_cache(runtime):
    with provider() as (base, seen, *_):
        with runtime.owned.transparent_bridge(owned_binding(runtime, base), dict(os.environ)) as active:
            parts = [image("low"), image(None), {**image(), "file_id": "already-invalid-both"},
                     {"type": "input_image", "file_id": "file-api-existing", "detail": "low"}]
            body = {"model": MODEL, "input": [{"role": "user", "content": parts}]}
            assert post(active, body).status_code == 200
        assert len(seen) == 1 and seen[0]["body"] == body
        assert not (runtime.root / "codex-home/meyo-image-files").exists()


def test_corrupt_cache_is_reported_and_provider_error_is_not_rewritten(runtime):
    with provider() as (base, seen, state, files):
        with runtime.owned.transparent_bridge(owned_binding(runtime, base), dict(os.environ)) as active:
            state["response_status"] = 400
            response = post(active, payload())
            assert response.status_code == 400
            assert response.json() == {"error": {"code": "original_provider_error"}}
            record = next((runtime.root / "codex-home/meyo-image-files").rglob("*.json"))
            record.write_text('{"secret":"' + KEY + '"')
            before = len(seen)
            response = post(active, payload())
            assert response.status_code == 424 and len(seen) == before
            assert response.json()["error"]["code"] == "files_cache_invalid"
            assert KEY not in response.text


def test_symlink_cache_is_not_followed(runtime):
    (runtime.root / "codex-home/meyo-image-files").symlink_to(runtime.work, target_is_directory=True)
    with provider() as (base, seen, *_):
        with runtime.owned.transparent_bridge(owned_binding(runtime, base), dict(os.environ)) as active:
            response = post(active, payload())
        assert response.status_code == 424 and not seen
        assert response.json()["error"]["code"] == "files_cache_not_private"


def test_default_off_keeps_inline_bytes_and_never_creates_cache(runtime):
    with provider() as (base, seen, *_):
        value = owned_binding(runtime, base)
        del value["image_file_cache"]
        value["max_output_tokens"] = 4096  # Existing bridge use, no Files opt-in.
        body = {**payload(), "max_output_tokens": 4096}
        with runtime.owned.transparent_bridge(value, dict(os.environ)) as active:
            assert post(active, body).status_code == 200
        assert len(seen) == 1 and seen[0]["body"] == body
        assert seen[0]["raw"] == json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        assert not (runtime.root / "codex-home/meyo-image-files").exists()
