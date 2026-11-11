"""The ordinary native execution return must reach the existing artifact channel."""
import importlib
import json
import os
from pathlib import Path

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
import test_project_export_terminal_binding as export_scene
from test_project_export_terminal_binding import TEAM, TASK, USER, LANE


@pytest.fixture
def scene(explicit_run_one):
    return export_scene.scene.__wrapped__(explicit_run_one)


def test_stop_preserves_last_tool_actions_files_and_session_after_reload(scene):
    original = scene.core._run_core_inprocess
    request_id = "__ut_stopped_game_files"

    def native(*args, **kwargs):
        result = original(*args, **kwargs)
        progress = kwargs["progress_cb"]
        progress({"t": "tool_started", "tool": "write_file", "call_id": "write-draft"})
        (scene.work / "index.html").write_text("<p>Unfinished game saved before stop</p>")
        progress({"t": "tool_end", "tool": "write_file", "call_id": "write-draft",
                  "ok": True, "status": "succeeded", "result_brief": "draft saved"})
        result.update(status="cancelled", final="", error="user stopped")
        result["native_facts"].update(process_returncode=130, turn_status="cancelled")
        # No final answer or artifact report: stop must still collect owned files.
        return result

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", native)
    result = scene.core.run_one(
        TASK, "build a game", qhash=request_id, user_id=USER, conversation_id=LANE,
        cb=scene.events.append,
        task_facts={"team_id": TEAM, "task_id": TASK,
                    "project_context": scene.pc.snapshot(TEAM, TASK)})
    assert result["status"] == "cancelled" and result["cancelled"] is True
    assert not scene.core._is_delivered(result)
    assert len(scene.native_calls) == 1, "stop must not restart the author"
    assert [(e["t"], e["call_id"]) for e in result["tool_trace"]] == [
        ("tool_started", "write-draft"), ("tool_end", "write-draft")]
    assert {a["name"] for a in result["artifacts"]} == {"index.html", "style.css"}
    draft = next(a for a in result["artifacts"] if a["name"] == "index.html")
    assert Path(draft["path"]).read_bytes() == (scene.work / "index.html").read_bytes()
    memory = importlib.import_module("task_memory").load(request_id, TASK)
    assert memory["completed_steps"] == result["tool_trace"]
    sessions = importlib.import_module("sessions")
    sid = "__ut_stopped_game_session"
    sessions.save_turn(sid, "build a game", {
        "status": "cancelled", "team_id": TEAM, "results": [result]}, user_id=USER)
    restored = sessions.load_session(sid, user_id=USER)["turns"][0]
    assert restored["status"] == "cancelled"
    assert restored["results"][0]["tool_trace"] == result["tool_trace"]
    assert restored["results"][0]["artifacts"] == result["artifacts"]


def test_export_success_does_not_erase_a_separate_export_failure(scene):
    original = scene.ad.collect_run_artifacts
    observation = scene.ad._not_exported("artifact_export_unavailable")

    def collect(**kwargs):
        facts = original(**kwargs)
        assert facts["artifacts"]  # Real authorized export, not a fabricated URL.
        facts["artifact_safety_observations"].append(observation)
        return facts

    scene.monkeypatch.setattr(scene.ad, "collect_run_artifacts", collect)
    result = scene.core.run_one(
        TASK, "retain independent export facts", qhash="__ut_export_parallel_facts",
        user_id=USER, conversation_id=LANE,
        task_facts={"team_id": TEAM, "task_id": TASK,
                    "project_context": scene.pc.snapshot(TEAM, TASK)})
    assert result["artifacts"]
    assert observation in result["artifact_safety_observations"]
    assert result["artifact_export"]["observations"] == result["artifact_safety_observations"]


