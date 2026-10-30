"""CPU continuation regression, promoted from the independent relay probe.

Real persistent -> dispatch -> run_one -> task_memory/team_state. Only native
execution, coordinator replies and catalog/authorization data are synthetic.
The shared fixtures deny network/process creation and isolate runtime stores.
"""
from collections import Counter
from copy import deepcopy
import json

import pytest

import legion_core as core
import model_registry as mr

# Capture before the shared fixtures substitute the catalog selectors.
REAL_SELECT = core.select_usable_model
REAL_ASSIGN = mr.resolve_assignments
from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, USER, CONVERSATION
from test_native_terminal_dispatch_graph import (
    graph, _node, _plan, _returned, _rows, ORIGINAL, RELAY, QUESTION,
)

M1, M2, M3 = "__ut_resume_M1", "__ut_resume_M2", "__ut_resume_M3"
MODELS = [M1, M2, M3]


@pytest.fixture
def unbound(graph):
    case, mp = graph, graph.monkeypatch
    mp.setenv("LEGION_CODEX_TRANSPORT", "auto")
    for name in ("LEGION_CODEX_RESPONSES_BINDING", "PRIVATE_PROVIDER_KEY",
                 "MEYO_UNIFIED_APPID", "AIGC_APPID"):
        mp.delenv(name, raising=False)
    assert core._explicit_provider_binding(RELAY) is None
    case.credentials = {m: ["synthetic-token-" + m] for m in MODELS}
    case.compatible = set(MODELS)
    # Two real automatic selectors see different rankings. Do not stub the
    # selectors to return the answer this regression expects.
    mp.setattr(mr, "_SCENE_MODELS", {
        "code": {"strong": [M2, M1, M3], "value": [], "cheap": []}})
    mp.setattr(mr, "_load_prefs", lambda: {})
    mp.setattr(mr, "_load_health", lambda: {})
    mp.setattr(mr, "recommended_for", lambda key: [])
    mp.setattr(mr, "list_models", lambda *a, **kw: {
        "candidates": [{"model": m, "usable": True} for m in MODELS]})
    mp.setattr(mr, "_compatible_with_harness", lambda key, model: model in case.compatible)
    mp.setattr(mr, "_appids_of", lambda model: list(case.credentials.get(model, [])))
    mp.setattr(mr, "_pick_appid", lambda key, model: next(iter(case.credentials.get(model, [])), None))
    mp.setattr(mr, "_resolve_alias", lambda alias: alias)
    mp.setattr(core, "_load_model_router", lambda: {
        "programming": {"candidates": [{"model": m} for m in MODELS]}})
    mp.setattr(core, "_load_model_health", lambda: {})
    mp.setattr(core, "_appids_for_model_full", lambda model, *a: list(case.credentials.get(model, [])))
    mp.setattr(core, "_appid_for_model", lambda model: next(iter(case.credentials.get(model, [])), None))
    mp.setattr(core, "_appid_in_cooldown", lambda appid: False)
    mp.setattr(core, "_APPID_CURSOR", iter(range(100)))
    case.assignment_calls = []

    def assignments(plan, keys):
        result = REAL_ASSIGN(plan, keys)
        case.assignment_calls.append({
            "overrides": deepcopy(plan.get("model_overrides")),
            "chosen": {k: {f: v.get(f) for f in ("model", "source")}
                       for k, v in result.items()},
        })
        return result

    mp.setattr(mr, "resolve_assignments", assignments)
    mp.setattr(core, "select_usable_model", REAL_SELECT)
    native = core._run_core_inprocess

    def observe_appid(key, task, qhash, model_override=None, appid_override=None, **kwargs):
        result = native(key, task, qhash, model_override, appid_override, **kwargs)
        case.native_calls[-1]["appid"] = appid_override
        return result

    mp.setattr(core, "_run_core_inprocess", observe_appid)
    return case


