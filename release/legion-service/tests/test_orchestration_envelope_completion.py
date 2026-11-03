"""Only framing may be incomplete; task values must be complete, strict JSON."""
import hashlib
import json

import pytest

import legion_core as _legion_core  # noqa: F401  # 注册真实 harness_registry 源码路径
from collaboration_protocol_fixtures import (  # noqa: F401
    HARNESS, OWNER, SID, TEAM, captain_scene, done, isolated, plan, scene,
)


def marked(payload):
    return "成员原摘要\n<<LEGION_ORCH>>" + payload + "<<END>>"


def proposal():
    return {"action": "continue", "task_updates": {}, "messages": [],
            "next_plan": {"tasks": {
                "next": {"task": '原文里的 }、{、"引号" 与 \\ 都不得改写', "depends_on": []},
            }}}


def test_complete_coordinate_value_survives_one_missing_outer_object_end(isolated):
    import harness_registry as registry

    expected = proposal()
    payload = json.dumps({"coordinate": expected}, ensure_ascii=False)[:-1]
    original = marked(payload)
    cleaned, result = registry._parse_orch_request(original)
    assert result and result["coordinate"] == expected
    assert cleaned == "成员原摘要"
    assert original == marked(payload)
    assert result["_protocol_recovery"] == {
        "kind": "complete_coordinate_in_unclosed_outer_object",
        "payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "value_inferred": False,
    }


@pytest.mark.parametrize("payload", [
    '{"coordinate":{"action":"continue"',
    '{"coordinate":{"action":"continue","reason":"unfinished',
    '{"coordinate":{"action":"continue","messages":[',
    '{"coordinate":{"action":"continue",}}',
    '{"coordinate":{"action":"continue"},"other":42',
    '{"coordinate":{"action":"complete"},"coordinate":{"action":"continue"}',
    '{"coordinate":[]',
    '{"fork":[{"task":"must remain rejected"}]',
    '{"need_approval":{"capability":"git_push"}',
])
def test_never_infer_values_other_fields_or_permission_commands(isolated, payload):
    import harness_registry as registry

    _, result = registry._parse_orch_request(marked(payload))
    assert result is None


def test_missing_end_marker_and_multiple_markers_cannot_trigger_recovery(isolated):
    import harness_registry as registry

    payload = json.dumps({"coordinate": proposal()})[:-1]
    original = "<<LEGION_ORCH>>" + payload
    assert registry._parse_orch_request(original) == (original, None)
    _, result = registry._parse_orch_request(marked(payload) + marked('{"coordinate":{}}'))
    assert result is None
    _, result = registry._parse_orch_request(marked(payload) + "后面还有未处理内容")
    assert result is None


def test_valid_protocol_and_absent_protocol_remain_unchanged(isolated):
    import harness_registry as registry

    expected = {"coordinate": proposal(), "need_approval": {"capability": "git_push"}}
    cleaned, result = registry._parse_orch_request(marked(json.dumps(expected)))
    assert cleaned == "成员原摘要" and result == expected
    assert registry._parse_orch_request("原来的普通答复") == ("原来的普通答复", None)


def test_non_json_whitespace_is_not_misreported_as_only_an_unclosed_frame(isolated):
    import harness_registry as registry

    payload = '{"coordinate":\u00a0' + json.dumps(proposal())
    assert registry._parse_orch_request(marked(payload))[1] is None


def test_recovery_fact_reaches_coordinator_and_existing_project_record(captain_scene, monkeypatch):
    import harness_registry as registry

    s = captain_scene
    original = marked(json.dumps({"coordinate": proposal()}, ensure_ascii=False)[:-1])
    cleaned, request = registry._parse_orch_request(original)
    monkeypatch.setattr(s.core, "run_one", lambda *a, **kw: {
        "status": "done", "final": cleaned, "orchestration_request": request})
    result = s.run()
    assert result["ok"]
    assert result["protocol_recovery"] == request["_protocol_recovery"]
    assert s.pc.load(s.team)["decisions"][-1]["protocol_recovery"] == result["protocol_recovery"]


