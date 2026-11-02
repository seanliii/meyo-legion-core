# -*- coding: utf-8 -*-
"""Shared discovery stays available without embedding the whole catalog in prompts."""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import harness_registry as registry


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        root = tmp_path / name
        root.mkdir()
        monkeypatch.setenv(name, str(root))
    monkeypatch.syspath_prepend(str(Path(HERE).parents[1] / "release/legion-service"))


def test_protocol_exposes_shared_search_fetch_and_mcp():
    prompt = registry._legion_prompt("test")
    assert "runnable:capability_catalog" in prompt
    assert "runnable:web_fetch" in prompt
    # The full manual moved to the real on-demand discovery endpoint before
    # this change; a bare prompt is not that endpoint's response.
    import legion_tool
    catalog = legion_tool._run_tool_inner("runnable:capability_catalog", {})
    assert catalog["ok"] is True
    entries = {row["id"]: row for row in catalog["discovery"]}
    assert {"research_capabilities", "skill_finder", "search_sources",
            "github_mcp", "local_code", "remote_code", "mcp_catalog"} <= entries.keys()
    assert "runnable:research_capabilities" in entries["research_capabilities"]["invoke"]
    assert "runnable:skill_finder" in entries["skill_finder"]["invoke"]
    assert "runnable:mcp_list" in entries["mcp_catalog"]["invoke"]
    assert "runnable:mcp_call" in entries["remote_code"]["invoke"]
    assert "filesystem" in entries["local_code"]["invoke"]


def test_all_non_auto_adapters_use_shared_protocol():
    adapters = [registry._cc_adapter, registry._codex_adapter, registry._hermes_adapter,
                registry._deepseek_adapter, registry._deepagents_adapter]
    for adapter in adapters:
        assert "_legion_prompt" in inspect.getsource(adapter), adapter.__name__


def test_retained_auto_source_is_not_reactivated_as_an_answer_adapter():
    # Retained implementation/source access does not make a retired member
    # eligible for dispatch. Do not restore the old adapter to satisfy a test.
    assert callable(registry._auto_adapter)
    assert "auto" not in registry._STANDALONE_ADAPTERS
