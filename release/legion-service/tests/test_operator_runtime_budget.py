"""可信预算回调接线：真实 core，原生/CLI 叶子离线；不读取评测控制器。

回调为空涵盖尚未耗尽和临时占额；文件鉴权、nonce 与 OS 隔离由调用者负责。
"""
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_project_export_terminal_binding import scene, USER, CONVERSATION
from test_native_terminal_dispatch_graph import graph, ORIGINAL, RELAY, WRITER, _node, _plan, _rows, _returned
from test_request_budget_dispatch import BUDGET, PROCESS, _failed, _choose_relay


OWNER = {
    "status": "error", "source": "operator_runtime_budget",
    "error_envelope": {**BUDGET, "details": {
        "budget_owner": "operator", "request_id": "__ut_original_owner",
        "dimension": "model_calls", "limit": 1, "used": 1,
    }},
}


@pytest.fixture
def budget_case(graph):
    case, mp = graph, graph.monkeypatch
    case.owner_facts, case.owner_reads = [], 0
    case.model_selections, case.planner_calls = [], []

    def observe():
        case.owner_reads += 1
        assert case.owner_reads < 1000, "collector must not recurse into itself"
        return list(case.owner_facts)

    case.observe_budget = observe
    case.publish_budget = lambda: case.owner_facts.append(deepcopy(OWNER))
    mp.setattr(case.core, "_selection_resource_context",
               lambda *a, **kw: {"members": [{"key": RELAY}], "resource_index": {}})
    mp.setattr(case.core, "_gateway_env", lambda: {})

    def forbidden(*args, **kwargs):
        raise AssertionError("operator budget tests must not open any network endpoint")

    for method in ("connect", "connect_ex", "bind", "listen"):
        mp.setattr(socket.socket, method, forbidden)
    return case


def execute(case, entry, *, failover=1, tasks=None, **kwargs):
    team = "__ut_operator_budget_" + entry
    case.active_team = team
    plan = _plan(case, tasks or {"A": _node("Continue original work", owner=ORIGINAL)},
                 failover=failover)
    plan.update(kwargs.pop("plan_fields", {}))
    common = dict(qhash=team, mode="pk", user_id=USER, conversation_id=CONVERSATION,
                  cb=case.events.append, _provider_results=case.observe_budget, **kwargs)
    if entry == "persistent":
        return case.core.dispatch_persistent("Original request", plan, max_rounds=3, **common)
    return case.core.dispatch("Original request", plan, **common)


def assert_pause(result):
    assert result["error_envelope"] == OWNER["error_envelope"]
    assert result["status"] in {"error", "blocked", "timeout"}
    assert not result.get("cancelled")
    assert result["native_facts"]["runtime_budget_observation"] == OWNER


@pytest.mark.parametrize("entry", ["dispatch", "persistent"])
def test_external_budget_is_admitted_without_recursion_or_native_work(budget_case, entry):
    case = budget_case
    case.publish_budget()
    out = execute(case, entry)
    assert not case.native_calls and not case.picks
    row = _rows(out)["A"]
    assert_pause(row)
    assert row["execution_calls"] == 0 and row["blocked_before_native"] is True
    saved = case.teams.load(case.active_team)["tasks"]["A"]["result"]
    assert_pause(saved)
    assert 0 < case.owner_reads < 100
    if entry == "persistent":
        assert out["pilot"]["kind"] == "request_budget_exhausted"
        assert len(case.rounds) == 1


