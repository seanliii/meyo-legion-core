"""Real HTTP feedback -> dispatch/persistent -> run_one -> native-input CPU tests.

Only transport identity, the scheduler wakeup and native/coordinator replies are
substituted. Reuse the isolated scene; no listener, process, network or real
credentials. Mail remains in team_state, never marked read by context assembly.
"""
from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import importlib
import json
from pathlib import Path
import threading

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, USER, CONVERSATION
from test_native_terminal_dispatch_graph import graph, _node, _plan, _returned, RELAY, QUESTION


@pytest.fixture
def mail_scene(graph):
    case, mp = graph, graph.monkeypatch
    case.server = importlib.import_module("server")
    case.inputs, case.wakeups = [], []
    mp.setattr(case.core, "_CANCEL_REGISTRY", {})
    native = case.core._run_core_inprocess

    def capture(key, task, qhash, *args, **kwargs):
        case.inputs.append({
            "key": key, "text": task, "qhash": qhash,
            "facts": deepcopy(kwargs.get("task_facts") or {}),
            "memory": deepcopy(kwargs.get("task_memory") or {}),
        })
        return native(key, task, qhash, *args, **kwargs)

    mp.setattr(case.core, "_run_core_inprocess", capture)
    mp.setattr(case.server, "_schedule_feedback_resume",
               lambda *args: case.wakeups.append(args))

    def coordinate(*args, **kwargs):
        proposal = {"action": "complete", "reason": "Synthetic revision returned"}
        if len(case.inputs) == case.revision_start:
            proposal = {
                "action": "continue", "reason": "Synthetic existing-node revision",
                "task_updates": {node: {"task": "Revise existing synthetic node " + node}
                                 for node in case.nodes},
            }
        return {"status": "done", "final": "Synthetic coordinator reply",
                "orchestration_request": {"coordinate": proposal}}

    mp.setattr(case.core, "_run_one_with_gateway", coordinate)
    return case


def _seed(case, nodes=("A",), *, scope="conversation"):
    case.nodes = nodes
    parent = "__ut_mail_parent"
    plan = _plan(case, {node: _node("Synthetic original " + node, scope=scope)
                        for node in nodes})
    plan["captain"] = RELAY if len(nodes) > 1 else ""
    out = case.core.dispatch(
        QUESTION, plan, qhash=parent, mode="orchestrate" if len(nodes) > 1 else "relay",
        user_id=USER, conversation_id=CONVERSATION)
    assert len(case.inputs) == len(nodes), out
    state = case.teams.load(parent)
    state["captain"] = plan["captain"]
    state["orchestration_spec"]["pattern"] = "orchestrate" if len(nodes) > 1 else "single"
    state["orchestration_spec"]["team_policy"]["continuous_coordination"] = True
    case.teams._write(state)
    case.server.SESSIONS.save_turn(
        CONVERSATION, QUESTION, out, mode="orchestrate" if len(nodes) > 1 else "single",
        user_id=USER)
    case.parent = parent
    return parent


def _post(case, team, recipient, text, *, request=""):
    body = {"team_id": team, "recipient": recipient, "text": text}
    if request:
        body.update(request_id=request, continue_execution=True)
    responses = []
    handler = case.server.Handler.__new__(case.server.Handler)
    handler.path = "/api/legion/team/message"
    handler._read_json_body = lambda: deepcopy(body)
    handler._personal_user = lambda: USER
    handler._send_json = lambda value, status=200: responses.append((status, deepcopy(value)))
    handler._do_POST_inner()
    assert len(responses) == 1 and responses[0][0] == 201, responses
    return responses[0][1]


def _queue(case, request):
    _post(case, case.parent, "commander", "Synthetic feedback " + request, request=request)
    messages = case.teams.load(case.parent)["mailboxes"]["commander"]
    return next(row for row in messages
                if (row.get("continuation") or {}).get("request_id") == request)


def _resume(case):
    case.revision_start = len(case.inputs)
    result = case.server._resume_saved_team(
        case.parent, explicit=False, user_id=USER, conversation_id=CONVERSATION)
    assert "result" in result, result
    return result, case.inputs[case.revision_start:]


def _http_single(case, *, pilot=False, task="Synthetic original HTTP single request"):
    """Real handler/persistence; replace only its HTTP transport, never run_one."""
    case.nodes = ("codex",)
    case.parent = "__ut_actual_http_single_parent"
    case.revision_start = 0
    case.monkeypatch.setattr(case.core, "pilot_settings", lambda *_: {"enabled": pilot})
    events = case.http_events = []
    handler = case.server.Handler.__new__(case.server.Handler)
    handler._sse_open = lambda: None

    def send(kind, value, **_):
        events.append((kind, deepcopy(value)))
        return True

    handler._sse_send = send
    handler._send_json = lambda value, status=200: events.append(
        ("json", {"status": status, "body": deepcopy(value)}))
    handler._handle_run_one({
        "key": "codex", "task": task, "uid": USER,
        "session_id": CONVERSATION, "team_id": case.parent,
        "project_context": {"repo_root": str(case.work)},
        "execution_depth": "quick",
    })
    return next((value["result"] for kind, value in events if kind == "final"), None)


