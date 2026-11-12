"""HTTP feedback model provenance, without a listener or a model process.

Exercise the real message handler, resume admission, dispatch/persistent,
run_one and saved native state. Only HTTP transport identity, the background
wakeup and native/coordinator replies are CPU substitutes.
"""
from copy import deepcopy
import importlib
import json

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, USER, CONVERSATION
from test_native_terminal_dispatch_graph import graph, _node, _plan, _returned, RELAY, QUESTION
from test_native_model_continuation_cpu import unbound, M1, M2, M3


@pytest.mark.parametrize("selection,order,receipt_change", [
    ("automatic", ("A", "B"), None),
    ("automatic", ("B", "A"), None),
    ("request", ("A", "B"), None),
    ("fixed", ("A", "B"), None),
    ("provider", ("A", "B"), None),
    ("automatic", ("A",), None),
    ("request", ("A",), None),
    ("fixed", ("A",), None),
    ("provider", ("A",), None),
    ("automatic", ("A",), "cancelled"),
    ("provider", ("A",), "cancelled"),
    *[("automatic", ("A", "B"), change) for change in (
        "missing", "wrong-team", "wrong-owner", "wrong-checkpoint",
        "wrong-model", "duplicate-node", "missing-workdir")],
])
def test_http_feedback_preserves_node_models_and_explicit_choices(
        unbound, selection, order, receipt_change):
    case, core, mp = unbound, unbound.core, unbound.monkeypatch
    mp.setattr(core, "_CANCEL_REGISTRY", {})
    server = importlib.import_module("server")
    persistent = len(order) > 1
    original_models = {key: {"A": M1, "B": M3}[key] for key in order}
    if selection == "provider":
        mp.setenv("LEGION_CODEX_TRANSPORT", "responses")
        mp.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps({
            "model": "fixture-model", "base_url": "https://models.example.invalid/v1",
            "api_key_env": "PRIVATE_PROVIDER_KEY",
        }))
        mp.setenv("PRIVATE_PROVIDER_KEY", "private-fixture-key")
        original_models = dict.fromkeys(order, "fixture-model")
    case.answer = lambda call: {
        **_returned(case, "__ut_feedback_native_" + call["task_id"]),
        "model_used": original_models[call["task_id"]],
    }
    parent = "__ut_feedback_models_" + selection + "_" + "".join(order)
    plan = _plan(case, {key: _node("Original synthetic node " + key,
                                  scope="conversation" if receipt_change == "cancelled" else None)
                       for key in order})
    plan["captain"] = RELAY if persistent else ""
    original = core.dispatch(
        QUESTION, plan, qhash=parent, mode="orchestrate" if persistent else "relay", user_id=USER,
        conversation_id=CONVERSATION)
    assert len(case.native_calls) == len(order)
    first_calls = {call["task_id"]: deepcopy(call) for call in case.native_calls}
    state = case.teams.load(parent)
    assert {key: row["result"]["model_used"] for key, row in state["tasks"].items()} == original_models
    # The saved parent's own coordination policy and explicit selection, not
    # fields smuggled through the incoming feedback body.
    state["captain"] = RELAY if persistent else ""
    state["orchestration_spec"]["pattern"] = "orchestrate" if persistent else "single"
    state["orchestration_spec"]["team_policy"]["continuous_coordination"] = True
    if selection == "request":
        state["orchestration_spec"]["model_overrides"] = {RELAY: M2}
    elif selection == "fixed":
        mp.setattr(case.mr, "_load_prefs", lambda: {RELAY: M2})
    case.teams._write(state)
    if receipt_change == "cancelled":
        case.teams.request_cancel(parent, reason="original maintenance Stop")
        core.request_cancel(parent, reason="original maintenance Stop")
        state = case.teams.load(parent)
        state["tasks"]["A"]["status"] = "cancelled"
        state["tasks"]["A"]["result"]["status"] = "cancelled"
        case.teams._write(state)
    original_tasks = deepcopy(state["tasks"])
    receipt = deepcopy(original)
    source_a = next(row for row in receipt["results"] if row.get("task_id") == "A")
    if receipt_change == "cancelled":
        source_a["status"] = "cancelled"
    if receipt_change == "missing":
        receipt["results"] = []
    elif receipt_change == "wrong-team":
        receipt["team_id"] = "__ut_unrelated_feedback_team"
    elif receipt_change == "wrong-owner":
        source_a["assigned_harness"] = "__ut_unrelated_feedback_author"
    elif receipt_change == "wrong-checkpoint":
        source_a["native_checkpoint"]["session_id"] = "__ut_unrelated_feedback_cursor"
    elif receipt_change == "wrong-model":
        source_a["model_used"] = M3
    elif receipt_change == "duplicate-node":
        receipt["results"].append(deepcopy(source_a))
    elif receipt_change == "missing-workdir":
        source_a.pop("workdir", None)
    server.SESSIONS.save_turn(
        CONVERSATION, QUESTION, receipt, mode="orchestrate", user_id=USER)
    case.native_calls.clear()
    case.rounds.clear()
    case.assignment_calls.clear()
    # After the saved completed nodes are replayed as context, the original
    # coordinator explicitly revises them. The scheduler itself is real.
    controls = []

    def coordinate(key, question, **kwargs):
        controls.append(kwargs["qhash"])
        proposal = {"action": "complete", "reason": "Synthetic revision returned"}
        if not case.native_calls:
            proposal = {
                "action": "continue", "reason": "Synthetic original author revision",
                "task_updates": {node: {"task": "Revise synthetic node " + node}
                                 for node in order},
            }
        return {"status": "done", "final": "Synthetic coordination response",
                "orchestration_request": {"coordinate": proposal}}

    mp.setattr(core, "_run_one_with_gateway", coordinate)
    case.answer = lambda call: _returned(case, "__ut_feedback_native_" + call["task_id"])
    resumes, invocations = [], []
    real_persistent = core.dispatch_persistent

    def observe_persistent(*args, **kwargs):
        invocations.append(deepcopy({k: v for k, v in kwargs.items() if k != "cb"}))
        return real_persistent(*args, **kwargs)

    mp.setattr(core, "dispatch_persistent", observe_persistent)

    def wake(team_id, user_id, conversation_id):
        resumes.append(server._resume_saved_team(
            team_id, explicit=False, user_id=user_id, conversation_id=conversation_id))

    mp.setattr(server, "_schedule_feedback_resume", wake)
    body = {"team_id": parent, "recipient": "commander",
            "text": "Synthetic feedback for the existing nodes",
            "request_id": "__ut_feedback_models", "continue_execution": True}
    response = []
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/api/legion/team/message"
    handler._read_json_body = lambda: deepcopy(body)
    handler._personal_user = lambda: USER
    handler._send_json = lambda value, status=200: response.append((status, deepcopy(value)))
    # No socket/server: enter the real HTTP branch after transport identity.
    handler._do_POST_inner()
    assert response[0][0] == 201 and len(resumes) == 1, response
    assert resumes[0].get("result"), resumes
    assert len(invocations) == 1 and "max_rounds" not in invocations[0]
    assert len(case.native_calls) == len(order) and not case.picks, resumes
    assert bool(controls) == persistent, "single feedback must not create a coordinator"
    resumed = {call["task_id"]: call for call in case.native_calls}
    observed = {key: row["model"] for key, row in resumed.items()}
    expected = (dict.fromkeys(order, M2) if selection in ("request", "fixed")
                else dict(original_models))
    if receipt_change in ("missing", "wrong-team"):
        expected = dict.fromkeys(order, M2)
    elif receipt_change and receipt_change != "cancelled":
        expected["A"] = M2
    diagnostic = {
        "selection": selection, "order": order, "receipt_change": receipt_change,
        "original_models": original_models,
        "feedback_overrides": invocations[0]["plan"].get("model_overrides"),
        "assignments": case.assignment_calls, "resumed_models": observed,
    }
    print("HTTP_FEEDBACK_MODEL_OBSERVATION", json.dumps(diagnostic, ensure_ascii=False))
    assert observed == expected, diagnostic
    for node in order:
        assert resumed[node]["checkpoint"]["session_id"] == "__ut_feedback_native_" + node
        assert resumed[node]["session_id"] == first_calls[node]["session_id"]
        assert resumed[node]["memory_workdir"] == str(case.work)
        assert resumed[node]["provider"] == first_calls[node]["provider"]
    if receipt_change == "cancelled":
        assert core.is_cancelled(parent) and not core.is_cancelled(resumes[0]["team_id"])
        assert invocations[0]["plan"]["tasks"]["A"]["native_session_scope"] == "conversation"
    assert case.teams.load(parent)["tasks"] == original_tasks, "do not erase the old model facts"
    expected_overrides = {RELAY: M2} if selection == "request" else {}
    if not persistent and selection == "automatic":
        expected_overrides = {RELAY: M1}  # Preserve the existing single-author fallback.
    assert invocations[0]["plan"].get("model_overrides", {}) == expected_overrides
    public = json.dumps([resumes, case.teams.load(resumes[0]["team_id"])], ensure_ascii=False)
    assert "synthetic-token-" not in public and "private-fixture-key" not in public
