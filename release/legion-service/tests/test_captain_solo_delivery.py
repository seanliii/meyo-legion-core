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


@pytest.mark.parametrize("candidates", [[], ["__ut_graph_original"]])
def test_election_cannot_audition_a_member_outside_the_request_scope(solo_case, candidates):
    case = solo_case
    case.plan["team_policy"] = {"candidate_harnesses": candidates}
    out = elect(case)
    assert not out["ok"]
    assert not case.native_calls, "No out-of-scope captain may run before task binding"


@pytest.mark.parametrize("pause", [False, True])
def test_auto_quick_keeps_selected_member_request_model_and_pause(solo_case, pause):
    case, mp, core = solo_case, solo_case.monkeypatch, solo_case.core
    server = importlib.import_module("server")
    mp.setattr(server, "_source_version_identity", lambda: {"commit": "offline-fixture"})
    mp.setattr(core, "pilot_settings", lambda *a, **kw: {"enabled": False})
    mp.setattr(importlib.import_module("harness_performance"), "route_policy",
               lambda *a: {"verified": False, "route": "strongest_single"})
    mp.setattr(server.DESKTOP_FS, "materialize_session", lambda *a, **kw: {})
    mp.setattr(server.DESKTOP_FS, "sync_personal_data", lambda *a, **kw: None)
    mp.setattr(core, "_autonomy_matrix", lambda: {
        "codex": {"cost_class": "heavy"}, "cc": {"cost_class": "light"}})
    mp.setattr(core, "_harness_keys", lambda: {"codex", "cc", "__ut_other_candidate"})
    mp.setattr(core, "_aigc_ready", lambda **kw: {
        "runnable_clis": ["codex", "cc", "__ut_other_candidate"]})
    mp.setattr(importlib.import_module("quick_pred"), "predict_simple",
               lambda *a, **kw: {"verdict": "needs_harness", "reason": "fixture uses native planner"})
    mp.setattr(core, "plan_request_with_model", lambda *a, **kw: {
        "ok": True, "suggested_mode": "pk", "solo": True,
        "tasks": {"answer": {"task": "Keep the original chosen member",
                                "assigned_harness": "codex", "assignment_locked": True,
                                "direction": "quick"}},
        "directions": {"answer": "quick"}})
    assignments, calls = [], []

    def resolve(value, keys):
        assignments.append(deepcopy(value))
        return {key: {"model": (value.get("model_overrides") or {}).get(key),
                      "appid": "synthetic-request-credential"} for key in keys}

    def native(key, task, **kw):
        calls.append((key, deepcopy(kw)))
        if pause:
            return {"key": key, "harness": key, "status": "error", "final": "",
                    "error": "original request budget is exhausted",
                    "error_envelope": {"category": "runtime_budget", "scope": "request",
                                       "retryable": False, "code": "request_total_budget_exhausted"}}
        return {"key": key, "harness": key, "status": "done", "final": "legitimate short answer"}

    mp.setattr(case.mr, "resolve_assignments", resolve)
    mp.setattr(core, "run_one", native)
    body = {
        "question": "Keep the original selected member and request configuration.",
        "mode": "auto", "uid": USER, "session_id": CONVERSATION, "team_id": case.team,
        "plan": {"project_context": {"repo_root": str(case.work)},
                 "team_policy": {"candidate_harnesses": ["codex", "__ut_other_candidate"]},
                 "model_overrides": {"codex": "fixture-request-model"}},
    }
    events = []
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/api/legion/dispatch"
    handler._personal_user = lambda: USER
    handler._read_json_body = lambda: deepcopy(body)
    handler._sse_open = lambda: None
    handler._sse_send = lambda kind, data, **kw: (events.append((kind, deepcopy(data))) or True)
    handler._send_json = lambda value, status=200: events.append(("json", {"status": status, **value}))
    handler._do_POST_inner()
    out = next(data for kind, data in reversed(events) if kind == "final")
    assert len(calls) == 1 and calls[0][0] == "codex"
    assert calls[0][1]["preferred_model"] == "fixture-request-model"
    assert assignments[-1]["team_policy"]["candidate_harnesses"] == ["codex", "__ut_other_candidate"]
    assert out["task_assignments"] == {"answer": "codex"}
    if pause:
        assert out["results"][0]["error"] == "original request budget is exhausted"


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


@pytest.mark.parametrize("endpoint", ["/api/legion/run_one", "/api/legion/dispatch"],
                         ids=["explicit-run-one", "auto-single-binding"])
@pytest.mark.parametrize("scenario", ["requested-peer", "short-without-peer", "outside-candidates",
                                      "peer-authorization", "peer-budget"])
