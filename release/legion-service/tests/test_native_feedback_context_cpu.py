"""Read-only feedback projection; all state is temporary, no native or network."""
from copy import deepcopy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
SID = "00000000-0000-4000-8000-000000000001"
OTHER_SID = "00000000-0000-4000-8000-000000000002"
USER, CONVERSATION, TASK = "__ut_owner", "__ut_order", "writer"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key, leaf in (
        ("HOME", "home"), ("CODEX_HOME", "codex"), ("TMPDIR", "tmp"),
        ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
        ("LEGION_TEAM_DIR", "teams"), ("LEGION_PROJECT_CONTEXT_DIR", "projects"),
        ("LEGION_ARTIFACT_STORE_DIR", "artifacts"),
        ("LEGION_TASK_MEMORY_DIR", "memory"),
        ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        path = tmp_path / leaf
        path.mkdir()
        monkeypatch.setenv(key, str(path))
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        raise AssertionError("network/native processes are forbidden in this CPU test")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    teams = importlib.import_module("team_state")
    projects = importlib.import_module("project_context")
    artifacts = importlib.import_module("artifact_store")
    monkeypatch.setattr(artifacts, "ROOT", tmp_path / "artifacts")
    work = tmp_path / "work"
    work.mkdir()
    root = "__ut_original"
    checkpoint = {"kind": "codex_thread", "session_id": SID}
    root_state = {
        "team_id": root, "user_id": USER, "conversation_id": CONVERSATION,
        "message_seq": 0, "mailboxes": {"commander": []},
        "tasks": {TASK: {"assigned_harness": "codex", "status": "done",
                         "result": {"native_checkpoint": checkpoint}}},
    }
    teams._write(root_state)

    def queue(text, request, team=root):
        return teams.send_message(team, "user", "commander", text,
                                  execution_request_id=request, user_id=USER)

    for i in range(1, 5):
        queue(f"old feedback {i}", f"old-{i}")
    fifth = queue("current feedback five", "current-five")
    state = teams.load(root)
    for message in state["mailboxes"]["commander"]:
        message["continuation"]["status"] = (
            "running" if message["id"] == fifth["id"] else "completed")
    teams._write(state)
    child = fifth["continuation"]["team_id"]
    packet = {
        "team_id": child, "user_id": USER, "conversation_id": CONVERSATION,
        "repo_root": str(work), "workspace_root": str(work),
        "continued_from_team_id": root,
        "user_feedback_request": {**fifth["continuation"], "message_id": fifth["id"]},
    }
    projects._atomic_write(projects._path(root), {
        "team_id": root, "user_id": USER, "conversation_id": CONVERSATION,
        "repo_root": str(work), "workspace_root": str(work),
    })
    projects._atomic_write(projects._path(child), packet)
    teams._write({
        "team_id": child, "user_id": USER, "conversation_id": CONVERSATION,
        "message_seq": 0, "mailboxes": {"commander": []},
        "orchestration_spec": {"team_policy": {"continuation_parent_team_id": root}},
        "tasks": {TASK: {"assigned_harness": "codex", "status": "running",
                         "active_attempt": 1, "lease_owner": "original-worker",
                         "lease_expires_at": 9999999999, "result": {}}},
    })
    ctx = SimpleNamespace(
        # legion_core.run_one namespaces HarnessCtx.session_id at :3597; the
        # persisted native_conversation_id at :4053 remains the logical ID.
        user_id=USER, session_id="codex:" + CONVERSATION + ":task:" + TASK, workdir=str(work),
        task_facts={"team_id": child, "task_id": TASK, "attempt_id": 1,
                    "native_session_scope": "conversation"},
        project_context=deepcopy(packet),
        task_memory={"native_checkpoint": checkpoint, "workdir": str(work),
                     "native_conversation_id": CONVERSATION + ":task:" + TASK,
                     "native_session_scope": "conversation"},
    )
    return SimpleNamespace(root=root, child=child, ctx=ctx, teams=teams, projects=projects,
                           artifacts=artifacts, work=work, tmp=tmp_path,
                           queue=queue, monkeypatch=monkeypatch)


def module():
    assert importlib.util.find_spec("native_feedback_context") is not None, (
        "missing read-only feedback projection at the existing browser result boundary")
    return importlib.import_module("native_feedback_context")


def tool_result():
    return {"content": [{"type": "image", "data": "fixture", "mimeType": "image/png"},
                        {"type": "text", "text": "existing browser observation"}],
            "isError": True, "_meta": {"original": True}}


def payload(result):
    blocks = [item["text"] for item in result["content"]
              if item.get("type") == "text"
              and item.get("text", "").startswith("MEYO_PENDING_FEEDBACK_JSON\n")]
    assert len(blocks) == 1
    return json.loads(blocks[0].split("\n", 1)[1])


def stored_bytes(case):
    return {str(path): path.read_bytes() for leaf in ("teams", "projects", "artifacts")
            for path in (case.tmp / leaf).rglob("*") if path.is_file() and ".locks" not in path.parts}


def test_later_root_feedback_reaches_next_result_without_consuming(isolated):
    case = isolated
    view = module().create(case.ctx)
    assert view is not None
    original = tool_result()
    assert view.prepare(original) == (original, ())
    sixth = case.queue("完整的新反馈：保留全部短正文。", "later-six")
    before = stored_bytes(case)
    result, keys = view.prepare(original)
    fact, = payload(result)["messages"]
    assert fact["text"] == sixth["text"]
    assert fact["sha256"] == digest(sixth["text"])
    assert (fact["source_team_id"], fact["message_id"]) == (case.root, 6)
    assert fact["queue_status"] == "pending"
    assert payload(result)["visibility_only"] is True
    assert payload(result)["execution_deduplicated"] is False
    assert result["content"][:-1] == original["content"]
    assert result["isError"] is True and result["_meta"] == original["_meta"]
    assert original == tool_result() and keys
    assert stored_bytes(case) == before
    # Preparing/failed HTTP transmission is not even a display acknowledgement.
    assert view.prepare(original) == (result, keys)
    view.note_sent(keys)
    assert view.prepare(original) == (original, ())
    assert stored_bytes(case) == before


@pytest.mark.parametrize("projected", [False, True], ids=["full-ctx", "env-projection"])
def test_real_harness_ctx_from_env_preserves_production_bindings(isolated, projected):
    case = isolated
    case.monkeypatch.syspath_prepend(str(SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"))
    registry = importlib.import_module("harness_registry")
    packet = deepcopy(case.ctx.project_context)
    refs = {}
    if projected:
        # _runtime_context_env uses this exact binding projection for large
        # isolated-worker env. Its marker must NOT authorize arbitrary reads.
        packet = registry._context_binding_view(packet, "MEYO_PROJECT_CONTEXT_JSON")
        packet["context_ref"] = {
            "path": str(case.tmp / "not-a-read-authority.json"),
            "json_pointer": "/runtime_context/fixture",
        }
        refs["MEYO_PROJECT_CONTEXT_JSON"] = packet["context_ref"]
        assert "user_feedback_request" not in packet
    for key, value in (
        ("MEYO_SESSION_ID", case.ctx.session_id), ("MEYO_USER_ID", USER),
        ("MEYO_WORKDIR", str(case.work)), ("MEYO_UNIFIED_APPID", "__ut_fixture"),
        ("MEYO_TASK_FACTS_JSON", json.dumps(case.ctx.task_facts)),
        ("MEYO_TASK_MEMORY_JSON", json.dumps(case.ctx.task_memory)),
        ("MEYO_PROJECT_CONTEXT_JSON", json.dumps(packet)),
        ("MEYO_CONTEXT_REFS_JSON", json.dumps(refs)),
    ):
        case.monkeypatch.setenv(key, value)
    ctx = registry.HarnessCtx.from_env()
    assert ctx.session_id == "codex:" + ctx.task_memory["native_conversation_id"]
    assert "assigned_harness" not in ctx.task_facts  # Owner lives in team_state.
    assert "native_context_origin" not in ctx.task_facts  # Only request scope sets it.
    case.queue("production-shaped later feedback", "six")
    view = module().create(ctx)
    assert view is not None
    result, _ = view.prepare(tool_result())
    assert payload(result)["messages"][0]["text"] == "production-shaped later feedback"


@pytest.mark.parametrize("broken", [
    "user", "conversation", "logical-session", "unprefixed-session", "task", "harness", "attempt",
    "uuid", "checkpoint-kind", "memory-workdir", "workdir", "request-scope",
    "missing-checkpoint", "project-owner", "project-team", "project-link", "context-origin",
])
def test_unbound_context_is_disabled(isolated, broken):
    case, ctx = isolated, deepcopy(isolated.ctx)
    if broken == "user":
        ctx.user_id = "foreign"
    elif broken == "conversation":
        ctx.project_context["conversation_id"] = "foreign"
    elif broken == "logical-session":
        ctx.session_id = "foreign"
    elif broken == "unprefixed-session":
        ctx.session_id = ctx.task_memory["native_conversation_id"]
    elif broken == "task":
        ctx.task_facts["task_id"] = "other-task"
    elif broken == "harness":
        ctx.task_facts["assigned_harness"] = "other-harness"
    elif broken == "attempt":
        ctx.task_facts["attempt_id"] = 2
    elif broken == "uuid":
        ctx.task_memory["native_checkpoint"]["session_id"] = OTHER_SID
    elif broken == "checkpoint-kind":
        ctx.task_memory["native_checkpoint"]["kind"] = "cc"
    elif broken == "memory-workdir":
        ctx.task_memory["workdir"] = str(case.tmp)
    elif broken == "workdir":
        ctx.workdir = str(case.tmp)
    elif broken == "request-scope":
        ctx.task_facts["native_session_scope"] = "request"
        ctx.task_memory["native_session_scope"] = "request"
    elif broken == "missing-checkpoint":
        ctx.task_memory.pop("native_checkpoint")
    elif broken == "project-owner":
        ctx.project_context["user_id"] = "foreign"
    elif broken == "project-team":
        ctx.project_context["team_id"] = "foreign"
    elif broken == "project-link":
        ctx.project_context["continued_from_team_id"] = "foreign"
    elif broken == "context-origin":
        ctx.task_facts["native_context_origin"] = {"user_id": "foreign"}
    assert module().create(ctx) is None


@pytest.mark.parametrize("broken", [
    "child-user", "child-conversation", "task-owner", "attempt",
    "parent-user", "parent-conversation", "parent-workspace", "child-workspace", "parent-owner",
    "reverse-link", "forward-link", "request-hash", "request-id",
    "duplicate-message", "parent-native",
])
def test_each_read_revalidates_owner_and_bidirectional_lineage(isolated, broken):
    case = isolated
    view = module().create(case.ctx)
    case.queue("private later feedback", "six")
    state, parent = case.teams.load(case.child), case.teams.load(case.root)
    packet, parent_packet = case.projects.load(case.child), case.projects.load(case.root)
    if broken == "child-user":
        state["user_id"] = "foreign"
    elif broken == "child-conversation":
        state["conversation_id"] = "foreign"
    elif broken == "task-owner":
        state["tasks"][TASK]["assigned_harness"] = "other"
    elif broken == "attempt":
        state["tasks"][TASK]["active_attempt"] = 2
    elif broken == "parent-user":
        parent["user_id"] = "foreign"
    elif broken == "parent-conversation":
        parent["conversation_id"] = "foreign"
    elif broken == "parent-owner":
        parent["tasks"][TASK]["assigned_harness"] = "other"
    elif broken == "parent-workspace":
        parent_packet["repo_root"] = str(case.tmp)
    elif broken == "child-workspace":
        packet["workspace_root"] = str(case.tmp)
    elif broken == "reverse-link":
        state["orchestration_spec"]["team_policy"]["continuation_parent_team_id"] = "fake"
    elif broken == "forward-link":
        packet["continued_from_team_id"] = "fake"
    elif broken == "request-hash":
        parent["mailboxes"]["commander"][4]["continuation"]["text_sha256"] = "0" * 64
    elif broken == "request-id":
        parent["mailboxes"]["commander"][4]["continuation"]["request_id"] = "fake"
    elif broken == "duplicate-message":
        parent["mailboxes"]["commander"].append(deepcopy(parent["mailboxes"]["commander"][4]))
    elif broken == "parent-native":
        parent["tasks"][TASK]["result"]["native_checkpoint"]["session_id"] = OTHER_SID
    case.teams._write(state)
    case.teams._write(parent)
    case.projects._atomic_write(case.projects._path(case.child), packet)
    case.projects._atomic_write(case.projects._path(case.root), parent_packet)
    before = stored_bytes(case)
    assert view.prepare(tool_result()) == (tool_result(), ())
    assert stored_bytes(case) == before


def test_pending_before_worker_creation_is_not_hidden_and_no_sibling_scan(isolated):
    case = isolated
    case.queue("later but already pending at worker creation", "six")
    sibling = "__ut_sibling"
    state = case.teams.load(case.root)
    state.update(team_id=sibling, message_seq=0, mailboxes={"commander": []})
    case.teams._write(state)
    case.queue("SIBLING_PRIVATE_BODY", "sibling-request", team=sibling)
    real_load = case.teams.load

    def scoped_load(team_id):
        assert team_id in (case.root, case.child), "must not scan/load a sibling or planned child"
        return real_load(team_id)

    case.monkeypatch.setattr(case.teams, "load", scoped_load)
    result, _ = module().create(case.ctx).prepare(tool_result())
    assert len(payload(result)["messages"]) == 1
    assert "SIBLING_PRIVATE_BODY" not in json.dumps(result)


def test_fake_ancestor_cannot_borrow_same_user_and_conversation(isolated):
    case = isolated
    sibling = "__ut_other_order"
    state, packet = case.teams.load(case.root), case.projects.load(case.root)
    state["team_id"], packet["team_id"] = sibling, sibling
    case.teams._write(state)
    case.projects._atomic_write(case.projects._path(sibling), packet)
    child, child_packet = case.teams.load(case.child), case.projects.load(case.child)
    child["orchestration_spec"]["team_policy"]["continuation_parent_team_id"] = sibling
    child_packet["continued_from_team_id"] = sibling
    case.ctx.project_context = deepcopy(child_packet)
    case.teams._write(child)
    case.projects._atomic_write(case.projects._path(case.child), child_packet)
    assert module().create(case.ctx) is None


@pytest.mark.parametrize("mail_policy", [False, True], ids=["ordinary-history", "broken-mail-link"])
def test_ordinary_project_history_is_not_a_mailbox_ancestor(isolated, mail_policy):
    case = isolated
    packet = case.projects.load(case.root)
    packet["continued_from_team_id"] = "__ut_unproven_ordinary_history"
    case.projects._atomic_write(case.projects._path(case.root), packet)
    if mail_policy:
        state = case.teams.load(case.root)
        state["orchestration_spec"] = {"team_policy": {
            "continuation_parent_team_id": packet["continued_from_team_id"]}}
        case.teams._write(state)
    real_load = case.teams.load

    def scoped_load(team_id):
        assert team_id in (case.root, case.child), "ordinary history is not a proven mail ancestor"
        return real_load(team_id)

    case.monkeypatch.setattr(case.teams, "load", scoped_load)
    case.queue("later feedback on the proven parent", "six")
    view = module().create(case.ctx)
    if mail_policy:
        assert view is None  # A declared mail link must have its matching request.
    else:
        assert view is not None
        result, _ = view.prepare(tool_result())
        assert payload(result)["messages"][0]["source_team_id"] == case.root


def test_no_write_apis_and_read_at_is_not_a_consumption_signal(isolated):
    case = isolated
    case.queue("already UI-read, still queued", "six")
    state = case.teams.load(case.root)
    state["mailboxes"]["commander"][-1]["read_at"] = 1234
    state["mailboxes"]["commander"][-1]["native_delivery"] = {"old": {"phase": "queued"}}
    case.teams._write(state)

    def no_write(*args, **kwargs):
        raise AssertionError("feedback projection must not write any execution/read receipt")

    for obj, attr in ((case.teams, "_write"), (case.teams, "record_continuation"),
                      (case.projects, "_atomic_write"), (case.artifacts, "put_text")):
        case.monkeypatch.setattr(obj, attr, no_write)
    view = module().create(case.ctx)
    result, keys = view.prepare(tool_result())
    assert payload(result)["messages"][0]["text"] == "already UI-read, still queued"
    view.note_sent(keys)


def test_owner_change_after_locked_read_cannot_escape_as_context(isolated):
    case = isolated
    view = module().create(case.ctx)
    case.queue("must not leak after reassignment", "six")
    real_read = case.teams.read_messages

    def changed(team_id, *args, **kwargs):
        messages = real_read(team_id, *args, **kwargs)
        if team_id == case.root:
            state = case.teams.load(case.child)
            state["tasks"][TASK]["assigned_harness"] = "foreign"
            case.teams._write(state)
        return messages

    case.monkeypatch.setattr(case.teams, "read_messages", changed)
    assert view.prepare(tool_result()) == (tool_result(), ())


def test_dedup_is_only_display_and_uses_message_id_and_body_hash(isolated):
    case = isolated
    view = module().create(case.ctx)
    case.queue("same words", "six")
    first, keys = view.prepare(tool_result())
    view.note_sent(keys)
    case.queue("same words", "seven")
    second, keys = view.prepare(tool_result())
    assert payload(first)["messages"][0]["message_id"] == 6
    assert payload(second)["messages"][0]["message_id"] == 7
    view.note_sent(keys)
    state = case.teams.load(case.root)
    state["mailboxes"]["commander"][-1]["text"] = "changed authenticated body"
    state["mailboxes"]["commander"][-1]["continuation"]["text_sha256"] = digest(
        "changed authenticated body")
    case.teams._write(state)
    third, _ = view.prepare(tool_result())
    assert payload(third)["messages"][0]["sha256"] == digest("changed authenticated body")
    assert {m["continuation"]["status"] for m in state["mailboxes"]["commander"][5:]} == {"pending"}
    # A later worker sees still-pending facts again, not an invented execution receipt.
    assert len(payload(module().create(case.ctx).prepare(tool_result())[0])["messages"]) == 2


def test_long_body_without_existing_read_grant_never_exposes_store_path(isolated):
    case = isolated
    text = "完整长正文。" * 1000
    case.queue(text, "six")
    result, _ = module().create(case.ctx).prepare(tool_result())
    fact, = payload(result)["messages"]
    assert fact["sha256"] == digest(text)
    assert fact["body_unavailable"] is True and "text" not in fact and "body_ref" not in fact
    assert str(case.artifacts.ROOT) not in json.dumps(result)
    assert text not in json.dumps(result, ensure_ascii=False)


def test_long_body_uses_only_an_existing_authorized_reference(isolated):
    case = isolated
    text = "long body " * 1000
    case.queue(text, "six")
    checked = []

    def readable_body_ref(ref):
        checked.append(ref)
        return {"uri": "https://fixture.invalid/owned/existing-mail",
                "sha256": ref["sha256"], "bytes": ref["bytes"]}

    result, _ = module().create(case.ctx, readable_body_ref=readable_body_ref).prepare(tool_result())
    fact, = payload(result)["messages"]
    assert checked and fact["sha256"] == digest(text)
    assert fact["body_ref"]["uri"].endswith("/owned/existing-mail")
    assert "text" not in fact and str(case.artifacts.ROOT) not in json.dumps(result)


@pytest.mark.parametrize("attack", [
    "outside-root", "other-team", "other-task", "file-symlink", "directory-symlink",
    "hash", "bytes", "hardlink",
])
def test_arbitrary_body_references_are_rejected_before_publication(isolated, attack):
    case = isolated
    text = "PRIVATE_REFERENCE_BODY" * 300
    case.queue(text, "six")
    state = case.teams.load(case.root)
    ref = state["mailboxes"]["commander"][-1]["artifact_refs"][-1]
    original = Path(ref["path"])
    if attack == "outside-root":
        target = case.tmp / "outside.txt"
        target.write_text(text)
        ref["path"] = str(target)
    elif attack in ("other-team", "other-task"):
        target = case.artifacts.put_text(
            "__ut_sibling" if attack == "other-team" else case.root,
            "commander" if attack == "other-team" else "foreign-task", text, "mail-6.txt")
        ref["path"] = target["path"]
    elif attack in ("file-symlink", "hardlink"):
        target = case.tmp / "outside.txt"
        target.write_text(text)
        original.unlink()
        if attack == "file-symlink":
            original.symlink_to(target)
        else:
            original.hardlink_to(target)
    elif attack == "directory-symlink":
        folder = original.parent
        moved = case.tmp / "moved"
        folder.rename(moved)
        folder.symlink_to(moved, target_is_directory=True)
    elif attack == "hash":
        ref["sha256"] = "0" * 64
    elif attack == "bytes":
        ref["bytes"] += 1
    case.teams._write(state)
    published = []
    view = module().create(case.ctx, readable_body_ref=lambda ref: published.append(ref))
    assert view is not None
    result, _ = view.prepare(tool_result())
    assert "PRIVATE_REFERENCE_BODY" not in json.dumps(result)
    assert not published


def test_projection_failure_is_nonfatal_and_preserves_original_tool_result(isolated):
    view = module().create(isolated.ctx)

    def failed(*args, **kwargs):
        raise RuntimeError("fixture read failure, not a browser failure")

    isolated.monkeypatch.setattr(isolated.teams, "read_messages", failed)
    result = tool_result()
    assert view.prepare(result) == (result, ())


def test_standalone_and_explicit_native_uuid_mismatch_are_disabled(isolated):
    assert module().create(SimpleNamespace()) is None
    assert module().create(isolated.ctx, native_session_id=OTHER_SID) is None


def test_nested_bidirectional_ancestors_only(isolated):
    case = isolated
    request = case.queue("nested current request", "nested", team=case.child)
    state = case.teams.load(case.child)
    state["mailboxes"]["commander"][0]["continuation"]["status"] = "running"
    state["tasks"][TASK].update(status="done", active_attempt=None, result={
        "native_checkpoint": {"kind": "codex_thread", "session_id": SID}})
    case.teams._write(state)
    grandchild = request["continuation"]["team_id"]
    packet = deepcopy(case.ctx.project_context)
    packet.update(team_id=grandchild, continued_from_team_id=case.child,
                  user_feedback_request={**request["continuation"], "message_id": request["id"]})
    case.projects._atomic_write(case.projects._path(grandchild), packet)
    child_state = deepcopy(state)
    child_state.update(team_id=grandchild, mailboxes={"commander": []}, message_seq=0)
    child_state["orchestration_spec"]["team_policy"]["continuation_parent_team_id"] = case.child
    child_state["tasks"][TASK].update(status="running", active_attempt=1, result={})
    case.teams._write(child_state)
    ctx = deepcopy(case.ctx)
    ctx.task_facts["team_id"], ctx.project_context = grandchild, packet
    view = module().create(ctx)
    assert view is not None
    case.queue("root later fact", "root-six")
    case.queue("parent later fact", "child-two", team=case.child)
    result, _ = view.prepare(tool_result())
    assert {(fact["source_team_id"], fact["text"]) for fact in payload(result)["messages"]} == {
        (case.root, "root later fact"), (case.child, "parent later fact")}


def test_context_bound_keeps_whole_bodies_and_later_calls_drain_only_display(isolated):
    case = isolated
    texts = [str(index) + "文" * 3000 for index in range(6)]
    for index, text in enumerate(texts):
        case.queue(text, f"batch-{index}")
    view = module().create(case.ctx)
    before = stored_bytes(case)
    seen = []
    for _ in range(7):
        result, keys = view.prepare(tool_result())
        if not keys:
            break
        assert len(result["content"][-1]["text"]) <= 12000
        seen.extend(fact["text"] for fact in payload(result)["messages"])
        view.note_sent(keys)
    assert seen == texts
    assert stored_bytes(case) == before
    assert view.prepare(tool_result()) == (tool_result(), ())


def test_later_read_grant_is_not_hidden_by_unavailable_display(isolated):
    case = isolated
    case.queue("long text " * 1000, "six")
    granted = []

    def readable(ref):
        return ({"uri": "https://fixture.invalid/owned/body", "sha256": ref["sha256"],
                 "bytes": ref["bytes"]} if granted else None)

    view = module().create(case.ctx, readable_body_ref=readable)
    first, keys = view.prepare(tool_result())
    assert payload(first)["messages"][0]["body_unavailable"]
    view.note_sent(keys)
    granted.append(True)
    second, _ = view.prepare(tool_result())
    assert "body_ref" in payload(second)["messages"][0]


@pytest.mark.parametrize("bad_ref", [
    {"uri": "https://fixture.invalid/other", "sha256": "0" * 64, "bytes": 10000},
    {"path": "/arbitrary/path", "sha256": "0" * 64, "bytes": 10000},
])
def test_reference_resolver_cannot_substitute_another_body(isolated, bad_ref):
    isolated.queue("long text " * 1000, "six")
    view = module().create(isolated.ctx, readable_body_ref=lambda _: bad_ref)
    assert view.prepare(tool_result()) == (tool_result(), ())
