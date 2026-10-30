"""CPU graph regressions: real dispatch/persistent -> run_one -> saved state.

Reuse the ordinary-entry scene and its network/process-denying fixtures. Only
native execution/discovery are synthetic; dispatch, run_one, task_memory and
team_state are not replaced. No live order, author, credentials or trace is read.

The single-node unbound-identity matrix remains in
test_native_final_diagnostics.test_unbound_absence_cannot_bypass_empty_answer_handling;
the unavailable-cursor matrix remains in
test_persistent_native_continuation.test_missing_terminal_without_reliable_cursor_does_not_retry_or_replan.
Here the write-failure case adds the previously missing mixed-DAG boundary.
"""
from collections import Counter
from copy import deepcopy
import importlib
import json

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_native_final_diagnostics import absent_message_facts, completed_facts
from test_project_export_terminal_binding import scene, USER, CONVERSATION


ORIGINAL = "__ut_graph_original"
WRITER = "__ut_graph_writer"
REPAIR = "__ut_graph_repair"
RELAY = "codex"
QUESTION = "Continue the existing CPU fixture and preserve its independent tasks."


def _node(text, *, owner=RELAY, depends=(), scope=None):
    node = {
        "task": text, "assigned_harness": owner, "assignment_locked": True,
        "depends_on": list(depends), "dependency_policy": "all_succeeded",
    }
    if scope is not None:
        node["native_session_scope"] = scope
    return node


def _plan(case, tasks, *, failover=0, repair=0):
    return {
        "_user_selected_mode": True, "tasks": tasks,
        "project_context": {"repo_root": str(case.work)},
        "execution_depth": "quick",
        "team_policy": {
            "adaptive": False, "max_parallel": 1,
            "max_failover": failover, "contract_repair_rounds": repair,
            "continuous_coordination": False,
        },
        "success_audit": {"enabled": False},
    }


def _returned(case, thread, *, absent=False, checkpoint=True):
    facts = absent_message_facts() if absent else completed_facts()
    if absent:
        facts["completion_message"]["thread_id"] = thread
    result = {
        "status": "ok", "final": "" if absent else str(case.work / "index.html"),
        "error": "", "workdir": str(case.work), "native_facts": facts,
    }
    if checkpoint:
        result["native_checkpoint"] = {"kind": "codex_thread", "session_id": thread}
    return result


