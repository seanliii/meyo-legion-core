"""Return facts through the real Handler, SSE ledger and owned session storage.

Only the harness execution/prelude is stubbed. The existing offline fixture
forbids sockets, service threads and subprocesses; all data/identities are synthetic.
"""
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace

import pytest

from test_artifact_preview_capability import offline, request
from test_project_artifact_export import case, OWNER, SESSION


KEY, TEAM = "__ut_surface_member", "__ut_surface_team"
FACT_FIELDS = ("pilot", "persistent_rounds", "persistent_diagnostics", "persistent_capped")


@pytest.fixture
def single(offline):
    env = offline
    core = env.server.LEGION
    pilot = importlib.import_module("legion_pilot")
    ctx = SimpleNamespace(
        env=env, core=core, pilot=pilot, calls=[], verdicts=[], config={"enabled": True},
        results=[{"key": KEY, "harness": KEY, "status": "done", "final": "好"}],
        verdict_override=None,
    )
    env.mp.setattr(core, "_build_prelude", lambda *a, **kw: "")
    env.mp.setattr(core, "_build_runtime_brief", lambda *a, **kw: {})
    env.mp.setattr(core, "_request_cancelled", lambda *a, **kw: False)
    env.mp.setattr(pilot, "load_user_config", lambda *a, **kw: pilot.merge_config(ctx.config))
    env.mp.setattr(pilot, "read_shared_goals", lambda: {
        "prime": "synthetic-private-standing-goal",
        "owner_proxy": ["synthetic-private-owner-direction"],
    })
    evaluate = pilot.evaluate

    def tracked_evaluate(*args, **kwargs):
        verdict = (deepcopy(ctx.verdict_override) if ctx.verdict_override is not None
                   else evaluate(*args, **kwargs))
        ctx.verdicts.append(deepcopy(verdict))
        return verdict

    def run_one(key, task, **kwargs):
        ctx.calls.append({"key": key, "task": task, **kwargs})
        return deepcopy(ctx.results[min(len(ctx.calls) - 1, len(ctx.results) - 1)])

    env.mp.setattr(pilot, "evaluate", tracked_evaluate)
    env.mp.setattr(core, "run_one", run_one)
    return ctx


def run(single, **overrides):
    payload = {
        "key": KEY, "task": "回一个字：好", "uid": OWNER,
        "team_id": TEAM, "session_id": SESSION,
        "project_context": {"repo_root": str(single.env.work)},
        **overrides,
    }
    before = deepcopy(payload)
    response = request(single.env, "POST", "/api/legion/run_one", body=payload)
    assert payload == before
    assert response.status == 200, response.body
    events = []
    for packet in response.raw.decode().replace("\r\n", "\n").split("\n\n"):
        lines = dict(line.split(": ", 1) for line in packet.splitlines() if ": " in line)
        if "event" in lines and "data" in lines:
            events.append((lines["event"], json.loads(lines["data"])))
    assert not [data for kind, data in events if kind == "error"], events
    final, = [data for kind, data in events if kind == "final"]
    assert set(final) == {"node_id", "result"}, "keep the single API envelope"
    assert final["node_id"] == KEY
    saved = request(single.env, "GET", "/api/legion/session?sid=" + SESSION)
    assert saved.status == 200, saved.body
    turn, = saved.body["turns"]  # completed result replaces the running placeholder
    assert saved.body["user_id"] == OWNER and turn["team_id"] == TEAM
    return final["result"], turn


@pytest.mark.parametrize("final", ["好", "2"])
def test_normal_single_return_keeps_real_verdict_in_sse_ledger_and_session(single, final):
    single.results[0]["final"] = final
    original = deepcopy(single.results)
    result, turn = run(single)
    assert len(single.calls) == len(single.verdicts) == 1
    verdict = single.verdicts[0]
    expected = {"round": 1, **{field: verdict[field] for field in ("action", "kind", "reason")}}
    assert expected["action"] == "stop" and expected["kind"] == "turn_returned"
    assert result.get("pilot") == expected, "normal Handler stop currently drops the existing verdict"
    assert turn.get("pilot") == expected
    assert turn["results"][0]["pilot"] == expected
    assert result["persistent_rounds"] == turn.get("persistent_rounds") == 1
    assert result["status"] == turn["status"] == "done"
    assert result["outcome"]["status"] == turn["outcome"]["status"] == "completed"
    assert result["final"] == turn["results"][0]["final"] == final
    assert single.results == original
    records = single.env.server.TEAMS.read_events(TEAM)
    recorded, = [row["data"] for row in records if row["event"] == "final"]
    assert recorded["result"]["pilot"] == expected
    assert "shared_goals" not in result["pilot"] and "owner_proxy" not in result["pilot"]
    assert "synthetic-private" not in json.dumps(result["pilot"])
    denied = request(single.env, "GET", "/api/legion/session?sid=" + SESSION,
                     token="fixture-other-personal-token")
    assert denied.status in (403, 404)


