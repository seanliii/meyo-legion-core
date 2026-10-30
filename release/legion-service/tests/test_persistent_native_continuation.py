"""Same native continuation intent must reach both existing caller paths."""
import copy
import importlib
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def loop(tmp_path, monkeypatch):
    for name, leaf in (
        ("HOME", "home"), ("CODEX_HOME", "codex-home"),
        ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "home"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_TASK_MEMORY_DIR", "memory"),
        ("LEGION_TEAM_DIR", "teams"), ("LEGION_SESSIONS_DIR", "sessions"),
        ("LEGION_USER_CONFIG_DIR", "users"),
    ):
        monkeypatch.setenv(name, str(tmp_path / leaf))
    monkeypatch.syspath_prepend(str(SERVICE))
    def forbidden(*args, **kwargs):
        raise AssertionError("real network/model/native process is forbidden")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    core = importlib.import_module("legion_core")
    pilot = importlib.import_module("legion_pilot")
    team_state = importlib.import_module("team_state")
    monkeypatch.setattr(pilot, "read_shared_goals", lambda: {})
    monkeypatch.setattr(pilot, "load_user_config", lambda *_a, **_kw: pilot.merge_config())
    monkeypatch.setattr(core, "_autocommit_delivery", lambda *a, **kw: {"status": "skipped"})
    monkeypatch.setattr(core, "_harness_keys", lambda: ["fixture-maker"])
    calls = []
    results = [
        {"key": "fixture-maker", "task_id": "maker", "assigned_harness": "fixture-maker",
         "status": "done", "final": "The current artifact exists; its required interaction is not finished.",
         "native_checkpoint": {"session_id": "original-native"},
         "orchestration_request": {"coordinate": {
             "action": "continue", "reason": "Actual interaction is unfinished; continue the original artifact."}}},
        {"key": "fixture-maker", "task_id": "maker", "assigned_harness": "fixture-maker",
         "status": "done", "final": "The original interaction now works.",
         "native_checkpoint": {"session_id": "original-native"}},
    ]

    def dispatch(question, plan=None, **kwargs):
        calls.append({"question": question, "plan": copy.deepcopy(plan), **copy.deepcopy(kwargs)})
        row = copy.deepcopy(results[min(len(calls) - 1, len(results) - 1)])
        team = kwargs["qhash"]
        team_state.ensure(
            team, question, plan["tasks"], {"maker": []},
            user_id="fixture", conversation_id=kwargs.get("conversation_id", ""),
            registered={"fixture-maker"})
        attempt = team_state.begin_attempt(team, "maker", owner="fixture-maker")
        assert team_state.finish_attempt(team, "maker", attempt, row)
        delivered = int(core._is_delivered(row))
        return {"results": [row], "delivered_count": delivered, "total": 1, "all_failed": not delivered,
                "dependencies": {"maker": []}, "task_assignments": {"maker": "fixture-maker"},
                "winner": "maker"}

    monkeypatch.setattr(core, "dispatch", dispatch)
    return SimpleNamespace(core=core, pilot=pilot, team_state=team_state, mp=monkeypatch,
                           calls=calls, results=results,
                           plan={"tasks": {"maker": {"task": "original user objective",
                                                     "harness": "fixture-maker",
                                                     "native_session_scope": "original-scope"}}})


