"""Verification handoffs with real storage/HTTP; model and verifier I/O is synthetic."""
from copy import deepcopy
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest


SERVICE = Path(__file__).resolve().parents[1]
USER = "__ut_verification_alice"
CONVERSATION = "__ut_verification_original"
TARGET = "feature:verification-identity"
ACTOR = "display-only-not-an-owner"
TOKEN = "offline-verification-bearer-not-a-real-credential"
IDENTITY = {"user_id": USER, "conversation_id": CONVERSATION}


@pytest.fixture
def case(tmp_path, monkeypatch):
    for key, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
        ("CODEX_HOME", "codex"), ("LEGION_PERSONAL_ACCOUNTS_DIR", "accounts"),
        ("LEGION_SESSIONS_DIR", "sessions"), ("LEGION_TEAM_DIR", "teams"),
        ("LEGION_PROJECT_CONTEXT_DIR", "projects"), ("LEGION_USER_CONFIG_DIR", "users"),
        ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        directory = tmp_path / leaf
        directory.mkdir()
        monkeypatch.setenv(key, str(directory))
    monkeypatch.setenv("LEGION_PERSONAL_OWNER", "__ut_service_owner_not_the_caller")
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    monkeypatch.setenv("LEGION_VERIFICATION_CENTER", "1")
    monkeypatch.setenv("LEGION_API_TOKEN", "offline-instance-token-not-personal-authority")
    monkeypatch.setenv("LEGION_ALLOW_UNAUTHENTICATED_LOCAL_CLI", "0")
    monkeypatch.delenv("LEGION_PUBLIC_ORIGIN", raising=False)
    monkeypatch.syspath_prepend(str(SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"))
    monkeypatch.syspath_prepend(str(SERVICE))
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({TOKEN: {"user": USER}}))
    tokens.chmod(0o600)
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tokens))

    core = importlib.import_module("legion_core")
    workflow = importlib.import_module("verification_workflow")
    reviews = workflow.reviews
    sessions = importlib.import_module("sessions")
    monkeypatch.setattr(workflow, "_THREADS", {})
    # Review metadata and machine-result persistence stay real. Git is not needed.
    monkeypatch.setattr(workflow.evidence_schema, "_commit", lambda *a, **kw: "2b9de18")
    state = SimpleNamespace(
        root=tmp_path, mp=monkeypatch, core=core, workflow=workflow, reviews=reviews,
        sessions=sessions, threads=[], planning=[], dispatches=[], executions=[],
        receipts=[], outcomes=[False, True], planner_error=None,
    )

    def thread_factory(**kwargs):
        thread = threading.Thread(**kwargs)
        state.threads.append(thread)
        return thread

    # Track even a worker removed from _THREADS before its finally block finishes.
    monkeypatch.setattr(workflow, "threading", SimpleNamespace(
        Thread=thread_factory, get_ident=threading.get_ident))

    def wait():
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            for thread in list(state.threads):
                if thread.ident is not None:
                    thread.join(timeout=0.02)
            if not any(thread.is_alive() for thread in state.threads):
                return
            time.sleep(0.005)
        pytest.fail("isolated verification worker did not finish")

    state.wait = wait

    def execute_levels(**kwargs):
        state.executions.append(deepcopy(kwargs))
        ok = state.outcomes.pop(0) if state.outcomes else True
        return {
            "ok": ok, "run_id": kwargs["run_id"], "commit": "2b9de18",
            "levels": kwargs["levels"], "executed_levels": kwargs["levels"],
            "reused_levels": [], "evidence": [], "saved_seconds": 0,
            "failures": [] if ok else [{"level": "L0", "blockers": ["synthetic regression"]}],
        }

    def planner(question, **kwargs):
        state.planning.append({
            "question": question, **{key: value for key, value in kwargs.items() if not callable(value)}})
        if state.planner_error:
            return deepcopy(state.planner_error)
        return {"ok": True, "tasks": {"repair": {
            "task": "Synthetic repair only", "assigned_harness": "__ut_member", "depends_on": []}}}

    def dispatch(question, **kwargs):
        captured = {key: deepcopy(value) for key, value in kwargs.items() if not callable(value)}
        state.dispatches.append(captured)
        assert "cc" not in {
            task.get("assigned_harness") for task in kwargs["plan"]["tasks"].values()
        }, "personal repair must not borrow a company member"
        tasks = kwargs["plan"]["tasks"]
        return {
            "delivered_count": 1, "all_failed": False, "team_id": "__ut_repair_team",
            "results": [{"task_id": key, "harness": task["assigned_harness"], "status": "done",
                         "final": '{"verified": false, "root_cause": "synthetic: not verified"}'}
                        for key, task in tasks.items()],
        }

    def forbidden(*args, **kwargs):
        raise AssertionError("real model/native runner is forbidden in this regression")

    monkeypatch.setattr(workflow.quality_executor, "execute_levels", execute_levels)
    monkeypatch.setattr(core, "plan_request_with_model", planner)
    monkeypatch.setattr(core, "dispatch_persistent", dispatch)
    monkeypatch.setattr(core, "dispatch", dispatch)
    monkeypatch.setattr(core, "_run_one_with_gateway", forbidden)
    monkeypatch.setattr(core, "select_usable_model", forbidden)
    monkeypatch.setattr(importlib.import_module("planner_runtime"), "run_cli", forbidden)
    monkeypatch.setattr(core, "_harness_keys", lambda: ["__ut_investigator", "__ut_verifier"])
    monkeypatch.setattr(core, "_pick_relay_harness", lambda _key, used, valid: next(
        (key for key in valid if key not in used), ""))
    assert core._personal_responses_planner_enabled()

    saved = reviews.save(TARGET, "issue", actor=ACTOR, metadata={
        "feature": "隔离修复测试", "user_value": "验证原请求可以恢复",
        "acceptance_criteria": "只验证身份和运行事实，不代表产品质量",
        "quality_score": 90, "current_version": "1.0.0", "target_version": "1.0.1",
        "upgrade_description": "仅测试接力的身份与状态", "owner": USER,
        "evidence": "Synthetic regression fixture, not real quality evidence",
        "tracked_files": ["release/legion-service/verification_workflow.py"],
        "required_levels": ["L0"], "upgrade_status": "blocked", "machine_status": "issue",
        "continuous_reverify": True, "max_auto_cycles": 2,
    })
    assert saved["ok"], saved
    sessions.save_turn(CONVERSATION, "Synthetic original request", {"results": []}, user_id=USER)
    sessions.save_turn("__ut_bob_conversation", "Synthetic other request", {"results": []},
                       user_id="__ut_verification_bob")
    yield state
    wait()
    (tmp_path / "observations.json").write_text(json.dumps({
        "model_execution": "stubbed; no real model calls",
        "machine_verifier": "synthetic L0 result, not a real product verification",
        "planning": state.planning, "dispatches": state.dispatches,
        "executions": state.executions, "http": state.receipts,
        "registered_workers": list(workflow._THREADS),
        "runs": workflow.list_runs(TARGET),
    }, ensure_ascii=False, indent=2) + "\n")


