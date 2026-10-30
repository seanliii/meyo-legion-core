"""Native execution observations survive parsing, persistence and repair input.

No real CLI, model, sandbox probe or remote execution is used by these tests.
"""
from __future__ import annotations

import copy
import importlib
import json
import socket
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
REGISTRY = SERVICE.parents[1] / "harness_baselines" / "meyo-orchestrator"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name, relative in {
        "HOME": "home",
        "CODEX_HOME": "codex-home",
        "MEYO_LEGION_DIR": "meyo",
        "LEGION_HOME_DIR": "legion",
        "LEGION_TEAM_DIR": "teams",
        "LEGION_PROJECT_CONTEXT_DIR": "projects",
        "LEGION_TASK_MEMORY_DIR": "memory",
        "LEGION_ARTIFACT_STORE_DIR": "artifacts",
    }.items():
        monkeypatch.setenv(name, str(tmp_path / relative))
    monkeypatch.setenv("LEGION_REAL_NATIVE_RESUME", "0")
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "bridge")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.syspath_prepend(str(REGISTRY))

    def forbidden(*args, **kwargs):
        raise AssertionError("real network/model/native process is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    # These modules may already have been imported by another test at collection.
    for name, directory in (("task_memory", "memory"), ("artifact_store", "artifacts")):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "ROOT", tmp_path / directory)


@pytest.fixture
def registry():
    return importlib.import_module("harness_registry")


def command_item(status="failed", exit_code=1, item_id="tool-1"):
    return {
        "id": item_id,
        "type": "command_execution",
        "command": "/bin/true",
        "status": status,
        "exit_code": exit_code,
        "aggregated_output": "native execution diagnostic\n" * 40,
    }


@pytest.mark.parametrize("native_status,exit_code", [
    ("failed", None), ("failed", 0), ("declined", None), ("declined", 0),
])
def test_native_failed_or_declined_cannot_be_success(registry, native_status, exit_code):
    event = registry._native_tool_event(
        {"type": "item.completed", "item": command_item(native_status, exit_code)}, "codex")
    assert event["t"] == "tool_end"
    assert event["status"] == "failed" and event["ok"] is False


@pytest.mark.parametrize("item,expected", [
    (command_item("completed", 0), True),
    (command_item("completed", 7), False),
    ({"id": "legacy", "type": "command_execution", "command": "true"}, True),
    ({"id": "mcp", "type": "mcp_tool_call", "status": "failed"}, False),
    ({"id": "mcp", "type": "mcp_tool_call", "status": "completed",
      "result": {"content": []}}, True),
])
def test_native_status_keeps_existing_nonfailure_semantics(registry, item, expected):
    event = registry._native_tool_event({"type": "item.completed", "item": item}, "codex")
    assert event["ok"] is expected


def invoke_adapter(registry, monkeypatch, tmp_path, events, *,
                   final="本轮说明", returncode=0, timeout=False, raw_tail="", ctx_values=None):
    calls = []
    monkeypatch.setattr(registry._sh, "which", lambda _: "/fixture/codex")
    monkeypatch.setattr(registry, "_codex_bridge_ctx", lambda *a, **k: nullcontext(True))
    monkeypatch.setattr(registry, "_legion_prompt", lambda question, ctx: question)
    stdout = "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + raw_tail

    def run(command, *args, **kwargs):
        calls.append(command)
        Path(command[command.index("-o") + 1]).write_text(final, encoding="utf-8")
        if timeout:
            error = subprocess.TimeoutExpired(command, 1)
            error.legion_stdout = stdout
            error.legion_stderr = ""
            raise error
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(registry, "_run_live", run)
    values = {"model": "fixture-model", "appid": "fixture-appid",
              "gateway": "http://fixture.invalid", "workdir": str(tmp_path / "work"),
              **(ctx_values or {})}
    result = registry._codex_adapter(
        "__ut_codex_facts", "本次任务", ctx=registry.HarnessCtx(**values))
    assert len(calls) == 1
    assert calls[0][calls[0].index("-s") + 1] == "workspace-write"
    return result