@pytest.fixture
def graph(scene):
    case, mp, core = scene, scene.monkeypatch, scene.core
    case.tm = importlib.import_module("task_memory")
    case.teams = importlib.import_module("team_state")
    pilot = importlib.import_module("legion_pilot")
    keys = {RELAY, ORIGINAL, WRITER, REPAIR}
    mp.setattr(core, "_harness_keys", lambda: set(keys))
    mp.setattr(core, "_CORE_INPROC_KEYS", set(keys))
    mp.setattr(core, "_aigc_ready", lambda **kw: {"runnable_clis": sorted(keys)})
    mp.setattr(core, "_cost_class", lambda *a, **kw: "light")
    mp.setattr(core, "_dynamic_max_attempts", lambda _pool: 1)
    mp.setattr(core, "_record_experience", lambda *a, **kw: None)
    mp.setattr(core, "_autocommit_delivery", lambda *a, **kw: {"status": "skipped"})
    mp.setattr(pilot, "read_shared_goals", lambda: {})
    mp.setattr(pilot, "load_user_config", lambda *a, **kw: pilot.merge_config())
    case.native_calls, case.rounds, case.picks, case.empty_plans = [], [], [], []
    case.answer = lambda call: _returned(case, "__ut_native_" + call["task_id"])

    def native(key, task, qhash, model_override=None, appid_override=None, **kwargs):
        facts = kwargs.get("task_facts") or {}
        memory = kwargs.get("task_memory") or {}
        binding = kwargs.get("provider_binding") or {}
        # Observe the actual selected cursor at the last CPU/native boundary,
        # not a plan's intended assignment. Never log the provider's secret.
        call = {
            "key": key, "task_id": facts.get("task_id", ""),
            "qhash": qhash, "session_id": kwargs.get("session_id"),
            "scope": facts.get("native_session_scope", "conversation"),
            "origin": deepcopy(facts.get("native_context_origin")),
            "memory_id": facts.get("native_request_memory_id") or qhash,
            "checkpoint": deepcopy(memory.get("native_checkpoint")),
            "memory_workdir": memory.get("workdir"),
            "project_workdir": (kwargs.get("project_context") or {}).get("repo_root"),
            "model": model_override,
            "provider": ({
                "model": binding["model"], "base_url": binding["base_url"],
                "binding": case.mr.ccr_binding_key(
                    binding["model"], binding["api_key"], binding["base_url"]),
            } if binding else None),
        }
        case.native_calls.append(call)
        return case.answer(call)

    mp.setattr(core, "_run_core_inprocess", native)

    def pick(failed, tried, valid, *args):
        case.picks.append({"failed": failed, "tried": sorted(tried)})
        return None

    mp.setattr(core, "_pick_relay_harness", pick)

    def no_new_plan(*args, **kwargs):
        case.empty_plans.append(True)
        raise AssertionError("a held original DAG must not become an empty-plan new task")

    mp.setattr(core, "_plan_empty_continuation", no_new_plan)
    real_dispatch = core.dispatch

    def observe_dispatch(question, plan=None, **kwargs):
        entry = {"plan": deepcopy(plan), "retained": deepcopy(kwargs.get("_retained_results") or {})}
        case.rounds.append(entry)
        result = real_dispatch(question, plan, **kwargs)
        entry["out"] = deepcopy(result)
        entry["state"] = case.teams.load(str((plan or {}).get("team_id") or kwargs["qhash"]))
        return result

    # Spy only: every call goes through the unmodified production dispatch.
    mp.setattr(core, "dispatch", observe_dispatch)
    return case


def _run(case, plan, team, *, rounds=3):
    return case.core.dispatch_persistent(
        QUESTION, plan, qhash=team, mode="pk", user_id=USER,
        conversation_id=CONVERSATION, max_rounds=rounds, cb=case.events.append)


def _rows(out):
    return {row.get("task_id") or row.get("key"): row for row in out["results"]}


def _diagnostic(case, out):
    view = {
        "native_calls": case.native_calls, "relay_selections": case.picks,
        "round_tasks": [list(row["plan"]["tasks"]) for row in case.rounds],
        "round_results": [{
            key: {"status": row.get("status"), "owner": row.get("assigned_harness"),
                  "checkpoint": row.get("native_checkpoint"), "error": row.get("error")}
            for key, row in _rows(entry.get("out") or {"results": []}).items()
        } for entry in case.rounds],
        "dependencies": out.get("dependencies"),
        "pilot": out.get("pilot"), "empty_plan_calls": len(case.empty_plans),
    }
    print("NATIVE_TERMINAL_GRAPH", json.dumps(view, ensure_ascii=False))
    return view


