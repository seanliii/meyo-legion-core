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
def test_real_default_http_dispatch_starts_author_without_losing_subject_or_project(http_entry, isolated, project_approved):
    server = http_entry.server
    monkeypatch = isolated.monkeypatch
    env, _ = isolated.runtime.environment(isolated.root, {})
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    calls, planner_calls = [], []
    monkeypatch.setattr(server.LEGION, "_harness_keys", lambda: {"codex", "cc"})
    monkeypatch.setattr(server.LEGION, "_aigc_ready", lambda **kw: {"runnable_clis": ["codex", "cc"]})
    monkeypatch.setattr(importlib.import_module("collaboration_runtime"), "_performance_order",
                        lambda keys, scene: ["cc", "codex"])
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
    assert planner_calls == [], "the actual author must start before any solution/formation planner"
    question, request = calls[0]
    assert question == "继续原工程"
    assert request["mode"] == "single"
    assert request["user_id"] == "invited-test"
    assert request["conversation_id"] == "personaldefault123"
    assert request["plan"]["tasks"]["delivery"]["assigned_harness"] == "cc"
    assert request["plan"]["tasks"]["delivery"]["task"] == question
    assert request["plan"]["initial_author"]["task_id"] == "delivery"
    assert not request["plan"].get("captain"), "initial author selection is not a permanent captain"
    assert request["plan"]["quality_context"]
    assert request["plan"]["project_context"]["workspace_root"] == str(workspace)
    assert request["plan"]["project_context"]["continued_from_team_id"] == "personal-default-prior"
    events = [
        json.loads(line[6:]) for line in raw.decode().splitlines()
        if line.startswith("data: ")
    ]
    selection = next(event for event in events if event.get("t") == "mode_selected")
    assert selection["mode"] == "single"
    assert selection.get("selection_source") != "personal_runtime_owner_binding"
    assert any(event.get("results") for event in events)


def test_initial_author_binding_keeps_whole_candidate_pool_and_does_not_infer_solution(isolated):
    runtime = importlib.import_module("collaboration_runtime")
    isolated.monkeypatch.setattr(runtime, "_performance_order", lambda keys, scene: ["cc", "codex"])
    plan = {
        "team_policy": {"candidate_harnesses": ["codex", "cc"], "max_agents": 2},
        "project_context": {"workspace_root": "/fixture/original"},
        "model_overrides": {"cc": "fixture-model"}, "resource_budget": {"remaining_calls": 12},
        "attachments": ["/fixture/material.json"],
    }
    before = copy.deepcopy(plan)
    bound = runtime.bind_initial_author_request(
        "完整用户原话", plan, valid={"cc", "codex"}, runnable={"cc", "codex"}, definitions={})
    assert plan == before
    assert bound["bound"] and bound["owner"] == "cc"
    output = bound["plan"]
    assert output["team_policy"] == before["team_policy"]
    assert len(output["tasks"]) == 1
    assert output["tasks"]["delivery"]["task"] == "完整用户原话"
    assert output["tasks"]["delivery"]["assigned_harness"] == "cc"
    assert not output.get("captain")
    assert not output.get("pipeline")
    assert output["initial_author"]["advantage_verification"] == "unproven"
    for field in ("project_context", "model_overrides", "resource_budget", "attachments"):
        assert output[field] == before[field]


@pytest.mark.parametrize("plan", [
    {"tasks": {"known": "explicit work"}}, {"team_policy": {"force_multi": True}},
    {"team_policy": {"min_agents": 2}}, {"solo": False},
    {"captain": "cc"}, {"captain_candidates": ["cc"]},
    {"pipeline": ["cc", "codex"]}, {"verification": {"enabled": True}},
])
def test_initial_author_does_not_replace_explicit_organization(isolated, plan):
    runtime = importlib.import_module("collaboration_runtime")
    before = copy.deepcopy(plan)
    bound = runtime.bind_initial_author_request(
        "完整用户原话", plan, valid={"cc", "codex"}, runnable={"cc", "codex"}, definitions={})
    assert not bound["bound"] and plan == before


def test_empty_candidate_boundary_never_expands_to_an_initial_author(isolated):
    runtime = importlib.import_module("collaboration_runtime")
    bound = runtime.bind_initial_author_request(
        "完整用户原话", {"team_policy": {"candidate_harnesses": []}},
        valid={"cc", "codex"}, runnable={"cc", "codex"}, definitions={})
    assert not bound["bound"] and bound["error"] == "no_initial_author_available"