def test_missing_terminal_reopens_same_author_only_after_existing_continue_verdict(loop):
    from test_native_final_diagnostics import absent_message_result

    loop.results[0] = absent_message_result(
        key="fixture-maker", task_id="maker", assigned_harness="fixture-maker",
        model_used="original-model", native_session_scope="original-scope")
    loop.plan["tasks"]["maker"].update(
        model="original-model", input_hash="old-input", idempotency_key="old-attempt")
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_absent_continue",
        mode="pk", user_id="fixture", conversation_id="original-conversation", max_rounds=2)
    assert len(loop.calls) == 2, out
    second = loop.calls[1]
    node = second["plan"]["tasks"]["maker"]
    assert (node.get("assigned_harness") or node.get("harness")) == "fixture-maker"
    assert node.get("harness") == "fixture-maker"
    assert node["model"] == "original-model" and node["native_session_scope"] == "original-scope"
    assert "input_hash" not in node and "idempotency_key" not in node
    assert second["conversation_id"] == "original-conversation"
    assert "task_complete" in second["question"] and "检查仍在进行。" in second["question"]
    assert "不是本轮最终答复" in second["question"]
    assert "原成员报告的未完事项" not in second["question"]
    assert not second.get("_retained_results"), "this author must execute, not replay its empty return"
    assert "native_continuation" not in out
    assert out["persistent_diagnostics"][0]["native_continuation_requested"] is False
    assert out["persistent_diagnostics"][0]["native_terminal_message_absent"] == ["maker"]
    assert out["pilot"]["kind"] == "turn_returned"
    task = loop.team_state.load("__ut_absent_continue")["tasks"]["maker"]
    assert task["attempt_seq"] == 2 and task["status"] == "done"
    previous = task["result_history"][0]
    assert previous["reason"] == "native_terminal_message_absent"
    assert previous["result"]["checkpoint_writes"]["request"] is True
    assert previous["result"]["native_checkpoint"]["session_id"] == "original-native"


@pytest.mark.parametrize("override", [
    {"native_checkpoint": None},
    {"checkpoint_writes": {}},
    {"checkpoint_writes": {"request": False}},
    {"checkpoint_storage_error": "fixture storage unavailable"},
    {"native_resume_error": {"category": "fixture native unavailable"}},
])
def test_missing_terminal_without_reliable_cursor_does_not_retry_or_replan(loop, override):
    from test_native_final_diagnostics import absent_message_result

    loop.results[0] = absent_message_result(key="fixture-maker", task_id="maker", **override)
    before = copy.deepcopy(loop.results[0])
    loop.mp.setattr(loop.core, "_plan_empty_continuation",
                    lambda *a, **kw: pytest.fail("must not invent a new task for a held original"))
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_absent_unsaved",
        mode="pk", user_id="fixture", max_rounds=3)
    assert len(loop.calls) == 1
    assert out["pilot"]["kind"] == "native_terminal_message_unrecoverable"
    assert out["pilot"]["action"] == "stop"
    assert out["results"] == [before]
    task = loop.team_state.load("__ut_absent_unsaved")["tasks"]["maker"]
    assert task["status"] == "partial" and task["attempt_seq"] == 1
    assert not task.get("result_history")


@pytest.mark.parametrize("enabled,max_rounds", [(False, 3), (True, 1)])
def test_missing_terminal_keeps_disabled_and_round_limit_semantics(loop, enabled, max_rounds):
    from test_native_final_diagnostics import absent_message_result

    loop.results[0] = absent_message_result(key="fixture-maker", task_id="maker")
    loop.mp.setattr(loop.pilot, "load_user_config",
                    lambda *_a, **_kw: loop.pilot.merge_config({"enabled": enabled}))
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_absent_budget",
        mode="pk", user_id="fixture", max_rounds=max_rounds)
    assert len(loop.calls) == 1
    assert out["results"][0]["status"] == "partial"
    if enabled:
        assert out["persistent_capped"] is True
    else:
        assert out["pilot"]["kind"] == "disabled"
    task = loop.team_state.load("__ut_absent_budget")["tasks"]["maker"]
    assert task["status"] == "partial" and not task.get("result_history")


def test_persistent_single_member_consumes_its_existing_continuation_intent(loop):
    direct = loop.pilot.classify(loop.results[0]["final"], loop.core._pilot_run_state(loop.results[0]))
    assert direct["action"] == "continue"
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_native_continue",
        mode="pk", user_id="fixture", conversation_id="original-conversation", max_rounds=2)
    assert len(loop.calls) == 2, out.get("pilot")
    second = loop.calls[1]
    assert list(second["plan"]["tasks"]) == ["maker"]
    assert second["plan"]["tasks"]["maker"].get("harness") == "fixture-maker"
    assert second["conversation_id"] == "original-conversation"
    assert "Actual interaction is unfinished" in second["question"]
    assert not second.get("_retained_results"), "continuation cannot replay the old completed result"
    assert out["pilot"]["action"] == "stop"
    assert out["results"][0]["native_checkpoint"]["session_id"] == "original-native"
    task = loop.team_state.load("__ut_native_continue")["tasks"]["maker"]
    assert task["status"] == "done"
    assert task["native_session_scope"] == "original-scope"
    assert task["attempt_seq"] == 2
    assert task["result_history"][0]["reason"] == "native_member_continue"