@pytest.mark.parametrize("cursor_failure", ["missing_checkpoint", "checkpoint_write_failed"])
def test_unrecoverable_absent_keeps_full_graph_while_independent_branch_continues(graph, cursor_failure):
    case = graph
    counts = Counter()
    real_update = case.tm.update

    def update(*args, **kwargs):
        if (cursor_failure == "checkpoint_write_failed"
                and (kwargs.get("native_checkpoint") or {}).get("session_id") == "__ut_native_A"):
            raise OSError("fixture checkpoint store unavailable")
        return real_update(*args, **kwargs)

    case.monkeypatch.setattr(case.tm, "update", update)

    def answer(call):
        task = call["task_id"]
        counts[task] += 1
        return _returned(
            case, "__ut_native_" + task,
            absent=(task == "A" or (task == "B" and counts[task] == 1)),
            checkpoint=not (task == "A" and cursor_failure == "missing_checkpoint"))

    case.answer = answer
    deps = {"A": [], "B": [], "C": ["A"], "D": ["B"]}
    plan = _plan(case, {key: _node("CPU task " + key, depends=value) for key, value in deps.items()},
                 failover=1)
    team = "__ut_terminal_graph_" + cursor_failure
    out = _run(case, plan, team)
    view = _diagnostic(case, out)
    first = _rows(case.rounds[0]["out"])
    assert first["A"]["status"] == first["B"]["status"] == "partial", view
    assert case.core._native_terminal_message_absent(first["A"])["checkpoint_saved"] is False
    assert first["B"]["checkpoint_writes"]["request"] is True
    if cursor_failure == "checkpoint_write_failed":
        assert first["A"]["checkpoint_storage_error"] == "fixture checkpoint store unavailable"

    rows = _rows(out)
    assert counts == {"A": 1, "B": 2, "D": 1}, view
    assert case.picks == [] and not case.empty_plans, view
    assert len(case.rounds) == 2, view
    assert all(set(entry["plan"]["tasks"]) == set(deps) for entry in case.rounds), view
    assert out["dependencies"] == deps and set(rows) == set(deps), view
    for entry in case.rounds:
        assert {key: row.get("depends_on", []) for key, row in entry["plan"]["tasks"].items()} == deps
    assert rows["A"] == first["A"], "the unrecoverable return must not be overwritten"
    assert rows["B"]["status"] == rows["D"]["status"] == "done"
    assert rows["C"]["status"] == "blocked" and rows["C"]["blocked_by"] == ["A"]
    saved = case.teams.load(team)["tasks"]
    assert set(saved) == set(deps)
    assert saved["A"]["attempt_seq"] == 1 and not saved["A"].get("result_history")
    assert saved["C"]["attempt_seq"] == 0
    assert saved["B"]["attempt_seq"] == 2 and saved["D"]["attempt_seq"] == 1
    b_calls = [call for call in case.native_calls if call["task_id"] == "B"]
    assert b_calls[1]["checkpoint"]["session_id"] == "__ut_native_B"
    assert b_calls[1]["memory_workdir"] == str(case.work)


@pytest.mark.parametrize("scope", ["conversation", "request"])
def test_error_then_absent_relay_continues_actual_relay_cursor_and_node_scope(graph, scope):
    case = graph
    counts = Counter()

    def answer(call):
        counts[call["key"]] += 1
        if call["key"] == ORIGINAL:
            return {
                "status": "error", "final": "", "error": "fixture native execution failed",
                "workdir": str(case.work), "model_used": "fixture-original-model",
                "native_facts": {"process_returncode": 1, "turn_status": "failed"},
                "error_envelope": {"category": "process", "scope": "task", "retryable": True,
                                   "message": "fixture native execution failed"},
            }
        return _returned(case, "__ut_actual_relay_native", absent=counts[RELAY] == 1)

    case.answer = answer

    def pick(failed, tried, valid, *args):
        case.picks.append({"failed": failed, "tried": sorted(tried)})
        # Observe forbidden attempts to find a third author without running one.
        return RELAY if failed == ORIGINAL and RELAY not in tried else None

    case.monkeypatch.setattr(case.core, "_pick_relay_harness", pick)
    plan = _plan(case, {"A": _node("CPU original node", owner=ORIGINAL, scope=scope)}, failover=2)
    assert "native_session_scope" not in plan["team_policy"], "request scope must be node-local"
    team = "__ut_terminal_relay_" + scope
    out = _run(case, plan, team, rounds=2)
    view = _diagnostic(case, out)
    relay_calls = [call for call in case.native_calls if call["key"] == RELAY]
    assert relay_calls, "the original failed native execution must reach the existing relay path"
    first_call = relay_calls[0]
    memory = case.tm.load(first_call["memory_id"], RELAY)
    assert memory["native_checkpoint"]["session_id"] == "__ut_actual_relay_native"
    first_row = _rows(case.rounds[0]["out"])["A"]
    assert first_row["status"] == "partial" and first_row["error"] == "", view
    assert first_row["assigned_harness"] == RELAY, view
    assert first_row["native_checkpoint"]["session_id"] == "__ut_actual_relay_native", view
    first_saved = case.rounds[0]["state"]["tasks"]["A"]
    assert first_saved["assigned_harness"] == RELAY and first_saved["status"] == "partial", view
    assert first_saved["result"]["checkpoint_writes"]["request"] is True
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, RELAY, RELAY], view
    assert case.picks == [{"failed": ORIGINAL, "tried": [ORIGINAL]}], view
    assert len(case.rounds) == 2 and not case.empty_plans, view
    resumed = relay_calls[1]
    assert resumed["checkpoint"]["session_id"] == "__ut_actual_relay_native", view
    assert resumed["memory_workdir"] == resumed["project_workdir"] == str(case.work)
    assert first_call["model"] == resumed["model"] == "fixture-model"
    assert resumed["provider"] == first_call["provider"] and resumed["provider"] is not None
    assert all(call["scope"] == scope for call in relay_calls), view
    if scope == "request":
        assert resumed["origin"] == first_call["origin"]
        assert resumed["origin"]["request_id"] == team
    # qhash itself is deliberately not constrained: production has request and
    # conversation storage keys. Only the actual selected identity must match.
    assert resumed["session_id"] == first_call["session_id"]
    assert _rows(out)["A"]["status"] == "done"
    saved = case.teams.load(team)["tasks"]["A"]
    assert saved["assigned_harness"] == RELAY and saved["attempt_seq"] == 2
    assert any((row["result"].get("native_checkpoint") or {}).get("session_id") == "__ut_actual_relay_native"
               for row in saved.get("result_history") or [])


