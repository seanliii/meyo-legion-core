"""首脑早答：真实候选调用、项目记录与正式导出；原生叶子离线，不重新答题。"""
from copy import deepcopy
import http.client
from http.server import ThreadingHTTPServer
import importlib
import json
import socket
import threading

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, USER, CONVERSATION
from test_native_terminal_dispatch_graph import graph, _returned


_SOCKET_CONNECT = socket.socket.connect
_CREATE_CONNECTION = socket.create_connection


@pytest.fixture
def solo_case(graph):
    case, mp, core = graph, graph.monkeypatch, graph.core
    mp.setattr(core, "_captain_candidates", lambda *a, **kw: ["codex"])
    mp.setattr(core, "_selection_resource_context",
               lambda *a, **kw: {"members": [{"key": "codex"}], "resource_index": {}})
    case.team = "__ut_captain_solo_delivery"
    case.before_native = []
    case.original_results = []
    (case.work / "challenge.txt").write_bytes(b"original authorized bytes\n")
    leaf = core._run_core_inprocess
    run = core.run_one

    def observe(*args, **kwargs):
        facts = kwargs.get("task_facts") or {}
        case.before_native.append({
            "qhash": args[2], "task_id": facts.get("task_id"), "team_id": facts.get("team_id"),
            "project": deepcopy(kwargs.get("project_context")),
            "packet": case.pc.load(case.team), "conversation": kwargs.get("session_id"),
        })
        return leaf(*args, **kwargs)

    def capture(*args, **kwargs):
        result = run(*args, **kwargs)
        case.original_results.append(deepcopy(result))
        return result

    def answer(call):
        (case.work / "challenge-response.txt").write_bytes((case.work / "challenge.txt").read_bytes())
        if getattr(case, "revoke", False):
            # Revoke the real fixture grant; do not mock the exporter/authorization.
            from pathlib import Path
            import os
            Path(os.environ["LEGION_PERSONAL_TOKENS_FILE"]).write_text("{}")
        return {
            **_returned(case, "__ut_actual_candidate_native"), "final": "DONE",
            "orchestration_request": {"captain_bid": {"solo": True, "reason": "already executed"}},
            "usage": {"input_tokens": 17, "output_tokens": 3, "total_tokens": 20},
        }

    mp.setattr(core, "_run_core_inprocess", observe)
    mp.setattr(core, "run_one", capture)
    case.answer = answer
    case.plan = {"captain_candidates": ["codex"], "project_context": {"repo_root": str(case.work)}}
    return case


def elect(case):
    return case.core.elect_captain(
        "Copy challenge.txt exactly to challenge-response.txt, then answer DONE.",
        case.plan, qhash=case.team, user_id=USER, conversation_id=CONVERSATION,
        cb=case.events.append)


@pytest.mark.parametrize("revoke", [False, True])
def test_real_candidate_is_registered_before_execution_and_original_result_survives(solo_case, revoke):
    case = solo_case
    case.revoke = revoke
    # A business task with the harness name must not be overwritten by the candidate.
    case.pc.initialize(case.team, "Original request", {
        **case.plan, "tasks": {"codex": {"task": "Existing business task", "assigned_harness": "other-owner"}}},
        mode="orchestrate", user_id=USER, conversation_id=CONVERSATION)
    out = elect(case)
    assert out["ok"] and out["solo"] and len(case.native_calls) == 1
    original = out["original_result"]
    scope = out["candidate_scope"]
    assert scope["request_id"] == case.team + "_captain_codex"
    assert scope["team_id"] == case.team and scope["task_id"] != "codex"
    before = case.before_native[0]
    assert before["qhash"] == scope["request_id"] and before["team_id"] == case.team
    assert before["task_id"] == scope["task_id"]
    rows = {r["task_id"]: r for r in before["packet"]["tasks"]}
    assert rows[scope["task_id"]]["assigned_harness"] == "codex"
    assert rows["codex"]["assigned_harness"] == "other-owner"
    assert original["native_checkpoint"]["session_id"] == "__ut_actual_candidate_native"
    assert original["native_conversation_id"] == CONVERSATION
    assert original["model"] == case.original_results[0]["model"]
    assert original["usage"] == case.original_results[0]["usage"]
    assert original["elapsed"] == case.original_results[0]["elapsed"]
    if revoke:
        assert not original.get("artifacts")
        assert original["artifact_safety_observations"]
    else:
        assert "challenge-response.txt" in {a["name"] for a in original["artifacts"]}
    saved = case.teams.load(case.team)["tasks"][scope["task_id"]]
    assert saved["result"]["native_checkpoint"] == original["native_checkpoint"]
    assert saved["attempt_seq"] == 1


