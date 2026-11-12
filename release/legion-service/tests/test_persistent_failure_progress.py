"""Real persistent controller; only execution is a fixture, never a model call."""
from copy import deepcopy

import pytest

from test_persistent_native_continuation import loop


def failed(*, error="native stream could not be decoded", final="", **fields):
    return {
        "key": "fixture-maker", "task_id": "maker", "assigned_harness": "fixture-maker",
        "status": "error", "final": final, "error": error, "fail_kind": "process",
        "error_envelope": {"category": "process", "scope": "harness", "retryable": True,
                           "code": "rc1", "message": error},
        "native_facts": {"turn_status": "failed", "process_returncode": 1, "tool_results": []},
        "native_checkpoint": {"kind": "codex_thread", "session_id": "original-native",
                              "provider_binding": "unchanged-provider"},
        "workdir": "/fixture/original-work", "attempts": 1, **fields,
    }


def artifact(digest="a", snapshot="one"):
    return {"name": "game.html", "sha256": digest * 64,
            "path": f"/fixture/exports/{snapshot}/game.html",
            "url": f"/home-outputs/exports/{snapshot}/game.html",
            "export_snapshot_id": snapshot}


def tool(output, identity="call-one"):
    return {"type": "command_execution", "id": identity, "command": "check game",
            "status": "completed", "exit_code": 0, "aggregated_output": output}


def execute(loop, rows, *, max_rounds=6, team="__ut_persistent_failure_progress"):
    loop.results[:] = deepcopy(rows)
    return loop.core.dispatch_persistent(
        "original user objective", deepcopy(loop.plan), qhash=team, mode="pk",
        user_id="fixture", conversation_id="original-conversation", max_rounds=max_rounds)


@pytest.mark.parametrize("error", [
    "stream disconnected before completion: UTF8 error: invalid utf-8 sequence",
    "independent transport failed to deliver its response",
])
def test_same_empty_failure_stops_and_preserves_original_recoverable_result(loop, error):
    row = failed(error=error, artifacts=[artifact()])
    out = execute(loop, [row])
    assert len(loop.calls) == 2, "empty final must not renew the same failed execution six times"
    assert out["persistent_stalled"] is True
    assert out["pilot"]["action"] == "stop"
    assert out["pilot"]["kind"] == "persistent_stalled"
    assert not out.get("persistent_capped")
    assert out["results"] == [row], "keep failure, old artifact, workspace and provider-bound cursor"
    saved = loop.team_state.load("__ut_persistent_failure_progress")["tasks"]["maker"]
    assert saved["attempt_seq"] == 2 and saved["active_attempt"] is None
    for field in ("status", "final", "error", "error_envelope", "artifacts", "native_checkpoint", "native_facts"):
        assert saved["result"][field] == row[field]


def test_same_content_in_new_export_snapshots_is_not_progress(loop):
    rows = [failed(artifacts=[artifact(snapshot=str(i))]) for i in range(6)]
    out = execute(loop, rows)
    assert len(loop.calls) == 2
    assert out["persistent_stalled"] is True
    assert out["results"] == [rows[1]]


@pytest.mark.parametrize("kind", ["native_tool", "tool_trace", "artifact"])
def test_real_new_result_keeps_identical_final_and_failure_running(loop, kind):
    first = failed(final="unchanged draft", artifacts=[artifact()])
    second = deepcopy(first)
    if kind == "native_tool":
        first["native_facts"]["tool_results"] = [tool("first observation")]
        second["native_facts"]["tool_results"] = [tool("new observation", "call-two")]
    elif kind == "tool_trace":
        first["tool_trace"] = [{"t": "tool_end", "tool": "check", "ok": True,
                                "call_id": "one", "brief": "first observation"}]
        second["tool_trace"] = [{"t": "tool_end", "tool": "check", "ok": True,
                                 "call_id": "two", "brief": "new observation"}]
    else:
        second["artifacts"] = [artifact("b", "two")]
    completed = {"key": "fixture-maker", "task_id": "maker",
                 "assigned_harness": "fixture-maker", "status": "done", "final": "Ready."}
    out = execute(loop, [first, second, completed])
    assert len(loop.calls) == 3, "real result changes must outrank identical final/error text"
    assert not out.get("persistent_stalled")
    assert out["results"] == [completed]


@pytest.mark.parametrize("noise", ["tool_start", "harness_error", "same_tool_new_id"])
def test_activity_or_replayed_tool_result_is_not_new_progress(loop, noise):
    first = failed()
    second = deepcopy(first)
    if noise == "same_tool_new_id":
        first["native_facts"]["tool_results"] = [tool("same result")]
        second["native_facts"]["tool_results"] = [{**tool("same result", "different-id"), "duration_ms": 42}]
    else:
        second["tool_trace"] = [
            {"t": "tool_started", "tool": "check", "call_id": "new"}
            if noise == "tool_start" else {"t": "harness_step", "brief": "本轮失败: same failure"}]
    out = execute(loop, [first, second])
    assert len(loop.calls) == 2
    assert out["persistent_stalled"] is True


@pytest.mark.parametrize("final", ["", "unchanged draft"])
def test_changed_full_error_gets_its_own_attempt_before_repetition_stops(loop, final):
    # A display truncation must not erase the distinguishing suffix.
    prefix = "same diagnostic prefix " * 40
    first = failed(error=prefix + "cause A", final=final)
    second = failed(error=prefix + "cause B", final=final)
    out = execute(loop, [first, second, second])
    assert len(loop.calls) == 3
    assert out["persistent_stalled"] is True
    assert out["results"] == [second]


def test_returning_to_an_already_seen_artifact_does_not_count_as_new(loop):
    rows = [failed(artifacts=[artifact("a")]), failed(artifacts=[artifact("b")]),
            failed(artifacts=[artifact("a", "new-snapshot")])]
    out = execute(loop, rows)
    assert len(loop.calls) == 3
    assert out["persistent_stalled"] is True


@pytest.mark.parametrize("boundary", ["cancelled", "budget", "backoff"])
def test_existing_stop_and_budget_take_priority_over_repeat_detection(loop, boundary):
    first = failed(artifacts=[artifact()])
    second = deepcopy(first)
    if boundary == "cancelled":
        second.update(status="cancelled", cancelled=True)
        expected = "user_cancelled"
    else:
        second["error_envelope"] = {
            "category": "runtime_budget" if boundary == "budget" else "rate_limit",
            "scope": "request" if boundary == "budget" else "provider",
            "retryable": boundary != "budget",
            "code": "request_total_budget_exhausted" if boundary == "budget" else "HTTP 429",
            "message": "existing runtime boundary",
        }
        expected = "request_budget_exhausted" if boundary == "budget" else "provider_backoff"
    out = execute(loop, [first, second])
    assert len(loop.calls) == 2
    assert out["pilot"]["action"] == "stop" and out["pilot"]["kind"] == expected
    assert not out.get("persistent_stalled")
    assert out["results"] == [second]


def test_original_round_budget_is_not_extended(loop):
    row = failed(artifacts=[artifact()])
    out = execute(loop, [row], max_rounds=1)
    assert len(loop.calls) == 1
    assert out["persistent_capped"] is True
    assert not out.get("persistent_stalled")
    assert out["results"] == [row]


def test_empty_unknown_result_is_not_invented_into_a_repeated_failure(loop):
    row = {"key": "fixture-maker", "task_id": "maker", "assigned_harness": "fixture-maker",
           "status": "error", "final": ""}
    out = execute(loop, [row], max_rounds=3)
    assert len(loop.calls) == 3
    assert out["persistent_capped"] is True
    assert not out.get("persistent_stalled")