def exercise(case, scope="conversation", selection="automatic", *,
             coordinated=False, checkpoint=True, after_first=None):
    counts = Counter()

    def answer(call):
        counts[call["key"]] += 1
        if call["key"] == ORIGINAL:
            return {
                "status": "error", "final": "", "error": "synthetic native process failure",
                "workdir": str(case.work),
                "native_facts": {"process_returncode": 1, "turn_status": "failed"},
                "error_envelope": {"category": "process", "scope": "task", "retryable": True,
                                   "message": "synthetic native process failure"},
            }
        first = counts[RELAY] == 1
        result = _returned(case, "__ut_resume_relay_uuid",
                           absent=not coordinated and first, checkpoint=checkpoint or not first)
        if first and after_first:
            after_first()
        return result

    case.answer = answer

    def pick(failed, tried, valid, *args):
        case.picks.append({"failed": failed, "tried": sorted(tried)})
        return RELAY if failed == ORIGINAL and RELAY not in tried else None

    case.monkeypatch.setattr(core, "_pick_relay_harness", pick)
    node = _node("Continue the synthetic original files", owner=ORIGINAL, scope=scope)
    if selection == "node-model":
        node["model"] = M3  # Not the model_registry override interface.
    plan = _plan(case, {"A": node}, failover=2)
    if selection in ("override-M1", "override-M3"):
        plan["model_overrides"] = {RELAY: M1 if selection == "override-M1" else M3}
    elif selection == "user-fixed":
        case.monkeypatch.setattr(mr, "_load_prefs", lambda: {RELAY: M3})
    elif selection == "provider":
        case.monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "responses")
        case.monkeypatch.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps({
            "model": "fixture-model", "base_url": "https://models.example.invalid/v1",
            "api_key_env": "PRIVATE_PROVIDER_KEY",
        }))
        case.monkeypatch.setenv("PRIVATE_PROVIDER_KEY", "private-fixture-key")
    if coordinated:
        plan["captain"] = RELAY
        plan["team_policy"]["continuous_coordination"] = True

        def coordinate(*args, **kwargs):
            if counts[RELAY] == 1:
                return {"ok": True, "action": "continue", "reason": "synthetic existing author continuation",
                        "task_updates": {"A": {
                            "task": "Continue the same files in the existing native cursor",
                            "assigned_harness": RELAY, "assignment_locked": True}}}
            return {"ok": True, "action": "complete", "reason": "synthetic complete"}

        case.monkeypatch.setattr(core, "_run_captain_coordination", coordinate)
    team = "__ut_resume_" + scope + "_" + selection
    out = core.dispatch_persistent(
        QUESTION, plan, qhash=team, mode="orchestrate" if coordinated else "pk",
        user_id=USER, conversation_id=CONVERSATION, max_rounds=2, cb=case.events.append)
    first_row = _rows(case.rounds[0]["out"])["A"]
    saved = case.teams.load(team)["tasks"]["A"]
    assert [c["key"] for c in case.native_calls] == [ORIGINAL, RELAY, RELAY], out
    assert len(case.rounds) == 2 and not case.empty_plans
    assert case.picks == [{"failed": ORIGINAL, "tried": [ORIGINAL]}]
    first, resumed = [c for c in case.native_calls if c["key"] == RELAY]
    assert first["session_id"] == resumed["session_id"]
    assert first_row["assigned_harness"] == saved["assigned_harness"] == RELAY
    assert saved["attempt_seq"] == 2 and _rows(out)["A"]["status"] == "done"
    assert first["scope"] == resumed["scope"] == scope
    if checkpoint:
        assert resumed["checkpoint"]["session_id"] == "__ut_resume_relay_uuid"
        assert resumed["memory_workdir"] == resumed["project_workdir"] == str(case.work)
        assert case.tm.load(first["memory_id"], RELAY)["native_checkpoint"]["session_id"] == "__ut_resume_relay_uuid"
    else:
        assert not resumed["checkpoint"]
    if scope == "request":
        assert first["origin"] == resumed["origin"]
    # Private credentials must not accompany stored model/checkpoint facts.
    public = json.dumps([plan, case.rounds, out, saved], ensure_ascii=False)
    for token in [*(a for values in case.credentials.values() for a in values),
                  "private-fixture-key"]:
        assert token not in public
    return first, resumed, first_row


@pytest.mark.parametrize("scope,selection", [
    ("conversation", "automatic"), ("request", "automatic"),
    ("conversation", "override-M1"), ("conversation", "override-M3"),
    ("conversation", "user-fixed"), ("conversation", "provider"),
    ("conversation", "node-model"),
])
def test_relay_continuation_preserves_actual_model_without_overriding_explicit_choice(unbound, scope, selection):
    first, resumed, row = exercise(unbound, scope, selection)
    expected = ("fixture-model" if selection == "provider"
                else M3 if selection in ("override-M3", "user-fixed") else M1)
    assert first["model"] == row["model_used"] == resumed["model"] == expected


def test_existing_captain_continue_preserves_actual_relay_model(unbound):
    first, resumed, _ = exercise(unbound, coordinated=True)
    assert resumed["model"] == first["model"] == M1