@pytest.mark.parametrize("case", ["owned", "revoked", "private", "outside"])
def test_cancelled_native_rechecks_project_scope_without_changing_outcome(scene, case):
    work_file = scene.work / "index.html"
    work_file.write_text("partial output created before cancellation")
    # No hidden neighboring assets: an explicit selected path cannot become a
    # wider grant merely because it is inside the authorized project.
    (scene.work / "assets/style.css").unlink()
    (scene.work / "assets").rmdir()
    outside = scene.root / "outside-cancelled.txt"
    outside.write_text("outside data")
    selected = outside if case == "outside" else work_file
    token_file = Path(os.environ["LEGION_PERSONAL_TOKENS_FILE"])
    original = scene.core._run_core_inprocess
    def native(*args, **kwargs):
        result = original(*args, **kwargs)
        if case == "private":
            result["artifacts"] = [{"path": str(selected), "name": selected.name, "visibility": "private"}]
        else:
            result["artifacts"] = [{"path": str(selected), "name": selected.name}]
        if case == "revoked":
            token_file.write_text("{}")
        result.update(status="cancelled", final="", error="user stopped", partial=True)
        result["native_facts"].update(process_returncode=130, turn_status="cancelled")
        return result
    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", native)
    scene.monkeypatch.setattr(scene.core, "_dynamic_max_attempts", lambda *_: 1)
    result = scene.core.run_one(
        TASK, "preserve this interrupted project", qhash="__ut_cancel_export_" + case,
        user_id=USER, conversation_id=LANE, cb=scene.events.append,
        task_facts={"team_id": TEAM, "task_id": TASK, "project_context": scene.pc.snapshot(TEAM, TASK)})
    assert result["status"] == "cancelled" and result["cancelled"] is True
    assert result["final"] == "" and result["error"] == "user stopped"
    assert len(scene.native_calls) == 1
    assert not scene.core._is_delivered(result)
    assert work_file.read_text() == "partial output created before cancellation"
    if case == "owned":
        selected, = result.get("artifacts") or []
        assert selected["name"] == "index.html" and selected["export_snapshot_id"]
        assert Path(selected["path"]).read_bytes() == work_file.read_bytes()
        assert len(scene.export_calls) == 1
    else:
        assert not result.get("artifacts"), "revoked, private and outside selections cannot be published"
        assert result.get("artifact_safety_observations"), "a rejection must remain visible"


@pytest.mark.parametrize("failed", [False, True], ids=["success", "native-failure"])
def test_terminal_collection_exports_once_and_preserves_native_outcome(scene, failed):
    original = scene.core._run_core_inprocess
    request_id = "__ut_terminal_collection_" + str(failed)
    scene.monkeypatch.setattr(scene.core, "_dynamic_max_attempts", lambda *_: 1)

    def native(*args, **kwargs):
        result = original(*args, **kwargs)
        if failed:
            result.update(status="rc1", final="", error="synthetic native execution failure",
                          error_envelope={"category": "process", "scope": "harness",
                                          "retryable": True, "code": "rc1",
                                          "message": "synthetic native execution failure"})
            result["native_facts"].update(process_returncode=1, turn_status="failed")
        return result

    scene.monkeypatch.setattr(scene.core, "_run_core_inprocess", native)
    result = scene.core.run_one(
        TASK, "retain this project's actual files", qhash=request_id,
        user_id=USER, conversation_id=LANE, cb=scene.events.append,
        task_facts={"team_id": TEAM, "task_id": TASK,
                    "project_context": scene.pc.snapshot(TEAM, TASK)})
    assert result["status"] == ("error" if failed else "done")
    assert result["native_facts"]["process_returncode"] == (1 if failed else 0)
    assert not result.get("artifact_safety_observations"), result
    assert len(scene.export_calls) == 1, "success and failure must use the same single collector"
    artifacts = result["artifacts"]
    assert {row["name"] for row in artifacts} == {"index.html", "style.css"}
    assert len({row["export_snapshot_id"] for row in artifacts}) == 1
    for row in artifacts:
        assert Path(row["path"]).read_bytes() == Path(row["provenance"]["source_path"]).read_bytes()
    memory = importlib.import_module("task_memory").load(request_id, TASK)
    assert memory["artifacts"] == artifacts
    assert memory["artifact_export"] == result["artifact_export"]