@pytest.mark.parametrize("with_independent", [False, True], ids=["only-absent", "independent-contract-gap"])
def test_contract_repair_excludes_absent_without_disabling_independent_candidate(graph, with_independent):
    case = graph
    tasks = {}
    if with_independent:
        tasks["B"] = _node("Return the fixture report.txt", owner=WRITER)
    # Put A last: selecting the last result instead of an eligible candidate is
    # exactly the missing boundary, not an accidental passing input order.
    tasks["A"] = _node("Keep the original CPU work", owner=RELAY)
    plan = _plan(case, tasks, repair=1)
    plan["delivery_contract"] = {
        "require_final": True,
        "required_artifacts": [{"kind": "file", "formats": ["txt"]}],
    }
    case.answer = lambda call: _returned(
        case, "__ut_native_" + call["key"], absent=call["task_id"] == "A")

    def pick(failed, tried, valid, *args):
        case.picks.append({"failed": failed, "tried": sorted(tried)})
        return REPAIR

    case.monkeypatch.setattr(case.core, "_pick_relay_harness", pick)
    team = "__ut_terminal_repair_" + str(with_independent)
    out = case.core.dispatch(
        QUESTION, plan, qhash=team, mode="pk", user_id=USER,
        conversation_id=CONVERSATION, cb=case.events.append)
    view = _diagnostic(case, out)
    rows = _rows(out)
    assert rows["A"]["status"] == "partial" and rows["A"]["assigned_harness"] == RELAY, view
    saved = case.teams.load(team)["tasks"]
    assert saved["A"]["status"] == "partial" and saved["A"]["attempt_seq"] == 1
    assert saved["A"]["assigned_harness"] == RELAY
    repairs = [row for row in out["results"] if row.get("contract_repair")]
    repair_calls = [call for call in case.native_calls if call["key"] == REPAIR]
    if not with_independent:
        assert not case.picks and not repair_calls and not repairs, view
    else:
        assert rows["B"]["status"] == "done" and saved["B"]["assigned_harness"] == WRITER
        assert len(repairs) == len(repair_calls) == len(case.picks) == 1, view
        assert case.picks[0]["failed"] == WRITER, "relay selection takes an author, not an absent task ID"
        assert repairs[0]["repair_for"] in {"B", WRITER}, view
        assert repairs[0]["repair_for"] not in {"A", RELAY}


