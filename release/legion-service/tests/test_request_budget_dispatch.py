"""请求预算与失败接力：真实 dispatch/persistent -> run_one -> 持久层。

复用禁网络、禁真实进程的 CPU 场景，仅 native 执行/能力发现为替身。
预算来自明确的 typed runtime 事实，不经过实验代理，不读取模型回答猜预算。
"""
from copy import deepcopy
import hashlib
import importlib
from pathlib import Path

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, USER, CONVERSATION
from test_native_terminal_dispatch_graph import graph, ORIGINAL, RELAY, REPAIR, WRITER, _node, _plan, _rows, _returned


BUDGET = {
    "category": "runtime_budget", "scope": "request", "retryable": False,
    "tool": "",
    "code": "request_total_budget_exhausted",
    "message": "The original request has exhausted its total model budget.",
    "details": {"budget_owner": "request", "dimension": "input_tokens",
                "limit": 1048576, "used": 1048576},
}
PROCESS = {
    "category": "process", "scope": "harness", "retryable": True,
    "tool": "", "details": {},
    "code": "rc1", "message": "The original native process failed.",
}
RATE_LIMIT = {
    "category": "rate_limit", "scope": "provider", "retryable": True,
    "tool": "", "details": {},
    "code": "HTTP 429", "message": "This provider requires backoff.",
}


def _failed(case, key, envelope, *, artifact=False, call=None):
    if artifact:
        path = case.work / (key + ".txt")
        path.write_text("file from " + key)
        # The native leaf has already reported a real file before failing.
        # Keep exporter authorization and task-memory persistence real; do not
        # replace the product's intentionally restricted project-file scanner.
        exported = case.ad.export_project_artifacts(
            [path.name], team_id=case.active_team, task_id="A",
            user_id=USER, conversation_id=CONVERSATION)
        assert len(exported["artifacts"]) == 1, exported
        case.tm.update(call["memory_id"], key, artifacts=exported["artifacts"])
    return {
        "status": "error", "final": "", "error": envelope["message"],
        "error_envelope": deepcopy(envelope), "fail_kind": envelope["category"],
        "workdir": str(case.work), "model_used": "fixture-model",
        "native_checkpoint": {"kind": "codex_thread", "session_id": "__ut_cursor_" + key},
        "native_facts": {"process_returncode": 1, "turn_status": "failed"},
    }


def _choose_relay(case):
    def pick(failed, tried, valid, *args):
        case.picks.append({"failed": failed, "tried": sorted(tried)})
        return RELAY if RELAY not in tried else None
    case.monkeypatch.setattr(case.core, "_pick_relay_harness", pick)


def _execute(case, entry, team, *, extra_tasks=None):
    case.active_team = team
    tasks = {"A": _node("Continue the original fixture", owner=ORIGINAL)}
    tasks.update(extra_tasks or {})
    plan = _plan(case, tasks, failover=2)
    kwargs = {
        "qhash": team, "mode": "pk", "user_id": USER,
        "conversation_id": CONVERSATION, "cb": case.events.append,
    }
    if entry == "persistent":
        return case.core.dispatch_persistent("Original fixture request", plan, max_rounds=3, **kwargs)
    return case.core.dispatch("Original fixture request", plan, **kwargs)


@pytest.mark.parametrize("entry", ["dispatch", "persistent"])
def test_explicit_request_budget_stops_before_selecting_or_running_a_relay(graph, entry):
    case = graph
    _choose_relay(case)
    case.answer = lambda call: _failed(case, call["key"], BUDGET, artifact=True, call=call)
    out = _execute(case, entry, "__ut_request_budget_" + entry)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL]
    assert not case.picks, "request-level exhaustion must not be treated as another author's chance"
    row = _rows(out)["A"]
    assert row["status"] == "error" and row["error_envelope"] == BUDGET
    assert row["harness"] == row["assigned_harness"] == ORIGINAL
    assert (case.work / (ORIGINAL + ".txt")).read_text() == "file from " + ORIGINAL
    saved = case.teams.load("__ut_request_budget_" + entry)["tasks"]["A"]
    assert saved["attempt_seq"] == 1 and saved["result"]["error_envelope"] == BUDGET
    if entry == "persistent":
        assert len(case.rounds) == 1 and not case.empty_plans
        assert out["pilot"]["action"] == "stop"
        assert out["pilot"]["kind"] == "request_budget_exhausted"