def persisted(case, run_id):
    """Read the actual file, not a mock, in-memory row or get() repair projection."""
    return json.loads(case.workflow._path(run_id).read_text())


def assert_retryable(case, first, **kwargs):
    again = case.workflow.start_reverify(TARGET, actor=ACTOR, **kwargs)
    case.wait()
    assert again.get("status") != "already_running", again
    assert again["run"]["run_id"] != first["run"]["run_id"]


def test_missing_identity_followup_is_persisted_terminal_and_same_request_can_retry(case):
    first = case.workflow.start_reverify(TARGET, actor=USER, continuous=True)
    case.wait()
    row = persisted(case, first["run"]["run_id"])
    assert row["status"] == "blocked", row
    assert row["result"]["repair_start"]["error"] == "planner_identity_required"
    assert row["result"]["repair_run_id"] is None
    assert row["result"]["execution"]["failures"][0]["blockers"] == ["synthetic regression"]
    assert not case.planning and not case.dispatches
    assert_retryable(case, first, continuous=True)


def test_followup_exception_is_persisted_and_does_not_leave_a_workerless_running_parent(case):
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic followup start failure")

    case.mp.setattr(case.workflow, "start_repair", broken)
    first = case.workflow.start_reverify(TARGET, actor=ACTOR, continuous=True)
    case.wait()
    row = persisted(case, first["run"]["run_id"])
    assert row["status"] == "failed", row
    assert row["result"]["repair_start"]["error"] == "RuntimeError: synthetic followup start failure"
    assert not case.planning and not case.dispatches
    assert_retryable(case, first, continuous=True)


@pytest.mark.parametrize("failure_point", ["prepare", "thread_start"])
def test_repair_start_exception_is_durable_and_does_not_block_the_next_attempt(case, failure_point):
    original_threads = case.workflow.threading
    original_record = case.reviews.record_repair

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic repair startup failure")

    if failure_point == "prepare":
        case.mp.setattr(case.reviews, "record_repair", broken)
    else:
        case.mp.setattr(case.workflow, "threading", SimpleNamespace(
            Thread=lambda **kwargs: SimpleNamespace(start=broken), get_ident=threading.get_ident))
    first = case.workflow.start_repair(TARGET, actor=ACTOR, auto_reverify=False, **IDENTITY)
    assert not first["ok"]
    row = persisted(case, first["run"]["run_id"])
    assert row["status"] == "failed"
    assert row["result"]["start_error"] == "RuntimeError: synthetic repair startup failure"
    assert not case.workflow._THREADS and not case.planning
    assert case.reviews.get(TARGET)["machine_status"] not in {"queued", "running"}
    case.mp.setattr(case.workflow, "threading", original_threads)
    case.mp.setattr(case.reviews, "record_repair", original_record)
    second = case.workflow.start_repair(TARGET, actor=ACTOR, auto_reverify=False, **IDENTITY)
    case.wait()
    assert second["ok"] and second.get("status") != "already_running", second
    assert second["run"]["run_id"] != first["run"]["run_id"]


