"""Partial native output uses the same authenticated exporter as completed output."""
from copy import deepcopy
import os
import hashlib

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, TEAM, TASK, USER, CONVERSATION


@pytest.mark.parametrize("outside", [False, True])
def test_reported_old_draft_requires_real_project_export(scene, outside):
    path = (scene.root / "other-project.txt") if outside else (scene.work / "draft.txt")
    path.write_text("a pre-existing draft")
    os.utime(path, (1, 1))
    reference = {"name": path.name, "path": str(path), "kind": "text", "bytes": path.stat().st_size}
    original = scene.core._run_core_inprocess

    def partial(*args, **kwargs):
        result = original(*args, **kwargs)
        result.update(status="partial", final="A saved draft exists", error="", artifacts=[reference])
        result["native_facts"].update(completed=False, turn_status="incomplete")
        return result

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", partial)
    plan = deepcopy(scene.plan)
    plan["team_policy"] = {"adaptive": False, "max_failover": 0}
    result = scene.core.dispatch(
        "retain the native partial draft", plan, qhash=TEAM, mode="single",
        user_id=USER, conversation_id=CONVERSATION, cb=scene.events.append)
    row, = result["results"]
    assert row["status"] != "done"
    assert not scene.core._is_delivered(row)
    assert row.get("reported_artifacts"), "the native reference must remain an observation"
    if outside:
        assert not row.get("artifacts"), "a reported path is not an export grant"
        assert row["artifact_export"]["exported_files"] == 0
        assert any(n["reason"] == "artifact_path_outside_project"
                   for n in row["artifact_safety_observations"])
    else:
        exported = [a for a in row.get("artifacts", []) if a.get("name") == "draft.txt"]
        assert len(exported) == 1
        assert exported[0].get("export_snapshot_id"), "must use the real authorized exporter"
        assert scene.export_calls[0]["conversation_id"] == CONVERSATION
        assert scene.export_calls[0]["exported_files"] == 1


