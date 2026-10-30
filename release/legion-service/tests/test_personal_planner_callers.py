"""Trusted planner call sites, no HTTP listener, native process or model."""
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace

import pytest

from test_personal_native_planner import (
    USER, CONVERSATION, planner_case, isolated, explicit_run_one,
)
from test_persistent_native_continuation import loop


class InlineThread:
    """Only exercise caller routing; heartbeat concurrency is not under test."""
    def __init__(self, target, args=(), kwargs=None, **ignored):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        if self.target.__name__ not in ("_dispatch_hb", "_plan_hb_thread"):
            self.target(*self.args, **self.kwargs)

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


def failure(kind="config", *, before=False):
    result = {
        "ok": False, "status": "error", "error": "synthetic planner failure",
        "planner_transport": "native_codex", "planner_attempts": [],
        "blocked_before_native": before, "execution_calls": 0 if before else 1,
        "error_envelope": {"category": "model_config", "scope": "request",
                           "retryable": False, "code": "fixture_config", "message": "fixture"},
    }
    if kind == "provider":
        result["error_envelope"].update(
            category="authorization_required", scope="provider", code="appid_disabled")
    elif kind == "cancelled":
        result.update(status="cancelled", cancelled=True, error="planner_cancelled")
    elif kind == "unconfirmed":
        result.pop("error_envelope")
        result["native_facts"] = {"native_turn_terminal": "unobserved",
                                  "cli_wait_timed_out": True}
    return result


@pytest.fixture
def http_case(planner_case):
    case, mp = planner_case, planner_case.monkeypatch
    server = importlib.import_module("server")
    mp.setattr(server.threading, "Thread", InlineThread)
    mp.setattr(server, "_source_version_identity", lambda: {"commit": "offline-fixture"})
    mp.setattr(case.core, "_harness_keys", lambda: {"codex", "cc"})
    mp.setattr(case.core, "pilot_settings", lambda uid: {"enabled": False})
    mp.setattr(case.core, "_appoint_runtime_captain", lambda *a, **kw: "")
    mp.setattr(importlib.import_module("harness_performance"), "route_policy",
               lambda scene: {"verified": False, "route": "strongest_single"})
    mp.setattr(server.ARTIFACTS, "enrich", lambda *a, **kw: None)
    mp.setattr(server.DESKTOP_FS, "materialize_session", lambda *a, **kw: {})
    mp.setattr(server.DESKTOP_FS, "sync_personal_data", lambda *a, **kw: None)
    planned, dispatched, events, elections = [], [], [], []
    case.planned_result = failure()
    case.inherited_results = [{"key": "fixture-election", "status": "error"}]

    def planner(*args, **kwargs):
        planned.append((args, kwargs))
        return deepcopy(case.planned_result)

    def dispatch(*args, **kwargs):
        dispatched.append((args, kwargs))
        return {"results": [{"key": "codex", "harness": "codex",
                             "status": "done", "final": "offline member reply"}],
                "delivered_count": 1}

    def elect(*args, **kwargs):
        elections.append((args, kwargs))
        return {"ok": False, "error": "fixture election unavailable",
                "provider_results": deepcopy(case.inherited_results)}

    mp.setattr(case.core, "plan_request_with_model", planner)
    mp.setattr(case.core, "dispatch", dispatch)
    mp.setattr(case.core, "dispatch_persistent", dispatch)
    mp.setattr(case.core, "elect_captain", elect)

    def request(mode="auto", *, default=False, project_allowed=True):
        handler = server.Handler.__new__(server.Handler)
        handler.path = "/api/legion/dispatch"
        body = {"question": "原项目的完整目标", "mode": mode, "session_id": CONVERSATION,
                "uid": "untrusted-body-owner", "team_id": "__ut_planner_http_" + mode,
                "plan": {} if default else {"team_policy": {"candidate_harnesses": ["codex", "cc"]}}}
        handler._read_json_body = lambda: deepcopy(body)
        handler._personal_user = lambda: USER  # Already-authenticated transport subject.
        handler._project_access_allowed = lambda *a: project_allowed
        handler._project_owner_allowed = lambda *a: project_allowed
        handler._sse_open = lambda: None
        handler._sse_send = lambda kind, data, **kw: (events.append((kind, deepcopy(data))) or True)
        handler._send_json = lambda value, status=200: events.append(("json", value))
        handler._do_POST_inner()
        return next((data for kind, data in reversed(events) if kind == "final"), None)

    return SimpleNamespace(case=case, server=server, planned=planned, dispatched=dispatched,
                           events=events, elections=elections, request=request)


