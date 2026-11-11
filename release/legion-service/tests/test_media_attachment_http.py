"""Real authenticated HTTP and production dispatch/recovery; model leaves offline."""
import ast
from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest

from test_auto_solo_bound_project import _post_auto
from test_explicit_responses_provider import isolated, explicit_run_one
import test_project_export_terminal_binding as project_fixture
from test_project_export_terminal_binding import USER, CONVERSATION
from test_native_terminal_dispatch_graph import graph
from test_standard_coordination_resume import (
    case as resume_case, seed, TEAM as RESUME_TEAM, OWNER as RESUME_OWNER,
    CONVERSATION as RESUME_CONVERSATION,
)


@pytest.fixture
def scene(explicit_run_one):
    # Reuse the existing scene only for graph tests, not its module's autouse
    # setting: recovery has its own fixture/control directory and identity.
    return project_fixture.scene.__wrapped__(explicit_run_one)


def _three(directory):
    directory.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, name in enumerate(("111.mp4", "2222.mp4", "333.mp4"), 1):
        path = directory / f"opaque-{index}.mp4"
        path.write_bytes(f"offline ownership fixture {index}".encode())
        rows.append({"path": str(path), "name": name,
                     "description": f"reference description {index}",
                     "source_ref": {"reference_id": f"source-{index:02d}"}})
    return rows


def _http_setup(case):
    mp, core = case.monkeypatch, case.core
    server = importlib.import_module("server")
    keys = {"codex", "cc"}
    mp.setattr(core, "_harness_keys", lambda: set(keys))
    mp.setattr(core, "_CORE_INPROC_KEYS", set(keys))
    mp.setattr(core, "_aigc_ready", lambda **kw: {"runnable_clis": sorted(keys)})
    mp.setattr(core, "_autonomy_matrix", lambda: {})
    mp.setattr(core, "pilot_settings", lambda *a, **kw: {"enabled": False})
    mp.setattr(server, "_source_version_identity", lambda: {"commit": "offline-fixture"})
    mp.setattr(importlib.import_module("harness_performance"), "route_policy",
               lambda *a: {"verified": False, "route": "strongest_single"})
    mp.setattr(server.DESKTOP_FS, "materialize_session", lambda *a, **kw: {})
    mp.setattr(server.DESKTOP_FS, "sync_personal_data", lambda *a, **kw: None)
    # The reused graph fixture suppresses all prelude text. Restore this actual
    # production function so the test observes real attachment context delivery.
    tree = ast.parse(Path(core.__file__).read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_build_prelude")
    scope = dict(core.__dict__)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), core.__file__, "exec"), scope)
    mp.setattr(core, "_build_prelude", scope["_build_prelude"])
    scope["_recall_experience"] = lambda *a, **kw: ""
    prompts = []
    native = core._run_core_inprocess
    def capture(key, task, *args, **kwargs):
        prompts.append(task)
        return native(key, task, *args, **kwargs)
    mp.setattr(core, "_run_core_inprocess", capture)
    rows = _three(Path(case.identity.upload_root(USER)))
    return server, rows, prompts


@pytest.mark.parametrize("mode", ["pk", "auto"], ids=["normal", "auto-solo"])
def test_http_author_receives_three_original_names_and_paths(graph, mode):
    server, rows, prompts = _http_setup(graph)
    graph.team = "__ut_media_http_" + mode
    question = "Use all three uploaded references and return the existing site."
    plan = {"attachments": deepcopy(rows), "project_context": {"repo_root": str(graph.work)},
            "team_policy": {"candidate_harnesses": ["codex", "cc"], "max_failover": 0,
                            "contract_repair_rounds": 0}}
    task = {"task": question, "assigned_harness": "codex", "assignment_locked": True}
    if mode == "pk":
        plan["tasks"] = {"codex": task}
    else:
        graph.monkeypatch.setattr(graph.core, "plan_request_with_model", lambda *a, **kw: {
            "ok": True, "suggested_mode": "pk", "tasks": {"codex": task},
            "directions": {"codex": "quick"}})
    out = _post_auto(graph, server, {
        "question": question, "mode": mode, "team_id": graph.team,
        "session_id": CONVERSATION, "plan": plan,
    })
    assert prompts and graph.native_calls, out
    if mode == "auto":
        assert out.get("solo"), out
    for prompt in prompts:
        for row in rows:
            assert row["name"] in prompt and row["path"] in prompt
            assert row["description"] in prompt and row["source_ref"]["reference_id"] in prompt
    assert graph.pc.load(graph.team)["input_snapshot"]["attachments"] == rows


