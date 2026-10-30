"""Operator directory grants at the real HTTP identity boundary, entirely offline."""
import ast
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
TOKEN = "fixture-personal-alice-000000"


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("LEGION_", "MEYO_")) or key == "CODEX_HOME":
            monkeypatch.delenv(key)
    for name, leaf in (
        ("HOME", "home"), ("CODEX_HOME", "codex-home"), ("TMPDIR", "native-tmp"),
        ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_TEAM_DIR", "teams"),
        ("LEGION_SESSIONS_DIR", "sessions"),
    ):
        directory = tmp_path / leaf
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.syspath_prepend(str(SERVICE))
    def forbidden(*args, **kwargs):
        raise AssertionError("directory grant tests must not use network or subprocesses")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    control = tmp_path / "control"
    # Never inherit a TMPDIR which also contains this operator control tree.
    control.mkdir(mode=0o700)
    tokens = control / "personal-tokens.json"
    tokens.write_text(json.dumps({TOKEN: {"user": "alice"}}))
    tokens.chmod(0o600)
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tokens))
    work = tmp_path / "project"
    work.mkdir()
    identity = importlib.import_module("personal_identity")
    return SimpleNamespace(identity=identity, tokens=tokens, root=tmp_path, work=work,
                           mp=monkeypatch)


def provision(case, *, root=None, exports=None):
    case.tokens.write_text(json.dumps({TOKEN: {
        "user": "alice", "project_grants": [{
            "root": str(root or case.work), "export_paths": exports if exports is not None else ["."]}]}}))


def payload(case, root=None):
    return {"uid": "alice", "team_id": "__ut_new_team", "session_id": "__ut_new_session",
            "project_context": {"repo_root": str(root or case.work)}}


def bind_http(case, body):
    """Actual Handler._bind_personal_request without constructing a live server."""
    tree = ast.parse((SERVICE / "server.py").read_text())
    handler = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Handler")
    fn = next(n for n in handler.body if isinstance(n, ast.FunctionDef)
              and n.name == "_bind_personal_request")
    from urllib.parse import urlparse
    env = {"urlparse": urlparse}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SERVICE / "server.py"), "exec"), env)
    replies = []
    request = SimpleNamespace(
        path="/api/legion/dispatch",
        _personal_user=lambda: case.identity.resolve_token(TOKEN),
        _send_json=lambda data, code: replies.append((code, data)))
    allowed = env[fn.name](request, body, allow_new=True)
    return allowed, replies


def test_authenticated_new_team_cannot_claim_arbitrary_host_root(case):
    assert case.identity.resolve_token(TOKEN) == "alice"
    allowed, replies = bind_http(case, payload(case))
    assert allowed is False
    assert replies[0][0] == 403
    pc = importlib.import_module("project_context")
    assert not pc.load("__ut_new_team"), "reject before initialize can launder caller root"


@pytest.mark.parametrize("nest", ["top", "project_context", "plan", "params"])
def test_every_accepted_root_context_is_checked(case, nest):
    values = {"workspace_root": str(case.work)}
    if nest != "top":
        values = {nest: values}
    assert not case.identity.check_request(values, "alice", allow_new=True)


def test_operator_grant_authorizes_root_not_other_owner_or_sibling(case):
    provision(case, exports=["site", "report.pdf"])
    assert bind_http(case, payload(case))[0] is True
    grant = case.identity.project_grant("alice", str(case.work))
    assert grant == {"source": "operator_project_grant", "user_id": "alice",
                     "root": str(case.work), "export_paths": ["site", "report.pdf"]}
    assert TOKEN not in repr(grant)
    assert case.identity.project_grant("bob", str(case.work)) is None
    sibling = case.root / "sibling"
    sibling.mkdir()
    assert not bind_http(case, payload(case, sibling))[0]


def test_http_grant_or_trusted_fields_cannot_authorize_root(case):
    body = payload(case)
    body["project_context"].update(trusted=True, project_grants=[
        {"root": str(case.work), "export_paths": ["."]}])
    assert not bind_http(case, body)[0]


def test_missing_root_does_not_block_normal_new_request(case):
    body = {"uid": "alice", "team_id": "__ut_new_team", "session_id": "__ut_new_session",
            "question": "ordinary new request"}
    assert bind_http(case, body)[0]


def test_existing_canonical_record_alone_cannot_authorize_explicit_root(case):
    pc = importlib.import_module("project_context")
    pc.initialize("__ut_new_team", "fixture",
                  {"tasks": {"maker": "task"}, "project_context": {"repo_root": str(case.work)}},
                  mode="orchestrate", user_id="alice", conversation_id="__ut_new_session")
    assert not bind_http(case, payload(case))[0]


@pytest.mark.parametrize("location", ["project", "home", "data"])
def test_operator_control_file_cannot_be_inside_native_writable_root(case, location):
    directory = case.work if location == "project" else case.root / location
    inside = directory / "personal-tokens.json"
    inside.write_text(json.dumps({TOKEN: {"user": "alice", "project_grants": [
        {"root": str(case.work), "export_paths": ["."]}]}}))
    inside.chmod(0o600)
    case.mp.setenv("LEGION_PERSONAL_TOKENS_FILE", str(inside))
    assert case.identity.project_grant("alice", str(case.work)) is None


@pytest.mark.parametrize("exports", [["../private"], ["/etc"], ["site/../notes"], "site"])
def test_invalid_export_scope_fails_closed(case, exports):
    provision(case, exports=exports)
    assert case.identity.project_grant("alice", str(case.work)) is None