def test_native_continue_respects_existing_round_budget(loop):
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_native_budget",
        mode="pk", user_id="fixture", max_rounds=1)
    assert len(loop.calls) == 1
    assert out["persistent_capped"] is True
    assert out["pilot"]["action"] == "continue"
    assert out["results"][0]["status"] == "done", "keep runtime completion separate from goal completion"


def test_disabled_pilot_does_not_execute_native_continuation(loop):
    loop.mp.setattr(loop.pilot, "load_user_config",
                    lambda *_a, **_kw: loop.pilot.merge_config({"enabled": False}))
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_native_disabled",
        mode="pk", user_id="fixture", max_rounds=2)
    assert len(loop.calls) == 1
    assert out["pilot"]["kind"] == "disabled"


def test_legitimate_short_answer_still_finishes_without_extra_turn(loop):
    loop.results[0] = {"key": "fixture-maker", "task_id": "maker", "status": "done", "final": "好"}
    out = loop.core.dispatch_persistent(
        "回一个字：好", loop.plan, qhash="__ut_native_short",
        mode="pk", user_id="fixture", max_rounds=2)
    assert len(loop.calls) == 1
    assert out["pilot"]["action"] == "stop"
    assert out["pilot"]["kind"] == "turn_returned"


@pytest.mark.parametrize("final", [
    "好", "2", "/outputs/report.pdf", "阶段成果已交回；剩余用户旅程尚未验证。",
])
def test_single_return_does_not_certify_user_goal(loop, final):
    row = {
        "status": "done", "final": final,
        "native_facts": {"turn_status": "completed"},
    }
    state = loop.core._pilot_run_state(row)
    assert state["delivered"] is True
    assert state["scope_goal_met"] is None, "运行完成和有返回都不是用户目标验收"
    assert state["delivery_return_ready"] is True
    verdict = loop.pilot.classify(final, state)
    assert verdict["action"] == "stop", "没有续作请求，不为补完成声明再调一次"
    assert verdict["kind"] == "turn_returned"


def test_ordinary_member_complete_does_not_acquire_team_completion_authority(loop):
    row = {
        "status": "done", "final": "已交回文件。",
        "orchestration_request": {"coordinate": {"action": "complete"}},
    }
    assert loop.core._pilot_run_state(row)["scope_goal_met"] is None


@pytest.mark.parametrize("status", ["error", "partial", "cancelled"])
def test_nonreturned_single_result_keeps_existing_open_scope(loop, status):
    row = {"status": status, "final": "已完成：/outputs/report.pdf"}
    state = loop.core._pilot_run_state(row)
    assert state["delivery_return_ready"] is False
    assert state["scope_goal_met"] is False
    assert loop.pilot.classify(row["final"], state)["action"] == "continue"


@pytest.mark.parametrize("final", ["好", "阶段成果已交回；剩余用户旅程尚未验证。"])
def test_persistent_records_return_without_certifying_user_goal(loop, final):
    loop.results[0] = {
        "key": "fixture-maker", "task_id": "maker", "status": "done", "final": final,
        "native_facts": {"turn_status": "completed"},
    }
    seen = []
    evaluate = loop.pilot.evaluate

    def capture(reply, state, user_id):
        seen.append(copy.deepcopy(state))
        return evaluate(reply, state, user_id)

    loop.mp.setattr(loop.pilot, "evaluate", capture)
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_return_not_goal",
        mode="pk", user_id="fixture", max_rounds=2)
    assert len(loop.calls) == 1
    assert seen[-1]["scope_goal_met"] is None
    assert seen[-1]["delivery_return_ready"] is True
    assert out["pilot"]["action"] == "stop"
    assert out["pilot"]["kind"] == "turn_returned"
    assert out["pilot"]["reason"]
    diagnostic = out["persistent_diagnostics"][-1]
    assert diagnostic["scope_goal_met"] is None
    assert diagnostic["scope_goal_met_source"] is None
    assert diagnostic["delivery_return_ready"] is True
    assert diagnostic["native_continuation_requested"] is False


