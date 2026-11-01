"""A successful native answer is content, not an inferred transport error."""
import copy
import json

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one


def completed_facts(kind="codex"):
    facts = {"process_returncode": 0}
    if kind == "codex":
        facts.update(turn_status="completed", turn_result={"type": "turn.completed"})
    else:
        facts.update(completed=True, failed=False, interrupted=False)
    return facts


def absent_message_facts():
    return {
        **completed_facts(),
        "completion_message": {
            "state": "absent", "thread_id": "original-native", "turn_id": "this-turn",
            "last_agent_message": None, "source": "codex_task_complete_event",
        },
        "last_message_output": {"text": "检查仍在进行。"},
    }


def absent_message_result(**overrides):
    return {
        "status": "partial", "final": "", "error": "",
        "native_facts": absent_message_facts(),
        "native_checkpoint": {"kind": "codex_thread", "session_id": "original-native"},
        "checkpoint_writes": {"request": True},
        **overrides,
    }


def test_terminal_context_is_a_bounded_view_not_a_second_full_trace(explicit_run_one):
    raw = absent_message_result()
    raw["native_facts"]["last_message_output"]["text"] = "公开过程记录。" * 10000
    observation = explicit_run_one.core._native_terminal_message_absent(raw)
    original = copy.deepcopy(observation)
    text = explicit_run_one.core._native_terminal_message_context({"maker": observation})
    assert len(text) < 5000, "a 60k output file must not flood the next turn"
    view = json.loads(text.split("\n")[-1])
    output = view["maker"]["last_message_output"]
    assert output["text_truncated"] is True
    assert output["text_char_count"] == len(original["last_message_output"]["text"])
    assert output["text"] and len(output["text"]) <= 1500
    assert view["maker"]["thread_id"] == "original-native"
    assert view["maker"]["turn_id"] == "this-turn"
    assert "不是本轮最终答复" in text
    assert observation == original, "the full public observation remains the source of truth"


def test_legacy_nondict_error_envelope_does_not_become_a_new_failure_channel(explicit_run_one):
    raw = absent_message_result(error_envelope="legacy non-structured diagnostic")
    assert explicit_run_one.core._native_terminal_message_absent(raw)["checkpoint_saved"] is True


def execute_result(case, native):
    attempts = []
    work = case.root / "work"
    work.mkdir(exist_ok=True)

    def execute(*_args, **_kwargs):
        attempts.append(True)
        return {"workdir": str(work), "model_used": "fixture-model", **copy.deepcopy(native)}

    case.monkeypatch.setattr(case.core, "_run_core_inprocess", execute)
    result = case.core.run_one(
        "codex", "Return the requested useful result", qhash="__ut_native_final_diagnostics",
        user_id="owner-a", conversation_id="original-session", execution_depth="quick",
        task_facts={"project_context": {"repo_root": str(work), "user_id": "owner-a"}},
    )
    return result, attempts


@pytest.mark.parametrize("kind", ["codex", "completed_flag"])
@pytest.mark.parametrize("answer", [
    "OK — model-connection-check.txt",
    "HTTP 429 表示限流，HTTP 502 表示上游错误。",
    "文档中的 maximum context length 指上下文上限。",
    "日志术语 timed out 的中文是超时。",
])
def test_completed_native_answer_is_not_used_as_failure_text(explicit_run_one, kind, answer):
    result, attempts = execute_result(explicit_run_one, {
        "status": "ok", "final": answer, "error": "", "native_facts": completed_facts(kind),
    })
    assert result["status"] == "done", result
    assert result["final"] == answer
    assert result["error"] == ""
    assert len(attempts) == 1
    assert result["native_facts"] == completed_facts(kind)
    assert not explicit_run_one.selections


@pytest.mark.parametrize("raw", [
    {"error": "HTTP 429 provider request failed"},
    {"error": "maximum context length exceeded"},
    {"error": "E_FINALIZE_17"},
    {"status": "rc1", "native_facts": {"process_returncode": 1, "turn_status": "failed"}},
    {"status": "cancelled"},
    {"error_envelope": {
        "category": "harness_runtime", "scope": "task", "retryable": True,
        "code": "E_FINALIZE_17", "message": "explicit finalization failure",
    }},
    {"error_envelope": {
        "category": "context_overflow", "scope": "request", "retryable": False,
        "message": "provider reported a context limit",
    }},
    {"error_envelope": {
        "category": "authorization_required", "scope": "request", "retryable": False,
        "message": "authorization required",
    }},
])
def test_real_failure_channels_keep_precedence(explicit_run_one, raw):
    result, _ = execute_result(explicit_run_one, {
        "status": "ok", "final": "OK — model-connection-check.txt", "error": "",
        "native_facts": completed_facts(), **raw,
    })
    assert result["status"] != "done", result


@pytest.mark.parametrize("facts", [
    {},
    {"turn_status": "completed"},
    {"turn_status": "completed", "process_returncode": None},
    {"turn_status": "completed", "process_returncode": False},
    {"turn_status": "completed", "process_returncode": 0},
    {"turn_status": "failed", "process_returncode": 0, "completed": True},
])
def test_unknown_native_outcome_retains_legacy_error_text_fallback(explicit_run_one, facts):
    result, _ = execute_result(explicit_run_one, {
        # Truly unknown receipts retain the legacy fallback. An adapter's
        # explicit success is a content-source fact, covered separately by
        # test_adapter_final_channel; it is not an unknown receipt.
        "status": "unknown", "final": "API call failed: HTTP 429", "error": "",
        "native_facts": facts,
    })
    assert result["status"] != "done", result


