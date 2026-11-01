"""Failed transports retain typed observations, not private diagnostic bodies.

The real registry and core entry points are exercised. CLI/HTTP fixtures do not
call a model; synthetic transport returns cover types unavailable on the wire.
"""
from __future__ import annotations

import copy
import json
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
KEY = "__ut_transport_failure_facts"
PRIVATE = "PRIVATE_FAILURE_BODY_SENTINEL_93ca"
sys.path.insert(0, str(SERVICE))


@pytest.fixture(autouse=True)
def stack(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ORCH))
    for name, relative in {
        "HOME": "home",
        "TMPDIR": "tmp",
        "MEYO_LEGION_DIR": "legion",
        "LEGION_HOME_DIR": "legion-home",
        "LEGION_TASK_MEMORY_DIR": "task-memory",
        "LEGION_SESSIONS_DIR": "sessions",
        "LEGION_EXTERNAL_SOURCE_ROOT": "sources",
        "LEGION_EXTERNAL_RUNTIME_ROOT": "runtimes",
    }.items():
        directory = tmp_path / relative
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setenv("AIGC_APPID", "offline-fixture-not-a-key")
    monkeypatch.setenv("MEYO_UNIFIED_APPID", "offline-fixture-not-a-key")
    monkeypatch.setenv("MEYO_UNIFIED_GATEWAY", "http://127.0.0.1:1/v1")
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setitem(sys.modules, "github_access",
                        SimpleNamespace(shared_environment=lambda env: env))
    connect = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple):
            assert address[0] in {"127.0.0.1", "::1", "localhost"}
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    import external_agent_registry as registry
    import legion_sandbox

    entry = {
        "key": KEY, "kind": "agent", "execution_role": "agent",
        "url": "https://github.com/example/offline-fixture",
        "revision": "a" * 40, "runtime": {},
        "adapter_capabilities": {"tools": False},
        "transports": [],
    }
    monkeypatch.setattr(registry, "load_catalog", lambda: [entry])
    monkeypatch.setattr(registry, "_probe_one", lambda *a, **kw: {
        "execution_ready": True, "status": "ready", "status_detail": ""})
    monkeypatch.setattr(registry, "_strip_dead_proxy", lambda env: dict(env))
    monkeypatch.setattr(legion_sandbox, "prepare_peer_sources", lambda *a, **kw: "")
    work = tmp_path / "work"
    work.mkdir()
    return SimpleNamespace(registry=registry, entry=entry, work=work, root=tmp_path,
                           monkeypatch=monkeypatch)


def run(stack, *, memory=None):
    return stack.registry.run(
        KEY, "Synthetic transport protocol check.", workdir=str(stack.work),
        model="offline-fixture", session_id="fixture-logical", timeout_s=5,
        runtime_context={"task_memory": memory or {}},
    )


@pytest.fixture
def http_fixture():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(self.path)
            if self.path == "/fail":
                status, payload = 503, {"error": "synthetic HTTP unavailable"}
            else:
                status, payload = 200, {
                    "final": "0",
                    "native_facts": {"completed": True, "turn_status": "completed"},
                    "usage": {"input_tokens": 7, "output_tokens": 1, "total_tokens": 8},
                }
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", requests=requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def real_transport(kind, success, http_fixture):
    if kind == "http":
        return {"type": "http", "url": http_fixture.url + ("/ok" if success else "/fail")}
    records = [
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "text", "part": {"type": "text", "text": "0" if success else PRIVATE}},
        {"type": "step_finish", "part": {
            "type": "step-finish", "reason": "stop" if success else "tool-calls"}},
        {"type": "usage", "usage": {
            "input_tokens": 3, "output_tokens": 1, "total_tokens": 4}},
    ]
    code = "import json\nfor row in " + repr(records) + ":\n print(json.dumps(row), flush=True)\n"
    return {"type": "cli", "command": [sys.executable, "-B", "-c", code]}


