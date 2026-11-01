"""The ordinary discovery entry must expose declarations without probing them."""
from pathlib import Path
import json
import sys

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))


def test_normal_inventory_exposes_real_superagent_actions_without_probe(monkeypatch):
    import capability_catalog
    import external_agent_registry as registry
    monkeypatch.setattr(registry, "CATALOG", SERVICE / "external_agents.json")
    monkeypatch.delenv("LEGION_EXTERNAL_CATALOG_PATH", raising=False)

    def no_probe(*args, **kwargs):
        raise AssertionError("Discovery must not start a process or query an instance")

    monkeypatch.setattr(registry, "_probe_one", no_probe)
    before = registry.CATALOG.read_bytes()
    rows = capability_catalog.inventory()["discovery"]
    selected = [row for row in rows if row["id"].startswith("external_capability:dsh-superagent:")]
    assert {row["id"].rsplit(":", 1)[-1] for row in selected} == {"status", "settings", "sources"}
    for row in selected:
        assert row["live_verified"] is False
        assert row["installed"] is None
        assert row["health"] == "unknown"
        assert row["id"] in row["invoke"]
    assert registry.CATALOG.read_bytes() == before
    assert {"research_capabilities", "skill_finder", "mcp_catalog"} <= {row["id"] for row in rows}


def test_inventory_rereads_declarations_and_does_not_mutate_them(monkeypatch):
    import capability_catalog
    import external_agent_registry as registry
    source = [{"id": "external_capability:fixture:inspect", "description": "inspect", "params": []}]
    monkeypatch.setattr(registry, "capability_actions", lambda: {"actions": source})
    original = json.dumps(source, sort_keys=True)
    first = capability_catalog.inventory()
    assert any(row["id"] == source[0]["id"] for row in first["discovery"])
    assert json.dumps(source, sort_keys=True) == original
    source.clear()
    second = capability_catalog.inventory()
    assert not any(row["id"].startswith("external_capability:fixture:") for row in second["discovery"])