def test_collection_refusal_never_revives_previous_source_paths(scene):
    scene.monkeypatch.setattr(scene.ad, "export_project_artifacts", lambda *a, **k: {
        "artifacts": [], "artifact_safety_observations": [
            scene.ad._not_exported("artifact_project_not_authorized")]})

    def forbidden(*args, **kwargs):
        raise AssertionError("refused project export must not fall back to scanning or extraction")

    scene.monkeypatch.setattr(scene.ad, "scan_new_files", forbidden)
    scene.monkeypatch.setattr(scene.ad, "extract", forbidden)
    result = scene.ad.collect_run_artifacts(
        since_ts=0, workdir=str(scene.work), project=scene.pc.snapshot(TEAM, TASK),
        team_id=TEAM, task_id=TASK, user_id=USER, harness_key=TASK,
        native_conversation_id=LANE, request_id="__ut_export_refusal",
        previous_artifacts=[{"path": str(scene.work / "index.html"), "name": "index.html"}])
    assert result["artifacts"] == []
    assert result["artifact_safety_observations"][0]["reason"] == "artifact_project_not_authorized"
    assert (scene.work / "index.html").is_file()


def test_run_one_exports_the_actual_owned_project_and_resources(explicit_run_one):
    case = explicit_run_one
    work = case.root / "outside-default-exports"
    work.mkdir()
    (work / "assets").mkdir()
    (work / "assets/style.css").write_text("p { color: navy; }")
    control = case.root / "operator-control"
    control.mkdir(mode=0o700)
    native_tmp = case.root / "native-tmp"
    native_tmp.mkdir()
    case.monkeypatch.setenv("TMPDIR", str(native_tmp))
    tokens = control / "tokens.json"
    tokens.write_text(json.dumps({"fixture-operator-token": {
        "user": "owner-a", "project_grants": [{"root": str(work), "export_paths": ["."]}]}}))
    tokens.chmod(0o600)
    case.monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tokens))
    pc = importlib.import_module("project_context")
    pc.initialize(
        "__ut_native_export", "original objective",
        {"tasks": {"maker": {"task": "deliver original work", "assigned_harness": "codex"}},
         "project_context": {"repo_root": str(work)}},
        user_id="owner-a", conversation_id="original-session", mode="orchestrate")
    artifact_store = importlib.import_module("artifact_store")
    case.monkeypatch.setattr(artifact_store, "ROOT", case.root / "private-artifacts")
    case.monkeypatch.setattr(importlib.import_module("artifact_delivery"),
                            "_git_changed_files", lambda *_: None)
    events = []

    def execute(*args, **kwargs):
        (work / "index.html").write_text('<link rel="stylesheet" href="assets/style.css"><p>Real file</p>')
        return {"status": "ok", "final": str(work / "index.html"), "workdir": str(work)}

    case.monkeypatch.setattr(case.core, "_run_core_inprocess", execute)
    result = case.core.run_one(
        "codex", "deliver the original project", qhash="__ut_native_export_maker",
        user_id="owner-a", conversation_id="original-session", cb=events.append,
        task_facts={"team_id": "__ut_native_export", "task_id": "maker",
                    "project_context": pc.load("__ut_native_export")})
    assert result["status"] == "done"
    artifacts = result.get("artifacts") or []
    assert {row["name"] for row in artifacts} == {"index.html", "style.css"}
    assert all(row["export_snapshot_id"] for row in artifacts)
    assert all(row["url"].startswith("/home-outputs/exports/") for row in artifacts)
    assert len([event for event in events if event["t"] == "artifact_ready"]) == 2
    for row in artifacts:
        assert Path(row["path"]).read_bytes() == Path(row["provenance"]["source_path"]).read_bytes()
