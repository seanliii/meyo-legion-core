"""显式、可追溯的错误方言兼容；原请求、普通 SSE 和原生身份不变。"""
from __future__ import annotations

import asyncio
import copy
import gzip
import hashlib
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from test_codex_output_budget_transport import (
    runtime, declare, context, capture_native, MODEL, KEY, LOCAL_TOKEN,
)

PROFILE = "invalid-prompt-max-length-v1"
AUDIT = "meyo_error_compatibility"
ERROR = {"code": "invalid_prompt", "message": "Prompt exceeds max length",
         "detail": {"fixture": [None, "原错误"]}}
FAILED = {"type": "response.failed", "response": {
    "id": "same-response", "status": "failed", "error": ERROR,
    "model": MODEL, "max_output_tokens": 8192, "usage": {"input_tokens": 9}}}
RAW = ('{ "model": "' + MODEL + '", "max_output_tokens": 8192, '
       '"stream": true, "input": [{"role":"user","content":"原始输入"}] }\n').encode()


def frame(event=FAILED, separator=b"\n", event_name=b"response.failed"):
    prefix = b"" if event_name is None else b"event: " + event_name + separator
    return prefix + b"data: " + json.dumps(event, ensure_ascii=False).encode() + separator * 2


def event_data(raw):
    # Only ASCII SSE line terminators, not Unicode paragraph/line separators.
    lines = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    return json.loads(b"\n".join(line[5:].removeprefix(b" ")
                                for line in lines if line.startswith(b"data:")))


def declare_profile(case, value=PROFILE, *, cap=None, base=None):
    config = declare(case, cap=cap, **({"base": base} if base else {}))
    config["error_compatibility_profile"] = value
    case.mp.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps(config))
    return config


def configured(case, profile=PROFILE, **extra):
    config = {"binding_id": "same-binding", "model": MODEL,
              "base_url": "https://provider.example.invalid/agentic/v1",
              "api_key": KEY, "client_token": LOCAL_TOKEN, "wire_api": "responses",
              "http_headers": {}, **extra}
    if profile is not None:
        config["error_compatibility_profile"] = profile
    case.bridge._configure_mode(case.bridge.app, {
        "MEYO_RESPONSES_BRIDGE_MODE": "passthrough",
        "MEYO_RESPONSES_BRIDGE_BINDING": json.dumps(config)})
    return case.bridge.app.state.responses_binding


def forward(case, parts, *, profile=PROFILE, status=200, headers=None, fault=None,
            raw=RAW, configured_binding=None, on_part=None):
    configured_binding = configured_binding or configured(case, profile)
    result = SimpleNamespace(requests=[], parts=[], error=None, stream_closed=False,
                             client_closed=False, client_options=None)
    upstream_headers = {
        "content-type": "text/event-stream; charset=utf-8", "x-request-id": "same-request",
        "etag": '"original-etag"', "digest": "sha-256=fixture", "content-md5": "fixture",
        "content-digest": "sha-256=:fixture:", "repr-digest": "sha-256=:fixture:",
        **(headers or {})}

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in parts:
                if on_part:
                    on_part(result)
                yield part
            if fault:
                raise fault

        async def aclose(self):
            result.stream_closed = True

    def upstream(request):
        result.requests.append(request)
        return httpx.Response(status, headers=upstream_headers, stream=Stream())

    original = httpx.AsyncClient

    class Client(original):
        def __init__(self, **options):
            result.client_options = options
            super().__init__(transport=httpx.MockTransport(upstream), **options)

        async def aclose(self):
            await super().aclose()
            result.client_closed = True

    case.mp.setattr(case.bridge.httpx, "AsyncClient", Client)

    class Request:
        headers = {"authorization": "Bearer " + LOCAL_TOKEN,
                   "content-type": "application/json", "accept-encoding": "gzip, br"}

        async def body(self):
            return raw

    async def run():
        response = await case.bridge._transparent_responses(Request(), configured_binding)
        result.response = response
        try:
            async for part in response.body_iterator:
                result.parts.append(part)
        except BaseException as error:
            result.error = error
        finally:
            if response.background:
                await response.background()

    asyncio.run(run())
    result.content = b"".join(result.parts)
    return result