def _http_receipt(case):
    state = case.teams.load(case.parent)
    session = case.server.SESSIONS.load_session(CONVERSATION, user_id=USER)
    turn = next(turn for turn in reversed(session["turns"]) if turn["team_id"] == case.parent)
    assert len(turn["results"]) == 1, "the actual HTTP call must persist its one terminal receipt"
    row, = turn["results"]
    assert state["user_id"] == USER and state["conversation_id"] == CONVERSATION
    assert (row["key"], row["task_id"], row["harness"], row["assigned_harness"]) == (
        "codex", "codex", "codex", "codex")
    return state, state["tasks"]["codex"], turn, row


def _assert_http_attempt_receipt(case, *, attempts, status):
    state, node, turn, row = _http_receipt(case)
    assert node["attempt_seq"] == attempts and node["active_attempt"] is None
    assert 0 < node["started_at"] <= node["finished_at"]
    assert not node["lease_owner"] and not node["lease_expires_at"]
    assert node["status"] == row["status"] == status
    for field in ("native_checkpoint", "checkpoint_writes", "native_session_scope",
                  "native_context_origin", "checkpoint_storage_error", "native_resume_error",
                  "model_used"):
        assert node["result"].get(field) == row.get(field), field
    starts = [event["attempt"] for event in state["events"] if event.get("t") == "attempt_started"]
    finishes = [event["attempt"] for event in state["events"] if event.get("t") == "attempt_finished"]
    assert starts == finishes == list(range(1, attempts + 1))
    return state, node, turn, row


@pytest.mark.parametrize("continuation", ["initial", "pilot", "http"])
def test_actual_http_single_parent_mail_reaches_first_feedback_input(mail_scene, continuation):
    """The reviewed counterexample, including both affected continuation paths."""
    case = mail_scene
    active_receipts = []

    def answer(call):
        node = case.teams.load(case.parent)["tasks"]["codex"]
        active_receipts.append((node["attempt_seq"], node["active_attempt"]))
        return _returned(case, "__ut_native_codex",
                         absent=continuation == "pilot" and len(case.native_calls) == 1)

    case.answer = answer
    case.monkeypatch.setattr(importlib.import_module("legion_pilot"), "evaluate",
                             lambda *a: {"action": "continue", "next_action": "Synthetic next round"})
    final = _http_single(case, pilot=continuation == "pilot")
    assert final and final["status"] == "done", case.http_events
    if continuation == "http":
        final = _http_single(case, task="Synthetic second HTTP request in the same owned team")
        assert final and final["status"] == "done", case.http_events
    attempts = 1 if continuation == "initial" else 2
    assert len(case.inputs) == attempts
    assert active_receipts == [(i, i) for i in range(1, attempts + 1)]
    assert [call["facts"].get("attempt_id") for call in case.inputs] == list(range(1, attempts + 1))
    original, node, _, parent_row = _assert_http_attempt_receipt(
        case, attempts=attempts, status="done")
    if attempts == 2:
        assert len(node["result_history"]) == 1
        assert node["result_history"][0]["result"]["native_checkpoint"] == parent_row["native_checkpoint"]
        assert case.inputs[1]["memory"]["native_checkpoint"] == parent_row["native_checkpoint"]

    _post(case, case.parent, "codex", "PEER_UNREAD_ORIGINAL_HTTP_PARENT_MAIL")
    before_mail = deepcopy(case.teams.load(case.parent)["mailboxes"])
    request = _queue(case, "peer-first-feedback")
    out, inputs = _resume(case)
    assert out["team_id"] == request["continuation"]["team_id"] and len(inputs) == 1
    call = inputs[0]
    assert call["memory"]["native_checkpoint"] == parent_row["native_checkpoint"]
    assert call["memory"]["native_conversation_id"] == parent_row["native_conversation_id"]
    assert call["memory"]["workdir"] == parent_row["workdir"] == str(case.work)
    assert case.teams.load(case.parent)["mailboxes"]["codex"] == before_mail["codex"]
    assert "PEER_UNREAD_ORIGINAL_HTTP_PARENT_MAIL" in call["text"], (
        "actual HTTP single parent has a durable native return, but its first feedback "
        "input silently omits that parent's unread mailbox")
    print("HTTP_SINGLE_PARENT_RECEIPT", json.dumps({
        "continuation": continuation, "attempt_seq": attempts,
        "same_owner": original["user_id"] == USER,
        "source_mail_unchanged": True, "mail_in_actual_child_input": True,
        "real_model_calls": 0,
    }, sort_keys=True))


