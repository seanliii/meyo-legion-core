"""Provider output truncation is not a transport crash or successful delivery."""
import copy
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one

MESSAGE = "Incomplete response returned, reason: max_output_tokens"


def native_failure():
    return {"type": "turn.failed", "error": {"message": MESSAGE}}


def test_exact_native_limit_becomes_structured_request_fact(isolated):
    errors = importlib.import_module("error_envelope")
    value = errors.output_limit_from_native(native_failure())
    assert value.category == "output_limit" and value.code == "max_output_tokens"
    assert value.scope == "request" and value.retryable
    assert value.details["reported_output_limit"] is None
    assert value.details["input_context_overflow"] is False
    assert "partial_tool_call_executable" not in value.details
    assert value.details["tool_execution_status"] == "inspect_native_results"
    assert value.details["replay_unconfirmed_operations"] is False


@pytest.mark.parametrize("record", [
    {"type": "turn.completed", "error": {"message": MESSAGE}},
    {"type": "item.completed", "item": {"type": "command_execution", "output": MESSAGE}},
    {"type": "turn.failed", "error": {"message": "Docs explain " + MESSAGE}},
    {"type": "turn.failed", "error": {"message": "HTTP 429 rate limit"}},
    {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "content_filter"}}},
])
def test_tool_quotes_and_other_failures_not_reclassified(isolated, record):
    errors = importlib.import_module("error_envelope")
    assert errors.output_limit_from_native(record) is None


def test_structured_responses_limit_preserves_reported_budget_and_usage(isolated):
    errors = importlib.import_module("error_envelope")
    record = {"type": "response.incomplete", "response": {
        "incomplete_details": {"reason": "max_output_tokens"},
        "max_output_tokens": 512,
        "usage": {"input_tokens": 100, "output_tokens": 512, "total_tokens": 612},
    }}
    result = errors.output_limit_from_native(record)
    assert result.details["reported_output_limit"] == 512
    assert result.details["reported_usage"]["output_tokens"] == 512
    assert errors.normalize("rc1", supplied=result).to_dict() == result.to_dict()


def limited_result(case):
    binding = case.mr.ccr_binding_key("fixture-model", "private-fixture-key",
                                    "https://models.example.invalid/v1")
    errors = importlib.import_module("error_envelope")
    return {"status": "rc1", "error": MESSAGE, "final": "", "model_used": "fixture-model",
            "native_checkpoint": {"kind": "codex_thread", "session_id": "same-native-thread",
                                  "provider_binding": binding},
            "native_facts": {"process_returncode": 1, "turn_status": "failed",
                             "turn_result": native_failure(), "tool_results": []},
            "error_envelope": errors.output_limit_from_native(native_failure()).to_dict(),
            "usage": {"available": False, "source": "not_returned"}}


@pytest.mark.parametrize("second_success", [True, False])
def test_limit_recovers_same_thread_once_then_stops_unchanged_replay(explicit_run_one, second_success):
    case = explicit_run_one
    work = case.root / "work"
    work.mkdir()
    original = work / "research.md"
    original.write_text("existing research must survive")
    calls = []
    events = []

    def execute(*args, **kwargs):
        calls.append((args, copy.deepcopy(kwargs)))
        if len(calls) > 1:
            assert kwargs["task_memory"]["native_checkpoint"]["session_id"] == "same-native-thread"
            prior = kwargs["prior_attempts"][-1]
            assert prior["fail_kind"] == "output_limit"
            assert "以原生工具结果" in prior["error_envelope"]["details"]["recovery_context"]
            assert "没有被执行" not in prior["error_envelope"]["details"]["recovery_context"]
            assert kwargs["provider_binding"]["api_key"] == "private-fixture-key"
            if second_success:
                (work / "index.html").write_text("<p>restored fixture file</p>")
                return {"status": "ok", "final": str(work / "index.html"), "error": "",
                        "native_checkpoint": limited_result(case)["native_checkpoint"],
                        "workdir": str(work), "resumed_native": True,
                        "native_facts": {"process_returncode": 0, "turn_status": "completed",
                                         "turn_result": {"type": "turn.completed"}}}
        return {**limited_result(case), "workdir": str(work)}

    case.monkeypatch.setattr(case.core, "_run_core_inprocess", execute)
    result = case.core.run_one(
        "codex", "finish the original product", qhash="__ut_output_limit",
        user_id="owner-a", conversation_id="original-session", cb=events.append,
        task_facts={"project_context": {"repo_root": str(work), "user_id": "owner-a"}},
    )
    assert len(calls) == 2
    assert not case.selections
    assert original.read_text() == "existing research must survive"
    assert any(row.get("t") == "output_limit_recovery" for row in events)
    assert result["native_checkpoint"]["session_id"] == "same-native-thread"
    if second_success:
        assert result["status"] == "done"
        assert (work / "index.html").is_file()
    else:
        assert result["status"] != "done"
        envelope = result["error_envelope"]
        assert envelope["category"] == "output_limit" and envelope["retryable"] is False
        assert importlib.import_module("error_envelope").requires_execution_pause(result)
        assert not (work / "index.html").exists()
    assert "private-fixture-key" not in json.dumps(result)