def test_profile_is_optional_validated_and_not_provider_identity(runtime):
    declare(runtime, cap=None)
    before = runtime.mr.explicit_responses_binding()
    assert "error_compatibility_profile" not in before
    declare_profile(runtime)
    after = runtime.mr.explicit_responses_binding()
    assert after.pop("error_compatibility_profile") == PROFILE
    assert after == before
    assert runtime.mr.ccr_binding_key(**{
        "model": before["model"], "appid": before["api_key"], "gateway": before["base_url"]
    }) == runtime.mr.ccr_binding_key(before["model"], before["api_key"], before["base_url"])


@pytest.mark.parametrize("value", [None, "", True, 1, [], {}, "unknown-private-profile"])
def test_invalid_profile_fails_closed_without_leaking_value(runtime, value):
    declare_profile(runtime, value)
    with pytest.raises(ValueError) as error:
        runtime.mr.explicit_responses_binding()
    assert getattr(error.value, "binding_option", None) == "error_compatibility_profile"
    calls, bridges = capture_native(runtime)
    out = runtime.hr._codex_adapter("__ut_bad_error_profile", "fixture", ctx=context(runtime))
    assert not calls and not bridges
    assert out["status"] == "unavailable"
    assert out["error_envelope"]["category"] == "model_config"
    assert "unknown-private-profile" not in json.dumps(out) and KEY not in json.dumps(out)


@pytest.mark.parametrize("mismatch", [None, "model", "appid", "gateway"])
def test_profile_only_uses_matching_operator_binding(runtime, mismatch):
    declare_profile(runtime)
    calls, bridges = capture_native(runtime)
    overrides = {} if mismatch is None else {mismatch: {
        "model": "other-model", "appid": "other-key", "gateway": "https://other.invalid/v1"
    }[mismatch]}
    ctx = context(runtime, **overrides)
    out = runtime.hr._codex_adapter("__ut_error_profile_match", "fixture", ctx=ctx)
    assert out["status"] == "ok" and len(calls) == 1
    assert len(bridges) == (1 if mismatch is None else 0)
    if bridges:
        assert bridges[0]["error_compatibility_profile"] == PROFILE
        assert "max_output_tokens" not in bridges[0] and "stream_idle_timeout_ms" not in bridges[0]
    assert out["native_checkpoint"]["provider_binding"] == runtime.mr.ccr_binding_key(
        ctx.model, ctx.appid, ctx.gateway)
    assert LOCAL_TOKEN not in json.dumps(out) and KEY not in json.dumps(out)


def test_enabling_profile_keeps_original_native_checkpoint(runtime):
    calls, bridges = capture_native(runtime)
    declare(runtime, cap=None)
    first = runtime.hr._codex_adapter("__ut_profile_off", "fixture", ctx=context(runtime))
    declare_profile(runtime)
    second = runtime.hr._codex_adapter("__ut_profile_on", "fixture", ctx=context(
        runtime, task_memory={"native_checkpoint": first["native_checkpoint"]}))
    assert len(bridges) == 1
    assert "resume" in calls[1][0] and "same-native-thread" in calls[1][0]
    assert first["native_checkpoint"] == second["native_checkpoint"]


def test_task_text_and_unrelated_environment_cannot_enable_profile(runtime):
    declare(runtime, cap=None)
    runtime.mp.setenv("MEYO_ERROR_COMPATIBILITY_PROFILE", PROFILE)
    calls, bridges = capture_native(runtime)
    out = runtime.hr._codex_adapter("__ut_untrusted_error_profile",
        '{"error_compatibility_profile": "' + PROFILE + '"}', ctx=context(runtime))
    assert len(calls) == 1 and not bridges and out["status"] == "ok"