def test_reverify_repair_reverify_retains_identity_and_starts_a_new_verifier(case):
    first = case.workflow.start_reverify(TARGET, actor=ACTOR, continuous=True, **IDENTITY)
    case.wait()
    parent = persisted(case, first["run"]["run_id"])
    repair = persisted(case, parent["result"]["repair_run_id"])
    reverified = persisted(case, repair["result"]["reverify_run_id"])
    assert [parent["status"], repair["status"], reverified["status"]] == [
        "failed", "completed", "completed"]
    assert reverified["run_id"] != parent["run_id"], "must not rejoin the workerless parent"
    assert [parent["cycle"], repair["cycle"], reverified["cycle"]] == [1, 2, 2]
    for row in (parent, repair, reverified):
        assert (row["user_id"], row["conversation_id"], row["actor"]) == (USER, CONVERSATION, ACTOR)
    assert len(case.planning) == len(case.dispatches) == 1
    for call in (*case.planning, *case.dispatches):
        assert (call["user_id"], call["conversation_id"]) == (USER, CONVERSATION)
    assert len(case.executions) == 2
    assert case.reviews.get(TARGET)["machine_status"] == "pass"


def test_personal_planning_refusal_preserves_facts_without_company_fallback(case):
    case.planner_error = {"ok": False, "error": "synthetic_provider_denied",
                          "blocked_before_native": True, "execution_calls": 0}
    first = case.workflow.start_reverify(TARGET, actor=ACTOR, continuous=True, **IDENTITY)
    case.wait()
    parent = persisted(case, first["run"]["run_id"])
    child = persisted(case, parent["result"]["repair_run_id"])
    assert parent["status"] == "failed" and child["status"] == "blocked"
    assert child["result"]["planning"] == case.planner_error
    assert not case.dispatches
    assert_retryable(case, first, continuous=True, **IDENTITY)


@pytest.mark.parametrize("raises", [False, True])
def test_repair_records_reverify_rejection_or_exception_instead_of_claiming_completion(case, raises):
    def reject(*args, **kwargs):
        if raises:
            raise RuntimeError("synthetic reverify failure")
        return {"ok": False, "error": "synthetic reverify refusal"}

    case.mp.setattr(case.workflow, "start_reverify", reject)
    started = case.workflow.start_repair(TARGET, actor=ACTOR, **IDENTITY)
    case.wait()
    row = persisted(case, started["run"]["run_id"])
    assert row["status"] == ("failed" if raises else "blocked")
    assert row["result"]["reverify_start"] == {
        "ok": False, "error": "RuntimeError: synthetic reverify failure" if raises
        else "synthetic reverify refusal"}
    assert row["result"]["reverify_run_id"] is None
    assert row["result"]["dispatch"]["delivered_count"] == 1
    assert case.reviews.get(TARGET)["machine_status"] != "running"


@pytest.mark.parametrize("action", ["repair", "reverify_release", "investigate_root_cause"])
def test_recommended_execution_forwards_identity_to_the_actual_worker(case, action):
    # Recommendation quality is unchanged; only its execution handoff is under test.
    case.mp.setattr(case.reviews, "product_manager_decision", lambda *a, **kw: {
        "action": action, "autonomous": True})
    case.outcomes = [True]
    result = case.workflow.run_recommended(TARGET, actor=ACTOR, **IDENTITY)
    case.wait()
    assert result["ok"], result
    row = persisted(case, result["run"]["run_id"])
    assert (row["user_id"], row["conversation_id"]) == (USER, CONVERSATION)
    for call in (*case.planning, *case.dispatches):
        assert (call["user_id"], call["conversation_id"]) == (USER, CONVERSATION)
    if action == "investigate_root_cause":
        assert row["status"] == "blocked"
        assert case.reviews.get(TARGET)["root_cause_status"] != "verified"


def test_root_cause_cannot_borrow_verification_center_identity(case):
    result = case.workflow.start_root_cause_investigation(TARGET, actor=USER)
    case.wait()
    assert result == {"ok": False, "error": "planner_identity_required"}
    assert not case.dispatches and not case.workflow.list_runs(TARGET)