def test_single_author_http_peer_receipt_precedes_original_native_resume(graph, endpoint, scenario):
    """Real Handler/core/state/export; only the native leaf and auto planner are CPU fixtures."""
    from pathlib import Path

    case, mp, core = graph, graph.monkeypatch, graph.core
    server = importlib.import_module("server")
    pilot = importlib.import_module("legion_pilot")
    peer = "__ut_graph_writer"
    paused = scenario in {"peer-authorization", "peer-budget"}
    requested = scenario == "requested-peer" or paused
    team = "__ut_single_peer_" + scenario + ("_auto" if endpoint.endswith("dispatch") else "_explicit")
    author_task = "A" if endpoint.endswith("dispatch") else "codex"
    original_bytes = b"authorized original input: CPU challenge 731\n"
    challenge = case.work / "challenge.txt"
    challenge.write_bytes(original_bytes)
    draft = case.work / "draft.txt"
    delivery = case.work / "delivery.txt"
    evidence = case.work / "peer-evidence.txt"
    receipt = "__ut_peer_findings_731_from_actual_challenge"
    revised = "__ut_author_revised_using_peer_731"
    checkpoint = {
        "kind": "codex_thread", "session_id": "__ut_single_original_author_native",
        "provider_binding": case.mr.ccr_binding_key(
            "fixture-model", "private-fixture-key", "https://models.example.invalid/v1"),
        "provider_descriptor": {"model": "fixture-model", "base_url": "https://models.example.invalid/v1"},
    }
    # Both ordinary Handler entries start in the original conversation slot.
    # Seed that real store, not a sessions resolver or a fabricated resume result.
    case.tm.update("conv:codex:" + CONVERSATION, "codex",
                   native_checkpoint=deepcopy(checkpoint), native_conversation_id=CONVERSATION,
                   workdir=str(case.work))
    candidates = ["codex", peer] if scenario != "outside-candidates" else ["codex", "__ut_graph_original"]
    policy = {
        "candidate_harnesses": candidates, "adaptive": False, "max_parallel": 1,
        "max_failover": 0, "contract_repair_rounds": 0, "continuous_coordination": False,
    }
    question = (
        "Read authorized challenge.txt; save draft.txt, request an independent peer input, "
        "then continue as the original author and save delivery.txt using its findings."
        if requested else "Return exactly one character: 好."
    )
    project = {"repo_root": str(case.work)}
    mp.setattr(server, "_source_version_identity", lambda: {"commit": "offline-fixture"})
    mp.setattr(core, "pilot_settings", lambda *a, **kw: {"enabled": True})
    mp.setattr(pilot, "load_user_config", lambda *a, **kw: pilot.merge_config({"enabled": True}))
    mp.setattr(importlib.import_module("harness_performance"), "route_policy",
               lambda *a, **kw: {"verified": False, "route": "strongest_single"})
    mp.setattr(server.DESKTOP_FS, "materialize_session", lambda *a, **kw: {})
    mp.setattr(server.DESKTOP_FS, "sync_personal_data", lambda *a, **kw: None)
    mp.setattr(importlib.import_module("quick_pred"), "predict_simple",
               lambda *a, **kw: {"verdict": "needs_harness", "reason": "CPU native fixture"})
    fastpath_outcomes = []
    mp.setattr(importlib.import_module("quick_pred"), "record_fastpath",
               lambda **kw: fastpath_outcomes.append(kw))

    planner_calls = []

    def plan(*args, **kwargs):
        planner_calls.append(True)
        return {
            "ok": True, "suggested_mode": "pk", "solo": True,
            "tasks": {"A": {"task": question, "assigned_harness": "codex",
                            "assignment_locked": True, "direction": "quick"}},
            "directions": {"A": "quick"},
        }

    mp.setattr(core, "plan_request_with_model", plan)
    # explicit_run_one config and the saved native cursor use this same CPU-only
    # provider pair. A different preferred_appid is a real admission conflict.
    mp.setattr(case.mr, "resolve_assignments", lambda value, keys: {
        key: {"model": "fixture-model", "appid": "private-fixture-key"} for key in keys})

    def no_adapter_process(*args, **kwargs):
        raise AssertionError("this regression must reach only the CPU native leaf")

    mp.setattr(case.hr, "_run_task", no_adapter_process)
    native = core._run_core_inprocess
    observed, evaluations = [], []
    real_evaluate = pilot.evaluate

    def evaluate(*args, **kwargs):
        verdict = real_evaluate(*args, **kwargs)
        evaluations.append(deepcopy(verdict))
        return verdict

    mp.setattr(pilot, "evaluate", evaluate)  # Spy, never substitute the real classification.

    def observe(key, task, qhash, *args, **kwargs):
        # Preserve actual task text and execution kwargs without retaining any key.
        facts = deepcopy(kwargs.get("task_facts") or {})
        safe_kwargs = {name: deepcopy(kwargs.get(name)) for name in
                       ("session_id", "task_facts", "task_memory", "project_context")}
        if isinstance(safe_kwargs.get("task_memory"), dict):
            safe_kwargs["task_memory"].pop("provider_binding", None)
        observed.append({
            "key": key, "task": task, "qhash": qhash, "kwargs": safe_kwargs,
            "task_id": facts.get("task_id"), "project": deepcopy(case.pc.load(team)),
            "team": deepcopy(case.teams.load(team)),
        })
        return native(key, task, qhash, *args, **kwargs)

    mp.setattr(core, "_run_core_inprocess", observe)

    def answer(call):
        at_leaf = observed[-1]
        assert at_leaf["key"] == call["key"]
        assert challenge.read_bytes() == original_bytes
        assert call["project_workdir"] == str(case.work)
        if call["key"] == peer:
            assert requested, "unrequested/out-of-candidate peer must never reach native execution"
            assert [row["key"] for row in observed] == ["codex", peer]
            assert call["task_id"] != author_task, "peer must have its own task binding"
            author_binding = next(value for value in at_leaf["project"]["tasks"]
                                  if value["task_id"] == author_task)
            assert author_binding["assigned_harness"] == "codex"
            assert at_leaf["team"]["tasks"][author_task]["assigned_harness"] == "codex"
            assert "read authorized challenge.txt and return findings" in at_leaf["task"]
            assert draft.read_bytes() == b"original author draft\n"
            assert call["checkpoint"] != checkpoint, "peer cannot borrow the author's native cursor"
            evidence.write_text(receipt + "\n", encoding="utf-8")
            if paused:
                envelope = ({"category": "authorization_required", "scope": "provider",
                             "code": "appid_disabled", "retryable": False}
                            if scenario == "peer-authorization" else
                            {"category": "runtime_budget", "scope": "request",
                             "code": "request_token_budget_exhausted", "retryable": False})
                return {**_returned(case, "__ut_single_peer_native"),
                        "status": "error", "final": "", "error": "CPU peer stopped",
                        "native_facts": {"process_returncode": 1, "turn_status": "failed"},
                        "error_envelope": {**envelope, "message": "CPU peer stopped"}}
            return {**_returned(case, "__ut_single_peer_native"), "final": receipt}

        assert call["key"] == "codex", "no replacement author or coordination runner is allowed"
        assert call["task_id"] == author_task
        assert call["checkpoint"] == checkpoint
        assert call["memory_workdir"] == str(case.work)
        assert call["model"] == "fixture-model"
        assert call["provider"]["binding"] == checkpoint["provider_binding"]
        bound = {row["task_id"]: row for row in at_leaf["project"]["tasks"]}
        assert bound[author_task]["assigned_harness"] == "codex"
        assert at_leaf["team"]["tasks"][author_task]["assigned_harness"] == "codex"
        author_calls = [row for row in observed if row["key"] == "codex"]
        result = {**_returned(case, checkpoint["session_id"]),
                  "native_checkpoint": deepcopy(checkpoint)}
        if not requested:
            assert len(author_calls) == 1, "a legitimate short answer must not be retried"
            result["final"] = "好"
            if scenario == "outside-candidates":
                result["orchestration_request"] = {"need_peer": {
                    "key": peer, "ask": "read authorized challenge.txt and return findings"}}
            return result
        if len(author_calls) == 1:
            draft.write_bytes(b"original author draft\n")
            result.update(final="Original draft saved at " + str(draft), orchestration_request={
                "need_peer": {"key": peer, "ask": "read authorized challenge.txt and return findings"},
                "coordinate": {"action": "continue", "reason": "need the independent peer findings"},
            })
            return result
        assert [row["key"] for row in observed] == ["codex", peer, "codex"], (
            "explicit need_peer must execute before original-author continuation", observed)
        assert receipt in at_leaf["task"], "peer receipt must reach the real native task body"
        assert "challenge.txt" in at_leaf["task"] and "draft.txt" in at_leaf["task"]
        assert evidence.read_text(encoding="utf-8") == receipt + "\n"
        assert author_calls[0]["kwargs"]["session_id"] == at_leaf["kwargs"]["session_id"]
        delivery.write_text(revised + "\n" + receipt + "\n", encoding="utf-8")
        result["final"] = revised + " " + str(delivery)
        return result

    case.answer = answer
    body = {"uid": USER, "session_id": CONVERSATION, "team_id": team}
    if endpoint.endswith("dispatch"):
        body.update(question=question, mode="auto", plan={
            "project_context": project, "team_policy": policy,
            "model_overrides": {"codex": "fixture-model"}, "success_audit": {"enabled": False}})
    else:
        body.update(key="codex", task=question, model="fixture-model",
                    execution_depth="standard", project_context=project, team_policy=policy)
    events = []
    handler = server.Handler.__new__(server.Handler)
    handler.path = endpoint
    handler._personal_user = lambda: USER  # Real project ownership/grants/export remain in force.
    handler._read_json_body = lambda: deepcopy(body)
    handler._sse_open = lambda: None
    handler._sse_send = lambda kind, data, **kw: (events.append((kind, deepcopy(data))) or True)
    handler._send_json = lambda value, status=200: events.append(("json", {"status": status, **value}))
    handler._do_POST_inner()

    expected_order = (["codex", peer] if paused else
                      ["codex", peer, "codex"] if requested else ["codex"])
    if not observed:
        finals = [data for kind, data in events if kind in {"final", "error"}]
        print("CPU_SINGLE_ADMISSION", json.dumps(finals, ensure_ascii=False, default=str))
    assert [row["key"] for row in observed] == expected_order, (
        "Handler must execute requested peer first, then resume the same author",
        json.dumps({"observed": observed, "events": events}, ensure_ascii=False, default=str))
    assert not case.empty_plans and not case.picks and not case.calls
    assert len(planner_calls) == int(endpoint.endswith("dispatch"))
    out = next(data for kind, data in reversed(events) if kind == "final")
    row = out["result"] if endpoint.endswith("run_one") else next(
        value for value in out["results"] if value["task_id"] == author_task)
    assert row["task_id"] == author_task and row["assigned_harness"] == "codex"
    assert row["native_checkpoint"] == checkpoint
    assert row["native_conversation_id"] == CONVERSATION
    assert row["model"] == "fixture-model"
    assert row["status"] == "done"
    saved = case.teams.load(team)["tasks"][author_task]
    assert saved["assigned_harness"] == "codex" and saved["active_attempt"] is None
    assert saved["result"]["native_checkpoint"] == checkpoint
    assert saved["attempt_seq"] == (2 if requested and not paused else 1)
    packet = {value["task_id"]: value for value in case.pc.load(team)["tasks"]}
    assert packet[author_task]["assigned_harness"] == "codex"
    assert challenge.read_bytes() == original_bytes
    if paused:
        receipt_out = row if endpoint.endswith("run_one") else out
        assert receipt_out["outcome"]["status"] == "partial"
        assert receipt_out["outcome"]["coordination_incomplete"] is True
        expected_kind = ("provider_authorization_required" if scenario == "peer-authorization"
                         else "request_budget_exhausted")
        assert receipt_out["pilot"]["kind"] == expected_kind
        assert receipt_out["pilot"]["action"] == "stop"
        assert row["final"] == "Original draft saved at " + str(draft)
        assert row["peer_input_pause"]["error_envelope"]["retryable"] is False
        assert len(row["peer_results"]) == 1 and row["peer_results"][0]["status"] == "error"
        assert not delivery.exists()
        artifact = next(value for value in row["artifacts"] if value["name"] == "draft.txt")
        assert Path(artifact["path"]).read_bytes() == draft.read_bytes()
        session = importlib.import_module("sessions").load_session(CONVERSATION, USER)
        turn = next(turn for turn in session["turns"] if turn["team_id"] == team)
        assert turn["outcome"]["status"] == "partial"
        if endpoint.endswith("dispatch"):
            assert out["completion"]["status"] == "partial"
            assert fastpath_outcomes == [{"ok": False}]
        return
    if requested:
        assert any(value["kind"] == "native_continue" for value in evaluations)
        assert row["final"] == revised + " " + str(delivery)
        assert saved["result"]["final"] == row["final"]
        if endpoint.endswith("dispatch"):
            assert out["task_assignments"][author_task] == "codex"
            assert core._winner_final(out) == row["final"], "peer must not replace the main draft"
        artifact = next(value for value in row["artifacts"] if value["name"] == "delivery.txt")
        assert Path(artifact["path"]).read_bytes() == delivery.read_bytes()
        assert Path(artifact["path"]).read_text(encoding="utf-8") == revised + "\n" + receipt + "\n"
        assert case.identity.expected_export_hash(
            artifact["path"], USER, CONVERSATION) == artifact["sha256"]
        assert any(value["team_id"] == team and value["task_id"] == author_task
                   and value["conversation_id"] == CONVERSATION and value["exported_files"]
                   for value in case.export_calls)
        assert case.tm.load(observed[0]["qhash"], "codex")["native_checkpoint"] == checkpoint
    else:
        assert row["final"] == "好"
        assert not draft.exists() and not delivery.exists() and not evidence.exists()
        assert not row.get("peer_results")