@pytest.mark.parametrize("outside", [False, True])
def test_failed_native_reported_source_reference_is_rechecked(scene, outside):
    path = (scene.root / "other-project.txt") if outside else (scene.work / "reported-draft.txt")
    path.write_text("a real pre-existing source file")
    os.utime(path, (1, 1))
    original = scene.core._run_core_inprocess
    envelope = {"category": "process", "scope": "harness", "retryable": False,
                "code": "native-failed", "message": "fixture failure", "tool": "", "details": {}}
    def failed(*args, **kwargs):
        result = original(*args, **kwargs)
        result.update(status="error", final="", error="fixture failure", error_envelope=envelope,
                      artifacts=[{"name": path.name, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}])
        result["native_facts"].update(process_returncode=1, turn_status="failed")
        return result
    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", failed)
    scene.monkeypatch.setattr(scene.core, "_dynamic_max_attempts", lambda *_: 1)
    result = scene.core.run_one(
        TASK, "retain the reported source file", qhash="__ut_failed_reported_" + str(outside),
        user_id=USER, conversation_id=CONVERSATION + ":task:" + TASK,
        task_facts={"team_id": TEAM, "task_id": TASK, "project_context": scene.pc.snapshot(TEAM, TASK)})
    assert result["status"] == "error" and result["error_envelope"] == envelope
    assert path.read_text() == "a real pre-existing source file"
    if outside:
        assert not result.get("artifacts")
        assert any(row["reason"] == "artifact_path_outside_project"
                   for row in result["artifact_safety_observations"])
    else:
        selected = [row for row in result.get("artifacts") or [] if row["name"] == path.name]
        assert len(selected) == 1 and selected[0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert selected[0]["export_snapshot_id"]
        assert {row["name"] for row in result["artifacts"]} == {path.name}


def test_failed_native_never_exports_explicitly_private_report(scene):
    path = scene.work / "private-draft.txt"
    path.write_text("fixture private file")
    original = scene.core._run_core_inprocess
    def failed(*args, **kwargs):
        result = original(*args, **kwargs)
        result.update(status="error", final="", error="fixture failure",
                      error_envelope={"category": "process", "scope": "harness", "retryable": False},
                      artifacts=[{"path": str(path), "name": path.name, "visibility": "private"}])
        result["native_facts"].update(process_returncode=1, turn_status="failed")
        return result
    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", failed)
    scene.monkeypatch.setattr(scene.core, "_dynamic_max_attempts", lambda *_: 1)
    result = scene.core.run_one(
        TASK, "do not expose private files", qhash="__ut_failed_private_artifact", user_id=USER,
        conversation_id=CONVERSATION + ":task:" + TASK,
        task_facts={"team_id": TEAM, "task_id": TASK, "project_context": scene.pc.snapshot(TEAM, TASK)})
    assert result["status"] == "error" and path.exists()
    assert not any(row.get("name") == path.name for row in result.get("artifacts") or [])
    assert not any(row.get("name") == path.name for row in result.get("reported_artifacts") or [])


@pytest.mark.parametrize("private_kind", ["internal", "marked", "symlink", "sensitive"])
def test_partial_mixed_selection_never_exports_private_references(scene, private_kind):
    """A project under state/ may export a draft, not its internal/private neighbors."""
    public = scene.work / "draft.txt"
    public.write_text("a public partial draft")
    if private_kind == "internal":
        private = scene.work / "state" / "turn.txt"
        private.parent.mkdir()
        private.write_text("an internal session")
        reference = {"path": str(private), "name": private.name}
    elif private_kind == "marked":
        private = scene.work / "private.txt"
        private.write_text("explicitly private")
        reference = {"path": str(private), "name": private.name, "visibility": "private"}
    elif private_kind == "symlink":
        outside = scene.root / "outside.txt"
        outside.write_text("outside the project")
        private = scene.work / "link.txt"
        private.symlink_to(outside)
        reference = {"path": str(private), "name": private.name}
    else:
        private = scene.work / "credentials.txt"
        private.write_text('api_key="live-fixture-credential-7a93c81f56dd"')
        reference = {"path": str(private), "name": private.name}
    original = scene.core._run_core_inprocess

    def partial(*args, **kwargs):
        result = original(*args, **kwargs)
        result.update(status="partial", final="A saved draft exists", error="",
                      artifacts=[{"path": str(public), "name": public.name}, reference])
        result["native_facts"].update(completed=False, turn_status="incomplete")
        return result

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", partial)
    result = scene.core.run_one(
        TASK, "preserve a partial draft", qhash="__ut_mixed_" + private_kind,
        user_id=USER, conversation_id=CONVERSATION + ":task:" + TASK,
        task_facts={"team_id": TEAM, "task_id": TASK, "project_context": scene.pc.snapshot(TEAM, TASK)})
    assert result["status"] != "done" and not result.get("artifacts")
    assert any(note["reason"] == "artifact_private_reference"
               for note in result["artifact_safety_observations"])
    assert not any(item.get("name") == private.name for item in result.get("reported_artifacts") or [])
    assert public.read_text() == "a public partial draft"


def test_relative_or_missing_project_root_does_not_relax_private_classification(scene):
    path = scene.work / "draft.txt"
    path.write_text("a draft")
    item = {"path": str(path), "name": path.name}
    assert scene.ad._is_private_artifact(item)
    assert scene.ad._is_private_artifact(item, project_root="state/outputs/original-work")
    assert scene.ad._is_private_artifact(item, project_root="/")
    assert not scene.ad._is_private_artifact(item, project_root=str(scene.work))


@pytest.mark.parametrize("name", ["state.json", "x.progress.json", "x.sidecar.json", ".config/x.json", "logs"])
def test_project_relative_internal_names_never_enter_partial_observations(scene, name):
    path = scene.work / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("internal state")
    assert scene.ad._is_private_artifact({"path": str(path)}, project_root=str(scene.work))
    assert scene.ad._is_private_artifact({"path": name}, project_root=str(scene.work))


def test_partial_relative_reference_is_bound_to_project_before_classification(scene):
    path = scene.work / "draft.txt"
    path.write_text("a relative draft")
    original = scene.core._run_core_inprocess

    def partial(*args, **kwargs):
        result = original(*args, **kwargs)
        result.update(status="partial", final="A saved draft exists", error="",
                      artifacts=[{"path": "draft.txt", "name": "draft.txt"}])
        result["native_facts"].update(completed=False, turn_status="incomplete")
        return result

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", partial)
    result = scene.core.run_one(
        TASK, "preserve a relative draft", qhash="__ut_relative_partial", user_id=USER,
        conversation_id=CONVERSATION + ":task:" + TASK,
        task_facts={"team_id": TEAM, "task_id": TASK, "project_context": scene.pc.snapshot(TEAM, TASK)})
    assert result["status"] != "done"
    selected, = result.get("artifacts") or []
    assert selected["name"] == path.name and selected["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert selected["export_snapshot_id"]
    for encoded in ("%73tate/turn.txt", "..%2f..%2foutside.txt", "../outside.txt"):
        assert scene.ad._is_private_artifact({"path": encoded}, project_root=str(scene.work))


def test_partial_export_exception_keeps_native_draft_and_diagnostic(scene):
    path = scene.work / "draft.txt"
    path.write_text("retain this draft")
    original = scene.core._run_core_inprocess

    def partial(*args, **kwargs):
        result = original(*args, **kwargs)
        result.update(status="partial", final="A saved draft exists", error="",
                      artifacts=[{"path": str(path), "name": path.name}])
        result["native_facts"].update(completed=False, turn_status="incomplete")
        return result

    def failed_export(*args, **kwargs):
        raise RuntimeError("controlled export storage failure")

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", partial)
    scene.monkeypatch.setattr(scene.ad, "export_project_artifacts", failed_export)
    plan = deepcopy(scene.plan)
    plan["team_policy"] = {"adaptive": False, "max_failover": 0}
    output = scene.core.dispatch("preserve a partial draft", plan, qhash=TEAM, mode="single",
                                 user_id=USER, conversation_id=CONVERSATION)
    row, = output["results"]
    assert row["status"] != "done"
    assert row["final"] == "A saved draft exists" and path.read_text() == "retain this draft"
    assert row["artifact_export"]["exported_files"] == 0
    note, = row["artifact_safety_observations"]
    assert note["exception_type"] == "RuntimeError"
    assert note["diagnostic"] == "controlled export storage failure"


def test_bound_partial_without_selected_references_cannot_fall_back_to_scan(scene):
    original = scene.core._run_core_inprocess

    def partial(*args, **kwargs):
        result = original(*args, **kwargs)
        (scene.work / "unselected.txt").write_text("not selected by the native producer")
        result.update(status="partial", final="A saved draft exists", error="", artifacts=[])
        result["native_facts"].update(completed=False, turn_status="incomplete")
        return result

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", partial)
    plan = deepcopy(scene.plan)
    plan["team_policy"] = {"adaptive": False, "max_failover": 0}
    output = scene.core.dispatch("retain partial work", plan, qhash=TEAM, mode="single",
                                 user_id=USER, conversation_id=CONVERSATION)
    row, = output["results"]
    assert row["status"] != "done" and not row.get("artifacts")
    assert row["artifact_export"]["exported_files"] == 0
    assert any(n["reason"] == "artifact_selection_required"
               for n in row["artifact_safety_observations"])
    assert (scene.work / "unselected.txt").exists()
