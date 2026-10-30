"""Count the current native call, not its whole resumed thread."""
import importlib
import json
from pathlib import Path

import pytest
from test_codex_output_budget_transport import runtime, declare, context, MODEL, KEY


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    for key in ("HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        monkeypatch.setenv(key, str(tmp_path / key))
    telemetry = importlib.import_module("token_telemetry")
    home = tmp_path / "CODEX_HOME"
    path = home / "sessions/2026/10/21/rollout-fixture-thread.jsonl"
    path.parent.mkdir(parents=True)
    return telemetry, home, path


def append(path, *records):
    with path.open("a") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def event(kind, turn, **fields):
    return {"type": "event_msg", "payload": {"type": kind, "turn_id": turn, **fields}}


def usage(turn, response, inp, out):
    return {"type": "token_usage_record", "payload": {
        "thread_id": "fixture-thread", "turn_id": turn, "response_id": response,
        "usage": {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out},
        "turn_token_usage": {"input_tokens": inp, "output_tokens": out,
                             "total_tokens": inp + out},
    }}


def test_resumed_call_counts_only_new_turn_and_deduplicates(case):
    telemetry, home, path = case
    append(path, event("task_started", "old"), usage("old", "r-old", 900, 100))
    cursor = telemetry.codex_usage_cursor(str(home), "fixture-thread")
    append(path, usage("old", "late-old", 100, 10),
           event("task_started", "current"),
           {"type": "response_item", "payload": {"type": "reasoning", "text": "PRIVATE\u2028CONTENT"}},
           usage("current", "r-current", 20, 5), usage("current", "r-current", 20, 5),
           event("task_complete", "current"))
    result = telemetry.codex_usage_after(
        cursor, "fixture-thread", {"input_tokens": 1020, "output_tokens": 115},
        resumed=True, terminal="completed", model="fixture")
    assert result["total_tokens"] == 25
    assert result["complete"] is True
    assert result["usage_scope"] == "native_call"
    assert result["response_records"] == 1
    assert "PRIVATE" not in json.dumps(result)
    assert telemetry.aggregate([result, result])["total_tokens"] == 25


@pytest.mark.parametrize("problem", ["missing", "replaced", "no_new_start", "partial"])
def test_uncertain_resumed_scope_is_not_a_full_bill(case, problem):
    telemetry, home, path = case
    append(path, event("task_started", "old"), usage("old", "old", 100, 10))
    cursor = telemetry.codex_usage_cursor(str(home), "fixture-thread")
    if problem == "missing":
        path.unlink()
    elif problem == "replaced":
        path.unlink()
        append(path, event("task_started", "new"), usage("new", "new", 50, 5))
    elif problem == "no_new_start":
        append(path, usage("old", "late", 60, 6))
    else:
        append(path, event("task_started", "new"), usage("new", "new", 50, 5))
    result = telemetry.codex_usage_after(
        cursor, "fixture-thread", {"input_tokens": 999, "output_tokens": 99},
        resumed=True, terminal="failed", model="fixture")
    normalized = telemetry.normalize(result)
    assert normalized["complete"] is False
    if problem != "partial":
        assert normalized["available"] is False
        assert normalized["total_tokens"] is None
    else:
        assert normalized["total_tokens"] == 55
        assert normalized["coverage"] == "partial"


def test_new_thread_legacy_counter_is_still_usable(case):
    telemetry, home, path = case
    result = telemetry.codex_usage_after(
        telemetry.codex_usage_cursor(str(home), ""),
        "fixture-thread", {"input_tokens": 5, "output_tokens": 2},
        resumed=False, terminal="completed", model="fixture")
    assert telemetry.normalize(result)["total_tokens"] == 7
    assert result["usage_scope"] == "new_native_thread"


def test_cumulative_counter_label_never_becomes_task_delta(case):
    telemetry, _, _ = case
    result = telemetry.normalize({
        "input_tokens": 999, "output_tokens": 99, "granularity": "thread_cumulative"})
    assert result["available"] is False
    assert result["total_tokens"] is None


@pytest.mark.parametrize("omit_second_total", [False, True])
def test_multiple_turn_starts_cannot_be_guessed_as_one_invocation(case, omit_second_total):
    telemetry, home, path = case
    append(path, event("task_started", "prior"))
    cursor = telemetry.codex_usage_cursor(str(home), "fixture-thread")
    second = usage("current", "second", 20, 5)
    if omit_second_total:
        second["payload"].pop("turn_token_usage")
    append(path, event("task_started", "late-old"), usage("late-old", "first", 900, 100),
           event("task_complete", "late-old"), event("task_started", "current"),
           second, event("task_complete", "current"))
    result = telemetry.codex_usage_after(
        cursor, "fixture-thread", {"input_tokens": 920, "output_tokens": 105},
        resumed=True, terminal="completed", model="fixture")
    assert result["complete"] is False
    assert telemetry.normalize(result)["total_tokens"] is None


def test_real_adapter_uses_the_current_invocation_trace(runtime):
    from types import SimpleNamespace
    case = runtime
    declared = declare(case, cap=None)
    path = case.root / "codex-home/sessions/2026/10/21/rollout-fixture-thread.jsonl"
    path.parent.mkdir(parents=True)
    append(path, event("task_started", "previous"), usage("previous", "r-old", 900, 100))

    def native(command, *args, **kwargs):
        append(path, event("task_started", "current"), usage("current", "r-new", 20, 5),
               event("task_complete", "current"))
        Path(command[command.index("-o") + 1]).write_text("真实短结果")
        return SimpleNamespace(returncode=0, stderr="", stdout="\n".join(map(json.dumps, [
            {"type": "thread.started", "thread_id": "fixture-thread"},
            {"type": "turn.completed", "usage": {"input_tokens": 920, "output_tokens": 105}},
        ])))

    case.mp.setattr(case.hr, "_run_task", native)
    checkpoint = {
        "kind": "codex_thread", "session_id": "fixture-thread",
        "provider_binding": case.mr.ccr_binding_key(MODEL, KEY, declared["base_url"]),
    }
    result = case.hr._codex_adapter(
        "__ut_usage_adapter", "fixture", ctx=context(case, task_memory={"native_checkpoint": checkpoint}))
    assert result["native_checkpoint"]["session_id"] == "fixture-thread"
    assert result["resumed_native"]
    assert result["final"] == "真实短结果"
    assert result["usage"]["total_tokens"] == 25
    assert result["native_facts"]["turn_result"]["usage"]["input_tokens"] == 920


@pytest.mark.parametrize("answer", [None, "", "好", "2", "HTTP 429 是限流。", "第一行\u2028第二行"])
def test_public_completion_is_bound_to_the_invocation_not_last_message(case, answer):
    telemetry, home, path = case
    append(path, event("task_started", "old"),
           event("task_complete", "old", last_agent_message="旧结果"))
    cursor = telemetry.codex_usage_cursor(str(home), "fixture-thread")
    append(path, event("task_complete", "old", last_agent_message="迟到旧结果"),
           event("task_started", "current", root_turn_id="current"),
           {"type": "response_item", "payload": {
               "type": "reasoning", "text": "PRIVATE_DO_NOT_RETURN"}},
           event("agent_message", "current", message="检查还在进行"),
           {"type": "response_item", "payload": {
               "type": "function_call_output", "output": "PRIVATE_TOOL_BODY"}},
           event("task_complete", "current", root_turn_id="current",
                 last_agent_message=answer))
    completion = {}
    telemetry.codex_usage_after(
        cursor, "fixture-thread", None, resumed=True, terminal="completed",
        model="fixture", completion=completion)
    assert completion["state"] == ("absent" if answer in (None, "") else "present")
    assert completion["thread_id"] == "fixture-thread"
    assert completion["turn_id"] == "current"
    assert completion["last_agent_message"] == answer
    assert "PRIVATE" not in json.dumps(completion)


@pytest.mark.parametrize("problem", [
    "missing_field", "missing_start", "wrong_turn", "wrong_thread",
    "wrong_root", "concurrent", "malformed", "rotated", "duplicate_file",
])
def test_uncertain_completion_stays_unknown(case, problem):
    telemetry, home, path = case
    append(path, event("task_started", "old"))
    cursor = telemetry.codex_usage_cursor(str(home), "fixture-thread")
    if problem == "rotated":
        path.rename(path.with_suffix(".old"))
    if problem != "missing_start":
        append(path, event("task_started", "current"))
    fields = {} if problem == "missing_field" else {"last_agent_message": None}
    if problem == "wrong_thread":
        fields["thread_id"] = "some-other-thread"
    if problem == "wrong_root":
        fields["root_turn_id"] = "some-other-root"
    append(path, event("task_complete", "other" if problem == "wrong_turn" else "current",
                       **fields))
    if problem == "concurrent":
        append(path, event("task_started", "other"),
               event("task_complete", "other", last_agent_message="别的调用"))
    elif problem == "malformed":
        with path.open("ab") as stream:
            stream.write(b'{"type":"event_msg",')
    elif problem == "duplicate_file":
        path.with_name("second-fixture-thread.jsonl").write_bytes(path.read_bytes())
    completion = {}
    telemetry.codex_usage_after(
        cursor, "fixture-thread", None, resumed=True, terminal="completed",
        model="fixture", completion=completion)
    assert completion["state"] == "unknown"
    assert "last_agent_message" not in completion


def test_invalid_usage_does_not_discard_a_bound_public_completion(case):
    telemetry, home, path = case
    append(path, event("task_started", "old"))
    cursor = telemetry.codex_usage_cursor(str(home), "fixture-thread")
    invalid = usage("current", "invalid", 4, 1)
    invalid["payload"]["usage"]["input_tokens"] = "invalid"
    append(path, event("task_started", "current"),
           usage("current", "valid", 20, 5), invalid,
           event("task_complete", "current", root_turn_id="current",
                 last_agent_message=None))
    completion = {}
    result = telemetry.codex_usage_after(
        cursor, "fixture-thread", None, resumed=True, terminal="completed",
        model="fixture", completion=completion)
    assert completion == {
        "state": "absent", "thread_id": "fixture-thread", "turn_id": "current",
        "last_agent_message": None, "source": "codex_task_complete_event",
    }
    assert result["complete"] is False
    assert result["coverage"] == "partial"
    assert result["input_tokens"] == 20


@pytest.mark.parametrize("answer", [None, "好", "2", "HTTP 429 是限流。"])
@pytest.mark.parametrize("timeout", [False, True])
def test_real_adapter_reconciles_last_message_with_public_completion(runtime, answer, timeout):
    from types import SimpleNamespace
    import subprocess

    case = runtime
    declared = declare(case, cap=None)
    path = case.root / "codex-home/sessions/2026/10/21/rollout-fixture-thread.jsonl"
    path.parent.mkdir(parents=True)
    append(path, event("task_started", "previous"),
           event("task_complete", "previous", last_agent_message="旧最终答案"))
    progress = "桌面检查完成，现在复验移动端。"

    def native(command, *args, **kwargs):
        append(path, event("task_started", "current"),
               event("agent_message", "current", message=progress),
               event("task_complete", "current", root_turn_id="current",
                     last_agent_message=answer))
        Path(command[command.index("-o") + 1]).write_text(progress)
        stdout = "\n".join(map(json.dumps, [
            {"type": "thread.started", "thread_id": "fixture-thread"},
            {"type": "turn.completed", "usage": {}},
        ]))
        if timeout:
            error = subprocess.TimeoutExpired(command, 1)
            error.legion_stdout, error.legion_stderr = stdout, ""
            raise error
        return SimpleNamespace(returncode=0, stderr="", stdout=stdout)

    case.mp.setattr(case.hr, "_run_task", native)
    checkpoint = {
        "kind": "codex_thread", "session_id": "fixture-thread",
        "provider_binding": case.mr.ccr_binding_key(MODEL, KEY, declared["base_url"]),
    }
    result = case.hr._codex_adapter(
        "__ut_completion_adapter", "fixture",
        ctx=context(case, task_memory={"native_checkpoint": checkpoint}))
    assert result["status"] == ("timeout" if timeout else "ok")
    assert result["final"] == (answer or "")
    assert result["native_checkpoint"]["session_id"] == "fixture-thread"
    facts = result["native_facts"]
    assert facts["turn_status"] == "completed"
    assert facts["process_returncode"] == (None if timeout else 0)
    assert facts["completion_message"]["state"] == ("absent" if answer is None else "present")
    assert facts["last_message_output"]["text"] == progress
    assert not result.get("error_envelope") or timeout
