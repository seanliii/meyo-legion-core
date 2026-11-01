"""Auto solo: real HTTP/binder/remap/relay/core/exporter; model leaves stay offline."""
from copy import deepcopy
import hashlib
import http.client
from http.server import ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import socket
import threading

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, USER, CONVERSATION
from test_native_terminal_dispatch_graph import graph, _returned


_SOCKET_CONNECT = socket.socket.connect
_CREATE_CONNECTION = socket.create_connection
TASK = "copy_challenge"
OTHER_TASK = "existing_unrelated_task"
QUESTION = "Copy challenge.txt exactly to challenge-response.txt, then answer DONE."


def _post_auto(case, server, body):
    """Use the actual authenticated transport; deny every other network target."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    address = httpd.server_address

    def connect(sock, target):
        assert target == address, "only this test's loopback socket is allowed"
        return _SOCKET_CONNECT(sock, target)

    def create_connection(target, *args, **kwargs):
        assert target == address, "only this test's loopback socket is allowed"
        return _CREATE_CONNECTION(target, *args, **kwargs)

    case.monkeypatch.setattr(socket.socket, "connect", connect)
    case.monkeypatch.setattr(socket, "create_connection", create_connection)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01},
                              daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*address, timeout=5)
    response = None
    events = []
    try:
        connection.request("POST", "/api/legion/dispatch", json.dumps(body), headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer fixture-token-not-an-original-key",
        })
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        kind = ""
        for _ in range(2000):
            line = response.readline().decode("utf-8").strip()
            if line.startswith("event:"):
                kind = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                events.append((kind, json.loads(line.partition(":")[2])))
                if kind == "final":
                    break
        else:
            pytest.fail("auto solo HTTP stream did not return a final event")
    finally:
        if response is not None:
            response.close()
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()
    return next(data for kind, data in reversed(events) if kind == "final")


def test_auto_solo_fast_remap_then_relay_binds_actual_owner_before_native_and_export(graph):
    case, mp, core = graph, graph.monkeypatch, graph.core
    server = importlib.import_module("server")
    keys = {"codex", "cc", "hermes"}
    mp.setattr(core, "_harness_keys", lambda: set(keys))
    mp.setattr(core, "_CORE_INPROC_KEYS", set(keys))
    mp.setattr(core, "_aigc_ready", lambda **kw: {"runnable_clis": sorted(keys)})
    mp.setattr(core, "_autonomy_matrix", lambda: {
        "codex": {"cost_class": "heavy"},
        "cc": {"cost_class": "light"},
        "hermes": {"cost_class": "light"},
    })
    mp.setattr(core, "pilot_settings", lambda *a, **kw: {"enabled": False})
    mp.setattr(server, "_source_version_identity", lambda: {"commit": "offline-fixture"})
    mp.setattr(importlib.import_module("harness_performance"), "route_policy",
               lambda *a: {"verified": False, "route": "strongest_single"})
    mp.setattr(server.DESKTOP_FS, "materialize_session", lambda *a, **kw: {})
    mp.setattr(server.DESKTOP_FS, "sync_personal_data", lambda *a, **kw: None)
    # The model proposes codex. Do not patch the binder or pre-fill cc/hermes
    # in canonical tasks: the real fast remap and failover must do the wiring.
    mp.setattr(core, "plan_request_with_model", lambda *a, **kw: {
        "ok": True, "suggested_mode": "pk",
        "tasks": {TASK: {"task": QUESTION, "assigned_harness": "codex"}},
        "directions": {TASK: "quick"},
    })
    case.team = "__ut_auto_solo_actual_owner"
    initial = case.pc.initialize(case.team, QUESTION, {
        "tasks": {OTHER_TASK: {"task": "Keep the unrelated task", "assigned_harness": "codex"}},
        "project_context": {"repo_root": str(case.work)},
    }, mode="single", user_id=USER, conversation_id=CONVERSATION)
    original_other = next(row for row in initial["tasks"] if row["task_id"] == OTHER_TASK)
    grant = deepcopy(case.identity.project_grant(USER, str(case.work)))
    (case.work / "challenge.txt").write_bytes(b"preserve exact fixture bytes\r\n")
    observed = []
    native = core._run_core_inprocess

    def observe(*args, **kwargs):
        observed.append({
            "owner": args[0], "packet": case.pc.load(case.team),
            "snapshot": deepcopy(kwargs.get("project_context")),
            "facts": deepcopy(kwargs.get("task_facts")),
        })
        return native(*args, **kwargs)

    def answer(call):
        if call["key"] == "cc":
            # A fact arriving after initialization must survive the relay refresh.
            case.pc.record_coordination(case.team, TASK, {
                "sequence": 1, "action": "continue", "reason": "First native attempt failed",
            })
            return {"status": "rc1", "final": "", "error": "offline native fixture failure",
                    "workdir": str(case.work)}
        assert call["key"] == "hermes"
        (case.work / "challenge-response.txt").write_bytes((case.work / "challenge.txt").read_bytes())
        return {**_returned(case, "__ut_actual_relay_native"), "final": "DONE",
                "usage": {"input_tokens": 17, "output_tokens": 3, "total_tokens": 20}}

    case.answer = answer
    mp.setattr(core, "_run_core_inprocess", observe)
    out = _post_auto(case, server, {
        "question": QUESTION, "mode": "auto", "team_id": case.team, "session_id": CONVERSATION,
        "plan": {
            "project_context": {"repo_root": str(case.work)},
            "team_policy": {"candidate_harnesses": sorted(keys)},
        },
    })
    assert out.get("solo") and out["solo_tried"] == ["cc", "hermes"], out
    assert [row["key"] for row in case.native_calls] == ["cc", "hermes"]
    result, = out["results"]
    assert result["status"] == "done"
    assert "challenge-response.txt" in {a["name"] for a in result.get("artifacts") or []}, {
        "result_owner": result.get("harness"), "assignments": out.get("task_assignments"),
        "canonical": case.pc.load(case.team)["tasks"],
        "export_rejections": [row["reasons"] for row in case.export_calls],
    }
    assert out["assignment_decisions"][0]["harness"] == "codex"
    for row in observed:
        assert row["facts"]["task_id"] == TASK
        assert row["facts"]["orchestration"]["assignment"]["harness"] == row["owner"]
        tasks = {n["task_id"]: n for n in row["packet"]["tasks"]}
        assert tasks[TASK]["assigned_harness"] == row["owner"]
        assert tasks[OTHER_TASK] == original_other
        # Quick snapshots intentionally expose the current node, not all tasks.
        assert row["snapshot"]["task_view"]["current"] == tasks[TASK]
        for packet in (row["packet"], row["snapshot"]):
            assert packet["user_id"] == USER and packet["conversation_id"] == CONVERSATION
    assert observed[1]["packet"]["decisions"] == case.pc.load(case.team)["decisions"]
    assert result["harness"] == result["assigned_harness"] == out["captain"] == "hermes"
    assert out["task_assignments"] == {TASK: "hermes"}
    assert case.teams.load(case.team)["tasks"][TASK]["attempt_seq"] == 2
    assert result["native_checkpoint"]["session_id"] == "__ut_actual_relay_native"
    assert result["native_conversation_id"] == CONVERSATION
    artifact = next(a for a in result["artifacts"] if a["name"] == "challenge-response.txt")
    data = Path(artifact["path"]).read_bytes()
    assert data == (case.work / "challenge.txt").read_bytes()
    assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
    assert case.identity.expected_export_hash(
        artifact["path"], USER, CONVERSATION) == artifact["sha256"]
    assert case.identity.project_grant(USER, str(case.work)) == grant

    # A valid relay binding cannot authorize another user's or another task's export.
    from artifact_content_safety import ArtifactUnsafe
    for user, task, reason in [
        ("another-owner", TASK, "artifact_owner_denied"),
        (USER, OTHER_TASK, "artifact_project_binding_required"),
    ]:
        with pytest.raises(ArtifactUnsafe, match=reason):
            case.ad.project_export_conversation(
                observed[-1]["snapshot"], team_id=case.team, task_id=task,
                user_id=user, harness_key="hermes", native_conversation_id=CONVERSATION,
                workdir=str(case.work))
