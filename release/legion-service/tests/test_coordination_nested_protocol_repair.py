"""Nested protocol faults must reach the original member, not become guessed work."""
from copy import deepcopy
import json

import pytest

from collaboration_protocol_fixtures import (  # noqa: F401
    OWNER, SID, TEAM, captain_scene, done, isolated, plan as dispatch_plan, scene,
)


def malformed_proposal():
    return {
        "action": "continue",
        "reason": "成员自己的判断",
        "task_updates": {},
        "next_plan": {"tasks": [
            {"id": "build", "owner": "cc", "description": "成员选择的后续工作"},
        ]},
        "messages": [{"to": "build", "body": "成员原始交接事实"}],
    }


def repaired_proposal():
    return {
        "action": "continue",
        "reason": "成员自己的判断",
        "task_updates": {"build": {
            "task": "成员选择的后续工作", "assigned_harness": "cc",
        }},
        "next_plan": {},
        "messages": [{"to": "build", "text": "成员原始交接事实"}],
    }


def run_responses(scene, monkeypatch, proposals, *, native_status="done", native_raw=True):
    calls = []
    responses = iter(deepcopy(proposals))

    def native(key, prompt, **kwargs):
        proposal = next(responses)
        calls.append({"key": key, "prompt": prompt, **deepcopy(kwargs)})
        raw = "<<LEGION_ORCH>>" + json.dumps(
            {"coordinate": proposal}, ensure_ascii=False) + "<<END>>"
        return {
            "status": native_status, "final": "成员的简短协调摘要",
            "error": "" if native_status == "done" else "fixture native failure",
            "native_checkpoint": {"kind": "fixture", "session_id": "same-native-control"},
            "native_facts": {"turn_result": {"result": raw}} if native_raw else {},
            "orchestration_request": {"coordinate": proposal},
        }

    monkeypatch.setattr(scene.core, "run_one", native)
    plan = {"captain": "cc", "tasks": {"build": {"task": "原待办"}}}
    result = scene.core._run_captain_coordination(
        "保留原目标，由成员决定后续工作", plan, scene.team, {}, ["build"],
        phase="wave_0_complete", sequence=1,
        user_id=scene.owner, conversation_id=scene.sid)
    return result, calls, plan


def test_nested_fields_get_one_same_member_repair_without_inferred_mapping(
        captain_scene, monkeypatch):
    s = captain_scene
    malformed, repaired = malformed_proposal(), repaired_proposal()
    result, calls, plan = run_responses(s, monkeypatch, [malformed, repaired])

    assert len(calls) == 2, "Nested structural errors need the existing format-only correction"
    assert {call["key"] for call in calls} == {"cc"}
    assert {call["user_id"] for call in calls} == {s.owner}
    assert {call["conversation_id"] for call in calls} == {s.sid + ":captain-control"}
    assert calls[0]["qhash"] != calls[1]["qhash"]
    assert calls[1]["qhash"].endswith("_format_repair")
    assert "next_plan.tasks" in calls[1]["prompt"]
    assert "messages[0].text" in calls[1]["prompt"]
    assert json.dumps({"coordinate": malformed}, ensure_ascii=False) in calls[1]["prompt"]
    assert "不要重新研究、运行测试或修改作品" in calls[1]["prompt"]
    assert result["ok"] is True
    assert result["task_updates"] == repaired["task_updates"]
    assert result["messages"] == repaired["messages"]
    assert plan["tasks"]["build"]["task"] == "原待办", "Validation must not execute the plan"
    messages = s.ts.read_messages(s.team, "build", mark_read=False)
    assert [message["text"] for message in messages] == ["成员原始交接事实"]
    assert result["protocol_recovery"]["format_retry"]["value_inferred_by_runtime"] is False


def test_body_message_is_not_silently_dropped_when_task_update_is_valid(
        captain_scene, monkeypatch):
    s = captain_scene
    malformed = repaired_proposal()
    malformed["messages"] = [{"to": "build", "body": "成员原始交接事实"}]
    result, calls, _ = run_responses(s, monkeypatch, [malformed, repaired_proposal()])
    assert len(calls) == 2, "An otherwise applicable plan must not hide a missing message field"
    assert result["ok"] is True
    assert "messages[0].text" in calls[1]["prompt"]
    assert len(s.ts.read_messages(s.team, "build", mark_read=False)) == 1


def test_exhausted_nested_repair_stays_failed_and_never_sends_body_as_text(
        captain_scene, monkeypatch):
    s = captain_scene
    malformed = malformed_proposal()
    result, calls, plan = run_responses(s, monkeypatch, [malformed, malformed])
    assert len(calls) == 2
    assert result["ok"] is False
    assert result["error"] == "captain_invalid_coordinate_shape"
    assert result["action"] is None
    assert result["reported_action"] == "continue"
    errors = result["protocol_feedback"]["shape_errors"]
    assert {item["field"] for item in errors} >= {"next_plan.tasks", "messages[0].text"}
    assert not s.ts.read_messages(s.team, "build", mark_read=False)
    assert plan == {"captain": "cc", "tasks": {"build": {"task": "原待办"}}}
    assert all(row["ok"] is False for row in s.pc.load(s.team)["decisions"])