@pytest.mark.parametrize("entry", ["dispatch", "persistent"])
def test_budget_arriving_with_native_failure_preserves_error_and_files(budget_case, entry):
    case = budget_case
    _choose_relay(case)

    def answer(call):
        if call["task_id"] == "P":
            return _returned(case, "__ut_independent_prerequisite")
        failed = _failed(case, call["key"], PROCESS, artifact=True, call=call)
        case.publish_budget()
        return failed

    case.answer = answer
    out = execute(case, entry, tasks={
        "A": _node("Original work", owner=ORIGINAL),
        "P": _node("Independent prerequisite", owner=WRITER),
        "B": _node("Independent next wave", owner=WRITER, depends=("P",)),
    })
    # A/P 同波可以同时准入，不能把正常在途调用当成预算漏洞；B 必须在后续波被挡住。
    assert [call["task_id"] for call in case.native_calls].count("A") == 1
    assert "B" not in [call["task_id"] for call in case.native_calls]
    assert not case.picks
    row = _rows(out)["A"]
    assert_pause(row)
    native = row["native_facts"]
    assert native["process_returncode"] == 1
    assert native["execution_failure"]["error_envelope"] == PROCESS
    assert native["execution_failure"]["error"] == PROCESS["message"]
    assert row["execution_calls"] == 1 and not row.get("blocked_before_native")
    assert (case.work / (ORIGINAL + ".txt")).read_text() == "file from " + ORIGINAL
    assert ORIGINAL + ".txt" in {item["name"] for item in row["artifacts"]}
    saved = case.teams.load(case.active_team)["tasks"]["A"]["result"]
    assert saved["native_facts"] == native
    call = next(call for call in case.native_calls if call["task_id"] == "A")
    memory = case.tm.load(call["memory_id"], ORIGINAL)
    assert memory["error_envelope"] == OWNER["error_envelope"]
    assert memory["native_facts"] == native
    rows = _rows(out)
    assert out["dependencies"]["B"] == ["P"]
    if rows["P"]["status"] == "done":
        assert_pause(rows["B"])
    else:
        assert_pause(rows["P"])
        assert rows["B"]["status"] == "blocked"
    if entry == "persistent":
        assert len(case.rounds) == 1 and out["pilot"]["kind"] == "request_budget_exhausted"


def test_single_run_one_observes_new_budget_after_failure(budget_case):
    case = budget_case

    def answer(call):
        case.publish_budget()
        return _failed(case, call["key"], PROCESS)

    case.answer = answer
    out = case.core.run_one(
        ORIGINAL, "Original request", qhash="__ut_single_owner",
        user_id=USER, conversation_id=CONVERSATION, execution_depth="quick",
        _provider_results=case.observe_budget)
    assert len(case.native_calls) == 1
    assert_pause(out)
    assert out["native_facts"]["execution_failure"]["error_envelope"] == PROCESS
    stored = case.tm.load(case.native_calls[0]["memory_id"], ORIGINAL)
    assert stored["error_envelope"] == out["error_envelope"]
    case.owner_facts[0]["error_envelope"]["details"]["used"] = 0
    assert out["error_envelope"]["details"]["used"] == 1


def test_owner_binding_metadata_is_preserved_not_reinterpreted_by_core(budget_case):
    case = budget_case
    observation = deepcopy(OWNER)
    # 只验证可信回调的附加事实透明传递，不冒充 operator reader 的 nonce/权限验证。
    observation["error_envelope"]["details"]["binding"] = {
        "job": "__ut_job", "native_id": "__ut_native",
        "nonce": "fixture-only-nonce", "revision": 7,
    }
    expected = deepcopy(observation)

    def answer(call):
        case.owner_facts.append(observation)
        return _failed(case, call["key"], PROCESS)

    case.answer = answer
    out = case.core.run_one(
        ORIGINAL, "Original request", qhash="__ut_opaque_owner_binding",
        user_id=USER, conversation_id=CONVERSATION, execution_depth="quick",
        _provider_results=case.observe_budget)
    assert out["error_envelope"] == expected["error_envelope"]
    assert out["native_facts"]["runtime_budget_observation"] == expected
    assert out["native_facts"]["execution_failure"]["error_envelope"] == PROCESS
    observation["error_envelope"]["details"]["binding"]["revision"] = 8
    assert out["native_facts"]["runtime_budget_observation"] == expected
    saved = case.tm.load(case.native_calls[0]["memory_id"], ORIGINAL)
    assert saved["native_facts"]["runtime_budget_observation"] == expected


@pytest.mark.parametrize("allowed", [True, False], ids=["authorized", "outside-project"])
def test_budget_pause_preserves_a_reported_file_from_before_this_attempt(budget_case, allowed):
    case = budget_case
    path = (case.work if allowed else case.root) / "existing-result.txt"
    path.write_text("Original file, not created by this attempt.")
    os.utime(path, (1, 1))  # 不能靠“本轮新文件扫描”误打误撞找回来。

    def answer(call):
        case.publish_budget()
        return {**_failed(case, call["key"], PROCESS),
                "artifacts": [{"name": path.name, "path": str(path), "kind": "file"}]}

    case.answer = answer
    row = _rows(execute(case, "dispatch"))["A"]
    assert_pause(row)
    assert row["native_facts"]["execution_failure"]["error_envelope"] == PROCESS
    if allowed:
        assert path.name in {item["name"] for item in row["artifacts"]}
    else:
        assert not row["artifacts"] and row["artifact_safety_observations"]
    assert path.read_text() == "Original file, not created by this attempt."
    saved = case.teams.load(case.active_team)["tasks"]["A"]["result"]
    assert saved["artifacts"] == row["artifacts"]