@pytest.mark.parametrize("order", [("cli", "http"), ("http", "cli")])
def test_real_two_transport_failures_keep_last_facts(stack, http_fixture, order):
    stack.entry["transports"] = [real_transport(kind, False, http_fixture) for kind in order]
    result = run(stack)
    failures = result["native_facts"]["transport_failures"]
    assert [f["transport"] for f in failures] == list(order)
    assert result["status"] == ("incomplete" if order[-1] == "cli" else "http503")
    cli = failures[order.index("cli")]
    assert cli["native_facts"]["process_returncode"] == 0
    assert cli["native_facts"]["completed"] is False
    assert cli["native_facts"]["turn_status"] == "incomplete"
    assert cli["native_facts"]["terminal_event"] == {
        "type": "step_finish", "reason": "tool-calls"}
    assert cli["usage_records"][0]["total_tokens"] == 4
    assert cli["elapsed"] >= 0
    if order[-1] == "cli":
        assert result["native_facts"]["process_returncode"] == 0
        assert result["native_facts"]["turn_status"] == "incomplete"
        assert result["usage_records"][0]["total_tokens"] == 4
    else:
        assert "process_returncode" not in result["native_facts"]
        assert "completed" not in result["native_facts"]
        assert "usage_records" not in result
    if order[-1] == "http":
        assert "synthetic HTTP unavailable" in result["error"]
    else:
        assert result["error"] == ""
    assert result["final"] == ""
    assert PRIVATE not in json.dumps(result)
    # Check private storage ownership, never read it back to recover an answer.
    for failure in failures:
        path = Path(failure["private_result_path"])
        assert path.is_relative_to(stack.work / ".sessions")
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("order", [("cli", "http"), ("http", "cli")])
def test_real_failure_then_success_does_not_import_failed_state(stack, http_fixture, order):
    stack.entry["transports"] = [
        real_transport(order[0], False, http_fixture),
        real_transport(order[1], True, http_fixture),
    ]
    result = run(stack)
    assert result["status"] == "ok"
    assert result["final"] == "0"
    assert result["error"] == ""
    assert result["native_facts"]["completed"] is True
    assert result["native_facts"]["turn_status"] == "completed"
    failures = result["native_facts"]["transport_failures"]
    assert len(failures) == 1 and failures[0]["transport"] == order[0]
    if order[0] == "cli":
        assert failures[0]["native_facts"]["completed"] is False
        assert "process_returncode" not in result["native_facts"]
    else:
        assert failures[0]["status"] == "http503"
        assert result["native_facts"]["process_returncode"] == 0
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("kind", ["cli", "http"])
def test_real_normal_success_is_unchanged(stack, http_fixture, kind):
    stack.entry["transports"] = [real_transport(kind, True, http_fixture)]
    result = run(stack)
    assert result["status"] == "ok" and result["final"] == "0"
    assert result["error"] == ""
    assert result["native_facts"]["completed"] is True
    assert "transport_failures" not in result
    assert "transport_failures" not in result["native_facts"]


def enqueue(stack, returns, kinds=("cli", "sdk")):
    stack.entry["transports"] = [{"type": kind, "command": ["fixture-only"]} for kind in kinds]
    calls = []

    def boundary(spec, context, env, timeout_s, **kwargs):
        index = len(calls)
        calls.append(copy.deepcopy({"spec": spec, "context": context}))
        value = returns[index]
        if isinstance(value, Exception):
            raise value
        return value

    stack.monkeypatch.setattr(stack.registry, "run_transport", boundary)
    return calls


def refusal(status="incomplete", rc=0):
    return {
        "status": status, "returncode": rc, "final": PRIVATE,
        "error": "original native diagnostic", "stderr": PRIVATE, "elapsed": .125,
        "native_facts": {
            "process_returncode": rc, "completed": False, "failed": rc != 0,
            "turn_status": "incomplete" if rc == 0 else "failed",
            "terminal_event": {"type": "step_finish", "reason": "length", "text": PRIVATE},
            "duration_ms": 125, "stderr": PRIVATE, "reasoning": PRIVATE,
        },
        "usage_records": [{"input_tokens": 11, "output_tokens": 0, "total_tokens": 11,
                           "source": "external_native_event", "model": PRIVATE, "text": PRIVATE}],
    }


def test_failure_history_snapshots_are_independent_and_last_wins(stack):
    first, last = refusal("rc7", 7), refusal()
    first["native_facts"]["duration_ms"] = 999
    first["usage_records"][0]["input_tokens"] = 99
    last["native_facts"]["cancelled"] = False
    enqueue(stack, [first, last])
    result = run(stack)
    facts = result["native_facts"]
    history = facts["transport_failures"]
    assert result["status"] == "incomplete"
    assert facts["process_returncode"] == 0 and facts["failed"] is False
    assert facts["cancelled"] is False and facts["duration_ms"] == 125
    assert [f["native_facts"]["process_returncode"] for f in history] == [7, 0]
    assert [f["usage_records"][0]["input_tokens"] for f in history] == [99, 11]
    assert result["usage_records"][0]["input_tokens"] == 11
    first["native_facts"]["completed"] = True
    last["native_facts"]["terminal_event"]["reason"] = PRIVATE
    assert history[0]["native_facts"]["completed"] is False
    assert facts["terminal_event"]["reason"] == "length"
    assert result["error"] == "original native diagnostic"
    assert PRIVATE not in json.dumps(result)


