"""Cold, offline graph execution in relocated retained and legacy source layouts."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
GRAPH_SOURCE = (
    ROOT / "runtime/core-sources/auto-tools/qa_system/agent/evidence_graph.py"
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    for variable, folder in (
        ("HOME", "home"),
        ("MEYO_LEGION_DIR", "data"),
        ("LEGION_HOME_DIR", "legion"),
        ("TMPDIR", "tmp"),
    ):
        directory = tmp_path / folder
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


def _relocated_service(tmp_path, layout):
    root = tmp_path / "relocated core 图谱"
    service = root / "release/legion-service"
    service.mkdir(parents=True)
    for name in ("research_pipeline.py", "capstack_bridge.py", "legion_paths.py"):
        shutil.copyfile(SERVICE / name, service / name)

    source_roots = []
    if layout in ("retained", "both"):
        retained = root / "runtime/core-sources/auto-tools"
        retained.mkdir(parents=True)
        (retained / "retained-tools.json").write_text(
            json.dumps({"engine_enabled": False, "bridge_bindings": {}}),
            encoding="utf-8",
        )
        source_roots.append(retained)
    if layout in ("legacy", "both"):
        source_roots.append(root / "seed-users-system")
    for source_root in source_roots:
        target = source_root / "qa_system/agent/evidence_graph.py"
        target.parent.mkdir(parents=True)
        shutil.copyfile(GRAPH_SOURCE, target)
    return service, source_roots[0] / "qa_system/agent/evidence_graph.py"


_GRAPH_PROBE = r"""
import json
from pathlib import Path
import sys

def offline_only(event, args):
    if event in ("subprocess.Popen", "os.system", "socket.connect", "socket.getaddrinfo"):
        raise AssertionError("graph portability test must not call models or the network")

sys.addaudithook(offline_only)
sys.path.insert(0, sys.argv[1])
import research_pipeline

assert not research_pipeline.CALL_MODEL.exists()
source = "fixture://portable-graph"
result = research_pipeline.graph(
    text="Aurora Labs is located in Berlin.",
    source=source,
    start_entity="Aurora Labs",
    workdir=sys.argv[2],
)
assert result["ok"], result
assert any(
    edge["s"] == "Aurora Labs" and edge["r"] == "located_in"
    and edge["o"] == "Berlin" and edge["srcs"] == [source]
    for edge in result["data"]["relations"]
), result
assert result["data"]["paths"], result
artifact = Path(result["artifacts"][0]["path"])
assert artifact.is_relative_to(Path(sys.argv[2]))
assert json.loads(artifact.read_text()) == result["data"]
assert not {"qa_auto_agent", "native_agent_loop", "capability_stack"} & sys.modules.keys()
print(json.dumps({"graph_module": sys.modules["evidence_graph"].__file__}))
"""


@pytest.mark.parametrize("layout", ("retained", "legacy", "both"))
def test_graph_uses_shared_source_root_after_relocation(tmp_path, layout):
    service, expected_graph = _relocated_service(tmp_path, layout)
    environment = {
        name: os.environ[name]
        for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "TMPDIR")
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", _GRAPH_PROBE,
         str(service), str(tmp_path / "graph-output")],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        start_new_session=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert Path(json.loads(completed.stdout)["graph_module"]) == expected_graph


def _dependency_names(path):
    return {
        match.group().lower().replace("_", "-")
        for line in path.read_text().splitlines()
        if (match := re.match(r"[A-Za-z0-9_.-]+", line.strip()))
        and not line.strip().startswith("-")
    }


def test_native_sqlite_saver_is_declared_as_an_optional_dependency():
    worker = ast.parse((SERVICE / "deepagents_checkpoint.py").read_text())
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "langgraph.checkpoint.sqlite"
        and any(alias.name == "SqliteSaver" for alias in node.names)
        for node in ast.walk(worker)
    ), "This dependency belongs to the existing native DeepAgents checkpoint path"
    dependency = "langgraph-checkpoint-sqlite"
    assert dependency in _dependency_names(SERVICE / "requirements-auto.txt")
    assert dependency not in _dependency_names(SERVICE / "requirements-core.txt")