def test_http_model_written_foreign_attachment_never_reaches_author(graph):
    server, rows, prompts = _http_setup(graph)
    foreign = _three(Path(graph.identity.upload_root("other-owner")))[0]
    graph.team = "__ut_media_http_model_path"
    # A real server election write-back accepts a model-produced plan object.
    # Only that model decision is replaced; final admission and dispatch stay real.
    graph.monkeypatch.setattr(graph.core, "elect_captain", lambda *a, **kw: {
        "ok": True, "captain": "codex",
        "plan": {"captain": "codex", "attachments": rows + [foreign],
                 "tasks": {"codex": {"task": "Use references", "assigned_harness": "codex"}}},
    })
    out = _post_auto(graph, server, {
        "question": "Use uploaded references", "mode": "orchestrate",
        "team_id": graph.team, "session_id": CONVERSATION,
        "plan": {"attachments": rows, "project_context": {"repo_root": str(graph.work)}},
    })
    assert out.get("error") == "attachment_not_authorized", out
    assert not prompts and not graph.native_calls


def _saved_references(case):
    seed(case)
    rows = _three(case.root / "work" / "reference-files")
    case.server.SESSIONS.save_turn(
        RESUME_CONVERSATION, "saved references",
        {"results": [{"key": "source", "artifacts": deepcopy(rows)}]}, user_id=RESUME_OWNER)
    snapshot = {"attachments": deepcopy(rows), "reference_receipt": "original-input"}
    case.pc.initialize(RESUME_TEAM, "original objective",
                       {"input_snapshot": snapshot}, mode="orchestrate",
                       user_id=RESUME_OWNER, conversation_id=RESUME_CONVERSATION)
    state = case.teams.load(RESUME_TEAM)
    state["input_snapshot"] = deepcopy(snapshot)
    case.teams._write(state)
    return rows, snapshot


def test_http_recovery_authorized_keeps_the_same_attachment_snapshot(resume_case):
    case = resume_case
    rows, snapshot = _saved_references(case)
    calls = []
    original = case.core.dispatch_persistent
    def observe(question, plan=None, **kwargs):
        calls.append(deepcopy(plan))
        return original(question, plan=plan, **kwargs)
    case.mp.setattr(case.core, "dispatch_persistent", observe)
    status, response = case.post()
    assert status == 200 and calls and case.controls, response
    assert calls[0]["attachments"] == rows
    assert calls[0]["input_snapshot"] == snapshot
    assert case.pc.load(RESUME_TEAM)["input_snapshot"] == snapshot


@pytest.mark.parametrize("when", ["before_claim", "after_claim"])
def test_http_recovery_revoked_file_grant_starts_no_author(resume_case, when):
    case = resume_case
    rows, snapshot = _saved_references(case)
    identity = importlib.import_module("personal_identity")
    assert identity.file_allowed(rows[0]["path"], RESUME_OWNER, RESUME_CONVERSATION)
    session_path = Path(case.server.SESSIONS._path(RESUME_CONVERSATION))
    def revoke():
        record = json.loads(session_path.read_text())
        for turn in record["turns"]:
            for result in turn.get("results") or []:
                result.pop("artifacts", None)
        session_path.write_text(json.dumps(record))
        assert not identity.file_allowed(rows[0]["path"], RESUME_OWNER, RESUME_CONVERSATION)
    claim = case.teams.claim_resume
    def claim_then_revoke(*args, **kwargs):
        accepted = claim(*args, **kwargs)
        if accepted and when == "after_claim":
            revoke()
        return accepted
    case.mp.setattr(case.teams, "claim_resume", claim_then_revoke)
    if when == "before_claim":
        revoke()
    status, response = case.post()
    assert status == 403 and "attachment_not_authorized" in response.get("reason", ""), response
    assert not case.members and not case.controls
    assert not case.teams.load(RESUME_TEAM).get("resume_lease")
    assert case.pc.load(RESUME_TEAM)["input_snapshot"] == snapshot