def test_recovery_survives_real_run_one_normalization_and_coordination(scene):
    import project_context

    s = scene
    spec = plan(s, {"build": []}, coordinate=True)
    project_context.initialize(
        TEAM, "原目标", spec, mode="orchestrate", user_id=OWNER, conversation_id=SID)
    s.teams.ensure(TEAM, "原目标", spec["tasks"], {"build": []},
                   captain=HARNESS, user_id=OWNER, conversation_id=SID)
    expected = proposal()
    s.replies["commander"] = [done(marked(
        json.dumps({"coordinate": expected}, ensure_ascii=False)[:-1]))]
    result = s.core._run_captain_coordination(
        "原目标", spec, TEAM, {}, [], phase="round_complete", sequence=1,
        user_id=OWNER, conversation_id=SID)
    assert result["ok"] is True
    assert result["next_plan"] == expected["next_plan"]
    assert result["protocol_recovery"]["value_inferred"] is False
    assert len(s.calls) == 1 and s.calls[0]["task_id"] == "commander"


def envelope(*operations, version=1):
    return {"protocol": "meyo.legion.collaboration", "version": version,
            "operations": list(operations)}


def test_v1_envelope_decodes_all_operations_with_runtime_identity(isolated):
    import collaboration_runtime as protocol
    import harness_registry as registry

    coordinate = proposal()
    handoff = {"to": "codex", "reason": "需要原生代码执行"}
    wire = envelope({"type": "coordinate", "payload": coordinate},
                    {"type": "handoff", "payload": handoff})
    cleaned, parsed = registry._parse_orch_request(marked(json.dumps(wire, ensure_ascii=False)))
    assert cleaned == "成员原摘要" and parsed == wire
    decoded, receipt = protocol.decode_orchestration_request(
        parsed, team_id="team-a", task_id="build", correlation_id="request-7", harness="cc")
    assert decoded == {"coordinate": coordinate, "handoff": handoff}
    assert receipt["name"] == "meyo.legion.collaboration"
    assert receipt["version"] == 1 and receipt["encoding"] == "collaboration-envelope/v1"
    assert receipt["team_id"] == "team-a" and receipt["task_id"] == "build"
    assert receipt["correlation_id"] == "request-7" and receipt["errors"] == []
    assert [row["operation"] for row in receipt["operations"]] == ["coordinate", "handoff"]
    assert len({row["operation_id"] for row in receipt["operations"]}) == 2


@pytest.mark.parametrize("wire", [
    envelope({"type": "handoff", "payload": {"to": "codex"}}, version=2),
    envelope({"type": "handoff", "payload": {"to": "codex"}},
             {"type": "unknown", "payload": {}}),
    envelope({"type": "handoff", "payload": {"to": "codex"}},
             {"type": "handoff", "payload": {"to": "hermes"}}),
])
def test_invalid_v1_envelope_never_executes_a_valid_prefix(isolated, wire):
    import collaboration_runtime as protocol

    decoded, receipt = protocol.decode_orchestration_request(wire)
    assert decoded == {}, "Envelope validation is atomic, not partial execution"
    assert receipt["errors"] and receipt["operations"] == []


def test_v1_coordinate_survives_real_run_one_and_reaches_project_receipt(scene):
    import project_context

    s = scene
    spec = plan(s, {"build": []}, coordinate=True)
    project_context.initialize(
        TEAM, "原目标", spec, mode="orchestrate", user_id=OWNER, conversation_id=SID)
    s.teams.ensure(TEAM, "原目标", spec["tasks"], {"build": []},
                   captain=HARNESS, user_id=OWNER, conversation_id=SID)
    wire = envelope({"type": "coordinate", "payload": proposal()})
    s.replies["commander"] = [done(marked(json.dumps(wire, ensure_ascii=False)))]
    result = s.core._run_captain_coordination(
        "原目标", spec, TEAM, {}, [], phase="round_complete", sequence=1,
        user_id=OWNER, conversation_id=SID)
    assert result["ok"] is True and result["next_plan"] == proposal()["next_plan"]
    receipt = result["collaboration_protocol"]
    assert receipt["encoding"] == "collaboration-envelope/v1"
    assert receipt["team_id"] == TEAM and receipt["task_id"] == "commander"
    assert receipt["correlation_id"] == f"{TEAM}:coordinate:1"
    assert receipt["harness"] == HARNESS
    persisted = project_context.load(TEAM)["decisions"][-1]["collaboration_protocol"]
    assert persisted == receipt
    assert len(s.calls) == 1 and s.calls[0]["task_id"] == "commander"
    assert all(row["operation_id"].startswith(f"{TEAM}:coordinate:1:")
               for row in receipt["operations"])