@pytest.mark.parametrize("phase", ["before-claim", "first-claimed", "between-rounds", "second-claimed"])
def test_actual_http_single_cancellation_finishes_only_real_attempts(mail_scene, phase):
    case = mail_scene
    real_ensure, real_begin = case.teams.ensure, case.teams.begin_attempt

    def cancel(team, reason):
        # The existing /api/legion/cancel writes both the live token and its
        # durable fact. A disk-only request is not the actual HTTP producer.
        case.core.request_cancel(team, reason=reason)
        case.teams.request_cancel(team, reason=reason)

    def ensure(team, *args, **kwargs):
        state = real_ensure(team, *args, **kwargs)
        if phase == "before-claim" and team == case.parent:
            cancel(team, "Synthetic pre-claim cancellation")
        return state

    def begin(team, key, **kwargs):
        attempt = real_begin(team, key, **kwargs)
        if phase == ("first-claimed" if attempt == 1 else "second-claimed"):
            cancel(team, "Synthetic cancellation before native spawn")
        return attempt

    def evaluate(*args):
        if phase == "between-rounds":
            cancel(case.parent, "Synthetic between-round cancellation")
        return {"action": "continue", "next_action": "Synthetic next round"}

    case.monkeypatch.setattr(case.teams, "ensure", ensure)
    case.monkeypatch.setattr(case.teams, "begin_attempt", begin)
    case.monkeypatch.setattr(importlib.import_module("legion_pilot"), "evaluate", evaluate)
    case.answer = lambda call: _returned(case, "__ut_native_codex", absent=True)
    final = _http_single(case, pilot=True)
    assert final and final["status"] == "cancelled", case.http_events
    assert final["cancelled"] is True
    if phase == "before-claim":
        _, node, _, row = _http_receipt(case)
        assert node["attempt_seq"] == 0 and node["active_attempt"] is None
        assert node["status"] == "cancelled" and not node.get("started_at")
        assert not row.get("native_checkpoint") and not case.inputs
    else:
        attempts = 2 if phase == "second-claimed" else 1
        _, _, _, row = _assert_http_attempt_receipt(case, attempts=attempts, status="cancelled")
        expected_native_calls = 0 if phase == "first-claimed" else 1
        assert len(case.inputs) == expected_native_calls
        if expected_native_calls:
            assert row["native_checkpoint"]["session_id"] == "__ut_native_codex"
    assert not case.picks and not case.empty_plans


@pytest.mark.parametrize("round_number", [1, 2])
@pytest.mark.parametrize("error_type", [RuntimeError, PermissionError, KeyboardInterrupt])
def test_actual_http_single_exception_closes_attempt_and_saves_same_identity(
        mail_scene, round_number, error_type):
    case = mail_scene
    real_run = case.core.run_one
    attempts = []

    def fail(key, task, **kwargs):
        attempts.append((kwargs.get("task_facts") or {}).get("attempt_id"))
        if len(attempts) == round_number:
            raise error_type("Synthetic single invocation failure")
        return real_run(key, task, **kwargs)

    case.monkeypatch.setattr(case.core, "run_one", fail)
    case.monkeypatch.setattr(importlib.import_module("legion_pilot"), "evaluate",
                             lambda *a: {"action": "continue", "next_action": "Synthetic next round"})
    case.answer = lambda call: _returned(case, "__ut_native_codex", absent=True)
    if error_type is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            _http_single(case, pilot=True)
    else:
        assert _http_single(case, pilot=True) is None, "retain the existing SSE error response"
        assert any(kind == "error" for kind, _ in case.http_events)
    _, node, turn, row = _assert_http_attempt_receipt(
        case, attempts=round_number, status="error")
    assert attempts == list(range(1, round_number + 1))
    assert turn["status"] == "error"
    assert row["error"] and node["result"]["error"]
    assert not row.get("native_checkpoint"), "an exception is not a new native checkpoint receipt"
    assert len(case.inputs) == round_number - 1


def test_actual_http_single_stale_attempt_cannot_publish_or_replace_result(mail_scene):
    case = mail_scene
    real_finish = case.teams.finish_attempt
    stale = []

    def finish(team, key, attempt, result):
        if team == case.parent:
            stale.append(attempt)
            assert case.teams.cancel_attempt(team, key, attempt, "Synthetic attempt invalidated")
        return real_finish(team, key, attempt, result)

    case.monkeypatch.setattr(case.teams, "finish_attempt", finish)
    assert _http_single(case) is None, "a rejected attempt must not publish its late result"
    assert stale == [1]
    node = case.teams.load(case.parent)["tasks"]["codex"]
    assert node["status"] == "cancelled" and node["active_attempt"] is None
    assert not node["result"].get("native_checkpoint")
    session = case.server.SESSIONS.load_session(CONVERSATION, user_id=USER)
    turn = next(turn for turn in reversed(session["turns"]) if turn["team_id"] == case.parent)
    assert turn["results"] == [], "the stale worker must not overwrite the running session placeholder"
    assert not any(kind == "final" for kind, _ in case.http_events)