def test_historical_tool_failure_does_not_override_a_completed_turn(explicit_run_one):
    facts = completed_facts()
    facts["tool_results"] = [
        {"type": "command_execution", "status": "failed", "exit_code": 1},
        {"type": "command_execution", "status": "completed", "exit_code": 0},
    ]
    facts["errors"] = [{"type": "error", "message": "HTTP 429 before native recovery"}]
    result, attempts = execute_result(explicit_run_one, {
        "status": "ok", "final": "OK — model-connection-check.txt", "error": "",
        "native_facts": facts,
    })
    assert result["status"] == "done", result
    assert result["native_facts"] == facts
    assert len(attempts) == 1


def test_completed_turn_does_not_override_empty_final(explicit_run_one):
    result, _ = execute_result(explicit_run_one, {
        "status": "ok", "final": "", "error": "", "native_facts": completed_facts(),
    })
    assert result["status"] != "done", result


@pytest.mark.parametrize("status", ["success", "completed"])
def test_native_budget_exit_cannot_gain_the_content_exception(explicit_run_one, status):
    result, _ = execute_result(explicit_run_one, {
        "status": status, "final": "OK — model-connection-check.txt", "error": "",
        "native_facts": {
            "process_returncode": 0, "completed": True,
            "turn_exit_reason": "max_iterations_reached(12/12)",
        },
    })
    assert result["status"] != "done", result


def test_explicit_missing_terminal_message_is_partial_not_model_failure(explicit_run_one):
    case = explicit_run_one
    health = []
    case.monkeypatch.setattr(
        case.core, "record_model_health", lambda *a, **kw: health.append(kw))
    facts = absent_message_facts()
    result, attempts = execute_result(case, {
        "status": "ok", "final": "", "error": "", "native_facts": facts,
        "native_checkpoint": {"kind": "codex_thread", "session_id": "original-native"},
    })
    assert result["status"] == "partial", result
    assert result["error"] == "" and not result.get("error_envelope")
    assert result["final"] == ""
    assert result["native_facts"] == facts
    assert result["native_checkpoint"]["session_id"] == "original-native"
    assert result["workdir"] == str(case.root / "work")
    assert len(attempts) == 1 and not case.selections
    assert not any(row.get("ok") is False for row in health)
    state = case.core._pilot_run_state(result)
    assert state["delivery_return_ready"] is False
    assert state["scope_goal_met"] is False
    assert state["native_terminal_message_absent"]["thread_id"] == "original-native"
    assert state["native_terminal_message_absent"]["turn_id"] == "this-turn"
    assert state["native_terminal_message_absent"]["checkpoint_saved"] is True
    assert "native_continuation" not in state, "a runtime observation is not author intent"


@pytest.mark.parametrize("problem", [
    "unknown", "missing_source", "missing_turn", "missing_thread", "nonempty_message",
    "missing_message_field", "wrong_checkpoint",
])
def test_unbound_absence_cannot_bypass_empty_answer_handling(explicit_run_one, problem):
    facts = absent_message_facts()
    fact = facts["completion_message"]
    checkpoint = {"kind": "codex_thread", "session_id": "original-native"}
    if problem == "unknown":
        fact["state"] = "unknown"
    elif problem.startswith("missing_"):
        fact.pop({"missing_source": "source", "missing_turn": "turn_id",
                  "missing_thread": "thread_id",
                  "missing_message_field": "last_agent_message"}[problem])
    elif problem == "nonempty_message":
        fact["last_agent_message"] = "真实答复"
    else:
        checkpoint["session_id"] = "another-native"
    result, _ = execute_result(explicit_run_one, {
        "status": "ok", "final": "", "error": "", "native_facts": facts,
        "native_checkpoint": checkpoint,
    })
    assert result["status"] != "partial", result
    assert not explicit_run_one.core._pilot_run_state(result).get("native_terminal_message_absent")


@pytest.mark.parametrize("override", [
    {"status": "timeout"},
    {"status": "cancelled"},
    {"error": "provider request failed"},
    {"error_envelope": {"category": "authorization_required", "retryable": False,
                        "scope": "request", "message": "approval required"}},
    {"native_facts": {**absent_message_facts(), "process_returncode": False}},
    {"native_facts": {**absent_message_facts(), "interrupted": True}},
    {"native_facts": {**absent_message_facts(), "turn_status": "failed"}},
    {"native_facts": {**absent_message_facts(),
                      "turn_exit_reason": "max_iterations_reached(12/12)"}},
])
def test_missing_message_does_not_override_real_stop(explicit_run_one, override):
    result, _ = execute_result(explicit_run_one, {
        "status": "ok", "final": "", "error": "", "native_facts": absent_message_facts(),
        "native_checkpoint": {"kind": "codex_thread", "session_id": "original-native"},
        **override,
    })
    assert not explicit_run_one.core._pilot_run_state(result).get("native_terminal_message_absent")


def test_missing_message_without_saved_checkpoint_is_not_resumable(explicit_run_one):
    import task_memory
    real_update = task_memory.update

    def fail_checkpoint(*args, **kwargs):
        if kwargs.get("native_checkpoint"):
            raise OSError("fixture checkpoint store unavailable")
        return real_update(*args, **kwargs)

    explicit_run_one.monkeypatch.setattr(task_memory, "update", fail_checkpoint)
    result, _ = execute_result(explicit_run_one, {
        "status": "ok", "final": "", "error": "", "native_facts": absent_message_facts(),
        "native_checkpoint": {"kind": "codex_thread", "session_id": "original-native"},
    })
    assert result["status"] == "partial", result
    assert result["checkpoint_storage_error"] == "fixture checkpoint store unavailable"
    fact = explicit_run_one.core._pilot_run_state(result)["native_terminal_message_absent"]
    assert fact["checkpoint_saved"] is False