def test_partial_multi_node_return_keeps_existing_scope_open(loop):
    loop.results[0] = {
        "key": "fixture-maker", "task_id": "maker",
        "status": "done", "final": "已完成：/outputs/report.pdf",
    }
    loop.plan["tasks"]["second"] = {"task": "a different dependency", "harness": "fixture-maker"}
    dispatch = loop.core.dispatch

    def partial_dispatch(*args, **kwargs):
        out = dispatch(*args, **kwargs)
        out["results"].append({
            "key": "fixture-maker", "task_id": "second",
            "status": "error", "fail_kind": "gateway",
        })
        out["total"] = 2
        return out

    loop.mp.setattr(loop.core, "dispatch", partial_dispatch)
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_return_partial_nodes",
        mode="pk", user_id="fixture", max_rounds=1)
    assert out["pilot"]["action"] == "continue"
    assert out["persistent_capped"] is True
    diagnostic = out["persistent_diagnostics"][-1]
    assert diagnostic["delivery_return_ready"] is False
    assert diagnostic["scope_goal_met"] is False
    assert diagnostic["scope_goal_met_source"] == "runtime_open_conditions"


@pytest.mark.parametrize(("second_status", "expected_scope", "expected_source",
                          "expected_action", "expected_kind"), [
    ("done", True, "captain_coordination", "stop", "done"),
    ("error", False, "runtime_open_conditions", "continue", "scope_not_met"),
])
def test_captain_complete_scope_source_distinguishes_open_runtime(
    loop, second_status, expected_scope, expected_source, expected_action, expected_kind,
):
    import task_outcome

    loop.results[0] = {
        "key": "fixture-maker", "task_id": "maker",
        "status": "done", "final": "已完成：/outputs/report.pdf",
    }
    loop.plan["tasks"]["second"] = {
        "task": "a different dependency", "harness": "fixture-maker",
    }
    loop.plan.update(captain="fixture-maker",
                     team_policy={"continuous_coordination": True})
    dispatch = loop.core.dispatch

    def audited_dispatch(*args, **kwargs):
        out = dispatch(*args, **kwargs)
        out["results"].append({
            "key": "fixture-maker", "task_id": "second", "status": second_status,
            "final": "已完成：/outputs/second.pdf" if second_status == "done" else "",
            "fail_kind": "gateway" if second_status == "error" else None,
        })
        # The existing outcome projection permits a terminal audit to pass
        # while retaining an earlier member failure; no model is called here.
        out["success_audit"] = {"enabled": True, "verdict": "pass"}
        return task_outcome.attach(out)

    declaration = {
        "ok": True, "action": "complete", "captain": "fixture-maker",
        "reason": "fixture completion declaration",
        "result": {"status": "done", "final": "fixture completion declaration"},
    }
    loop.mp.setattr(loop.core, "dispatch", audited_dispatch)
    loop.mp.setattr(loop.core, "_run_captain_coordination",
                    lambda *a, **kw: copy.deepcopy(declaration))
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_captain_scope_source",
        mode="orchestrate", user_id="fixture", max_rounds=1)
    assert len(loop.calls) == 1
    assert out["persistent_rounds"] == 1
    assert out["outcome"]["status"] == "completed"
    assert out["coordination"]["ok"] is True
    assert out["coordination"]["action"] == "complete"
    assert out["coordination"]["reason"] == declaration["reason"]
    assert out["pilot"]["action"] == expected_action
    assert out["pilot"]["kind"] == expected_kind
    assert bool(out.get("persistent_capped")) is (expected_action == "continue")
    diagnostic = out["persistent_diagnostics"][-1]
    assert diagnostic["delivery_return_ready"] is expected_scope
    assert diagnostic["scope_goal_met"] is expected_scope
    assert diagnostic["scope_goal_met_source"] == expected_source


def test_unknown_legacy_hook_state_does_not_gain_return_permission(loop):
    verdict = loop.pilot.classify("好", {})
    assert verdict["action"] == "continue"
    assert verdict["kind"] == "unfinished"