@pytest.mark.parametrize("envelope", [
    PROCESS,
    {**PROCESS, "category": "harness_runtime", "retryable": False},
    {**BUDGET, "scope": "harness"},
], ids=["process", "nonretryable-harness-runtime", "harness-only-budget"])
def test_harness_scoped_failures_still_allow_a_real_relay(graph, envelope):
    case = graph
    _choose_relay(case)
    case.answer = lambda call: (
        _failed(case, call["key"], envelope)
        if call["key"] == ORIGINAL else _returned(case, "__ut_relay_success"))
    out = _execute(case, "persistent", "__ut_harness_fault_" + envelope["category"])
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, RELAY]
    row = _rows(out)["A"]
    assert row["status"] == "done" and row["harness"] == RELAY and row["via_relay"] is True
    assert len(case.rounds) == 1


@pytest.mark.parametrize("entry", ["dispatch", "persistent"])
@pytest.mark.parametrize("pause", [RATE_LIMIT, BUDGET], ids=["provider-backoff", "request-budget"])
def test_failed_relay_new_pause_reaches_original_result_and_both_persistent_stores(graph, entry, pause):
    case = graph
    _choose_relay(case)
    case.answer = lambda call: _failed(
        case, call["key"], PROCESS if call["key"] == ORIGINAL else pause, artifact=True, call=call)
    team = "__ut_failed_relay_" + entry + "_" + pause["category"]
    out = _execute(case, entry, team)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, RELAY]
    row = _rows(out)["A"]
    assert row["status"] == "error" and row["error_envelope"] == pause
    assert row["task_id"] == "A" and row["harness"] == RELAY
    assert row["assigned_harness"] == out["task_assignments"]["A"] == RELAY
    assert not row.get("via_relay"), "failed relay is not delivered work"
    previous = next(item for item in row["prior_attempts"]
                    if item.get("harness") == ORIGINAL and item.get("error_envelope") == PROCESS)
    assert previous["native_checkpoint"]["session_id"] == "__ut_cursor_" + ORIGINAL
    names = {item.get("name") or Path(item["path"]).name for item in row["artifacts"]}
    assert {ORIGINAL + ".txt", RELAY + ".txt"} <= names
    for key in (ORIGINAL, RELAY):
        path = case.work / (key + ".txt")
        expected = ("file from " + key).encode()
        assert path.read_bytes() == expected
        assert hashlib.sha256(expected).hexdigest() in {
            item.get("sha256") for item in row["artifacts"]}

    saved = case.teams.load(team)["tasks"]["A"]
    assert saved["status"] == "error" and saved["assigned_harness"] == RELAY
    assert saved["result"]["error_envelope"] == pause
    assert saved["result"]["native_checkpoint"]["session_id"] == "__ut_cursor_" + RELAY
    history = saved["result_history"]
    assert any(item["assigned_harness"] == ORIGINAL
               and item["result"]["error_envelope"] == PROCESS for item in history)
    packet = case.pc.load(team)
    assert any(item["harness"] == ORIGINAL and item["error_envelope"] == PROCESS
               for item in packet["attempts"])
    assert any(item["harness"] == RELAY and item["error_envelope"] == pause
               for item in packet["attempts"])
    assert {ORIGINAL + ".txt", RELAY + ".txt"} <= {item["name"] for item in packet["artifacts"]}
    if entry == "persistent":
        assert len(case.rounds) == 1 and not case.empty_plans
        assert out["pilot"]["action"] == "stop"
        assert out["pilot"]["kind"] == (
            "request_budget_exhausted" if pause is BUDGET else "provider_backoff")


