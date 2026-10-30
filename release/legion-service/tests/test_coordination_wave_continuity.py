"""Offline real-dispatch regressions: facts survive a failed control turn."""
from contextlib import nullcontext
from copy import deepcopy
import importlib
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
TEAM = "__ut_coordination_continuity"
OWNER = "fixture-owner"
CONVERSATION = "fixture-conversation"
HARNESS = "__ut_member"


@pytest.fixture
def case(tmp_path, monkeypatch):
    for name, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_TASK_MEMORY_DIR", "memory"),
        ("LEGION_TEAM_DIR", "teams"), ("LEGION_SESSIONS_DIR", "sessions"),
        ("LEGION_USER_CONFIG_DIR", "users"), ("LEGION_ARTIFACT_STORE_DIR", "artifacts"),
        ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        (tmp_path / leaf).mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(tmp_path / leaf))
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        raise AssertionError("real model/network/process access forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    core = importlib.import_module("legion_core")
    pc = importlib.import_module("project_context")
    tm = importlib.import_module("task_memory")
    artifacts = importlib.import_module("artifact_store")
    # These three roots are import-time constants; pin at test execution time.
    for module, leaf in ((pc, "projects"), (tm, "memory"), (artifacts, "artifacts")):
        monkeypatch.setattr(module, "ROOT", tmp_path / leaf)
    monkeypatch.setattr(core, "_harness_keys", lambda: {HARNESS})
    monkeypatch.setattr(core, "_aigc_ready", lambda **kw: {"runnable_clis": [HARNESS]})
    monkeypatch.setattr(core, "_build_prelude", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_build_runtime_brief", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_read_async_deep_plans", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_captain_mail_context", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_baseline_snapshot", lambda *a, **kw: None)
    class Lease(nullcontext):
        wait_seconds = 0

    monkeypatch.setattr(core, "_GatewayLease", lambda *a, **kw: Lease())
    monkeypatch.setattr(core, "run_one", forbidden)
    monkeypatch.setattr(core, "_run_one_with_gateway", forbidden)
    plan = {
        "captain": HARNESS, "team_policy": {"continuous_coordination": True},
        "tasks": {
            task: {"task": task + " original work", "assigned_harness": HARNESS,
                   "assignment_locked": True, "depends_on": [prior] if prior else []}
            for task, prior in (("A", ""), ("B", "A"), ("C", "B"))
        },
        "project_context": {"repo_root": str(tmp_path / "work")},
    }
    (tmp_path / "work").mkdir()
    return SimpleNamespace(core=core, pc=pc, tm=tm, mp=monkeypatch, root=tmp_path, plan=plan)


def initialize(case):
    return case.pc.initialize(TEAM, "original objective", case.plan, mode="orchestrate",
                              user_id=OWNER, conversation_id=CONVERSATION)


def dispatch(case, **kwargs):
    return case.core.dispatch(
        "original objective", deepcopy(case.plan), qhash=TEAM, mode="orchestrate",
        user_id=OWNER, conversation_id=CONVERSATION, **kwargs)


def install_native(case, *, cancel_after_failure=False, pause=False):
    members, controls = [], []

    def member(key, prompt, **kwargs):
        facts = deepcopy(kwargs["task_facts"])
        members.append(facts)
        return {"status": "done", "final": facts["task_id"] + " actual result"}

    def control(key, prompt, **kwargs):
        controls.append({"prompt": prompt, **deepcopy(kwargs)})
        if len(controls) == 1:
            if cancel_after_failure:
                case.mp.setattr(case.core, "is_cancelled", lambda *a, **kw: True)
            result = {"status": "timeout", "final": "", "error": "fixture control timeout"}
            if pause:
                result["error_envelope"] = {
                    "category": "authorization_required", "scope": "provider",
                    "code": "appid_disabled", "retryable": False, "message": "fixture denial",
                }
            return result
        return {"status": "done", "final": "use the new returned facts",
                "orchestration_request": {"coordinate": {
                    "action": "continue", "reason": "B supplied new evidence",
                    "task_updates": {"C": {"task": "C revised by captain after B"}},
                }}}

    case.mp.setattr(case.core, "run_one", member)
    case.mp.setattr(case.core, "_run_one_with_gateway", control)
    return members, controls


@pytest.mark.parametrize("check", ["next_member", "next_coordination", "resolved_history"])
def test_failed_control_does_not_lose_facts_or_disable_next_new_wave(case, check):
    members, controls = install_native(case)
    out = dispatch(case)
    assert [row["task_id"] for row in members] == ["A", "B", "C"]
    assert [row["status"] for row in out["results"]] == ["done"] * 3
    if check == "next_member":
        decisions = members[1]["project_context"]["decisions"]
        assert any(row.get("error") == "captain_execution_failed" for row in decisions)
        assert members[1]["role"] == "B original work", "failure must not rewrite the graph"
    elif check == "next_coordination":
        assert len(controls) == 2, "B is new evidence, not a retry of the A control turn"
        latest = controls[1]["task_facts"]["project_context"]
        assert {row["task_id"] for row in latest["completed"]} == {"A", "B"}
        assert latest["decisions"][-1]["error"] == "captain_execution_failed"
        assert "PREVIOUS_COORDINATION_EXECUTION_JSON" in controls[1]["prompt"]
        assert members[2]["project_context"]["decisions"][-1]["sequence"] == 2
        assert members[2]["role"] == "C revised by captain after B"
    else:
        assert not out.get("coordination_incomplete"), "superseded failure stays history, not veto"
        assert [row["ok"] for row in out["coordination_history"]] == [False, True]
        assert [row["sequence"] for row in case.pc.load(TEAM)["decisions"]] == [1, 2]


def test_existing_sequence_continues_without_reusing_old_request_slot(case):
    initialize(case)
    case.pc.record_coordination(TEAM, HARNESS, {
        "sequence": 7, "ok": False, "error": "captain_execution_failed", "action": None})
    members, controls = install_native(case)
    dispatch(case)
    assert controls[0]["qhash"] == TEAM + "_coordinate_8"
    assert controls[1]["qhash"] == TEAM + "_coordinate_9"


def test_sequence_uses_saved_record_not_truncated_model_view(case):
    initialize(case)
    saved = case.pc.record_coordination(TEAM, HARNESS, {
        "sequence": 7, "ok": False, "error": "captain_execution_failed", "action": None})
    saved["decisions"].extend({"kind": "other_fixture_observation"} for _ in range(41))
    case.pc._atomic_write(case.pc._path(TEAM), saved)
    _, controls = install_native(case)
    dispatch(case)
    assert controls[0]["qhash"] == TEAM + "_coordinate_8"


@pytest.mark.parametrize("prior_sequence", [0, 7])
def test_round_complete_does_not_reuse_wave_or_previous_request_slots(case, prior_sequence):
    initialize(case)
    if prior_sequence:
        case.pc.record_coordination(TEAM, HARNESS, {
            "sequence": prior_sequence, "ok": False,
            "error": "captain_execution_failed", "action": None})
    members, _ = install_native(case)
    controls = []

    def control(key, prompt, **kwargs):
        controls.append(kwargs["qhash"])
        target = {1: "B", 2: "C"}.get(len(controls))
        return {"status": "done", "final": "observed current results",
                "orchestration_request": {"coordinate": {
                    "action": "continue" if target else "complete",
                    "reason": "current results consumed",
                    "task_updates": {
                        target: {"task": target + " original work"}} if target else {},
                }}}

    case.mp.setattr(case.core, "_run_one_with_gateway", control)
    pilot = importlib.import_module("legion_pilot")
    case.mp.setattr(pilot, "evaluate", lambda *a, **kw: {"action": "stop", "kind": "done"})
    case.core.dispatch_persistent(
        "original objective", deepcopy(case.plan), qhash=TEAM, mode="orchestrate",
        user_id=OWNER, conversation_id=CONVERSATION, max_rounds=1)
    assert [row["task_id"] for row in members] == ["A", "B", "C"]
    assert controls == [
        f"{TEAM}_coordinate_{prior_sequence + offset}" for offset in (1, 2, 3)]


@pytest.mark.parametrize("stop", ["cancel", "provider_pause", "retained_wave"])
def test_new_control_opportunity_preserves_existing_stop_and_reuse_boundaries(case, stop):
    members, controls = install_native(
        case, cancel_after_failure=stop == "cancel", pause=stop == "provider_pause")
    kwargs = {}
    if stop == "retained_wave":
        kwargs["_retained_results"] = {
            "B": {"key": "B", "task_id": "B", "status": "done", "final": "already done",
                  "assigned_harness": HARNESS}}
    dispatch(case, **kwargs)
    assert len(controls) == 1
    if stop == "retained_wave":
        assert [row["task_id"] for row in members] == ["A", "C"]


def coordinate(case, native_result):
    initialize(case)
    case.mp.setattr(case.core, "_run_one_with_gateway",
                    lambda *a, **kw: deepcopy(native_result))
    case.core._run_captain_coordination(
        "original objective", case.plan, TEAM,
        {"A": {"task_id": "A", "status": "done", "final": "A actual result"}},
        ["B", "C"], phase="wave_0_complete", sequence=1,
        user_id=OWNER, conversation_id=CONVERSATION)
    return case.pc.load(TEAM)["decisions"][-1]["execution_result_ref"]


@pytest.mark.parametrize("stored", ["absent", "previous_only", "different_checkpoint", "wrong_identity"])
def test_missing_current_checkpoint_never_creates_a_fictitious_reference(case, stored):
    checkpoint = {"kind": "fixture", "session_id": "original-native", "provider_binding": "opaque-fixture"}
    prior_id = f"conv:{HARNESS}:{CONVERSATION}:captain-control"
    before = case.tm.update(prior_id, HARNESS, native_checkpoint=checkpoint,
                            native_conversation_id=CONVERSATION + ":captain-control")
    result = {"status": "timeout", "final": "", "error": "fixture timeout"}
    request_id = TEAM + "_coordinate_1"
    if stored in ("different_checkpoint", "wrong_identity"):
        result["native_checkpoint"] = checkpoint
        case.tm.update(
            request_id, HARNESS,
            native_checkpoint={**checkpoint, "session_id": "different-native"}
            if stored == "different_checkpoint" else checkpoint,
            native_conversation_id="other-conversation" if stored == "wrong_identity"
            else CONVERSATION + ":captain-control")
    reference = coordinate(case, result)
    assert reference.get("checkpoint_status") == "missing_current"
    assert not reference.get("task_memory_ref"), "a path is not proof that its field exists"
    assert not reference.get("project_context_ref"), "no commander artifact was saved"
    assert not reference.get("resumed_native")
    assert not reference.get("native_checkpoint"), "old/mismatched anchors are not current evidence"
    assert case.tm.load(prior_id, HARNESS) == before, "no empty checkpoint may overwrite the old slot"


def test_saved_current_checkpoint_and_artifact_references_read_back(case):
    checkpoint = {"kind": "fixture", "session_id": "current-native", "provider_binding": "opaque-fixture"}
    request_id = TEAM + "_coordinate_1"
    case.tm.update(request_id, HARNESS, native_checkpoint=checkpoint,
                   native_conversation_id=CONVERSATION + ":captain-control")
    path = case.root / "work" / "control-note.txt"
    path.write_text("actual local bytes")
    reference = coordinate(case, {
        "status": "timeout", "final": "", "native_checkpoint": checkpoint,
        "artifacts": [{"path": str(path), "kind": "text"}]})
    memory_ref = reference["task_memory_ref"]
    stored = json.loads(Path(memory_ref["path"]).read_text())[memory_ref["field"]]
    assert stored == checkpoint
    project_ref = reference["project_context_ref"]
    rows = json.loads(Path(project_ref["path"]).read_text())[project_ref["field"]]
    assert any(row["task_id"] == "commander" and row["path"] == str(path) for row in rows)
    assert "opaque-fixture" not in json.dumps(reference), "do not export binding digests"


def test_artifact_storage_failure_does_not_publish_dangling_project_reference(case):
    def cannot_save(*args, **kwargs):
        raise OSError("fixture storage failure")

    case.mp.setattr(case.pc, "record_progress_artifact", cannot_save)
    reference = coordinate(case, {
        "status": "timeout", "final": "", "artifacts": [{"path": "/fixture/unsaved.txt"}]})
    assert not reference.get("project_context_ref")
    assert reference.get("artifact_storage_error") == "OSError"