def test_cpu_graph_control_uses_real_saved_state_and_all_dependencies(graph):
    """Positive control for the new fixture, not a rerun of the old suite."""
    case = graph
    deps = {"A": [], "B": [], "C": ["A"], "D": ["B"]}
    plan = _plan(case, {key: _node("CPU task " + key, depends=value) for key, value in deps.items()})
    team = "__ut_terminal_graph_control"
    out = _run(case, plan, team)
    assert len(case.rounds) == 1
    assert Counter(call["task_id"] for call in case.native_calls) == dict.fromkeys(deps, 1)
    assert out["dependencies"] == deps and all(row["status"] == "done" for row in out["results"])
    assert not case.picks and not case.empty_plans
    saved = case.teams.load(team)["tasks"]
    for call in case.native_calls:
        task = call["task_id"]
        assert saved[task]["status"] == "done" and saved[task]["attempt_seq"] == 1
        assert case.tm.load(call["memory_id"], call["key"])["native_checkpoint"]["session_id"] == "__ut_native_" + task


@pytest.mark.parametrize("existing_independent", [True, False],
                         ids=["rewire-independent-successor", "add-independent-successor"])
def test_held_branch_preserves_received_coordination_graph_changes(graph, existing_independent):
    """An unavailable cursor cannot discard the captain's already returned plan."""
    case = graph
    deps = {"A": [], "B": [], "C": ["A"]}
    if existing_independent:
        deps["D"] = ["B"]
    plan = _plan(case, {key: _node("CPU task " + key, depends=value)
                       for key, value in deps.items()})
    plan["captain"] = RELAY
    plan["team_policy"]["continuous_coordination"] = True
    case.answer = lambda call: _returned(
        case, "__ut_native_" + call["task_id"],
        absent=call["task_id"] == "A", checkpoint=call["task_id"] != "A")
    controls = []

    def control(key, prompt, **kwargs):
        controls.append(kwargs["qhash"])
        proposal = {"action": "complete", "reason": "No further fixture changes."}
        if len(controls) == 1:
            proposal = {
                "action": "continue", "reason": "Use the completed independent input.",
                "next_plan": {"tasks": {
                    "E": _node("CPU captain-added task E", depends=["B"])}},
                "task_updates": {"D": {"depends_on": ["E"]}} if existing_independent else {},
            }
        return {"status": "done", "final": "CPU coordination result",
                "orchestration_request": {"coordinate": proposal}}

    case.monkeypatch.setattr(case.core, "_run_one_with_gateway", control)
    team = "__ut_terminal_coordination_" + str(existing_independent)
    out = case.core.dispatch_persistent(
        QUESTION, plan, qhash=team, mode="orchestrate", user_id=USER,
        conversation_id=CONVERSATION, max_rounds=3, cb=case.events.append)
    view = _diagnostic(case, out)
    first = case.rounds[0]["out"]
    assert first["_coordination_handoff"]["proposal"]["ok"] is True, view
    assert "E" in first["_coordination_handoff"]["proposal"]["next_plan"]["tasks"]
    assert not case.core._native_terminal_message_absent(_rows(first)["A"])["checkpoint_saved"]
    order = [call["task_id"] for call in case.native_calls]
    expected = {"A": 1, "B": 1, "E": 1, **({"D": 1} if existing_independent else {})}
    assert Counter(order) == expected, view
    if existing_independent:
        assert order.index("E") < order.index("D"), "old dependencies must not run before the handoff"
        deps["D"] = ["E"]
    deps["E"] = ["B"]
    assert len(case.rounds) == 2 and not case.empty_plans and not case.picks, view
    assert len(controls) == (2 if existing_independent else 1), (
        "applying an already received handoff must not call the coordinator again at round end")
    assert out["dependencies"] == deps, view
    saved = case.teams.load(team)["tasks"]
    assert set(saved) == set(deps)
    assert saved["A"]["attempt_seq"] == 1 and not saved["A"].get("result_history")
    assert saved["C"]["attempt_seq"] == 0
    for task in expected.keys() - {"A"}:
        assert saved[task]["status"] == "done" and saved[task]["attempt_seq"] == 1
    assert out["pilot"]["kind"] == "native_terminal_message_unrecoverable", view
