"""显式 Codex 输出预算：真实原生环回 + 现有透明桥，无真实模型或浏览器。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import tomllib

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
BRIDGE = SERVICE.parents[1] / "harness_baselines/comparison/responses_chat_bridge.py"
NATIVE = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
MODEL = "fixture-output-budget"
KEY = "fixture-upstream-only"
LOCAL_TOKEN = "fixture-local-only"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "ANTHROPIC_", "CODEX_")):
            monkeypatch.delenv(key)
    for key, leaf in (
        ("HOME", "home"), ("CODEX_HOME", "codex-home"), ("TMPDIR", "tmp"),
        ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
        ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        root = tmp_path / leaf
        root.mkdir()
        monkeypatch.setenv(key, str(root))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    monkeypatch.setenv("LEGION_MANAGED_BROWSER", "0")
    monkeypatch.setenv("FIXTURE_OUTPUT_KEY", KEY)
    monkeypatch.syspath_prepend(str(ORCH))
    monkeypatch.syspath_prepend(str(SERVICE))
    real_connect = socket.socket.connect

    def loopback_only(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            assert address[0] in ("127.0.0.1", "::1", "localhost"), "不得访问真实 provider"
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    hr = importlib.import_module("harness_registry")
    mr = importlib.import_module("model_registry")
    owned = importlib.import_module("meyo_harnesses.backends.omnigent_responses_bridge")
    spec = importlib.util.spec_from_file_location("_output_budget_bridge_test", BRIDGE)
    bridge = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, bridge)
    spec.loader.exec_module(bridge)
    monkeypatch.setattr(hr, "_WALL_S", 25)
    real_which = hr._sh.which
    monkeypatch.setattr(hr._sh, "which", lambda name, *a, **k:
                        str(NATIVE) if name == "codex" else real_which(name, *a, **k))
    work = tmp_path / "work"
    work.mkdir()
    return SimpleNamespace(root=tmp_path, work=work, mp=monkeypatch,
                           hr=hr, mr=mr, owned=owned, bridge=bridge)


def declare(case, base="https://provider.example.invalid/agentic/v1", cap=4096):
    value = {"model": MODEL, "base_url": base, "api_key_env": "FIXTURE_OUTPUT_KEY"}
    if cap is not None:
        value["max_output_tokens"] = cap
    case.mp.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps(value))
    return value


def context(case, base="https://provider.example.invalid/agentic/v1", **overrides):
    values = dict(model=MODEL, appid=KEY, gateway=base, model_protocol="responses",
                  user_id="fixture", session_id="fixture-native", execution_depth="quick",
                  workdir=str(case.work))
    values.update(overrides)
    return case.hr.HarnessCtx(**values)


def binding(case, cap=4096):
    value = {"binding_id": "fixture-binding", "model": MODEL,
             "base_url": "https://provider.example.invalid/agentic/v1",
             "api_key": KEY, "client_token": LOCAL_TOKEN, "wire_api": "responses",
             "http_headers": {}}
    if cap is not None:
        value["max_output_tokens"] = cap
    case.bridge._configure_mode(case.bridge.app, {
        "MEYO_RESPONSES_BRIDGE_MODE": "passthrough",
        "MEYO_RESPONSES_BRIDGE_BINDING": json.dumps(value),
    })
    return case.bridge.app.state.responses_binding


def test_operator_cap_is_optional_and_not_provider_identity(runtime):
    case = runtime
    declare(case, cap=None)
    before = case.mr.explicit_responses_binding()
    assert "max_output_tokens" not in before
    stamp = case.mr.ccr_binding_key(before["model"], before["api_key"], before["base_url"])
    for cap in (1, 4096, 8192):
        declare(case, cap=cap)
        current = case.mr.explicit_responses_binding()
        assert current["max_output_tokens"] == cap
        assert {k: v for k, v in current.items() if k != "max_output_tokens"} == before
        assert case.mr.ccr_binding_key(current["model"], current["api_key"], current["base_url"]) == stamp


@pytest.mark.parametrize("cap", [None, 0, -1, True, False, "4096", 4096.0, [], {}])
def test_invalid_operator_cap_fails_before_native(runtime, cap):
    value = declare(runtime)
    value["max_output_tokens"] = cap
    runtime.mp.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps(value))
    with pytest.raises(ValueError):
        runtime.mr.explicit_responses_binding()
    called = []
    runtime.mp.setattr(runtime.hr, "_run_task", lambda *a, **k: called.append(a))
    out = runtime.hr._codex_adapter("__ut_invalid_output_cap", "fixture", ctx=context(runtime))
    assert not called
    assert out["status"] == "unavailable"
    assert KEY not in json.dumps(out)


def forward(case, raw, cap=4096, upstream_status=200):
    configured = binding(case, cap)
    seen = []
    response_bytes = (
        b'event: response.created\ndata: {"type":"response.created","response":{"id":"fixture"}}\n\n'
        b'event: response.incomplete\ndata: {"type":"response.incomplete","response":'
        b'{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"}}}\n\n'
    ) if upstream_status == 200 else b'{"error":{"code":"fixture_original_error","message":"unchanged"}}'
    original_client = httpx.AsyncClient

    def upstream(request):
        seen.append(request)
        return httpx.Response(upstream_status, stream=httpx.ByteStream(response_bytes),
                              headers={"content-type": "text/event-stream", "x-fixture": "preserved"})

    case.mp.setattr(case.bridge.httpx, "AsyncClient", lambda **kwargs:
                   original_client(transport=httpx.MockTransport(upstream), **kwargs))

    class Request:
        headers = {"authorization": "Bearer " + LOCAL_TOKEN, "content-type": "application/json"}
        async def body(self):
            return raw

    async def run():
        response = await case.bridge._transparent_responses(Request(), configured)
        if hasattr(response, "body_iterator"):
            content = b"".join([part async for part in response.body_iterator])
            if response.background:
                await response.background()
        else:
            content = response.body
        return response, content

    response, content = asyncio.run(run())
    return seen, response, content, response_bytes


@pytest.mark.parametrize("upstream_status", [200, 401, 429])
def test_only_missing_budget_is_added_everything_else_is_transparent(runtime, upstream_status):
    body = {"model": MODEL, "stream": True, "store": False,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "原始输入"},
                {"type": "input_image", "image_url": "data:image/png;base64,fixture"}]},
                {"type": "reasoning", "encrypted_content": "opaque-original"},
                {"type": "function_call_output", "call_id": "same", "output": "original"}],
            "tools": [{"type": "function", "name": "fixture", "parameters": {"type": "object"}}],
            "reasoning": {"effort": "low"}, "include": ["reasoning.encrypted_content"],
            "unknown_future_field": {"keep": [1, False, None]}}
    raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
    seen, response, content, original = forward(runtime, raw, upstream_status=upstream_status)
    assert len(seen) == 1
    assert json.loads(seen[0].content) == {**body, "max_output_tokens": 4096}
    assert str(seen[0].url) == "https://provider.example.invalid/agentic/v1/responses"
    assert seen[0].headers["authorization"] == "Bearer " + KEY
    assert response.status_code == upstream_status and content == original
    assert response.headers["x-fixture"] == "preserved"
    assert KEY.encode() not in content and LOCAL_TOKEN.encode() not in content


@pytest.mark.parametrize("native_cap", [128, 4096, 8192])
def test_native_explicit_budget_wins_without_reencoding(runtime, native_cap):
    raw = ('{ "model": "' + MODEL + '", "max_output_tokens": ' + str(native_cap)
           + ', "input": [] }\n').encode()
    seen, *_ = forward(runtime, raw)
    assert len(seen) == 1 and seen[0].content == raw


def test_old_transparent_mode_remains_byte_for_byte(runtime):
    raw = ('{ "model": "' + MODEL + '", "input": [], "tools": [] }\n').encode()
    seen, *_ = forward(runtime, raw, cap=None)
    assert len(seen) == 1 and seen[0].content == raw


@pytest.mark.parametrize("value", [None, False, 0, -1, "4096"])
def test_invalid_native_cap_is_not_silently_overwritten(runtime, value):
    raw = json.dumps({"model": MODEL, "input": [], "max_output_tokens": value}).encode()
    seen, response, *_ = forward(runtime, raw)
    assert not seen
    assert response.status_code == 400


def capture_native(case):
    calls, bridge_calls = [], []
    case.closed_budget_transports = []

    @contextmanager
    def transport(value, environment, checkpoint=None):
        bridge_calls.append(copy.deepcopy(value))
        try:
            yield {**value, "client_base_url": "http://127.0.0.1:49999/v1",
                   "client_api_key": LOCAL_TOKEN}
        finally:
            case.closed_budget_transports.append(True)

    def native(command, *args, **kwargs):
        calls.append((command, kwargs))
        Path(command[command.index("-o") + 1]).write_text("fixture result")
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "type": "thread.started", "thread_id": "same-native-thread"}), stderr="")

    case.mp.setattr(case.owned, "transparent_bridge", transport)
    case.mp.setattr(case.hr, "_run_task", native)
    return calls, bridge_calls


def input_facts(call):
    text = call[1]["input"].split("NATIVE_MODEL_INPUT_FACTS_JSON\n", 1)[1].splitlines()[0]
    return json.loads(text)


@pytest.mark.parametrize("mismatch", [None, "model", "appid", "gateway"])
def test_only_the_effective_matching_declaration_enables_bridge(runtime, mismatch):
    case = runtime
    declare(case)
    overrides = {} if mismatch is None else {
        mismatch: {"model": "different-model", "appid": "different-key",
                   "gateway": "https://different.example.invalid/v1"}[mismatch]}
    calls, bridge_calls = capture_native(case)
    ctx = context(case, **overrides)
    out = case.hr._codex_adapter("__ut_budget_matching", "fixture", ctx=ctx)
    assert len(calls) == 1
    assert len(bridge_calls) == (1 if mismatch is None else 0)
    if bridge_calls:
        assert bridge_calls[0]["max_output_tokens"] == 4096
    for facts in (input_facts(calls[0]), out["native_facts"]):
        if mismatch is None:
            assert facts.get("declared_output_budget") == {
                "max_output_tokens": 4096, "source": "matched_operator_binding",
                "policy": "fill_if_native_request_omits"}
        else:
            assert "declared_output_budget" not in facts
    assert out["native_checkpoint"]["provider_binding"] == case.mr.ccr_binding_key(
        ctx.model, ctx.appid, ctx.gateway)
    assert LOCAL_TOKEN not in " ".join(calls[0][0])
    assert LOCAL_TOKEN not in calls[0][1]["input"]
    assert LOCAL_TOKEN not in json.dumps(out) and KEY not in json.dumps(out)


def test_absent_cap_does_not_start_a_bridge(runtime):
    declare(runtime, cap=None)
    runtime.mp.setenv("MEYO_MAX_TOKENS", "99999")
    calls, bridge_calls = capture_native(runtime)
    out = runtime.hr._codex_adapter("__ut_no_budget", "fixture", ctx=context(runtime, max_tokens=8192))
    assert len(calls) == 1 and not bridge_calls
    assert out["status"] == "ok"
    assert "declared_output_budget" not in input_facts(calls[0])
    assert "declared_output_budget" not in out["native_facts"]


def test_budget_changes_keep_original_checkpoint_and_upstream_identity(runtime):
    case = runtime
    calls, bridge_calls = capture_native(case)
    declare(case, cap=4096)
    first = case.hr._codex_adapter("__ut_cap_first", "fixture", ctx=context(case))
    checkpoint = first["native_checkpoint"]
    declare(case, cap=8192)
    second = case.hr._codex_adapter(
        "__ut_cap_resume", "fixture", ctx=context(case, task_memory={"native_checkpoint": checkpoint}))
    assert len(bridge_calls) == 2
    assert [row["max_output_tokens"] for row in bridge_calls] == [4096, 8192]
    assert bridge_calls[0]["stamp"] == bridge_calls[1]["stamp"]
    assert "resume" in calls[1][0] and "same-native-thread" in calls[1][0]
    assert second["native_checkpoint"]["provider_binding"] == checkpoint["provider_binding"]
    for call, result, cap in zip(calls, (first, second), (4096, 8192)):
        assert input_facts(call)["declared_output_budget"]["max_output_tokens"] == cap
        assert result["native_facts"]["declared_output_budget"]["max_output_tokens"] == cap


def test_budget_does_not_bypass_checkpoint_provider_check(runtime):
    declare(runtime)
    calls, bridge_calls = capture_native(runtime)
    out = runtime.hr._codex_adapter(
        "__ut_cap_bad_checkpoint", "fixture", ctx=context(runtime, task_memory={
            "native_checkpoint": {"session_id": "old-native", "provider_binding": "different-provider"}}))
    assert not calls and not bridge_calls
    assert out["status"] == "unavailable"


def test_native_timeout_closes_budget_scope_and_redacts_local_token(runtime):
    case = runtime
    declare(case)
    capture_native(case)

    def timeout(command, *args, **kwargs):
        Path(command[command.index("-o") + 1]).write_text("partial " + LOCAL_TOKEN)
        error = subprocess.TimeoutExpired(command, 1)
        error.legion_stdout = json.dumps({"type": "thread.started", "thread_id": "same-native-thread"})
        raise error

    case.mp.setattr(case.hr, "_run_task", timeout)
    result = case.hr._codex_adapter("__ut_budget_timeout", "fixture", ctx=context(case))
    assert result["status"] == "timeout"
    assert case.closed_budget_transports == [True]
    assert LOCAL_TOKEN not in json.dumps(result) and KEY not in json.dumps(result)
    assert result["native_checkpoint"]["session_id"] == "same-native-thread"


def test_omnigent_no_headers_no_cap_still_yields_original_binding(runtime):
    original = {"model": MODEL, "api_key": KEY, "base_url": "https://unused.example.invalid/v1",
                "http_headers": {}, "stamp": {"sha256": "same"}}
    with runtime.owned.transparent_bridge(original, {}) as actual:
        assert actual is original


@contextmanager
def loopback_provider():
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            observed.append({"body": body, "authorization": self.headers.get("Authorization")})
            rid, mid = "resp_fixture", "msg_fixture"
            item = {"id": mid, "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "fixture acknowledgment"}]}
            if type(body.get("max_output_tokens")) is not int or body["max_output_tokens"] <= 0:
                events = [{"type": "response.incomplete", "response": {
                    "id": rid, "status": "incomplete", "output": [],
                    "incomplete_details": {"reason": "max_output_tokens"}}}]
            else:
                events = [
                    {"type": "response.created", "response": {"id": rid, "status": "in_progress", "output": []}},
                    {"type": "response.output_item.added", "output_index": 0,
                     "item": {"id": mid, "type": "message", "role": "assistant", "content": []}},
                    {"type": "response.output_text.delta", "item_id": mid, "output_index": 0,
                     "content_index": 0, "delta": "fixture acknowledgment"},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {
                        "id": rid, "status": "completed", "output": [item],
                        "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}}},
                ]
            data = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event)
                           + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/agentic/v1", observed
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_real_native_request_gets_budget_through_existing_owned_bridge(runtime):
    """真 Codex CLI -> 既有透明桥进程 -> 本机HTTP夹具；没有真实模型或工具执行。"""
    assert NATIVE.is_file(), "该实测需当前已安装的原生 CLI，不能假通过或静默跳过"
    case = runtime
    stopped = []
    original_stop = case.owned._stop_owned

    def stop(proc, registry):
        original_stop(proc, registry)
        stopped.append((proc.pid, proc.poll()))
        assert not Path(registry._entry_path(proc.pid)).exists()

    case.mp.setattr(case.owned, "_stop_owned", stop)
    with loopback_provider() as (base, observed):
        declare(case, base=base)
        result = case.hr._codex_adapter(
            "__ut_real_output_budget", "Return a fixture acknowledgment; do not use tools.",
            ctx=context(case, base=base))
        assert observed, result
        assert len(observed) == 1, "环回验证不应自动重试或重放"
        assert observed[0]["body"].get("max_output_tokens") == 4096, observed[0]["body"].keys()
        assert result["status"] == "ok", result
        declare(case, base=base, cap=8192)
        resumed = case.hr._codex_adapter(
            "__ut_real_output_budget_resume", "Return the same fixture acknowledgment; no tools.",
            ctx=context(case, base=base, task_memory={"native_checkpoint": result["native_checkpoint"]}))
    assert len(observed) == 2
    assert [row["body"]["max_output_tokens"] for row in observed] == [4096, 8192]
    assert all(row["authorization"] == "Bearer " + KEY for row in observed)
    assert resumed["status"] == "ok" and resumed["resumed_native"] is True, resumed
    assert resumed["native_checkpoint"]["session_id"] == result["native_checkpoint"]["session_id"]
    for item in (result, resumed):
        assert item["final"] == "fixture acknowledgment"
        assert item["native_checkpoint"]["provider_binding"] == case.mr.ccr_binding_key(MODEL, KEY, base)
        assert KEY not in json.dumps(item)
    assert result["native_facts"]["declared_output_budget"]["max_output_tokens"] == 4096
    assert resumed["native_facts"]["declared_output_budget"]["max_output_tokens"] == 8192
    assert len(stopped) == 2 and all(returncode is not None for _, returncode in stopped)