def test_previous_failure_diagnostic_does_not_reclassify_last_failure(stack):
    first, last = refusal("rc7", 7), refusal()
    first["error"] = PRIVATE
    first["error_envelope"] = {
        "category": "runtime_budget", "scope": "request", "retryable": False,
        "code": "request_total_budget_exhausted", "message": PRIVATE}
    last["error"] = "last native diagnostic"
    enqueue(stack, [first, last])
    result = run(stack)
    assert result["status"] == "incomplete"
    assert result["error"] == "last native diagnostic"
    assert "error_envelope" not in result
    assert result["transport_failures"][0]["error_envelope"]["category"] == "runtime_budget"
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("field,value", [
    ("completed", "false"), ("failed", 1), ("process_returncode", False),
    ("turn_status", PRIVATE), ("turn_exit_reason", PRIVATE),
    ("duration_ms", float("inf")), ("elapsed_s", -1), ("num_turns", "3"),
])
def test_machine_projection_requires_types_and_closed_enums(stack, field, value):
    payload = refusal()
    payload.pop("returncode")
    payload["native_facts"][field] = value
    enqueue(stack, [payload], kinds=("sdk",))
    result = run(stack)
    assert field not in result["native_facts"]
    assert field not in result["transport_failures"][0]["native_facts"]
    assert PRIVATE not in json.dumps(result)


def test_unknown_terminal_reason_and_untyped_usage_stay_private(stack):
    payload = refusal()
    payload["native_facts"]["terminal_event"] = {
        "type": "step_finish", "reason": PRIVATE, "message": PRIVATE}
    payload["usage_records"] = [{
        "input_tokens": "200", "output_tokens": True, "total_tokens": -1,
        "cost_usd": float("nan"), "source": PRIVATE, "granularity": PRIVATE,
        "input_tokens_details": {"cached_tokens": 0, "secret": PRIVATE},
    }]
    enqueue(stack, [payload], kinds=("sdk",))
    result = run(stack)
    assert result["native_facts"]["terminal_event"] == {"type": "step_finish"}
    usage = result["usage_records"][0]
    assert usage["input_tokens_details"] == {"cached_tokens": 0}
    assert not any(name in usage for name in (
        "input_tokens", "output_tokens", "total_tokens", "cost_usd", "source", "granularity"))
    assert PRIVATE not in json.dumps(result)


def test_latest_unknown_facts_do_not_fall_back_to_previous_failure(stack):
    first = refusal("rc8", 8)
    last = {"status": "error", "final": PRIVATE, "error": "latest untyped failure"}
    enqueue(stack, [first, last])
    result = run(stack)
    assert result["status"] == "error"
    assert "process_returncode" not in result["native_facts"]
    assert "completed" not in result["native_facts"]
    assert "usage_records" not in result
    assert result["transport_failures"][0]["native_facts"]["process_returncode"] == 8
    assert result["error"].endswith("latest untyped failure")
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("category,status", [
    ("user_cancel", "cancelled"), ("runtime_budget", "error"),
])
def test_latest_error_envelope_and_original_session_are_preserved(stack, category, status):
    first, last = refusal("rc7", 7), refusal(status, 130)
    envelope = {
        "category": category, "scope": "request", "retryable": False,
        "code": "request_total_budget_exhausted" if category == "runtime_budget" else "cancelled",
        "message": "original structured failure",
        "details": {"dimension": "input_tokens", "used": 7, "limit": 7},
    }
    checkpoint = {"session_id": "native-fixture", "state": {"cursor": "fixture-cursor"}}
    first["native_checkpoint"] = {"session_id": "older-fixture"}
    last.update(error_envelope=envelope, native_checkpoint=checkpoint, resumed_native=True)
    last["native_facts"].update(cancelled=category == "user_cancel",
                                interrupted=category == "user_cancel")
    calls = enqueue(stack, [first, last])
    stack.entry["transports"][1]["resume_command"] = ["resume-fixture", "{session_id}"]
    result = run(stack, memory={"native_checkpoint": {"session_id": "existing-fixture"}})
    assert result["status"] == status
    assert result["error_envelope"] == envelope
    assert result["native_checkpoint"] == checkpoint
    assert result["resumed_native"] is True
    assert calls[1]["spec"]["command"] == ["resume-fixture", "{session_id}"]
    assert calls[1]["context"]["session_id"] == "existing-fixture"
    assert result["native_facts"]["process_returncode"] == 130
    assert result["native_facts"]["cancelled"] is (category == "user_cancel")
    observed = result["transport_failures"][-1]["error_envelope"]
    assert observed["category"] == category and observed["retryable"] is False
    assert observed["code"] == envelope["code"]
    assert observed["details"] == envelope["details"]
    assert "message" not in observed
    assert PRIVATE not in json.dumps(result)


