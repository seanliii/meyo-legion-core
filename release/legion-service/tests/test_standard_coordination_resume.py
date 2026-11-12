"""Real loopback recovery API; only model/process boundaries are replaced."""
import ast
from contextlib import nullcontext
from copy import deepcopy
import http.client
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

SERVICE = Path(__file__).resolve().parents[1]
TEAM = "__ut_standard_control_resume"
OWNER, OTHER = "__ut_resume_owner", "__ut_other_owner"
CONVERSATION = "__ut_original_conversation"
HARNESS = "__ut_resume_member"
TOKENS = {OWNER: "__ut_owner_token_1234567890", OTHER: "__ut_other_token_1234567890"}


@pytest.fixture
def case(tmp_path, monkeypatch):
    for name, leaf in (
        ("HOME", "home"), ("CODEX_HOME", "codex-home"), ("TMPDIR", "native-tmp"),
        ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
        ("LEGION_TEAM_DIR", "teams"), ("LEGION_PROJECT_CONTEXT_DIR", "projects"),
        ("LEGION_TASK_MEMORY_DIR", "memory"), ("LEGION_SESSIONS_DIR", "sessions"),
        ("LEGION_ARTIFACT_STORE_DIR", "artifacts"), ("LEGION_USER_CONFIG_DIR", "users"),
        ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        (tmp_path / leaf).mkdir(exist_ok=True)
        monkeypatch.setenv(name, str(tmp_path / leaf))
    control = tmp_path / "operator-control"
    control.mkdir(mode=0o700)
    tokens = control / "personal-tokens.json"
    tokens.write_text(json.dumps({token: {"user": user} for user, token in TOKENS.items()}))
    tokens.chmod(0o600)
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tokens))
    monkeypatch.setenv("LEGION_API_TOKEN", "__ut_instance_token_not_a_user")
    monkeypatch.setenv("LEGION_EXT_AUTO_UPDATE", "0")
    monkeypatch.setenv("LEGION_RESUME_MAX_ATTEMPTS", "4")
    monkeypatch.delenv("LEGION_PUBLIC_ORIGIN", raising=False)
    monkeypatch.syspath_prepend(str(SERVICE))
    ports, forbidden_calls = set(), []
    connect = socket.socket.connect

    def local_only(sock, address):
        if not (isinstance(address, tuple) and address[0] == "127.0.0.1"
                and address[1] in ports):
            forbidden_calls.append("network")
            raise AssertionError("only this fixture's HTTP listener is permitted")
        return connect(sock, address)

    def no_process(*args, **kwargs):
        forbidden_calls.append("native_process")
        raise AssertionError("no real native/model/device process")

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.setattr(subprocess, "Popen", no_process)
    server = importlib.import_module("server")
    core, teams = server.LEGION, server.TEAMS
    pc, tm = importlib.import_module("project_context"), importlib.import_module("task_memory")
    monkeypatch.setattr(tm, "ROOT", tmp_path / "memory")
    monkeypatch.setattr(importlib.import_module("artifact_store"), "ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(server, "_PUBLIC_ORIGIN", None)
    monkeypatch.setattr(server, "_REMOTE_BIND_OK", False)
    monkeypatch.setattr(server, "_LEGACY_NO_ORIGIN", False)
    monkeypatch.setattr(core, "_harness_keys", lambda: {HARNESS})
    monkeypatch.setattr(core, "_aigc_ready", lambda **kw: {"runnable_clis": [HARNESS]})
    monkeypatch.setattr(core, "_autonomy_matrix", lambda: {})
    monkeypatch.setattr(importlib.import_module("model_registry"), "resolve_assignments",
                        lambda *a, **kw: {})
    for name in ("_build_prelude", "_build_runtime_brief",
                 "_read_async_deep_plans", "_captain_mail_context"):
        monkeypatch.setattr(core, name, lambda *a, **kw: "")
    monkeypatch.setattr(core, "_record_experience", lambda *a, **kw: None)
    monkeypatch.setattr(core, "_baseline_snapshot", lambda *a, **kw: None)
    monkeypatch.setattr(core, "_autocommit_delivery", lambda *a, **kw: {"status": "skipped"})
    pilot = importlib.import_module("legion_pilot")
    monkeypatch.setattr(pilot, "read_shared_goals", lambda: {})
    monkeypatch.setattr(pilot, "load_user_config", lambda *a, **kw: pilot.merge_config())

    class Lease(nullcontext):
        wait_seconds = 0

    monkeypatch.setattr(core, "_GatewayLease", lambda *a, **kw: Lease())
    members, controls, member_calls = [], [], []

    def member(key, question, **kw):
        members.append(deepcopy(kw["task_facts"]))
        member_calls.append({"key": key, "question": question, **deepcopy({
            name: kw.get(name) for name in
            ("qhash", "conversation_id", "user_id", "preferred_model", "task_facts")})})
        return {"status": "done", "final": "new task finished"}

    def control(key, question, **kw):
        controls.append({"prompt": question, "conversation_id": kw["conversation_id"],
                         "qhash": kw["qhash"], "facts": deepcopy(kw["task_facts"])})
        return {"status": "done", "final": "original coordination finished",
                "orchestration_request": {"coordinate": {"action": "complete",
                                                        "reason": "original facts consumed"}}}

    monkeypatch.setattr(core, "run_one", member)
    monkeypatch.setattr(core, "_run_one_with_gateway", control)
    listener = server.LegionServer(("127.0.0.1", 0), server.Handler)
    ports.add(listener.server_port)
    thread = threading.Thread(target=listener.serve_forever,
                              kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()

    def post(user=OWNER, path="/api/legion/team/resume", **fields):
        conn = http.client.HTTPConnection("127.0.0.1", listener.server_port, timeout=8)
        try:
            conn.request("POST", path,
                         json.dumps({"team_id": TEAM, **fields}),
                         {"Content-Type": "application/json",
                          "Authorization": "Bearer " + TOKENS[user]})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def get(path, user=OWNER, **params):
        conn = http.client.HTTPConnection("127.0.0.1", listener.server_port, timeout=8)
        try:
            headers = {"Authorization": "Bearer " + TOKENS[user]} if user else {}
            conn.request("GET", path + "?" + urlencode(params, doseq=True), headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def events(after=0, team_id=TEAM, user=OWNER):
        return get("/api/legion/events", user=user, team_id=team_id, after=after)

    yield SimpleNamespace(core=core, teams=teams, pc=pc, tm=tm, server=server,
                          mp=monkeypatch, root=tmp_path, post=post, get=get, events=events,
                          members=members, controls=controls, control=control,
                          member_calls=member_calls)
    listener.shutdown()
    listener.server_close()
    thread.join(2)
    assert not thread.is_alive()
    assert not forbidden_calls


def grant_project(case, work, *, user=OWNER):
    """Test operator authority lives outside every declared native writable root."""
    path = Path(os.environ["LEGION_PERSONAL_TOKENS_FILE"])
    for key in ("HOME", "CODEX_HOME", "TMPDIR", "MEYO_LEGION_DIR", "LEGION_HOME_DIR",
                "LEGION_PROC_REGISTRY_DIR"):
        assert not path.resolve().is_relative_to(Path(os.environ[key]).resolve())
    assert not path.resolve().is_relative_to(work.resolve())
    records = json.loads(path.read_text())
    records[TOKENS[OWNER]] = {"user": user, "project_grants": [
        {"root": str(work), "export_paths": ["."]}]}
    path.write_text(json.dumps(records))
    path.chmod(0o600)


def seed(case, *, remaining=False, decision="failed"):
    work = case.root / "work"
    work.mkdir(exist_ok=True)
    grant_project(case, work)
    tasks = {"A": {"task": "original task A", "assigned_harness": HARNESS,
                   "assignment_locked": True, "depends_on": []}}
    if remaining:
        tasks["B"] = {"task": "original pending B", "assigned_harness": HARNESS,
                      "assignment_locked": True, "depends_on": ["A"]}
    policy = {"continuous_coordination": True, "auto_resume": True}
    plan = {"captain": HARNESS, "tasks": tasks, "_continuous_coordination": True,
            "team_policy": policy, "project_context": {"repo_root": str(work)}}
    case.pc.initialize(TEAM, "original objective", plan, mode="orchestrate",
                       user_id=OWNER, conversation_id=CONVERSATION)
    case.teams.ensure(
        TEAM, "original objective", tasks,
        {key: list(node["depends_on"]) for key, node in tasks.items()}, captain=HARNESS,
        orchestration_spec={"pattern": "orchestrate", "auto_resume": True,
                            "_continuous_coordination": True, "team_policy": policy},
        user_id=OWNER, conversation_id=CONVERSATION, registered={HARNESS})
    seq = case.teams.begin_attempt(TEAM, "A", owner="fixture")
    result = {"status": "done", "final": "original evidence " * 100,
              "native_checkpoint": {"kind": "fixture", "session_id": "same-native-A"}}
    assert case.teams.finish_attempt(TEAM, "A", seq, result)
    case.pc.record_result(TEAM, "A", result, assigned_harness=HARNESS)
    proposal = {"sequence": 2, "ok": decision != "failed",
                "action": "complete" if decision == "complete" else None,
                "execution_result_ref": {"status": "timeout", "harness": HARNESS,
                                         "qhash": TEAM + "_coordinate_2",
                                         "conversation_id": CONVERSATION + ":captain-control"}}
    if decision == "failed":
        proposal["error"] = "captain_execution_failed"
    case.pc.record_coordination(TEAM, HARNESS, proposal)
    return result


@pytest.mark.parametrize("remaining", [False, True], ids=["zero-business-nodes", "one-business-node"])
def test_http_resume_preserves_original_graph_and_control_session(case, remaining):
    original = seed(case, remaining=remaining)
    status, response = case.post()
    assert status == 200, response
    assert len(case.controls) == 1, "required coordination must reach the native boundary"
    assert [r["task_id"] for r in case.members] == (["B"] if remaining else [])
    call = case.controls[0]
    assert call["conversation_id"] == CONVERSATION + ":captain-control"
    assert call["qhash"] == TEAM + "_coordinate_3"
    assert call["facts"]["project_context"]["repo_root"] == str(case.root / "work")
    assert "captain_execution_failed" in call["prompt"]
    rows = response["result"]["results"]
    assert next(r for r in rows if r["task_id"] == "A")["final"] == original["final"]
    task = case.teams.load(TEAM)["tasks"]["A"]
    assert task["attempt_seq"] == 1 and not task.get("result_history")
    assert case.pc.load(TEAM)["decisions"][-1]["action"] == "complete"


def test_original_complete_is_not_revived(case):
    seed(case, decision="complete")
    status, response = case.post()
    assert status == 200 and response["status"] == "unchanged"
    assert not case.controls and not case.members


def seed_single_feedback(case):
    original = seed(case, decision="complete")
    state = case.teams.load(TEAM)
    state["captain"] = "unselected"
    state["orchestration_spec"] = {"pattern": "single", "auto_resume": True}
    state["tasks"]["A"]["result"]["model_used"] = "__ut_original_model"
    case.teams._write(state)
    packet = case.pc.load(TEAM)
    packet.update(mode="single", captain="unselected", decisions=[],
                  verification_findings=[{"report_id": "fixture-observation-t9",
                                           "summary": "saved feedback is not execution"}])
    case.pc._atomic_write(case.pc._path(TEAM), packet)
    case.server.SESSIONS.save_turn(
        CONVERSATION, "original objective",
        {"team_id": TEAM, "results": [{"agent": HARNESS, "task_id": "A", **original}]},
        user_id=OWNER, mode="single")
    case.mp.setattr(
        importlib.import_module("model_registry"), "resolve_assignments",
        lambda plan, agents: {HARNESS: {"model": (plan.get("model_overrides") or {}).get(HARNESS)}})
    return deepcopy(case.teams.load(TEAM))


def send_feedback(case, text="fix the original artifact", request_id="__ut_feedback_1", **kw):
    return case.post(path="/api/legion/team/message", recipient="commander", text=text,
                     request_id=request_id, continue_execution=True, **kw)


def wait_for_feedback(case, request_id="__ut_feedback_1", *, team_id=TEAM):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        messages = case.teams.read_messages(team_id, "commander", mark_read=False)
        found = next((m for m in messages
                      if (m.get("continuation") or {}).get("request_id") == request_id), {})
        if (found.get("continuation") or {}).get("status") in ("completed", "failed", "held"):
            return found
        time.sleep(0.01)
    return found


def seed_discovery_team(case, team_id, *, user=OWNER, conversation=CONVERSATION):
    """A canonical turn may contain a stale/bad reference; it is not ownership."""
    case.teams.ensure(
        team_id, "discovery fixture", {"A": "saved task"}, {},
        user_id=user, conversation_id=conversation)
    case.server.SESSIONS.save_turn(
        CONVERSATION, "saved turn", {"team_id": team_id, "status": "running"},
        user_id=OWNER, mode="relay")
    return team_id


def test_running_feedback_is_discovered_and_its_existing_events_are_readable(case):
    seed_single_feedback(case)
    entered, release = threading.Event(), threading.Event()
    member = case.core.run_one

    def active_member(*args, **kwargs):
        kwargs["cb"]({"t": "tool_started", "tool": "fixture_tool", "call_id": "live-child"})
        entered.set()
        assert release.wait(5)
        return member(*args, **kwargs)

    case.mp.setattr(case.core, "run_one", active_member)
    assert send_feedback(case)[0] == 201
    try:
        assert entered.wait(3)
        session = case.server.SESSIONS.load_session(CONVERSATION, user_id=OWNER)
        child = session["turns"][-1]["team_id"]
        assert session["turns"][-1]["status"] == "running"
        code, response = case.get("/api/legion/session/teams", sid=CONVERSATION)
        assert code == 200, response
        row = next((r for r in response["teams"] if r["team_id"] == child), None)
        assert row is not None, "a bound child must not disappear because of its filename"
        assert row["active"]
        code, stream = case.events(team_id=child, after=row["replay_after"])
        assert code == 200
        assert any(r["data"].get("call_id") == "live-child" for r in stream["events"])
    finally:
        release.set()
        message = wait_for_feedback(case)
    assert message["continuation"]["status"] == "completed"
    assert len(case.member_calls) == 1, "discovery and replay must never dispatch work"


def test_discovery_filters_owned_conversation_references_before_limit(case):
    valid = [seed_discovery_team(case, f"web_{CONVERSATION}_valid_{i}") for i in range(6)]
    invalid = []
    for i, fields in enumerate([
        {"user": OTHER}, {"user": ""}, {"conversation": "__ut_other_conversation"},
        {}, {}, {},
    ]):
        tid = seed_discovery_team(case, f"web_{CONVERSATION}_invalid_{i}", **fields)
        state = case.teams.load(tid)
        state["question"] = "FOREIGN-QUESTION-MUST-NOT-LEAK"
        if i == 3:
            state["team_id"] = "__ut_mismatched_file_identity"
        case.teams._path(tid).write_text(json.dumps(state))
        if i == 4:
            case.teams._path(tid).write_text("{broken")
        elif i == 5:
            case.teams._path(tid).unlink()
        invalid.append(tid)
    code, response = case.get("/api/legion/session/teams", sid=CONVERSATION)
    assert code == 200
    assert [r["team_id"] for r in response["teams"]] == list(reversed(valid[1:]))
    assert not set(invalid).intersection(r["team_id"] for r in response["teams"])
    assert "FOREIGN-QUESTION-MUST-NOT-LEAK" not in json.dumps(response)


def test_discovery_uses_flat_and_legacy_bindings_without_directory_scan(case):
    modern = seed_discovery_team(case, "__ut_any_name_current")
    legacy = seed_discovery_team(case, f"web_{CONVERSATION}_legacy")
    sessions = case.server.SESSIONS
    session = sessions.load_session(CONVERSATION, user_id=OWNER)
    session["turns"] = [
        None, {"result": {"team_id": legacy}}, {"team_id": modern},
        {"team_id": modern}, {"team_id": "__ut_deleted_child"},
        {"result": "malformed"}, {"team_id": modern, "result": {"team_id": legacy}},
    ]
    Path(sessions._path(CONVERSATION)).write_text(json.dumps(session))
    glob = Path.glob

    def no_team_scan(path, pattern):
        assert path != case.teams._team_dir(), "discovery must follow exact references"
        return glob(path, pattern)

    case.mp.setattr(Path, "glob", no_team_scan)
    code, response = case.get("/api/legion/session/teams", sid=CONVERSATION)
    assert code == 200
    assert {r["team_id"] for r in response["teams"]} == {modern, legacy}
    assert len(response["teams"]) == 2


@pytest.mark.parametrize("user,fields,expected", [
    (None, {}, 401),
    (OTHER, {}, 403),
    (OWNER, {"uid": OTHER}, 403),
    (OWNER, {"conversation_id": "__ut_other_conversation"}, 403),
    (OWNER, {"sid": [CONVERSATION, "__ut_other_conversation"]}, 403),
])
def test_discovery_requires_verified_session_owner_and_unambiguous_aliases(case, user, fields, expected):
    seed_discovery_team(case, "__ut_bound_private_team")
    code, response = case.get(
        "/api/legion/session/teams", user=user, **{"sid": CONVERSATION, **fields})
    assert code == expected
    assert "teams" not in response


@pytest.mark.parametrize("terminal", ["final", "error"])
def test_discovered_terminal_stream_cannot_be_revived_by_heartbeat(case, terminal):
    tid = seed_discovery_team(case, "__ut_arbitrary_terminal_team")
    case.teams.append_event(tid, {"team_id": tid}, "meta")
    end = case.teams.append_event(tid, {"status": "partial"}, terminal)
    case.teams.append_event(tid, {"t": "heartbeat", "x": "late transport activity"})
    code, response = case.get("/api/legion/session/teams", sid=CONVERSATION)
    assert code == 200 and len(response["teams"]) == 1
    row = response["teams"][0]
    assert not row["active"] and row["terminal_event_id"] == end["event_id"]
    restart = case.teams.append_event(tid, {"team_id": tid}, "meta")
    _, response = case.get("/api/legion/session/teams", sid=CONVERSATION)
    row = response["teams"][0]
    assert row["active"] and row["execution_id"] != "event:1"
    assert row["replay_after"] == restart["event_id"] - 1


def test_discovery_keeps_legacy_no_meta_stream_readable_but_stale_inactive(case):
    tid = seed_discovery_team(case, "__ut_legacy_no_meta")
    event = case.teams.append_event(tid, {"t": "tool_started", "call_id": "legacy-tool"})
    stale = time.time() - 3600
    for path in (case.teams._path(tid), case.teams._event_path(tid)):
        os.utime(path, (stale, stale))
    _, response = case.get("/api/legion/session/teams", sid=CONVERSATION)
    assert len(response["teams"]) == 1
    row = response["teams"][0]
    assert not row["active"] and row["execution_id"] == "event:0"
    code, stream = case.events(team_id=tid, after=row["replay_after"])
    assert code == 200 and stream["events"][0]["event_id"] == event["event_id"]


@pytest.mark.parametrize("transport", ["normal", "early_callback", "no_callback"])
def test_feedback_persists_one_meta_before_any_child_step_or_final(case, transport):
    seed_single_feedback(case)
    runner = case.core.dispatch_persistent

    def run_with_transport(*args, **kwargs):
        if transport == "early_callback":
            kwargs["cb"]({"t": "reason", "x": "team has not been created yet"})
        elif transport == "no_callback":
            kwargs["cb"] = None
        return runner(*args, **kwargs)

    case.mp.setattr(case.core, "dispatch_persistent", run_with_transport)
    assert send_feedback(case)[0] == 201
    message = wait_for_feedback(case)
    assert message["continuation"]["status"] == "completed"
    child = message["continuation"]["team_id"]
    code, response = case.events(team_id=child)
    assert code == 200
    rows = response["events"]
    assert rows[0]["event"] == "meta", "meta written before ensure was silently lost"
    assert [r["event"] for r in rows if r["event"] != "step"] == ["meta", "final"]
    assert rows[0]["data"]["team_id"] == child
    assert rows[0]["data"]["sid"] == CONVERSATION
    assert rows[0]["data"]["selected_mode"] == "relay", "feedback's selected route must survive reload"
    lifecycle = case.teams.load(child)["stream_lifecycle"]
    assert lifecycle["start_event_id"] < lifecycle["terminal_event_id"]


def test_feedback_meta_preserves_original_persistent_route(case):
    seed(case, decision="complete")
    assert send_feedback(case)[0] == 201
    message = wait_for_feedback(case)
    assert message["continuation"]["status"] == "completed", message
    code, stream = case.events(team_id=message["continuation"]["team_id"])
    assert code == 200
    assert stream["events"][0]["event"] == "meta"
    assert stream["events"][0]["data"]["selected_mode"] == "orchestrate"
    assert len(case.controls) == 1 and not case.member_calls


def test_feedback_concurrent_callbacks_share_one_persisted_stream_start(case):
    seed_single_feedback(case)
    runner, append = case.core.dispatch_persistent, case.teams.append_event

    def slow_meta(team_id, event, event_type="step"):
        if event_type == "meta":
            time.sleep(0.02)  # Expose a check/write race without replacing persistence.
        return append(team_id, event, event_type)

    def concurrent_transport(*args, **kwargs):
        callback, kwargs["cb"] = kwargs["cb"], None
        out = runner(*args, **kwargs)  # Keep actual team creation and completion.
        barrier, failures = threading.Barrier(8), []

        def emit(index):
            try:
                barrier.wait(timeout=2)
                callback({"t": "reason", "x": f"concurrent-event-{index}"})
            except Exception as exc:
                failures.append(exc)

        workers = [threading.Thread(target=emit, args=(index,), daemon=True)
                   for index in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        assert not any(worker.is_alive() for worker in workers)
        assert not failures
        return out

    case.mp.setattr(case.teams, "append_event", slow_meta)
    case.mp.setattr(case.core, "dispatch_persistent", concurrent_transport)
    assert send_feedback(case)[0] == 201
    message = wait_for_feedback(case)
    assert message["continuation"]["status"] == "completed"
    code, response = case.events(team_id=message["continuation"]["team_id"])
    assert code == 200
    rows = response["events"]
    assert [row["event"] for row in rows] == ["meta"] + ["step"] * 8 + ["final"]
    assert {row["data"]["x"] for row in rows[1:-1]} == {
        f"concurrent-event-{index}" for index in range(8)}


def test_feedback_retries_stream_start_when_event_store_returns_none(case):
    seed_single_feedback(case)
    append, skipped = case.teams.append_event, False

    def temporarily_unavailable(team_id, event, event_type="step"):
        nonlocal skipped
        if event_type == "meta" and case.teams.load(team_id) and not skipped:
            skipped = True
            return None
        return append(team_id, event, event_type)

    case.mp.setattr(case.teams, "append_event", temporarily_unavailable)
    assert send_feedback(case)[0] == 201
    message = wait_for_feedback(case)
    assert message["continuation"]["status"] == "completed"
    code, response = case.events(team_id=message["continuation"]["team_id"])
    assert code == 200
    rows = response["events"]
    assert rows[0]["event"] == "meta"
    assert [row["event"] for row in rows if row["event"] != "step"] == ["meta", "final"]
    assert skipped, "the transient event-store failure must have been exercised"


def test_feedback_exception_terminates_the_child_stream_not_the_parent(case):
    seed_single_feedback(case)
    runner = case.core.dispatch_persistent

    def fail_after_team_creation(*args, **kwargs):
        runner(*args, **kwargs)
        raise RuntimeError("PRIVATE-RUNNER-DETAILS")

    case.mp.setattr(case.core, "dispatch_persistent", fail_after_team_creation)
    assert send_feedback(case)[0] == 201
    message = wait_for_feedback(case)
    assert message["continuation"]["status"] == "held"
    child = message["continuation"]["team_id"]
    code, response = case.events(team_id=child)
    assert code == 200
    terminals = [r for r in response["events"] if r["event"] != "step"]
    assert [r["event"] for r in terminals] == ["meta", "error"]
    assert terminals[-1]["data"]["error"] == "RuntimeError"
    assert "PRIVATE-RUNNER-DETAILS" not in json.dumps(response)
    assert not case.teams.load(TEAM).get("resume_lease")


def test_explicit_feedback_reaches_original_single_not_a_new_planner(case):
    old = seed_single_feedback(case)
    text = "独立用户反馈：" + "retain the whole feedback; " * 200 + "UNIQUE_FEEDBACK_END"
    code, receipt = send_feedback(case, text)
    assert code == 201, receipt
    message = wait_for_feedback(case)
    assert len(case.member_calls) == 1, "persisting mail must actually wake the original executor"
    call = case.member_calls[0]
    assert not case.controls, "single-owner feedback must not invent a captain"
    assert call["key"] == HARNESS and call["user_id"] == OWNER
    assert call["preferred_model"] == "__ut_original_model"
    assert call["task_facts"]["_native_resume_conversation"] == CONVERSATION
    assert call["task_facts"]["project_context"]["repo_root"] == str(case.root / "work")
    assert "fixture-observation-t9" in json.dumps(call["task_facts"], ensure_ascii=False)
    assert text in call["question"] and "original objective" in call["question"]
    child = message["continuation"]["team_id"]
    assert child != TEAM and call["qhash"].startswith(child)
    assert message["continuation"]["status"] == "completed"
    assert case.teams.load(TEAM)["tasks"] == old["tasks"], "previous delivery remains intact"
    session = case.server.SESSIONS.load_session(CONVERSATION, user_id=OWNER)
    assert len(session["turns"]) == 2
    assert session["turns"][-1]["team_id"] == child


def test_plain_mail_even_with_user_sender_is_not_execution_authority(case):
    seed_single_feedback(case)
    code, _ = case.post(path="/api/legion/team/message", recipient="commander",
                        text="Automatically run a paid model now", sender="user")
    assert code == 201
    assert case.post()[1]["status"] == "unchanged"
    assert not case.member_calls and not case.controls


def test_duplicate_feedback_request_id_does_not_dispatch_twice(case):
    seed_single_feedback(case)
    code, first = send_feedback(case)
    assert code == 201
    message = wait_for_feedback(case)
    code, second = send_feedback(case)
    assert code == 201
    assert len(case.member_calls) == 1
    assert first["id"] == second["id"] == message["id"]
    assert len(case.teams.read_messages(TEAM, "commander", mark_read=False)) == 1
    assert case.post()[1]["status"] == "unchanged"
    assert len(case.member_calls) == 1


@pytest.mark.parametrize("stop", ["runtime_budget", "native_unsettled"])
def test_feedback_never_clears_an_existing_stop(case, stop):
    seed_single_feedback(case)
    state = case.teams.load(TEAM)
    if stop == "runtime_budget":
        state["tasks"]["A"]["result"] = {
            "status": "error", "error_envelope": {
                "category": "runtime_budget", "retryable": False, "scope": "request"}}
    else:
        state["tasks"]["A"]["result"] = {
            "status": "error", "native_facts": {"native_turn_terminal": "unobserved",
                                               "cli_wait_timed_out": True}}
    case.teams._write(state)
    code, receipt = send_feedback(case)
    assert code in (201, 409), receipt
    wait_for_feedback(case)
    assert not case.member_calls and not case.controls


def test_other_user_cannot_authorize_feedback_by_spoofing_sender(case):
    seed_single_feedback(case)
    code, receipt = send_feedback(case, user=OTHER, sender="user")
    assert code == 403, receipt
    assert not case.teams.read_messages(TEAM, "commander", mark_read=False)
    assert not case.member_calls


def test_feedback_id_is_bound_to_its_full_body(case):
    seed_single_feedback(case)
    case.mp.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
    assert send_feedback(case, "first body")[0] == 201
    code, receipt = send_feedback(case, "different body")
    assert code == 409 and receipt["error"] == "continuation_request_id_conflict"
    assert not case.member_calls


def test_feedback_restart_consumes_saved_action_once_not_read_receipts(case):
    seed_single_feedback(case)
    with case.mp.context() as stopped:
        stopped.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
        assert send_feedback(case)[0] == 201
    case.teams.read_messages(TEAM, "commander", mark_read=True)
    assert not case.member_calls
    run_startup_resume(case)
    assert len(case.member_calls) == 1
    run_startup_resume(case)
    assert len(case.member_calls) == 1
    assert not case.teams.list_auto_resumable()


@pytest.mark.parametrize("parent_field", ["team_policy", "legacy"])
def test_startup_resumes_a_childs_own_saved_feedback_once(case, parent_field):
    """An origin marker must not discard a separately authorized user turn."""
    seed_single_feedback(case)
    assert send_feedback(case)[0] == 201
    first = wait_for_feedback(case)
    assert first["continuation"]["status"] == "completed"
    child = first["continuation"]["team_id"]
    run_startup_resume(case)
    assert not case.teams.load(TEAM).get("resume_lease")
    if parent_field == "legacy":
        state = case.teams.load(child)
        spec = state["orchestration_spec"]
        spec["continuation_parent_team_id"] = spec["team_policy"].pop(
            "continuation_parent_team_id")
        case.teams._write(state)

    request_id = "__ut_child_feedback"
    with case.mp.context() as stopped:
        stopped.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
        code, saved = send_feedback(
            case, "continue the child artifact", request_id=request_id, team_id=child)
        assert code == 201, saved
    case.teams.read_messages(child, "commander", mark_read=True)
    assert len(case.member_calls) == 1
    run_startup_resume(case, team_ids=(TEAM, child))
    receipt = wait_for_feedback(case, request_id, team_id=child)
    assert receipt["continuation"]["status"] == "completed", receipt
    assert len(case.member_calls) == 2
    call = case.member_calls[-1]
    assert call["key"] == HARNESS and call["user_id"] == OWNER
    assert call["preferred_model"] == "__ut_original_model"
    assert call["task_facts"]["_native_resume_conversation"] == CONVERSATION
    assert call["task_facts"]["project_context"]["repo_root"] == str(case.root / "work")
    assert "continue the child artifact" in call["question"]
    assert call["qhash"].startswith(receipt["continuation"]["team_id"])
    code, duplicate = send_feedback(
        case, "continue the child artifact", request_id=request_id, team_id=child)
    assert code == 201 and duplicate["id"] == saved["id"]
    run_startup_resume(case, team_ids=(TEAM, child))
    assert len(case.member_calls) == 2, "restart and request replay must not rerun either turn"
    assert not case.teams.pending_continuation(case.teams.load(child))
    assert not case.teams.load(child).get("resume_lease")


def test_startup_does_not_take_a_running_child_from_its_parent(case):
    seed_single_feedback(case)
    entered, release = threading.Event(), threading.Event()
    member, started = case.core.run_one, []

    def active_member(*args, **kwargs):
        started.append(kwargs["qhash"])
        if len(started) == 1:
            entered.set()
            assert release.wait(5)
        return member(*args, **kwargs)

    case.mp.setattr(case.core, "run_one", active_member)
    try:
        code, first = send_feedback(case)
        assert code == 201 and entered.wait(3)
        child = first["continuation"]["team_id"]
        assert case.teams.load(TEAM).get("resume_lease")
        # No new action: the child's original work still belongs to the parent.
        run_startup_resume(case, team_ids=())
        with case.mp.context() as stopped:
            stopped.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
            assert send_feedback(
                case, "after the running turn", request_id="__ut_after_active",
                team_id=child)[0] == 201
        # Even its own new action must wait for the in-flight native turn.
        run_startup_resume(case, team_ids=())
        assert len(started) == 1
    finally:
        release.set()
        finished = wait_for_feedback(case)
    assert finished["continuation"]["status"] == "completed"
    run_startup_resume(case, team_ids=(TEAM, child))
    assert wait_for_feedback(
        case, "__ut_after_active", team_id=child)["continuation"]["status"] == "completed"
    assert len(started) == len(case.member_calls) == 2


@pytest.mark.parametrize("stop", [
    "cancelled", "runtime_budget", "native_unsettled", "canonical_owner",
])
def test_child_restart_keeps_existing_stop_and_owner_checks(case, stop):
    seed_single_feedback(case)
    assert send_feedback(case)[0] == 201
    child = wait_for_feedback(case)["continuation"]["team_id"]
    run_startup_resume(case)
    with case.mp.context() as stopped:
        stopped.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
        assert send_feedback(
            case, request_id="__ut_stopped_child", team_id=child)[0] == 201
    state = case.teams.load(child)
    if stop == "cancelled":
        state["cancel_requests"] = [{"key": "__all__"}]
    elif stop == "runtime_budget":
        state["tasks"]["A"]["result"] = {
            "status": "error", "error_envelope": {
                "category": "runtime_budget", "retryable": False, "scope": "request"}}
    elif stop == "native_unsettled":
        state["tasks"]["A"]["result"] = {
            "status": "error", "native_facts": {
                "native_turn_terminal": "unobserved", "cli_wait_timed_out": True}}
    else:
        packet = case.pc.load(child)
        packet["user_id"] = OTHER
        case.pc._atomic_write(case.pc._path(child), packet)
    case.teams._write(state)
    run_startup_resume(case, team_ids=(TEAM, child))
    assert len(case.member_calls) == 1 and not case.controls
    request = next(
        message["continuation"]
        for message in case.teams.read_messages(child, "commander", mark_read=False)
        if (message.get("continuation") or {}).get("request_id") == "__ut_stopped_child")
    assert request["status"] != "completed"
    assert not case.teams.load(child).get("resume_lease")


def test_startup_replays_child_feedback_receipt_without_another_native_turn(case):
    seed_single_feedback(case)
    assert send_feedback(case)[0] == 201
    child = wait_for_feedback(case)["continuation"]["team_id"]
    run_startup_resume(case)
    assert send_feedback(case, request_id="__ut_child_receipt", team_id=child)[0] == 201
    assert wait_for_feedback(
        case, "__ut_child_receipt", team_id=child)["continuation"]["status"] == "completed"
    run_startup_resume(case, team_ids=(TEAM, child))
    state = case.teams.load(child)
    request = next(message["continuation"] for message in state["mailboxes"]["commander"]
                   if (message.get("continuation") or {}).get("request_id") == "__ut_child_receipt")
    request["status"] = "running"
    case.teams._write(state)
    run_startup_resume(case, team_ids=(TEAM, child))
    receipt = wait_for_feedback(case, "__ut_child_receipt", team_id=child)
    assert receipt["continuation"]["status"] == "completed", receipt
    assert len(case.member_calls) == 2, "the persisted terminal result is the recovery receipt"
    session = case.server.SESSIONS.load_session(CONVERSATION, user_id=OWNER)
    assert len(session["turns"]) == 3


@pytest.mark.parametrize("parent_field", ["team_policy", "legacy"])
def test_startup_does_not_resume_child_original_work_from_plain_mail(case, parent_field):
    seed_single_feedback(case)
    assert send_feedback(case)[0] == 201
    child = wait_for_feedback(case)["continuation"]["team_id"]
    run_startup_resume(case)
    assert case.post(
        path="/api/legion/team/message", team_id=child, recipient="commander",
        sender="user", text="ordinary context is not a new execution request")[0] == 201
    state = case.teams.load(child)
    # Runnable original work still belongs to the parent's continuation. Neither
    # auto_resume nor ordinary mail transfers that ownership to a startup worker.
    state["tasks"]["A"].update(status="pending", active_attempt=None)
    spec = state["orchestration_spec"]
    spec["auto_resume"] = True
    if parent_field == "legacy":
        spec["continuation_parent_team_id"] = spec["team_policy"].pop(
            "continuation_parent_team_id")
    case.teams._write(state)
    from legion_contracts import resume_facts_from_state
    assert resume_facts_from_state(state)["eligible"], "exercise the parent-origin filter"
    assert not case.teams.pending_continuation(state)
    assert child not in {row["team_id"] for row in case.teams.list_auto_resumable()}
    run_startup_resume(case, team_ids=(TEAM, child))
    assert len(case.member_calls) == 1 and not case.controls
    assert case.teams.load(child)["tasks"]["A"]["status"] == "pending"


def test_feedback_crash_after_native_return_replays_receipt_not_model(case):
    seed_single_feedback(case)
    send_feedback(case)
    message = wait_for_feedback(case)
    assert message["continuation"]["status"] == "completed"
    state = case.teams.load(TEAM)
    state["mailboxes"]["commander"][0]["continuation"]["status"] = "running"
    case.teams._write(state)
    run_startup_resume(case)
    assert len(case.member_calls) == 1
    assert len(case.server.SESSIONS.load_session(CONVERSATION, user_id=OWNER)["turns"]) == 2
    assert case.teams.read_messages(TEAM, "commander", mark_read=False)[0]["continuation"]["status"] == "completed"


def test_feedback_rechecks_revoked_directory_grant_after_claim(case):
    seed_single_feedback(case)
    claim = case.teams.claim_resume

    def revoke(*a, **kw):
        admitted = claim(*a, **kw)
        path = Path(os.environ["LEGION_PERSONAL_TOKENS_FILE"])
        records = json.loads(path.read_text())
        records[TOKENS[OWNER]]["project_grants"] = []
        path.write_text(json.dumps(records))
        return admitted

    case.mp.setattr(case.teams, "claim_resume", revoke)
    send_feedback(case)
    receipt = wait_for_feedback(case)
    assert receipt["continuation"]["status"] == "held"
    assert not case.member_calls


def test_feedback_for_existing_captain_does_not_repeat_completed_members(case):
    seed(case, decision="complete")
    send_feedback(case, "unique explicit review of the existing work")
    message = wait_for_feedback(case)
    assert message["continuation"]["status"] == "completed", message
    assert not case.member_calls
    assert len(case.controls) == 1
    assert case.controls[0]["conversation_id"] == CONVERSATION + ":captain-control"
    assert "unique explicit review of the existing work" in case.controls[0]["prompt"]


def test_feedback_during_another_feedback_waits_instead_of_double_dispatch(case):
    seed_single_feedback(case)
    entered, release = threading.Event(), threading.Event()
    member = case.core.run_one

    def blocking(*a, **kw):
        entered.set()
        assert release.wait(3)
        return member(*a, **kw)

    case.mp.setattr(case.core, "run_one", blocking)
    try:
        assert send_feedback(case, "first")[0] == 201
        assert entered.wait(2)
        assert send_feedback(case, "second", request_id="__ut_feedback_2")[0] == 201
        assert not case.member_calls
    finally:
        release.set()
    assert wait_for_feedback(case)["continuation"]["status"] == "completed"
    assert wait_for_feedback(case, "__ut_feedback_2")["continuation"]["status"] == "completed"
    assert len(case.member_calls) == 2


def test_feedback_arriving_between_empty_snapshot_and_worker_exit_is_not_lost(case):
    seed_single_feedback(case)
    pending = case.teams.pending_continuation
    injected, contenders = threading.Event(), []

    def at_empty_snapshot(state):
        message = pending(state)
        if (state.get("team_id") == TEAM and message is None
                and threading.current_thread().name == f"feedback:{TEAM}"
                and not injected.is_set()):
            injected.set()
            case.teams.send_message(
                TEAM, sender="user", recipient="commander", text="second at worker exit",
                execution_request_id="__ut_feedback_exit", user_id=OWNER)
            producer = threading.Thread(target=case.server._schedule_feedback_resume,
                                        args=(TEAM, OWNER, CONVERSATION))
            contenders.append(producer)
            producer.start()
            # Force the old lost-wakeup ordering. With an atomic idle handoff,
            # the producer must instead wait for the worker's registry lock.
            if not case.server._feedback_workers_lock.locked():
                producer.join(2)
                assert not producer.is_alive()
        return message

    case.mp.setattr(case.teams, "pending_continuation", at_empty_snapshot)
    try:
        assert send_feedback(case)[0] == 201
        assert injected.wait(2)
        receipt = wait_for_feedback(case, "__ut_feedback_exit")
        assert receipt["continuation"]["status"] == "completed", receipt
        assert len(case.member_calls) == 2
    finally:
        for producer in contenders:
            producer.join(2)


def test_startup_drains_all_saved_feedback_not_only_first(case):
    seed_single_feedback(case)
    with case.mp.context() as stopped:
        stopped.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
        assert send_feedback(case, "first")[0] == 201
        assert send_feedback(case, "second", request_id="__ut_feedback_2")[0] == 201
    run_startup_resume(case)
    assert len(case.member_calls) == 2
    assert not case.teams.pending_continuation(case.teams.load(TEAM))
    run_startup_resume(case)
    assert len(case.member_calls) == 2


def test_startup_recovers_dead_parent_resume_owner_before_lease_expiry(case):
    seed_single_feedback(case)
    with case.mp.context() as stopped:
        stopped.setattr(case.server, "_schedule_feedback_resume", lambda *a: None)
        assert send_feedback(case)[0] == 201
    state = case.teams.load(TEAM)
    state["resume_lease"] = {"owner": "server:999999999:42:7",
                             "expires_at": time.time() + 3600}
    case.teams._write(state)
    run_startup_resume(case)
    assert len(case.member_calls) == 1
    assert not case.teams.pending_continuation(case.teams.load(TEAM))
    assert not case.teams.load(TEAM).get("resume_lease")


def test_live_resume_owner_with_request_suffix_is_not_a_dead_process(case):
    seed_single_feedback(case)
    owner = f"server:{os.getpid()}:{threading.get_ident()}:123"
    assert case.teams._owner_process_alive(owner)
    state = case.teams.load(TEAM)
    state["resume_lease"] = {"owner": owner, "expires_at": time.time() + 3600}
    case.teams._write(state)
    assert case.teams.claim_resume(TEAM, "different-owner") is False
    assert case.teams.list_auto_resumable() == []


def test_feedback_on_older_card_retains_new_source_review_and_latest_artifacts(case):
    seed_single_feedback(case)
    assert send_feedback(case)[0] == 201
    first = wait_for_feedback(case)
    child = first["continuation"]["team_id"]
    packet = case.pc.load(TEAM)
    packet.setdefault("verification_findings", []).append({
        "report_id": "__ut_new_source_review", "summary": "new observation on the original work"})
    case.pc._atomic_write(case.pc._path(TEAM), packet)
    latest = case.pc.load(child)
    latest["completed"] = [{"task_id": "A", "summary": "newest work already exists"}]
    case.pc._atomic_write(case.pc._path(child), latest)
    assert send_feedback(case, "use the new review", request_id="__ut_feedback_2")[0] == 201
    assert wait_for_feedback(case, "__ut_feedback_2")["continuation"]["status"] == "completed"
    context = case.member_calls[-1]["task_facts"]["project_context"]
    assert "__ut_new_source_review" in json.dumps(context)
    assert "newest work already exists" in json.dumps(context)


def test_new_feedback_on_old_card_cannot_bypass_a_newer_native_pause(case):
    seed_single_feedback(case)
    send_feedback(case)
    message = wait_for_feedback(case)
    child = message["continuation"]["team_id"]
    state = case.teams.load(child)
    state["tasks"]["A"]["result"] = {
        "status": "error", "native_facts": {"native_turn_terminal": "unobserved",
                                           "cli_wait_timed_out": True}}
    case.teams._write(state)
    assert send_feedback(case, "try again", request_id="__ut_feedback_2")[0] == 201
    second = wait_for_feedback(case, "__ut_feedback_2")
    assert second["continuation"]["status"] == "held"
    assert len(case.member_calls) == 1


@pytest.mark.parametrize("mismatch", ["request_owner", "canonical_owner", "canonical_conversation"])
def test_foreign_context_never_reaches_native(case, mismatch):
    seed(case)
    if mismatch != "request_owner":
        packet = case.pc.load(TEAM)
        packet["user_id" if mismatch == "canonical_owner" else "conversation_id"] = "different"
        case.pc._atomic_write(case.pc._path(TEAM), packet)
    status, _ = case.post(user=OTHER if mismatch == "request_owner" else OWNER)
    assert status == 403
    assert not case.controls and not case.members


@pytest.mark.parametrize("when", ["before_claim", "after_claim"])
def test_revoked_project_grant_cannot_resume_from_owned_canonical_context(case, when):
    seed(case)
    identity = importlib.import_module("personal_identity")
    work = case.root / "work"
    assert identity.authorized_project_path(OWNER, str(work), conversation_id=CONVERSATION)
    control = Path(os.environ["LEGION_PERSONAL_TOKENS_FILE"])
    before = case.teams._path(TEAM).read_bytes()
    claims, runners = [], []
    claim = case.teams.claim_resume

    def revoke():
        records = json.loads(control.read_text())
        records[TOKENS[OWNER]].pop("project_grants")
        control.write_text(json.dumps(records))

    def claim_then_revoke(*args, **kwargs):
        claims.append(True)
        accepted = claim(*args, **kwargs)
        if accepted and when == "after_claim":
            revoke()
        return accepted

    def forbidden_runner(*args, **kwargs):
        runners.append(True)
        raise AssertionError("revoked project grant must be rejected before the runner")

    case.mp.setattr(case.teams, "claim_resume", claim_then_revoke)
    case.mp.setattr(case.core, "dispatch_persistent", forbidden_runner)
    case.mp.setattr(case.core, "dispatch", forbidden_runner)
    if when == "before_claim":
        revoke()
    status, response = case.post()
    assert status == 403, response
    assert not runners and not case.members and not case.controls
    assert not case.teams.load(TEAM).get("resume_lease")
    if when == "before_claim":
        assert not claims and case.teams._path(TEAM).read_bytes() == before


@pytest.mark.parametrize("stop", [
    "cancelled", "authorization", "output_limit", "native_unsettled", "runtime_budget",
    "live_attempt", "live_resume",
])
def test_stop_facts_take_priority_over_an_explicit_http_resume(case, stop):
    seed(case, remaining=True)
    state = case.teams.load(TEAM)
    if stop == "cancelled":
        state["cancel_requests"] = [{"key": "__all__", "scope": "all", "reason": "user stop"}]
    elif stop == "live_attempt":
        state["tasks"]["B"].update(status="running", active_attempt=1,
                                  lease_owner=f"server:{os.getpid()}",
                                  lease_expires_at=time.time() + 300)
    elif stop == "live_resume":
        state["resume_lease"] = {"owner": f"server:{os.getpid()}",
                                 "expires_at": time.time() + 300}
    else:
        result = {"status": "error", "final": ""}
        if stop == "native_unsettled":
            result["native_facts"] = {"native_turn_terminal": "unobserved",
                                      "cli_wait_timed_out": True}
        else:
            result["error_envelope"] = {
                "category": ("authorization_required" if stop == "authorization"
                             else "runtime_budget" if stop == "runtime_budget" else "output_limit"),
                "scope": "provider" if stop == "authorization" else "request",
                "code": "appid_disabled" if stop == "authorization" else "max_output_tokens",
                "retryable": False, "message": "fixture paused",
            }
        case.teams.append_event(TEAM, {
            "coordination": {"sequence": 2, "ok": False, "result": result},
            "results": [],
        }, "final")
        state = case.teams.load(TEAM)
    case.teams._write(state)
    status, response = case.post()
    assert status in (200, 409), response
    assert not case.controls and not case.members, stop


def test_missing_indexed_terminal_receipt_is_not_a_clean_pause_check(case):
    seed(case, remaining=True)
    case.teams.append_event(TEAM, {"coordination": {
        "sequence": 2, "result": {"status": "error", "native_facts": {
            "native_turn_terminal": "unobserved", "cli_wait_timed_out": True}}}}, "final")
    case.teams._event_path(TEAM).unlink()
    assert case.post()[0] in (200, 409)
    assert not case.controls and not case.members


def test_concurrent_http_resume_never_double_dispatches(case):
    seed(case)
    entered, release = threading.Event(), threading.Event()

    def blocking_control(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return case.control(*args, **kwargs)

    case.mp.setattr(case.core, "_run_one_with_gateway", blocking_control)
    first = []
    worker = threading.Thread(target=lambda: first.append(case.post()))
    worker.start()
    try:
        assert entered.wait(2), "first recovery never entered coordination"
        status, second = case.post()
        assert status in (200, 409) and "result" not in second
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert len(case.controls) == 1 and first[0][0] == 200


def test_http_resume_exposes_public_steps_before_returning_and_pairs_meta_final(case):
    seed(case)
    previous = case.teams.append_event(TEAM, {"status": "partial"}, "final")
    entered, release = threading.Event(), threading.Event()

    def progressing_control(*args, **kwargs):
        callback = kwargs["cb"]
        callback({"t": "tool_started", "tool": "fixture_tool", "call_id": "public-call"})
        callback({"t": "usage", "usage": {"input_tokens": 7, "output_tokens": 3}})
        callback({"t": "heartbeat", "x": "HEARTBEAT-NOT-PERSISTED"})
        callback({"t": "reason", "hb": True, "x": "HEARTBEAT-NOT-PERSISTED"})
        callback({"t": "runtime_activity", "call_id": "private-call", "phase": "bytes",
                  "budget_s": 10, "x": "PRIVATE-FRAME-NOT-PERSISTED"})
        callback({"t": "harness_event", "protocol": "cli", "kind": "output",
                  "x": "PRIVATE-OUTPUT-NOT-PERSISTED"})
        callback({"t": "artifact_ready", "path": str(case.root / "state/traces/private.txt"),
                  "x": "PRIVATE-ARTIFACT-NOT-PERSISTED"})
        entered.set()
        assert release.wait(5)
        return case.control(*args, **kwargs)

    case.mp.setattr(case.core, "_run_one_with_gateway", progressing_control)
    response = []
    worker = threading.Thread(target=lambda: response.append(case.post()))
    worker.start()
    try:
        assert entered.wait(3)
        status, current = case.events(after=previous["event_id"])
        assert status == 200
        rows = current["events"]
        assert rows and rows[0]["event"] == "meta"
        assert {"tool_started", "usage"} <= {r["data"].get("t") for r in rows}
        assert not any(r["event"] in ("final", "error") for r in rows)
        assert "NOT-PERSISTED" not in json.dumps(rows)
        lifecycle = case.teams.load(TEAM)["stream_lifecycle"]
        assert lifecycle["start_event_id"] > lifecycle["terminal_event_id"]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and response[0][0] == 200
    _, completed = case.events(after=previous["event_id"])
    rows = completed["events"]
    assert [r["event"] for r in rows if r["event"] != "step"] == ["meta", "final"]
    ids = [r["event_id"] for r in rows]
    assert ids == sorted(set(ids))
    assert "NOT-PERSISTED" not in case.teams._event_path(TEAM).read_text()


def test_http_resume_exception_closes_meta_with_error_and_releases_claim(case):
    seed(case)

    def failed_control(*args, **kwargs):
        raise RuntimeError("fixture exception details are not a public progress frame")

    case.mp.setattr(case.core, "_run_one_with_gateway", failed_control)
    assert case.post()[0] == 500
    _, response = case.events()
    terminals = [r for r in response["events"] if r["event"] != "step"]
    assert [r["event"] for r in terminals] == ["meta", "error"]
    assert terminals[-1]["data"]["error"] == "RuntimeError"
    assert "fixture exception details" not in json.dumps(terminals)
    assert not case.teams.load(TEAM).get("resume_lease")


@pytest.mark.parametrize("terminal", ["completed", "failed", "paused", "cancelled"])
def test_captain_usage_records_keep_both_format_attempts_on_every_terminal(case, terminal):
    original = seed(case)
    calls, receipts = [], []

    def native(key, question, **kwargs):
        calls.append(kwargs["qhash"])
        first = len(calls) == 1
        result = {
            "key": HARNESS, "model_used": "fixture-model",
            "status": "done" if first or terminal == "completed" else
                      "cancelled" if terminal == "cancelled" else "error",
            "final": "PRIVATE-CONTENT-NOT-A-USAGE-RECORD",
            "api_key": "PRIVATE-CREDENTIAL-NOT-A-USAGE-RECORD",
            "usage": {"input_tokens": 7 if first else 4, "output_tokens": 3 if first else 1,
                      "usage_identity": f"fixture-execution-{len(calls)}",
                      "unrecognized_payload": "PRIVATE-CONTENT-NOT-A-USAGE-RECORD"},
        }
        if not first and terminal == "completed":
            result["orchestration_request"] = {"coordinate": {"action": "complete",
                                                             "reason": "original work finished"}}
        if not first and terminal == "paused":
            result["error_envelope"] = {"category": "authorization_required", "scope": "provider",
                                        "code": "appid_disabled", "retryable": False}
        return result

    case.mp.setattr(case.core, "_run_one_with_gateway", native)
    out = case.core._run_captain_coordination(
        "original objective", {"captain": HARNESS, "tasks": {"A": {"task": "original A"}}},
        TEAM, {"A": original}, [], phase="round_complete", sequence=3,
        user_id=OWNER, conversation_id=CONVERSATION,
        cb=lambda event: receipts.append(deepcopy(event)))
    records = out.get("execution_usage_records") or []
    assert [r["qhash"] for r in records] == [
        TEAM + "_coordinate_3", TEAM + "_coordinate_3_format_repair"]
    assert all(set(r) == {"qhash", "key", "model_used", "usage"} for r in records)
    assert all(r["key"] == HARNESS and r["model_used"] == "fixture-model" for r in records)
    assert [r["usage"]["usage_identity"] for r in records] == [
        "fixture-execution-1", "fixture-execution-2"]
    assert sum(r["usage"]["total_tokens"] for r in records) == 15
    assert "PRIVATE-" not in json.dumps(records)
    first_receipt = next(e["coordination"] for e in receipts if e["t"] == "coordination_result")
    assert len(first_receipt["execution_usage_records"]) == 1, "later attempts cannot rewrite history"


def test_captain_cancel_before_execution_returns_an_empty_usage_record_list(case):
    seed(case)
    case.mp.setattr(case.core, "is_cancelled", lambda *a, **kw: True)
    out = case.core._run_captain_coordination(
        "original objective", {"captain": HARNESS, "tasks": {}}, TEAM, [], [],
        phase="round_complete", sequence=3, user_id=OWNER, conversation_id=CONVERSATION)
    assert out["cancelled"] is True
    assert out.get("execution_usage_records") == []
    assert not case.controls


def run_startup_resume(case, *, team_ids=(TEAM,)):
    # Execute only the existing startup recovery function, not main/prewarm/listeners.
    tree = ast.parse(Path(case.server.__file__).read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_resume_incomplete_teams")
    env = dict(vars(case.server))
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<startup-recovery>", "exec"), env)
    env[fn.name]()
    deadline = time.monotonic() + 3
    while (any(team_id in case.server._feedback_workers for team_id in team_ids)
           and time.monotonic() < deadline):
        time.sleep(0.01)
    assert not any(team_id in case.server._feedback_workers for team_id in team_ids), (
        "startup feedback worker did not settle")


def test_startup_consumes_new_facts_once_not_its_own_failure(case):
    seed(case)
    run_startup_resume(case)
    assert not case.controls, "unchanged historical control failure is not a startup retry"
    # A real new completed result arrives after that control failure.
    changed = {"status": "done", "final": "new actual evidence"}
    case.pc.record_result(TEAM, "A", changed, assigned_harness=HARNESS)

    def fail_again(key, question, **kwargs):
        case.controls.append({"qhash": kwargs["qhash"]})
        return {"status": "timeout", "final": "", "error": "fixture failure"}

    case.mp.setattr(case.core, "_run_one_with_gateway", fail_again)
    run_startup_resume(case)
    assert len(case.controls) == 1
    run_startup_resume(case)
    assert len(case.controls) == 1, "failure receipt/lease timestamps are not new business facts"
    assert not case.members


def test_explicit_recovery_keeps_existing_resume_budget(case):
    seed(case)
    case.mp.setenv("LEGION_RESUME_MAX_ATTEMPTS", "1")

    def fail_again(key, question, **kwargs):
        case.controls.append({"qhash": kwargs["qhash"]})
        return {"status": "timeout", "final": "", "error": "fixture failure"}

    case.mp.setattr(case.core, "_run_one_with_gateway", fail_again)
    assert case.post()[0] == 200
    assert case.post()[0] in (200, 409)
    assert len(case.controls) == 1


def test_original_sequence_two_in_an_isolated_copy(case):
    """Optional local receipt fixture; never opens a production/home path itself."""
    path = os.environ.get("LEGION_RESUME_HISTORY_FIXTURE")
    if not path:
        pytest.skip("supply a read-only historical receipt copied into a temporary fixture")
    source = json.loads(Path(path).read_text())
    original = source["result"]
    assert original["coordination_incomplete"] is True
    assert original["coordination"]["sequence"] == 2
    assert original["coordination"]["error"] == "captain_execution_failed"
    plan = deepcopy(source["plan"])
    team_id = original["team_id"]
    uid, sid = source["user_id"], source["conversation_id"]
    work = case.root / "copied-original-workspace"
    work.mkdir()
    plan["project_context"] = {"repo_root": str(work)}
    plan["team_id"] = team_id
    case.mp.setattr(case.core, "_harness_keys", lambda: {"codex"})
    case.mp.setattr(case.core, "_aigc_ready", lambda **kw: {"runnable_clis": ["codex"]})
    case.mp.setattr(importlib.import_module("model_registry"), "resolve_assignments",
                    lambda value, keys: {
                        key: {"model": (value.get("model_overrides") or {}).get(key)}
                        for key in keys})
    grant_project(case, work, user=uid)
    case.pc.initialize(team_id, source["question"], plan, mode="orchestrate",
                       user_id=uid, conversation_id=sid)
    case.teams.ensure(
        team_id, source["question"], plan["tasks"], original["dependencies"],
        captain=plan["captain"], orchestration_spec=original["orchestration_spec"],
        delivery_contract=original["delivery_contract"],
        user_id=uid, conversation_id=sid, registered={"codex"})
    for row in original["results"]:
        assert row["status"] == "done"
        key = row["task_id"]
        attempt = case.teams.begin_attempt(team_id, key, owner="isolated-history-copy",
                                          dependency_ready=True)
        case.teams.finish_attempt(team_id, key, attempt, row)
        case.pc.record_result(team_id, key, row, assigned_harness=row["assigned_harness"])
    case.pc.record_coordination(team_id, plan["captain"], source["decision"])
    case.teams.append_event(team_id, original, "final")
    status, response = case.post(team_id=team_id)
    assert status == 200, response
    assert not case.members, "all six historical business results must be reused"
    assert len(case.controls) == 1
    assert case.controls[0]["qhash"] == team_id + "_coordinate_3"
    assert case.controls[0]["conversation_id"] == sid + ":captain-control"
    assert case.controls[0]["facts"]["goal"] == source["question"]
    assert {r["task_id"] for r in response["result"]["results"]} == {
        r["task_id"] for r in original["results"]}
    assert {r["task_id"]: r["final"] for r in response["result"]["results"]} == {
        r["task_id"]: r["final"] for r in original["results"]}
    assert all(t["attempt_seq"] == 1 for t in case.teams.load(team_id)["tasks"].values())
    print("HISTORICAL_HTTP_REPLAY", json.dumps({
        "original_sequence": 2, "next_sequence": 3,
        "retained_business_nodes": len(original["results"]),
        "business_native_calls": len(case.members), "control_calls": len(case.controls),
        "same_control_conversation": True,
    }))
