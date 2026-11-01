"""Penguin native JSONL channels, replayed offline without running a model.

Structure comes from the retained 2026-11-01 trace: session_meta, user text,
request_begin, assistant thinking, assistant text, token_usage, request_end.
All private text here is a synthetic sentinel, never a copied model trace.
"""
from __future__ import annotations

import importlib
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
PRIVATE = "PRIVATE_THINKING_SENTINEL"
QUESTION = "Return the requested public result."


def event(kind, **fields):
    return {"timestamp": "2026-11-01T03:27:20.000Z", "type": "event_msg",
            "payload": {"type": kind, **fields}}


def message(role, kind, value, *, stop="completed"):
    field = "thinking" if kind == "thinking" else "text"
    return {"timestamp": "2026-11-01T03:27:20.000Z", "type": "model_msg",
            "payload": {"type": kind, "role": role, field: value, "stop_reason": stop}}


def complete(answer="public result", *, question=QUESTION):
    return [
        message("user", "text", question), event("request_begin"),
        message("assistant", "thinking", PRIVATE),
        message("assistant", "text", answer),
        event("token_usage", request={"cache_read": 2, "cache_write": 3, "output": 7, "total": 12}),
        event("request_end", status="completed"),
    ]


@pytest.fixture
def penguin(tmp_path, monkeypatch):
    for name, leaf in (
        ("HOME", "home"), ("PENGUIN_HOME", "data"), ("MEYO_LEGION_DIR", "meyo"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_PROC_REGISTRY_DIR", "processes"),
    ):
        directory = tmp_path / leaf
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.syspath_prepend(str(ORCH))
    monkeypatch.syspath_prepend(str(SERVICE))
    hr = importlib.import_module("harness_registry")
    monkeypatch.setattr(hr._INPROC_RUNTIME_ENV, "values", {}, raising=False)
    monkeypatch.setattr(hr._sh, "which", lambda name: "/offline/penguin" if name == "penguin" else None)
    monkeypatch.setattr(hr, "_legion_prompt", lambda text, ctx: text)
    monkeypatch.setattr(hr, "_prior_attempts_prelude", lambda attempts: "")

    def forbidden(*args, **kwargs):
        raise AssertionError("this test must not launch a process or contact a model")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    work = tmp_path / "work"
    work.mkdir()
    traces = tmp_path / "data/default_project/agents/default_agent/traces"
    state = SimpleNamespace(
        hr=hr, root=tmp_path, work=work, traces=traces, writes=[], calls=[], events=[],
        stdout=f"{PRIVATE}\nconsole output is not the native final\n",
        stderr="", rc=0, checkpoint={}, exception=None, cancelled=False)

    def write(sid, rows, *, workspace=None, append=False, metadata=True,
              filename_sid=None, segment=1, day=None):
        path = state.traces / (day or time.strftime("%Y-%m-%d")) / f"{filename_sid or sid}_{segment:03}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        records = list(rows)
        if metadata and not append:
            records.insert(0, {"type": "session_meta", "payload": {
                "session_id": sid, "workspace": str(workspace or state.work),
                "model_id": "offline-model", "system_prompt": "synthetic private system context",
            }})
        with path.open("a" if append else "w") as out:
            for row in records:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    state.write = write

    def run(command, *args, **kwargs):
        state.calls.append((command, kwargs))
        for write_action in state.writes:
            write_action()
        event_context = hr._RunEventContext(state.events.append)
        for line in state.stdout.splitlines():
            hr._emit_harness_event("penguin", "stdout", line, event_context)
        if state.exception is not None:
            raise state.exception
        result = subprocess.CompletedProcess(command, state.rc, state.stdout, state.stderr)
        result.cancelled = state.cancelled
        return result

    monkeypatch.setattr(hr, "_run_task", run)
    return state


def invoke(case):
    ctx = case.hr.HarnessCtx(
        model="offline-model", appid="offline-key", gateway="https://offline.example.invalid/v1",
        workdir=str(case.work), memory_dir=str(case.root / "memory"),
        session_id="offline-conversation", task_memory={"native_checkpoint": case.checkpoint})
    return case.hr._penguin_adapter("__ut_penguin_final", QUESTION, ctx=ctx)


def assert_private_not_exported(case, result):
    assert PRIVATE not in json.dumps({"result": result, "events": case.events}), (
        "thinking channel leaked through the adapter or public event channel")


@pytest.mark.parametrize("answer", [
    "好", "Error: empty list / connection", "Ready. Please send the assigned subtask.",
    "  leading space\ntrailing space  ", "Thinking is just a word in this public answer.",
])
def test_only_completed_native_text_is_final_without_word_or_length_filters(penguin, answer):
    penguin.stdout = f"{PRIVATE}\n{answer}\n{PRIVATE}\n"
    penguin.writes = [lambda: penguin.write("session-current", complete(answer))]
    result = invoke(penguin)
    assert result["status"] == "ok", result
    assert result["final"] == answer
    assert result["native_checkpoint"]["session_id"] == "session-current"
    assert result["usage"]["input_tokens"] == 5
    assert result["usage"]["output_tokens"] == 7
    assert len(penguin.calls) == 1
    assert "--thinking" not in penguin.calls[0][0], "do not solve channel parsing by disabling thinking"
    assert_private_not_exported(penguin, result)


@pytest.mark.parametrize("problem", [
    "wrong_session", "wrong_header", "wrong_workspace", "old_offset", "missing_end",
    "thinking_only", "failed_text", "later_unfinished_request",
])
def test_missing_current_completed_text_never_reuses_stdout_or_old_final(penguin, problem):
    sid = "session-current"
    penguin.checkpoint = {"kind": "penguin_session", "session_id": sid}
    if problem == "old_offset":
        penguin.write(sid, complete("OLD FINAL"))
    elif problem == "wrong_session":
        penguin.writes = [lambda: penguin.write("session-other", complete("OTHER FINAL"))]
    elif problem == "wrong_header":
        penguin.writes = [lambda: penguin.write("session-other", complete(), filename_sid=sid)]
    elif problem == "wrong_workspace":
        penguin.writes = [lambda: penguin.write(sid, complete(), workspace=penguin.root / "other")]
    elif problem == "missing_end":
        penguin.writes = [lambda: penguin.write(sid, complete()[:-1])]
    elif problem == "thinking_only":
        penguin.writes = [lambda: penguin.write(sid, [
            message("user", "text", QUESTION), event("request_begin"),
            message("assistant", "thinking", PRIVATE), event("request_end", status="completed")])]
    elif problem == "failed_text":
        penguin.writes = [lambda: penguin.write(sid, [
            message("user", "text", QUESTION), event("request_begin"),
            message("assistant", "text", "not a completed answer", stop="failed"),
            event("request_end", status="completed")])]
    else:
        penguin.writes = [lambda: penguin.write(sid, [
            *complete("EARLIER FINAL"), event("request_begin"),
            message("assistant", "thinking", PRIVATE)])]
    result = invoke(penguin)
    assert result["status"] != "ok"
    assert result["final"] == ""
    assert_private_not_exported(penguin, result)


def test_resume_reads_only_the_appended_request(penguin):
    sid = "session-resumed"
    path = penguin.write(sid, complete("OLD FINAL"))
    old_size = path.stat().st_size
    penguin.checkpoint = {"kind": "penguin_session", "session_id": sid}
    penguin.writes = [lambda: penguin.write(sid, complete("NEW FINAL"), append=True)]
    result = invoke(penguin)
    assert result["final"] == "NEW FINAL"
    assert result["usage"]["num_turns"] == 1
    assert result["native_checkpoint"]["session_id"] == sid
    assert result["resumed_native"] is True
    assert path.stat().st_size > old_size
    assert_private_not_exported(penguin, result)


def test_fresh_session_binds_to_the_actual_request_not_the_latest_mtime(penguin):
    penguin.writes = [
        lambda: penguin.write("session-mine", complete("MINE")),
        lambda: penguin.write("session-foreign", complete("FOREIGN", question="another request")),
    ]
    result = invoke(penguin)
    assert result["final"] == "MINE"
    assert result["native_checkpoint"]["session_id"] == "session-mine"


def test_ambiguous_matching_fresh_sessions_are_not_guessed(penguin):
    penguin.writes = [
        lambda: penguin.write("session-one", complete("FIRST")),
        lambda: penguin.write("session-two", complete("SECOND")),
    ]
    result = invoke(penguin)
    assert result["status"] != "ok"
    assert result["final"] == ""
    assert_private_not_exported(penguin, result)


def test_compaction_text_after_completion_is_not_the_user_final(penguin):
    penguin.writes = [lambda: penguin.write("session-current", [
        *complete("USER ANSWER"), event("compaction_begin"),
        event("request_begin"), message("assistant", "text", "PRIVATE COMPACTION SUMMARY"),
        event("request_end", status="completed"), event("compaction_end"),
    ])]
    result = invoke(penguin)
    assert result["status"] == "ok"
    assert result["final"] == "USER ANSWER"
    assert "PRIVATE COMPACTION SUMMARY" not in json.dumps(result)
    assert result["native_events"] == ["compaction_begin", "compaction_end"]


@pytest.mark.parametrize("status", ["failed", "auth", "timeout", "aborted"])
def test_native_noncompletion_cannot_be_overridden_by_cli_zero(penguin, status):
    penguin.writes = [lambda: penguin.write("session-current", [
        message("user", "text", QUESTION), event("request_begin"),
        message("assistant", "text", "intermediate output"),
        event("request_end", status=status),
    ])]
    result = invoke(penguin)
    assert result["status"] != "ok"
    assert result["final"] == ""
    assert_private_not_exported(penguin, result)


def test_existing_usage_helper_keeps_its_three_value_contract(penguin):
    traces, offsets = penguin.hr._penguin_trace_offsets()
    penguin.write("session-current", complete("ANSWER"))
    usage, sid, events = penguin.hr._penguin_usage_after(
        traces, offsets, workdir=str(penguin.work), resume_id="session-current")
    assert sid == "session-current"
    assert usage["total_tokens"] == 12 and usage["num_turns"] == 1
    assert events == []


def test_effective_native_home_is_used_for_trace_offsets(penguin, monkeypatch):
    data = penguin.root / "request-home"
    penguin.traces = data / "default_project/agents/default_agent/traces"
    monkeypatch.setattr(penguin.hr._INPROC_RUNTIME_ENV, "values", {"PENGUIN_HOME": str(data)})
    penguin.writes = [lambda: penguin.write("session-current", complete("RIGHT HOME"))]
    result = invoke(penguin)
    assert result["status"] == "ok"
    assert result["final"] == "RIGHT HOME"


@pytest.mark.parametrize("status,detail,category,retryable", [
    ("failed", "HTTP 400 Bad Request: unsupported model parameter", "model_config", True),
    ("failed", "HTTP 503 upstream unavailable", "gateway", True),
    ("failed", "HTTP 429 quota exceeded", "rate_limit", True),
    ("auth", "Invalid API key", "authorization_required", False),
    ("timeout", "", "timeout", True),
])
def test_native_error_detail_survives_abort_without_exporting_private_diagnostics(
        penguin, status, detail, category, retryable):
    errors = importlib.import_module("error_envelope")
    # error_message is the actual wire field; permanent/retryable are NOT wire fields.
    raw = detail + " " + PRIVATE + " bearer=synthetic-secret" if detail else ""
    penguin.rc = 1
    penguin.stderr = "optional integration warning"
    penguin.writes = [lambda: penguin.write("session-current", [
        message("user", "text", QUESTION), event("request_begin"),
        event("request_end", status=status, error_message=raw, attempt=3),
        event("abort", reason="llm request error: " + raw),
    ])]
    result = invoke(penguin)
    assert result["final"] == "" and result["status"] != "ok"
    facts = result["native_facts"]
    assert facts["request_status"] == status, "abort must not erase the request's failure"
    assert facts["request_end"] == {"type": "request_end", "status": status, "attempt": 3}
    assert facts["aborted"] is True
    cause = facts["terminal_error"]
    assert cause["source"] == "penguin_trace.request_end"
    assert cause["native_retryable"] is None and cause["native_scope"] is None
    if raw:
        assert cause["message_chars"] == len(raw)
        assert cause["message_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    envelope = result["error_envelope"]
    assert envelope["category"] == category and envelope["retryable"] is retryable
    normalized = errors.normalize(
        result["status"], result["error"], supplied=envelope, native_facts=facts)
    assert normalized.category == category and normalized.retryable is retryable
    assert errors.requires_request_budget_pause(result) is False
    assert "synthetic-secret" not in json.dumps(result)
    assert_private_not_exported(penguin, result)


def test_model_configuration_stderr_before_trace_is_not_lost(penguin):
    penguin.rc = 1
    penguin.stderr = "Model is not in the Project config: model_id=offline-model " + PRIVATE
    result = invoke(penguin)
    assert result["final"] == ""
    assert result["native_facts"]["terminal_error"]["source"] == "penguin_stderr"
    assert result["error_envelope"]["category"] == "model_config"
    assert result["error_envelope"]["retryable"] is True
    assert_private_not_exported(penguin, result)


def test_unknown_native_failure_does_not_invent_budget_scope_or_retry_permission(penguin):
    errors = importlib.import_module("error_envelope")
    penguin.writes = [lambda: penguin.write("session-current", [
        message("user", "text", QUESTION), event("request_begin"),
        event("request_end", status="failed", error_message="opaque native failure " + PRIVATE),
        event("abort", reason="llm request error"),
    ])]
    result = invoke(penguin)
    cause = result["native_facts"]["terminal_error"]
    assert cause["category"] == "unknown"
    assert cause["native_retryable"] is None and cause["native_scope"] is None
    assert errors.requires_request_budget_pause(result) is False
    assert_private_not_exported(penguin, result)


@pytest.mark.parametrize("latest_status", ["completed", "failed"])
def test_latest_noncompaction_request_owns_its_text_and_error(penguin, latest_status):
    latest = [
        event("request_begin"), message("assistant", "text", "LATEST"),
        event("request_end", status=latest_status,
              **({"error_message": "HTTP 400 Bad Request"} if latest_status == "failed" else {})),
    ]
    penguin.writes = [lambda: penguin.write("session-current", [
        message("user", "text", QUESTION), event("request_begin"),
        event("request_end", status="timeout", error_message="HTTP 503", attempt=1, retry_in_ms=500),
        *latest,
    ])]
    result = invoke(penguin)
    assert result["native_facts"]["request_status"] == latest_status
    assert "retry_in_ms" not in result["native_facts"]["request_end"]
    if latest_status == "completed":
        assert result["status"] == "ok" and result["final"] == "LATEST"
        assert result["error"] == "" and not result.get("error_envelope")
        assert result["native_facts"]["terminal_error"] is None
    else:
        assert result["final"] == ""
        assert result["error_envelope"]["category"] == "model_config"


def test_retry_delay_is_reported_as_a_native_plan_not_a_request_budget(penguin):
    errors = importlib.import_module("error_envelope")
    penguin.writes = [lambda: penguin.write("session-current", [
        message("user", "text", QUESTION), event("request_begin"),
        event("request_end", status="timeout", attempt=2, retry_in_ms=1250),
    ])]
    result = invoke(penguin)
    assert result["native_facts"]["request_end"]["retry_in_ms"] == 1250
    assert result["native_facts"]["terminal_error"]["native_retryable"] is None
    assert errors.requires_request_budget_pause(result) is False


@pytest.mark.parametrize("ending", ["abort", "compaction_failure", "compaction_then_abort"])
def test_compaction_and_abort_do_not_supply_or_resurrect_a_final(penguin, ending):
    rows = complete("USER ANSWER")
    if ending != "abort":
        rows += [
            event("compaction_begin", reason="context", mode="summarize", context=10, turns=1),
            event("request_begin"), message("assistant", "text", PRIVATE),
            event("request_end", status="auth", error_message="HTTP 403 " + PRIVATE),
            event("compaction_end", reason="context", mode="summarize", status="failed",
                  error_message=PRIVATE),
        ]
    if ending != "compaction_failure":
        rows += [event("abort", reason="aborted by user")]
    penguin.writes = [lambda: penguin.write("session-current", rows)]
    result = invoke(penguin)
    if ending == "compaction_failure":
        assert result["status"] == "ok" and result["final"] == "USER ANSWER"
        assert result["error"] == "" and not result.get("error_envelope")
    else:
        assert result["status"] != "ok" and result["final"] == ""
        assert result["native_facts"]["completed"] is False
    assert_private_not_exported(penguin, result)


@pytest.mark.parametrize("resumed", [False, True])
def test_later_user_input_in_same_session_is_not_this_invocations_answer(penguin, resumed):
    sid = "session-current"
    if resumed:
        penguin.checkpoint = {"session_id": sid}
    penguin.writes = [lambda: penguin.write(sid, [
        *complete("MINE"), *complete("OTHER", question="a different invocation"),
    ])]
    result = invoke(penguin)
    assert result["status"] != "ok" and result["final"] == ""


@pytest.mark.parametrize("resumed", [False, True])
@pytest.mark.parametrize("opening,closing", [
    ("[context_summary]", "[/context_summary]"),
    ("<context_summary>", "</context_summary>"),
])
def test_compaction_rotation_keeps_only_the_followup_native_text(penguin, resumed, opening, closing):
    if resumed:
        penguin.write("session-current", complete("OLD FINAL"))
        penguin.checkpoint = {"kind": "penguin_session", "session_id": "session-current"}
    penguin.writes = [
        lambda: penguin.write("session-current", [
            message("user", "text", QUESTION), event("request_begin"),
            {"type": "model_msg", "payload": {
                "type": "tool_call", "role": "assistant", "tool_call_id": "one",
                "name": "read_file", "arguments": {}, "stop_reason": "completed"}},
            event("request_end", status="completed"),
            event("compaction_begin", reason="context", mode="summarize", context=10, turns=1),
            event("request_begin"), message("assistant", "text", PRIVATE),
            event("request_end", status="completed"),
            event("compaction_end", reason="context", mode="summarize", status="completed"),
        ], segment=1, append=resumed),
        lambda: penguin.write("session-current", [
            # Penguin buildContextSummaryText wraps the native context, rather
            # than treating an arbitrary new human message as a continuation.
            message("user", "text", opening + "\nsynthetic compacted context\n" + closing),
            event("request_begin"), message("assistant", "text", "AFTER ROTATION"),
            event("request_end", status="completed"),
        ], segment=2),
    ]
    result = invoke(penguin)
    assert result["status"] == "ok" and result["final"] == "AFTER ROTATION"
    assert_private_not_exported(penguin, result)


def test_harness_background_completion_is_not_a_new_human_invocation(penguin):
    notice = message("user", "text", "synthetic background tool completion")
    notice["payload"]["sender"] = "harness"
    penguin.writes = [lambda: penguin.write("session-current", [
        message("user", "text", QUESTION), event("request_begin"),
        {"type": "model_msg", "payload": {
            "type": "tool_call", "role": "assistant", "tool_call_id": "one",
            "name": "background_command", "arguments": {}, "stop_reason": "completed"}},
        event("request_end", status="completed"), notice,
        event("request_begin"), message("assistant", "text", "AFTER BACKGROUND"),
        event("request_end", status="completed"),
    ])]
    result = invoke(penguin)
    assert result["status"] == "ok" and result["final"] == "AFTER BACKGROUND"


def test_rotation_after_compaction_does_not_authorize_an_unrelated_human_request(penguin):
    penguin.writes = [
        lambda: penguin.write("session-current", [
            *complete("MINE"), event("compaction_begin"),
            event("request_begin"), message("assistant", "text", PRIVATE),
            event("request_end", status="completed"), event("compaction_end", status="completed"),
        ], segment=1),
        lambda: penguin.write("session-current", complete("OTHER", question="a different human request"),
                             segment=2),
    ]
    result = invoke(penguin)
    assert result["status"] != "ok" and result["final"] == ""
    assert_private_not_exported(penguin, result)


def test_failed_offset_snapshot_cannot_reuse_an_existing_completed_trace(penguin, monkeypatch):
    penguin.write("session-current", complete("OLD FINAL"))
    original = penguin.hr._penguin_trace_files
    calls = []

    def once_unavailable(*args):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("synthetic unreadable snapshot")
        return original(*args)

    monkeypatch.setattr(penguin.hr, "_penguin_trace_files", once_unavailable)
    result = invoke(penguin)
    assert result["status"] != "ok" and result["final"] == ""


def test_parent_runtime_budget_keeps_its_existing_scope_and_nonretryable_fact(penguin):
    errors = importlib.import_module("error_envelope")
    exc = subprocess.TimeoutExpired(["offline-penguin"], 3)
    exc.runtime_reason = "runtime_budget"
    exc.legion_stdout = PRIVATE
    penguin.exception = exc
    penguin.writes = [lambda: penguin.write("session-current", [
        message("user", "text", QUESTION), event("request_begin"),
        event("request_end", status="failed", error_message="HTTP 400 Bad Request"),
    ])]
    result = invoke(penguin)
    assert result["status"] == "timeout"
    assert result["error_envelope"] == penguin.hr._runtime_timeout(exc)["error_envelope"]
    assert result["error_envelope"]["retryable"] is False
    assert result["error_envelope"]["scope"] == "harness", "do not manufacture a request-total budget"
    assert errors.requires_request_budget_pause(result) is False
    assert result["native_facts"].get("process_returncode") is None
    assert result["native_checkpoint"]["session_id"] == "session-current"
    assert_private_not_exported(penguin, result)