def test_adapter_keeps_tool_failure_separate_from_completed_turn(registry, monkeypatch, tmp_path):
    failed, recovered = command_item(), command_item("completed", 0, "tool-2")
    turn = {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}
    result = invoke_adapter(registry, monkeypatch, tmp_path, [
        {"type": "thread.started", "thread_id": "native-thread"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": failed},
        {"type": "item.completed", "item": recovered},
        turn,
    ])
    facts = result["native_facts"]
    assert facts["tool_results"] == [failed, recovered]
    assert facts["pending_tool_results"] == []
    assert facts["turn_status"] == "completed" and facts["turn_result"] == turn
    assert facts["process_returncode"] == 0
    assert result["status"] == "ok" and result["final"] == "本轮说明"
    assert result["error"] == "" and not result.get("error_envelope")
    # A single failed tool does not become a failed native turn or task.
    assert not facts.get("failed") and not facts.get("error")
    assert result["usage"]["total_tokens"] == 5
    assert result["native_checkpoint"]["session_id"] == "native-thread"


def test_native_diagnostic_does_not_invent_verified_runtime_failure(
        registry, monkeypatch, tmp_path):
    failed = command_item("failed", None)
    failed["aggregated_output"] = (
        "bwrap: Creating new namespace failed: nesting depth or "
        "/proc/sys/user/max_*_namespaces exceeded (ENOSPC)")
    result = invoke_adapter(registry, monkeypatch, tmp_path, [
        {"type": "item.completed", "item": failed},
        {"type": "turn.completed", "usage": {}},
    ], final="执行端阻断说明")
    assert result["native_facts"]["tool_results"] == [failed]
    assert result["native_facts"]["environment_verification"] == "unverified"
    assert result["status"] == "ok" and result["final"] == "执行端阻断说明"
    assert not result.get("error_envelope")


def test_adapter_preserves_native_turn_error_and_process_exit(registry, monkeypatch, tmp_path):
    turn = {"type": "turn.failed", "error": {"message": "native turn failed"}}
    result = invoke_adapter(registry, monkeypatch, tmp_path, [
        {"type": "error", "message": "native diagnostic"},
        turn,
    ], returncode=1)
    assert result["native_facts"]["turn_status"] == "failed"
    assert result["native_facts"]["turn_result"] == turn
    assert result["native_facts"]["process_returncode"] == 1
    assert result["native_facts"]["errors"] == [
        {"type": "error", "message": "native diagnostic"}]
    assert result["status"] == "rc1"


def test_short_answer_without_tools_stays_valid(registry, monkeypatch, tmp_path):
    result = invoke_adapter(registry, monkeypatch, tmp_path, [
        "non-object JSON", [], {"unrelated": True},
        {"type": "turn.completed", "usage": {}},
    ], final="好")
    assert result["status"] == "ok" and result["final"] == "好"
    assert result["native_facts"]["tool_results"] == []
    assert result["native_facts"]["pending_tool_results"] == []
    assert result["native_facts"]["turn_status"] == "completed"
    assert not result.get("error_envelope")


def started_command(item_id="item_21", command="/bin/zsh -lc 'python3.12 server.py'"):
    return {"id": item_id, "type": "command_execution", "command": command,
            "status": "in_progress", "exit_code": None, "aggregated_output": ""}


@pytest.mark.parametrize("last_event", ["item.started", "item.updated"])
def test_pending_reports_missing_terminal_event_without_changing_final(
        registry, monkeypatch, tmp_path, last_event):
    item = started_command()
    events = [{"type": "thread.started", "thread_id": "native-pending"},
              {"type": "item.started", "item": item}]
    if last_event == "item.updated":
        events.append({"type": last_event, "item": {**item, "aggregated_output": "not copied"}})
    events.append({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}})
    result = invoke_adapter(registry, monkeypatch, tmp_path, events, final="好")
    facts = result["native_facts"]
    assert facts["pending_tool_results"] == [{
        "id": "item_21", "type": "command_execution", "command": item["command"],
        "last_event": last_event, "terminal_event": "not_received",
    }]
    assert facts["tool_results"] == [] and facts["turn_status"] == "completed"
    assert facts["environment_verification"] == "unverified"
    assert result["status"] == "ok" and result["final"] == "好" and result["error"] == ""
    assert not result.get("error_envelope") and not facts.get("failed")
    assert result["usage"]["total_tokens"] == 5
    assert result["native_checkpoint"]["session_id"] == "native-pending"


@pytest.mark.parametrize("late", [False, True])
def test_pending_closes_by_id_even_after_turn_completed(
        registry, monkeypatch, tmp_path, late):
    item = started_command()
    completed = {**item, "status": "completed", "exit_code": 0}
    turn = {"type": "turn.completed", "usage": {}}
    end = {"type": "item.completed", "item": completed}
    events = [{"type": "item.started", "item": item}, *([turn, end] if late else [end, turn]),
              # An out-of-order old update cannot resurrect a received terminal.
              {"type": "item.updated", "item": item}]
    result = invoke_adapter(registry, monkeypatch, tmp_path, events, final="好")
    assert result["native_facts"]["pending_tool_results"] == []
    assert result["native_facts"]["tool_results"] == [completed]
    assert result["status"] == "ok" and result["final"] == "好"