@pytest.mark.parametrize("wire", [
    envelope({"type": "need_approval", "payload": {}}),
    envelope({"type": "handoff", "payload": {}}),
    envelope({"type": "need_peer", "payload": {"key": "codex"}}),
    envelope({"type": "fork", "payload": [{}]}),
    {**envelope({"type": "handoff", "payload": {"to": "codex"}}),
     "need_approval": {"capability": "git_push"}},
])
def test_v1_rejects_unexecutable_operations_before_any_side_effect(isolated, wire):
    import collaboration_runtime as protocol

    decoded, receipt = protocol.decode_orchestration_request(wire)
    assert decoded == {} and receipt["errors"] and receipt["operations"] == []


def test_invalid_v1_worker_result_is_not_a_completed_delivery(scene):
    import project_context

    s = scene
    spec = plan(s, {"build": []})
    project_context.initialize(
        TEAM, "原目标", spec, mode="orchestrate", user_id=OWNER, conversation_id=SID)
    wire = envelope({"type": "handoff", "payload": {"to": HARNESS}}, version=2)
    s.replies["build"] = [done(marked(json.dumps(wire)))]
    out = s.core.dispatch(
        "原目标", spec, qhash=TEAM, mode="orchestrate", user_id=OWNER, conversation_id=SID)
    row = next(row for row in out["results"] if row["task_id"] == "build")
    assert row["status"] != "done"
    assert row.get("collaboration_protocol", {}).get("errors"), (row["status"], sorted(row))
    assert row["raw_orchestration_request"] == wire
    packet = project_context.load(TEAM)
    assert packet["completed"] == []
    assert packet["attempts"][-1]["collaboration_protocol"]["errors"]
    assert packet["attempts"][-1]["raw_orchestration_request"] == wire


def test_v1_coordination_rejects_extra_operations_instead_of_dropping_them(scene):
    import project_context

    s = scene
    spec = plan(s, {"build": []}, coordinate=True)
    project_context.initialize(
        TEAM, "原目标", spec, mode="orchestrate", user_id=OWNER, conversation_id=SID)
    s.teams.ensure(TEAM, "原目标", spec["tasks"], {"build": []},
                   captain=HARNESS, user_id=OWNER, conversation_id=SID)
    wire = envelope({"type": "coordinate", "payload": proposal()},
                    {"type": "handoff", "payload": {"to": HARNESS}})
    s.replies["commander"] = [done(marked(json.dumps(wire)))]
    result = s.core._run_captain_coordination(
        "原目标", spec, TEAM, {}, [], phase="round_complete", sequence=1,
        user_id=OWNER, conversation_id=SID)
    assert result["ok"] is False and result["error"] == "captain_invalid_coordinate_shape"
    assert len(s.calls) == 2
    assert any(error["code"] == "coordinate_requires_single_operation"
               for error in result["protocol_feedback"]["shape_errors"])
    assert not s.teams.read_messages(TEAM, "build", mark_read=False)


def test_v1_artifact_only_message_is_valid_and_persisted(scene):
    import project_context

    s = scene
    spec = plan(s, {"build": []}, coordinate=True)
    project_context.initialize(
        TEAM, "原目标", spec, mode="orchestrate", user_id=OWNER, conversation_id=SID)
    s.teams.ensure(TEAM, "原目标", spec["tasks"], {"build": []},
                   captain=HARNESS, user_id=OWNER, conversation_id=SID)
    value = proposal()
    value["messages"] = [{"to": "build", "artifact_refs": [{"path": "/tmp/evidence.txt"}]}]
    wire = envelope({"type": "coordinate", "payload": value})
    s.replies["commander"] = [done(marked(json.dumps(wire)))]
    result = s.core._run_captain_coordination(
        "原目标", spec, TEAM, {}, [], phase="round_complete", sequence=1,
        user_id=OWNER, conversation_id=SID)
    assert result["ok"] is True and len(result["message_receipts"]) == 1
    persisted = project_context.load(TEAM)["decisions"][-1]
    assert persisted["message_receipts"] == result["message_receipts"]