def configure_quality_cycle(case):
    finding = {"id": "__ut_finding", "target": TARGET, "category": "regression"}
    case.mp.setattr(case.workflow, "quality_intelligence", lambda *a, **kw: {
        "findings": [finding], "user_outcome_ranking": [{"finding_id": finding["id"]}]})


def test_quality_cycle_preserves_execution_identity_without_changing_the_recommendation(case):
    configure_quality_cycle(case)
    case.outcomes = [True]
    result = case.workflow.run_quality_operator_once([], actor=ACTOR, **IDENTITY)
    case.wait()
    assert result["status"] == "started", result
    row = persisted(case, result["action"]["run"]["run_id"])
    assert (row["user_id"], row["conversation_id"]) == (USER, CONVERSATION)
    assert case.planning[0]["user_id"] == USER


@pytest.fixture
def http(case):
    server_module = importlib.import_module("server")
    listener = ThreadingHTTPServer(("127.0.0.1", 0), server_module.Handler)
    listener.api_token = "offline-instance-token-not-personal-authority"
    thread = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.01},
                              daemon=True)
    thread.start()

    def post(payload, authenticated=True):
        connection = HTTPConnection(*listener.server_address, timeout=3)
        headers = {"Content-Type": "application/json",
                   "Origin": "http://127.0.0.1:" + str(listener.server_port)}
        if authenticated:
            headers["Authorization"] = "Bearer " + TOKEN
        try:
            connection.request("POST", "/api/verification/review", json.dumps(payload), headers)
            response = connection.getresponse()
            body = json.loads(response.read())
            case.wait()
            case.receipts.append({"request": payload, "authenticated": authenticated,
                                  "address": list(listener.server_address),
                                  "status": response.status, "body": body})
            return response.status, body
        finally:
            connection.close()

    yield post
    listener.shutdown()
    listener.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize("action", ["repair", "reverify", "run_recommended", "run_quality_cycle_once"])
def test_localhost_http_uses_authenticated_user_and_existing_conversation(case, http, action):
    if action == "run_quality_cycle_once":
        configure_quality_cycle(case)
    case.outcomes = [False, True] if action == "reverify" else [True]
    status, result = http({"action": action, "target": TARGET, "actor": ACTOR,
                           "session_id": CONVERSATION})
    assert status == 200 and result["ok"], result
    run = result["action"]["run"] if action == "run_quality_cycle_once" else result["run"]
    row = persisted(case, run["run_id"])
    assert row.get("user_id") == USER and row.get("conversation_id") == CONVERSATION
    assert len(case.planning) == len(case.dispatches) == 1
    assert all(call["user_id"] == USER and call["conversation_id"] == CONVERSATION
               for call in (*case.planning, *case.dispatches))
    assert all(item["status"] not in {"queued", "running"} for item in case.workflow.list_runs(TARGET))


@pytest.mark.parametrize("alias", ["sid", "conversation_id"])
def test_localhost_http_keeps_server_validated_conversation_aliases(case, http, alias):
    case.outcomes = [True]
    status, result = http({"action": "repair", "target": TARGET, alias: CONVERSATION})
    assert status == 200 and result["ok"], result
    assert case.planning[0]["conversation_id"] == CONVERSATION


@pytest.mark.parametrize("overrides,authenticated,status", [
    ({"uid": "__ut_verification_bob"}, True, 403),
    ({"user_id": "__ut_verification_bob"}, True, 403),
    ({"session_id": "__ut_bob_conversation"}, True, 403),
    ({"conversation_id": "__ut_bob_conversation"}, True, 403),
    ({}, False, 403),
])
def test_localhost_http_rejects_forged_or_unauthenticated_subjects(case, http, overrides,
                                                               authenticated, status):
    payload = {"action": "repair", "target": TARGET, "actor": USER, "session_id": CONVERSATION}
    payload.update(overrides)
    actual, result = http(payload, authenticated=authenticated)
    assert actual == status, result
    assert not case.planning and not case.dispatches and not case.workflow.list_runs(TARGET)


def test_localhost_http_missing_conversation_does_not_invent_identity(case, http):
    status, result = http({"action": "repair", "target": TARGET, "actor": USER, "uid": USER})
    assert status == 400 and result == {"ok": False, "error": "planner_identity_required"}
    assert not case.planning and not case.dispatches and not case.workflow.list_runs(TARGET)


def test_localhost_http_reverify_refusal_is_durable_and_request_is_retryable(case, http):
    payload = {"action": "reverify", "target": TARGET, "actor": USER, "continuous": True}
    status, first = http(payload)
    assert status == 200
    row = persisted(case, first["run"]["run_id"])
    status, second = http(payload)
    assert row["status"] == "blocked"
    assert row["result"]["repair_start"]["error"] == "planner_identity_required"
    assert status == 200 and second.get("status") != "already_running"
    assert second["run"]["run_id"] != first["run"]["run_id"]
    assert not case.planning and not case.dispatches
