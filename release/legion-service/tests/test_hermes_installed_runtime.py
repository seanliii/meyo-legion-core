"""Hermes installation discovery only: isolated native Python, never a model."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import venv

import pytest

from test_cli_runtime_channels import cli_stack


@pytest.fixture
def installed(cli_stack, tmp_path):
    def create(name="one", version="0.20.5", source_leaf="native-source", venv_leaf="runtime"):
        root = tmp_path / name
        python_home = root / venv_leaf
        venv.EnvBuilder(with_pip=False, symlinks=True).create(python_home)
        python = python_home / "bin/python"
        site = python_home / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        metadata = site / f"hermes_agent-{version}.dist-info"
        metadata.mkdir()
        metadata.joinpath("METADATA").write_text(
            f"Metadata-Version: 2.1\nName: hermes-agent\nVersion: {version}\n"
            "Requires-Python: >=3.11,<3.14\n")
        metadata.joinpath("entry_points.txt").write_text(
            "[console_scripts]\nhermes = hermes_cli.main:main\nhermes-agent = run_agent:main\n")
        source = root / source_leaf
        for name in ("run_agent.py", "hermes_cli/main.py", "hermes_cli/__init__.py",
                     "agent/__init__.py", "tools/__init__.py"):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("raise AssertionError('discovery must not import Hermes')\n")
        source.joinpath("pyproject.toml").write_text(
            f'[project]\nname = "hermes-agent"\nversion = "{version}"\n'
            'requires-python = ">=3.11,<3.14"\n')
        metadata.joinpath("direct_url.json").write_text(json.dumps({
            "url": source.as_uri(), "dir_info": {"editable": True}}))
        return SimpleNamespace(python=python, source=source, metadata=metadata, version=version)
    return create


def test_installed_source_uses_the_queried_venv_not_its_base_interpreter(cli_stack, installed):
    native = installed()
    result = cli_stack.registry._hermes_installed_source(
        str(native.python), workdir=str(cli_stack.project))
    assert result["source"] == str(native.source)
    assert result["python"] == str(native.python)
    assert result["python"] != str(native.python.resolve())
    assert result["version"] == "0.20.5"
    assert result["python_prefix"] == str(native.python.parent.parent)
    assert result["discovery"] == "installed_distribution"


def test_different_interpreters_and_versions_do_not_share_discovery(cli_stack, installed):
    a, b = installed("a", "0.20.5"), installed("b", "0.21.0")
    resolve = cli_stack.registry._hermes_installed_source
    first = resolve(str(a.python), workdir=str(cli_stack.project))
    second = resolve(str(b.python), workdir=str(cli_stack.project))
    again = resolve(str(a.python), workdir=str(cli_stack.project))
    assert first["version"] == again["version"] == "0.20.5"
    assert second["version"] == "0.21.0"
    assert first["source"] == again["source"] == str(a.source)
    assert second["source"] == str(b.source)
    assert first["python_prefix"] != second["python_prefix"]


def test_cwd_and_pythonpath_cannot_supply_a_fake_distribution(cli_stack, installed, monkeypatch):
    native = installed()
    marker = cli_stack.project / "IMPORTED"
    for name in ("run_agent.py", "sitecustomize.py", "usercustomize.py"):
        (cli_stack.project / name).write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n")
    fake = cli_stack.project / "hermes_agent-99.0.dist-info"
    fake.mkdir()
    (fake / "METADATA").write_text("Name: hermes-agent\nVersion: 99.0\n")
    monkeypatch.chdir(cli_stack.project)
    monkeypatch.setenv("PYTHONPATH", str(cli_stack.project))
    monkeypatch.setenv("PYTHONHOME", str(cli_stack.project))
    result = cli_stack.registry._hermes_installed_source(
        str(native.python), workdir=str(cli_stack.project))
    assert result["version"] == "0.20.5" and result["source"] == str(native.source)
    assert not marker.exists()


@pytest.mark.parametrize("fault,code", [
    ("missing_distribution", "hermes_distribution_missing"),
    ("version", "hermes_source_version_mismatch"),
    ("remote_url", "hermes_source_url_invalid"),
    ("relative_url", "hermes_source_url_invalid"),
    ("missing_source", "hermes_source_layout_invalid"),
    ("missing_tools", "hermes_source_layout_invalid"),
    ("symlink_module", "hermes_source_layout_invalid"),
    ("task_source", "hermes_source_overlaps_workdir"),
])
def test_invalid_installed_metadata_fails_closed(cli_stack, installed, fault, code):
    native = installed()
    if fault == "missing_distribution":
        shutil.rmtree(native.metadata)
    elif fault == "version":
        (native.source / "pyproject.toml").write_text(
            '[project]\nname="hermes-agent"\nversion="9.9.9"\n')
    elif fault in {"remote_url", "relative_url"}:
        (native.metadata / "direct_url.json").write_text(json.dumps({
            "url": "https://example.invalid/source" if fault == "remote_url" else "file:../source",
            "dir_info": {"editable": True}}))
    elif fault == "missing_source":
        shutil.rmtree(native.source)
    elif fault == "missing_tools":
        (native.source / "tools/__init__.py").unlink()
    elif fault == "symlink_module":
        original = native.source / "run_agent.py"
        original.unlink()
        target = cli_stack.project / "untrusted.py"
        target.write_text("raise AssertionError('never import')\n")
        original.symlink_to(target)
    elif fault == "task_source":
        native.metadata.joinpath("direct_url.json").write_text(json.dumps({
            "url": cli_stack.project.as_uri(), "dir_info": {"editable": True}}))
        shutil.copytree(native.source, cli_stack.project, dirs_exist_ok=True)
    with pytest.raises(ValueError, match=code):
        cli_stack.registry._hermes_installed_source(
            str(native.python), workdir=str(cli_stack.project))


def test_broken_interpreter_is_not_replaced_by_another_python(cli_stack, tmp_path):
    python = tmp_path / "broken-python"
    python.symlink_to(tmp_path / "removed-python")
    with pytest.raises(ValueError, match="hermes_python_unavailable"):
        cli_stack.registry._hermes_installed_source(
            str(python), workdir=str(cli_stack.project))


def test_task_owned_python_is_not_executed_as_a_metadata_probe(cli_stack):
    python = cli_stack.project / "python3.12"
    marker = cli_stack.project / "EXECUTED"
    python.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    python.chmod(0o700)
    with pytest.raises(ValueError, match="hermes_python_overlaps_workdir"):
        cli_stack.registry._hermes_installed_source(
            str(python), workdir=str(cli_stack.project))
    assert not marker.exists()


def test_normal_installed_file_layout_is_owned_by_distribution(cli_stack, installed):
    native = installed()
    site = native.metadata.parent
    shutil.copytree(native.source, site, dirs_exist_ok=True)
    (native.metadata / "direct_url.json").unlink()
    owned = ["run_agent.py", "hermes_cli/main.py", "hermes_cli/__init__.py",
             "agent/__init__.py", "tools/__init__.py"]
    (native.metadata / "RECORD").write_text("".join(f"{name},,\n" for name in owned))
    result = cli_stack.registry._hermes_installed_source(
        str(native.python), workdir=str(cli_stack.project))
    assert result["source"] == str(site) and result["editable"] is False
    assert result["python"] == str(native.python) and result["version"] == "0.20.5"


def test_unsupported_native_python_does_not_borrow_another_runtime(cli_stack, installed, tmp_path):
    other = shutil.which("python3.14")
    if not other:
        pytest.skip("optional second native Python is not installed; never install it")
    home = tmp_path / "python314"
    created = cli_stack.registry._run_live(
        [other, "-I", "-m", "venv", "--without-pip", str(home)], 15,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)}, tag="__ut_hermes_venv")
    assert created.returncode == 0, created.stderr
    with pytest.raises(ValueError, match="hermes_python_unsupported"):
        cli_stack.registry._hermes_installed_source(
            str(home / "bin/python"), workdir=str(cli_stack.project))
    native = installed()
    result = cli_stack.registry._hermes_installed_source(
        str(native.python), workdir=str(cli_stack.project))
    assert result["python_version"].startswith("3.12.") and result["version"] == "0.20.5"


def _sdk_process(command, *args, **kwargs):
    code = command[command.index("-c") + 1]
    binding = next(ast.literal_eval(node.value) for node in ast.parse(code).body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "_HERMES_BINDING"
                           for target in node.targets))
    return subprocess.CompletedProcess(command, 0, json.dumps({
        "hermes_adapter_result": True, "hermes_adapter_binding": binding,
        "hermes_native_session": "same-native-session",
        "native_checkpoint": {"kind": "hermes_session", "session_id": "same-native-session"},
        "status": "ok", "final": "SAFE_FIXTURE_FINAL", "model_used": "fixture-model",
        "resumed_native": True, "native_facts": {"completed": True},
    }), "")


@pytest.mark.parametrize("layout", ["sibling", "packaged"])
def test_existing_source_layout_keeps_priority(cli_stack, monkeypatch, tmp_path, layout):
    registry = cli_stack.registry
    anchor = tmp_path / "app/harness_baselines/meyo-orchestrator/harness_registry.py"
    anchor.parent.mkdir(parents=True)
    source = (anchor.parent.parent / "hermes-agent" if layout == "sibling"
              else tmp_path / "app/runtime/core-sources/hermes-agent")
    source.mkdir(parents=True)
    (source / "run_agent.py").write_text("# existing trusted layout\n")
    if layout == "packaged":
        python = tmp_path / "app/runtime/core-runtimes/hermes-agent/venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
    monkeypatch.setattr(registry, "__file__", str(anchor))
    monkeypatch.setattr(registry, "_legion_prompt", lambda question, ctx: question)
    monkeypatch.setattr(registry, "_hermes_installed_source", lambda *a, **kw:
                        pytest.fail("existing source layout must not query another installation"))
    calls = []
    def run(command, *args, **kwargs):
        calls.append(command)
        return _sdk_process(command, *args, **kwargs)
    monkeypatch.setattr(registry, "_run_task", run)
    result = registry._hermes_adapter("priority", "fixture", ctx=registry.HarnessCtx(
        model="fixture-model", appid="fixture-key", workdir=str(cli_stack.project)))
    assert result["status"] == "ok" and result["final"] == "SAFE_FIXTURE_FINAL"
    assert len(calls) == 1 and str(source) in calls[0][-1]


def test_adapter_binds_installed_source_and_python_and_preserves_context(
        cli_stack, installed, monkeypatch, tmp_path):
    case, registry = cli_stack, cli_stack.registry
    native = installed()
    anchor = tmp_path / "pure/harness_baselines/meyo-orchestrator/harness_registry.py"
    anchor.parent.mkdir(parents=True)
    monkeypatch.setattr(registry, "__file__", str(anchor))
    monkeypatch.setattr(registry, "_legion_prompt", lambda question, ctx: question)
    monkeypatch.setattr(registry._sh, "which", lambda name:
                        str(native.python) if name == "python3.12" else None)
    import external_agent_registry
    monkeypatch.setattr(external_agent_registry, "_context", lambda *a, **kw:
                        pytest.fail("no runtime declaration: do not invent a catalog item"))
    calls = []
    def run(command, *args, **kwargs):
        calls.append((command, kwargs))
        return _sdk_process(command, *args, **kwargs)
    monkeypatch.setattr(registry, "_run_task", run)
    checkpoint = {"kind": "hermes_session", "session_id": "same-native-session"}
    result = registry._hermes_adapter("bound", "fixture", ctx=registry.HarnessCtx(
        model="fixture-model", appid="fixture-key", workdir=str(case.project),
        user_id="owner", session_id="logical-session", memory_dir=str(tmp_path / "memory"),
        task_memory={"native_checkpoint": checkpoint}))
    assert result["status"] == "ok" and result["resumed_native"] is True
    assert result["native_checkpoint"] == checkpoint
    command, options = calls[0]
    assert command[0] == str(native.python) and command[1:3] == ["-I", "-B"]
    assert options["cwd"] == str(case.project)
    assert options["env"]["TERMINAL_CWD"] == str(case.project)
    assert options["env"]["HERMES_HOME"] == str(tmp_path / "memory")
    assert options["env"]["AIGC_APPID"] == "fixture-key"
    assert "PYTHONHOME" not in options["env"] and "PYTHONPATH" not in options["env"]
    binding = result["native_facts"]["source_runtime"]
    assert binding["source"] == str(native.source) and binding["python"] == str(native.python)
    assert binding["version"] == "0.20.5"


def test_missing_installation_is_runtime_failure_without_dispatch(
        cli_stack, installed, monkeypatch, tmp_path):
    registry = cli_stack.registry
    native = installed()
    shutil.rmtree(native.metadata)
    anchor = tmp_path / "pure/harness_baselines/meyo-orchestrator/harness_registry.py"
    anchor.parent.mkdir(parents=True)
    monkeypatch.setattr(registry, "__file__", str(anchor))
    monkeypatch.setattr(registry._sh, "which", lambda name:
                        str(native.python) if name == "python3.12" else None)
    monkeypatch.setattr(registry, "_run_task", lambda *a, **kw:
                        pytest.fail("source discovery failure must not start a model/CLI"))
    checkpoint = {"kind": "hermes_session", "session_id": "preserved-session"}
    result = registry._hermes_adapter("missing", "fixture", ctx=registry.HarnessCtx(
        model="fixture-model", appid="fixture-key", workdir=str(cli_stack.project),
        task_memory={"native_checkpoint": checkpoint}))
    assert result["status"] == "harness_runtime" and result["model_used"] is None
    assert result["error_envelope"]["code"] == "hermes_cli_unavailable"
    assert result["native_facts"]["metadata_fallback_code"] == "hermes_distribution_missing"
    assert result["native_checkpoint"] == checkpoint and result["resumed_native"] is False


@pytest.mark.parametrize("declaration", ["valid", "escape", "wrong_version"])
def test_only_actual_runtime_declaration_selects_existing_pair_resolver(
        cli_stack, installed, monkeypatch, tmp_path, declaration):
    registry = cli_stack.registry
    native = installed("store/generation", source_leaf="source", venv_leaf="runtime/venv")
    monkeypatch.setenv("MEYO_RUNTIME_STORE", str(tmp_path / "store"))
    anchor = tmp_path / "pure/harness_baselines/meyo-orchestrator/harness_registry.py"
    anchor.parent.mkdir(parents=True)
    monkeypatch.setattr(registry, "__file__", str(anchor))
    monkeypatch.setattr(registry, "_legion_prompt", lambda question, ctx: question)
    import runtime_manifest
    monkeypatch.setattr(runtime_manifest, "get", lambda key: {
        "key": "hermes", "version": "9.9.9" if declaration == "wrong_version" else "0.20.5",
        "runtime": {"deployment_directory": "../generation" if declaration == "escape" else "generation",
                    "python": "3.12"},
    })
    calls = []
    def sdk(command, *args, **kwargs):
        calls.append(command)
        return _sdk_process(command, *args, **kwargs)
    monkeypatch.setattr(registry, "_run_task", sdk)
    result = registry._hermes_adapter("declared", "fixture", ctx=registry.HarnessCtx(
        model="fixture-model", appid="fixture-key", workdir=str(cli_stack.project)))
    if declaration == "valid":
        assert result["status"] == "ok"
        assert calls[0][0] == str(native.python)
        assert result["native_facts"]["source_runtime"]["discovery"] == "declared_runtime"
    else:
        assert not calls and result["status"] == "harness_runtime"
        assert result["error_envelope"]["code"] == (
            "hermes_declared_version_mismatch" if declaration == "wrong_version"
            else "hermes_runtime_declaration_invalid")


def _legacy_cli(tmp_path, *, broken=False):
    """A real offline console-script fixture, not a real Hermes/model invocation."""
    home = tmp_path / "separate-cli-runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(home)
    cli = home / "bin/hermes"
    python = home / "bin/python"
    if broken:
        python = home / "bin/removed-python"
    marker = tmp_path / "CLI_EXECUTED"
    cli.write_text(
        f"#!{python}\n"
        "import json,sys\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('native CLI fixture executed')\n"
        "if '--help' in sys.argv:\n print('ai-gateway'); raise SystemExit(0)\n"
        "output=Path(sys.argv[sys.argv.index('--usage-file')+1])\n"
        "output.write_text(json.dumps({'completed':True,'failed':False,'api_calls':0,"
        "'input_tokens':0,'output_tokens':0,'total_tokens':0}))\n"
        "print('SAFE_LEGACY_CLI_FINAL')\n")
    cli.chmod(0o700)
    return cli, python, marker


@pytest.mark.parametrize("absence", ["missing_python", "missing_distribution"])
@pytest.mark.parametrize("broken", [False, True])
def test_metadata_absence_preserves_a_separate_cli_but_not_a_broken_shebang(
        cli_stack, installed, monkeypatch, tmp_path, absence, broken):
    case, registry = cli_stack, cli_stack.registry
    primary = installed()
    shutil.rmtree(primary.metadata)
    cli, interpreter, marker = _legacy_cli(tmp_path, broken=broken)
    anchor = tmp_path / "pure/harness_baselines/meyo-orchestrator/harness_registry.py"
    anchor.parent.mkdir(parents=True)
    monkeypatch.setattr(registry, "__file__", str(anchor))
    monkeypatch.setattr(registry, "_legion_prompt", lambda question, ctx: question)
    monkeypatch.setattr(registry._sh, "which", lambda name:
                        str(cli) if name == "hermes" else
                        str(primary.python) if name == "python3.12" and absence != "missing_python"
                        else None)
    calls = []
    original = registry._run_task
    def run(command, *args, **kwargs):
        calls.append(command)
        return original(command, *args, **kwargs)
    monkeypatch.setattr(registry, "_run_task", run)
    result = registry._hermes_adapter("cli-fallback", "fixture", ctx=registry.HarnessCtx(
        model="fixture-model", appid="fixture-key", workdir=str(case.project),
        memory_dir=str(tmp_path / "memory")))
    metadata_code = ("hermes_python_unavailable" if absence == "missing_python"
                     else "hermes_distribution_missing")
    if broken:
        assert result["status"] == "harness_runtime" and result["model_used"] is None
        assert result["error_envelope"]["code"] == "hermes_cli_interpreter_unavailable"
        assert result["native_facts"]["metadata_fallback_code"] == metadata_code
        assert result["native_facts"]["model_call_count"] == 0
        assert not marker.exists() and not calls
    else:
        assert result["status"] == "ok" and result["final"] == "SAFE_LEGACY_CLI_FINAL"
        assert marker.exists() and len(calls) == 1 and calls[0][0] == str(cli)
        binding = result["native_facts"]["source_runtime"]
        assert binding["discovery"] == "legacy_cli"
        assert binding["interpreter"] == str(interpreter)
        assert binding["metadata_fallback_code"] == metadata_code
        assert result["usage"]["api_calls"] == 0


@pytest.mark.parametrize("fault", [
    "declared_missing", "declared_python_missing", "identity_failure", "source_missing", "probe_failure",
])
def test_explicit_or_invalid_metadata_never_bypasses_to_an_available_cli(
        cli_stack, installed, monkeypatch, tmp_path, fault):
    registry = cli_stack.registry
    native = installed("store/generation", source_leaf="source", venv_leaf="runtime/venv")
    cli, _, marker = _legacy_cli(tmp_path)
    anchor = tmp_path / "pure/harness_baselines/meyo-orchestrator/harness_registry.py"
    anchor.parent.mkdir(parents=True)
    monkeypatch.setattr(registry, "__file__", str(anchor))
    monkeypatch.setattr(registry._sh, "which", lambda name:
                        str(cli) if name == "hermes" else
                        str(native.python) if name == "python3.12" else None)
    if fault in {"declared_missing", "declared_python_missing"}:
        import runtime_manifest
        monkeypatch.setenv("MEYO_RUNTIME_STORE", str(tmp_path / "store"))
        monkeypatch.setattr(runtime_manifest, "get", lambda key: {
            "key": "hermes", "runtime": {"deployment_directory": "generation", "python": "3.12"}})
        if fault == "declared_missing":
            shutil.rmtree(native.metadata)
            expected = "hermes_distribution_missing"
        else:
            native.python.unlink()
            expected = "hermes_python_unavailable"
    elif fault == "identity_failure":
        native.source.joinpath("pyproject.toml").write_text(
            '[project]\nname="hermes-agent"\nversion="9.9.9"\n')
        expected = "hermes_source_version_mismatch"
    elif fault == "source_missing":
        shutil.rmtree(native.source)
        expected = "hermes_source_layout_invalid"
    else:
        expected = "hermes_metadata_probe_failed"
        def failed(*args, **kwargs):
            raise ValueError(expected)
        monkeypatch.setattr(registry, "_hermes_installed_source", failed)
    monkeypatch.setattr(registry, "_run_task", lambda *a, **kw:
                        pytest.fail("invalid/explicit binding must not execute the alternative CLI"))
    result = registry._hermes_adapter("no-bypass", "fixture", ctx=registry.HarnessCtx(
        model="fixture-model", appid="fixture-key", workdir=str(cli_stack.project)))
    assert result["status"] == "harness_runtime"
    assert result["error_envelope"]["code"] == expected
    assert not marker.exists() and result["native_facts"]["model_call_count"] == 0
