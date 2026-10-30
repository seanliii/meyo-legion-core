"""The ordinary native execution return must reach the existing artifact channel."""
import importlib
import json
from pathlib import Path

from test_explicit_responses_provider import isolated, explicit_run_one


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
