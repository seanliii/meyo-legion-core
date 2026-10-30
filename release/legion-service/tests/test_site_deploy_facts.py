"""Local deployment reports mapping facts, not untested application availability."""
from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import ModuleType
from urllib.parse import quote

import pytest


SERVICE = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:18765"


@pytest.fixture(autouse=True)
def private_state(tmp_path, monkeypatch):
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        folder = tmp_path / name
        folder.mkdir()
        monkeypatch.setenv(name, str(folder))
    monkeypatch.setenv("LEGION_PUBLIC_HOST", BASE_URL)
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        pytest.fail("Mapping facts must not start processes or probe the network")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def deployer(private_state):
    # Fresh module after isolation; do not change another test's cached host.
    spec = importlib.util.spec_from_file_location(
        "__ut_site_deploy_facts", SERVICE / "site_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("root,relative,prefix", [
    ("outputs", "project/页面 (1)#%.html", "/home-outputs/"),
    ("media", "plain file.txt", "/media/"),
])
def test_local_reports_only_mapping_facts_and_preserves_url(deployer, root, relative, prefix):
    path = Path(os.environ["MEYO_LEGION_DIR"]) / root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b"<html><script>fetch('/api/example')</script></html>"
    path.write_bytes(original)

    result = deployer.deploy(str(path))

    assert {key: result[key] for key in ("ok", "mode", "url", "path")} == {
        "ok": True, "mode": "local", "url": BASE_URL + prefix + quote(relative),
        "path": str(path.resolve()),
    }
    assert {key: result.get(key) for key in (
        "delivery_kind", "runtime_managed", "http_verification", "business_verification",
    )} == {
        "delivery_kind": "static_file_url_mapping",
        "runtime_managed": False,
        "http_verification": "not_tested",
        "business_verification": "not_tested",
    }
    for fact in ("静态文件地址映射", "未托管应用进程", "HTTP/业务可用性尚未测试",
                 "授权", "mode=public", "confirm"):
        assert fact in result["note"]
    assert "运行中页面" not in result["note"]
    assert path.read_bytes() == original


def test_local_missing_file_keeps_existing_failure(deployer, tmp_path):
    path = tmp_path / "missing.html"
    assert deployer.deploy(str(path)) == {"ok": False, "error": f"文件不存在: {path}"}


def test_local_outside_supported_roots_keeps_existing_failure(deployer, tmp_path):
    path = tmp_path / "outside.html"
    path.write_text("<html></html>", encoding="utf-8")
    result = deployer.deploy(str(path))
    assert result["ok"] is False
    assert "文件不在可 serve 的目录" in result["error"]
    assert "MEYO_WORKDIR" in result["hint"]
    assert "url" not in result


@pytest.mark.parametrize("provider", ["vercel", "gist"])
def test_public_without_confirmation_stays_a_plan(deployer, monkeypatch, provider):
    def forbidden(*args, **kwargs):
        pytest.fail("Public deployment needs explicit confirmation")

    monkeypatch.setattr(deployer, "deploy_public_vercel", forbidden)
    monkeypatch.setattr(deployer, "deploy_public_gist", forbidden)
    result = deployer.deploy("index.html", mode="public", provider=provider)
    assert result == {
        "ok": True, "status": "planned", "mode": "public", "provider": provider,
        "note": f"公网部署是对外发布动作·确认后执行。将用 {provider} 部署 index.html。",
    }


@pytest.mark.parametrize("provider", ["vercel", "gist"])
def test_confirmed_public_dispatch_is_unchanged_without_publishing(deployer, monkeypatch, provider):
    calls = []
    sentinel = {"ok": True, "mode": "public", "provider": provider}

    def selected(path):
        calls.append(path)
        return sentinel

    def unselected(path):
        pytest.fail("Wrong provider selected")

    monkeypatch.setattr(deployer, "deploy_public_vercel", selected if provider == "vercel" else unselected)
    monkeypatch.setattr(deployer, "deploy_public_gist", selected if provider == "gist" else unselected)
    assert deployer.deploy("index.html", mode="public", provider=provider, confirm=True) is sentinel
    assert calls == ["index.html"]


def test_actual_toolbox_hint_explains_local_mapping_boundary(tmp_path, monkeypatch):
    # Execute the real function without importing the runtime or scanning tools.
    source = SERVICE / "legion_core.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_toolbox_hint")
    toolbox = ModuleType("toolbox")
    toolbox.scan_toolbox = lambda: {"summary": {"total": 1}}
    bridge = ModuleType("capstack_bridge")
    bridge.source_root = lambda: str(tmp_path / "retained-source")
    monkeypatch.setitem(sys.modules, "toolbox", toolbox)
    monkeypatch.setitem(sys.modules, "capstack_bridge", bridge)
    namespace = {"os": os, "HERE": str(SERVICE), "_TOOL_HINT_CACHE": {"txt": None}}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

    hint = namespace["_toolbox_hint"]()

    assert "run runnable:deploy_site" in hint
    assert '{"path":"产物","mode":"local"}' in hint
    for fact in ("静态文件地址映射", "未托管应用进程", "HTTP/业务可用性尚未测试",
                 "授权", "mode:public+confirm"):
        assert fact in hint
    assert "点开即用" not in hint
    assert namespace["_toolbox_hint"]() == hint