def test_new_user_fixed_choice_has_priority_over_the_previous_native_model(unbound):
    first, resumed, _ = exercise(
        unbound, coordinated=True,
        after_first=lambda: unbound.monkeypatch.setattr(mr, "_load_prefs", lambda: {RELAY: M3}))
    assert first["model"] == M1 and resumed["model"] == M3


def test_resumed_model_resolves_current_appid_instead_of_carrying_other_model_key(unbound):
    fresh = "synthetic-newly-authorized-M1"
    first, resumed, _ = exercise(
        unbound, after_first=lambda: unbound.credentials.update({M1: [fresh]}))
    assert first["appid"] != fresh
    assert resumed["model"] == M1
    assert resumed["appid"] == fresh
    assert resumed["appid"] not in unbound.credentials[M2]


@pytest.mark.parametrize("change", ["missing-checkpoint", "incompatible", "no-authorization"])
def test_unreliable_or_unavailable_historical_model_does_not_pin_automatic_choice(unbound, change):
    def alter():
        if change == "incompatible":
            unbound.compatible.remove(M1)
        elif change == "no-authorization":
            unbound.credentials[M1] = []

    first, resumed, _ = exercise(
        unbound, coordinated=True, checkpoint=change != "missing-checkpoint", after_first=alter)
    assert first["model"] == M1 and resumed["model"] == M2
    assert resumed["appid"] in unbound.credentials[M2]


@pytest.mark.parametrize("scope", ["conversation", "request"])
def test_same_harness_nodes_keep_their_own_actual_checkpoint_model_and_appid(unbound, scope):
    case, counts = unbound, Counter()
    expected = {"A": M1, "B": M3}

    def answer(call):
        node = call["task_id"]
        counts[node] += 1
        # The native harness may report a model different from the initial
        # automatic assignment. model_used, not the old plan, is authoritative.
        return {**_returned(case, "__ut_resume_node_" + node, absent=counts[node] == 1),
                "model_used": expected[node]}

    case.answer = answer
    plan = _plan(case, {k: _node("Synthetic node " + k, scope=scope) for k in expected})
    out = core.dispatch_persistent(
        QUESTION, plan, qhash="__ut_resume_nodes_" + scope, mode="pk",
        user_id=USER, conversation_id=CONVERSATION, max_rounds=2, cb=case.events.append)
    assert counts == {"A": 2, "B": 2} and not case.picks and not case.empty_plans
    assert all(row["status"] == "done" for row in out["results"])
    for node, model in expected.items():
        first, resumed = [c for c in case.native_calls if c["task_id"] == node]
        assert first["model"] == M2
        assert resumed["checkpoint"]["session_id"] == "__ut_resume_node_" + node
        assert resumed["model"] == model
        assert resumed["appid"] in case.credentials[model]
        assert first["session_id"] == resumed["session_id"]
    auto = [c["chosen"][RELAY]["model"] for c in case.assignment_calls
            if c["chosen"].get(RELAY, {}).get("source") == "task_auto"]
    assert auto and set(auto) == {M2}, "never mutate a shared harness assignment"
    stored = json.dumps([plan, case.rounds, out], ensure_ascii=False)
    assert "synthetic-token-" not in stored


@pytest.mark.parametrize("missing_fact", [
    "owner", "model", "write-confirmation", "cursor-match", "workspace-type",
    "conversation-identity", "checkpoint-kind", "scope-match",
])
def test_unreliable_saved_facts_cannot_supply_a_model(unbound, missing_fact):
    first, _, row = exercise(unbound)
    previous = deepcopy(row)
    memory = deepcopy(unbound.tm.load(first["memory_id"], RELAY))
    if missing_fact == "owner":
        previous["assigned_harness"] = ORIGINAL
    elif missing_fact == "model":
        previous.pop("model_used")
    elif missing_fact == "write-confirmation":
        previous["checkpoint_writes"]["request"] = False
    elif missing_fact == "cursor-match":
        memory["native_checkpoint"]["session_id"] = "__ut_other_native"
    elif missing_fact == "workspace-type":
        previous["workdir"] = {"not": "a path"}
    elif missing_fact == "conversation-identity":
        previous.pop("native_conversation_id", None)
        memory.pop("native_conversation_id", None)
    elif missing_fact == "checkpoint-kind":
        previous["native_checkpoint"].pop("kind", None)
        memory["native_checkpoint"].pop("kind", None)
    else:
        previous["native_session_scope"] = "request"
    assert core._native_continuation_assignment(RELAY, previous, memory, "programming") == {}