@pytest.mark.parametrize("override", [
    {"scope": "harness"},
    {"scope": "tool"},
    {"retryable": True},
    {"retryable": None},
    {"category": "native_iteration_budget"},
    {"category": "rate_limit", "scope": "provider"},
    {"category": "process"},
], ids=["harness", "tool", "retryable", "unknown-retry", "iteration",
        "provider-429", "process"])
def test_request_budget_helper_does_not_invent_a_request_stop_from_other_failures(graph, override):
    errors = importlib.import_module("error_envelope")
    predicate = getattr(errors, "requires_request_budget_pause", None)
    assert callable(predicate), "a typed request-budget predicate is needed; do not classify answer text"
    assert not predicate({"status": "error", "error_envelope": {**BUDGET, **override}})


def test_request_budget_pause_uses_typed_facts_not_http_status_or_answer_words(graph):
    errors = importlib.import_module("error_envelope")
    assert errors.requires_execution_pause({"status": "error", "error_envelope": BUDGET})
    predicate = getattr(errors, "requires_request_budget_pause", None)
    assert callable(predicate)
    assert predicate({"status": "error", "error_envelope": BUDGET})
    assert not predicate({"status": "error", "error": "HTTP 429 experiment_budget_exhausted"})
    assert not predicate({"status": "done", "final": "runtime_budget/request/retryable=false"})
    assert not predicate({"status": "cancelled", "error_envelope": BUDGET})
    assert not predicate({"status": "error", "error_envelope": {
        k: v for k, v in BUDGET.items() if k != "retryable"}})


def test_relay_is_bound_to_the_same_owned_project_before_its_native_call(graph):
    case = graph
    _choose_relay(case)
    observed = []
    def answer(call):
        packet = case.pc.load(case.active_team)
        task = next(node for node in packet["tasks"] if node["task_id"] == "A")
        observed.append((call["key"], task["assigned_harness"], packet["user_id"],
                         packet["conversation_id"], packet["repo_root"]))
        return _failed(case, call["key"], PROCESS) if call["key"] == ORIGINAL else _returned(case, "__ut_bound")
    case.answer = answer
    team = "__ut_relay_prebound"
    out = _execute(case, "dispatch", team)
    assert [row[:2] for row in observed] == [(ORIGINAL, ORIGINAL), (RELAY, RELAY)]
    assert all(row[2:] == (USER, CONVERSATION, str(case.work)) for row in observed)
    assert _rows(out)["A"]["status"] == "done"


def test_failed_relay_binding_does_not_run_unattributed_native_work(graph):
    case = graph
    _choose_relay(case)
    case.answer = lambda call: _failed(case, call["key"], PROCESS)
    original = case.pc.record_task_bindings
    def bind(team_id, tasks, **kwargs):
        if any(node.get("assigned_harness") == RELAY for node in tasks.values()):
            raise PermissionError("synthetic binding refusal")
        return original(team_id, tasks, **kwargs)
    case.monkeypatch.setattr(case.pc, "record_task_bindings", bind)
    out = _execute(case, "dispatch", "__ut_relay_binding_refused")
    assert [call["key"] for call in case.native_calls] == [ORIGINAL]
    assert _rows(out)["A"]["status"] == "error"
    assert any(event.get("phase") == "接力归属校验" and event.get("status") == "blocked"
               for event in case.events)


def test_relay_binding_does_not_restore_revoked_project_grant(graph):
    case = graph
    _choose_relay(case)
    tokens = Path(__import__("os").environ["LEGION_PERSONAL_TOKENS_FILE"])
    def answer(call):
        if call["key"] == ORIGINAL:
            return _failed(case, call["key"], PROCESS)
        tokens.write_text("{}")
        result = _failed(case, call["key"], PROCESS)
        path = case.work / "relay-unapproved.txt"
        path.write_text("not authorized after revocation")
        result["artifacts"] = [{"path": str(path), "name": path.name}]
        return result
    case.answer = answer
    out = _execute(case, "dispatch", "__ut_relay_revoked")
    row = _rows(out)["A"]
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, RELAY]
    assert not any(item.get("name") == "relay-unapproved.txt" for item in row.get("artifacts") or [])
    notes = row.get("artifact_safety_observations") or []
    assert any(note.get("reason") == "artifact_project_not_authorized" for note in notes), notes