@pytest.mark.parametrize("transport", ["handler", "loopback_http"])
def test_actual_http_captain_solo_retains_native_result_without_second_execution(solo_case, transport):
    case, mp, core = solo_case, solo_case.monkeypatch, solo_case.core
    server = importlib.import_module("server")
    mp.setattr(server, "_source_version_identity", lambda: {"commit": "offline-fixture"})
    mp.setattr(core, "pilot_settings", lambda *a, **kw: {"enabled": False})
    mp.setattr(importlib.import_module("harness_performance"), "route_policy",
               lambda *a: {"verified": False, "route": "strongest_single"})
    mp.setattr(server.DESKTOP_FS, "materialize_session", lambda *a, **kw: {})
    mp.setattr(server.DESKTOP_FS, "sync_personal_data", lambda *a, **kw: None)
    body = {
        "question": "Copy challenge.txt exactly to challenge-response.txt, then answer DONE.",
        "mode": "orchestrate", "uid": USER, "session_id": CONVERSATION, "team_id": case.team,
        "plan": {**case.plan, "team_policy": {"candidate_harnesses": ["codex"]}},
    }
    events, access = [], []
    real_access = server.Handler._project_access_allowed

    def allowed(handler, *args):
        value = real_access(handler, *args)
        access.append(value)
        return value

    mp.setattr(server.Handler, "_project_access_allowed", allowed)
    if transport == "handler":
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/api/legion/dispatch"
        handler._personal_user = lambda: USER  # Directory checks remain real.
        handler._read_json_body = lambda: deepcopy(body)
        handler._sse_open = lambda: None
        handler._sse_send = lambda kind, data, **kw: (events.append((kind, deepcopy(data))) or True)
        handler._send_json = lambda value, status=200: events.append(("json", {"status": status, **value}))
        handler._do_POST_inner()
    else:
        # Real socket -> do_POST -> personal token -> project admission -> solo.
        # Only this temporary loopback endpoint is allowed by the no-network fixture.
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        address = httpd.server_address

        def connect(sock, target):
            assert target == address, "only this test's loopback socket is allowed"
            return _SOCKET_CONNECT(sock, target)

        def create_connection(target, *args, **kwargs):
            assert target == address, "only this test's loopback socket is allowed"
            return _CREATE_CONNECTION(target, *args, **kwargs)

        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket, "create_connection", create_connection)
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01},
                                  daemon=True)
        thread.start()
        connection = http.client.HTTPConnection(*address, timeout=5)
        response = None
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
                pytest.fail("captain solo HTTP stream did not return a final event")
        finally:
            if response is not None:
                response.close()
            connection.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
        assert not thread.is_alive()
    out = next(data for kind, data in reversed(events) if kind == "final")
    assert access == [True] and len(case.native_calls) == 1
    row = out["results"][0]
    raw = case.original_results[0]
    for name in ("model", "model_used", "elapsed", "attempts", "native_checkpoint", "usage",
                 "native_conversation_id"):
        assert row.get(name) == raw.get(name), (name, row, raw)
    assert "challenge-response.txt" in {a["name"] for a in row["artifacts"]}
    artifact = next(a for a in row["artifacts"] if a["name"] == "challenge-response.txt")
    from pathlib import Path
    assert Path(artifact["path"]).read_bytes() == (case.work / "challenge.txt").read_bytes()
    assert case.identity.expected_export_hash(
        artifact["path"], USER, CONVERSATION) == artifact["sha256"]
    assert row["task_id"] in {n["task_id"] for n in case.pc.load(case.team)["tasks"]}
    assert out["task_assignments"][row["task_id"]] == "codex"
    assert out["usage"]["available"] is raw["usage"]["available"]


def test_candidate_keeps_original_native_lane_without_faking_an_export_alias(solo_case):
    case, mp = solo_case, solo_case.monkeypatch
    slot = CONVERSATION + ":task:original_business_node"
    checkpoint = {
        "kind": "codex_thread", "session_id": "__ut_existing_native",
        "provider_binding": case.mr.ccr_binding_key(
            "fixture-model", "private-fixture-key", "https://models.example.invalid/v1"),
    }
    case.tm.update("conv:codex:" + slot, "codex", native_checkpoint=checkpoint,
                   native_conversation_id=slot, workdir=str(case.work))
    mp.setattr(importlib.import_module("sessions"), "continuation_native_slot", lambda *a, **kw: slot)
    answer = case.answer

    def resumed(call):
        assert call["checkpoint"]["session_id"] == checkpoint["session_id"]
        value = answer(call)
        value["native_checkpoint"] = deepcopy(checkpoint)
        return value

    case.answer = resumed
    out = elect(case)
    result = out["original_result"]
    assert len(case.native_calls) == 1 and result["native_conversation_id"] == slot
    assert all(result["native_checkpoint"].get(k) == v for k, v in checkpoint.items())
    # The native slot was not renamed to make this new candidate task pass
    # the existing exporter. An unproven alias remains a visible refusal.
    assert not result.get("artifacts")
    assert any(n["reason"] == "artifact_project_binding_required"
               for n in result["artifact_safety_observations"])