def test_http_single_cancel_after_finished_attempt_preserves_complete_native_receipt(mail_scene):
    case = mail_scene
    real_finish = case.teams.finish_attempt
    accepted = []

    def finish(team, key, attempt, result):
        ok = real_finish(team, key, attempt, result)
        if ok and team == case.parent:
            accepted.append((attempt, deepcopy(result),
                             deepcopy(case.teams.load(team)["tasks"][key]["result"])))
            # Cancel strictly after the durable finish, not during Pilot or
            # before finish. No new attempt has been claimed in this window.
            case.core.request_cancel(team, reason="Synthetic after-finish cancellation")
            case.teams.request_cancel(team, reason="Synthetic after-finish cancellation")
        return ok

    case.monkeypatch.setattr(case.teams, "finish_attempt", finish)
    case.monkeypatch.setattr(importlib.import_module("legion_pilot"), "evaluate",
                             lambda *a: {"action": "continue", "next_action": "Synthetic next round"})
    case.answer = lambda call: _returned(case, "__ut_native_codex", absent=True)
    final = _http_single(case, pilot=True)
    state, node, turn, row = _http_receipt(case)
    assert len(accepted) == len(case.inputs) == 1
    attempt, original, stored = accepted[0]
    assert attempt == node["attempt_seq"] == 1 and node["active_attempt"] is None
    assert node["result"] == stored and node["status"] == "partial"
    assert final["status"] == row["status"] == turn["status"] == "cancelled"
    assert final["cancelled"] is True
    assert original["native_checkpoint"] and original["checkpoint_writes"]
    # The request can be cancelled while its already-finished attempt remains
    # partial. Retain all native facts, not just the cursor's three fields.
    request_fields = {"status", "error", "cancelled", "pilot", "persistent_rounds",
                      "outcome", "delivery_contract", "partial", "contract_incomplete",
                      "delivered_count", "total", "all_failed", "source_ref",
                      "version_identity", "execution_role"}
    for field, value in original.items():
        if field not in request_fields:
            assert final.get(field) == row.get(field) == value, field
    assert final["persistent_rounds"] == 1, "a denied next claim is not an executed round"
    for kind in ("attempt_started", "attempt_finished"):
        assert [e["attempt"] for e in state["events"] if e.get("t") == kind] == [1]
    assert not node.get("result_history") and not case.picks
    print("HTTP_SINGLE_AFTER_FINISH_CANCEL", json.dumps({
        "native_calls": len(case.inputs), "attempt_seq": node["attempt_seq"],
        "request_status": turn["status"], "finished_attempt_status": node["status"],
        "complete_native_receipt_preserved": True, "real_model_calls": 0,
    }, sort_keys=True))


@pytest.mark.parametrize("first_publishes_first", [True, False],
                         ids=["older-publishes-while-newer-running", "newer-publishes-first"])
def test_http_single_overlapping_publish_keeps_each_requests_session_slot(
        mail_scene, first_publishes_first):
    case = mail_scene
    real_finish = case.teams.finish_attempt
    first_finished, release_first = threading.Event(), threading.Event()
    second_entered, release_second = threading.Event(), threading.Event()
    results, failures = {}, []
    names = {1: "SYNTHETIC_FIRST", 2: "SYNTHETIC_SECOND"}

    def finish(team, key, attempt, result):
        ok = real_finish(team, key, attempt, result)
        if ok and attempt == 1:
            first_finished.set()
            assert release_first.wait(10), "test did not release the first publisher"
        return ok

    def answer(call):
        attempt = case.inputs[-1]["facts"]["attempt_id"]
        if attempt == 2:
            second_entered.set()
            assert release_second.wait(10), "test did not release the second native return"
        return {**_returned(case, "__ut_native_codex"), "final": names[attempt]}

    def request(label):
        try:
            # Identical owner/session/team/task/model/UUID: text matching or
            # selecting the latest same-team row cannot identify this request.
            results[label] = _http_single(case)
        except BaseException as error:
            failures.append(error)

    case.monkeypatch.setattr(case.teams, "finish_attempt", finish)
    case.answer = answer
    first = threading.Thread(target=request, args=("first",), daemon=True)
    second = threading.Thread(target=request, args=("second",), daemon=True)
    during = None
    try:
        first.start()
        assert first_finished.wait(10), failures
        second.start()
        assert second_entered.wait(10), failures
        active = case.teams.load(case.parent)["tasks"]["codex"]
        assert active["active_attempt"] == active["attempt_seq"] == 2
        if first_publishes_first:
            release_first.set()
            first.join(10)
            assert not first.is_alive(), failures
            during = deepcopy(case.server.SESSIONS.load_session(CONVERSATION, user_id=USER))
            assert case.teams.load(case.parent)["tasks"]["codex"]["active_attempt"] == 2
        else:
            release_second.set()
            second.join(10)
            assert not second.is_alive(), failures
    finally:
        release_first.set()
        release_second.set()
        for worker in (first, second):
            if worker.ident is not None:
                worker.join(10)
        assert not first.is_alive() and not second.is_alive(), "no test thread may leak"
    assert not failures, failures
    assert results["first"]["final"] == names[1] and results["second"]["final"] == names[2]
    turns = case.server.SESSIONS.load_session(CONVERSATION, user_id=USER)["turns"]
    assert [t["results"][0]["final"] for t in turns] == [names[1], names[2]]
    assert len({t.get("turn_id") for t in turns}) == 2 and all(t.get("turn_id") for t in turns)
    node = case.teams.load(case.parent)["tasks"]["codex"]
    assert node["result"]["final"] == names[2] and node["active_attempt"] is None
    assert [call["facts"]["attempt_id"] for call in case.inputs] == [1, 2]
    for turn in turns:
        row, = turn["results"]
        assert row["native_checkpoint"]["session_id"] == "__ut_native_codex"
        assert row["workdir"] == str(case.work)
    if during is not None:
        assert [t["status"] for t in during["turns"]] == ["done", "running"]
        assert during["turns"][0]["results"][0]["final"] == names[1]
        assert during["turns"][1]["results"] == []
    print("HTTP_SINGLE_OVERLAPPING_PUBLISH", json.dumps({
        "first_publishes_first": first_publishes_first,
        "session_order": [t["results"][0]["final"] for t in turns],
        "during_session_status": [t["status"] for t in during["turns"]] if during else None,
        "latest_team_result": node["result"]["final"],
        "native_attempts": [1, 2], "same_native_uuid": True, "real_model_calls": 0,
    }, sort_keys=True))