@pytest.mark.parametrize("mode", ["auto", "orchestrate"])
def test_both_http_planner_calls_forward_trusted_identity_cancel_and_provider_context(http_case, mode):
    h = http_case
    result = h.request(mode)
    assert len(h.planned) == 1, h.events
    args, kwargs = h.planned[0]
    assert kwargs.get("user_id") == USER
    assert kwargs.get("conversation_id") == CONVERSATION
    assert callable(kwargs.get("cancel_check")) and kwargs["cancel_check"]() is False
    if mode == "orchestrate":
        assert kwargs["_provider_results"]() == h.case.inherited_results
    assert result and result["ok"] is False, h.events
    assert not h.dispatched, "a personal planning failure must not become an old-provider task"


@pytest.mark.parametrize("mode", ["auto", "orchestrate"])
@pytest.mark.parametrize("kind", ["config", "cancelled", "provider", "unconfirmed"])
def test_http_planning_failure_keeps_runtime_facts_and_never_dispatches(http_case, mode, kind):
    h = http_case
    h.case.planned_result = failure(kind)
    result = h.request(mode)
    assert not h.dispatched, h.events
    assert result and result["ok"] is False, h.events
    assert result.get("execution_calls") == 1
    assert result.get("blocked_before_native") is False
    assert result.get("error_envelope") == h.case.planned_result.get("error_envelope")
    if kind == "cancelled":
        assert result["status"] == "cancelled" and result["cancelled"] is True
    else:
        assert result["outcome"]["failure_stage"] == "planning"
    if kind in ("provider", "unconfirmed"):
        assert result["status"] == "blocked"
    if kind == "unconfirmed":
        assert result["native_facts"] == h.case.planned_result["native_facts"]


