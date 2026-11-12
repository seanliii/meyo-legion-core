"""Real HTTP Stop/message/resume ordering; only native/model leaves are fake."""
from copy import deepcopy
import threading

import pytest

from test_standard_coordination_resume import (
    case, seed_single_feedback, send_feedback, wait_for_feedback, run_startup_resume,
    TEAM, OWNER, OTHER, CONVERSATION, HARNESS,
)


@pytest.fixture(autouse=True)
def isolated_cancellation(case):
    case.mp.setattr(case.core, "_CANCEL_REGISTRY", {})


def stop(case, **fields):
    code, out = case.post(path="/api/legion/cancel", **fields)
    assert code == 200, out
    return out


def wake(case):
    case.server._schedule_feedback_resume(TEAM, OWNER, CONVERSATION)
    message = wait_for_feedback(case)
    run_startup_resume(case)
    return message


@pytest.mark.parametrize("legacy", [False, True])
def test_new_owner_action_after_stop_runs_once_without_reopening_parent(case, legacy):
    seed_single_feedback(case)
    state = case.teams.load(TEAM)
    state["tasks"]["A"].update(status="interrupted", native_session_scope="request",
                                 verification_criteria=["original acceptance remains"])
    state["orchestration_spec"]["team_policy"] = {"max_rounds": 2, "auto_resume": True}
    case.teams._write(state)
    stop(case)
    if legacy:
        state = case.teams.load(TEAM)
        state.pop("cancel_sequence")
        for row in state["cancel_requests"]:
            row.pop("sequence")
        case.teams._write(state)
    before = deepcopy(case.teams.load(TEAM))
    assert before["tasks"]["A"]["status"] == "cancelled"
    assert case.post()[1]["status"] == "unchanged", "resume alone is not new authority"
    run_startup_resume(case)
    assert not case.member_calls
    assert send_feedback(case)[0] == 201
    message = wait_for_feedback(case)
    child = message["continuation"]["team_id"]
    assert message["continuation"]["status"] == "completed", message
    assert len(case.member_calls) == 1 and not case.controls
    assert case.member_calls[0]["conversation_id"] == CONVERSATION + ":task:A"
    node = case.teams.load(child)["tasks"]["A"]
    for field in ("native_session_scope", "verification_criteria"):
        assert node[field] == before["tasks"]["A"][field]
    assert case.teams.load(child)["orchestration_spec"]["team_policy"]["max_rounds"] == 2
    assert case.core.is_cancelled(TEAM) and not case.core.is_cancelled(child)
    for field in ("tasks", "cancel_requests", "orchestration_spec"):
        assert case.teams.load(TEAM)[field] == before[field]
    assert send_feedback(case)[1]["id"] == message["id"]
    run_startup_resume(case, team_ids=(TEAM, child))
    assert len(case.member_calls) == 1


def test_old_held_request_is_never_migrated_released_or_replayed(case):
    seed_single_feedback(case)
    stop(case)
    with case.mp.context() as quiet:
        quiet.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
        assert send_feedback(case)[0] == 201
    state = case.teams.load(TEAM)
    old = state["mailboxes"]["commander"][-1]
    old["continuation"].pop("cancel_boundaries", None)  # deployed pre-fix receipt
    old["continuation"].update(status="held", reason="cancelled")
    case.teams._write(state)
    before = deepcopy(old)
    assert send_feedback(case)[1] == before
    assert case.post()[1]["status"] == "unchanged"
    run_startup_resume(case)
    assert not case.member_calls
    assert not case.teams.load(before["continuation"]["team_id"])
    assert send_feedback(case, request_id="__ut_fresh_owner_action")[0] == 201
    fresh = wait_for_feedback(case, "__ut_fresh_owner_action")
    assert fresh["continuation"]["status"] == "completed", fresh
    assert len(case.member_calls) == 1
    assert case.teams.load(TEAM)["mailboxes"]["commander"][0] == before
    run_startup_resume(case)
    assert len(case.member_calls) == 1


@pytest.mark.parametrize("phase", ["pending", "claimed", "admitted"])
def test_new_stop_before_native_start_prevents_execution(case, phase):
    seed_single_feedback(case)
    stop(case)  # superseded only by the new user action below
    if phase == "pending":
        with case.mp.context() as quiet:
            quiet.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
            assert send_feedback(case)[0] == 201
        stop(case)
        message = wake(case)
    else:
        if phase == "claimed":
            real = case.teams.record_continuation

            def record(*args, **kwargs):
                if kwargs.get("status") == "running":
                    stop(case)
                return real(*args, **kwargs)

            case.mp.setattr(case.teams, "record_continuation", record)
        else:
            real = case.core.dispatch_persistent

            def dispatch(*args, **kwargs):
                assert case.teams.load(kwargs["qhash"]) is None
                stop(case)
                assert case.core.is_cancelled(kwargs["qhash"])
                return real(*args, **kwargs)

            case.mp.setattr(case.core, "dispatch_persistent", dispatch)
        assert send_feedback(case)[0] == 201
        message = wait_for_feedback(case)
    assert not case.member_calls and not case.controls, message
    if phase != "admitted":
        assert message["continuation"]["status"] == "held", message
        assert not case.teams.load(message["continuation"]["team_id"])
    else:
        child = case.teams.load(message["continuation"]["team_id"])
        assert child["tasks"]["A"]["status"] == "cancelled"
    run_startup_resume(case)
    assert not case.member_calls


