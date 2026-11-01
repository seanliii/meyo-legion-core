"""Successful adapter receipts identify content using only observed native facts.

Exercises the actual DSH/Penguin/JCode CLI adapters with offline process results,
then the shared run_one path. No model, native binary, fleet probe or gold answer.
"""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
import subprocess
import tempfile

import pytest

from test_cli_runtime_channels import cli_stack, invoke, KEY


@pytest.fixture
def direct_stack(cli_stack, monkeypatch, tmp_path):
    case = cli_stack
    case.answer = "好"
    case.stderr = ""
    case.packets = []
    case.native_calls = []
    registry = case.registry
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(registry, "_legion_prompt", lambda task, ctx: task)
    monkeypatch.setattr(registry._sh, "which",
                        lambda name: f"/offline/{name}" if name in {"dsh", "penguin"} else None)
    monkeypatch.setattr(registry, "_penguin_trace_offsets", lambda *a: (tmp_path / "trace", {}))
    jcode = importlib.import_module("external_adapters.jcode_native")
    monkeypatch.setattr(jcode, "executable", lambda: "/offline/jcode")

    def no_process(*args, **kwargs):
        raise AssertionError("this adapter regression must remain offline")

    monkeypatch.setattr(subprocess, "Popen", no_process)

    def native_result(command, *args, **kwargs):
        tag = kwargs.get("tag")
        case.native_calls.append(tag)
        stdout = case.answer
        if tag == "jcode":
            home = Path(kwargs["env"]["JCODE_HOME"])
            (home / "sessions").mkdir(parents=True, exist_ok=True)
            sid = "offline-received-session"
            (home / "sessions" / f"{sid}.json").write_text(json.dumps({"id": sid}))
            stdout = json.dumps({
                "type": "done", "session_id": sid, "model": "offline-model",
                "provider": "offline-provider", "text": case.answer,
            })
        elif tag == "penguin":
            # The native CLI console interleaves channels. Exercise its real
            # trace protocol instead of making stdout-only fixtures authoritative.
            trace = tmp_path / "trace/2026-11-01/session-offline_001.jsonl"
            trace.parent.mkdir(parents=True, exist_ok=True)
            rows = [
                {"type": "session_meta", "payload": {
                    "session_id": "session-offline", "workspace": str(case.project)}},
                {"type": "model_msg", "payload": {
                    "role": "user", "type": "text", "text": command[command.index("-m") + 1]}},
                {"type": "event_msg", "payload": {"type": "request_begin"}},
                {"type": "model_msg", "payload": {
                    "role": "assistant", "type": "text", "text": case.answer,
                    "stop_reason": "completed"}},
                {"type": "event_msg", "payload": {"type": "request_end", "status": "completed"}},
            ]
            trace.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
            stdout = "SYNTHETIC_CONSOLE_PROGRESS\n" + case.answer
        return subprocess.CompletedProcess(command, 0, stdout, case.stderr)

    monkeypatch.setattr(registry, "_run_task", native_result)
    return case


def select_adapter(case, monkeypatch, name):
    function = {
        "deepseek": case.registry._deepseek_adapter,
        "jcode": case.registry._jcode_adapter,
        "penguin": case.registry._penguin_adapter,
    }[name]

    def actual_adapter(*args, **kwargs):
        packet = function(*args, **kwargs)
        case.packets.append(copy.deepcopy(packet))
        return packet

    monkeypatch.setitem(case.registry._STANDALONE_ADAPTERS, KEY, actual_adapter)


@pytest.mark.parametrize("name", ["deepseek", "jcode", "penguin"])
@pytest.mark.parametrize("answer", [
    "好", "empty", "connection", "Error: this is the requested literal text",
    "API call failed: HTTP 429", '{"error":"empty","status":"error"}',
])
def test_actual_direct_adapter_success_is_content_with_only_observed_native_facts(
        direct_stack, monkeypatch, name, answer):
    case = direct_stack
    case.answer = answer
    select_adapter(case, monkeypatch, name)
    result = invoke(case)
    assert case.native_calls == [name]
    packet = case.packets[0]
    assert packet["status"] == "ok", "the real adapter, not core, must report this success"
    assert packet["final"] == answer
    facts = packet.get("native_facts") or {}
    if name == "penguin":
        assert facts["process_returncode"] == 0
        assert facts["final_source"] == "penguin_trace_assistant_text"
        assert facts["completed"] is True
    else:
        assert facts.get("process_returncode") is None
        assert facts.get("final_source") is None
    assert result["status"] == "done", result
    assert result["final"] == answer and result["error"] == ""
    if name == "penguin":
        assert result["native_facts"]["process_returncode"] == 0
        assert result["native_facts"]["completed"] is True
    else:
        assert result["native_facts"].get("process_returncode") is None
        assert result["native_facts"].get("completed") is not True
    assert result["native_facts"].get("turn_result") is None
    assert "goal_completed" not in result["native_facts"]