@pytest.mark.parametrize("mode", ["auto", "orchestrate"])
def test_planner_preparation_exception_does_not_leak_through_either_http_path(http_case, mode):
    h = http_case
    calls = []

    def planner(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("fixture-private-key and a fixture-private-prompt")

    h.case.monkeypatch.setattr(h.case.core, "plan_request_with_model", planner)
    result = h.request(mode)
    assert result and not result["ok"], h.events
    assert result["planner_error"] == "planner_call:RuntimeError"
    assert len(calls) == 1 and not h.dispatched
    for private in ("fixture-private-key", "fixture-private-prompt"):
        assert private not in json.dumps(h.events)


def test_default_owner_and_denied_project_add_no_planning_call(http_case):
    h = http_case
    assert h.request(default=True, project_allowed=False) is None
    assert not h.dispatched and not h.planned
    result = h.request(default=True)
    assert result and len(h.dispatched) == 1, h.events
    assert not h.planned and not h.elections
    kwargs = h.dispatched[0][1]
    assert kwargs["mode"] == "single" and kwargs["user_id"] == USER
    assert kwargs["conversation_id"] == CONVERSATION


def test_empty_continuation_forwards_identity_and_observes_the_real_failure(planner_case):
    case, calls, observations = planner_case, [], []
    proposed = failure("provider", before=True)

    def planner(*args, **kwargs):
        calls.append((args, kwargs))
        return deepcopy(proposed)

    case.monkeypatch.setattr(case.core, "plan_request_with_model", planner)
    provider_results = lambda: [{"key": "already-rejected"}]
    cancelled = lambda: False
    original = {"conversation_context": "original project context"}
    updated, error = case.core._plan_empty_continuation(
        "original", original, {}, "__ut_continuation_planner", "remaining work",
        user_id=USER, conversation_id=CONVERSATION, cancel_check=cancelled,
        _provider_results=provider_results, _result_observer=observations.append)
    assert updated == original and error == proposed["error"]
    assert observations == [proposed]
    kwargs = calls[0][1]
    assert kwargs["user_id"] == USER and kwargs["conversation_id"] == CONVERSATION
    assert kwargs["cancel_check"] is cancelled and kwargs["_provider_results"] is provider_results


@pytest.mark.parametrize("kind", ["provider", "cancelled", "config"])
def test_persistent_empty_plan_uses_its_trusted_caller_and_keeps_failure(loop, kind):
    case, calls = loop, []
    case.results[0].pop("orchestration_request")
    proposed = failure(kind, before=True)
    case.mp.setattr(case.pilot, "evaluate", lambda *a, **kw: {
        "action": "continue", "kind": "fixture_open_goal", "next_action": "Remaining work"})

    def planner(*args, **kwargs):
        calls.append((args, kwargs))
        return deepcopy(proposed)

    case.mp.setattr(case.core, "plan_request_with_model", planner)
    result = case.core.dispatch_persistent(
        "original objective", deepcopy(case.plan), qhash="__ut_empty_plan_" + kind,
        user_id="fixture", conversation_id=CONVERSATION, max_rounds=2)
    assert len(case.calls) == 1 and len(calls) == 1, result
    kwargs = calls[0][1]
    assert kwargs.get("user_id") == "fixture"
    assert kwargs.get("conversation_id") == CONVERSATION
    assert callable(kwargs.get("cancel_check")) and callable(kwargs.get("_provider_results"))
    assert kwargs["_provider_results"]()[0]["task_id"] == "maker"
    assert result["continuation_planning"] == proposed
    assert result["pilot"]["action"] == "stop"
    if kind == "cancelled":
        assert result["cancelled"] is True
    else:
        assert result["error_envelope"] == proposed["error_envelope"]
        assert result["blocked_before_native"] is True
    assert result["results"][0]["final"] == case.results[0]["final"]


@pytest.fixture
def repair_case(planner_case):
    case, mp = planner_case, planner_case.monkeypatch
    workflow = importlib.import_module("verification_workflow")
    mp.setattr(workflow.threading, "Thread", InlineThread)
    review = {"target": "feature:fixture", "tracked_files": ["fixture.py"]}
    mp.setattr(workflow.reviews, "get", lambda target: deepcopy(review))
    mp.setattr(workflow.reviews, "record_repair", lambda *a, **kw: None)
    mp.setattr(workflow, "_levels", lambda *a: ["L1"])
    mp.setattr(workflow, "_repair_question", lambda *a: "Synthetic repair request")
    claimed, planned, dispatched = [], [], []

    def claim(target, kind, row):
        claimed.append(row)
        return row, True

    mp.setattr(workflow, "_claim_run", claim)
    mp.setattr(workflow, "_event", lambda row, stage, message, **kw: row.update(
        status=stage, message=message))
    case.repair_plan = failure()

    def planner(*args, **kwargs):
        planned.append((args, kwargs))
        return deepcopy(case.repair_plan)

    def dispatch(*args, **kwargs):
        dispatched.append((args, kwargs))
        return {"results": [], "delivered_count": 0}

    mp.setattr(case.core, "plan_request_with_model", planner)
    mp.setattr(case.core, "dispatch_persistent", dispatch)
    return SimpleNamespace(case=case, workflow=workflow, claimed=claimed,
                           planned=planned, dispatched=dispatched)


def test_verification_actor_is_not_a_trusted_execution_identity(repair_case):
    h = repair_case
    result = h.workflow.start_repair("feature:fixture", actor=USER, auto_reverify=False)
    assert not result["ok"] and result["error"] == "planner_identity_required"
    assert not h.claimed and not h.planned and not h.dispatched


@pytest.mark.parametrize("success", [False, True])
def test_verification_explicit_identity_is_forwarded_without_cc_failure_fallback(repair_case, success):
    h = repair_case
    if success:
        h.case.repair_plan = {"ok": True, "tasks": {"repair": {
            "task": "Synthetic repair", "assigned_harness": "codex"}}}
    result = h.workflow.start_repair(
        "feature:fixture", actor="display-only-actor", user_id=USER,
        conversation_id=CONVERSATION, auto_reverify=False)
    assert result["ok"] and len(h.planned) == 1
    assert h.planned[0][1]["user_id"] == USER
    assert h.planned[0][1]["conversation_id"] == CONVERSATION
    assert callable(h.planned[0][1]["cancel_check"])
    assert len(h.dispatched) == int(success)
    if success:
        assert h.dispatched[0][1]["user_id"] == USER
        assert h.dispatched[0][1]["conversation_id"] == CONVERSATION
    else:
        assert h.claimed[0]["result"]["planning"] == h.case.repair_plan
