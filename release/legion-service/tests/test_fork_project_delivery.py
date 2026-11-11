"""Fork delivery through real state/export; only native CPU leaves are synthetic."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, USER, CONVERSATION
from test_native_terminal_dispatch_graph import (
    graph, ORIGINAL, WRITER, REPAIR, _node, _plan, _returned,
)


@pytest.fixture
def fork_case(graph):
    case = graph
    case.team = "__ut_fork_project_delivery"
    case.parent_text = "Original author draft: preserve these independent input bytes."
    case.input_file = case.work / "parent-input.txt"
    case.input_file.write_bytes(b"original parent input\n")
    case.inputs_before = case.input_file.read_bytes()
    case.specs = [{"task_id": "support", "task": "Write an independent support file",
                   "suggest_key": WRITER}]
    case.plan = _plan(case, {"A": _node("Original objective", owner=ORIGINAL)})
    case.plan["initial_author"] = {"task_id": "A", "harness": ORIGINAL}
    case.plan["team_policy"]["candidate_harnesses"] = [ORIGINAL, WRITER, REPAIR]
    case.monkeypatch.setattr(case.core, "_CANCEL_REGISTRY", {})
    case.seen, case.parent_before = {}, {}
    native = case.core._run_core_inprocess

    def observe(key, prompt, *args, **kwargs):
        task_id = kwargs["task_facts"]["task_id"]
        case.seen[task_id] = {
            "prompt": prompt, "project": deepcopy(kwargs["project_context"]),
            "canonical": case.pc.load(case.team),
        }
        if task_id != "A":
            case.parent_before.setdefault("task", deepcopy(case.teams.load(case.team)["tasks"]["A"]))
        return native(key, prompt, *args, **kwargs)

    case.monkeypatch.setattr(case.core, "_run_core_inprocess", observe)

    def answer(call):
        result = _returned(case, "__ut_fork_native_" + call["task_id"])
        if call["task_id"] == "A":
            return {**result, "final": case.parent_text,
                    "orchestration_request": {"fork": deepcopy(case.specs)}}
        target = case.work / (call["task_id"] + ".txt")
        target.write_bytes(("independent " + call["task_id"] + "\n").encode())
        return {**result, "final": "Independent contribution: " + str(target)}

    case.answer = answer
    return case


def run(case, callback=None):
    out = case.core.dispatch(
        "Original objective", deepcopy(case.plan), qhash=case.team, mode="single",
        user_id=USER, conversation_id=CONVERSATION, cb=callback)
    return {row["task_id"]: row for row in out["results"]}


@pytest.mark.parametrize("parallel", [1, 2])
def test_fork_binds_before_real_export_and_preserves_parent_and_newer_facts(fork_case, parallel):
    case = fork_case
    if parallel == 2:
        case.specs.append({"task_id": "second", "task": "Write second support file",
                           "suggest_key": REPAIR})
    case.plan["team_policy"]["max_parallel"] = parallel
    grant_before = case.identity.project_grant(USER, str(case.work))

    def fresh_fact(event):
        if event.get("fork", {}).get("children"):
            case.pc.record_coordination(case.team, ORIGINAL, {
                "sequence": 37, "action": "continue", "reason": "new fact after parent finished"})

    rows = run(case, fresh_fact)
    assert rows["A"]["final"] == case.parent_text
    assert case.input_file.read_bytes() == case.inputs_before
    assert case.identity.project_grant(USER, str(case.work)) == grant_before
    parent_saved = case.teams.load(case.team)["tasks"]["A"]
    parent_before = case.parent_before["task"]
    for field in ("assigned_harness", "attempt_seq", "active_attempt"):
        assert parent_saved[field] == parent_before[field]
    for field in ("final", "artifacts", "native_checkpoint"):
        assert parent_saved["result"][field] == parent_before["result"][field]
    for spec in case.specs:
        task_id, owner = spec["task_id"], spec["suggest_key"]
        seen, result = case.seen[task_id], rows[task_id]
        nodes = {row["task_id"]: row for row in seen["canonical"]["tasks"]}
        assert nodes[task_id]["assigned_harness"] == owner
        assert nodes[task_id]["depends_on"] == ["A"]
        assert nodes["A"]["assigned_harness"] == ORIGINAL
        assert case.parent_text in seen["prompt"]
        assert seen["project"]["repo_root"] == str(case.work)
        assert seen["project"]["user_id"] == USER
        assert seen["project"]["conversation_id"] == CONVERSATION
        assert seen["project"]["decisions"][-1]["sequence"] == 37
        assert result["status"] == "done" and not result["artifact_safety_observations"]
        artifact, = [a for a in result["artifacts"] if a["name"] == task_id + ".txt"]
        expected = (case.work / (task_id + ".txt")).read_bytes()
        assert Path(artifact["path"]).read_bytes() == expected
        assert artifact["sha256"] == hashlib.sha256(expected).hexdigest()
        assert artifact["export_snapshot_id"]
        stored = case.teams.load(case.team)["tasks"][task_id]["result"]
        assert stored["artifacts"] == result["artifacts"]
        message, = [m for m in case.teams.read_messages(case.team, "A", mark_read=False)
                    if m.get("idempotency_key") == task_id]
        assert artifact["sha256"] in {a.get("sha256") for a in message["artifact_refs"]}


@pytest.mark.parametrize("boundary", [
    "project_owner", "project_conversation", "project_team", "project_parent_assignment",
    "project_child_collision", "team_owner", "team_parent_assignment", "team_child_assignment",
    "project_write_failure",
])
def test_fork_refusal_does_not_execute_or_overwrite_project_or_parent(fork_case, boundary):
    case = fork_case
    before = {}

    def disturb(event):
        if not event.get("fork", {}).get("children"):
            return
        packet = case.pc.load(case.team)
        if boundary == "project_owner":
            packet["user_id"] = "another-owner"
        elif boundary == "project_conversation":
            packet["conversation_id"] = "another-conversation"
        elif boundary == "project_team":
            packet["team_id"] = "another-team"
        elif boundary == "project_parent_assignment":
            next(r for r in packet["tasks"] if r["task_id"] == "A")["assigned_harness"] = REPAIR
        elif boundary == "project_child_collision":
            packet["tasks"].append({"task_id": "support", "assigned_harness": REPAIR,
                                     "role": "Existing independent project task", "depends_on": []})
        if boundary.startswith("project_") and boundary != "project_write_failure":
            case.pc._atomic_write(case.pc._path(case.team), packet)
        if boundary.startswith("team_"):
            state = case.teams.load(case.team)
            if boundary == "team_owner":
                state["user_id"] = "another-owner"
            else:
                state["tasks"]["A" if boundary == "team_parent_assignment" else "support"]["assigned_harness"] = REPAIR
            case.teams._write(state)
        if boundary == "project_write_failure":
            real_bind = case.pc.record_task_bindings

            def refuse(team_id, tasks, **kwargs):
                if "support" in tasks:
                    raise OSError("fixture project write unavailable")
                return real_bind(team_id, tasks, **kwargs)

            case.monkeypatch.setattr(case.pc, "record_task_bindings", refuse)
        before["project"] = case.pc.load(case.team)
        before["parent"] = deepcopy(case.teams.load(case.team)["tasks"]["A"])

    rows = run(case, disturb)
    assert [call["task_id"] for call in case.native_calls] == ["A"]
    child = rows["support"]
    assert child["status"] == "blocked" and child["blocked_before_native"] is True
    assert child["execution_calls"] == 0 and child["error"]
    assert rows["A"]["fork_results"][0]["error"] == child["error"]
    assert not (case.work / "support.txt").exists()
    assert case.pc.load(case.team) == before["project"]
    assert case.teams.load(case.team)["tasks"]["A"] == before["parent"]
    assert case.teams.read_messages(case.team, "A", mark_read=False) == []
    assert rows["A"]["final"] == case.parent_text
    assert case.input_file.read_bytes() == case.inputs_before


@pytest.mark.parametrize("grant_change", ["revoked", "narrowed"])
def test_fork_binding_does_not_supply_or_expand_export_grants(fork_case, grant_change):
    case = fork_case
    identity_file = case.root / "operator-control/identities.json"
    native_answer = case.answer

    def answer(call):
        if call["task_id"] == "support":
            if grant_change == "revoked":
                identity_file.write_text("{}")
            else:
                records = json.loads(identity_file.read_text())
                for record in records.values():
                    record["project_grants"][0]["export_paths"] = ["assets"]
                identity_file.write_text(json.dumps(records))
        return native_answer(call)

    case.answer = answer
    rows = run(case)
    assert [call["task_id"] for call in case.native_calls] == ["A", "support"]
    assert (case.work / "support.txt").exists()
    child = rows["support"]
    if grant_change == "revoked":
        assert child["artifacts"] == []
        assert any(n["reason"] == "artifact_project_not_authorized"
                   for n in child["artifact_safety_observations"])
        assert json.loads(identity_file.read_text()) == {}
    else:
        assert {a["name"] for a in child["artifacts"]} == {"style.css"}
        assert case.identity.project_grant(USER, str(case.work))["export_paths"] == ["assets"]
    assert rows["A"]["final"] == case.parent_text
    assert case.input_file.read_bytes() == case.inputs_before
    assert not any(a["name"] == "support.txt" for a in rows["A"]["artifacts"])


@pytest.mark.parametrize("target", ["A", "support"])
@pytest.mark.parametrize("store", ["runtime", "durable"])
def test_cancel_before_fork_binding_keeps_parent_and_does_not_start_child(fork_case, target, store):
    case = fork_case

    def cancel(event):
        if event.get("fork", {}).get("children"):
            service = case.core if store == "runtime" else case.teams
            service.request_cancel(case.team, target, scope="task")

    rows = run(case, cancel)
    assert [call["task_id"] for call in case.native_calls] == ["A"]
    assert rows["support"]["status"] == "cancelled"
    assert rows["support"]["execution_calls"] == 0
    assert "support" not in {row["task_id"] for row in case.pc.load(case.team)["tasks"]}
    assert case.teams.load(case.team)["tasks"]["support"]["attempt_seq"] == 0
    assert rows["A"]["final"] == case.parent_text
    assert case.input_file.read_bytes() == case.inputs_before


def test_cancel_after_binding_is_seen_before_native_execution(fork_case):
    case = fork_case
    bind = case.pc.record_task_bindings

    def cancel_after_binding(team_id, tasks, **kwargs):
        packet = bind(team_id, tasks, **kwargs)
        if "support" in tasks:
            case.core.request_cancel(case.team, "A", scope="task")
        return packet

    case.monkeypatch.setattr(case.pc, "record_task_bindings", cancel_after_binding)
    rows = run(case)
    assert [call["task_id"] for call in case.native_calls] == ["A"]
    assert rows["support"]["status"] == "cancelled"
    assert not (case.work / "support.txt").exists()
    assert rows["A"]["final"] == case.parent_text
    assert case.input_file.read_bytes() == case.inputs_before