def test_missing_checkpoint_does_not_restart_or_switch_model(explicit_run_one):
    case = explicit_run_one
    calls = []

    def execute(*args, **kwargs):
        calls.append(True)
        result = limited_result(case)
        result.pop("native_checkpoint")
        return result

    case.monkeypatch.setattr(case.core, "_run_core_inprocess", execute)
    result = case.core.run_one("codex", "original task", qhash="__ut_no_output_checkpoint")
    assert len(calls) == 1 and not case.selections
    assert result["error_envelope"]["retryable"] is False
    assert result["error_envelope"]["details"]["recovery_status"] == "checkpoint_unavailable"


def test_actual_adapter_passes_recovery_facts_into_native_input(explicit_run_one):
    case = explicit_run_one
    inputs = []

    def native(command, *args, **kwargs):
        inputs.append(kwargs["input"])
        output = Path(command[command.index("-o") + 1])
        if len(inputs) == 1:
            return SimpleNamespace(returncode=1, stderr="", stdout="\n".join([
                json.dumps({"type": "thread.started", "thread_id": "same-native-thread"}),
                json.dumps(native_failure()),
            ]))
        assert "resume" in command and "same-native-thread" in command
        assert "recovery_context" in kwargs["input"] and "以原生工具结果" in kwargs["input"]
        assert "未完整的工具调用没有被执行" not in kwargs["input"]
        output.write_text("fixture continuation completed")
        return SimpleNamespace(returncode=0, stderr="", stdout="\n".join([
            json.dumps({"type": "thread.started", "thread_id": "same-native-thread"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]))

    case.monkeypatch.setattr(case.hr, "_run_task", native)
    result = case.core.run_one("codex", "original task", qhash="__ut_limit_native_prompt",
                               user_id="owner-a", conversation_id="original-session")
    assert len(inputs) == 2 and result["status"] == "done"
    assert result["resumed_native"]


def test_completed_tool_before_incomplete_response_is_not_relabelled_unexecuted(explicit_run_one):
    case = explicit_run_one
    calls = []
    recorded_tool = {
        "type": "command_execution", "id": "already-finished-tool",
        "status": "completed", "exit_code": 0, "aggregated_output": "read-only observed result",
    }

    def execute(*args, **kwargs):
        calls.append(kwargs)
        result = limited_result(case)
        result["native_facts"]["tool_results"] = [recorded_tool]
        return result

    case.monkeypatch.setattr(case.core, "_run_core_inprocess", execute)
    result = case.core.run_one("codex", "do not repeat observed action", qhash="__ut_completed_before_limit")
    assert len(calls) == 2
    assert result["native_facts"]["tool_results"] == [recorded_tool]
    for call in calls[1:]:
        facts = call["prior_attempts"][-1]
        assert facts["native_facts"]["tool_results"] == [recorded_tool]
        note = facts["error_envelope"]["details"]["recovery_context"]
        assert "以原生工具结果" in note and "没有被执行" not in note


def test_recovered_result_usage_stays_partial_when_first_attempt_did_not_report(explicit_run_one):
    case = explicit_run_one
    calls = []

    def execute(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return limited_result(case)
        return {
            "status": "ok", "final": "fixture finished", "error": "", "model_used": "fixture-model",
            "native_checkpoint": limited_result(case)["native_checkpoint"], "resumed_native": True,
            "native_facts": {"process_returncode": 0, "turn_status": "completed",
                             "turn_result": {"type": "turn.completed"}},
            "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        }

    case.monkeypatch.setattr(case.core, "_run_core_inprocess", execute)
    result = case.core.run_one("codex", "original", qhash="__ut_limit_partial_usage")
    assert result["status"] == "done"
    usage = result["usage"]
    assert usage["available"] is True and usage["total_tokens"] == 10
    assert usage["complete"] is False and usage["coverage"] == "partial"
    assert usage["attempts_total"] == 2 and usage["attempts_unreported"] == 1
    assert "已知部分" in importlib.import_module("token_telemetry").summary_text(usage)