@pytest.mark.parametrize("bound_first", [True, False])
def test_session_request_binding_and_legacy_placeholder_do_not_replace_each_other(mail_scene, bound_first):
    sessions = mail_scene.server.SESSIONS
    order = ["bound", "legacy"] if bound_first else ["legacy", "bound"]
    for name in order:
        sessions.save_turn(CONVERSATION, name, {
            "team_id": "__ut_mixed_session", "status": "running", "results": []},
            user_id=USER, **({"turn_id": "__ut_bound_request"} if name == "bound" else {}))
    # The older request finishes while the newer slot is still running.
    # Exercise both bound->legacy and legacy->bound, not just latest-first.
    for name in order:
        info = sessions.save_turn(CONVERSATION, name, {
            "team_id": "__ut_mixed_session", "status": "done",
            "results": [{"status": "done", "final": name}]},
            user_id=USER, **({"turn_id": "__ut_bound_request"} if name == "bound" else {}))
        assert not info.get("error")
    turns = sessions.load_session(CONVERSATION, user_id=USER)["turns"]
    assert [t["results"][0]["final"] for t in turns] == order
    assert [bool(t.get("turn_id")) for t in turns] == [name == "bound" for name in order]


@pytest.mark.parametrize("mismatch", ["unknown-id", "wrong-team", "wrong-owner", "already-finished"])
def test_session_bound_terminal_mismatch_cannot_fallback_to_another_turn(mail_scene, mismatch):
    sessions = mail_scene.server.SESSIONS
    team, token = "__ut_bound_session", "__ut_bound_request"
    running = {"team_id": team, "status": "running", "results": []}
    terminal = {"team_id": team, "status": "done", "results": [{"final": "owned result"}]}
    sessions.save_turn(CONVERSATION, "owned", running, user_id=USER, turn_id=token)
    if mismatch == "already-finished":
        sessions.save_turn(CONVERSATION, "owned", terminal, user_id=USER, turn_id=token)
    # Also leave an eligible-looking legacy placeholder. A mismatch must not
    # fall back to it, append an invented turn, or change the file at all.
    sessions.save_turn(CONVERSATION, "other request", running, user_id=USER)
    path = Path(sessions._path(CONVERSATION))
    before = path.read_bytes()
    result = sessions.save_turn(
        CONVERSATION, "late", {**terminal, **({"team_id": "__ut_other_team"}
                                             if mismatch == "wrong-team" else {})},
        user_id="__ut_other_owner" if mismatch == "wrong-owner" else USER,
        turn_id="__ut_unknown_request" if mismatch == "unknown-id" else token)
    assert result.get("error") == (
        "session belongs to another user" if mismatch == "wrong-owner"
        else "running turn unavailable")
    assert path.read_bytes() == before


def _source(case, *, nodes=("A",), scope="conversation"):
    _seed(case, nodes, scope=scope)
    first = _queue(case, "first")
    second = _queue(case, "second")
    out, inputs = _resume(case)
    assert out["team_id"] == first["continuation"]["team_id"] and len(inputs) == len(nodes)
    source = out["team_id"]
    for node in nodes:
        _post(case, source, node, "UNREAD_SOURCE_" + node)
    return source, second["continuation"]["team_id"]