def test_candidate_exception_closes_its_real_attempt(solo_case):
    case = solo_case

    def failed(call):
        raise RuntimeError("offline native leaf failure")

    case.answer = failed
    with pytest.raises(RuntimeError, match="offline native leaf"):
        elect(case)
    task_id = case.before_native[0]["task_id"]
    task = case.teams.load(case.team)["tasks"][task_id]
    assert task["active_attempt"] is None
    assert task["status"] == "error"


def _short_answer_without_project_grant(case):
    # An anonymous short answer must not borrow USER's project/grant fixture.
    case.plan = {"captain_candidates": ["codex"]}
    case.answer = lambda call: {
        "status": "ok", "final": "DONE",
        "native_checkpoint": {"kind": "codex_thread", "session_id": "__ut_anonymous_native"},
        "orchestration_request": {"captain_bid": {"solo": True}},
    }


def test_captain_omitted_conversation_binds_to_effective_request(solo_case):
    case = solo_case
    out = case.core.elect_captain(
        "Copy challenge.txt exactly to challenge-response.txt, then answer DONE.",
        case.plan, qhash=case.team, user_id=USER)
    assert out["ok"] and out["solo"], out
    assert len(case.native_calls) == 1
    assert case.pc.load(case.team)["conversation_id"] == case.team
    assert out["original_result"]["native_conversation_id"] == case.team
    assert "challenge-response.txt" in {a["name"] for a in out["original_result"]["artifacts"]}


def test_captain_default_calls_are_isolated_from_existing_default_project(solo_case):
    case = solo_case
    _short_answer_without_project_grant(case)
    case.pc.initialize("adhoc", "Another caller's project", {"tasks": {}},
                       mode="orchestrate", user_id="default", conversation_id="another-session")
    original = case.pc.load("adhoc")
    teams = []
    for _ in range(2):
        # Plan contents are not caller identity, even at this legacy entry.
        out = case.core.elect_captain(
            "Answer DONE.", {**case.plan, "user_id": USER, "conversation_id": CONVERSATION})
        assert out["ok"] and out["solo"], out
        team = out["candidate_scope"]["team_id"]
        teams.append(team)
        assert team.startswith("adhoc_")
        packet = case.pc.load(team)
        assert packet["user_id"] == f"anon:{team}"
        assert packet["conversation_id"] == team
        assert out["original_result"]["native_conversation_id"] == team
    assert teams[0] != teams[1]
    assert len(case.native_calls) == 2
    assert case.pc.load("adhoc") == original


@pytest.mark.parametrize("user_id", ["", None, " default "])
def test_captain_anonymous_identity_is_normalized_before_binding(solo_case, user_id):
    case = solo_case
    _short_answer_without_project_grant(case)
    out = case.core.elect_captain("Answer DONE.", case.plan, qhash=case.team, user_id=user_id)
    assert out["ok"] and out["solo"], out
    packet = case.pc.load(case.team)
    assert packet["user_id"] == f"anon:{case.team}"
    assert packet["conversation_id"] == case.team
    assert len(case.native_calls) == 1


@pytest.mark.parametrize("owner", [USER, "default"])
def test_captain_anonymous_cannot_claim_an_existing_owner_project(solo_case, owner):
    case = solo_case
    _short_answer_without_project_grant(case)
    case.pc.initialize(case.team, "Another caller's project", {"tasks": {}},
                       mode="orchestrate", user_id=owner, conversation_id=CONVERSATION)
    original = case.pc.load(case.team)
    out = case.core.elect_captain(
        "Answer DONE.", case.plan, qhash=case.team, conversation_id=CONVERSATION)
    assert not out["ok"], out
    assert "belongs to another user" in out["provider_results"][0]["error"]
    assert not case.native_calls
    assert case.pc.load(case.team) == original


def test_captain_default_entry_still_requires_explicit_provider_configuration(solo_case):
    case = solo_case
    _short_answer_without_project_grant(case)
    case.monkeypatch.delenv("LEGION_CODEX_RESPONSES_BINDING", raising=False)
    out = case.core.elect_captain("Answer DONE.", case.plan)
    assert not out["ok"]
    assert out["provider_results"][0]["error_envelope"]["category"] == "model_config"
    assert not case.native_calls and not case.selections
