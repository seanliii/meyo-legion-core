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
