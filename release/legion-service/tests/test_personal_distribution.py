"""Independent distribution content checks; no models, provider calls or installs."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "release/legion-service"
PACKAGED = ROOT / "runtime/core-sources/auto-tools"


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    for variable, folder in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        directory = tmp_path / folder
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


def test_existing_tool_bridge_resolves_bundled_source_without_company_checkout():
    spec = importlib.util.spec_from_file_location(
        "distribution_bridge", SERVICE / "capstack_bridge.py")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    assert Path(bridge.source_root()) == PACKAGED
    assert Path(bridge._CAPSTACK_DIR).is_relative_to(PACKAGED)
    assert set(bridge._BINDINGS) == {"sympy_calc", "duckdb_query", "ijson_stream"}


def test_bundled_programs_match_existing_export_manifest_and_remain_reference_scoped():
    path = PACKAGED / "retained-tools.json"
    assert path.is_file(), "Independent distribution is missing retained shared-tool source"
    manifest = json.loads(path.read_text())
    assert manifest["engine_enabled"] is False
    assert manifest["execution_role"] == "capability"
    programs = manifest["program_sha256"]
    assert programs and set(manifest["declared_tool_files"]) <= set(programs)
    for relative, expected in programs.items():
        path = PACKAGED / relative
        assert path.resolve().is_relative_to(PACKAGED)
        assert path.suffix == ".py" and not path.is_symlink()
        assert not set(path.relative_to(PACKAGED).parts) & {
            ".env", "sessions", "logs", "state", "outputs", "testdata", "fixtures",
        }
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected, relative
        ast.parse(data, filename=relative)
    assert {str(p.relative_to(PACKAGED)) for p in PACKAGED.rglob("*.py")} == set(programs)


def test_basic_install_does_not_require_optional_auto_engine():
    core = SERVICE / "requirements-core.txt"
    optional = SERVICE / "requirements-auto.txt"
    assert core.is_file(), "Basic CLI members need a separate small dependency install"
    assert optional.is_file(), "Optional auto engine dependencies must remain installable"
    core_text = core.read_text()
    optional_text = optional.read_text()
    for dependency in ("ddgs", "httpx", "markdown-it-py", "sympy", "duckdb", "ijson"):
        assert dependency in core_text
    for dependency in ("deepagents", "langchain-openai", "langchain-anthropic", "langgraph"):
        assert dependency in optional_text
        assert dependency not in core_text
    aggregate = (SERVICE / "requirements.txt").read_text()
    assert "-r requirements-core.txt" in aggregate
    assert "-r requirements-auto.txt" in aggregate


def test_retained_search_credentials_have_no_builtin_default():
    """Inspect syntax without importing tools or displaying a secret on failure."""
    checked = 0
    for path in PACKAGED.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_bytes(), filename=str(path))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not isinstance(node.args[0], ast.Constant):
                continue
            if node.args[0].value != "TAVILY_API_KEY":
                continue
            checked += 1
            defaults = list(node.args[1:]) + [
                item.value for item in node.keywords if item.arg == "default"
            ]
            if any(not isinstance(value, ast.Constant) or value.value not in (None, "")
                   for value in defaults):
                pytest.fail(
                    f"Built-in search credential default: {path.relative_to(PACKAGED)}:"
                    f"{node.lineno}; value intentionally withheld",
                    pytrace=False,
                )
    assert checked > 0, "The retained search provider configuration must remain present"