def test_previous_cancel_budget_does_not_replace_success_top_level(stack):
    first = refusal("cancelled", 130)
    first["native_facts"].update(cancelled=True, interrupted=True)
    first["error_envelope"] = {
        "category": "runtime_budget", "scope": "request", "retryable": False,
        "code": "request_total_budget_exhausted", "message": PRIVATE}
    success = {"status": "ok", "final": "0", "error": "", "usage": {"output_tokens": 0}}
    enqueue(stack, [first, success])
    result = run(stack)
    assert result["status"] == "ok" and result["final"] == "0"
    assert "error_envelope" not in result
    assert result["native_facts"].keys() == {"transport_failures"}
    history = result["native_facts"]["transport_failures"]
    assert history[0]["native_facts"]["cancelled"] is True
    assert history[0]["error_envelope"]["category"] == "runtime_budget"
    assert result["usage"] == {"output_tokens": 0}
    assert PRIVATE not in json.dumps(result)


def test_exception_attached_machine_facts_survive_without_body(stack):
    failure = subprocess.TimeoutExpired(["synthetic-fixture"], 5)
    failure.native_facts = {"parent_timeout": True, "interrupted": True,
                            "completed": False, "stderr": PRIVATE}
    failure.native_checkpoint = {"session_id": "exception-fixture"}
    enqueue(stack, [refusal("rc7", 7), failure])
    result = run(stack)
    assert result["native_facts"]["parent_timeout"] is True
    assert result["native_facts"]["interrupted"] is True
    assert "process_returncode" not in result["native_facts"]
    assert result["native_checkpoint"] == {"session_id": "exception-fixture"}
    assert result["transport_failures"][-1]["exception_type"] == "TimeoutExpired"
    assert PRIVATE not in json.dumps(result)


def test_unknown_event_type_cannot_promote_a_reason_into_a_terminal_frame(stack):
    payload = refusal()
    payload["native_facts"]["terminal_event"] = {"type": PRIVATE, "reason": "length"}
    enqueue(stack, [payload], kinds=("sdk",))
    result = run(stack)
    assert "terminal_event" not in result["native_facts"]
    assert PRIVATE not in json.dumps(result)


def test_status_and_exception_fields_are_not_free_text_output_channels(stack):
    payload = refusal()
    payload.update(status=PRIVATE, exception_type=PRIVATE)
    enqueue(stack, [payload], kinds=("sdk",))
    result = run(stack)
    assert result["status"] == "error"
    assert result["transport_failures"][0]["status"] == "error"
    assert "exception_type" not in result["transport_failures"][0]
    assert PRIVATE not in json.dumps(result)


