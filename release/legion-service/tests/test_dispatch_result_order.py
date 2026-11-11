"""Offline persistent result projection; no original task data is replayed.

Only native execution and environment discovery are stubbed. DAG compilation,
scheduling, persistent prerequisite restoration and _winner_final stay real.
Direct reverse-declaration failures are diagnostic, not a global sorting rule.
"""
from contextlib import nullcontext
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
HARNESS = "__ut_result_order_member"
TEAM = "__ut_dispatch_result_order"
OWNER = "__ut_order_owner"
CONVERSATION = "__ut_order_conversation"


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    # Pin at execution time, not collection time; never inherit live bindings.
    for name in tuple(os.environ):
        if name.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(name)
    for name, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_TASK_MEMORY_DIR", "memory"),
        ("LEGION_TEAM_DIR", "teams"), ("LEGION_SESSIONS_DIR", "sessions"),
        ("LEGION_USER_CONFIG_DIR", "users"), ("LEGION_ARTIFACT_STORE_DIR", "artifacts"),
        ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        (tmp_path / leaf).mkdir()
        monkeypatch.setenv(name, str(tmp_path / leaf))
    monkeypatch.syspath_prepend(str(SERVICE))
    forbidden_calls = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append(True)
        raise AssertionError("real model/network/process access forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    core = importlib.import_module("legion_core")
    for module, leaf in (
        ("project_context", "projects"), ("task_memory", "memory"),
        ("artifact_store", "artifacts"),
    ):
        monkeypatch.setattr(importlib.import_module(module), "ROOT", tmp_path / leaf)
    models = importlib.import_module("model_registry")
    monkeypatch.setattr(models, "resolve_assignments", lambda *a, **kw: {})
    monkeypatch.setattr(core, "_harness_keys", lambda: {HARNESS})
    monkeypatch.setattr(core, "_aigc_ready", lambda **kw: {"runnable_clis": [HARNESS]})
    # Cost discovery otherwise schedules a background probe of real harnesses.
    monkeypatch.setattr(core, "_autonomy_matrix", lambda: {})
    for name in ("_build_prelude", "_build_runtime_brief",
                 "_read_async_deep_plans", "_captain_mail_context"):
        monkeypatch.setattr(core, name, lambda *a, **kw: "")
    monkeypatch.setattr(core, "_baseline_snapshot", lambda *a, **kw: None)

    class Lease(nullcontext):
        wait_seconds = 0

    monkeypatch.setattr(core, "_GatewayLease", lambda *a, **kw: Lease())
    monkeypatch.setattr(core, "_run_one_with_gateway", forbidden)
    calls = []

    def native_member(key, prompt, **kwargs):
        assert key == HARNESS
        task_id = kwargs["task_facts"]["task_id"]
        calls.append(task_id)
        # Equal-length, domain-free results: no content/length ranking is valid.
        return {"status": "done", "final": task_id.lower()}

    monkeypatch.setattr(core, "run_one", native_member)
    work = tmp_path / "work"
    work.mkdir()
    yield SimpleNamespace(core=core, mp=monkeypatch, calls=calls, work=work)
    assert not forbidden_calls, "dispatch tried forbidden IO, even if it swallowed the error"


def task(prerequisites=()):
    return {"task": "Produce this node's result", "assigned_harness": HARNESS,
            "assignment_locked": True, "depends_on": list(prerequisites)}


def plan(case, *, independent=False, coordinate=False):
    # Deliberately not topological (nor alphabetic in the independent case).
    tasks = ({"C": task(), "A": task(), "B": task()} if independent else
             {"C": task(["B"]), "B": task(["A"]), "A": task()})
    return {
        "captain": HARNESS, "tasks": tasks,
        "team_policy": {"continuous_coordination": coordinate},
        "project_context": {"repo_root": str(case.work), "user_id": OWNER,
                            "conversation_id": CONVERSATION},
    }


def dispatch(case, task_plan, *, mode="orchestrate"):
    return case.core.dispatch(
        "Return the result of this declared graph", deepcopy(task_plan),
        qhash=TEAM, mode=mode, user_id=OWNER, conversation_id=CONVERSATION)


def assert_projection(case, out, expected, check):
    actual = [row["task_id"] for row in out["results"]]
    chosen = case.core._winner_final(out)
    print("DISPATCH_PROJECTION", json.dumps({
        "native_calls": case.calls, "returned": actual, "expected": expected,
        "winner": out.get("winner"), "selected_final": chosen,
        "expected_final": expected[-1].lower(),
    }))
    if check == "order":
        assert actual == expected, "restored prerequisites must precede the new declaration"
    else:
        assert chosen == expected[-1].lower(), "fallback must not select an older prerequisite"


@pytest.mark.parametrize("check", ["order", "winner_final"])
def test_persistent_restores_old_prerequisites_before_new_declaration(case, check):
    initial = plan(case, coordinate=True)
    initial["tasks"] = {"B": task(), "A": task(["B"])}
    declared = {
        "D": {**task(["A"]), "task": "The first newly declared node"},
        "C": {**task(["A"]), "task": "The second newly declared node"},
    }
    dispatch_inputs = []
    real_dispatch = case.core.dispatch

    def observe_dispatch(question, plan=None, **kwargs):
        # Record inputs, then run the actual dispatcher unchanged.
        dispatch_inputs.append({
            "tasks": deepcopy(plan["tasks"]),
            "retained": deepcopy(kwargs.get("_retained_results") or {}),
        })
        return real_dispatch(question, plan=plan, **kwargs)

    case.mp.setattr(case.core, "dispatch", observe_dispatch)
    controls = []

    def native_control(key, prompt, **kwargs):
        assert key == HARNESS
        controls.append(kwargs["qhash"])
        if len(controls) == 1:
            proposal = {"action": "continue", "reason": "consume the first result",
                        "task_updates": {"A": {"task": "Use the returned prerequisite"}}}
        elif len(controls) == 2:
            # Only the native coordinator declares new work. Both new nodes
            # need A; B is restored transitively through A, without reexecution.
            # Preserve D,C as declared, not alphabetically/wave sorted C,D.
            proposal = {"action": "continue", "reason": "append the next work",
                        "next_plan": {"tasks": deepcopy(declared)}}
        else:
            assert len(controls) == 3, "unexpected extra coordination/retry"
            proposal = {"action": "complete", "reason": "all declared nodes completed"}
        return {"status": "done", "final": "dependency update",
                "orchestration_request": {"coordinate": proposal}}

    case.mp.setattr(case.core, "_run_one_with_gateway", native_control)
    pilot = importlib.import_module("legion_pilot")
    case.mp.setattr(pilot, "evaluate", lambda *a, **kw: {"action": "stop", "kind": "done"})
    case.mp.setattr(case.core, "_autocommit_delivery",
                    lambda *a, **kw: {"status": "skipped", "reason": "offline_fixture"})
    out = case.core.dispatch_persistent(
        "Return the result of this declared graph", initial,
        qhash=TEAM, mode="orchestrate", user_id=OWNER,
        conversation_id=CONVERSATION, max_rounds=2)
    assert case.calls[:2] == ["B", "A"]
    assert sorted(case.calls[2:]) == ["C", "D"], "no restored predecessor may rerun"
    assert len(dispatch_inputs) == 2
    assert list(dispatch_inputs[0]["tasks"]) == ["B", "A"]
    current = dispatch_inputs[1]
    assert set(current["retained"]) == {"A", "B"}
    assert [key for key in current["tasks"] if key in declared] == ["D", "C"]
    for key, node in declared.items():
        for field, value in node.items():
            assert current["tasks"][key][field] == value, "do not rewrite the native declaration"
    assert len(controls) == 3
    assert out["persistent_rounds"] == 2
    assert out["coordination"]["action"] == "complete"
    assert out["pilot"]["kind"] == "done"
    assert out["delivered_count"] == out["total"] == 4
    assert all(row["status"] == "done" for row in out["results"])
    assert out["winner"] == "" and out["final_result"] is None
    # The historical request order is B,A and the new declaration is D,C.
    # This specifically does NOT require a change to global dispatch ordering.
    assert_projection(case, out, ["B", "A", "D", "C"], check)


@pytest.mark.parametrize("result_policy,reported_winner,verdict,expected_winner,expected_verdict", [
    ("bundle", "candidate_1", "pass", "", "unverified"),
    ("bundle", "candidate_1", "revise", "", "revise"),
    ("bundle", "delivery_bundle", "pass", "__delivery__", "pass"),
    ("parallel", "candidate_1", "pass", "A", "pass"),
    ("direct", "candidate_2", "pass", "B", "pass"),
])
def test_success_audit_winner_respects_actual_evaluation_scope(
        case, result_policy, reported_winner, verdict, expected_winner, expected_verdict):
    audit = importlib.import_module("success_auditor")
    candidates = [{"key": "A", "task_id": "A", "final": "first", "status": "done"},
                  {"key": "B", "task_id": "B", "final": "second", "status": "done"}]
    policy = {} if result_policy == "bundle" else {"result_policy": result_policy}
    prompt, aliases = audit.build_prompt("original user goal", policy, candidates)
    sample = prompt.split("<<LEGION_VERIFY>>", 1)[1].split("<<END>>", 1)[0]
    sample_report = json.loads(sample)
    expected_example = "delivery_bundle" if result_policy == "bundle" else "candidate_1"
    assert sample_report["winner"] == expected_example
    assert expected_example in sample_report["scores"]
    values = {name: 80 for name in (*audit.DIMENSIONS, "overall")}
    frame = {"verdict": verdict, "winner": reported_winner, "scores": {reported_winner: values},
             "findings": ["finding retained"], "evidence": ["evidence retained"],
             "reason": "audit observation"}
    cleaned, report = audit.parse_report(
        "public audit summary\n<<LEGION_VERIFY>>" + json.dumps(frame) + "<<END>>",
        aliases, reviewer="independent-reviewer", author_hidden=True)
    assert cleaned == "public audit summary"
    assert report["winner"] == expected_winner
    assert report["verdict"] == expected_verdict
    assert report["scores"][aliases[reported_winner]] == values
    assert report["findings"] == ["finding retained"]
    assert report["evidence"] == ["evidence retained"]
    if result_policy == "bundle" and reported_winner == "candidate_1":
        assert report["winner_rejected"] == {
            "alias": "candidate_1", "reason": "bundle_requires_delivery_bundle"}
    else:
        assert "winner_rejected" not in report


@pytest.mark.parametrize("mode,policy,claimed,expected_final,expected_verdict", [
    ("relay", {}, "candidate_1", "b", "unverified"),
    ("relay", {}, "delivery_bundle", "b", "pass"),
    ("orchestrate", {}, "candidate_1", "", "unverified"),
    ("pk", {"result_policy": "parallel"}, "candidate_1", "a", "pass"),
])
def test_dispatch_audit_scope_preserves_real_delivery_and_session(
        case, mode, policy, claimed, expected_final, expected_verdict):
    import sessions
    audit = importlib.import_module("success_auditor")
    reviewer = "__ut_independent_reviewer"
    case.mp.setattr(case.core, "_harness_keys", lambda: {HARNESS, reviewer})
    case.mp.setattr(case.core, "_aigc_ready", lambda **kw: {"runnable_clis": [HARNESS, reviewer]})
    case.mp.setattr(case.core, "_pick_relay_harness", lambda *args, **kw: reviewer)
    fields = {name: 90 for name in (*audit.DIMENSIONS, "overall")}
    frame = {"winner": claimed, "verdict": "pass", "scores": {claimed: fields},
             "reason": "fixture audit", "findings": ["retained finding"], "evidence": ["retained evidence"]}
    audit_calls = []
    def run_audit(key, prompt, **kwargs):
        audit_calls.append((key, prompt))
        assert key == reviewer
        return {"status": "done", "final": "Independent audit\n<<LEGION_VERIFY>>"
                + json.dumps(frame) + "<<END>>", "model_used": "fixture-reviewer"}
    case.mp.setattr(case.core, "_run_one_with_gateway", run_audit)
    task_plan = plan(case, independent=True)
    task_plan["tasks"] = {"A": task(), "B": task()}
    task_plan["success_audit"] = {"enabled": True}
    task_plan.update(policy)
    if mode == "relay":
        task_plan["pipeline"] = ["A", "B"]
    out = dispatch(case, task_plan, mode=mode)
    assert len(audit_calls) == 1
    assert out["success_audit"]["verdict"] == expected_verdict
    assert case.core._winner_final(out) == expected_final
    assert [row["final"] for row in out["results"]] == ["a", "b"]
    assert out["success_audit"]["findings"] == ["retained finding"]
    assert out["success_audit"]["evidence"] == ["retained evidence"]
    if mode == "relay":
        assert out["final_result"]["task_id"] == "B", "overall audit must not replace the relay tail"
        if claimed == "candidate_1":
            assert out["success_audit"]["winner_rejected"]["alias"] == claimed
            assert out["outcome"]["verification_incomplete"] is True
    elif mode == "orchestrate":
        assert out["winner"] == ""
        assert out["selection"]["source"] == "unadjudicated_candidates"
        assert out["selection"]["selected_task_id"] == ""
        assert out["success_audit"]["winner_rejected"]["alias"] == claimed
        assert out["outcome"]["verification_incomplete"] is True
    else:
        assert out["winner"] == "A"
        assert out["selection"]["selected_task_id"] == "A"
    sid = "__ut_scoped_audit_" + mode + "_" + claimed
    saved = sessions.save_turn(sid, "fixture", out, mode=mode, user_id=OWNER)
    turn = sessions.load_session(saved["sid"], user_id=OWNER)["turns"][-1]
    assert turn["success_audit"] == out["success_audit"]
    assert turn["selection"] == out["selection"]
    assert case.core._winner_final(turn) == expected_final


def test_success_audit_bundle_without_winner_cannot_fabricate_pass(case):
    audit = importlib.import_module("success_auditor")
    _, aliases = audit.build_prompt("original goal", {}, [{"task_id": "A", "final": "candidate"}])
    values = {name: 80 for name in (*audit.DIMENSIONS, "overall")}
    _, report = audit.parse_report(
        "<<LEGION_VERIFY>>" + json.dumps({"verdict": "pass", "scores": {"delivery_bundle": values}}) + "<<END>>",
        aliases, reviewer="independent-reviewer")
    assert report["winner"] == ""
    assert report["verdict"] == "unverified"
    assert report["scores"]["__delivery__"] == values
    assert "winner_rejected" not in report


@pytest.mark.parametrize("selection", ["winner", "success_audit", "final_result"])
def test_explicit_selection_still_overrides_order_fallback(case, selection):
    out = dispatch(case, plan(case))
    chosen = next(row for row in out["results"] if row["task_id"] == "B")
    if selection == "winner":
        out["winner"] = "B"
    elif selection == "success_audit":
        out["success_audit"] = {"winner": "B"}
    else:
        out["final_result"] = chosen
    assert case.core._winner_final(out) == "b"


def test_independent_pk_preserves_declared_order_without_false_quality_winner(case):
    out = dispatch(case, plan(case, independent=True), mode="pk")
    assert sorted(case.calls) == ["A", "B", "C"]
    assert out["dependencies"] == {"C": [], "A": [], "B": []}
    assert [row["task_id"] for row in out["results"]] == ["C", "A", "B"]
    assert out["winner"] == "", "last delivery is not evidence that it is better"
    assert out["winner_note"] == out["success_audit"]["reason"] == "简单任务无需额外模型审计"
    assert case.core._winner_final(out) == "", "do not silently adopt a last candidate as truth"
    selected = out.get("selection")
    assert selected["source"] == "unadjudicated_candidates"
    assert selected["selected_task_id"] == ""
    assert selected["selected_harness"] == ""
    assert selected["audit_basis"] == "not_recorded"
    assert [row["final"] for row in out["results"]] == ["c", "a", "b"]


@pytest.mark.parametrize("mode", ["pk", "orchestrate"])
@pytest.mark.parametrize("claimed,audit,expected_source", [
    ("missing", {}, "unresolved_winner"),
    ("missing", {"winner": "missing", "verdict": "unverified"}, "unresolved_winner"),
    ("__delivery__", {"winner": "__delivery__", "verdict": "pass"}, "success_audit_bundle"),
])
def test_unmatched_winner_never_attributes_last_candidate(case, mode, claimed, audit, expected_source):
    rows = [
        {"key": "worker-a", "task_id": "task-a", "harness": "actual-a", "status": "done", "final": "first"},
        {"key": "worker-b", "task_id": "task-b", "harness": "actual-b", "status": "done", "final": "last"},
    ]
    out = {"execution_mode": mode, "winner": claimed, "success_audit": audit, "results": rows}
    facts = case.core._selection_facts(out, rows, mode, None, audit)
    assert facts["source"] == expected_source
    assert facts["selected_task_id"] == ""
    assert facts["selected_harness"] == ""
    assert facts["author_basis"] == "not_recorded"
    assert facts["audit_basis"] == ("recorded" if expected_source == "success_audit_bundle" else "not_recorded")
    assert case.core._winner_final(out) == "", "an unresolved winner cannot silently choose the last candidate"


def test_audited_task_id_and_actual_executor_are_not_confused(case):
    rows = [
        {"key": "slot-a", "task_id": "task-a", "harness": "actual-a", "assigned_harness": "assigned-a",
         "status": "done", "final": "first"},
        {"key": "slot-b", "task_id": "task-b", "harness": "actual-b", "status": "done", "final": "last"},
    ]
    audit = {"winner": "task-a", "verdict": "pass"}
    out = {"execution_mode": "pk", "winner": "task-a", "success_audit": audit, "results": rows}
    facts = case.core._selection_facts(out, rows, "pk", None, audit)
    assert facts["source"] == "success_audit"
    assert facts["selected_task_id"] == "task-a"
    assert facts["selected_harness"] == "actual-a"
    assert facts["author_basis"] == "not_recorded", "auditor selection is not an author decision"
    assert facts["audit_basis"] == "recorded"
    assert case.core._winner_final(out) == "first"


@pytest.mark.parametrize("claimed", ["missing", "__delivery__"])
def test_invalid_winner_never_displaces_valid_relay_tail(case, claimed):
    rows = [
        {"key": "A", "task_id": "A", "harness": "first", "status": "done", "final": "earlier"},
        {"key": "B", "task_id": "B", "harness": "last", "status": "done", "final": "relay tail"},
    ]
    audit = {"winner": claimed}
    out = {"execution_mode": "relay", "winner": claimed, "success_audit": audit,
           "final_result": rows[-1], "results": rows}
    facts = case.core._selection_facts(out, rows, "relay", rows[-1], audit)
    assert facts["source"] == "final_result_reference"
    assert facts["selected_task_id"] == "B"
    assert facts["audit_basis"] == "not_recorded"
    assert case.core._winner_final(out) == "relay tail"


def test_duplicate_legacy_key_is_unresolved_and_preserves_both_rows(case):
    rows = [
        {"key": "same", "task_id": "A", "harness": "first", "status": "done", "final": "first"},
        {"key": "same", "task_id": "B", "harness": "last", "status": "done", "final": "second"},
    ]
    out = {"execution_mode": "pk", "winner": "same", "results": rows}
    facts = case.core._selection_facts(out, rows, "pk", None, {})
    assert facts["source"] == "unresolved_winner"
    assert facts["selected_task_id"] == ""
    assert case.core._winner_final(out) == ""
    assert [row["final"] for row in out["results"]] == ["first", "second"]


def test_unadjudicated_selection_survives_session_round_trip(case):
    out = dispatch(case, plan(case, independent=True), mode="pk")
    assert out["execution_mode"] == "pk"
    assert out["selection"]["source"] == "unadjudicated_candidates"
    assert case.core._winner_final(out) == ""
    import sessions
    saved = sessions.save_turn("__ut_unadjudicated_pk", "fixture", out, mode="pk", user_id=OWNER)
    restored = sessions.load_session(saved["sid"], user_id=OWNER)
    turn = restored["turns"][-1]
    assert turn["selection"] == out["selection"]
    assert turn["execution_mode"] == "pk"
    assert case.core._winner_final(turn) == "", "a reload must not promote the last candidate"
    assert [row["final"] for row in turn["results"]] == ["c", "a", "b"]
    continuation = sessions.conversation_context(saved["sid"], user_id=OWNER)
    assert "【C 的原答案】c" in continuation
    assert "【A 的原答案】a" in continuation
    assert "【B 的原答案】b" in continuation


@pytest.mark.parametrize("verdict", ["pass", "revise", "fail", "blocked", "unverified"])
def test_session_round_trip_preserves_actual_audit_report(case, verdict):
    import sessions

    rows = [{"key": "A", "task_id": "A", "harness": HARNESS,
             "status": "done", "final": "candidate evidence"}]
    audit = {"enabled": True, "verdict": verdict, "winner": "A", "profile": "frontier",
             "scores": {"A": {"overall": 42}},
             "frontier": {"proven": False, "blockers": ["comparison not proven"]},
             "reason": "synthetic audit reason", "evidence": ["synthetic evidence"]}
    out = {"execution_mode": "pk", "results": rows, "winner": "A", "success_audit": audit,
           "selection": {"source": "success_audit", "selected_task_id": "A", "audit_basis": "recorded"},
           "outcome": {"status": "partial" if verdict != "pass" else "completed"}}
    sid = "__ut_audit_round_trip_" + verdict
    saved = sessions.save_turn(sid, "fixture", out, mode="pk", user_id=OWNER)
    turn = sessions.load_session(saved["sid"], user_id=OWNER)["turns"][-1]
    assert turn["success_audit"] == audit
    assert turn["selection"] == out["selection"]
    assert turn["execution_mode"] == "pk"
    assert turn["results"] == rows
    assert case.core._winner_final(turn) == "candidate evidence"


def test_legacy_session_without_audit_remains_unknown(case):
    import sessions

    saved = sessions.save_turn("__ut_legacy_audit", "fixture",
                               {"results": [{"key": "A", "status": "done", "final": "candidate"}]},
                               mode="pk", user_id=OWNER)
    turn = sessions.load_session(saved["sid"], user_id=OWNER)["turns"][-1]
    assert "success_audit" not in turn, "absence must not become a fabricated disabled verdict"


def test_only_review_report_is_not_removed_from_final_projection(case):
    # This patch does not redefine reviewer eligibility. If only a review
    # report exists, the existing projection must still return that report.
    out = {"results": [{"key": "R", "task_id": "R", "status": "done",
                        "verification_role": "critic", "final": "review report"}]}
    assert case.core._winner_final(out) == "review report"