@pytest.mark.parametrize("open_fact", [
    {"scope_goal_met": False},
    {"verification_incomplete": True},
    {"contract_incomplete": True},
    {"all_failed": True},
    {"captain_coordination": {"action": "continue"}},
])
def test_return_fact_does_not_override_existing_open_conditions(loop, open_fact):
    state = {
        "delivered": True, "delivery_return_ready": True,
        "scope_goal_met": None, "scope_progressed": True, **open_fact,
    }
    assert loop.pilot.classify("2", state)["action"] == "continue"


@pytest.mark.parametrize("category", [
    "runtime_budget", "idle_stall", "harness_runtime", "native_iteration_budget",
])
@pytest.mark.parametrize("with_envelope", [True, False])
def test_native_continue_does_not_reopen_nonretryable_runtime_result(
    loop, category, with_envelope,
):
    import error_envelope
    row = loop.results[0]
    envelope = error_envelope.normalize("error", "Blocked: caller stopped.", fail_kind=category)
    assert envelope.retryable is False
    row.update(status="error", final="Blocked: caller stopped.", fail_kind=category)
    if with_envelope:
        row["error_envelope"] = envelope.to_dict()
    before = copy.deepcopy(row)
    team = "__ut_native_stop_" + category + str(with_envelope)
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash=team, mode="pk",
        user_id="fixture", max_rounds=2)
    assert len(loop.calls) == 1, "不可重试的运行终态不得被成员继续意图覆盖"
    assert out["pilot"]["action"] == "stop"
    assert out["results"] == [before], "保留原失败和原生 checkpoint，不伪造完成"
    task = loop.team_state.load(team)["tasks"]["maker"]
    assert task["attempt_seq"] == 1
    assert not task.get("result_history"), "停止后不得重开已结束的 attempt"


def test_native_continue_preserves_partial_artifact_on_runtime_stop(loop, tmp_path):
    import error_envelope
    artifact = tmp_path / "existing-artifact.txt"
    artifact.write_text("existing work, do not replay or overwrite")
    row = loop.results[0]
    row.update(status="partial", artifacts=[{"path": str(artifact)}],
               error_envelope=error_envelope.normalize(
                   "error", "Caller budget exhausted.", fail_kind="runtime_budget").to_dict())
    before = copy.deepcopy(row)
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_native_partial_stop",
        mode="pk", user_id="fixture", max_rounds=2)
    assert len(loop.calls) == 1
    assert out["pilot"]["action"] == "stop"
    assert out["results"] == [before]
    assert artifact.read_text() == "existing work, do not replay or overwrite"


def test_retryable_failure_keeps_existing_native_continuation(loop):
    import error_envelope
    loop.results[0].update(
        status="error", final="A retryable failure; original work remains.",
        error_envelope=error_envelope.normalize("timeout", "Temporary timeout.").to_dict())
    assert loop.results[0]["error_envelope"]["retryable"] is True
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash="__ut_native_retryable",
        mode="pk", user_id="fixture", max_rounds=2)
    assert len(loop.calls) == 2
    assert out["pilot"]["action"] == "stop"


@pytest.mark.parametrize(("status", "kind"), [
    ("cancelled", "user_cancelled"), ("error", "provider_authorization_required"),
])
def test_existing_cancellation_and_authorization_stops_keep_priority(loop, status, kind):
    row = loop.results[0]
    row["status"] = status
    if status == "error":
        row["error_envelope"] = {
            "category": "authorization_required", "scope": "provider",
            "code": "appid_disabled", "retryable": False, "message": "fixture rejection",
        }
    team = "__ut_native_existing_stop_" + status
    out = loop.core.dispatch_persistent(
        "original user objective", loop.plan, qhash=team,
        mode="pk", user_id="fixture", max_rounds=2)
    assert len(loop.calls) == 1
    assert out["pilot"]["action"] == "stop"
    assert out["pilot"]["kind"] == kind
    task = loop.team_state.load(team)["tasks"]["maker"]
    assert task["attempt_seq"] == 1 and not task.get("result_history")
