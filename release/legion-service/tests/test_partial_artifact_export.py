"""Partial native output uses the same authenticated exporter as completed output."""
from copy import deepcopy
import os

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