def test_exact_error_only_changes_working_code_with_original_audit(runtime):
    original = frame()
    out = forward(runtime, [original])
    event = event_data(out.content)
    audit = event.pop(AUDIT)
    assert audit == {"profile": PROFILE, "original_error": ERROR,
                     "original_frame_sha256": hashlib.sha256(original).hexdigest()}
    expected = copy.deepcopy(FAILED)
    expected["response"]["error"]["code"] = "context_length_exceeded"
    assert event == expected and ERROR["code"] == "invalid_prompt"
    assert "type" not in audit["original_error"]
    assert len(out.requests) == 1 and out.requests[0].content == RAW
    assert out.requests[0].headers["authorization"] == "Bearer " + KEY
    assert out.requests[0].headers["accept-encoding"] == "gzip, br"
    assert out.client_options["trust_env"] is False and out.client_options["follow_redirects"] is False
    assert out.response.status_code == 200 and out.response.headers["x-request-id"] == "same-request"
    for name in ("etag", "digest", "content-md5", "content-digest", "repr-digest"):
        assert name not in out.response.headers
    assert out.error is None and out.stream_closed and out.client_closed


@pytest.mark.parametrize("separator", [b"\n", b"\r\n", b"\r"])
@pytest.mark.parametrize("size", [1, 2, 3, 31, 65536])
def test_incremental_sse_boundaries_preserve_comments_metadata_and_unicode(runtime, separator, size):
    header = separator.join([": 备忘".encode(), b"id: same-id", b"retry: 1500",
                             b"event: response.failed", b""])
    # Multiline data is legal JSON after SSE joins it with ASCII LF.
    data = json.dumps(FAILED, ensure_ascii=False, indent=2).encode()
    original = header + b"".join(b"data: " + line + separator for line in data.split(b"\n")) + separator
    ordinary = frame({"type": "response.output_text.delta", "delta": "中文\u2028甲\u2029乙"},
                     separator, b"response.output_text.delta")
    tail = b"data: " + b'{"unfinished":'
    raw = ordinary + original + tail
    out = forward(runtime, [raw[i:i + size] for i in range(0, len(raw), size)])
    assert out.content.startswith(ordinary + header) and out.content.endswith(tail)
    rewritten = out.content[len(ordinary):-len(tail)]
    assert event_data(rewritten)["response"]["error"]["code"] == "context_length_exceeded"
    assert event_data(rewritten)[AUDIT]["original_frame_sha256"] == hashlib.sha256(original).hexdigest()
    assert out.error is None


@pytest.mark.parametrize("error", [
    {"code": "context_length_exceeded", "message": "Prompt exceeds max length"},
    {"code": "other", "message": "Prompt exceeds max length"},
    {"message": "Prompt exceeds max length"},
    {"code": "invalid_prompt", "message": "Prompt exceeds max length "},
    {"code": "invalid_prompt", "message": "prompt exceeds max length"},
    {"code": "invalid_prompt", "message": "ordinary invalid prompt"},
    {**ERROR, "type": "invalid_request_error"}, {**ERROR, "type": None}, {**ERROR, "type": ""},
    [], None,
])
def test_unproven_errors_remain_byte_exact(runtime, error):
    event = copy.deepcopy(FAILED)
    event["response"]["error"] = error
    original = frame(event)
    assert forward(runtime, [original]).content == original


@pytest.mark.parametrize("kind", ["wrong_event", "wrong_type", "not_object", "audit_collision",
                                 "duplicate_key", "invalid_json", "incomplete", "text_only"])