@pytest.mark.parametrize("completed", [False, True], ids=["failed", "successful"])
def test_budget_arriving_during_export_preserves_the_same_attempt(budget_case, completed):
    case = budget_case
    path = case.work / "index.html"
    case.answer = lambda call: {
        **_returned(case, "__ut_export_final", absent=not completed),
        "artifacts": [{"name": path.name, "path": str(path), "kind": "web"}],
    }
    export = case.ad.export_project_artifacts

    def publish_after_export(*args, **kwargs):
        exported = export(*args, **kwargs)
        case.publish_budget()
        return exported

    case.monkeypatch.setattr(case.ad, "export_project_artifacts", publish_after_export)
    row = _rows(execute(case, "dispatch", tasks={"A": _node("Original work", owner=RELAY)}))["A"]
    assert len(case.native_calls) == 1
    assert path.name in {item["name"] for item in row["artifacts"]}
    assert row["execution_calls"] == 1 and not row.get("blocked_before_native")
    assert case.owner_facts, "the owner fact must actually arrive at the export boundary"
    if completed:
        assert row["status"] == "done" and not row.get("error_envelope")
        assert not row["native_facts"].get("execution_failure")
    else:
        assert_pause(row)
        assert row["native_facts"]["process_returncode"] == 0
        assert not row["native_facts"].get("execution_failure")
        assert row["native_facts"]["execution_observation"]
        saved = case.tm.load(case.native_calls[0]["memory_id"], RELAY)
        assert saved["error_envelope"] == OWNER["error_envelope"]


def test_post_native_binding_refusal_keeps_budget_without_reviving_rejected_output(budget_case):
    case = budget_case

    def answer(call):
        case.publish_budget()
        failed = _failed(case, call["key"], PROCESS)
        failed["native_checkpoint"]["provider_binding"] = "different-provider-fixture"
        failed["final"] = "Output from a rejected binding must not be restored."
        failed["artifacts"] = [{"path": str(case.work / "index.html"), "name": "index.html"}]
        return failed

    case.answer = answer
    out = case.core.run_one(
        RELAY, "Original request", qhash="__ut_native_binding_owner",
        user_id=USER, conversation_id=CONVERSATION, execution_depth="quick",
        _provider_results=case.observe_budget)
    assert len(case.native_calls) == 1
    assert_pause(out)
    assert out["execution_calls"] == 1
    assert out["native_facts"]["execution_failure"]["error_envelope"]["category"] == "native_session_unavailable"
    assert not out.get("native_checkpoint") and not out.get("artifacts") and not out["final"]
    saved = case.tm.load(case.native_calls[0]["memory_id"], RELAY)
    assert saved["error_envelope"] == OWNER["error_envelope"]


def test_budget_during_retry_selection_preserves_the_prior_native_failure(budget_case):
    case = budget_case
    case.monkeypatch.setattr(case.core, "_dynamic_max_attempts", lambda _pool: 3)
    case.answer = lambda call: _failed(case, call["key"], PROCESS)

    def select(*args, **kwargs):
        case.model_selections.append("selection")
        if len(case.model_selections) == 2:
            case.publish_budget()
        return {"model": "fixture-" + str(len(case.model_selections)), "appid": "fixture-key"}

    case.monkeypatch.setattr(case.core, "select_usable_model", select)
    out = case.core.run_one(
        ORIGINAL, "Retry original request", qhash="__ut_budget_during_retry",
        user_id=USER, conversation_id=CONVERSATION, execution_depth="quick",
        _provider_results=case.observe_budget)
    assert len(case.native_calls) == 1 and len(case.model_selections) == 2
    assert_pause(out)
    assert out["execution_calls"] == 1 and not out.get("blocked_before_native")
    assert out["native_facts"]["execution_failure"]["error_envelope"] == PROCESS
    assert out["native_facts"]["process_returncode"] == 1


def test_failed_relay_keeps_original_history_and_new_owner_fact(budget_case):
    case = budget_case
    _choose_relay(case)

    def answer(call):
        result = _failed(case, call["key"], PROCESS, artifact=True, call=call)
        if call["key"] == RELAY:
            case.publish_budget()
        return result

    case.answer = answer
    out = execute(case, "persistent")
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, RELAY]
    row = _rows(out)["A"]
    assert_pause(row)
    assert row["harness"] == RELAY and not row.get("via_relay")
    assert any(item.get("harness") == ORIGINAL and item.get("error_envelope") == PROCESS
               for item in row["prior_attempts"])
    assert {ORIGINAL + ".txt", RELAY + ".txt"} <= {item["name"] for item in row["artifacts"]}
    assert out["pilot"]["kind"] == "request_budget_exhausted"