def test_direct_success_stderr_remains_an_observation_without_fabricated_completion(
        direct_stack, monkeypatch):
    case = direct_stack
    case.answer = "empty"
    case.stderr = "optional integration was unavailable"
    select_adapter(case, monkeypatch, "deepseek")
    result = invoke(case)
    assert case.packets[0]["status"] == "ok"
    assert result["status"] == "done", result
    assert result["native_facts"]["stderr"] == case.stderr
    assert result["native_facts"].get("completed") is not True


def receipt(case, monkeypatch, packet):
    before = copy.deepcopy(packet)
    monkeypatch.setitem(case.registry._STANDALONE_ADAPTERS, KEY,
                        lambda *a, **kw: copy.deepcopy(packet))
    result = invoke(case)
    assert packet == before
    return result


@pytest.mark.parametrize("status", ["ok", "done", "completed", "success"])
def test_success_receipt_is_a_channel_fact_not_a_quality_verdict(cli_stack, monkeypatch, status):
    result = receipt(cli_stack, monkeypatch, {
        "status": status, "final": "Error: literal connection example", "error": "",
    })
    assert result["status"] == "done", result
    assert result["final"] == "Error: literal connection example"
    assert result["native_facts"] == {}
    assert not result.get("artifacts"), "no completed work or artifact should be invented"


@pytest.mark.parametrize("override", [
    {"status": "error", "error": "E_NATIVE_FAILURE"},
    {"status": "rc1", "error": "E_PROCESS_FAILURE"},
    {"status": "incomplete"},
    {"status": "timeout"},
    {"status": "cancelled"},
    {"error": "E_EXPLICIT_ADAPTER_ERROR"},
    {"error_envelope": {"category": "gateway", "retryable": True, "scope": "provider",
                        "code": "E_NATIVE_PROVIDER", "message": "native provider failure"}},
    {"error_envelope": {"category": "authorization_required", "retryable": False,
                        "scope": "user", "message": "authorization required"}},
    {"native_facts": {"failed": True}},
    {"native_facts": {"completed": False}},
    {"native_facts": {"turn_status": "failed", "completed": True}},
    {"native_facts": {"turn_status": "incomplete"}},
    {"native_facts": {"process_returncode": 1}},
    {"native_facts": {"interrupted": True}},
])
def test_runtime_failure_or_incomplete_facts_win_over_a_success_receipt(cli_stack, monkeypatch, override):
    result = receipt(cli_stack, monkeypatch, {
        "status": "ok", "final": "retained public output", "error": "", **override,
    })
    assert result["status"] != "done", result
    for key, value in override.get("native_facts", {}).items():
        assert result["native_facts"][key] == value


def test_success_with_an_empty_final_does_not_gain_delivery(cli_stack, monkeypatch):
    result = receipt(cli_stack, monkeypatch, {"status": "ok", "final": "", "error": ""})
    assert result["status"] != "done"
    assert result["final"] == ""


def test_json_answer_fields_cannot_override_the_adapter_runtime_error(cli_stack, monkeypatch):
    result = receipt(cli_stack, monkeypatch, {
        "status": "error", "error": "E_RUNTIME_FAILURE",
        "final": '{"status":"ok","error":"","completed":true}',
    })
    assert result["status"] != "done"
    assert "E_RUNTIME_FAILURE" in result["error"]


@pytest.mark.parametrize("status", ["incomplete", "partial", "unavailable", "blocked", "unknown"])
def test_non_success_receipt_keeps_a_draft_without_claiming_done(
        cli_stack, monkeypatch, record_property, status):
    draft = "A saved draft exists"
    result = receipt(cli_stack, monkeypatch, {"status": status, "final": draft, "error": ""})
    record_property("input_status", status)
    record_property("returned_status", result["status"])
    record_property("draft_preserved", str(result["final"] == draft))
    record_property("error_envelope_code", str((result.get("error_envelope") or {}).get("code") or ""))
    assert result["final"] == draft, "runtime non-success must not discard the saved draft"
    assert result["status"] != "done", "nonempty text is not an adapter success receipt"
    assert result["status"] == status, "partial/blocked/unknown must remain independent runtime facts"
    assert result["execution_calls"] == 1, "a native state alone is not a model failure to retry"
    assert result["error"] == "", "do not invent a runtime error from a retained draft"


def test_partial_keeps_native_coordination_and_reported_files(cli_stack, monkeypatch, tmp_path):
    draft = tmp_path / "draft.txt"
    draft.write_text("draft")
    request = {"coordinate": {"action": "continue", "reason": "Native work is not finished"}}
    artifact = {"name": "draft.txt", "path": str(draft), "kind": "text", "bytes": 5}
    result = receipt(cli_stack, monkeypatch, {
        "status": "partial", "final": "A saved draft exists", "error": "",
        "model_used": "fixture-model", "orchestration_request": request,
        "artifacts": [artifact],
    })
    assert result["status"] == "partial"
    assert result["orchestration_request"] == request
    assert result["model_used"] == "fixture-model"
    assert any(item.get("path") == str(draft) for item in result.get("reported_artifacts", []))
    assert not result.get("artifacts"), "this unbound fixture does not grant a project export"