def test_pending_does_not_merge_same_command_or_other_item_types(registry, monkeypatch, tmp_path):
    first, second = started_command("item_1"), started_command("item_2")
    completed = {**first, "status": "completed", "exit_code": 0}
    result = invoke_adapter(registry, monkeypatch, tmp_path, [
        {"type": "item.started", "item": first},
        {"type": "item.updated", "item": second},  # A captured update needs no invented start.
        {"type": "item.started", "item": {"type": "command_execution", "command": "no-id"}},
        {"type": "item.started", "item": {"id": "private", "type": "reasoning", "text": "PRIVATE"}},
        {"type": "item.completed", "item": completed},
        {"type": "turn.completed", "usage": {}},
    ])
    pending = result["native_facts"]["pending_tool_results"]
    assert [row["id"] for row in pending] == ["item_2"]
    assert pending[0]["last_event"] == "item.updated"
    assert result["native_facts"]["tool_results"] == [completed]
    assert "PRIVATE" not in json.dumps(result["native_facts"])


def test_pending_is_local_to_each_parse_not_inherited_from_memory(registry, monkeypatch, tmp_path):
    first = invoke_adapter(registry, monkeypatch, tmp_path, [
        {"type": "item.started", "item": started_command()}])
    second = invoke_adapter(registry, monkeypatch, tmp_path, [
        {"type": "turn.completed", "usage": {}}],
        final="好", ctx_values={"task_memory": {"native_facts": first["native_facts"]}})
    assert first["native_facts"]["pending_tool_results"][0]["id"] == "item_21"
    assert second["native_facts"]["pending_tool_results"] == []
    third = invoke_adapter(registry, monkeypatch, tmp_path, [
        {"type": "item.started", "item": started_command(command="new invocation")}],
        ctx_values={"task_memory": {"native_facts": {
            "tool_results": [{**started_command(), "status": "completed", "exit_code": 0}]}}})
    assert third["native_facts"]["pending_tool_results"][0]["command"] == "new invocation"


def test_pending_count_and_fields_are_bounded_without_copying_output(registry, monkeypatch, tmp_path):
    events = [{"type": "item.started", "item": {
        **started_command(f"item_{index}", '命令"\\\n' * 600),
        "aggregated_output": "HIDDEN_STREAM_BODY" * 1000,
        "arguments": {"private": "HIDDEN_ARGUMENT"}, "result": "HIDDEN_RESULT",
    }} for index in range(60)]
    events.append({"type": "item.updated", "item": {
        **started_command("very-long-id-" * 100, "last command"),
        "server": "s" * 1000, "tool": "t" * 1000,
    }})
    result = invoke_adapter(registry, monkeypatch, tmp_path, events)
    facts = result["native_facts"]
    pending = facts["pending_tool_results"]
    assert 0 < len(pending) <= 32
    assert len(json.dumps(pending, ensure_ascii=False).encode()) <= 8192
    assert facts["pending_tool_results_omitted"] == len(events) - len(pending)
    assert pending[-1]["command"] == "last command"
    assert {"id", "server", "tool"} <= set(pending[-1]["truncated_fields"])
    for row in pending:
        for field, limit in (("id", 128), ("command", 512), ("server", 128), ("tool", 128)):
            assert len(row.get(field, "").encode()) <= limit
        assert not {"status", "exit_code", "aggregated_output", "arguments", "result"} & row.keys()
    assert "HIDDEN_" not in json.dumps(facts)