def test_normal_stop_only_publishes_bounded_short_verdict_fields(single):
    single.verdict_override = {
        "action": "stop", "kind": "turn_returned", "reason": "事实<&> " * 300,
        "shared_goals": {"prime": "must-not-be-published"},
        "owner_proxy": {"text": "must-not-be-published"}, "next_action": "not-a-dispatch",
    }
    before = deepcopy(single.verdict_override)
    result, turn = run(single)
    public = result.get("pilot", {})
    assert set(public) == {"round", "action", "kind", "reason"}
    assert public["reason"] == before["reason"][:500]
    assert turn["pilot"] == public and len(single.calls) == 1
    assert single.verdict_override == before


@pytest.mark.parametrize("quick,enabled", [(True, True), (False, False)])
def test_uncategorized_quick_and_disabled_single_results_do_not_invent_pilot(single, quick, enabled):
    single.config["enabled"] = enabled
    result, turn = run(single, execution_depth="quick" if quick else "standard")
    assert len(single.calls) == 1 and not single.verdicts
    assert "pilot" not in result and "pilot" not in turn
    assert turn["status"] == "done" and result["final"] == "好"


def test_existing_cancel_stop_is_preserved_without_another_evaluation(single):
    single.results[0].update(status="cancelled", final="", cancelled=True)
    result, turn = run(single)
    assert len(single.calls) == 1 and not single.verdicts
    assert result["pilot"]["kind"] == "user_cancelled"
    assert turn.get("pilot") == result["pilot"]
    assert result["outcome"]["status"] == turn["outcome"]["status"] == "cancelled"
    assert turn["status"] == "cancelled"


def test_continue_verdict_is_not_attached_to_a_later_unevaluated_failure(single):
    single.results = [
        {"key": KEY, "status": "done", "final": "first",
         "orchestration_request": {"coordinate": {"action": "continue", "reason": "original intent"}}},
        {"key": KEY, "status": "error", "final": "", "error": "synthetic terminal",
         "error_envelope": {"retryable": False, "category": "synthetic_terminal"}},
    ]
    result, turn = run(single)
    assert len(single.calls) == 2 and len(single.verdicts) == 1
    assert single.verdicts[0]["action"] == "continue"
    assert "pilot" not in result and "pilot" not in turn
    assert result["persistent_rounds"] == turn.get("persistent_rounds") == 2
    assert result["outcome"]["status"] == "failed" and turn["status"] == "error"


def test_continue_then_return_saves_only_the_final_round_stop(single):
    single.results = [
        {"key": KEY, "status": "done", "final": "first",
         "orchestration_request": {"coordinate": {"action": "continue", "reason": "original intent"}}},
        {"key": KEY, "status": "done", "final": "2"},
    ]
    result, turn = run(single)
    assert len(single.calls) == len(single.verdicts) == 2
    assert [v["action"] for v in single.verdicts] == ["continue", "stop"]
    assert result.get("pilot", {}).get("round") == 2
    assert result["pilot"]["kind"] == "turn_returned"
    assert turn["pilot"] == result["pilot"] and turn["persistent_rounds"] == 2