def test_usage_aliases_zero_cache_semantics_and_identity_dedup_survive(stack):
    import token_telemetry

    payload = refusal()
    usage = {
        "input": 10, "cacheRead": 7, "cacheWrite": 3, "output": 0, "total": 10,
        "input_semantics": "cache_buckets", "granularity": "request",
        "usage_identity": PRIVATE, "model": PRIVATE,
    }
    payload["usage_records"] = [usage, copy.deepcopy(usage)]
    enqueue(stack, [payload], kinds=("sdk",))
    result = run(stack)
    assert len(result["usage_records"]) == 1
    native = result["usage_records"][0]
    assert native["output"] == 0 and native["input_semantics"] == "cache_buckets"
    assert "usage_identity" not in native
    expected = token_telemetry.normalize(usage)
    actual = token_telemetry.normalize(native)
    for field in ("input_tokens", "cache_read_tokens", "cache_write_tokens",
                  "output_tokens", "total_tokens"):
        assert actual[field] == expected[field]
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("reason", ["max_iterations_reached", "max_iterations_reached(5/5)"])
def test_existing_parameterized_budget_reason_is_preserved_for_core(stack, reason):
    import legion_core

    payload = refusal()
    payload["native_facts"]["turn_exit_reason"] = reason
    enqueue(stack, [payload], kinds=("sdk",))
    result = run(stack)
    assert result["native_facts"]["turn_exit_reason"] == reason
    assert legion_core._native_budget_exit(result) is True
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("success", [False, True])
def test_real_core_external_entry_consumes_registry_facts(stack, http_fixture, success):
    import harness_registry
    import legion_core
    import legion_sandbox

    stack.monkeypatch.setattr(legion_sandbox, "workdir", lambda *a, **kw: str(stack.work))
    stack.monkeypatch.setattr(legion_core, "_stable_prelude", lambda *a, **kw: "")
    stack.monkeypatch.setattr(harness_registry, "_external_adapter_capabilities",
                             lambda key: {"tools": False})
    stack.monkeypatch.setattr(harness_registry, "_legion_prompt", lambda task, ctx, **kw: task)
    stack.entry["transports"] = [
        real_transport("http", False, http_fixture),
        real_transport("cli", success, http_fixture),
    ]
    result = legion_core._run_external_inprocess(
        KEY, "Synthetic core consumer check.", "__ut_transport_core",
        model_override="offline-fixture", appid_override="offline-fixture-not-a-key",
        session_id="core-fixture", user_id="fixture-owner", timeout_s=5)
    assert result["status"] == ("ok" if success else "incomplete")
    assert result["native_facts"]["process_returncode"] == 0
    assert result["native_facts"]["completed"] is success
    assert result["native_facts"]["turn_status"] == ("completed" if success else "incomplete")
    assert result["final"] == ("0" if success else "")
    assert result["usage_records"][0]["total_tokens"] == 4
    assert result["native_facts"]["transport_failures"][0]["status"] == "http503"
    assert PRIVATE not in json.dumps(result)


@pytest.mark.parametrize("success", [False, True])
def test_real_run_one_preserves_last_transport_state_and_history(stack, http_fixture, success):
    import harness_registry
    import legion_core as core
    import legion_sandbox
    import runtime_manifest
    import task_memory

    stack.monkeypatch.setattr(legion_sandbox, "workdir", lambda *a, **kw: str(stack.work))
    stack.monkeypatch.setattr(core, "_harness_keys", lambda: {KEY})
    stack.monkeypatch.setattr(core, "_ORCH_OK", True)
    stack.monkeypatch.setattr(core, "_dynamic_max_attempts", lambda pool: 1)
    stack.monkeypatch.setattr(core, "_fallback_candidates", lambda *a, **kw: [])
    stack.monkeypatch.setattr(core, "_stable_prelude", lambda *a, **kw: "")
    stack.monkeypatch.setattr(core, "record_model_health", lambda *a, **kw: None)
    stack.monkeypatch.setattr(core, "_record_gateway_call", lambda *a, **kw: None)
    stack.monkeypatch.setattr(runtime_manifest, "supports_depth", lambda *a, **kw: True)
    stack.monkeypatch.setattr(task_memory, "ROOT", stack.root / "task-memory")
    stack.monkeypatch.setattr(harness_registry, "_external_adapter_capabilities",
                             lambda key: {"tools": False})
    stack.monkeypatch.setattr(harness_registry, "_legion_prompt", lambda task, ctx, **kw: task)
    stack.entry["transports"] = [
        real_transport("http", False, http_fixture),
        real_transport("cli", success, http_fixture),
    ]
    result = core.run_one(
        KEY, "Synthetic transport-consumption check.", qhash="__ut_transport_run_one",
        preferred_model="offline-fixture", preferred_appid="offline-fixture-not-a-key",
        attempt_timeout=5, user_id="fixture-owner", conversation_id="fixture-conversation",
        task_facts={"project_context": {"workspace_root": str(stack.work),
                                        "user_id": "fixture-owner"}},
    )
    assert http_fixture.requests == ["/fail"], result
    assert result["native_facts"]["process_returncode"] == 0
    assert result["native_facts"]["completed"] is success
    assert result["native_facts"]["turn_status"] == ("completed" if success else "incomplete")
    assert result["status"] == ("done" if success else "incomplete")
    assert result["final"] == ("0" if success else "")
    assert result["native_facts"]["transport_failures"][0]["status"] == "http503"
    assert result["usage"]["available"] is True
    assert result["usage"]["total_tokens"] == 4
    assert PRIVATE not in json.dumps(result)