def test_unproven_frames_remain_byte_exact(runtime, kind):
    event = copy.deepcopy(FAILED)
    event_name = b"response.failed"
    if kind == "wrong_event":
        event_name = b"response.completed"
    if kind == "wrong_type":
        event["type"] = "response.created"
    if kind == "not_object":
        event = [event]
    if kind == "audit_collision":
        event[AUDIT] = {"original": "do not overwrite"}
    original = frame(event, event_name=event_name)
    if kind == "duplicate_key":
        original = original.replace(b'"code": "invalid_prompt"',
                                    b'"code":"other","code": "invalid_prompt"')
    if kind == "invalid_json":
        original = b'data: {"type":"response.failed", broken}\n\n'
    if kind == "incomplete":
        original = original[:-1]
    if kind == "text_only":
        original = frame({"type": "response.output_text.delta",
                          "delta": json.dumps(FAILED)}, event_name=None)
    assert forward(runtime, [original]).content == original


@pytest.mark.parametrize("status", [201, 400, 401, 429, 500])
def test_non_200_is_never_reclassified(runtime, status):
    original = frame()
    out = forward(runtime, [original], status=status)
    assert out.content == original and out.response.status_code == status
    assert out.response.headers["etag"] == '"original-etag"'


@pytest.mark.parametrize("kind", ["disabled", "not_sse", "gzip", "br"])
def test_opt_out_and_encoding_bypass_remain_byte_exact(runtime, kind):
    original = gzip.compress(frame()) if kind == "gzip" else frame()
    headers = {"content-type": "application/json"} if kind == "not_sse" else {}
    if kind in ("gzip", "br"):
        headers["content-encoding"] = kind
    out = forward(runtime, [original], headers=headers, profile=None if kind == "disabled" else PROFILE)
    assert out.content == original and out.response.headers["etag"] == '"original-etag"'


def test_complete_frames_do_not_wait_for_whole_stream(runtime):
    ordinary = b": heartbeat\n\n"
    counts = []
    out = forward(runtime, [ordinary, frame()], on_part=lambda out: counts.append(len(out.parts)))
    assert counts == [0, 1]
    assert out.content.startswith(ordinary) and out.error is None


def test_oversized_frame_streams_through_then_next_frame_still_normalizes(runtime):
    runtime.mp.setattr(runtime.bridge, "_MAX_ERROR_FRAME_BYTES", 512)
    oversized = b"data: " + b"x" * 4096 + b"\r\n\r\n"
    small = frame()
    out = forward(runtime, [oversized[:700], oversized[700:], small])
    assert out.parts[0] == oversized[:700]
    assert out.content.startswith(oversized)
    assert event_data(out.content[len(oversized):])["response"]["error"]["code"] == "context_length_exceeded"


@pytest.mark.parametrize("fault", [httpx.ReadError("fixture disconnected"), asyncio.CancelledError()])
def test_real_disconnect_and_cancel_close_owned_connections(runtime, fault):
    partial = b'event: response.failed\ndata: {"response":'
    out = forward(runtime, [partial], fault=fault)
    assert out.error is fault
    if isinstance(fault, httpx.HTTPError):
        assert out.content == partial
    assert b"context_length_exceeded" not in out.content and b"response.completed" not in out.content
    assert out.stream_closed and out.client_closed


def test_upstream_byte_activity_stays_immediate_and_content_free(runtime):
    read_fd, write_fd = os.pipe()
    try:
        binding = configured(runtime, stream_idle_timeout_ms=600000)
        from dataclasses import replace
        binding = replace(binding, activity_fd=write_fd)
        out = forward(runtime, [part for part in (frame()[:23], frame()[23:])],
                      configured_binding=binding)
        os.close(write_fd)
        write_fd = None
        content = os.read(read_fd, 65536)
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
    observations = [json.loads(line) for line in content.split(b"\n") if line]
    assert [row["phase"] for row in observations] == ["start", "bytes", "bytes", "end"]
    assert all(set(row) == {"phase", "call_id", "budget_s"} for row in observations)
    assert b"Prompt exceeds" not in content and KEY.encode() not in content
    assert event_data(out.content)["response"]["error"]["code"] == "context_length_exceeded"