def test_single_missing_terminal_returns_public_facts_to_same_author(single):
    from test_native_final_diagnostics import absent_message_result

    single.results = [
        absent_message_result(key=KEY, harness=KEY),
        {"key": KEY, "harness": KEY, "status": "done", "final": "2"},
    ]
    result, turn = run(single, model="fixture-original-model", native_session_scope="session")
    assert len(single.calls) == 2
    first, second = single.calls
    assert first["key"] == second["key"] == KEY
    for field in ("qhash", "conversation_id", "preferred_model", "preferred_appid"):
        assert second[field] == first[field]
    assert second["preferred_model"] == "fixture-original-model"
    assert second["task_facts"]["native_session_scope"] == first["task_facts"]["native_session_scope"]
    assert "task_complete" in second["task"] and "检查仍在进行。" in second["task"]
    assert "不是本轮最终答复" in second["task"]
    assert result["pilot"]["kind"] == "turn_returned" and result["final"] == "2"
    assert turn["persistent_rounds"] == 2


@pytest.mark.parametrize("override", [
    {"native_checkpoint": None},
    {"checkpoint_writes": {}},
    {"checkpoint_storage_error": "fixture storage unavailable"},
    {"native_resume_error": {"category": "fixture native unavailable"}},
])
def test_single_missing_terminal_without_cursor_stops_without_changing_result(single, override):
    from test_native_final_diagnostics import absent_message_result

    single.results[0] = absent_message_result(key=KEY, harness=KEY, **override)
    result, turn = run(single)
    assert len(single.calls) == 1
    assert result["status"] == "partial" and result["final"] == ""
    assert result["native_facts"] == single.results[0]["native_facts"]
    assert result["pilot"]["kind"] == "native_terminal_message_unrecoverable"
    assert result["pilot"]["action"] == "stop"
    assert turn["persistent_rounds"] == 1
    assert result["outcome"]["status"] != "completed"


def test_single_existing_stop_verdict_precedes_terminal_recovery(single):
    from test_native_final_diagnostics import absent_message_result

    single.results[0] = absent_message_result(key=KEY, harness=KEY, native_checkpoint=None)
    single.verdict_override = {"action": "stop", "kind": "waiting", "reason": "fixture explicit wait"}
    result, _ = run(single)
    assert len(single.calls) == 1
    assert result["pilot"]["kind"] == "waiting"


def test_single_save_keeps_existing_persistent_facts_without_recomputing_them(single):
    diagnostics = [{"round": 1, "scope_goal_met": None, "scope_goal_met_source": "not_declared"}]
    single.results[0].update(persistent_diagnostics=diagnostics, persistent_capped=False)
    result, turn = run(single)
    for field in FACT_FIELDS:
        assert field in turn and turn[field] == result[field]
    assert turn["persistent_diagnostics"] == diagnostics


def test_dispatch_session_retains_optional_request_facts_and_never_promotes_a_member(single):
    env = single.env
    payload = {
        "team_id": TEAM, "results": [{"key": "a", "status": "done", "final": "好"},
                                    {"key": "b", "status": "done", "final": "2"}],
        "outcome": {"status": "completed"},
        "pilot": {"round": 2, "action": "stop", "kind": "turn_returned", "reason": "actual stop"},
        "persistent_rounds": 2, "persistent_capped": False,
        "persistent_diagnostics": [{"round": 2, "scope_goal_met": None,
                                    "scope_goal_met_source": "not_declared"}],
    }
    before = deepcopy(payload)
    env.sessions.save_turn(SESSION, "fixture", payload, mode="orchestrate", user_id=OWNER)
    saved = request(env, "GET", "/api/legion/session?sid=" + SESSION)
    assert saved.status == 200
    turn, = saved.body["turns"]
    for field in FACT_FIELDS:
        assert field in turn and turn[field] == payload[field]
    assert payload == before and turn["status"] == "done"
    legacy = {key: value for key, value in payload.items() if key not in FACT_FIELDS}
    legacy["results"] = [{"status": "done", "final": "old", "pilot": payload["pilot"]}]
    old_before = deepcopy(legacy)
    env.sessions.save_turn("legacy-surface", "older", legacy, user_id=OWNER)
    disk_before = (env.root / "sessions/legacy-surface.json").read_bytes()
    old = env.sessions.load_session("legacy-surface", user_id=OWNER)["turns"][0]
    assert all(field not in old for field in FACT_FIELDS)
    assert old["results"][0]["pilot"] == payload["pilot"] and old["status"] == "done"
    assert legacy == old_before
    assert (env.root / "sessions/legacy-surface.json").read_bytes() == disk_before
