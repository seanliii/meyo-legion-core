"""A settled repeated output limit must not become a fresh retry on restart."""
import copy
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def state(tmp_path, monkeypatch):
    service = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(service))
    monkeypatch.setenv("LEGION_TEAM_DIR", str(tmp_path / "teams"))
    monkeypatch.setenv("LEGION_HOME_DIR", str(tmp_path / "legion"))
    monkeypatch.setenv("MEYO_LEGION_DIR", str(tmp_path / "data"))
    return importlib.import_module("team_state")


def held_task():
    return {
        "status": "error", "text": "original task", "assigned_harness": "codex",
        "attempt_seq": 1, "dependencies": [],
        "result": {
            "status": "error", "error_envelope": {
                "category": "output_limit", "scope": "request", "retryable": False,
                "code": "max_output_tokens", "message": "repeated limit",
                "details": {"recovery_status": "unchanged_limit_after_recovery"},
            },
            "native_checkpoint": {"kind": "codex_thread", "session_id": "original-thread"},
        },
    }


def test_restart_does_not_spend_another_recovery_budget(state):
    team = {"team_id": "output-held", "user_id": "owner", "tasks": {"a": held_task()},
            "orchestration_spec": {"auto_resume": True}}
    state._write(team)
    path = state._path("output-held")
    before = path.read_bytes()
    assert state.recoverable_tasks("output-held") == []
    assert state.list_auto_resumable() == []
    assert state.claim_resume("output-held", "new-server") is False
    assert path.read_bytes() == before


def test_independent_work_remains_recoverable_but_dependants_do_not_run_without_input(state):
    team = {"team_id": "output-mixed", "user_id": "owner",
            "tasks": {
                "a": held_task(),
                "b": {"status": "pending", "text": "independent", "dependencies": []},
                "c": {"status": "blocked", "text": "depends on a", "dependencies": ["a"],
                      "dependency_policy": "all_succeeded"},
            }, "orchestration_spec": {"auto_resume": True}}
    state._write(team)
    tasks = {row["key"] for row in state.recoverable_tasks("output-mixed")}
    assert tasks == {"b"}
    contracts = importlib.import_module("legion_contracts")
    plan, _ = contracts.resume_plan_from_state(team)
    assert set(plan["tasks"]) == {"a", "b", "c"}
    assert plan["tasks"]["c"]["depends_on"] == ["a"]
    assert contracts.retained_resume_results(team)["a"]["error_envelope"]["retryable"] is False
    assert plan["tasks"]["a"]["assignment_locked"] is True
    assert state.load("output-mixed")["tasks"]["a"]["result"]["native_checkpoint"]["session_id"] == "original-thread"


def test_output_pause_is_not_a_permanent_user_or_provider_ban(state):
    first = held_task()
    after = copy.deepcopy(first)
    after["result"]["error_envelope"]["retryable"] = True
    team = {"team_id": "changed-request", "user_id": "owner", "tasks": {"a": after},
            "orchestration_spec": {"auto_resume": True}}
    state._write(team)
    assert [row["key"] for row in state.recoverable_tasks("changed-request")] == ["a"]