@pytest.mark.parametrize("fault", ["authorization", "workspace"])
def test_unstarted_relay_keeps_original_author_and_admission_error(graph, fault):
    case = graph
    _choose_relay(case)
    case.answer = lambda call: _failed(case, call["key"], PROCESS, artifact=True, call=call)
    original = case.core._run_one_with_gateway
    envelope = {"category": "authorization_required", "scope": "request", "retryable": False,
                "code": "provider_denied", "message": "fixture authorization denied"}
    def unavailable(key, *args, **kwargs):
        if key == RELAY:
            return {"key": RELAY, "harness": RELAY, "status": "error", "final": "",
                    "error": "fixture admission refusal", "blocked_before_native": True,
                    "execution_calls": 0,
                    **({"error_envelope": deepcopy(envelope)} if fault == "authorization"
                       else {"fail_kind": "project_workspace"})}
        return original(key, *args, **kwargs)
    case.monkeypatch.setattr(case.core, "_run_one_with_gateway", unavailable)
    team = "__ut_relay_admission_" + fault
    out = _execute(case, "dispatch", team)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL]
    row = _rows(out)["A"]
    assert row["harness"] == row["assigned_harness"] == ORIGINAL
    assert ORIGINAL + ".txt" in {item["name"] for item in row["artifacts"]}
    admission = row["native_facts"]["unstarted_relay_admission"]
    assert admission["harness"] == RELAY and admission["blocked_before_native"] is True
    assert admission["error"] == "fixture admission refusal"
    if fault == "authorization":
        assert row["error_envelope"] == envelope
    else:
        assert admission["fail_kind"] == "project_workspace"
    assert next(node for node in case.pc.load(team)["tasks"] if node["task_id"] == "A")["assigned_harness"] == ORIGINAL
    saved = case.teams.load(team)["tasks"]["A"]
    assert saved["assigned_harness"] == ORIGINAL
    assert saved["result"]["native_facts"]["unstarted_relay_admission"] == admission


def test_harness_cancel_after_relay_binding_keeps_original_author(graph):
    case = graph
    _choose_relay(case)
    case.answer = lambda call: _failed(case, call["key"], PROCESS)
    original = case.core._run_one_with_gateway
    def cancel_relay(key, *args, **kwargs):
        if key == RELAY:
            case.core.request_cancel(case.active_team, key=RELAY, scope="harness", reason="fixture stop")
        return original(key, *args, **kwargs)
    case.monkeypatch.setattr(case.core, "_run_one_with_gateway", cancel_relay)
    team = "__ut_harness_cancel_before_relay"
    out = _execute(case, "dispatch", team)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL]
    assert _rows(out)["A"]["harness"] == ORIGINAL
    assert _rows(out)["A"]["assigned_harness"] == ORIGINAL
    assert next(row for row in case.pc.load(team)["tasks"] if row["task_id"] == "A")["assigned_harness"] == ORIGINAL
    assert case.teams.load(team)["tasks"]["A"]["assigned_harness"] == ORIGINAL


def test_cancel_during_relay_binding_does_not_reassign_or_start_new_author(graph):
    case = graph
    _choose_relay(case)
    case.answer = lambda call: _failed(case, call["key"], PROCESS)
    original = case.pc.record_task_bindings
    def bind(team_id, tasks, **kwargs):
        if any(node.get("assigned_harness") == RELAY for node in tasks.values()):
            assert case.teams.request_cancel(team_id, reason="fixture cancellation")
            assert case.core.request_cancel(team_id, reason="fixture cancellation")["ok"]
        return original(team_id, tasks, **kwargs)
    case.monkeypatch.setattr(case.pc, "record_task_bindings", bind)
    team = "__ut_relay_cancel_binding"
    _execute(case, "dispatch", team)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL]
    packet = case.pc.load(team)
    node = next(item for item in packet["tasks"] if item["task_id"] == "A")
    assert node["assigned_harness"] == ORIGINAL, "unstarted relay cannot become the persisted author"


