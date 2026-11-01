"""Selection context only: no new routing, model call, credential or tool grants."""
import importlib
import json
from pathlib import Path

import pytest

from test_public_origin_entry import isolated


@pytest.fixture
def selection_case(isolated):
    mp = isolated.monkeypatch
    core = importlib.import_module("legion_core")
    view = importlib.import_module("harness_view")
    registry = importlib.import_module("runtime_manifest")
    models = importlib.import_module("model_registry")
    capabilities = importlib.import_module("capability_catalog")
    host = importlib.import_module("native_capability_host")
    definitions = {
        "first": {"profile": {"name": "第一家", "strengths": ["原生代码与恢复"]},
                  "specs": {"tools": True, "resume": "native", "artifacts": True},
                  "execution": {"depths": ["standard", "deep"]}},
        "later": {"profile": {"name": "后来加入", "strengths": ["资料研究"]},
                  "specs": {"tools": True, "checkpoint": True}},
        "device": {"execution_role": "capability", "profile": {"name": "设备服务"}},
    }
    ready = {"clis": {"first": True, "later": False}, "runnable_clis": ["first"],
             "warming": False, "stale": False}
    mp.setattr(core, "_harness_keys", lambda: {"first", "later"})
    mp.setattr(core, "_aigc_ready", lambda: ready)
    mp.setattr(registry, "load_definitions", lambda: definitions)
    mp.setattr(models, "resolve_assignments", lambda plan, keys: {
        k: {"model": "fixture-model", "appid": "DO-NOT-PROJECT-SECRET" if k == "first" else None,
            "source": "request_override"} for k in keys})
    mp.setattr(models, "explicit_responses_status", lambda key: None)
    mp.setattr(capabilities, "inventory", lambda: {"discovery": [
        {"id": "actual_index", "name": "现有能力目录", "invoke": "legion_tool.py list",
         "ready": True, "api_key": "DO-NOT-PROJECT-SECRET"}]})
    mp.setattr(host, "declared_capability_actions", lambda: {"actions": [
        {"id": "external_capability:device:read", "name": "read"},
        {"id": "external_capability:device:write", "name": "write"}]})
    mp.setenv("LEGION_PERSONAL_OWNER", "invited-test")
    mp.setenv("MEYO_NATIVE_CAPABILITY_IDS", '["external_capability:device:read"]')
    return core, view, definitions, ready


def context(case, **kwargs):
    _, view, _, _ = case
    function = getattr(view, "selection_context", None)
    assert callable(function), "native catalog lacks the bounded selection-resource projection"
    return function(user_id="invited-test", **kwargs)


def test_all_candidates_native_features_and_real_indexes_are_visible(selection_case):
    value = context(selection_case)
    assert value["candidate_keys"] == ["first", "later"]
    assert value["selection"] == "automatic"
    rows = {r["key"]: r for r in value["members"]}
    assert rows["first"]["native"]["resume"] == "native"
    assert rows["first"]["runtime_available"] is True
    assert rows["later"]["runtime_available"] is False
    assert rows["first"]["model"]["configured"] is True
    assert rows["later"]["model"]["configured"] is False
    assert rows["first"]["model"]["provider_verified"] is False
    assert value["resource_index"]["discovery"][0]["id"] == "actual_index"
    assert "DO-NOT-PROJECT-SECRET" not in json.dumps(value)
    assert "device" not in value["candidate_keys"]


@pytest.mark.parametrize("flag", ["warming", "stale", "public_catalog_only"])
def test_cache_placeholder_is_not_current_execution_readiness(selection_case, flag):
    selection_case[3][flag] = True
    value = context(selection_case)
    assert value["cache"][flag] is True
    assert all(r["runtime_available"] is None for r in value["members"])


def test_explicit_candidate_order_and_explicit_empty_do_not_expand(selection_case):
    selected = context(selection_case, plan={"team_policy": {"candidate_harnesses": ["later", "first"]}})
    assert selected["candidate_keys"] == ["later", "first"]
    assert selected["selection"] == "user_candidates"
    empty = context(selection_case, plan={"team_policy": {"candidate_harnesses": []}})
    assert empty["candidate_keys"] == []


def test_grants_are_separate_from_declarations_and_not_shared_between_users(selection_case):
    value = context(selection_case)
    actions = {r["id"]: r for r in value["resource_index"]["native_capabilities"]}
    assert actions["external_capability:device:read"]["operator_granted"] is True
    assert actions["external_capability:device:write"]["operator_granted"] is False
    other = selection_case[1].selection_context(user_id="other-user")
    assert all(r["operator_granted"] is None for r in other["resource_index"]["native_capabilities"])


def test_missing_observation_does_not_mean_uninstalled(selection_case):
    selection_case[3]["clis"].pop("later")
    rows = {r["key"]: r for r in context(selection_case)["members"]}
    assert rows["later"]["runtime_available"] is None
    assert "installed" not in rows["later"] or rows["later"]["installed"] is None


def test_large_descriptions_cannot_hide_late_candidate_identity(selection_case):
    core, _, definitions, _ = selection_case
    definitions["first"]["profile"]["strengths"] = ["x" * 200000]
    for i in range(140):
        definitions[f"member-{i:03}"] = definitions["later"]
    core._harness_keys = lambda: set(definitions) - {"device"}
    value = context(selection_case)
    assert "member-139" in value["candidate_keys"]
    assert len(json.dumps(value, ensure_ascii=False).encode()) <= 65536
    assert value["member_count"] == 142


def test_runtime_brief_does_not_invent_keys_and_carries_same_resource_index(selection_case):
    core = selection_case[0]
    brief = core._build_runtime_brief("原问题", {}, user_id="invited-test")
    assert "key 已就绪" not in str(brief)
    assert "actual_index" in str(brief)
    assert "external_capability:device:read" in str(brief)
    assert "DO-NOT-PROJECT-SECRET" not in str(brief)


def test_native_declaration_text_is_bounded_and_redacted(selection_case):
    selection_case[2]["first"]["specs"]["resume"] = "Bearer sk-abcdefghijklmnopqrstuvwxyz123456"
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(context(selection_case))


def test_native_tool_prelude_does_not_claim_all_user_credentials_exist(selection_case, isolated):
    isolated.monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[3] / "harness_baselines/meyo-orchestrator"))
    registry = importlib.import_module("harness_registry")
    assert "ready with the user's keys" not in registry._LEGION_TOOL_ACCESS
    assert "ON\nDEMAND" in registry._LEGION_TOOL_ACCESS