@pytest.mark.parametrize("entry", ["dispatch", "persistent"])
def test_empty_callback_keeps_temporary_reservation_and_normal_relay_open(budget_case, entry):
    case = budget_case
    _choose_relay(case)
    case.answer = lambda call: (_failed(case, call["key"], PROCESS)
                               if call["key"] == ORIGINAL else _returned(case, "__ut_relay"))
    # 普通计划中的自报预算不是授权来源；临时占额的可信回调按接口返回 []。
    out = execute(case, entry, plan_fields={
        "runtime_budget": deepcopy(OWNER),
        "provider_results": [deepcopy(OWNER)],
    })
    assert [call["key"] for call in case.native_calls] == [ORIGINAL, RELAY]
    assert _rows(out)["A"]["status"] == "done"
    assert not _rows(out)["A"].get("error_envelope")


@pytest.mark.parametrize("entry", ["single", "persistent"])
@pytest.mark.parametrize("native_status", ["ok", "done", "completed", "success"])
def test_budget_does_not_rewrite_an_inflight_success(budget_case, entry, native_status):
    case = budget_case

    def answer(call):
        case.publish_budget()
        return {**_returned(case, "__ut_success"), "status": native_status}

    case.answer = answer
    if entry == "single":
        row = case.core.run_one(
            RELAY, "Already admitted", qhash="__ut_success_owner",
            user_id=USER, conversation_id=CONVERSATION, execution_depth="quick",
            _provider_results=case.observe_budget)
    else:
        row = _rows(execute(case, entry))["A"]
    assert row["status"] == "done" and not row.get("cancelled")
    assert not row.get("error_envelope")
    assert not row["native_facts"].get("execution_failure")
    assert len(case.native_calls) == 1


def test_same_owner_callback_survives_the_next_persistent_round(budget_case):
    case = budget_case
    case.answer = lambda call: _failed(case, call["key"], PROCESS)
    case.monkeypatch.setattr(
        importlib.import_module("legion_pilot"), "evaluate",
        lambda *a, **kw: {"action": "continue", "kind": "fixture_retry", "reason": "Try original work"})

    def observe():
        return [deepcopy(OWNER)] if len(case.rounds) >= 2 else []

    case.observe_budget = observe
    out = execute(case, "persistent", failover=0)
    assert len(case.rounds) == 2
    assert len(case.native_calls) == 1
    assert_pause(_rows(out)["A"])
    assert out["pilot"]["kind"] == "request_budget_exhausted"


@pytest.mark.parametrize("when", ["before", "failure"])
def test_election_reports_budget_without_starting_another_candidate(budget_case, when):
    case = budget_case
    case.monkeypatch.setattr(case.core, "_captain_candidates", lambda *a, **kw: [ORIGINAL, RELAY])
    if when == "before":
        case.publish_budget()

    def answer(call):
        case.publish_budget()
        return _failed(case, call["key"], PROCESS)

    case.answer = answer
    out = case.core.elect_captain(
        "Original request", {"captain_candidates": [ORIGINAL, RELAY]},
        qhash="__ut_election_owner", user_id=USER, conversation_id=CONVERSATION,
        _provider_results=case.observe_budget)
    assert_pause(out)
    assert not out["ok"]
    assert len(case.native_calls) == (1 if when == "failure" else 0)
    assert any(row.get("source") == "operator_runtime_budget" for row in out["provider_results"])
    if when == "failure":
        assert out["native_facts"]["execution_failure"]["error_envelope"] == PROCESS


def test_explicit_user_cancellation_does_not_become_a_budget_failure(budget_case):
    case = budget_case
    case.publish_budget()
    out = case.core.run_one(
        ORIGINAL, "Original request", qhash="__ut_cancel_and_budget",
        user_id=USER, conversation_id=CONVERSATION, execution_depth="quick",
        cancel_check=lambda: True, _provider_results=case.observe_budget)
    assert out["status"] == "cancelled" and not case.native_calls
    assert (out.get("error_envelope") or {}).get("category") != "runtime_budget"


