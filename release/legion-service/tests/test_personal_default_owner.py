"""Personal auto stays automatic; explicit members and ownership remain native."""
from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path

import pytest

from test_public_origin_entry import http_entry, isolated


def test_personal_startup_keeps_auto_without_inheriting_an_implicit_owner(isolated):
    env, _ = isolated.runtime.environment(
        isolated.root, {"MEYO_PERSONAL_DEFAULT_HARNESS": "unrelated-owner"}
    )
    assert env["MEYO_PERSONAL_DEFAULT_HARNESS"] == ""
    assert "LEGION_CODEX_RESPONSES_BINDING" not in env
    # An execution identity/model binding is not a choice of the task's worker.
    # Native planning still requires its own valid model binding.


def test_default_owner_preserves_original_task_and_context(isolated):
    binding = importlib.import_module("collaboration_runtime")
    plan = {
        "project_context": {"workspace": "/fixture/original-project", "revision": 2},
        "attachments": ["/fixture/input.png"],
        "quality_context": {"sources": [{"url": "https://example.invalid/original"}]},
        "team_policy": {"max_attempts": 1},
        "execution_depth": "deep",
    }
    before = copy.deepcopy(plan)
    result = binding.bind_explicit_owner_request(
        "继续修改原成果，不要重建工程", plan, default_owner="codex"
    )
    assert result["bound"] and result["owner"] == "codex"
    assert result["source"] == "personal_runtime_owner_binding"
    assert result["plan"]["tasks"]["codex"]["task"] == "继续修改原成果，不要重建工程"
    assert plan == before
    for name in before:
        if name != "team_policy":
            assert result["plan"][name] == before[name]
    assert result["plan"]["team_policy"]["max_attempts"] == 1


@pytest.mark.parametrize("plan, mode", [
    ({"team_policy": {"candidate_harnesses": ["codex", "cc"]}}, "auto"),
    ({"team_policy": {"candidate_harnesses": []}}, "auto"),
    ({"team_policy": {"force_multi": True}}, "auto"),
    ({"team_policy": {"min_agents": 2}}, "auto"),
    ({"solo": False}, "auto"),
    ({"tasks": {"cc": "explicit task"}}, "auto"),
    ({"pipeline": ["cc", "codex"]}, "auto"),
    ({"organization": {"nodes": ["cc", "codex"]}}, "auto"),
    ({"captain_candidates": ["cc"]}, "auto"),
    ({}, "orchestrate"),
    ({}, "relay"),
    ({}, "pk"),
])
def test_default_cannot_override_explicit_execution(isolated, plan, mode):
    binding = importlib.import_module("collaboration_runtime")
    before = copy.deepcopy(plan)
    result = binding.bind_explicit_owner_request(
        "原始用户需求", plan, mode=mode, default_owner="codex"
    )
    assert result["bound"] is False
    assert plan == before


def test_explicit_owner_still_wins_and_absent_default_keeps_legacy_route(isolated):
    binding = importlib.import_module("collaboration_runtime")
    selected = binding.bind_explicit_owner_request(
        "原始需求", {"team_policy": {"candidate_harnesses": ["cc"]}},
        default_owner="codex",
    )
    assert selected["bound"] and selected["owner"] == "cc"
    assert selected["source"] == "explicit_owner_binding"
    assert binding.bind_explicit_owner_request("原始需求", {}) == {"bound": False}


@pytest.mark.parametrize("project_approved", [False, True])
def test_real_default_http_dispatch_plans_without_losing_subject_or_project(http_entry, isolated, project_approved):
    server = http_entry.server
    monkeypatch = isolated.monkeypatch
    env, _ = isolated.runtime.environment(isolated.root, {})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    calls, planner_calls = [], []
    monkeypatch.setattr(server.LEGION, "_harness_keys", lambda: ["codex", "cc"])
    monkeypatch.setattr(server.LEGION, "pilot_settings", lambda _uid: {"enabled": False})
    monkeypatch.setattr(
        importlib.import_module("harness_performance"), "route_policy",
        lambda _scene: {"verified": False, "route": "strongest_single"},
    )

    def planner(*args, **kwargs):
        planner_calls.append((args, kwargs))
        return {"ok": True, "suggested_mode": "pk", "reason": "offline native planner",
                "tasks": {"chosen": {"task": "继续原工程", "assigned_harness": "cc"}},
                "directions": {"chosen": "programming"}}

    def dispatch(question, **kwargs):
        calls.append((question, kwargs))
        return {
            "results": [{"key": "codex", "harness": "codex", "status": "done",
                         "final": "isolated native boundary", "elapsed": 0.01}],
            "solo": True,
        }

    monkeypatch.setattr(server.LEGION, "plan_request_with_model", planner)
    monkeypatch.setattr(server.LEGION, "dispatch", dispatch)
    monkeypatch.setattr(server.ARTIFACTS, "enrich", lambda *_a, **_k: None)
    monkeypatch.setattr(server.DESKTOP_FS, "materialize_session", lambda *_a: {})
    monkeypatch.setattr(server.DESKTOP_FS, "sync_personal_data", lambda *_a: None)
    workspace = isolated.root / "projects/original"
    workspace.mkdir(parents=True)
    native_tmp = isolated.root / "native-tmp"
    native_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(native_tmp))
    if project_approved:
        token_path = Path(os.environ["LEGION_PERSONAL_TOKENS_FILE"])
        records = json.loads(token_path.read_text())
        for record in records.values():
            if record.get("user") == "invited-test":
                record["project_grants"] = [{"root": str(workspace), "export_paths": ["."]}]
        token_path.write_text(json.dumps(records))
    importlib.import_module("project_context").initialize(
        "personal-default-prior", "原始工程",
        {"project_context": {"workspace_root": str(workspace)}},
        mode="single", user_id="invited-test", conversation_id="personaldefault123",
    )
    server.SESSIONS.save_turn(
        "personaldefault123", "原始工程",
        {"status": "done", "team_id": "personal-default-prior", "results": []},
        mode="single", user_id="invited-test",
    )
    status, _, raw = http_entry.request(
        "POST", "/api/legion/dispatch",
        body={"question": "继续原工程", "plan": {}, "mode": "auto",
              "session_id": "personaldefault123", "uid": "invited-test"},
    )
    if not project_approved:
        assert status == 403
        assert not calls and not planner_calls
        return
    assert status == 200
    assert len(calls) == 1, raw.decode("utf-8", "replace")[-2200:]
    assert len(planner_calls) == 1
    question, request = calls[0]
    assert question == "继续原工程"
    assert request["mode"] == "pk"
    assert request["user_id"] == "invited-test"
    assert request["conversation_id"] == "personaldefault123"
    assert request["plan"]["tasks"]["chosen"]["assigned_harness"] == "cc"
    assert request["plan"]["tasks"]["chosen"]["task"] == question
    assert request["plan"]["quality_context"]
    assert request["plan"]["project_context"]["workspace_root"] == str(workspace)
    assert request["plan"]["project_context"]["continued_from_team_id"] == "personal-default-prior"
    events = [
        json.loads(line[6:]) for line in raw.decode().splitlines()
        if line.startswith("data: ")
    ]
    selection = next(event for event in events if event.get("t") == "mode_selected")
    assert selection["mode"] == "pk"
    assert selection.get("selection_source") != "personal_runtime_owner_binding"
    assert any(event.get("results") for event in events)