def test_pending_survives_truncated_completion_and_timeout(registry, monkeypatch, tmp_path):
    result = invoke_adapter(registry, monkeypatch, tmp_path, [
        {"type": "thread.started", "thread_id": "native-truncated"},
        {"type": "item.started", "item": started_command()},
    ], timeout=True, raw_tail='\n{"type":"item.completed","item":{"id":"item_21"')
    assert result["status"] == "timeout"
    assert result["native_facts"]["process_returncode"] is None
    assert result["native_facts"]["pending_tool_results"][0]["terminal_event"] == "not_received"
    assert result["native_checkpoint"]["session_id"] == "native-truncated"


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("transport", ["responses", "bridge", "auto"])
def test_pending_uses_existing_provider_secret_redaction(
        registry, monkeypatch, tmp_path, timeout, transport):
    import model_registry
    secret = "fixture-private-provider-token"
    monkeypatch.setattr(model_registry, "responses_endpoint",
                        lambda *a, **k: {"base_url": "https://fixture.invalid/v1", "http_headers": {}})
    monkeypatch.setattr(model_registry, "explicit_responses_binding",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("no optional binding")))
    monkeypatch.setattr(model_registry, "_resolve_alias", lambda *a, **k: secret)
    monkeypatch.setattr(registry, "_codex_bound_model_catalog",
                        lambda *a, **k: {"models": [{"input_modalities": ["text"]}]})
    result = invoke_adapter(registry, monkeypatch, tmp_path, [
        {"type": "item.started", "item": started_command(command="echo " + secret)},
        {"type": "turn.completed", "usage": {}},
    ], timeout=timeout, ctx_values={
        "model_protocol": transport,
        "appid": "appid-fixture-alias" if transport == "auto" else secret,
    })
    assert result["native_facts"]["pending_tool_results"][0]["command"] == "echo [REDACTED]"
    assert secret not in json.dumps(result)
    assert result["status"] == ("timeout" if timeout else "ok")
    projected = registry._context_binding_view(
        {"team_id": "__ut_redaction", "native_facts": result["native_facts"]},
        "MEYO_PROJECT_CONTEXT_JSON")
    assert secret not in json.dumps(projected)
    assert projected["native_facts"]["pending_tool_results"][0]["command"] == "echo [REDACTED]"


def test_tool_payloads_and_warnings_are_kept_without_agent_or_reasoning_text(
        registry, monkeypatch, tmp_path):
    tools = [
        {"id": "patch", "type": "file_change", "status": "failed",
         "changes": [{"path": "output.txt", "kind": "add"}]},
        {"id": "mcp", "type": "mcp_tool_call", "status": "completed",
         "result": {"content": [{"type": "text", "text": "tool output"}]}},
        {"id": "search", "type": "web_search", "query": "fixture"},
        {"id": "peer", "type": "collab_tool_call", "status": "completed",
         "agents_states": {"peer": {"status": "completed", "message": "peer result"}}},
    ]
    warning = {"type": "item.completed",
               "item": {"id": "warning", "type": "error", "message": "native warning"}}
    result = invoke_adapter(registry, monkeypatch, tmp_path, [
        *({"type": "item.completed", "item": item} for item in tools),
        warning,
        {"type": "item.completed",
         "item": {"type": "reasoning", "text": "private reasoning sentinel"}},
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "agent answer sentinel"}},
    ])
    facts = result["native_facts"]
    assert facts["tool_results"] == tools and facts["errors"] == [warning]
    assert facts["turn_status"] == "unreported" and "turn_result" not in facts
    assert "sentinel" not in json.dumps(facts)
    assert result["status"] == "ok" and not result.get("error_envelope")


