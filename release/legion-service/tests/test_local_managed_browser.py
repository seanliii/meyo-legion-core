"""Local native browser wiring: original host capability, not shell sandbox escape."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from test_explicit_responses_provider import isolated

SERVICE = Path(__file__).resolve().parents[1]
ENTRY = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator/meyo_harnesses/playwright_runtime.cjs"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS material profile")
def test_mac_material_profile_uses_explicit_existing_browser(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    manifest = package / "package.json"
    manifest.write_text(json.dumps({"name": "@playwright/mcp", "version": "0.0.79"}))
    (package / "cli.js").write_text("// fixture")
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    profile = tmp_path / "profile.json"
    fields = {"schema_version": 1, "platform": "darwin", "runtime_root": str(tmp_path),
              "mcp_package_path": str(manifest),
              "mcp_package_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
              "browser_executable_path": str(executable),
              "browser_sha256": hashlib.sha256(executable.read_bytes()).hexdigest()}
    profile.write_text(json.dumps(fields))
    profile.chmod(0o600)
    env = dict(os.environ, MEYO_PLAYWRIGHT_RUNTIME_PROFILE=str(profile))
    command = ["node", "-e", "console.log(JSON.stringify(require(process.argv[1]).loadMaterials()))", str(ENTRY)]
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr
    result = json.loads(result.stdout)
    assert result["executable"] == str(executable.resolve())
    assert result["version"] == "0.0.79" and result["managed"] is True
    executable.write_text("changed")
    rejected = subprocess.run(command, env=env, text=True, capture_output=True, timeout=8)
    assert rejected.returncode != 0 and "checksum" in rejected.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native host wiring")
def test_mac_native_cli_gets_host_browser_and_cannot_write_its_source(isolated):
    case, mp = isolated, isolated.monkeypatch
    browser = importlib.import_module("managed_browser")
    control = case.root / "host-control"
    control.mkdir()
    native_tmp = case.root / "native-tmp"
    native_tmp.mkdir()
    mp.setenv("TMPDIR", str(native_tmp))
    mp.setenv("MEYO_RUNTIME_STORE", str(control))
    mp.setenv("LEGION_MANAGED_BROWSER", "1")
    mp.setenv("MEYO_PLAYWRIGHT_RUNTIME_PROFILE", str(control / "profile.json"))
    calls = []

    class Lease:
        closed = False
        observations = []
        cleanup = {}
        tools = [{"name": "browser_navigate"}, {"name": "browser_take_screenshot"}]
        mcp_config = {"mcpServers": {"browser": {
            "type": "http", "url": "http://127.0.0.1:45001/mcp",
            "headers": {"Authorization": "Bearer fixture-owned-browser-token"},
        }}}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    def open_browser(ctx, **kwargs):
        calls.append(kwargs)
        assert Path(kwargs["config_dir"]).is_relative_to(control)
        return Lease()

    mp.setattr(browser, "open_managed_browser", open_browser)
    work = case.root / "work"
    work.mkdir()
    result = case.hr._codex_adapter(
        "__ut_local_browser", "inspect actual result",
        ctx=case.hr.HarnessCtx(
            model="fixture-model", appid="fixture-key", gateway="https://models.example.invalid/v1",
            model_protocol="responses", user_id="fixture-user", session_id="fixture-session",
            workdir=str(work), execution_depth="standard"))
    assert len(calls) == 1
    command, kwargs = case.calls[0]
    config = tomllib.loads("\n".join(command[i + 1] for i, v in enumerate(command) if v == "-c"))
    assert config["mcp_servers"]["meyo_playwright"]["url"] == "http://127.0.0.1:45001/mcp"
    source_root = str(SERVICE.parents[1])
    assert source_root not in config["sandbox_workspace_write"]["writable_roots"]
    assert str(native_tmp) in config["sandbox_workspace_write"]["writable_roots"]
    assert config["sandbox_workspace_write"]["exclude_slash_tmp"] is True
    assert config["sandbox_workspace_write"]["exclude_tmpdir_env_var"] is True
    assert command[command.index("-s") + 1] == "workspace-write"
    assert not Path(calls[0]["config_dir"]).exists()
    assert "fixture-owned-browser-token" not in " ".join(command)
    assert "fixture-owned-browser-token" not in kwargs["input"]
    input_facts = json.loads(kwargs["input"].split("NATIVE_MODEL_INPUT_FACTS_JSON\n", 1)[1].splitlines()[0])
    assert input_facts["input_modalities"] == ["text"]
    assert input_facts["scope"] == "native_input_gate_only_not_a_provider_capability_test"
    assert result["native_facts"]["managed_browser"]["closed"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native host wiring")
@pytest.mark.parametrize("bad_config", ["missing_store", "mutable_store", "global_tmp", "invalid_mode"])
def test_mac_browser_never_falls_back_to_native_shell_when_unsafe(isolated, bad_config):
    case, mp = isolated, isolated.monkeypatch
    work = case.root / "work"
    work.mkdir()
    control = case.root / "host-control"
    control.mkdir()
    native_tmp = case.root / "native-tmp"
    native_tmp.mkdir()
    mp.setenv("LEGION_MANAGED_BROWSER", "1")
    mp.setenv("MEYO_RUNTIME_STORE", str(control))
    mp.setenv("TMPDIR", str(native_tmp))
    if bad_config == "missing_store":
        mp.delenv("MEYO_RUNTIME_STORE")
    elif bad_config == "mutable_store":
        mp.setenv("MEYO_RUNTIME_STORE", str(work))
    elif bad_config == "global_tmp":
        mp.setenv("TMPDIR", "/tmp")
    else:
        mp.setenv("LEGION_MANAGED_BROWSER", "maybe")
    result = case.hr._codex_adapter(
        "__ut_bad_local_browser", "inspect actual result",
        ctx=case.hr.HarnessCtx(
            model="fixture-model", appid="fixture-key", gateway="https://models.example.invalid/v1",
            model_protocol="responses", user_id="fixture-user", session_id="fixture-session",
            workdir=str(work), execution_depth="standard"))
    assert not case.calls
    assert result["status"] == "error"
    assert result["error_envelope"]["category"] == "harness_runtime"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS private temporary scope")
def test_mac_host_profile_cannot_be_selected_from_native_private_tmp(tmp_path):
    from types import SimpleNamespace
    sys.path.insert(0, str(SERVICE))
    from managed_browser import ManagedBrowserLease, BrowserUnavailable

    roots = {name: tmp_path / name for name in ("work", "control", "runtime", "native-tmp", "home")}
    for root in roots.values():
        root.mkdir()
    node, worker = roots["runtime"] / "node", roots["runtime"] / "worker.cjs"
    node.write_text("fixture")
    worker.write_text("fixture")
    profile = roots["native-tmp"] / "profile.json"
    profile.write_text("{}")
    profile.chmod(0o600)
    lease = ManagedBrowserLease(
        SimpleNamespace(workdir=str(roots["work"]), user_id="fixture", session_id="fixture"),
        config_dir=str(roots["control"]),
        env={"MEYO_RUNTIME_STORE": str(roots["control"]), "MEYO_RUNTIME_ROOT": str(roots["runtime"]),
             "HOME": str(roots["home"]), "TMPDIR": str(roots["native-tmp"]),
             "MEYO_PLAYWRIGHT_RUNTIME_PROFILE": str(profile)})
    with pytest.raises(BrowserUnavailable, match="outside all native-writable roots"):
        lease._validate_materials(node, worker)


@pytest.fixture
def material_scope(tmp_path, monkeypatch):
    """只造最小包文件与链接；Node 仅解析材料，不执行包、浏览器或网络。"""
    from types import SimpleNamespace
    monkeypatch.syspath_prepend(str(SERVICE))
    from managed_browser import ManagedBrowserLease
    roots = {name: (tmp_path / name).resolve()
             for name in ("work", "control", "runtime", "native-tmp", "home")}
    for root in roots.values():
        root.mkdir()
    package = roots["runtime"] / "mcp"
    core = package / "node_modules" / "playwright-core"
    (core / "lib").mkdir(parents=True)
    manifest = package / "package.json"
    manifest.write_text(json.dumps({
        "name": "@playwright/mcp", "version": "0.0.79",
        "dependencies": {"playwright-core": "fixture"},
    }))
    (core / "package.json").write_text(json.dumps({
        "name": "playwright-core", "version": "fixture", "main": "index.js",
    }))
    for target in (package / "cli.js", package / "index.js",
                   core / "index.js", core / "lib/utilsBundle.js"):
        target.write_text("throw new Error('fixture module must never execute');\n")
    node, worker, executable = (roots["runtime"] / name for name in ("node", "worker.cjs", "chrome"))
    for target in (node, worker, executable):
        target.write_text("fixture, never executed\n")
        target.chmod(0o700)
    profile = roots["control"] / "profile.json"
    profile.write_text(json.dumps({
        "schema_version": 1, "platform": "darwin", "runtime_root": str(roots["runtime"]),
        "mcp_package_path": str(manifest),
        "mcp_package_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "browser_executable_path": str(executable),
        "browser_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }))
    profile.chmod(0o600)
    env = dict(os.environ, HOME=str(roots["home"]), TMPDIR=str(roots["native-tmp"]),
               MEYO_RUNTIME_STORE=str(roots["control"]), MEYO_RUNTIME_ROOT=str(roots["runtime"]),
               MEYO_PLAYWRIGHT_RUNTIME_PROFILE=str(profile),
               MEYO_MANAGED_BROWSER_CONFIG=str(roots["control"] / "parent-owned.json"))
    # 这是 parent 由现有写边界生成的事实，不来自任务参数或用户 profile。
    env["MEYO_MANAGED_BROWSER_WRITABLE_ROOTS"] = json.dumps(
        [str(roots[name]) for name in ("work", "home", "native-tmp")])
    lease = ManagedBrowserLease(
        SimpleNamespace(workdir=str(roots["work"]), user_id="fixture", session_id="fixture"),
        config_dir=str(roots["control"]), env=env)

    def inspect(extra=""):
        return subprocess.run([
            "node", "-e",
            "const materials=require(process.argv[1]).loadMaterials();"
            + extra + "console.log(JSON.stringify(materials));", str(ENTRY),
        ], env=env, capture_output=True, text=True, timeout=8)

    return SimpleNamespace(roots=roots, package=package, core=core, env=env,
                           node=node, worker=worker, lease=lease, inspect=inspect)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS material boundary")
@pytest.mark.parametrize("entry", ["cli.js", "index.js"])
def test_mac_python_preflight_rejects_native_writable_package_entry(material_scope, entry):
    from managed_browser import BrowserUnavailable
    case = material_scope
    replacement = case.roots["work"] / entry
    replacement.write_text("throw new Error('native-writable module');\n")
    path = case.package / entry
    path.unlink()
    path.symlink_to(replacement)
    with pytest.raises(BrowserUnavailable, match="outside all native-writable roots"):
        case.lease._validate_materials(case.node, case.worker)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS material boundary")
@pytest.mark.parametrize("entry", ["cli.js", "index.js", "core-index", "core-bundle", "dependency"])
def test_managed_loader_rejects_native_writable_entries_and_dependencies(material_scope, entry):
    case = material_scope
    if entry == "dependency":
        # Node 可以从包的祖先 node_modules 解析依赖，不能只检查包内文件。
        dependency = case.roots["work"] / "dependency"
        dependency.mkdir()
        (dependency / "package.json").write_text('{"name":"fixture-dependency","main":"index.js"}')
        (dependency / "index.js").write_text("throw new Error('must not execute');")
        parent_modules = case.roots["runtime"] / "node_modules"
        parent_modules.mkdir()
        (parent_modules / "fixture-dependency").symlink_to(dependency, target_is_directory=True)
        (case.core / "package.json").write_text(json.dumps({
            "name": "playwright-core", "main": "index.js",
            "dependencies": {"fixture-dependency": "fixture"},
        }))
    else:
        path = (case.core / ("index.js" if entry == "core-index" else "lib/utilsBundle.js")
                if entry.startswith("core-") else case.package / entry)
        replacement = case.roots["work"] / "selected.js"
        replacement.write_text("throw new Error('must not execute');")
        path.unlink()
        path.symlink_to(replacement)
    result = case.inspect()
    assert result.returncode != 0, "宿主不得从原生可写路径加载入口或传递依赖"
    assert "native-writable" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS material boundary")
def test_managed_loader_accepts_readonly_dependency_symlink(material_scope):
    case = material_scope
    relocated = case.roots["runtime"] / "readonly-core"
    case.core.rename(relocated)
    case.core.symlink_to(relocated, target_is_directory=True)
    result = case.inspect()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["managed"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS material boundary")
def test_managed_loader_requires_parent_supplied_write_boundary(material_scope):
    case = material_scope
    case.env.pop("MEYO_MANAGED_BROWSER_WRITABLE_ROOTS")
    result = case.inspect()
    assert result.returncode != 0
    assert "native-writable" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS material boundary")
def test_actual_module_resolution_cannot_later_select_native_writable_file(material_scope):
    case = material_scope
    target = case.roots["work"] / "later-selected.js"
    target.write_text("throw new Error('must never execute');")
    result = case.inspect(
        "require('node:module').createRequire(materials.cli).resolve("
        + json.dumps(str(target)) + ");")
    assert result.returncode != 0
    assert "native-writable" in result.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS material boundary")
def test_parent_passes_original_write_boundary_not_task_environment(material_scope, monkeypatch):
    from types import SimpleNamespace
    case = material_scope
    monkeypatch.syspath_prepend(str(SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"))
    from meyo_harnesses import mcp_bridge
    captured = {}

    class backend:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(mcp_bridge, "_StdioMCP", backend)
    lease = case.lease
    lease.env["MEYO_MANAGED_BROWSER_WRITABLE_ROOTS"] = "[]"
    lease.scratch_dir = case.roots["control"] / "host-scratch"
    lease.scratch_dir.mkdir()
    lease.output_dir = lease.scratch_dir / "output"
    lease.output_dir.mkdir()
    lease.base_url = "http://127.0.0.1:45001"
    expected = [str(root) for root in lease._native_writable_roots()]
    lease._create_backend()
    env = captured["environment"]
    assert json.loads(env["MEYO_MANAGED_BROWSER_WRITABLE_ROOTS"]) == expected
    assert str(case.roots["home"]) in expected
    assert env["HOME"] not in expected, "宿主 scratch 不能替换原生写边界"