@pytest.mark.parametrize("nodes", [("A",), ("A", "B")], ids=["single", "persistent"])
@pytest.mark.parametrize("entry", ["inproc", "isolated"])
def test_next_native_input_receives_exact_predecessor_and_fresh_current_mail(mail_scene, nodes, entry):
    case = mail_scene
    if entry == "isolated":
        # Exercise run_one's isolated branch while substituting only the native
        # leaf. Preserve its signature so cursor/policy propagation stays real.
        @wraps(case.core._run_harness_isolated)
        def native_leaf(key, task, qhash, model_override=None, appid_override=None,
                        timeout_s=120, **kwargs):
            return case.core._run_core_inprocess(
                key, task, qhash, model_override, appid_override,
                timeout_s=timeout_s, **kwargs)

        case.monkeypatch.setattr(case.core, "_CORE_INPROC_KEYS", set())
        case.monkeypatch.setattr(case.core, "_run_harness_isolated", native_leaf)
    source, child = _source(case, nodes=nodes)
    # UI read_at is not a native delivery receipt; unknown consumption replays.
    read = case.teams.send_message(source, "user", "A", "UI_READ_ONLY_SOURCE")
    state = case.teams.load(source)
    state["mailboxes"]["A"][-1]["read_at"] = 123.0
    case.teams._write(state)
    before = deepcopy(case.teams.load(source)["mailboxes"])
    attachment = {"path": str(case.work / "evidence.png"), "mime_type": "image/png",
                  "sha256": "synthetic-image-digest"}
    delivered = []

    @contextmanager
    def slot(*args, **kwargs):
        # Arrive after dispatch prelude/cursor selection, before the real input.
        if not delivered:
            for node in nodes:
                delivered.append(case.teams.send_message(
                    child, "user", node, "FRESH_CURRENT_" + node, [attachment]))
        yield

    case.monkeypatch.setattr(importlib.import_module("legion_concurrency"), "slot", slot)
    out, inputs = _resume(case)
    assert out["team_id"] == child and len(inputs) == len(nodes), out
    for call in inputs:
        node = call["facts"]["task_id"]
        missing = [prefix + node for prefix in ("FRESH_CURRENT_", "UNREAD_SOURCE_")
                   if prefix + node not in call["text"]]
        assert not missing, "mail absent at actual native input: " + repr(missing)
        assert attachment["path"] in call["text"], "keep real image refs, not just inline text"
        assert str(case.teams._path(source).resolve()) in call["text"]
        if node == "A":
            assert "UI_READ_ONLY_SOURCE" in call["text"]
        for other in set(nodes) - {node}:
            assert "UNREAD_SOURCE_" + other not in call["text"], "same harness is not same node"
            assert "FRESH_CURRENT_" + other not in call["text"]
    assert case.teams.load(source)["mailboxes"] == before
    assert all(row["read_at"] is None for node in nodes
               for row in case.teams.load(child)["mailboxes"][node]
               if row["id"] in {message["id"] for message in delivered})
    assert read["id"] > 0 and len(case.wakeups) == 2, "context must not enqueue another order"
    assert not case.picks and not case.empty_plans


def test_run_one_current_mail_retains_long_body_reference_without_claiming_delivery(mail_scene):
    case = mail_scene
    _seed(case)
    text = "LONG_CURRENT_BODY_" + "x" * 14000
    mail = case.teams.send_message(case.parent, "user", "A", text)
    before = deepcopy(case.teams.load(case.parent)["mailboxes"]["A"])
    packet = case.pc.snapshot(case.parent, "A")
    start = len(case.inputs)
    out = case.core.run_one(
        RELAY, "Continue this same node", qhash="__ut_current_input",
        user_id=USER, conversation_id=CONVERSATION + ":task:A", execution_depth="quick",
        task_facts={"team_id": case.parent, "task_id": "A", "project_context": packet})
    assert len(case.inputs) == start + 1, out
    prompt = case.inputs[-1]["text"]
    assert "TASK_MAIL_CONTEXT_JSON" in prompt
    assert mail["artifact_refs"][0]["path"] in prompt
    assert "text_omitted" in prompt and text not in prompt
    assert case.teams.load(case.parent)["mailboxes"]["A"] == before