def test_later_success_keeps_each_failed_author_and_file_in_the_same_request(graph):
    case = graph
    case.monkeypatch.setattr(
        case.core, "_pick_relay_harness",
        lambda failed, tried, valid, *args: next(
            (key for key in (RELAY, REPAIR) if key not in tried), None))

    def answer(call):
        failed = _failed(case, call["key"], PROCESS, artifact=True, call=call)
        if call["key"] != REPAIR:
            return failed
        completed = _returned(case, "__ut_final_relay")
        completed["final"] = str(case.work / (REPAIR + ".txt"))
        return completed

    case.answer = answer
    team = "__ut_multihop_relay_history"
    out = _execute(case, "persistent", team)
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, RELAY, REPAIR]
    row = _rows(out)["A"]
    assert row["status"] == "done" and row["harness"] == REPAIR
    assert {ORIGINAL, RELAY} <= {
        item.get("harness") for item in row.get("prior_attempts", [])
        if item.get("error_envelope") == PROCESS}
    # This regression concerns files already reported by failed authors.
    # Do not bypass the current author's separate export/binding checks.
    assert {key + ".txt" for key in (ORIGINAL, RELAY)} <= {
        item.get("name") for item in row["artifacts"]}
    assert (case.work / (REPAIR + ".txt")).read_text() == "file from " + REPAIR
    saved = case.teams.load(team)["tasks"]["A"]
    assert [item["assigned_harness"] for item in saved["result_history"]] == [ORIGINAL, RELAY]


@pytest.mark.parametrize("entry", ["dispatch", "persistent"])
def test_request_budget_stops_independent_next_wave_at_original_native_admission(graph, entry):
    case = graph
    _choose_relay(case)

    def answer(call):
        if call["task_id"] == "B":
            return _returned(case, "__ut_independent_prerequisite")
        return _failed(case, call["key"], BUDGET if call["task_id"] == "A" else PROCESS)

    case.answer = answer
    team = "__ut_request_scope_" + entry
    out = _execute(case, entry, team, extra_tasks={
        "B": _node("Independent prerequisite", owner=WRITER),
        "C": _node("Independent next wave", owner=WRITER, depends=("B",)),
    })
    assert "C" not in [call["task_id"] for call in case.native_calls]
    rows = _rows(out)
    assert rows["C"]["status"] in {"blocked", "error"}
    assert out["dependencies"]["C"] == ["B"], "do not invent a dependency on exhausted A"
    if rows["B"]["status"] == "done":
        assert rows["C"]["blocked_before_native"] is True
        assert rows["C"]["execution_calls"] == 0
        assert rows["C"]["error_envelope"] == BUDGET
    else:
        assert rows["B"]["blocked_before_native"] is True
        assert rows["B"]["error_envelope"] == BUDGET
    if entry == "persistent":
        assert len(case.rounds) == 1
        assert out["pilot"]["kind"] == "request_budget_exhausted"


def test_request_budget_fact_arriving_after_native_admission_does_not_cancel_inflight_result(graph):
    case = graph
    observed = []

    def answer(call):
        # Another node's budget fact becomes known while this native execution
        # is already in flight; the runtime must keep its returned result.
        observed.append({"status": "error", "error_envelope": deepcopy(BUDGET)})
        return _returned(case, "__ut_inflight_native")

    case.answer = answer
    out = case.core.run_one(
        RELAY, "Already admitted native work", qhash="__ut_budget_inflight",
        user_id=USER, conversation_id=CONVERSATION,
        _provider_results=lambda: list(observed), execution_depth="quick")
    assert len(case.native_calls) == 1
    assert out["status"] == "done" and not out.get("cancelled")
    errors = importlib.import_module("error_envelope")
    held = errors.request_budget_hold(observed)
    assert held == BUDGET
    held["details"]["used"] = 0
    assert observed[0]["error_envelope"] == BUDGET, "the request fact must not be mutated"
