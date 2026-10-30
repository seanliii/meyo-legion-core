# -*- coding: utf-8 -*-
"""All harness adapters receive the same on-demand web and MCP entrypoints."""
from __future__ import annotations

import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import harness_registry as registry


def test_protocol_exposes_shared_search_fetch_and_mcp():
    prompt = registry._legion_prompt("test")
    assert "runnable:capability_catalog" in prompt
    assert "runnable:research_capabilities" in prompt
    assert "structured extraction" in prompt and "multi-hop paths" in prompt
    assert "runnable:skill_finder" in prompt
    assert "runnable:search_sources" in prompt
    assert "runnable:search_source_probe" in prompt
    assert "runnable:web_search" in prompt
    assert "runnable:web_fetch" in prompt
    assert "runnable:mcp_list" in prompt
    assert "runnable:mcp_call" in prompt
    assert "search_repositories/search_code" in prompt and "issues/PRs" in prompt
    assert "filesystem" in prompt and "search_files/read_text_file" in prompt


def test_all_non_auto_adapters_use_shared_protocol():
    adapters = [registry._cc_adapter, registry._codex_adapter, registry._hermes_adapter,
                registry._deepseek_adapter, registry._deepagents_adapter]
    for adapter in adapters:
        assert "_legion_prompt" in inspect.getsource(adapter), adapter.__name__


def test_auto_receives_same_shared_protocol():
    source = inspect.getsource(registry._auto_adapter)
    assert "LEGION_PROTOCOL.format" in source
    assert "legion_cli=_legion_tool_path()" in source