def test_timeout_retains_reported_facts_without_guessing_returncode(registry, monkeypatch, tmp_path):
    failed = command_item()
    result = invoke_adapter(registry, monkeypatch, tmp_path, [
        {"type": "thread.started", "thread_id": "native-before-timeout"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": failed},
    ], timeout=True)
    assert result["status"] == "timeout"
    assert result["native_facts"]["process_returncode"] is None
    assert result["native_facts"]["turn_status"] == "started"
    assert result["native_facts"]["tool_results"] == [failed]
    assert result["native_checkpoint"]["session_id"] == "native-before-timeout"


@pytest.mark.parametrize("status", ["done", "error", "timeout", "partial"])
def test_native_facts_survive_both_team_writes_and_project_snapshot(status):
    import project_context
    import task_memory
    import team_state

    team = "__ut_native_facts"
    facts = {"process_returncode": 0, "turn_status": "completed",
             "tool_results": [command_item()], "environment_verification": "unverified",
             "pending_tool_results": [{
                 "id": "item_21", "type": "command_execution",
                 "last_event": "item.started", "terminal_event": "not_received",
                 "command": "/bin/zsh -lc 'python3.12 server.py'",
             }]}
    task_memory.update(team, "__ut_writer", native_facts=facts)
    assert task_memory.context(team, "__ut_writer")["native_facts"] == facts
    envelope = {"category": "harness_runtime", "retryable": False, "scope": "harness",
                "details": {"evidence_ref": "fixture-reported-evidence"}}
    host_facts = {
        "checkpoint_writes": {"request": True, "conversation": False},
        "checkpoint_storage_error": "fixture storage diagnostic",
        "native_resume_error": {"category": "fixture_resume_diagnostic"},
    }
    result = {"key": "__ut_writer", "status": status, "final": "原始说明",
              "native_facts": facts, "error_envelope": envelope, **host_facts}
    team_state.ensure(team, "原目标", {"produce": "任务"}, {"produce": []})
    project_context.initialize(team, "原目标", {
        "tasks": {"produce": {"task": "任务", "assigned_harness": "__ut_writer"}},
    }, mode="orchestrate")
    attempt = team_state.begin_attempt(team, "produce")
    assert team_state.finish_attempt(team, "produce", attempt, result)
    stored = team_state.load(team)["tasks"]["produce"]["result"]
    assert stored["native_facts"] == facts
    assert stored["error_envelope"] == envelope and stored["status"] == status
    for field, value in host_facts.items():
        assert stored.get(field) == value, "host recovery facts must not disappear at finish_attempt"
        assert field not in stored["native_facts"], "host observations are not native events"
    project_context.record_result(team, "produce", stored)
    for depth in ("standard", "quick"):
        assert project_context.snapshot(team, execution_depth=depth)["attempts"][-1]["native_facts"] == facts

    replacement = {**result, "native_facts": {
        **facts, "tool_results": [], "pending_tool_results": []}}
    assert team_state.replace_result(team, "produce", replacement, previous_harness="__ut_writer")
    task = team_state.load(team)["tasks"]["produce"]
    assert task["result"]["native_facts"] == replacement["native_facts"]
    assert task["result_history"][-1]["result"]["native_facts"] == facts
    for field, value in host_facts.items():
        assert task["result"].get(field) == value
        assert task["result_history"][-1]["result"].get(field) == value
    project_context.record_result(team, "produce", task["result"])
    assert project_context.snapshot(team)["attempts"][-1]["native_facts"] == replacement["native_facts"]


def test_contract_repair_receives_upstream_runtime_facts(monkeypatch, tmp_path):
    import legion_core as core

    first = {"key": "__ut_writer", "status": "done", "final": "原始说明",
             "elapsed": .01, "artifacts": [], "workdir": str(tmp_path / "work"),
             "native_checkpoint": {"kind": "codex_thread", "session_id": "fixture-native"},
             "native_facts": {"turn_status": "completed", "process_returncode": 0,
                              "tool_results": [command_item()],
                              "environment_verification": "unverified"},
             "error_envelope": {"category": "harness_runtime", "retryable": False,
                                "scope": "harness", "details": {"evidence_ref": "fixture"}}}
    observed = []
    monkeypatch.setattr(core, "_harness_keys", lambda: {"__ut_writer", "__ut_repair"})
    monkeypatch.setattr(core, "_aigc_ready", lambda: {
        "runnable_clis": ["__ut_writer", "__ut_repair"]})
    monkeypatch.setattr(core, "_autonomy_matrix", lambda: {})
    monkeypatch.setattr(core, "_cost_class", lambda *a, **k: "light")
    monkeypatch.setattr(core, "_record_experience", lambda *a, **k: None)
    monkeypatch.setattr(core, "_build_prelude", lambda *a, **k: "")
    monkeypatch.setattr(core, "_pick_relay_harness", lambda *a, **k: "__ut_repair")

    def execute(key, *args, **kwargs):
        if key == "__ut_writer":
            return copy.deepcopy(first)
        observed.append(copy.deepcopy(kwargs["task_facts"]))
        return {"key": key, "status": "error", "final": "执行端尚未验证", "elapsed": .01}

    monkeypatch.setattr(core, "_run_one_with_gateway", execute)
    monkeypatch.setattr(core, "run_one", execute)
    result = core.dispatch("原目标", {
        "_user_selected_mode": True,
        "tasks": {"produce": {"task": "产出文件", "assigned_harness": "__ut_writer",
                             "assignment_locked": True}},
        "delivery_contract": {"required_artifacts": [{"kind": "file", "formats": ["txt"]}],
                              "require_final": True},
        "team_policy": {"adaptive": False, "max_failover": 0, "contract_repair_rounds": 1},
        "success_audit": {"enabled": False},
    }, qhash="__ut_contract_native_facts", user_id="audit")
    assert len(observed) == 1
    upstream = observed[0]["upstream_results"][0]
    for field in ("status", "error_envelope", "native_facts", "native_checkpoint", "workdir"):
        assert upstream[field] == first[field]
    assert result["outcome"]["status"] != "completed"