def test_root_symlink_and_store_symlink_are_not_grants(case):
    provision(case)
    alias = case.root / "alias"
    alias.symlink_to(case.work, target_is_directory=True)
    assert not bind_http(case, payload(case, alias))[0]
    store_alias = case.root / "token-alias.json"
    store_alias.symlink_to(case.tokens)
    case.mp.setenv("LEGION_PERSONAL_TOKENS_FILE", str(store_alias))
    assert case.identity.project_grant("alice", str(case.work)) is None


def test_missing_or_revoked_operator_store_does_not_use_cache(case):
    provision(case)
    assert case.identity.project_grant("alice", str(case.work))
    case.tokens.write_text(json.dumps({TOKEN: {"user": "alice"}}))
    assert case.identity.project_grant("alice", str(case.work)) is None
    case.tokens.unlink()
    assert case.identity.project_grant("alice", str(case.work)) is None


def test_declared_root_aliases_cannot_disagree(case):
    provision(case)
    other = case.root / "other"
    other.mkdir()
    body = payload(case)
    body["project_context"]["workspace_root"] = str(other)
    assert not bind_http(case, body)[0]


def test_default_output_prefix_and_canonical_state_are_not_allocation_proof(case):
    default = case.root / "data/outputs/other-task"
    default.mkdir(parents=True)
    pc = importlib.import_module("project_context")
    pc.initialize("__ut_new_team", "fixture",
                  {"tasks": {"maker": "task"}, "project_context": {"repo_root": str(default)}},
                  mode="orchestrate", user_id="alice", conversation_id="__ut_new_session")
    assert not bind_http(case, payload(case, default))[0]


def test_saved_owned_native_default_workdir_allows_only_exact_directory(case):
    default = case.root / "data/outputs/native-task"
    default.mkdir(parents=True)
    sessions = importlib.import_module("sessions")
    sessions.save_turn("__ut_new_session", "fixture", {"results": [{
        "key": "__ut_member", "workdir": str(default),
        "native_checkpoint": {"kind": "codex_thread", "session_id": "native-fixture"},
        "native_facts": {"turn_status": "completed", "process_returncode": 0},
        "status": "done",
    }]}, user_id="alice")
    assert bind_http(case, payload(case, default))[0]
    assert case.identity.authorized_project_path(
        "alice", str(default), conversation_id="__ut_new_session")["source"] == "owned_native_default_workdir"
    assert case.identity.authorized_project_path(
        "bob", str(default), conversation_id="__ut_new_session") is None
    assert not bind_http(case, payload(case, default.parent))[0]
    sibling = default.with_name("another-task")
    sibling.mkdir()
    assert not bind_http(case, payload(case, sibling))[0]


def test_saved_external_native_path_still_requires_operator_grant(case):
    sessions = importlib.import_module("sessions")
    sessions.save_turn("__ut_new_session", "fixture", {"results": [{
        "key": "__ut_member", "workdir": str(case.work), "status": "done",
        "native_checkpoint": {"kind": "codex_thread", "session_id": "native-fixture"},
    }]}, user_id="alice")
    assert not bind_http(case, payload(case))[0]
    provision(case)
    assert bind_http(case, payload(case))[0]


def test_operator_empty_export_scope_allows_work_but_no_export(case):
    provision(case, exports=[])
    grant = case.identity.project_grant("alice", str(case.work))
    assert bind_http(case, payload(case))[0]
    assert grant["export_paths"] == []
    assert not case.identity.project_export_path_allowed(grant, ".")
    assert not case.identity.project_export_path_allowed(grant, "site/index.html")


def test_export_path_helper_cannot_widen_granted_subtree(case):
    provision(case, exports=["site", "report.pdf"])
    grant = case.identity.project_grant("alice", str(case.work))
    assert case.identity.project_export_path_allowed(grant, "site/index.html")
    assert case.identity.project_export_path_allowed(grant, "report.pdf")
    assert not case.identity.project_export_path_allowed(grant, ".")
    assert not case.identity.project_export_path_allowed(grant, "notes/private.txt")
    assert not case.identity.project_export_path_allowed(grant, "site/../notes/private.txt")


def test_password_account_registration_cannot_create_operator_grants(case):
    accounts = importlib.import_module("personal_accounts")
    case.mp.setenv("LEGION_PERSONAL_ACCOUNTS_DIR", str(case.root / "accounts"))
    case.mp.setenv("LEGION_PERSONAL_OWNER", "operator-owner")
    before = case.tokens.read_bytes()
    account, token = accounts.register("fixture-new-account", "fixture-password-only")
    assert case.tokens.read_bytes() == before
    assert case.identity.resolve_token(token) == account["user_id"]
    assert case.identity.project_grant(account["user_id"], str(case.work)) is None


def test_operator_store_hardlink_in_task_cannot_be_used_as_authority(case):
    provision(case)
    os.link(case.tokens, case.work / "editable-token-copy.json")
    assert case.identity.project_grant("alice", str(case.work)) is None


def test_explicit_invalid_default_grant_does_not_gain_broader_session_fallback(case):
    default = case.root / "data/outputs/native-task"
    default.mkdir(parents=True)
    sessions = importlib.import_module("sessions")
    sessions.save_turn("__ut_new_session", "fixture", {"results": [{
        "workdir": str(default), "native_checkpoint": {
            "kind": "codex_thread", "session_id": "native-fixture"}}]}, user_id="alice")
    provision(case, root=default, exports=["../notes"])
    assert case.identity.authorized_project_path(
        "alice", str(default), conversation_id="__ut_new_session") is None
