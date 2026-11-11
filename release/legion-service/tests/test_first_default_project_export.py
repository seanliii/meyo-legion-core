"""First allocated workspaces must publish versions, not mutable source URLs.

Only synthetic owners, sessions and files. The real allocator, run_one, project
exporter, store, session registration and download authorization execute.
Native/model execution is substituted; no HTTP listener or real model runs.
"""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one


OWNER = "__ut_first_export_owner"
SESSION = "__ut_first_export_session"
FIRST = b"# First fixture version\n"
SECOND = b"# Second fixture version with revised instructions\n"


@pytest.fixture
def case(explicit_run_one):
    case = explicit_run_one
    mp, root = case.monkeypatch, case.root
    for name, leaf in (
        ("CODEX_HOME", "codex-home"), ("TMPDIR", "native-tmp"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_TEAM_DIR", "teams"),
        ("LEGION_SESSIONS_DIR", "sessions"), ("LEGION_ARTIFACT_STORE_DIR", "store"),
        ("LEGION_USER_CONFIG_DIR", "users"),
    ):
        directory = root / leaf
        directory.mkdir(exist_ok=True)
        mp.setenv(name, str(directory))
    # This account has NO directory grant. Never borrow an operator grant to
    # make the first default allocation appear authorized.
    control = root / "control"
    control.mkdir(mode=0o700)
    token_file = control / "identities.json"
    token_file.write_text(json.dumps({"synthetic-account-only": {"user": OWNER}}))
    token_file.chmod(0o600)
    mp.setenv("LEGION_PERSONAL_TOKENS_FILE", str(token_file))
    case.pc = importlib.import_module("project_context")
    case.teams = importlib.import_module("team_state")
    case.sessions = importlib.import_module("sessions")
    case.sandbox = importlib.import_module("legion_sandbox")
    case.identity = importlib.import_module("personal_identity")
    case.ad = importlib.import_module("artifact_delivery")
    case.store = importlib.import_module("artifact_store")
    mp.setattr(case.pc, "ROOT", root / "projects")
    mp.setattr(case.store, "ROOT", root / "store")
    mp.setattr(case.ad, "OUTPUTS", str(root / "service-outputs"))
    mp.setattr(case.ad, "_git_changed_files", lambda _root: None)
    mp.setattr(case.core, "_harness_keys", lambda: {"codex"})
    mp.setattr(case.core, "_CANCEL_REGISTRY", {})
    for name in ("_build_prelude", "_build_runtime_brief", "_captain_mail_context"):
        mp.setattr(case.core, name, lambda *a, **kw: "")
    case.content, case.native_calls, case.events = FIRST, [], []
    case.include_checkpoint = True
    case.after_write = lambda _work: None
    case.tokens = token_file

    def native(key, task, qhash, *args, **kwargs):
        # Keep actual workdir resolution below the execution seam as well.
        work = Path(case.sandbox.workdir(
            key, qhash, kwargs.get("project_context"), kwargs["user_id"]))
        case.native_calls.append((key, str(work)))
        (work / "README.md").write_bytes(case.content)
        case.after_write(work)
        return {
            "status": "ok", "final": "The fixture file is ready.", "workdir": str(work),
            **({"native_checkpoint": {"kind": "codex_thread", "session_id": "__ut_native_first"}}
               if case.include_checkpoint else {}),
        }

    # run_one inspects the real worker signature before passing context.
    native.__signature__ = inspect.signature(case.core._run_harness_isolated)
    mp.setattr(case.core, "_run_core_inprocess", native)
    mp.setattr(case.core, "_run_external_inprocess", native)
    mp.setattr(case.core, "_run_harness_isolated", native)
    return case


def request(case, *, key="codex", team="__ut_first_export", task="maker",
            owner=OWNER, session=SESSION, context=None, native_lane=True, tasks=None,
            session_team=None):
    plan = {
        "tasks": {node: {"task": "write the fixture", "assigned_harness": key}
                  for node in (tasks or [task])},
        "project_context": deepcopy(context or {}),
    }
    case.pc.initialize(team, "fixture", plan, mode="single",
                       user_id=owner, conversation_id=session)
    case.teams.ensure(team, "fixture", plan["tasks"], {node: [] for node in plan["tasks"]},
                      user_id=owner, conversation_id=session, registered={key})
    attempt = case.teams.begin_attempt(team, task, owner="synthetic-service")
    turn = team + "-turn"
    saved = case.sessions.save_turn(
        session, "fixture", {"status": "running", "team_id": session_team or team, "results": []},
        user_id=owner, mode="single", turn_id=turn)
    assert not saved.get("error")
    return SimpleNamespace(key=key, team=team, task=task, owner=owner, session=session,
                           turn=turn, attempt=attempt, qhash=team + "_task_" + task,
                           lane=session + ":task:" + task if native_lane else session)


def run(case, req, *, context=None):
    return case.core.run_one(
        req.key, "write the fixture", qhash=req.qhash, user_id=req.owner,
        conversation_id=req.lane, cb=case.events.append,
        task_facts={"team_id": req.team, "task_id": req.task, "attempt_id": req.attempt,
                    "project_context": (context if context is not None else
                                        case.pc.snapshot(req.team, req.task))})


def save_result(case, req, result):
    assert case.teams.finish_attempt(req.team, req.task, req.attempt, result)
    saved = case.sessions.save_turn(
        req.session, "fixture", {"status": result["status"], "team_id": req.team,
                                "results": [result]}, user_id=req.owner,
        mode="single", turn_id=req.turn)
    assert not saved.get("error")


@pytest.mark.parametrize("boundary,checkpoint", [
    ("core_inproc", True), ("external_inproc", True),
    ("isolated", True), ("isolated", False),
])
def test_first_export_survives_same_workdir_continuation_under_original_sha(case, boundary, checkpoint):
    # Test the shared routing seam, not a real harness/model. Auto stays disabled.
    case.include_checkpoint = checkpoint
    case.monkeypatch.setattr(case.core, "_CORE_INPROC_KEYS",
                             {"codex"} if boundary == "core_inproc" else set())
    registry = importlib.import_module("external_agent_registry")
    case.monkeypatch.setattr(registry, "load_catalog",
                             lambda: [{"key": "codex"}] if boundary == "external_inproc" else [])
    first_req = request(case)
    first = run(case, first_req)
    assert first["status"] == "done", first
    original, = first.get("artifacts") or []
    registered_sha = original.get("sha256") or original["provenance"]["sha256"]
    assert registered_sha == hashlib.sha256(FIRST).hexdigest()
    save_result(case, first_req, first)

    case.content = SECOND
    next_req = request(case, team="__ut_followup_export",
                       context={"workspace_root": first["workdir"]})
    later = run(case, next_req)
    assert Path(first["workdir"], "README.md").read_bytes() == SECOND
    readback = case.ad._content_safety.read_public_file(original["path"])
    print("FIRST_DEFAULT_EXPORT", json.dumps({
        "boundary": boundary, "synthetic_checkpoint": checkpoint, "original_sha": registered_sha,
        "readback_sha": hashlib.sha256(readback).hexdigest(),
        "versioned": bool(original.get("export_snapshot_id")),
        "continuation_status": later["status"],
    }))
    assert readback == FIRST, "a later edit must not overwrite the first registered version"
    assert original["url"].startswith("/home-outputs/exports/")
    assert original["export_snapshot_id"]
    assert case.identity.expected_export_hash(original["path"], OWNER, SESSION) == original["sha256"]
    assert case.identity.file_allowed(original["path"], OWNER, SESSION)
    assert not case.identity.file_allowed(original["path"], "another-owner", SESSION)
    assert not case.identity.file_allowed(str(Path(first["workdir"], "README.md")), OWNER, SESSION)
    current, = later["artifacts"]
    assert current["sha256"] == hashlib.sha256(SECOND).hexdigest()
    assert current["export_snapshot_id"] != original["export_snapshot_id"]
    if checkpoint:
        assert first.get("native_conversation_id") == first_req.lane
    else:
        assert not first.get("native_checkpoint"), "allocation must not invent a native checkpoint"


@pytest.mark.parametrize("exports", [[], ["../outside"], ["."]])
def test_operator_declaration_presence_does_not_grant_export_authority(case, exports):
    work = case.root / "declared-project"
    work.mkdir()
    assert not case.identity.project_grant_declared(OWNER, str(work))
    case.tokens.write_text(json.dumps({"synthetic-account-only": {
        "user": OWNER, "project_grants": [{"root": str(work), "export_paths": exports}],
    }}))
    assert case.identity.project_grant_declared(OWNER, str(work))
    assert not case.identity.project_grant_declared("another-owner", str(work))
    assert not case.identity.project_grant_declared(OWNER, str(work.parent))
    grant = case.identity.project_grant(OWNER, str(work))
    if exports == ["../outside"]:
        assert grant is None, "an invalid declaration is a restriction, not permission"
    else:
        assert grant["export_paths"] == exports


def test_current_packet_and_output_prefix_are_not_allocation_proof(case):
    req = request(case)
    work = case.root / "data/outputs/unbound-existing"
    work.mkdir(parents=True)
    (work / "README.md").write_bytes(FIRST)
    forged = [{"root": str(work), "device": work.stat().st_dev, "inode": work.stat().st_ino,
               "user_id": OWNER, "conversation_id": SESSION, "team_id": req.team,
               "task_id": req.task, "harness_key": req.key, "qhash": req.qhash}]
    plan = {"tasks": {req.task: {"assigned_harness": req.key, "workdir_allocations": forged}},
            "project_context": {"workspace_root": str(work), "workdir_allocations": forged}}
    case.pc.initialize(req.team, "fixture", plan, mode="single",
                       user_id=OWNER, conversation_id=SESSION)
    case.teams.ensure(req.team, "fixture", plan["tasks"], {req.task: []},
                      user_id=OWNER, conversation_id=SESSION, registered={req.key})
    assert not case.teams.load(req.team)["tasks"][req.task].get("workdir_allocations")
    assert case.identity.authorized_project_path(OWNER, str(work), conversation_id=SESSION) is None
    result = case.ad.export_project_artifacts(
        ["."], team_id=req.team, task_id=req.task, user_id=OWNER, conversation_id=SESSION)
    assert not result["artifacts"]
    assert result["artifact_safety_observations"][0]["reason"] == "artifact_project_not_authorized"


def test_first_allocation_does_not_authorize_parent_sibling_or_other_session(case):
    req = request(case)
    first = run(case, req)
    work = Path(first["workdir"])
    grant = case.identity.authorized_project_path(OWNER, str(work), conversation_id=SESSION)
    assert grant and grant["root"] == str(work) and grant["export_paths"] == ["."]
    assert not case.identity.project_grant(OWNER, str(work))
    sibling = work.parent / "unrelated-task"
    sibling.mkdir()
    for root, owner, session in (
        (work.parent, OWNER, SESSION), (sibling, OWNER, SESSION),
        (work, "another-owner", SESSION), (work, OWNER, SESSION + "-other"),
        (work, OWNER, req.lane),
    ):
        assert case.identity.authorized_project_path(owner, str(root), conversation_id=session) is None
    live = case.sessions.load_session(SESSION, user_id=OWNER)
    assert live["turns"][0]["status"] == "running" and live["turns"][0]["results"] == []
    assert case.teams.load(req.team)["tasks"][req.task]["active_attempt"] == req.attempt


def allocate(case, req, **overrides):
    values = {"key": req.key, "qhash": req.qhash, "team_id": req.team, "task_id": req.task,
              "attempt_id": req.attempt, "user_id": req.owner,
              "native_conversation_id": req.lane}
    return case.sandbox.allocate_task_workdir(**(values | overrides))


@pytest.mark.parametrize("invalid", ["owner", "lane", "task", "harness", "attempt",
                                   "cancelled", "session_team"])
def test_allocation_requires_current_owned_task_not_request_claims(case, invalid):
    req = request(case, session_team="__ut_elsewhere" if invalid == "session_team" else None)
    overrides = {
        "owner": {"user_id": "another-owner"},
        "lane": {"native_conversation_id": SESSION + ":task:another"},
        "task": {"task_id": "another"},
        "harness": {"key": "another-harness"},
        "attempt": {"attempt_id": req.attempt + 1},
    }.get(invalid, {})
    if invalid == "cancelled":
        assert case.teams.request_cancel(req.team, req.task, scope="task")
    with pytest.raises(PermissionError):
        allocate(case, req, **overrides)
    assert not case.native_calls
    assert not case.teams.load(req.team)["tasks"][req.task].get("workdir_allocations")
    assert not list((case.root / "data/outputs").glob("*"))


def test_same_harness_tasks_keep_separate_allocations_and_canonical_root(case):
    one = request(case, task="one", tasks=["one", "two"])
    first = run(case, one)
    case.content = SECOND
    two = SimpleNamespace(**(vars(one) | {
        "task": "two", "qhash": one.team + "_task_two", "lane": SESSION + ":task:two",
        "attempt": case.teams.begin_attempt(one.team, "two", owner="synthetic-service"),
    }))
    second = run(case, two)
    assert first["status"] == second["status"] == "done"
    assert first["workdir"] != second["workdir"]
    assert not (case.pc.load(one.team).get("repo_root") or case.pc.load(one.team).get("workspace_root"))
    for req, result, body in ((one, first, FIRST), (two, second, SECOND)):
        assert case.pc.snapshot(req.team, req.task)["workspace_root"] == result["workdir"]
        artifact, = result["artifacts"]
        assert Path(artifact["path"]).read_bytes() == body
        assert artifact["provenance"]["source_path"] == str(Path(result["workdir"], "README.md"))
    assert not case.pc.snapshot(one.team).get("workspace_root"), "a team is not its first member's root"


@pytest.mark.parametrize("kind", ["existing_directory", "symlink"])
def test_unbound_existing_default_directory_is_not_adopted_or_modified(case, kind):
    req = request(case)
    root = case.root / "data/outputs" / f"{req.key}_{req.qhash}"
    root.parent.mkdir(parents=True, exist_ok=True)
    target = root if kind == "existing_directory" else case.root / "not-this-task"
    target.mkdir()
    (target / "README.md").write_bytes(SECOND)
    if kind == "symlink":
        root.symlink_to(target, target_is_directory=True)
    result = run(case, req)
    assert result["status"] == "error" and result["blocked_before_native"]
    assert result["fail_kind"] == "project_workspace"
    assert not case.native_calls and not result.get("artifacts")
    assert (target / "README.md").read_bytes() == SECOND
    assert case.identity.authorized_project_path(OWNER, str(root), conversation_id=SESSION) is None


@pytest.mark.parametrize("kind", ["replaced_directory", "symlink"])
def test_allocation_receipt_cannot_authorize_a_replacement_directory(case, kind):
    req = request(case)
    root = Path(allocate(case, req))
    moved = root.with_name(root.name + "-saved")
    root.rename(moved)
    if kind == "symlink":
        root.symlink_to(moved, target_is_directory=True)
    else:
        root.mkdir()
    assert case.identity.authorized_project_path(OWNER, str(root), conversation_id=SESSION) is None
    with pytest.raises(PermissionError):
        allocate(case, req)
    assert moved.is_dir()


def test_completed_allocation_replacement_is_not_reauthorized_by_native_result(case):
    """FE-F1: a saved final must not revive a replaced allocator-owned directory."""
    server = importlib.import_module("server")
    case.monkeypatch.setattr(case.core, "pilot_settings", lambda *_: {"enabled": False})
    events = []
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/api/legion/run_one"
    handler._personal_user = lambda: OWNER
    handler._sse_open = lambda: None
    handler._sse_send = lambda kind, value, **_: events.append((kind, deepcopy(value))) or True
    handler._send_json = lambda value, status=200: events.append(("json", (status, deepcopy(value))))

    def invoke(team):
        events.clear()
        body = {"key": "codex", "task": "write the fixture", "session_id": SESSION,
                "team_id": team, "execution_depth": "quick"}
        assert handler._bind_personal_request(body, allow_new=True)
        handler._handle_run_one(body)
        return next((value["result"] for kind, value in events if kind == "final"), None)

    first_team = "__ut_completed_allocation"
    first = invoke(first_team)
    assert first and first["status"] == "done", events
    original, = first["artifacts"]
    root = Path(first["workdir"])
    before = root.stat()
    assert case.sandbox.owned_workdir_allocations(first_team, OWNER, SESSION)
    saved = root.with_name(root.name + "-original")
    root.rename(saved)
    root.mkdir()
    assert (root.stat().st_dev, root.stat().st_ino) != (before.st_dev, before.st_ino)
    replacement = b"# Synthetic replacement, never allocated to this task\n"
    (root / "unrelated-replacement.md").write_bytes(replacement)
    assert not case.sandbox.owned_workdir_allocations(first_team, OWNER, SESSION)
    fallback = case.identity.authorized_project_path(OWNER, str(root), conversation_id=SESSION)
    case.content = SECOND
    second = invoke("__ut_replaced_allocation_followup")
    rejection = next((value for kind, value in events if kind == "json"), None)
    readback = case.ad._content_safety.read_public_file(original["path"])
    print("COMPLETED_ALLOCATION_REPLACEMENT", json.dumps({
        "authorization_source": (fallback or {}).get("source"),
        "followup_rejection": rejection, "native_seam_calls": len(case.native_calls),
        "followup_exports": [row["name"] for row in (second or {}).get("artifacts") or []],
        "original_sha": original["sha256"], "readback_sha": hashlib.sha256(readback).hexdigest(),
    }))
    assert fallback is None and second is None and len(case.native_calls) == 1
    assert rejection == (403, {"error": "project_workspace_not_authorized"})
    assert not (root / "README.md").exists(), "denied continuation must not run in the replacement"
    assert (root / "unrelated-replacement.md").read_bytes() == replacement
    assert readback == FIRST and hashlib.sha256(readback).hexdigest() == original["sha256"]
    assert case.identity.file_allowed(original["path"], OWNER, SESSION)
    assert case.identity.expected_export_hash(original["path"], OWNER, SESSION) == original["sha256"]
    assert not case.identity.file_allowed(original["path"], "another-owner", SESSION)
    assert not case.identity.file_allowed(str(root / "unrelated-replacement.md"), OWNER, SESSION)

    # Restoring the actual allocated inode permits a legitimate retry; the
    # rejected replacement is neither adopted nor deleted by the runtime.
    root.rename(root.with_name(root.name + "-rejected"))
    saved.rename(root)
    recovered = invoke("__ut_restored_allocation_followup")
    assert recovered and recovered["status"] == "done", events
    assert recovered["workdir"] == str(root)
    assert Path(recovered["artifacts"][0]["path"]).read_bytes() == SECOND
    assert case.ad._content_safety.read_public_file(original["path"]) == FIRST


def test_failed_allocation_persistence_never_starts_native_and_allows_retry(case):
    req = request(case)
    writer = case.teams._write

    def fail_receipt(state):
        if any(node.get("workdir_allocations") for node in state["tasks"].values()):
            raise OSError("synthetic allocation store unavailable")
        writer(state)

    with case.monkeypatch.context() as mp:
        mp.setattr(case.teams, "_write", fail_receipt)
        refused = run(case, req)
    assert refused["status"] == "error" and refused["blocked_before_native"]
    assert "synthetic allocation store unavailable" in refused["error"]
    assert not case.native_calls
    assert not list((case.root / "data/outputs").glob("*"))
    assert not case.teams.load(req.team)["tasks"][req.task].get("workdir_allocations")
    recovered = run(case, req)
    assert recovered["status"] == "done"
    assert recovered["artifacts"][0]["export_snapshot_id"]


def test_failed_allocation_write_does_not_remove_a_replacement_directory(case):
    req = request(case)
    root = case.root / "data/outputs" / f"{req.key}_{req.qhash}"
    moved = root.with_name(root.name + "-original")

    def replace_then_fail(_state):
        root.rename(moved)
        root.mkdir()
        raise OSError("synthetic concurrent replacement and write failure")

    case.monkeypatch.setattr(case.teams, "_write", replace_then_fail)
    with pytest.raises(OSError):
        allocate(case, req)
    assert root.is_dir() and moved.is_dir(), "rollback may remove only its own still-empty allocation"
    assert case.identity.authorized_project_path(OWNER, str(root), conversation_id=SESSION) is None


@pytest.mark.parametrize("failure,reason", [
    ("invalid_grant", "artifact_project_not_authorized"),
    ("sensitive_content", "artifact_sensitive_content"),
    ("store_write", "artifact_export_unavailable"),
])
def test_first_export_refusal_cannot_fall_back_to_mutable_source(case, failure, reason):
    req = request(case)
    if failure == "invalid_grant":
        def invalid_grant(work):
            case.tokens.write_text(json.dumps({"synthetic-account-only": {
                "user": OWNER, "project_grants": [{"root": str(work), "export_paths": ["../outside"]}],
            }}))
        case.after_write = invalid_grant
    elif failure == "sensitive_content":
        case.content = b'api_key="live-fixture-credential-7a93c81f56dd"\n'
    else:
        def fail(*_a, **_kw):
            raise OSError("synthetic export store unavailable")
        case.monkeypatch.setattr(case.store, "put_export_bundle", fail)
    result = run(case, req)
    assert result["status"] == "done", "an export refusal must not discard the native result"
    assert result["final"] == "The fixture file is ready."
    assert not result.get("artifacts"), "refused export must not become a legacy source URL"
    assert reason in {note["reason"] for note in result["artifact_safety_observations"]}
    assert Path(result["workdir"], "README.md").read_bytes() == case.content
    out = {"team_id": req.team, "results": [result]}
    case.ad.enrich(out, since_ts=1, workdirs=[result["workdir"]])
    assert not out.get("artifacts") and not out["results"][0].get("artifacts")
    assert "live-fixture-credential-7a93c81f56dd" not in repr(out)


@pytest.mark.parametrize("pause", ["partial", "budget"])
def test_partial_collection_cannot_downgrade_a_revoked_operator_grant(case, pause):
    req = request(case)
    work = Path(allocate(case, req))
    case.tokens.write_text(json.dumps({"synthetic-account-only": {
        "user": OWNER, "project_grants": [{"root": str(work), "export_paths": ["."]}],
    }}))
    original = case.core._run_core_inprocess
    observations = []

    def native(*args, **kwargs):
        result = original(*args, **kwargs)
        case.tokens.write_text(json.dumps({"synthetic-account-only": {"user": OWNER}}))
        result.update(status="partial", final="The saved partial file is not a completed task.",
                      artifacts=[{"name": "README.md", "path": str(work / "README.md")}])
        result["native_facts"] = {"process_returncode": 0, "turn_status": "incomplete"}
        if pause == "budget":
            observations.append({"status": "error", "source": "operator_runtime_budget",
                "error_envelope": {"category": "runtime_budget", "scope": "request",
                    "retryable": False, "code": "request_total_budget_exhausted",
                    "message": "Synthetic request budget exhausted."}})
        return result

    case.monkeypatch.setattr(case.core, "_run_core_inprocess", native)
    result = case.core.run_one(
        req.key, "retain only the authorized partial work", qhash=req.qhash,
        user_id=req.owner, conversation_id=req.lane,
        _provider_results=lambda: list(observations),
        task_facts={"team_id": req.team, "task_id": req.task, "attempt_id": req.attempt,
                    "project_context": case.pc.snapshot(req.team, req.task)})
    assert result["status"] != "done"
    assert (work / "README.md").read_bytes() == FIRST
    assert case.identity.authorized_project_path(OWNER, str(work), conversation_id=SESSION)
    assert not result.get("artifacts"), "the native allocation cannot replace the revoked operator grant"
    assert "artifact_project_not_authorized" in {
        note["reason"] for note in result["artifact_safety_observations"]}
    memory = importlib.import_module("task_memory").load(req.qhash, req.key)
    assert memory["artifact_export"] == result["artifact_export"]
    assert memory["artifact_safety_observations"] == result["artifact_safety_observations"]


def test_operator_subtree_still_limits_a_real_default_allocation(case):
    req = request(case)

    def scope(work):
        (work / "site").mkdir()
        (work / "site/index.html").write_bytes(b'<link rel="stylesheet" href="style.css">')
        (work / "site/style.css").write_bytes(b"body {color: blue}")
        case.tokens.write_text(json.dumps({"synthetic-account-only": {
            "user": OWNER, "project_grants": [{"root": str(work), "export_paths": ["site"]}],
        }}))

    case.after_write = scope
    result = run(case, req)
    assert {row["name"] for row in result["artifacts"]} == {"index.html", "style.css"}
    save_result(case, req, result)
    for artifact in result["artifacts"]:
        assert artifact["export_snapshot_id"]
        assert case.identity.file_allowed(artifact["path"], OWNER, SESSION)
        assert case.identity.expected_export_hash(artifact["path"], OWNER, SESSION) == artifact["sha256"]
    assert not case.identity.file_allowed(str(Path(result["workdir"], "README.md")), OWNER, SESSION)


@pytest.mark.parametrize("change", ["revoked", "narrowed"])
def test_operator_grant_cannot_be_replaced_by_allocation_fallback_during_export(case, change):
    req = request(case)
    root = Path(allocate(case, req))
    (root / "README.md").write_bytes(FIRST)
    case.tokens.write_text(json.dumps({"synthetic-account-only": {
        "user": OWNER, "project_grants": [{"root": str(root), "export_paths": ["."]}],
    }}))
    inspect_file = case.ad._content_safety._inspect

    def change_grant(stream, name=""):
        checked = inspect_file(stream, name)
        record = {"user": OWNER}
        if change == "narrowed":
            record["project_grants"] = [{"root": str(root), "export_paths": []}]
        case.tokens.write_text(json.dumps({"synthetic-account-only": record}))
        return checked

    case.monkeypatch.setattr(case.ad._content_safety, "_inspect", change_grant)
    result = case.ad.export_project_artifacts(
        ["."], team_id=req.team, task_id=req.task, user_id=OWNER, conversation_id=SESSION)
    assert not result["artifacts"], "operator revocation cannot fall back to the allocation's wider scope"
    assert result["artifact_safety_observations"][0]["reason"] == (
        "artifact_project_not_authorized" if change == "revoked" else "artifact_export_path_not_authorized")
    assert (root / "README.md").read_bytes() == FIRST


@pytest.mark.parametrize("change,reason", [
    ("revoked", "artifact_project_not_authorized"),
    ("narrowed", "artifact_export_path_not_authorized"),
    ("unchanged", None),
])
def test_runtime_selected_operator_grant_is_not_downgraded_at_export_entry(case, change, reason):
    """FE-F2: preserve the selected grant requirement before the first binding."""
    req = request(case)
    real_export = case.ad.export_project_artifacts
    real_inspect = case.ad._content_safety._inspect
    inspected, selection = [], {}
    page = b"<h1>Synthetic bounded export</h1>"

    def scope(work):
        (work / "site").mkdir()
        (work / "site/index.html").write_bytes(page)
        case.tokens.write_text(json.dumps({"synthetic-account-only": {
            "user": OWNER, "project_grants": [{"root": str(work), "export_paths": ["site"]}],
        }}))

    def change_then_export(paths, **binding):
        root = case.pc.task_workspace(case.pc.load(req.team), req.task)
        before = case.identity.authorized_project_path(OWNER, root, conversation_id=SESSION)
        selection.update(paths=paths, source_before=(before or {}).get("source"))
        if change != "unchanged":
            record = {"user": OWNER}
            if change == "narrowed":
                record["project_grants"] = [{"root": root, "export_paths": ["site/index.html"]}]
            case.tokens.write_text(json.dumps({"synthetic-account-only": record}))
        after = case.identity.authorized_project_path(OWNER, root, conversation_id=SESSION)
        selection["source_after"] = (after or {}).get("source")
        return real_export(paths, **binding)

    def inspect_file(stream, name=""):
        inspected.append(name)
        return real_inspect(stream, name)

    case.after_write = scope
    case.monkeypatch.setattr(case.ad, "export_project_artifacts", change_then_export)
    case.monkeypatch.setattr(case.ad._content_safety, "_inspect", inspect_file)
    result = run(case, req)
    reasons = [note["reason"] for note in result.get("artifact_safety_observations") or []]
    print("RUNTIME_EXPORT_ENTRY", json.dumps({
        "change": change, **selection, "reasons": reasons, "inspected": inspected,
        "exported_names": [row["name"] for row in result.get("artifacts") or []],
    }))
    assert selection["paths"] == ["site"] and selection["source_before"] == "operator_project_grant"
    assert result["status"] == "done" and result["final"] == "The fixture file is ready."
    if reason:
        assert not result.get("artifacts") and reason in reasons
        assert not inspected, "revocation/narrowing must be checked before reading selected bytes"
        out = {"team_id": req.team, "results": [result]}
        case.ad.enrich(out, since_ts=1, workdirs=[result["workdir"]])
        assert not out.get("artifacts") and not out["results"][0].get("artifacts")
    else:
        artifact, = result["artifacts"]
        assert not reasons and artifact["name"] == "index.html"
        assert artifact["export_snapshot_id"] and Path(artifact["path"]).read_bytes() == page
        assert artifact["provenance"]["source_path"] == str(Path(result["workdir"], "site/index.html"))


def test_actual_dispatch_binds_rootless_task_before_native_and_formal_export(case):
    team = "__ut_first_dispatch_export"
    case.sessions.save_turn(
        SESSION, "fixture", {"status": "running", "team_id": team, "results": []},
        user_id=OWNER)
    for name in ("_read_async_deep_plans",):
        case.monkeypatch.setattr(case.core, name, lambda *a, **kw: "")
    case.monkeypatch.setattr(case.core, "_aigc_ready", lambda **kw: {"runnable_clis": ["codex"]})
    case.monkeypatch.setattr(case.core, "_autonomy_matrix", lambda: {})
    case.monkeypatch.setattr(case.core, "_baseline_snapshot", lambda *a, **kw: None)
    case.monkeypatch.setattr(importlib.import_module("model_registry"), "resolve_assignments",
                             lambda *a, **kw: {})

    class Lease(nullcontext):
        wait_seconds = 0

    case.monkeypatch.setattr(case.core, "_GatewayLease", lambda *a, **kw: Lease())
    result = case.core.dispatch(
        "write the fixture",
        {"tasks": {"maker": {"task": "write the fixture", "assigned_harness": "codex",
                              "assignment_locked": True}}},
        qhash=team, mode="single", user_id=OWNER, conversation_id=SESSION)
    row, = result["results"]
    assert row["status"] == "done", row
    artifact, = row["artifacts"]
    assert artifact["export_snapshot_id"] and Path(artifact["path"]).read_bytes() == FIRST
    assert row["native_conversation_id"] == SESSION + ":task:maker"
    assert case.pc.snapshot(team, "maker")["workspace_root"] == row["workdir"]
    assert not case.pc.snapshot(team).get("workspace_root")
    node = case.teams.load(team)["tasks"]["maker"]
    assert node["active_attempt"] is None and node["result"]["artifacts"] == row["artifacts"]


def test_actual_single_handler_registers_first_version_before_implicit_continuation(case):
    """Real handler/identity binding/store; transport and native reply only are fake."""
    server = importlib.import_module("server")
    case.monkeypatch.setattr(case.core, "pilot_settings", lambda *_: {"enabled": False})
    events = []
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/api/legion/run_one"
    handler._personal_user = lambda: OWNER
    handler._sse_open = lambda: None
    handler._sse_send = lambda kind, value, **_: events.append((kind, deepcopy(value))) or True
    handler._send_json = lambda value, status=200: events.append(("json", (status, deepcopy(value))))

    def invoke(team):
        events.clear()
        body = {"key": "codex", "task": "write the fixture", "session_id": SESSION,
                "team_id": team, "execution_depth": "quick"}
        assert handler._bind_personal_request(body, allow_new=True)
        handler._handle_run_one(body)
        result = next((value["result"] for kind, value in events if kind == "final"), None)
        assert result and result["status"] == "done", events
        return result

    first = invoke("__ut_first_http_export")
    artifact, = first["artifacts"]
    assert artifact["export_snapshot_id"]
    assert case.identity.expected_export_hash(artifact["path"], OWNER, SESSION) == artifact["sha256"]
    case.content = SECOND
    second = invoke("__ut_second_http_export")
    assert second["workdir"] == first["workdir"]
    assert Path(first["workdir"], "README.md").read_bytes() == SECOND
    assert case.ad._content_safety.read_public_file(artifact["path"]) == FIRST
    for team in ("__ut_first_http_export", "__ut_second_http_export"):
        node = case.teams.load(team)["tasks"]["codex"]
        assert node["active_attempt"] is None and node["status"] == "done"
        assert node["result"]["artifacts"][0]["export_snapshot_id"]