def test_valid_shape_does_not_add_a_model_call(captain_scene, monkeypatch):
    s = captain_scene
    expected = repaired_proposal()
    result, calls, _ = run_responses(s, monkeypatch, [expected])
    assert len(calls) == 1
    assert result["ok"] is True
    assert result["task_updates"] == expected["task_updates"]
    assert result["messages"] == expected["messages"]


def test_cleaned_final_does_not_hide_the_original_structured_proposal(
        captain_scene, monkeypatch):
    malformed = malformed_proposal()
    result, calls, _ = run_responses(
        captain_scene, monkeypatch, [malformed, repaired_proposal()], native_raw=False)
    assert result["ok"] is True and len(calls) == 2
    assert json.dumps({"coordinate": malformed}, ensure_ascii=False) in calls[1]["prompt"]
    assert '"field": "orchestration_request"' in calls[1]["prompt"]


def test_missing_outer_and_bad_inner_shape_share_one_retry_budget(captain_scene, monkeypatch):
    s = captain_scene
    result, calls, _ = run_responses(s, monkeypatch, [None, malformed_proposal()])
    assert len(calls) == 2
    assert result["ok"] is False
    assert result["error"] == "captain_invalid_coordinate_shape"
    decisions = s.pc.load(s.team)["decisions"]
    assert [row["error"] for row in decisions] == [
        "captain_missing_coordinate", "captain_invalid_coordinate_shape"]
    assert not s.ts.read_messages(s.team, "build", mark_read=False)


@pytest.mark.parametrize("native_status", ["error", "unavailable", "cancelled"])
def test_unsuccessful_native_turn_never_gets_a_shape_retry(
        captain_scene, monkeypatch, native_status):
    s = captain_scene
    result, calls, _ = run_responses(
        s, monkeypatch, [malformed_proposal()], native_status=native_status)
    assert len(calls) == 1
    assert result["ok"] is False
    if native_status == "cancelled":
        assert result["error"] == "captain_cancelled"
    else:
        assert result["result"]["status"] == native_status
        assert result["result"]["error"] == "fixture native failure"
    assert not s.ts.read_messages(s.team, "build", mark_read=False)


@pytest.mark.parametrize("wave", [False, True], ids=["round-end", "between-waves"])
def test_exhausted_shape_repair_remains_partial_in_the_real_caller(scene, monkeypatch, wave):
    import sessions
    import task_outcome

    s = scene
    s.replies["commander"] = [
        {**done(), "orchestration_request": {"coordinate": malformed_proposal()}}]
    s.artifact_tasks.add("build")
    monkeypatch.setattr(s.core, "plan_request_with_model", lambda *a, **k:
                        pytest.fail("Protocol failure must not start business replanning"))
    spec = dispatch_plan(
        s, {"build": [], **({"later": ["build"]} if wave else {})}, coordinate=True)
    out = s.core.dispatch_persistent(
        "继续原订单", plan=spec, mode="orchestrate", qhash=TEAM, max_rounds=3,
        user_id=OWNER, conversation_id=SID)
    assert s.counts["build"] == 1 and s.counts["commander"] == 2
    assert s.counts["later"] == int(wave)
    assert out["coordination_incomplete"] is True
    assert out["pilot"]["kind"] == "coordination_protocol_error"
    out = task_outcome.attach(json.loads(json.dumps(out)))
    assert out["status"] == out["outcome"]["status"] == "partial"
    sessions.save_turn(SID, "继续原订单", out, "orchestrate", OWNER)
    assert sessions.load_session(SID, user_id=OWNER)["turns"][-1]["status"] == "partial"
    assert (s.work / "build.txt").read_text() == "persisted build"


@pytest.mark.parametrize("action", ["contine", 1, None])
def test_legacy_invalid_action_retains_compatibility_coercion(captain_scene, monkeypatch, action):
    s = captain_scene
    legacy = repaired_proposal()
    legacy["action"] = action
    result, calls, plan = run_responses(s, monkeypatch, [legacy])
    assert len(calls) == 1
    assert result["ok"] is True and result["action"] == "continue"
    assert result["collaboration_protocol"]["encoding"] == "legacy-operation-map"
    assert plan["tasks"]["build"]["task"] == "原待办"
    assert s.ts.read_messages(s.team, "build", mark_read=False)


def test_coordination_message_delivers_artifact_refs_with_protocol_receipt(
        captain_scene, monkeypatch):
    s = captain_scene
    reference = s.store.put_text(s.team, "build", "可复核的原始证据", name="evidence.txt")
    original = (reference["path"], open(reference["path"], "rb").read())
    expected = repaired_proposal()
    expected["messages"] = [{"to": "build", "text": "请复核附件，不只凭这段文字",
                             "artifact_refs": [reference]}]
    result, calls, _ = run_responses(s, monkeypatch, [expected])
    assert result["ok"] is True and len(calls) == 1
    messages = s.ts.read_messages(s.team, "build", mark_read=False)
    assert len(messages) == 1 and messages[0]["artifact_refs"] == [reference]
    assert messages[0]["text"] == expected["messages"][0]["text"]
    receipt = result["message_receipts"][0]
    assert receipt["message_id"] == messages[0]["id"]
    assert receipt["artifact_ref_count"] == 1
    assert receipt["correlation_id"] == result["collaboration_protocol"]["correlation_id"]
    assert open(original[0], "rb").read() == original[1]
