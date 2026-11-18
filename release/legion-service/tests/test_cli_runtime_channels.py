"""Real offline CLI -> transport -> run_one; no provider calls or task answers.

OpenCode framing follows its run.ts JSON emitter and the retained V2 native
step-finish records (tool-calls, tool-calls, stop). Runtime completion is not a
claim that a produced file or a task answer is correct.
"""
from __future__ import annotations

import copy
from contextlib import nullcontext
import importlib
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
KEY = "__ut_cli_channels"


@pytest.fixture
def cli_stack(tmp_path, monkeypatch):
    for name, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_PROC_REGISTRY_DIR", "registry"),
        ("LEGION_TASK_MEMORY_DIR", "memory"), ("LEGION_SESSIONS_DIR", "sessions"),
    ):
        directory = tmp_path / leaf
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setenv("AIGC_APPID", "offline-fixture")
    monkeypatch.setenv("MEYO_UNIFIED_APPID", "offline-fixture")
    monkeypatch.setenv("MEYO_UNIFIED_GATEWAY", "https://offline.example.invalid/v1")
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.syspath_prepend(str(ORCH))
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.setitem(sys.modules, "github_access",
                        SimpleNamespace(shared_environment=lambda env: env))

    def no_network(*args, **kwargs):
        raise AssertionError("offline regression must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    core = importlib.import_module("legion_core")
    registry = importlib.import_module("harness_registry")
    transports = importlib.import_module("external_agent_transports")
    memory = importlib.import_module("task_memory")
    external = importlib.import_module("external_agent_registry")
    concurrency = importlib.import_module("legion_concurrency")
    manifest = importlib.import_module("runtime_manifest")
    monkeypatch.setattr(memory, "ROOT", tmp_path / "memory")
    monkeypatch.setattr(core, "_ORCH_OK", True)
    monkeypatch.setattr(core, "_harness_keys", lambda: {KEY})
    monkeypatch.setattr(core, "_CORE_INPROC_KEYS", {KEY})
    monkeypatch.setattr(core, "_dynamic_max_attempts", lambda _: 1)
    monkeypatch.setattr(core, "_fallback_candidates", lambda *a, **kw: [])
    monkeypatch.setattr(core, "_stable_prelude", lambda *a, **kw: "")
    monkeypatch.setattr(core, "record_model_health", lambda *a, **kw: None)
    monkeypatch.setattr(core, "_record_gateway_call", lambda *a, **kw: None)
    monkeypatch.setattr(core, "select_usable_model", no_network)
    monkeypatch.setattr(external, "load_catalog", lambda: [])
    monkeypatch.setattr(concurrency, "slot", lambda *a, **kw: nullcontext())
    supports = manifest.supports_depth
    monkeypatch.setattr(manifest, "supports_depth", lambda key, depth:
                        depth in {"quick", "standard", "deep"} if key == KEY
                        else supports(key, depth))
    project = tmp_path / "work"
    project.mkdir()
    script = tmp_path / "native_fixture.py"
    state = SimpleNamespace(core=core, registry=registry, transport=transports,
                            project=project, script=script, calls=[], mapped=[],
                            payload="", stderr="", rc=0, protocol="json-events",
                            contract=None, legacy_stderr_only=False, events=[],
                            env_overlay={}, appid="offline-fixture")

    def adapter(qhash, question, gold="", ctx=None):
        state.calls.append((qhash, ctx.workdir))
        script.write_text(
            "import sys\n"
            f"sys.stdout.write({state.payload!r})\n"
            f"sys.stderr.write({state.stderr!r})\n"
            f"raise SystemExit({state.rc})\n")
        spec = {"command": [sys.executable, str(script)],
                "streaming_protocol": state.protocol}
        if state.contract:
            spec["output_contract"] = state.contract
        result = transports.run_cli(
            spec, {"workdir": ctx.workdir}, {**os.environ, **state.env_overlay}, timeout_s=3)
        state.mapped.append(copy.deepcopy(result))
        if state.legacy_stderr_only:
            # Actual worker decorates stderr-only adapter results this way.
            result["error"] = f"工作目录已保留:{ctx.workdir}·接力家可读它续做。"
            result["stderr"] = state.stderr
        return result

    monkeypatch.setattr(registry, "_STANDALONE_ADAPTERS", {KEY: adapter})
    return state


def frames(text, reason="stop", *, tail=()):
    records = [
        {"type": "step_start", "sessionID": "native-fixture",
         "part": {"type": "step-start"}},
        {"type": "text", "sessionID": "native-fixture",
         "part": {"type": "text", "text": text}},
        {"type": "step_finish", "sessionID": "native-fixture",
         "part": {"type": "step-finish", "reason": reason}},
        *tail,
    ]
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n"


def invoke(case):
    return case.core.run_one(
        KEY, "Return the requested result", qhash="__ut_cli_runtime_channels",
        preferred_model="offline-model", preferred_appid=case.appid,
        user_id="offline-owner", conversation_id="offline-conversation",
        execution_depth="quick", cb=case.events.append,
        task_facts={"project_context": {
            "repo_root": str(case.project), "user_id": "offline-owner"}},
    )


@pytest.mark.parametrize("answer", [
    "好",
    # Public completion text from the real V2 false-failure trace; no gold code.
    "Done. `solution.py` implements `filter_by_substring` using a list comprehension; "
    "doctests and extra edge cases (empty list, empty substring) pass.",
    "Done. `solution.py` implements `select_words`, splitting on whitespace and counting "
    "letters not in `aeiouAEIOU` (so `y` counts as a consonant). All five examples and the empty-string case verified.",
    "HTTP 429、connection、empty 是本说明讨论的术语，不是这次运行失败。",
    '{"status":"error","error":"HTTP 502","native_facts":{"failed":true}}',
])
def test_real_stream_completion_keeps_final_out_of_failure_channel(cli_stack, answer):
    cli_stack.payload = frames(answer)
    result = invoke(cli_stack)
    assert len(cli_stack.calls) == 1
    assert result["status"] == "done", result
    assert result["final"] == answer
    assert result["error"] == ""
    facts = result["native_facts"]
    assert facts["process_returncode"] == 0
    assert facts["completed"] is True
    assert facts["turn_status"] == "completed"
    assert "goal_completed" not in facts and "correct" not in facts


@pytest.mark.parametrize("answer", [
    "好", "The result is an empty list.",
    '{"final":"business field","status":"error","error":"HTTP 429",'
    '"native_facts":{"completed":true,"failed":true}}',
])
def test_declared_plain_stdout_is_content_not_a_runtime_json_envelope(cli_stack, answer):
    cli_stack.protocol = "text"
    cli_stack.payload = answer + "\n"
    result = invoke(cli_stack)
    assert result["status"] == "done", result
    assert result["final"] == answer
    assert result["native_facts"]["process_returncode"] == 0
    assert result["native_facts"].get("completed") is not True, (
        "plain stdout has no native turn-completion record")


# Public native diagnostic only; no session input, bindings or credentials.
NON_SSE_ERROR = (
    '[API Error: Streaming request received a non-SSE response '
    '(HTTP 200, Content-Type: application/json;charset=UTF-8). Body prefix: '
    '{"code":524,"msg":"exception: Promise was collected"}\n]'
)


def assistant_result_frames(text, *, failed=False, error="reported native failure", output_tokens=3):
    # Schema read from the LOCAL pinned Qwen source, not a captured user event:
    # revision 98a9c964158697dd5631d15a62174684ff7bbb53,
    # packages/cli/src/nonInteractive/io/BaseJsonOutputAdapter.ts:1199-1264.
    # createUsage:210-216 defaults zeros when metadata is absent; no proof.
    return "\n".join(json.dumps(row, ensure_ascii=False) for row in [
        {"type": "assistant", "session_id": "offline-native", "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}]}},
        {"type": "result", "session_id": "offline-native", "uuid": "offline-result",
         "subtype": "error_during_execution" if failed else "success",
         "is_error": failed,
         **({"error": {"message": error}} if failed else {"result": text}),
         "duration_ms": 1, "duration_api_ms": 0, "num_turns": 1,
         "permission_denials": [], "usage": {"input_tokens": 0, "output_tokens": output_tokens}},
    ])


@pytest.mark.parametrize("partial", ["", "已保存可恢复的部分正文。\n"])
def test_native_non_sse_payload_is_transport_failure_even_with_rc_zero(cli_stack, partial):
    cli_stack.protocol = "stream-json"
    cli_stack.payload = assistant_result_frames(partial, failed=True, error=NON_SSE_ERROR,
                                              output_tokens=0)
    result = invoke(cli_stack)
    transport = cli_stack.mapped[0]
    assert len(cli_stack.calls) == 1, "fixture budget must not become an unbounded retry"
    assert transport["returncode"] == 0 and transport["status"] == "error"
    assert transport["final"] == partial.strip()
    assert result["status"] != "done", result
    assert result["final"] == partial.strip()
    assert NON_SSE_ERROR in transport["error"] and NON_SSE_ERROR in result["error"]
    facts = result["native_facts"]
    assert facts["failed"] is True and facts["completed"] is False
    assert facts["error_source"] == "native_protocol_error"
    assert facts["source_authority"]["error"] == "native_protocol_error"
    assert NON_SSE_ERROR in facts["native_error_message"]
    envelope = result["error_envelope"]
    assert envelope["category"] == "gateway" and envelope["scope"] == "provider"
    assert envelope["retryable"] is True
    assert envelope["code"] == "native_api_non_sse_response"
    assert envelope["details"]["transport_failure"] is True
    assert transport["usage_records"][0]["output_tokens"] == 0
    assert transport["events"][0]["message"]["content"][0]["text"] == partial
    assert facts["native_error_records"][-1]["error"]["message"] == NON_SSE_ERROR


@pytest.mark.parametrize("answer", [
    "好", "2", "APIError 是 SDK 的异常类型，API Error 表示接口错误。",
    "文档例子：" + NON_SSE_ERROR,
    NON_SSE_ERROR,
    "```text\n" + NON_SSE_ERROR + "\n```",
    "> 引用\n> " + NON_SSE_ERROR.replace("\n", "\n> "),
    "原样日志：\n" + NON_SSE_ERROR + "\n仅作示例。",
    "[API Error: 本文解释错误日志，并非本次调用失败]",
])
def test_native_assistant_result_keeps_short_and_error_discussion_answers(cli_stack, answer):
    cli_stack.protocol = "stream-json"
    cli_stack.payload = assistant_result_frames(answer)
    result = invoke(cli_stack)
    assert result["status"] == "done", result
    assert result["final"] == answer
    assert result["error"] == "" and not result.get("error_envelope")
    assert len(cli_stack.calls) == 1


@pytest.mark.parametrize("tokens", [0, None, False])
def test_untyped_embedded_diagnostic_is_unknown_not_deleted(cli_stack, tokens):
    cli_stack.protocol = "stream-json"
    cli_stack.payload = assistant_result_frames(NON_SSE_ERROR, output_tokens=tokens)
    result = invoke(cli_stack)
    assert result["final"] == NON_SSE_ERROR
    assert result["error"] == "" and not result.get("error_envelope")
    assert result["native_facts"]["source_authority"]["embedded_diagnostic_origin"] == "unknown"


@pytest.mark.parametrize("cause", [
    NON_SSE_ERROR.replace('Promise was collected', 'body [inner] [unclosed'),
    NON_SSE_ERROR.replace('Promise was collected', 'body ] trailing [ bytes'),
    NON_SSE_ERROR[:-1],
])
def test_brackets_in_authoritative_error_are_not_parsed_or_removed(cli_stack, cause):
    cli_stack.protocol = "stream-json"
    text = "已保存部分正文。\n```text\n" + cause + "\n```"
    cli_stack.payload = assistant_result_frames(text, failed=True, error=cause)
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert result["final"] == text
    assert result["error_envelope"]["category"] == "gateway"
    assert result["error_envelope"]["message"] == cause.strip()
    assert result["native_facts"]["native_error_records"][-1]["error"]["message"] == cause


def test_subagent_error_is_not_a_main_turn_failure(cli_stack):
    cli_stack.protocol = "stream-json"
    child = {"type": "result", "session_id": "offline-native", "parent_tool_use_id": "child",
             "is_error": True, "error": {"message": NON_SSE_ERROR}}
    cli_stack.payload = assistant_result_frames("好") + "\n" + json.dumps(child)
    result = invoke(cli_stack)
    assert result["status"] == "done" and result["final"] == "好"
    assert not result.get("error_envelope")
    assert "native_error_records" not in result["native_facts"]


def test_separate_api_error_event_keeps_assistant_literal_and_partial(cli_stack):
    cli_stack.protocol = "stream-json"
    text = "部分正文\n" + NON_SSE_ERROR
    # Existing type=error protocol; an overlay can expose the otherwise lost
    # LlmEventType.Error here. This fixture is NOT evidence of an installed overlay.
    cli_stack.payload = assistant_result_frames(text) + "\n" + json.dumps({
        "type": "error", "error": {"name": "APIError", "message": NON_SSE_ERROR}})
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert result["final"] == text
    assert result["error_envelope"]["message"] == NON_SSE_ERROR
    assert result["native_facts"]["source_authority"]["error"] == "native_protocol_error"


def test_plain_stderr_literal_does_not_establish_native_error(cli_stack):
    cli_stack.payload = assistant_result_frames("好")
    cli_stack.stderr = NON_SSE_ERROR
    result = invoke(cli_stack)
    assert result["status"] == "done" and result["final"] == "好"
    assert result["error"] == "" and not result.get("error_envelope")


def test_native_transport_failure_returns_full_context_without_borrowing_binding(cli_stack, monkeypatch):
    models = importlib.import_module("model_registry")
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    binding = {"model": "offline-model", "api_key": "offline-fixture",
               "base_url": "https://offline.example.invalid/v1", "protocol": "chat"}
    monkeypatch.setattr(models, "explicit_member_bindings", lambda: {
        KEY: binding, "other-member": {**binding, "api_key": "other-member-fixture"},
    })
    monkeypatch.setattr(cli_stack.core, "_dynamic_max_attempts", lambda _: 3)
    cli_stack.protocol = "stream-json"
    cli_stack.payload = assistant_result_frames("", failed=True, error=NON_SSE_ERROR, output_tokens=0)
    result = invoke(cli_stack)
    assert result["status"] == "error", result
    assert result["execution_calls"] == 1, "ordinary failure must stop at the explicit binding boundary"
    assert len(cli_stack.calls) == 1
    prior = result["prior_attempts"][0]
    assert prior["fail_kind"] == "gateway" and prior["workdir"] == str(cli_stack.project)
    assert NON_SSE_ERROR in prior["error_envelope"]["message"]
    assert "other-member-fixture" not in repr(result)
    assert result["final"] == ""


def test_structured_native_error_retains_partial_body_and_error_facts(cli_stack):
    cli_stack.protocol = "stream-json"
    cli_stack.payload = assistant_result_frames("部分真实正文", failed=True)
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert result["final"] == "部分真实正文"
    assert "reported native failure" in result["error"]
    assert result["native_facts"]["failed"] is True
    assert cli_stack.mapped[0]["events"][-1]["is_error"] is True


def test_plain_json_error_payload_is_still_business_content(cli_stack):
    cli_stack.protocol = "text"
    cli_stack.payload = assistant_result_frames(NON_SSE_ERROR, failed=True)
    result = invoke(cli_stack)
    assert result["status"] == "done", result
    assert result["final"] == cli_stack.payload
    assert not result.get("error_envelope")


@pytest.mark.skipif(not os.environ.get("QWEN_NATIVE_PROTOCOL_TEST_CLI"),
                    reason="explicit opt-in to the already installed CLI, never installs")
@pytest.mark.parametrize("response_mode", ["non_sse", "normal", "partial"])
def test_installed_qwen_non_sse_protocol_observation(cli_stack, response_mode):
    from collections import Counter
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from urllib.parse import quote

    cli = Path(os.environ["QWEN_NATIVE_PROTOCOL_TEST_CLI"]).resolve()
    package = json.loads((cli.parent / "package.json").read_text())
    assert package["name"] == "@qwen-code/qwen-code" and package["version"] == "0.22.3"
    patched = os.environ.get("QWEN_NATIVE_PROTOCOL_TEST_OVERLAY") == "1"
    if patched:
        import runpy
        hook = SERVICE / "external_adapters/overlays/penguin-v0.2.4/source_patch_hook.py"
        stage = runpy.run_path(str(hook))["stage_npm_bundle"]
        original = cli.parent
        destination = cli_stack.project / "patched-qwen-0.22.3"
        stage(original, destination, SERVICE / "external_adapters/overlays/qwen-code-v0.22.3",
              Path(os.environ["QWEN_NATIVE_PROTOCOL_TEST_ARCHIVE"]))
        cli = destination / cli.name
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            # Never record headers, request text, credentials or system context.
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append(self.path)
            body = b'{"code":524,"msg":"exception: Promise was collected"}'
            content_type = "application/json;charset=UTF-8"
            if response_mode != "non_sse":
                content_type = "text/event-stream"
                chunk = {"id": "offline", "object": "chat.completion.chunk", "model": "offline-protocol-fixture",
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "好"}, "finish_reason": None}]}
                end = ({"error": {"message": "offline partial provider failure", "type": "server_error", "code": "524"}}
                       if response_mode == "partial" else
                       {"id": "offline", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                body = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\ndata: " + json.dumps(end) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
    thread.start()
    # Runs before the actual CLI entry. Assert every TCP connection is loopback;
    # this is a test-only network boundary, not a model/provider fallback.
    guard = (
        "import net from 'node:net';const original=net.Socket.prototype.connect;"
        "net.Socket.prototype.connect=function(...args){let a=args[0];"
        "if(Array.isArray(a))a=a[0];const host=typeof a==='object'?a.host:"
        "(typeof args[1]==='string'?args[1]:'localhost');"
        "if(host&&!['127.0.0.1','localhost','::1'].includes(host))"
        "throw Error('OFFLINE_NETWORK_ASSERTION:'+host);"
        "return original.apply(this,args)};"
    )
    environment = {
        "HOME": os.environ["HOME"], "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
        "TMPDIR": str(cli_stack.project), "NO_PROXY": "127.0.0.1,localhost,::1",
        "OPENAI_API_KEY": "offline-protocol-placeholder-not-a-credential",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
        "OPENAI_MODEL": "offline-protocol-fixture",
        "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1", "CI": "1",
        "MEYO_LEGION_DIR": os.environ["MEYO_LEGION_DIR"],
        "LEGION_HOME_DIR": os.environ["LEGION_HOME_DIR"],
        "LEGION_PROC_REGISTRY_DIR": os.environ["LEGION_PROC_REGISTRY_DIR"],
    }
    command = ["/opt/homebrew/bin/node", "--import", "data:text/javascript," + quote(guard),
               str(cli), "-p", "Return OK; do not use tools.", "-o", "stream-json", "-y",
               "--auth-type", "openai", "--max-session-turns", "1"]
    try:
        result = cli_stack.transport._supervise_cli_process(
            command, {"streaming_protocol": "stream-json", "snapshot_terminal": True},
            {"workdir": str(cli_stack.project)}, environment, 60)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    snapshot = result["terminal_snapshot"]["streams"]["stdout"]["path"]
    raw = Path(snapshot).read_text()
    records = [json.loads(line) for line in raw.splitlines() if line.strip().startswith("{")]
    print("QWEN_NATIVE_PROTOCOL_OBSERVATION=" + json.dumps({
        "version": package["version"], "requests": requests,
        "returncode": result["returncode"], "status": result["status"],
        "raw_bytes": len(raw.encode()), "record_count": len(records),
        "types": dict(Counter(row.get("type") for row in records)),
        "records": records, "transport_final": result["final"],
        "transport_error": result["error"], "native_facts": result["native_facts"],
    }, ensure_ascii=False))
    assert requests and set(requests) == {"/v1/chat/completions"}
    assert len(requests) == (2 if response_mode == "normal" else 1), (
        "Qwen's normal stream includes its native second request; failures are single-shot")
    assert "OFFLINE_NETWORK_ASSERTION" not in result["stderr"]
    if patched and response_mode == "non_sse":
        errors = [row for row in records if row.get("type") == "error"]
        assert len(errors) == 1 and errors[0]["native_source"] == "LlmEventType.Error"
        assert result["returncode"] == 0 and result["status"] == "error"
        assert result["final"] == "", "native error must not become an answer payload"
        assert result["native_facts"]["failed"] is True
        assert result["error_envelope"]["category"] == "gateway"
        assert result["error_envelope"]["code"] == "native_api_non_sse_response"
    if patched and response_mode == "normal":
        assert result["status"] == "ok" and result["final"] == "好", result
        assert not result.get("error_envelope")
    if patched and response_mode == "partial":
        assert result["status"] == "error" and result["final"] == "好", result
        assert "offline partial provider failure" in result["error"]
        assert any(row.get("native_source") == "LlmEventType.Error" for row in records)
    import legion_proc_registry
    # Normal completion leaves observations of already exited children until
    # the existing registry sweep. Only this test's isolated registry is used.
    legion_proc_registry.sweep(kill=False)
    entries = legion_proc_registry._load_entries()
    assert not any(legion_proc_registry.is_alive(row["pid"]) for _, row in entries), (
        "native process remained alive after protocol observation")
    for _, row in entries:
        legion_proc_registry.unregister(row["pid"])


def test_npm_overlay_stage_rejects_unknown_package_without_publication(tmp_path):
    import runpy
    import hashlib
    overlay = SERVICE / "external_adapters/overlays/qwen-code-v0.22.3"
    manifest = json.loads((overlay / "source-overlay.json").read_text())
    assert hashlib.sha256((overlay / manifest["patch_file"]).read_bytes()).hexdigest() == manifest["patch_sha256"]
    stage = runpy.run_path(str(SERVICE / "external_adapters/overlays/penguin-v0.2.4/source_patch_hook.py"))["stage_npm_bundle"]
    source = tmp_path / "source"
    source.mkdir()
    (source / "package.json").write_text(json.dumps({"name": manifest["package"], "version": "0.22.4"}))
    destination = tmp_path / "generation"
    with pytest.raises(ValueError, match="package identity"):
        stage(source, destination, overlay)
    assert not destination.exists()
    (source / "package.json").write_text(json.dumps({"name": manifest["package"], "version": manifest["version"]}))
    (source / "LICENSE").write_text("fixture license")
    target = source / next(iter(manifest["files"]))
    target.parent.mkdir()
    target.write_text("unknown bundle must not be overwritten")
    with pytest.raises(ValueError, match="bundle hash/path"):
        stage(source, destination, overlay)
    assert not destination.exists() and target.read_text() == "unknown bundle must not be overwritten"


def test_full_package_closure_rejects_extra_files_and_bad_sri(tmp_path):
    import runpy, hashlib, base64, io, tarfile
    stage = runpy.run_path(str(SERVICE / "external_adapters/overlays/penguin-v0.2.4/source_patch_hook.py"))["stage_npm_bundle"]
    source, overlay = tmp_path / "source", tmp_path / "overlay"
    source.mkdir(); overlay.mkdir()
    files = {"package.json": json.dumps({"name": "fixture", "version": "1"}).encode(),
             "LICENSE": b"license", "chunk.js": b"original"}
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, content in files.items():
            member = tarfile.TarInfo("package/" + name); member.size = len(content)
            tar.addfile(member, io.BytesIO(content)); (source / name).write_bytes(content)
    archive = tmp_path / "official.tgz"; archive.write_bytes(data.getvalue())
    (overlay / "patch").write_text("fixture")
    manifest = {"kind": "npm_bundle", "package": "fixture", "version": "1", "license_file": "LICENSE",
                "patch_file": "patch", "patch_sha256": hashlib.sha256(b"fixture").hexdigest(),
                "official_integrity": "sha512-" + base64.b64encode(hashlib.sha512(data.getvalue()).digest()).decode(),
                "files": {"chunk.js": {"before_sha256": hashlib.sha256(b"original").hexdigest(), "after_sha256": "unused"}}}
    (overlay / "source-overlay.json").write_text(json.dumps(manifest))
    (source / "extra-entry.js").write_text("unknown entry")
    with pytest.raises(ValueError, match="byte closure"):
        stage(source, tmp_path / "dest", overlay, archive)
    assert not (tmp_path / "dest").exists()
    archive.write_bytes(b"not the locked archive")
    with pytest.raises(ValueError, match="SRI"):
        stage(source, tmp_path / "dest", overlay, archive)


def test_overlay_publication_never_replaces_empty_directory_or_race(tmp_path):
    import runpy
    from concurrent.futures import ThreadPoolExecutor
    publish = runpy.run_path(str(SERVICE / "external_adapters/overlays/penguin-v0.2.4/source_patch_hook.py"))["_publish_noreplace"]
    for name in ("a", "b", "existing"):
        (tmp_path / name).mkdir()
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            publish(fd, "a", "existing")
        assert (tmp_path / "a").exists() and (tmp_path / "existing").exists()
        def attempt(name):
            try:
                publish(fd, name, "winner")
                return True
            except FileExistsError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(attempt, ("a", "b"))) == [False, True]
        assert (tmp_path / "winner").exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize("tamper", [None, "content", "extra", "mode", "marker", "symlink"])
def test_same_overlay_generation_is_reused_only_with_exact_contents(tmp_path, tamper):
    import runpy, hashlib, base64, io, tarfile
    stage = runpy.run_path(str(SERVICE / "external_adapters/overlays/penguin-v0.2.4/source_patch_hook.py"))["stage_npm_bundle"]
    source, overlay, destination = tmp_path / "source", tmp_path / "overlay", tmp_path / "generation"
    source.mkdir(); overlay.mkdir()
    files = {"package.json": json.dumps({"name": "fixture", "version": "1"}).encode(),
             "LICENSE": b"license", "chunk.js": b"original\n"}
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo("package/" + name)
            member.size, member.mode = len(content), 0o644
            archive.addfile(member, io.BytesIO(content))
            (source / name).write_bytes(content)
    archive = tmp_path / "official.tgz"; archive.write_bytes(data.getvalue())
    patch = b"diff --git a/chunk.js b/chunk.js\n--- a/chunk.js\n+++ b/chunk.js\n@@ -1 +1 @@\n-original\n+patched\n"
    (overlay / "patch").write_bytes(patch)
    manifest = {"kind": "npm_bundle", "package": "fixture", "version": "1", "license_file": "LICENSE",
                "patch_file": "patch", "patch_sha256": hashlib.sha256(patch).hexdigest(),
                "official_integrity": "sha512-" + base64.b64encode(hashlib.sha512(data.getvalue()).digest()).decode(),
                "files": {"chunk.js": {"before_sha256": hashlib.sha256(b"original\n").hexdigest(),
                                       "after_sha256": hashlib.sha256(b"patched\n").hexdigest()}}}
    (overlay / "source-overlay.json").write_text(json.dumps(manifest))
    assert stage(source, destination, overlay, archive)["reused"] is False
    identity = destination.stat().st_ino
    if tamper == "content": (destination / "chunk.js").write_text("unknown\n")
    elif tamper == "extra": (destination / "unknown.js").write_text("unreviewed")
    elif tamper == "mode": (destination / "chunk.js").chmod(0o600)
    elif tamper == "marker": (destination / ".native-overlay.json").write_text("{}")
    elif tamper == "symlink":
        (destination / "chunk.js").unlink()
        (destination / "chunk.js").symlink_to(source / "chunk.js")
    if tamper is None:
        assert stage(source, destination, overlay, archive)["reused"] is True
        assert (destination / "chunk.js").read_bytes() == b"patched\n"
    else:
        with pytest.raises(ValueError, match="existing generation"):
            stage(source, destination, overlay, archive)
    assert destination.stat().st_ino == identity
    assert (source / "chunk.js").read_bytes() == b"original\n"


def test_catalog_qwen_install_and_run_share_fixed_overlay(cli_stack):
    catalog = json.loads((SERVICE / "external_agents.json").read_text())
    rows = catalog if isinstance(catalog, list) else catalog["agents"]
    qwen = next(row for row in rows if row["key"] == "qwen-code")
    import external_agent_registry as registry
    assert registry._probe_spec_identity(qwen) == registry._TRUSTED_PROBE_SPECS["qwen-code"]
    assert "stage-npm" in qwen["install"][1] and "--fetch-official" in qwen["install"][1]
    assert qwen["probe"]["target"] == qwen["transports"][0]["command"][0]
    assert "native-overlays/qwen-code-0.22.3-native-error" in qwen["probe"]["target"]


def test_observed_qwen_0223_non_sse_success_schema_cannot_prove_error(cli_stack):
    # Observed with the installed 0.22.3 CLI against one loopback HTTP request.
    # Volatile IDs/workdir/timing omitted; no provider or credential was used.
    diagnostic = NON_SSE_ERROR.replace("\n]", "]")
    rows = [
        {"type": "system", "subtype": "init", "session_id": "offline-native",
         "qwen_code_version": "0.22.3"},
        {"type": "stream_event", "session_id": "offline-native", "parent_tool_use_id": None,
         "event": {"type": "goal_state", "goal_state": {
             "v": 2, "goal": None, "activity": "idle"}}},
        {"type": "assistant", "session_id": "offline-native", "parent_tool_use_id": None,
         "message": {"type": "message", "role": "assistant", "model": "offline-protocol-fixture",
                     "content": [{"type": "text", "text": diagnostic}], "stop_reason": None,
                     "usage": {"input_tokens": 0, "output_tokens": 0}}},
        {"type": "result", "session_id": "offline-native", "subtype": "success",
         "is_error": False, "result": diagnostic, "num_turns": 1,
         "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0},
         "permission_denials": []},
    ]
    cli_stack.protocol = "stream-json"
    cli_stack.payload = "\n".join(json.dumps(row) for row in rows)
    result = invoke(cli_stack)
    assert result["final"] == diagnostic
    assert result["error"] == "" and not result.get("error_envelope")
    assert result["native_facts"]["source_authority"]["embedded_diagnostic_origin"] == "unknown"
    assert "native_error_records" not in result["native_facts"]


def test_native_error_wins_even_with_a_prior_completed_step_and_rc_zero(cli_stack):
    cli_stack.payload = frames("partial result", tail=({
        "type": "error", "error": {
            "name": "APIError", "data": {"message": "native request rejected"}},
    },))
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert "native request rejected" in result["error"]
    assert result["native_facts"]["failed"] is True
    assert result["native_facts"]["completed"] is False


@pytest.mark.parametrize("reason", ["length", "tool-calls", "unknown"])
def test_unfinished_native_step_is_not_success_even_when_process_exits_zero(cli_stack, reason):
    cli_stack.payload = frames("partial public output", reason)
    result = invoke(cli_stack)
    assert result["status"] != "done", result
    assert result["native_facts"]["completed"] is False
    assert result["final"] == "partial public output"
    if reason == "length":
        assert result["error_envelope"]["category"] == "output_limit"


def test_new_step_after_completed_step_revokes_that_terminal_fact(cli_stack):
    cli_stack.payload = frames("earlier answer", tail=({
        "type": "step_start", "sessionID": "native-fixture",
        "part": {"type": "step-start"},
    },))
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert result["native_facts"]["completed"] is False


def test_missing_terminal_does_not_bless_a_text_delta(cli_stack):
    cli_stack.payload = json.dumps({
        "type": "text", "part": {"type": "text", "text": "unfinished public output"},
    })
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert result["native_facts"]["completed"] is False


def test_empty_terminal_is_not_delivered_as_raw_protocol_json(cli_stack):
    cli_stack.payload = frames("")
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert result["final"] == ""
    assert result["native_facts"]["completed"] is True


def test_successful_terminal_retains_nonfatal_stderr_as_a_separate_observation(cli_stack):
    cli_stack.payload = frames("好")
    cli_stack.stderr = "native diagnostic: optional integration is unavailable\n"
    result = invoke(cli_stack)
    assert result["status"] == "done", result
    assert result["final"] == "好"
    assert result["native_facts"]["stderr"] == cli_stack.stderr


@pytest.mark.parametrize("workdir_hint", [False, True])
def test_real_stderr_cause_survives_worker_workdir_decoration(cli_stack, workdir_hint):
    cli_stack.payload = frames("partial output")
    cli_stack.rc = 1
    cli_stack.stderr = "E_NATIVE_WRITE_FAILURE: offline fixture\n"
    cli_stack.legacy_stderr_only = workdir_hint
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert "E_NATIVE_WRITE_FAILURE" in result["error"]
    assert result["native_facts"]["process_returncode"] == 1


def test_declared_json_terminal_is_not_inferred_from_its_answer_words(cli_stack):
    cli_stack.protocol = "stream-json"
    cli_stack.contract = {
        "type": "json", "text_field": "answer", "status_field": "state",
        "completed_statuses": ["completed"],
        "record_match": {"type": "native_result"},
    }
    cli_stack.payload = json.dumps({
        "type": "native_result", "state": "completed", "answer": "An empty result is valid.",
    })
    result = invoke(cli_stack)
    assert result["status"] == "done", result
    assert result["final"] == "An empty result is valid."
    assert result["native_facts"]["completed"] is True


def test_known_cli_exit_does_not_overwrite_declared_native_failure(cli_stack):
    cli_stack.protocol = "stream-json"
    cli_stack.contract = {
        "type": "json", "text_field": "answer", "success_field": "success",
        "error_field": "is_error", "error_message_field": "failure",
        "record_match": {"type": "native_result"},
    }
    cli_stack.payload = json.dumps({
        "type": "native_result", "success": False, "is_error": True,
        "answer": "unfinished", "failure": "E_NATIVE_TERMINAL",
    })
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert "E_NATIVE_TERMINAL" in result["error"]
    assert result["native_facts"]["failed"] is True


@pytest.mark.parametrize("native_error,expected", [
    ("max_output_tokens", "output_limit"),
    ("unrelated_failure", "process"),
])
def test_cc_native_machine_cause_reaches_shared_normalizer(cli_stack, monkeypatch, native_error, expected):
    # Same structured fields as the retained V3 CC failure. No private message
    # history or model-generated reasoning is included.
    stdout = "\n".join(json.dumps(row) for row in [
        {"type": "assistant", "error": native_error, "message": {
            "id": "offline-message", "content": [{"type": "text", "text": "native API diagnostic"}]}},
        {"type": "result", "is_error": True, "subtype": "success",
         "terminal_reason": "api_error", "result": "native API diagnostic"},
    ])
    final, status, facts, _, _, _ = cli_stack.registry._cc_native_terminal(stdout, 1)
    monkeypatch.setitem(cli_stack.registry._STANDALONE_ADAPTERS, KEY,
                        lambda *a, **kw: {
                            "status": status, "final": final, "error": native_error,
                            "native_facts": copy.deepcopy(facts), "workdir": str(cli_stack.project)})
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert result["error_envelope"]["category"] == expected
    assert result["native_facts"]["last_assistant"]["error"] == native_error


def test_output_limit_words_in_error_text_are_not_machine_cause(cli_stack):
    errors = importlib.import_module("error_envelope")
    result = errors.normalize("rc1", "documentation mentions max_output_tokens",
                              native_facts={"process_returncode": 1})
    assert result.category == "process"
    supplied = {"category": "authorization_required", "retryable": False,
                "scope": "user", "message": "explicit approval required"}
    facts = {"turn_result": {"type": "turn.failed", "error": {
        "message": "Incomplete response returned, reason: max_output_tokens"}}}
    assert errors.normalize("rc1", supplied=supplied, native_facts=facts).category == "authorization_required"


@pytest.mark.parametrize("rc", [0, 1])
def test_plain_stdout_and_stderr_are_independent_of_exit_success(cli_stack, rc):
    cli_stack.protocol = "text"
    cli_stack.payload = "好\n"
    cli_stack.stderr = "optional native integration was unavailable\n"
    cli_stack.rc = rc
    result = invoke(cli_stack)
    assert (result["status"] == "done") is (rc == 0), result
    assert result["native_facts"]["stderr"] == cli_stack.stderr
    assert result["native_facts"]["stderr_is_error"] is (rc != 0)
    if rc == 0:
        assert result["final"] == "好" and result["error"] == ""
        assert result["native_facts"].get("completed") is not True
        assert "goal_completed" not in result["native_facts"]
    else:
        assert "optional native integration" in result["error"]


def test_stderr_observation_does_not_hide_a_typed_failure_with_rc_zero(cli_stack):
    cli_stack.protocol = "text"
    cli_stack.contract = {
        "type": "json", "text_field": "answer", "success_field": "success",
        "error_field": "is_error", "error_message_field": "failure",
        "record_match": {"type": "native_result"},
    }
    cli_stack.payload = json.dumps({
        "type": "native_result", "success": False, "is_error": True,
        "answer": "unfinished", "failure": "E_TYPED_NATIVE_FAILURE",
    })
    cli_stack.stderr = "optional native integration was unavailable\n"
    result = invoke(cli_stack)
    assert result["status"] != "done"
    assert "E_TYPED_NATIVE_FAILURE" in result["error"]
    assert result["native_facts"]["failed"] is True


@pytest.mark.parametrize("protocol,rc", [("text", 0), ("text", 1), ("json-events", 0)])
def test_stderr_uses_launch_environment_redaction_before_public_projection(cli_stack, protocol, rc):
    # Deliberately absent from os.environ: only this native invocation owns it.
    secret = "offline-request-secret-7cd91c6d3e"
    bearer = "offline-unbound-bearer-82be10690f"
    password = "offline-unbound-password-32b09ccf"
    cli_stack.env_overlay = {"REQUEST_API_KEY": secret}
    cli_stack.protocol, cli_stack.rc = protocol, rc
    cli_stack.payload = "好\n" if protocol == "text" else frames("好")
    cli_stack.stderr = (
        f"diagnostic binding={secret}\nAuthorization: Bearer {bearer}\n"
        f'password="{password}"\nE_NATIVE_DIAGNOSTIC\n')
    result = invoke(cli_stack)
    public = json.dumps({"transport": cli_stack.mapped, "result": result,
                         "events": cli_stack.events}, ensure_ascii=False)
    assert not any(value in public for value in (secret, bearer, password)), "credential escaped diagnostic projection"
    assert "E_NATIVE_DIAGNOSTIC" in result["native_facts"]["stderr"]


def test_stderr_redaction_precedes_tail_truncation(cli_stack):
    secret = "offline-secret-" + "X" * 48 + "ENDPRIVATE"
    cli_stack.env_overlay = {"REQUEST_API_KEY": secret}
    cli_stack.payload = frames("好")
    cli_stack.stderr = secret + "z" * 3970
    result = invoke(cli_stack)
    public = json.dumps({"transport": cli_stack.mapped, "result": result}, ensure_ascii=False)
    assert "ENDPRIVATE" not in public, "truncation exposed a suffix of a known credential"


def test_legacy_stderr_redaction_uses_this_calls_binding(cli_stack, monkeypatch):
    cli_stack.appid = "offline-per-call-secret-5f24c3"

    def legacy(*args, ctx=None, **kwargs):
        return {"status": "rc1", "final": "", "error": "工作目录已保留。",
                "stderr": "E_LEGACY_NATIVE binding=" + ctx.appid,
                "workdir": str(cli_stack.project)}

    monkeypatch.setitem(cli_stack.registry._STANDALONE_ADAPTERS, KEY, legacy)
    result = invoke(cli_stack)
    assert cli_stack.appid not in json.dumps(result), "legacy diagnostic bypassed per-call redaction"
    assert "E_LEGACY_NATIVE" in result["error"]