@pytest.mark.parametrize("broken", [
    "owner", "conversation", "workspace", "harness", "task", "checkpoint", "scope",
    "checkpoint_write", "active", "never_executed", "reverse_request",
    "reverse_message", "reverse_parent", "forward_child", "forward_status",
])
def test_predecessor_mail_requires_two_way_queue_and_exact_node_native_proof(mail_scene, broken):
    case = mail_scene
    source, child = _source(case, nodes=("A", "B"))
    state = case.teams.load(source)
    packet = case.pc.load(source)
    if broken in ("owner", "conversation"):
        state["user_id" if broken == "owner" else "conversation_id"] = "__ut_foreign"
        case.teams._write(state)
    elif broken == "workspace":
        packet.update(repo_root=str(case.root / "different-work"),
                      workspace_root=str(case.root / "different-work"))
    elif broken == "harness":
        state["tasks"]["A"]["assigned_harness"] = "__ut_other_author"
        case.teams._write(state)
    elif broken == "task":
        state["tasks"]["other-A"] = state["tasks"].pop("A")
        case.teams._write(state)
    elif broken == "checkpoint":
        state["tasks"]["A"]["result"]["native_checkpoint"]["session_id"] = "__ut_other_native"
        case.teams._write(state)
    elif broken == "scope":
        state["tasks"]["A"]["result"]["native_session_scope"] = "request"
        case.teams._write(state)
    elif broken == "checkpoint_write":
        state["tasks"]["A"]["result"]["checkpoint_writes"]["request"] = False
        case.teams._write(state)
    elif broken == "active":
        state["tasks"]["A"]["active_attempt"] = state["tasks"]["A"]["attempt_seq"]
        case.teams._write(state)
    elif broken == "never_executed":
        state["tasks"]["A"]["attempt_seq"] = 0
        case.teams._write(state)
    elif broken.startswith("reverse_"):
        if broken == "reverse_parent":
            packet["continued_from_team_id"] = "__ut_unrelated_parent"
        else:
            field = "request_id" if broken == "reverse_request" else "message_id"
            packet["user_feedback_request"][field] = "__ut_unrelated_request"
    elif broken.startswith("forward_"):
        parent = case.teams.load(case.parent)
        request = next(row["continuation"] for row in parent["mailboxes"]["commander"]
                       if (row.get("continuation") or {}).get("team_id") == source)
        request["team_id" if broken == "forward_child" else "status"] = (
            "__ut_unrelated_child" if broken == "forward_child" else "held")
        case.teams._write(parent)
    # Use the same canonical persistence function used by production.
    if broken in {"workspace", "reverse_request", "reverse_message", "reverse_parent"}:
        case.pc._atomic_write(case.pc._path(source), packet)
    before = deepcopy(case.teams.load(source)["mailboxes"])
    if broken == "owner":
        start = len(case.inputs)
        # Existing resume admission rejects a cross-user child even before the
        # optional mailbox projection. Preserve that stricter behavior.
        with pytest.raises(PermissionError, match="another user"):
            _resume(case)
        assert len(case.inputs) == start and case.teams.load(source)["mailboxes"] == before
        return
    _, inputs = _resume(case)
    a = [call for call in inputs if call["facts"]["task_id"] == "A"]
    assert a, "a rejected optional mail source must not block the valid execution"
    assert all("UNREAD_SOURCE_A" not in call["text"] for call in a), broken
    assert case.teams.load(source)["mailboxes"] == before
    assert len(case.wakeups) == 2 and not case.picks


def test_request_scoped_feedback_does_not_import_previous_request_mail(mail_scene):
    case = mail_scene
    source, child = _source(case, scope="request")
    _, inputs = _resume(case)
    assert len(inputs) == 1
    assert "UNREAD_SOURCE_A" not in inputs[0]["text"]
    assert inputs[0]["facts"]["native_context_origin"]["request_id"] == child
    assert case.teams.load(source)["mailboxes"]["A"][0]["read_at"] is None


def test_each_node_uses_last_executed_predecessor_not_retained_sibling(mail_scene):
    case = mail_scene
    source, middle = _source(case, nodes=("A", "B"))
    case.nodes = ("B",)
    _, second_inputs = _resume(case)
    assert [call["facts"]["task_id"] for call in second_inputs] == ["B"]
    middle_state = case.teams.load(middle)
    assert middle_state["tasks"]["A"]["attempt_seq"] == 0
    _post(case, middle, "A", "RETAINED_NODE_NOT_EXECUTED")
    _post(case, middle, "B", "LATEST_EXECUTED_B")
    _queue(case, "third")
    case.nodes = ("A", "B")
    _, inputs = _resume(case)
    texts = {call["facts"]["task_id"]: call["text"] for call in inputs}
    assert "UNREAD_SOURCE_A" in texts["A"]
    assert "RETAINED_NODE_NOT_EXECUTED" not in texts["A"]
    assert "LATEST_EXECUTED_B" in texts["B"] and "UNREAD_SOURCE_B" not in texts["B"]
    assert str(case.teams._path(source).resolve()) in texts["A"]
    assert str(case.teams._path(middle).resolve()) in texts["B"]
    assert len(case.wakeups) == 3 and not case.picks


def test_same_session_latest_team_is_not_mailbox_lineage(mail_scene):
    case = mail_scene
    source, _ = _source(case, nodes=("A", "B"))
    unrelated = "__ut_same_session_unrelated_team"
    out = case.core.dispatch(
        "Another synthetic work request", _plan(case, {n: _node("Other " + n) for n in case.nodes}),
        qhash=unrelated, mode="orchestrate", user_id=USER, conversation_id=CONVERSATION)
    case.server.SESSIONS.save_turn(
        CONVERSATION, "Another synthetic work request", out, mode="orchestrate", user_id=USER)
    _post(case, unrelated, "A", "UNRELATED_LATEST_MAIL")
    _, inputs = _resume(case)
    a = next(call for call in inputs if call["facts"]["task_id"] == "A")
    assert "UNREAD_SOURCE_A" in a["text"] and "UNRELATED_LATEST_MAIL" not in a["text"]
    assert str(case.teams._path(source).resolve()) in a["text"]


