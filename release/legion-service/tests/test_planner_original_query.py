"""The planner must receive the user's actual task, not its first few lines."""
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def planner(tmp_path, monkeypatch):
    for name, leaf in (("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
                       ("LEGION_HOME_DIR", "legion")):
        monkeypatch.setenv(name, str(tmp_path / leaf))
    monkeypatch.syspath_prepend(str(SERVICE))
    core = importlib.import_module("legion_core")
    runtime = importlib.import_module("planner_runtime")
    profiles = importlib.import_module("harness_profiles")
    here = tmp_path / "repo/release/legion-service"
    here.mkdir(parents=True)
    script = tmp_path / "repo/scripts/export-model-tool.py"
    script.parent.mkdir()
    script.write_text("# Discovery fixture only: never executed.\n")
    monkeypatch.setattr(core, "HERE", str(here))
    monkeypatch.setattr(core, "_harness_keys", lambda: {"__ut_raw_query"})
    monkeypatch.setattr(core, "_autonomy_matrix", lambda: {})
    monkeypatch.setattr(core, "_aigc_ready", lambda: {"runnable_clis": ["__ut_raw_query"]})
    monkeypatch.setattr(profiles, "get_profiles", lambda: [{"key": "__ut_raw_query"}])
    monkeypatch.setattr(core, "select_usable_model",
                        lambda *a, **kw: {"model": "fixture-model", "appid": "fixture-key"})
    monkeypatch.setattr(core, "_gateway_env", lambda: {})
    captured = []

    def run(command, *, input, env, cancel_check):
        captured.append(input)
        return SimpleNamespace(
            returncode=0, stderr="", cancelled=False,
            stdout=json.dumps({"tasks": {"__ut_raw_query": "member-generated task"},
                               "suggested_mode": "pk"}))

    monkeypatch.setattr(runtime, "run_cli", run)
    return SimpleNamespace(core=core, captured=captured)


def test_long_original_query_is_not_silently_sliced_before_planning(planner):
    query = "原始需求第一部分。\n" + "仍然重要的真实用户语境。" * 500 + "\n末尾关键需求：请保留完整使用目标。"
    result = planner.core.plan_request_with_model(query, candidate_harnesses=["__ut_raw_query"])
    assert result["ok"]
    context = json.loads(planner.captured[0].split("现场:", 1)[1])
    assert context["goal"] == query
    assert context["quality_context"]["original_query"] == query


def test_owner_preferences_and_raw_input_provenance_reach_planner(planner):
    preferences = {"owner_facing_language": "zh-CN", "source": "explicit user preference"}
    snapshot = {"original_query": "逐字原话", "derived_summary": "另一份说明，不能替代原话",
                "source_id": "fixture-user-turn"}
    planner.core.plan_request_with_model(
        "当前完整请求", request_context={"user_profile": preferences, "input_snapshot": snapshot},
        candidate_harnesses=["__ut_raw_query"])
    context = json.loads(planner.captured[0].split("现场:", 1)[1])
    assert context["user_profile"] == preferences
    assert context["input_snapshot"] == snapshot


def test_short_question_is_unchanged_and_does_not_gain_invented_preferences(planner):
    planner.core.plan_request_with_model("你好", candidate_harnesses=["__ut_raw_query"])
    context = json.loads(planner.captured[0].split("现场:", 1)[1])
    assert context["goal"] == "你好"
    assert context.get("user_profile") in (None, {}, "")