@pytest.mark.parametrize("scope,key", [("all", ""), ("task", "A"), ("harness", HARNESS)])
def test_stop_during_child_execution_reaches_only_its_actual_scope(case, scope, key):
    seed_single_feedback(case)
    stop(case)
    entered, release = threading.Event(), threading.Event()
    observed = []

    def running(_key, _question, **kwargs):
        observed.append(kwargs["task_facts"]["team_id"])
        entered.set()
        assert release.wait(3)
        assert kwargs["cancel_check"](), "the real native cancellation callback must see Stop"
        assert case.core.is_cancelled(observed[-1], key or None, scope=scope)
        return {"status": "cancelled", "final": "", "error": "test stop"}

    case.mp.setattr(case.core, "run_one", running)
    try:
        assert send_feedback(case)[0] == 201
        assert entered.wait(3)
        stop(case, key=key, scope=scope)
        child = case.teams.load(observed[0])
        assert child["cancel_requests"][-1]["scope"] == scope
        if key:
            assert not case.core.is_cancelled(observed[0])
            assert not case.core.is_cancelled(observed[0], "unrelated", scope=scope)
    finally:
        release.set()
        wait_for_feedback(case)
    run_startup_resume(case)
    assert len(observed) == 1


def test_late_stop_http_completion_cannot_cancel_a_newer_feedback(case):
    seed_single_feedback(case)
    persisted, finish_stop, entered, finish_native = [threading.Event() for _ in range(4)]
    real = case.core.request_cancel
    responses = []

    def delayed(team_id, *args, **kwargs):
        if team_id == TEAM:
            persisted.set()  # durable cancel has already committed
            assert finish_stop.wait(3)
        return real(team_id, *args, **kwargs)

    def running(_key, _question, **kwargs):
        entered.set()
        assert finish_native.wait(3)
        assert not kwargs["cancel_check"]()
        return {"status": "done", "final": "new owner action"}

    case.mp.setattr(case.core, "request_cancel", delayed)
    case.mp.setattr(case.core, "run_one", running)
    thread = threading.Thread(target=lambda: responses.append(stop(case)))
    thread.start()
    try:
        assert persisted.wait(3)
        assert send_feedback(case)[0] == 201
        assert entered.wait(3)
        finish_stop.set()
        thread.join(3)
        assert not thread.is_alive() and responses
        message = case.teams.pending_continuation(case.teams.load(TEAM))
        assert not case.core.is_cancelled(message["continuation"]["team_id"])
    finally:
        finish_stop.set()
        finish_native.set()
        thread.join(3)
        message = wait_for_feedback(case)
    assert message["continuation"]["status"] == "completed", message


@pytest.mark.parametrize("kind", ["runtime_budget", "native_iteration_budget", "native_unsettled", "authorization"])
@pytest.mark.parametrize("where", ["parent", "latest-child"])
def test_feedback_after_cancel_preserves_other_execution_holds(case, kind, where):
    seed_single_feedback(case)
    target = TEAM
    if where == "latest-child":
        assert send_feedback(case, request_id="__ut_previous")[0] == 201
        target = wait_for_feedback(case, "__ut_previous")["continuation"]["team_id"]
        run_startup_resume(case)
    stop(case, team_id=target)
    state = case.teams.load(target)
    result = {"status": "cancelled", "final": ""}
    if kind == "native_unsettled":
        result["native_facts"] = {"native_turn_terminal": "unobserved", "cli_cancelled": True}
    else:
        result["error_envelope"] = {
            "category": "authorization_required" if kind == "authorization" else kind,
            "scope": "provider" if kind == "authorization" else "request",
            "retryable": False, "code": "appid_disabled" if kind == "authorization" else kind,
        }
    state["tasks"]["A"]["result"] = result
    case.teams._write(state)
    before = len(case.member_calls)
    assert send_feedback(case)[0] == 201
    message = wait_for_feedback(case)
    assert message["continuation"]["status"] == "held", message
    assert len(case.member_calls) == before and not case.controls
    assert case.teams.load(target)["tasks"]["A"]["result"] == result


@pytest.mark.parametrize("after", [False, True])
def test_latest_child_stop_is_ordered_against_the_new_parent_feedback(case, after):
    seed_single_feedback(case)
    assert send_feedback(case, request_id="__ut_previous")[0] == 201
    previous = wait_for_feedback(case, "__ut_previous")["continuation"]["team_id"]
    run_startup_resume(case)
    if not after:
        stop(case, team_id=previous)
    with case.mp.context() as quiet:
        quiet.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
        assert send_feedback(case)[0] == 201
    if after:
        stop(case, team_id=previous)
    message = wake(case)
    assert message["continuation"]["status"] == ("held" if after else "completed"), message
    assert len(case.member_calls) == (1 if after else 2)


def test_cancelled_parent_still_requires_canonical_owner(case):
    seed_single_feedback(case)
    stop(case)
    before = case.teams._path(TEAM).read_bytes()
    assert send_feedback(case, user=OTHER, sender="user")[0] == 403
    assert case.post(user=OTHER, path="/api/legion/cancel")[0] == 403
    assert case.teams._path(TEAM).read_bytes() == before
    assert not case.member_calls


def test_stop_sequence_does_not_depend_on_wall_clock_or_truncated_history(case):
    seed_single_feedback(case)
    case.mp.setattr(case.teams.time, "time", lambda: 1000)
    stop(case)
    with case.mp.context() as quiet:
        quiet.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
        assert send_feedback(case)[0] == 201
    accepted = case.teams.pending_continuation(case.teams.load(TEAM))
    assert case.teams.continuation_after_cancel(case.teams.load(TEAM), accepted)
    case.mp.setattr(case.teams.time, "time", lambda: 999)
    for _ in range(51):
        case.teams.request_cancel(TEAM)
    state = case.teams.load(TEAM)
    assert len(state["cancel_requests"]) == 50 and state["cancel_sequence"] == 52
    assert not case.teams.continuation_after_cancel(state, accepted)
    assert not case.member_calls
