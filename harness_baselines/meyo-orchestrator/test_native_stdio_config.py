"""Offline conversion of the existing MCP registry; never launch a native engine."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        monkeypatch.setenv(key, str(tmp_path / key))
    monkeypatch.setenv("NATIVE_EXPORT_SECRET", "never-materialize-this-test-secret")
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", "never-inherit-this-test-value")


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "_native_stdio_config_test_bridge", HERE / "meyo_harnesses/mcp_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    forbidden_calls = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append("attempted side effect or proxy resolution")
        raise AssertionError("configuration export must not start services or use the network")

    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    monkeypatch.setattr(module.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(module, "_client", forbidden)
    # Native export must not resolve even a proxy credential from the platform.
    monkeypatch.setitem(sys.modules, "github_access", SimpleNamespace(proxy_url=forbidden))
    yield module
    assert forbidden_calls == []


def registry(bridge, tmp_path, declarations):
    file = tmp_path / "mcp_servers.json"
    file.write_text(json.dumps({"servers": declarations}))
    bridge.SERVERS_FILE = file
    return file


def test_real_registry_exports_only_selected_playwright(bridge):
    original = bridge.SERVERS_FILE.read_bytes()
    before_environment = dict(os.environ)
    result = bridge.native_stdio_config(["playwright"])
    assert result == {"mcpServers": {"meyo-playwright": {
        "type": "stdio",
        "command": "node",
        "args": [str(HERE / "meyo_harnesses/playwright_runtime.cjs")],
    }}}
    assert bridge.SERVERS_FILE.read_bytes() == original
    assert dict(os.environ) == before_environment


def test_export_preserves_arguments_and_declared_environment_references(bridge, tmp_path):
    registry(bridge, tmp_path, {"selected": {
        "command": "/existing path/node",
        "args": ["script with spaces.js", "--literal=two words", "${NATIVE_EXPORT_SECRET}"],
        "env": {"TOKEN_ALIAS": "${NATIVE_EXPORT_SECRET}"},
    }, "not-selected": {"command": "other", "env": {"PRIVATE": "do-not-export"}}})
    result = bridge.native_stdio_config(["selected"], namespace="my-legion")
    assert result == {"mcpServers": {"my-legion-selected": {
        "type": "stdio",
        "command": "/existing path/node",
        "args": ["script with spaces.js", "--literal=two words", "${NATIVE_EXPORT_SECRET}"],
        "env": {"TOKEN_ALIAS": "${NATIVE_EXPORT_SECRET}"},
    }}}
    serialized = json.dumps(result)
    assert os.environ["NATIVE_EXPORT_SECRET"] not in serialized
    assert os.environ["UNRELATED_PRIVATE_VALUE"] not in serialized
    assert "do-not-export" not in serialized


def test_namespaced_names_are_distinct_without_normalizing_user_names(bridge, tmp_path):
    registry(bridge, tmp_path, {
        "playwright": {"command": "node", "args": ["one.js"]},
        "other-tool": {"command": "node", "args": ["two.js"]},
        "other_tool": {"command": "node", "args": ["three.js"]},
    })
    user_config = {"playwright": {"command": "user-owned"}}
    exported = bridge.native_stdio_config(["playwright", "other-tool", "other_tool"])
    assert list(exported["mcpServers"]) == [
        "meyo-playwright", "meyo-other-tool", "meyo-other_tool"]
    assert user_config == {"playwright": {"command": "user-owned"}}
    assert "playwright" not in exported["mcpServers"]


def test_export_results_do_not_alias_prior_results_or_registry(bridge, tmp_path):
    file = registry(bridge, tmp_path, {"one": {
        "command": "node", "args": ["one.js"], "env": {"TOKEN": "${NATIVE_EXPORT_SECRET}"}}})
    raw = file.read_bytes()
    first = bridge.native_stdio_config(["one"])
    first["mcpServers"]["meyo-one"]["args"].append("changed")
    first["mcpServers"]["meyo-one"]["env"]["TOKEN"] = "changed"
    second = bridge.native_stdio_config(["one"])["mcpServers"]["meyo-one"]
    assert second["args"] == ["one.js"]
    assert second["env"] == {"TOKEN": "${NATIVE_EXPORT_SECRET}"}
    assert file.read_bytes() == raw


def test_export_has_no_file_writes(bridge, tmp_path, monkeypatch):
    registry(bridge, tmp_path, {"one": {"command": "node", "args": ["one.js"]}})
    def forbidden(*args, **kwargs):
        raise AssertionError("export is an in-memory conversion, not config installation")
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    assert bridge.native_stdio_config(["one"])["mcpServers"]["meyo-one"]["args"] == ["one.js"]


@pytest.mark.parametrize("names", [
    ["missing"], ["off"], ["one", "one"], "one", None, [""], [1], ["has space"],
])
def test_unknown_disabled_duplicate_or_invalid_selection_is_explicit(bridge, tmp_path, names):
    registry(bridge, tmp_path, {"one": {"command": "node"},
                               "off": {"command": "node", "enabled": False}})
    with pytest.raises(ValueError):
        bridge.native_stdio_config(names)


@pytest.mark.parametrize("namespace", ["", None, "../meyo", "meyo space", 5])
def test_invalid_namespace_is_rejected_not_silently_normalized(bridge, tmp_path, namespace):
    registry(bridge, tmp_path, {"one": {"command": "node"}})
    with pytest.raises(ValueError):
        bridge.native_stdio_config(["one"], namespace=namespace)


@pytest.mark.parametrize("spec", [
    {"transport": "http", "url": "https://example.invalid/mcp"},
    {"transport": "streamable-http", "command": "node"},
    {"type": "sse", "command": "node"},
    {"command": "node", "cwd": "__ORCH_ROOT__"},
    {"command": "node", "url": "https://example.invalid/mcp"},
    {"command": "node", "headers": {"Authorization": "do-not-copy-this"}},
    {"command": "node", "auth": "github"},
    {"command": "node", "optional_args": {"proxy": ["--proxy-url", "__PROXY_URL__"]}},
    {"command": []},
    {"command": " "},
    {"command": "node\x00"},
    {"command": "node", "enabled": "false"},
    {"command": "node", "args": "script.js"},
    {"command": "node", "args": [12]},
    {"command": "node", "args": ["bad\x00argument"]},
    {"command": "node", "env": ["invalid"]},
    {"command": "node", "env": {"TOKEN": 12}},
    {"command": "node", "env": {"BAD=NAME": "${NATIVE_EXPORT_SECRET}"}},
    {"command": "node", "env": {"TOKEN": "__LEGION_DIR__/\x00bad"}},
    {"command": "node", "env": {"TOKEN": "inline-secret-must-not-leak"}},
])
def test_unsupported_specs_fail_without_leaking_values(bridge, tmp_path, spec):
    registry(bridge, tmp_path, {"one": spec})
    with pytest.raises(ValueError) as error:
        bridge.native_stdio_config(["one"])
    assert "inline-secret-must-not-leak" not in str(error.value)
    assert "do-not-copy-this" not in str(error.value)
    assert os.environ["NATIVE_EXPORT_SECRET"] not in str(error.value)


def test_one_unsupported_selection_does_not_silently_return_partial_config(bridge, tmp_path):
    registry(bridge, tmp_path, {"one": {"command": "node"},
                               "bad": {"transport": "http"}})
    with pytest.raises(ValueError):
        bridge.native_stdio_config(["one", "bad"])


@pytest.mark.parametrize("text", [
    "{broken JSON",
    "[]",
    '{"servers":[]}',
    '{"servers":{"one":{"command":"first"},"one":{"command":"second"}}}',
])
def test_malformed_or_duplicate_registry_is_explicit_for_export_only(bridge, tmp_path, text):
    path = tmp_path / "invalid.json"
    path.write_text(text)
    bridge.SERVERS_FILE = path
    with pytest.raises(ValueError):
        bridge.native_stdio_config(["one"])


def test_missing_file_export_errors_but_legacy_loader_keeps_empty_behavior(bridge, tmp_path):
    bridge.SERVERS_FILE = tmp_path / "missing.json"
    assert bridge._load_servers() == {}
    with pytest.raises(ValueError):
        bridge.native_stdio_config(["one"])


def test_empty_explicit_selection_does_not_load_registry(bridge, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("empty selection must not load all MCPs")
    monkeypatch.setattr(bridge, "_load_servers", forbidden)
    assert bridge.native_stdio_config([]) == {"mcpServers": {}}


def test_existing_packaged_node_resolution_is_reused(bridge, tmp_path, monkeypatch):
    root = tmp_path / "repo/harness_baselines/meyo-orchestrator"
    monkeypatch.setattr(bridge, "_ROOT", root)
    executable = tmp_path / "repo/runtime/node-tools/bin/node"
    executable.parent.mkdir(parents=True)
    executable.write_text("not executed")
    executable.chmod(0o700)
    registry(bridge, tmp_path, {"one": {
        "command": "node", "args": ["__ORCH_ROOT__/tool.cjs", "literal argument"]}})
    spec = bridge.native_stdio_config(["one"])["mcpServers"]["meyo-one"]
    assert spec["command"] == str(executable)
    assert spec["args"] == [str(root / "tool.cjs"), "literal argument"]


def test_existing_packaged_python_entry_resolution_is_reused(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "_ROOT", tmp_path / "repo/harness_baselines/meyo-orchestrator")
    family = tmp_path / "repo/runtime/py-tools/mcp-time"
    entry = family / "bin/mcp-server-time"
    entry.parent.mkdir(parents=True)
    entry.write_text("not executed")
    registry(bridge, tmp_path, {"time": {
        "command": "uvx", "args": ["--from", "mcp-server-time", "mcp-server-time", "--local-timezone", "UTC"]}})
    spec = bridge.native_stdio_config(["time"])["mcpServers"]["meyo-time"]
    assert spec["command"] == sys.executable
    assert spec["args"] == [str(entry), "--local-timezone", "UTC"]
    assert spec["env"] == {"PYTHONPATH": str(family)}


def test_safe_path_markers_preserved_for_declared_file_environment(bridge, tmp_path):
    registry(bridge, tmp_path, {"memory": {
        "command": "node", "env": {"MEMORY_FILE_PATH": "__LEGION_DIR__/memory.jsonl"}}})
    result = bridge.native_stdio_config(["memory"])["mcpServers"]["meyo-memory"]
    assert result["env"] == {"MEMORY_FILE_PATH": str(Path(os.environ["MEYO_LEGION_DIR"]) / "memory.jsonl")}


def test_legacy_loader_still_expands_environment_and_optional_proxy(bridge, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "github_access",
                        SimpleNamespace(proxy_url=lambda: "http://test-proxy.invalid:80"))
    registry(bridge, tmp_path, {"one": {
        "command": "node", "args": ["${NATIVE_EXPORT_SECRET}"],
        "env": {"TOKEN": "${NATIVE_EXPORT_SECRET}"},
        "optional_args": {"proxy": ["--proxy-url", "__PROXY_URL__"]}}})
    spec = bridge._load_servers()["one"]
    assert spec["args"] == [os.environ["NATIVE_EXPORT_SECRET"], "--proxy-url", "http://test-proxy.invalid:80"]
    assert spec["env"] == {"TOKEN": os.environ["NATIVE_EXPORT_SECRET"]}
