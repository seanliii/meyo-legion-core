"""Current local declarations must survive cold and cached discovery, without probes."""
import copy
import importlib.util
import json
from pathlib import Path
import time

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture
def context(tmp_path, monkeypatch):
    for name, folder in (("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
                         ("LEGION_HOME_DIR", "legion"), ("LEGION_PROC_REGISTRY_DIR", "registry")):
        monkeypatch.setenv(name, str(tmp_path / folder))
    monkeypatch.syspath_prepend(str(SERVICE))
    spec = importlib.util.spec_from_file_location("visibility_toolbox", SERVICE / "toolbox.py")
    toolbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(toolbox)
    import external_agent_registry as registry
    actions = [{
        "id": "external_capability:fixture:work", "key": "fixture", "action": "work",
        "description": "precision-operation native collaboration", "capabilities": ["precision-operation"],
        "params": ["request_file"],
    }]
    class LocalCatalog:
        broken = False

        def read_text(self, **kwargs):
            if self.broken:
                return "{"
            entries = {}
            for action in actions:
                entry = entries.setdefault(action["key"], {
                    "key": action["key"], "capabilities": action["capabilities"], "capability_actions": {},
                })
                entry["capability_actions"][action["action"]] = {
                    "description": action["description"], "params": action["params"],
                    "command": ["python3.12", "--version"],
                }
            return json.dumps({"agents": list(entries.values())})

    catalog = LocalCatalog()
    monkeypatch.setattr(registry, "CATALOG", catalog)
    monkeypatch.setattr(registry, "probe", lambda *a, **kw: pytest.fail("discovery must not probe a backend"))
    monkeypatch.setattr(toolbox, "skill_source_identity", lambda: {"fixture": "source"})
    monkeypatch.setattr(toolbox, "_schedule_refresh", lambda: None)
    monkeypatch.setattr(toolbox, "_read_cached_toolbox", lambda: None)
    return toolbox, actions


def test_cold_catalog_contains_current_declarations_without_claiming_backend_ready(context):
    toolbox, _ = context
    snapshot = toolbox.scan_toolbox()
    row = next((r for r in snapshot["items"] if r["id"] == "external_capability:fixture:work"), None)
    assert row is not None, "cold discovery dropped an already declared capability"
    assert snapshot["partial"] and snapshot["warming"]
    assert row["runnable"] is True
    assert row["installed"] is None
    assert row["health"] == "unknown"
    assert snapshot["summary"]["total"] == len(snapshot["items"])


def test_cached_catalog_tracks_add_update_remove_without_mutating_the_snapshot(context, monkeypatch):
    toolbox, actions = context
    core = {"id": "core", "name": "core", "kind": "runnable_tool",
            "runnable": True, "shared": True, "capability_tags": ["core"]}
    cached = {"items": [core], "summary": {
        "total": 1, "runnable_tools": 1, "shared": 1, "by_capability": {"core": 1}},
        "scanned_at": time.time(), "skill_sources": {"fixture": "source"}}
    before = copy.deepcopy(cached)
    monkeypatch.setattr(toolbox, "_read_cached_toolbox", lambda: cached)
    first = toolbox.scan_toolbox()
    assert any(r["id"] == actions[0]["id"] for r in first["items"])
    assert first["scanned_at"] == before["scanned_at"]
    actions[0]["description"] = "updated current declaration"
    updated = toolbox.scan_toolbox()
    assert next(r for r in updated["items"] if r["id"] == actions[0]["id"])["description"] == actions[0]["description"]
    actions.clear()
    removed = toolbox.scan_toolbox()
    assert [r["id"] for r in removed["items"]] == [
        "core", "runnable:model_catalog", "runnable:model_chat", "runnable:model_decisions"]
    assert removed["summary"]["total"] == 4
    assert cached == before


def test_real_search_keeps_unknown_readiness_from_current_declarations(context, monkeypatch):
    toolbox, _ = context
    import legion_tool
    import tool_semantic_index
    monkeypatch.setattr(legion_tool, "_load_toolbox", toolbox.scan_toolbox)
    monkeypatch.setattr(legion_tool, "_search_mcp", lambda *a, **kw: [])
    monkeypatch.setattr(tool_semantic_index, "scores", lambda items, query: ([0.0] * len(items), "none"))
    result = legion_tool.search("precision-operation", top=4)
    row = next((r for r in result["tools"] if r["id"] == "external_capability:fixture:work"), None)
    assert row is not None, "actual search did not receive the current capability"
    assert row["execution"]["installed"] is None
    assert row["execution"]["health"] == "unknown"
    assert row["params"] == ["request_file"]


def test_read_failure_is_not_an_authoritative_deletion(context, monkeypatch):
    toolbox, actions = context
    import external_agent_registry as registry
    cached = toolbox.scan_toolbox()
    cached.pop("partial")
    cached.pop("warming")
    monkeypatch.setattr(toolbox, "_read_cached_toolbox", lambda: cached)
    registry.CATALOG.broken = True
    failed = toolbox.scan_toolbox()
    assert any(row["id"] == actions[0]["id"] for row in failed["items"])
    assert failed["capability_declarations_current"] is False
    assert failed["capability_declaration_error"] == "JSONDecodeError"
    registry.CATALOG.broken = False
    actions.clear()
    empty = toolbox.scan_toolbox()
    assert empty["capability_declarations_current"] is True
    assert not any(row["id"].startswith("external_capability:") for row in empty["items"])


def test_page_and_summary_use_the_same_current_declaration_view(context):
    toolbox, actions = context
    page = toolbox.toolbox_page(limit=100)
    assert any(row["id"] == actions[0]["id"] for row in page["items"])
    assert toolbox.toolbox_summary()["summary"]["total"] == page["summary"]["total"]
    assert toolbox.cached_toolbox() is None


def test_coalesced_force_return_also_refreshes_declarations(context, monkeypatch):
    toolbox, actions = context
    cached = {"items": [], "summary": {"total": 0, "runnable_tools": 0}, "scanned_at": time.time()}
    monkeypatch.setattr(toolbox, "_read_cached_toolbox", lambda: cached)

    class AlreadyScanning:
        def acquire(self, blocking=True):
            return blocking

        def release(self):
            pass

    monkeypatch.setattr(toolbox, "_FULL_SCAN_LOCK", AlreadyScanning())
    result = toolbox.scan_toolbox(force=True)
    assert result["coalesced"] is True
    assert result["items"][0]["id"] == actions[0]["id"]
    assert result["scanned_at"] == cached["scanned_at"]


def test_explicit_empty_snapshot_does_not_fall_back_to_another_catalog(context, monkeypatch):
    import external_agent_registry as registry
    monkeypatch.setattr(registry, "load_catalog", lambda: pytest.fail("snapshot must not be read twice"))
    assert registry.capability_actions(catalog=[]) == {"ok": True, "actions": [], "count": 0}


def test_existing_default_enumeration_keeps_its_contract(context):
    _, actions = context
    import external_agent_registry as registry
    assert registry.capability_actions("fixture")["actions"][0]["id"] == actions[0]["id"]
    assert registry.capability_actions("not-the-fixture")["count"] == 0


@pytest.mark.parametrize("broken", [False, True])
def test_full_scan_keeps_current_or_last_known_declarations(context, monkeypatch, broken):
    toolbox, actions = context
    import external_agent_registry as registry
    previous = toolbox.scan_toolbox()
    previous.pop("partial")
    previous.pop("warming")
    monkeypatch.setattr(toolbox, "_read_cached_toolbox", lambda: previous)
    for name in ("_scan_skills", "_scan_code_tools", "_scan_ecosystem_manifest",
                 "_scan_auto_registered_tools", "_scan_vision_tools", "_scan_runnable_media_tools",
                 "_scan_shared_web_tools", "_scan_agent_tool_providers", "_scan_opencli", "_scan_forged"):
        monkeypatch.setattr(toolbox, name, lambda: [])
    registry.CATALOG.broken = broken
    result = toolbox.scan_toolbox(force=True)
    assert result["items"][0]["id"] == actions[0]["id"]
    assert result["items"][0]["installed"] is None
    assert result["capability_declarations_current"] is not broken
    assert result["summary"]["total"] == 4
    assert {"runnable:model_catalog", "runnable:model_chat", "runnable:model_decisions"} <= {
        row["id"] for row in result["items"]}