def configure_planner(case, transport, outcome, publish_at):
    mp = case.monkeypatch
    mp.setattr(case.core, "_personal_responses_planner_enabled", lambda: transport == "native")
    here = case.root / "planner-package/release/legion-service"
    here.mkdir(parents=True)
    script = case.root / "planner-package/scripts/export-model-tool.py"
    script.parent.mkdir()
    script.write_text("# Exists only for helper discovery; never executed.\n")
    mp.setattr(case.core, "HERE", str(here))
    text = json.dumps({"tasks": {"A": {"task": "Original work", "assigned_harness": RELAY}},
                       "suggested_mode": "pk"})

    def select(*args, **kwargs):
        case.model_selections.append("selected")
        if publish_at == "selection":
            case.publish_budget()
        return {"model": "fixture-model", "appid": "fixture-key"}

    def finish():
        if publish_at == "result":
            case.publish_budget()
        if outcome == "exception":
            raise RuntimeError("fixture native execution failed")

    def cli(*args, **kwargs):
        case.planner_calls.append("cli")
        finish()
        return SimpleNamespace(
            returncode=1 if outcome == "rc1" else 0, stderr="", cancelled=False,
            stdout=text if outcome == "success" else "not a plan")

    def native(call):
        case.planner_calls.append("native")
        finish()
        if outcome == "rc1":
            return _failed(case, call["key"], PROCESS)
        return {**_returned(case, "__ut_planning"), "final": text if outcome == "success" else "not a plan"}

    mp.setattr(case.core, "select_usable_model", select)
    mp.setattr(importlib.import_module("planner_runtime"), "run_cli", cli)
    case.answer = native


def plan(case):
    return case.core.plan_request_with_model(
        "Original complete request", candidate_harnesses=[RELAY],
        user_id=USER, conversation_id=CONVERSATION,
        _provider_results=case.observe_budget)


@pytest.mark.parametrize("transport", ["native", "cli"])
def test_planner_preexisting_budget_stops_before_selection_and_execution(budget_case, transport):
    case = budget_case
    configure_planner(case, transport, "success", "never")
    case.publish_budget()
    out = plan(case)
    assert not case.planner_calls and not case.model_selections
    assert not out["ok"]
    assert_pause(out)
    assert out["execution_calls"] == 0 and out["blocked_before_native"] is True
    assert out["planner_attempts"] == []


def test_planner_budget_arriving_after_selection_stops_before_cli(budget_case):
    case = budget_case
    configure_planner(case, "cli", "success", "selection")
    out = plan(case)
    assert case.model_selections == ["selected"] and not case.planner_calls
    assert_pause(out)
    assert not out["ok"] and out["execution_calls"] == 0
    assert out["planner_attempts"] == []


def test_planner_setup_failure_keeps_budget_without_claiming_native_execution(budget_case):
    case = budget_case
    configure_planner(case, "cli", "success", "never")
    (case.root / "planner-package/scripts/export-model-tool.py").unlink()

    def resources(*args, **kwargs):
        case.publish_budget()
        return {"members": [{"key": RELAY}], "resource_index": {}}

    case.monkeypatch.setattr(case.core, "_selection_resource_context", resources)
    out = plan(case)
    assert not case.planner_calls and not case.model_selections
    assert_pause(out)
    assert out["execution_calls"] == 0 and out["blocked_before_native"] is True
    assert "planner_tool_unavailable" in out["error"]
    assert not out["native_facts"].get("execution_failure")


@pytest.mark.parametrize("transport", ["native", "cli"])
@pytest.mark.parametrize("outcome", ["rc1", "no_json", "exception"])
def test_planning_failure_carries_new_budget_without_selecting_next_model(budget_case, transport, outcome):
    case = budget_case
    configure_planner(case, transport, outcome, "result")
    out = plan(case)
    assert case.planner_calls == [transport]
    assert len(case.model_selections) == (1 if transport == "cli" else 0)
    assert not out["ok"]
    assert_pause(out)
    assert out["native_facts"]["execution_failure"]["error"]
    if transport == "native" and outcome == "exception":
        # runner 抛异常而未交回计数，不能把调用入口被触达当成已确认执行一次。
        assert out["execution_calls"] is None
    else:
        assert out["execution_calls"] == 1
    assert not out.get("blocked_before_native")


@pytest.mark.parametrize("transport", ["native", "cli"])
def test_successful_planning_inflight_is_not_rejected_by_later_budget(budget_case, transport):
    case = budget_case
    configure_planner(case, transport, "success", "result")
    out = plan(case)
    assert out["ok"] and "A" in out["tasks"]
    assert case.planner_calls == [transport]
    assert not out.get("error_envelope")
