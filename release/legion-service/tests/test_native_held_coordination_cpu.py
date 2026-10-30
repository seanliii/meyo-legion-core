"""Only the new held+received graph handoff seam; real dispatch/persistent.
The imported fixtures deny sockets/subprocesses and isolate all runtime stores.
"""
from collections import Counter
from copy import deepcopy
import json
import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, USER, CONVERSATION
from test_native_terminal_dispatch_graph import (
    graph, _node, _plan, _returned, _rows, RELAY, QUESTION,
)


@pytest.mark.parametrize("scenario", [
    "release-held-child", "attach-child-to-held", "reopen-independent",
    "reopen-held", "reopen-held-rewire", "cancel-after-handoff", "provider-stop", "round-limit",
])
def test_received_graph_pause_boundaries(graph, scenario):
    case, core = graph, graph.core
    deps = {"A": [], "B": [], "C": ["A"], "D": ["B"]}
    plan = _plan(case, {k: _node("Synthetic task " + k, depends=v) for k,v in deps.items()})
    plan["captain"] = RELAY
    plan["team_policy"]["continuous_coordination"] = True
    case.answer = lambda call: _returned(
        case, "__ut_peer_held_" + call["task_id"], absent=call["task_id"] == "A",
        checkpoint=call["task_id"] != "A")
    controls, phases = [], []
    real_coordinator = core._run_captain_coordination

    def observe_coordination(*a, **kw):
        phases.append(kw.get("phase"))
        return real_coordinator(*a, **kw)

    case.monkeypatch.setattr(core, "_run_captain_coordination", observe_coordination)

    def control(key, prompt, **kwargs):
        controls.append(kwargs["qhash"])
        proposal = {"action": "complete", "reason": "Synthetic response; no additional work."}
        if len(controls) == 1:
            tasks = {"E": _node("Synthetic newly declared E", depends=["B"])}
            updates = {"D": {"depends_on": ["E"]}}
            if scenario == "release-held-child":
                updates["C"] = {"depends_on": ["E"]}
            elif scenario == "attach-child-to-held":
                updates["D"] = {"depends_on": ["A"]}
            elif scenario == "reopen-independent":
                tasks["B"] = _node("Synthetic explicit revision of completed B")
            elif scenario in ("reopen-held", "reopen-held-rewire"):
                tasks["A"] = _node(
                    "Synthetic explicit revision of held A",
                    depends=["B"] if scenario == "reopen-held-rewire" else [])
            proposal = {"action": "continue", "reason": "Synthetic received graph changes",
                        "next_plan": {"tasks": tasks}, "task_updates": updates}
        result = {"status": "done", "final": "Synthetic coordinator result",
                  "orchestration_request": {"coordinate": proposal}}
        if scenario == "provider-stop":
            result.update(status="error", final="", error="Synthetic authorization rejection",
                          error_envelope={"category": "authorization_required", "scope": "provider",
                                          "code": "appid_disabled", "retryable": False,
                                          "message": "Synthetic authorization rejection"})
        return result

    case.monkeypatch.setattr(core, "_run_one_with_gateway", control)
    # A cancellation signal arriving after a joined handoff but before the
    # persistent consumer: signal only, never stop a real service/process.
    if scenario == "cancel-after-handoff":
        cancelled = [False]
        real_is_cancelled = core.is_cancelled
        real_dispatch = core.dispatch
        def cancelled_now(*a, **kw):
            return cancelled[0] or real_is_cancelled(*a, **kw)
        def cancel_after_dispatch(*a, **kw):
            out = real_dispatch(*a, **kw)
            assert out.get("_coordination_handoff"), "exercise the received-proposal boundary"
            cancelled[0] = True
            return out
        case.monkeypatch.setattr(core, "is_cancelled", cancelled_now)
        case.monkeypatch.setattr(core, "dispatch", cancel_after_dispatch)
    team = "__ut_peer_held_" + scenario
    limit = 1 if scenario == "round-limit" else 3
    out = core.dispatch_persistent(
        QUESTION, plan, qhash=team, mode="orchestrate", user_id=USER,
        conversation_id=CONVERSATION, max_rounds=limit, cb=case.events.append)
    counts = Counter(c["task_id"] for c in case.native_calls)
    saved = case.teams.load(team)["tasks"]
    first_state = case.rounds[0]["state"]["tasks"]
    first_rows = _rows(case.rounds[0]["out"])
    report = {
        "scenario": scenario, "counts": dict(counts), "rounds": len(case.rounds),
        "controls": controls, "phases": phases, "dependencies": out.get("dependencies"),
        "pilot": out.get("pilot"), "capped": out.get("persistent_capped"),
        "rows": {k:{f:r.get(f) for f in ("status", "blocked_by", "error")} for k,r in _rows(out).items()},
        "state": {k:{f:r.get(f) for f in ("status", "attempt_seq", "dependencies", "assigned_harness")}
                  for k,r in saved.items()},
        "held_state_before": {f:first_state["A"].get(f) for f in ("status", "attempt_seq")},
        "held_result_preserved": _rows(out).get("A") == first_rows["A"],
        "retained_per_round": [list(r["retained"]) for r in case.rounds],
        "picks": case.picks, "empty_plans": case.empty_plans,
    }
    print("HELD_PEER_OBSERVATION " + json.dumps(report,ensure_ascii=False,sort_keys=True))
    assert counts["A"] == 1 and saved["A"]["attempt_seq"] == 1, report
    assert _rows(out).get("A") == first_rows["A"], report
    assert not case.picks and not case.empty_plans, report
    assert "round_complete" not in phases, report
    if scenario in ("cancel-after-handoff", "provider-stop", "round-limit"):
        assert counts == {"A":1, "B":1} and len(case.rounds) == 1, report
        assert len(controls) == 1, report
        if scenario == "cancel-after-handoff":
            assert out["pilot"]["kind"] == "user_cancelled", report
        elif scenario == "provider-stop":
            assert out["pilot"]["kind"] == "provider_authorization_required", report
        else:
            assert out["persistent_capped"] is True and out["persistent_rounds"] == 1, report
            assert saved["E"]["attempt_seq"] == 0, report
        return
    expected={"A":1, "B":1, "E":1, "D":1}
    if scenario == "release-held-child": expected["C"]=1
    if scenario == "attach-child-to-held": expected.pop("D")
    if scenario == "reopen-independent": expected["B"]=2
    assert counts == expected, report
    assert len(case.rounds) == 2, report
    deps["E"]=["B"]
    deps["D"]=["A"] if scenario == "attach-child-to-held" else ["E"]
    if scenario == "release-held-child": deps["C"]=["E"]
    if scenario == "reopen-held-rewire": deps["A"]=["B"]
    assert out["dependencies"] == deps, report
    assert set(saved) == set(deps), report
    # A failed-to-resume node remains a saved partial result, not a falsely
    # pending author that could be admitted by an unrelated later invocation.
    assert saved["A"]["status"] == first_state["A"]["status"] == "partial", report
    assert saved["A"]["result"] == first_state["A"]["result"], report
    assert saved["A"].get("result_history") == first_state["A"].get("result_history"), report
    assert {key: row["dependencies"] for key, row in saved.items()} == deps, report
    assert not any(event.get("t") == "task_reopened" and event.get("key") == "A"
                   for event in case.teams.load(team).get("events", [])), report
