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


def test_independent_pk_preserves_declared_order_and_recommendation(case):
    out = dispatch(case, plan(case, independent=True), mode="pk")
    assert sorted(case.calls) == ["A", "B", "C"]
    assert out["dependencies"] == {"C": [], "A": [], "B": []}
    assert [row["task_id"] for row in out["results"]] == ["C", "A", "B"]
    assert out["winner"] == "B"
    assert case.core._winner_final(out) == "b"


def test_only_review_report_is_not_removed_from_final_projection(case):
    # This patch does not redefine reviewer eligibility. If only a review
    # report exists, the existing projection must still return that report.
    out = {"results": [{"key": "R", "task_id": "R", "status": "done",
                        "verification_role": "critic", "final": "review report"}]}
    assert case.core._winner_final(out) == "review report"
