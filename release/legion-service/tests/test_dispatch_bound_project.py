"""任务归属随真实调度落盘；模型叶子离线，正式 exporter 保持原样。"""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, USER, CONVERSATION, TASK


@pytest.mark.parametrize("mode", ["pk", "relay", "orchestrate"])
@pytest.mark.parametrize("declaration", ["implicit", "unbound"])
def test_bound_tasks_are_persisted_before_native_execution_and_export(scene, mode, declaration):
    team = "__ut_bound_project_" + mode + "_" + declaration
    plan = {
        "team_id": team,
        "_user_selected_mode": True,
        "project_context": {"repo_root": str(scene.work)},
        "team_policy": {
            "adaptive": False, "candidate_harnesses": [TASK],
            "max_failover": 0, "contract_repair_rounds": 0,
        },
        "success_audit": {"enabled": False},
    }
    task = TASK if declaration == "implicit" else "return_existing_site"
    if declaration == "unbound":
        plan["tasks"] = {task: {"task": "Return the existing fixture site"}}
    observed = []
    original = scene.core._run_core_inprocess

    def native(key, prompt, qhash, *args, **kwargs):
        packet = scene.pc.load(team)
        observed.append({
            "key": key, "tasks": deepcopy(packet.get("tasks") or []),
            "conversation_id": packet.get("conversation_id"),
        })
        return original(key, prompt, qhash, *args, **kwargs)

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", native)
    out = scene.core.dispatch(
        "Return the existing fixture site", plan=plan, mode=mode, qhash=team,
        user_id=USER, conversation_id=CONVERSATION, cb=scene.events.append)

    assert len(observed) == 1, out
    bound = {row["task_id"]: row["assigned_harness"] for row in observed[0]["tasks"]}
    assert bound.get(task) == TASK, "实际执行前，正式项目中必须是同一个任务与成员"
    assert observed[0]["conversation_id"] == CONVERSATION
    row, = out["results"]
    assert row["status"] == "done", row
    assert {a["name"] for a in row.get("artifacts") or []} == {"index.html", "style.css"}
    for artifact in row["artifacts"]:
        data = Path(artifact["path"]).read_bytes()
        assert data == Path(artifact["provenance"]["source_path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"]


def test_binding_update_preserves_newer_project_facts_and_workspace(scene):
    team = "__ut_binding_does_not_replay_stale_context"
    packet = scene.pc.initialize(
        team, "Original objective", deepcopy(scene.plan), mode="pk",
        user_id=USER, conversation_id=CONVERSATION)
    stale = deepcopy(packet)
    current = scene.pc.record_coordination(team, TASK, {
        "sequence": 1, "action": "continue", "reason": "New fact arrived during discovery",
    })
    tasks = {"next_task": {"task": "Continue", "assigned_harness": TASK}}
    original = deepcopy(tasks)
    actual = scene.pc.record_task_bindings(
        team, tasks, user_id=USER, conversation_id=CONVERSATION)
    assert tasks == original
    assert actual["decisions"] == current["decisions"] != stale["decisions"]
    for field in ("user_id", "conversation_id", "repo_root", "resource_budget", "artifacts", "open_gaps"):
        assert actual.get(field) == current.get(field)
    assert {row["task_id"] for row in actual["tasks"]} == {TASK, "next_task"}
    assert scene.identity.project_grant(USER, str(scene.work))["export_paths"] == ["."]


def test_conditional_binding_does_not_overwrite_a_newer_assignment(scene):
    team = "__ut_conditional_project_binding"
    scene.pc.initialize(team, "Original", deepcopy(scene.plan), mode="pk",
                        user_id=USER, conversation_id=CONVERSATION)
    updated = scene.pc.record_task_bindings(
        team, {TASK: {"assigned_harness": "new-owner", "task": "Original"}},
        user_id=USER, conversation_id=CONVERSATION, expected_assigned_harnesses={TASK: TASK})
    before = deepcopy(updated)
    with pytest.raises(ValueError, match="project_task_binding_changed"):
        scene.pc.record_task_bindings(
            team, {TASK: {"assigned_harness": TASK}}, user_id=USER,
            conversation_id=CONVERSATION, expected_assigned_harnesses={TASK: "stale-owner"})
    assert scene.pc.load(team) == before
    assert next(row for row in before["tasks"] if row["task_id"] == TASK)["assigned_harness"] == "new-owner"


def test_assignment_only_cas_preserves_concurrent_task_facts(scene):
    team = "__ut_assignment_only_cas"
    scene.pc.initialize(team, "Original", deepcopy(scene.plan), mode="pk",
                        user_id=USER, conversation_id=CONVERSATION)
    newer = scene.pc.record_task_bindings(
        team, {TASK: {"task": "New task body", "assigned_harness": TASK,
                      "depends_on": ["other"], "dependency_policy": "all_done",
                      "verification_role": "independent-reviewer"}},
        user_id=USER, conversation_id=CONVERSATION)
    before = next(row for row in newer["tasks"] if row["task_id"] == TASK)
    updated = scene.pc.record_task_bindings(
        team, {TASK: {"assigned_harness": "relay", "task": "STALE TASK", "depends_on": []}},
        user_id=USER, conversation_id=CONVERSATION,
        expected_assigned_harnesses={TASK: TASK}, assignments_only=True)
    after = next(row for row in updated["tasks"] if row["task_id"] == TASK)
    assert after == {**before, "assigned_harness": "relay"}
    restored = scene.pc.record_task_bindings(
        team, {TASK: {"assigned_harness": TASK}}, user_id=USER, conversation_id=CONVERSATION,
        expected_assigned_harnesses={TASK: "relay"}, assignments_only=True)
    assert next(row for row in restored["tasks"] if row["task_id"] == TASK) == before
    with pytest.raises(ValueError, match="existing task and assigned harness required"):
        scene.pc.record_task_bindings(team, {"unknown": {"assigned_harness": TASK}},
                                      user_id=USER, conversation_id=CONVERSATION,
                                      assignments_only=True)
    assert scene.pc.load(team) == restored


@pytest.mark.parametrize("user,conversation", [
    ("another-user", CONVERSATION), (USER, "another-conversation"),
])
def test_binding_update_cannot_change_project_ownership(scene, user, conversation):
    team = "__ut_binding_owner"
    before = scene.pc.initialize(
        team, "Original objective", deepcopy(scene.plan), mode="pk",
        user_id=USER, conversation_id=CONVERSATION)
    with pytest.raises(PermissionError):
        scene.pc.record_task_bindings(
            team, {"other_task": {"assigned_harness": TASK}},
            user_id=user, conversation_id=conversation)
    assert scene.pc.load(team) == before


def test_binding_update_does_not_create_unregistered_project(scene):
    team = "__ut_binding_missing"
    with pytest.raises(LookupError, match="project_context_missing"):
        scene.pc.record_task_bindings(
            team, {TASK: {"assigned_harness": TASK}},
            user_id=USER, conversation_id=CONVERSATION)
    assert not scene.pc.load(team)
