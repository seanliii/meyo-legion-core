"""Protocol rendering/authority contracts, not a model-quality evaluator."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
import re

import pytest


SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        root = tmp_path / name
        root.mkdir()
        monkeypatch.setenv(name, str(root))
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.syspath_prepend(str(ORCH))
    return importlib.import_module("harness_registry")


def test_tool_and_text_intents_share_one_explicit_gap_section(registry):
    gap = registry._LEGION_CAPABILITY_GAP
    assert registry._LEGION_INTENT_PROTOCOL.count(gap) == 1
    assert gap not in registry._LEGION_TEXT_INTENT
    assert "existing handoff" in registry._LEGION_TEXT_INTENT
    assert registry._LEGION_INTENT_PROTOCOL != registry._LEGION_TEXT_INTENT


@pytest.mark.parametrize("name", [
    "LEGION_PROTOCOL", "_LEGION_TEXT_ACCESS", "_LEGION_TEXT_INTENT",
    "_LEGION_COLLABORATION_PROTOCOL",
])
def test_real_protocol_examples_still_format_and_parse(registry, name):
    rendered = getattr(registry, name).format(
        legion_cli="/fixture with spaces/legion_tool.py",
        media_cli="/fixture with spaces/media_tools.py",
    )
    assert "<<PEER_SOURCE_MAP>>" not in rendered
    for text in re.findall(r"<<LEGION_ORCH>>(.*?)<<END>>", rendered, re.S):
        value = json.loads(text)
        assert isinstance(value, dict) and len(value) == 1


def test_all_existing_collaboration_operations_remain_available(registry):
    rendered = registry._LEGION_COLLABORATION_PROTOCOL.format()
    messages = [json.loads(text) for text in re.findall(
        r"<<LEGION_ORCH>>(.*?)<<END>>", rendered, re.S)]
    assert {key for row in messages for key in row} == {
        "fork", "handoff", "need_peer", "captain_bid",
        "handoff_captain", "coordinate", "need_approval",
    }
    evidence = re.search(r"<<LEGION_FRONTIER_CONTEXT>>(.*?)<<END>>", rendered, re.S)
    assert set(json.loads(evidence.group(1))) == {
        "sources", "analysis", "journey_observations", "gap_plan",
    }


def test_shared_documentation_does_not_grant_unbounded_host_authority(registry):
    protocol = registry.LEGION_PROTOCOL
    assert "no path off-limits" not in protocol
    assert "--break-system-packages" not in protocol
    assert "All other local actions are authorized" not in protocol
    assert "native permissions" in protocol.lower()


def test_text_interface_keeps_its_actual_capability_boundary(registry):
    protocol = registry._LEGION_TEXT_ACCESS
    assert "tools=false" in protocol
    assert "does not add missing tools" in protocol
    assert "not a tool call" in protocol
