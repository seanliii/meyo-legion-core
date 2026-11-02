"""Stable discovery context must not perform eager inventory/runtime discovery."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType

import pytest


SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    monkeypatch.syspath_prepend(str(SERVICE))


@pytest.fixture
def core(private_state, monkeypatch):
    module = importlib.import_module("legion_core")
    monkeypatch.setitem(module._TOOL_HINT_CACHE, "txt", None)
    return module


def test_discovery_hint_does_not_scan_or_resolve_unused_capabilities(core, monkeypatch):
    calls = []
    toolbox = ModuleType("toolbox")
    bridge = ModuleType("capstack_bridge")

    def inventory():
        calls.append("inventory")
        return {"summary": {"total": 321}}

    def retained_source():
        calls.append("retained_source")
        return "/fixture/retained-source"

    toolbox.scan_toolbox = inventory
    bridge.source_root = retained_source
    monkeypatch.setitem(sys.modules, "toolbox", toolbox)
    monkeypatch.setitem(sys.modules, "capstack_bridge", bridge)

    first = core._toolbox_hint()
    second = core._toolbox_hint()

    assert first == second and first.strip()
    assert calls == [], "Rendering stable context must not discover unused runtime capabilities"


def test_discovery_hint_keeps_the_real_lazy_tool_entrypoint(core):
    value = core._toolbox_hint()
    assert str(SERVICE / "legion_tool.py") in value
    assert "capability_catalog" in value
    assert "skill_finder" in value
    assert "scope" in value


@pytest.mark.parametrize("depth", ["standard", "deep"])
def test_standard_and_deep_share_the_existing_discovery_prefix(core, monkeypatch, depth):
    monkeypatch.setattr(core, "_toolbox_hint", lambda: "UNIQUE_DISCOVERY_ENTRY")
    monkeypatch.setattr(core, "_delivery_context", lambda: "EXISTING_DELIVERY_CONTEXT")
    monkeypatch.setattr(core, "_desktop_context", lambda: "EXISTING_DESKTOP_CONTEXT")
    value = core._stable_prelude(depth)
    assert value.count("UNIQUE_DISCOVERY_ENTRY") == 1
    assert "EXISTING_DELIVERY_CONTEXT" in value
    assert "EXISTING_DESKTOP_CONTEXT" in value


def test_quick_does_not_gain_a_new_discovery_payload(core, monkeypatch):
    def unexpected():
        pytest.fail("Quick must preserve its existing compact path")

    monkeypatch.setattr(core, "_toolbox_hint", unexpected)
    assert core._stable_prelude("quick") == core._QUICK_DELIVERY_CONTRACT