@pytest.mark.parametrize("field", ["missing", "duplicate", "lane", "workdir", "uuid"])
def test_exact_completed_node_receipt_is_required_not_summary_alone(mail_scene, field):
    case = mail_scene
    source, _ = _source(case, nodes=("A", "B"))
    session = case.server.SESSIONS.load_session(CONVERSATION, user_id=USER)
    turn = next(turn for turn in reversed(session["turns"]) if turn["team_id"] == source)
    row = next(row for row in turn["results"] if row["task_id"] == "A")
    if field == "missing":
        turn["results"].remove(row)
    elif field == "duplicate":
        turn["results"].append(deepcopy(row))
    elif field == "lane":
        row["native_conversation_id"] = CONVERSATION + ":task:B"
    elif field == "workdir":
        row["workdir"] = str(case.root / "unrelated-work")
    else:
        row["native_checkpoint"]["session_id"] = "__ut_unrelated_native"
    Path(case.server.SESSIONS._path(CONVERSATION)).write_text(json.dumps(session))
    _, inputs = _resume(case)
    texts = {call["facts"]["task_id"]: call["text"] for call in inputs}
    assert "UNREAD_SOURCE_A" not in texts["A"]
    assert "UNREAD_SOURCE_B" in texts["B"], "one rejected receipt must not discard the other node"


@pytest.mark.parametrize("changed", ["source-owner", "source-workspace", "parent-link", "target-owner"])
def test_mail_read_rechecks_binding_after_predecessor_selection(mail_scene, changed):
    case = mail_scene
    source, child = _source(case, nodes=("A", "B"))
    original_read = case.teams.read_messages
    changed_once = []

    def read(team, recipient, *args, **kwargs):
        if team == source and recipient == "A" and not changed_once:
            changed_once.append(True)
            if changed == "source-workspace":
                packet = case.pc.load(source)
                packet.update(repo_root=str(case.root / "new-source-work"),
                              workspace_root=str(case.root / "new-source-work"))
                case.pc._atomic_write(case.pc._path(source), packet)
            elif changed == "parent-link":
                state = case.teams.load(case.parent)
                request = next(row["continuation"] for row in state["mailboxes"]["commander"]
                               if (row.get("continuation") or {}).get("team_id") == source)
                request["request_id"] = "__ut_not_the_saved_request"
                case.teams._write(state)
            else:
                state = case.teams.load(source if changed == "source-owner" else child)
                state["tasks"]["A"]["assigned_harness"] = "__ut_other_native_author"
                case.teams._write(state)
        return original_read(team, recipient, *args, **kwargs)

    case.monkeypatch.setattr(case.teams, "read_messages", read)
    _, inputs = _resume(case)
    a = next(call for call in inputs if call["facts"]["task_id"] == "A")
    assert changed_once
    assert "UNREAD_SOURCE_A" not in a["text"], "a checked snapshot must not authorize a later changed source"


def test_parent_execution_must_finish_before_child_request_begins(mail_scene):
    case = mail_scene
    parent = _seed(case, nodes=("A", "B"))
    for node in case.nodes:
        _post(case, parent, node, "ORIGINAL_PARENT_MAIL_" + node)
    state = case.teams.load(parent)
    state["tasks"]["A"]["finished_at"] += 3600
    case.teams._write(state)
    _queue(case, "first")
    _, inputs = _resume(case)
    texts = {call["facts"]["task_id"]: call["text"] for call in inputs}
    assert "ORIGINAL_PARENT_MAIL_A" not in texts["A"], "future execution is not a predecessor"
    assert "ORIGINAL_PARENT_MAIL_B" in texts["B"]


def test_missing_predecessor_record_does_not_fall_back_to_older_same_native(mail_scene):
    case = mail_scene
    source, _ = _source(case)
    _post(case, case.parent, "A", "OLDER_PARENT_NOT_EXACT_PREDECESSOR")
    removed = []

    @contextmanager
    def slot(*args, **kwargs):
        if not removed:
            case.teams._path(source).unlink()
            removed.append(True)
        yield

    case.monkeypatch.setattr(importlib.import_module("legion_concurrency"), "slot", slot)
    _, inputs = _resume(case)
    assert len(inputs) == 1 and removed
    assert inputs[0]["memory"]["native_checkpoint"]["session_id"] == "__ut_native_A"
    assert "OLDER_PARENT_NOT_EXACT_PREDECESSOR" not in inputs[0]["text"]
    assert not case.picks, "optional mail failure must not change the execution owner"
