"""Onboarding reports must distinguish absent distribution checks from failures.

Slow/member/provider boundaries are test doubles. The report branch and any
temporary mode-check script execute for real; this is not member acceptance.
"""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile

import pytest


SERVICE_DIR = Path(__file__).resolve().parents[1]
KEY = "__ut_distribution_member"
MODES = ["single_harness", "parallel_pk", "serial_relay", "orchestrate"]


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.syspath_prepend(str(SERVICE_DIR))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


@pytest.fixture
def onboard(tmp_path, monkeypatch, isolated_environment):
    spec = importlib.util.spec_from_file_location(
        "__ut_onboard_distribution", SERVICE_DIR / "harness_onboard.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    core = tmp_path / "core"
    service = core / "release/legion-service"
    service.mkdir(parents=True)
    monkeypatch.setattr(module, "__file__", str(service / "harness_onboard.py"))
    monkeypatch.setattr(module, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(
        module.R, "load_catalog",
        lambda: [{"key": KEY, "execution_role": "agent"}],
    )
    monkeypatch.setattr(
        module.R, "install", lambda *a, **kw: {"ok": True, "status": "installed"}
    )
    monkeypatch.setattr(
        module.R, "probe", lambda *a, **kw: {"runnable": True, "status": "ready"}
    )
    monkeypatch.setattr(module, "_harness_pool_has", lambda *a: (True, "test pool"))
    monkeypatch.setattr(module, "_tool_run", lambda *a: (True, "test tool boundary"))
    monkeypatch.setattr(module, "_speed_runs", lambda *a: ([
        {"wall_s": 0.1, "api_ms": 1, "ok": True, "retried": False,
         "status": "ok", "final": "2", "error": ""}
        for _ in range(3)
    ], []))
    return module, core


def invoke(onboard, monkeypatch, *options):
    module, _ = onboard
    monkeypatch.setattr(sys, "argv", ["harness_onboard.py", "--key", KEY, *options])
    code = module.main()
    reports = list(module.REPORT_DIR.glob(f"{KEY}-*.json"))
    assert len(reports) == 1
    return code, json.loads(reports[0].read_text())


def write_modes(core, *, mode_names=MODES, exit_code=0, write_report=True):
    script = core / "dev/benchmark_execution_modes.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import json, pathlib, sys\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        + (f"output.write_text(json.dumps({{'runs': "
           f"[{{'mode': value}} for value in {mode_names!r}]}}))\n"
           if write_report else "")
        + f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    return script


def test_official_cc_runtime_spec_is_locked_capability(onboard, monkeypatch, tmp_path):
    module, _ = onboard
    path = SERVICE_DIR / "external_adapters/cc-runtime.spec.json"
    entry = json.loads(path.read_text())
    lock = json.loads(path.with_name("cc-runtime-package-lock.json").read_text())
    package = json.loads(path.with_name("cc-runtime-package.json").read_text())
    assert module.R.agent_manifest.validate(entry)["ok"]
    assert entry["key"] == "cc-runtime" and entry["execution_role"] == "capability"
    assert not entry.get("transports") and not entry.get("provided_by")
    assert entry["version"] == "2.1.317"
    assert entry["revision"] == "964d69565952a176ac1975ed218362991db71433"
    assert package["dependencies"] == lock["packages"][""]["dependencies"]
    wrapper = lock["packages"]["node_modules/@anthropic-ai/claude-code"]
    assert wrapper["integrity"] == entry["source_evidence"]["npm_integrity"]
    assert wrapper["engines"]["node"] == ">=22.0.0"
    for name, version in wrapper["optionalDependencies"].items():
        native = lock["packages"]["node_modules/" + name]
        assert native["version"] == version == "2.1.317"
        assert native["integrity"].startswith("sha512-") and native["optional"]
        assert native["resolved"].startswith("https://registry.npmjs.org/")
    for command in [*entry["install"], entry["build_probe"]["command"]]:
        assert module.R._safe_step(module.R._format_argv(command, entry))
    assert "--include=optional" in entry["install"][2]
    assert "--ignore-scripts" in entry["install"][2]
    assert "require(script)" in entry["install"][3][2]
    assert "req.resolve" in entry["install"][3][2]
    saved = []
    monkeypatch.setattr(module.R, "load_catalog", lambda: [{"key": "cc", "execution_role": "agent"}])
    monkeypatch.setattr(module.R, "_save_catalog", lambda rows: saved.extend(rows))
    monkeypatch.setattr(module.R, "catalog_lock", __import__("contextlib").nullcontext)

    def install(key, **kwargs):
        assert key == "cc-runtime" and kwargs["build_only"] is True
        assert saved == [{"key": "cc", "execution_role": "agent"}, entry]
        return {"ok": True, "status": "built", "runtime_acceptance": "not_run"}

    monkeypatch.setattr(module.R, "install", install)
    monkeypatch.setattr(module, "_run_agent", lambda *a, **k: pytest.fail("capability answered"))
    result = module.onboard(key="cc-runtime", spec=str(path), build_only=True)
    assert result["report"]["scope"] == "build_only"
    assert result["report"]["runtime_acceptance"] == "not_run"


@pytest.mark.parametrize("case", ["exact", "modified", "managed_restore"])
def test_cc_build_only_cli_uses_exact_reviewed_host_identity(tmp_path, monkeypatch, capsys, case):
    from types import SimpleNamespace
    spec_path = SERVICE_DIR / "external_adapters/cc-runtime.spec.json"
    entry = json.loads(spec_path.read_text())
    registry_spec = importlib.util.spec_from_file_location(
        "__ut_cc_host_registry", SERVICE_DIR / "external_agent_registry.py")
    registry = importlib.util.module_from_spec(registry_spec)
    registry_spec.loader.exec_module(registry)
    assert registry._TRUSTED_PROBE_SPECS["cc-runtime"] == registry._probe_spec_identity(entry)
    monkeypatch.setenv("LEGION_EXTERNAL_SOURCE_ROOT", str(tmp_path / "sources"))
    monkeypatch.setenv("LEGION_EXTERNAL_RUNTIME_ROOT", str(tmp_path / "native"))
    monkeypatch.setattr(registry, "CATALOG", tmp_path / "state/catalog.json")
    monkeypatch.setattr(registry, "_STATE_PATH", tmp_path / "state/install-state.json")
    monkeypatch.setattr(registry, "_LOG_DIR", tmp_path / "state/logs")
    monkeypatch.setattr(registry, "_native_onboard_lease", lambda: None)
    module_spec = importlib.util.spec_from_file_location(
        "__ut_cc_host_onboard", SERVICE_DIR / "harness_onboard.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "R", registry)
    monkeypatch.setattr(module, "REPORT_DIR", tmp_path / "reports")
    if case == "modified":
        entry["name"] += " user replacement"
        spec_path = tmp_path / "modified-spec.json"
        spec_path.write_text(json.dumps(entry))
    elif case == "managed_restore":
        # Record a real sticky managed replacement, then restore the bundled
        # declaration through the ordinary host CLI; neither may inherit trust.
        registry._save_catalog([entry])
        with monkeypatch.context() as patch:
            patch.setattr(registry, "_native_onboard_lease", lambda: SimpleNamespace(check=lambda: None))
            registry._save_catalog([{**entry, "name": "managed replacement"}])
    source = registry.source_dir(entry)
    (source / ".git").mkdir(parents=True)
    monkeypatch.setattr(registry, "_git_revision", lambda *a: entry["revision"])
    monkeypatch.setattr(registry, "_strip_dead_proxy", lambda env: dict(env))
    install_calls, probe_calls = [], []

    def install_step(command, *args):
        assert registry._native_onboard_lease() is None
        install_calls.append(command)
        return dict(exit_code=0, timed_out=False, stdout="", stderr="")

    def probe_command(command, root, **kwargs):
        probe_calls.append(command)
        assert command == registry._format_argv(entry["build_probe"]["command"], entry)
        return True, "fixture build proof only; task acceptance not_run"

    monkeypatch.setattr(registry, "_run_install_step", install_step)
    monkeypatch.setattr(registry, "_run_probe_command", probe_command)
    monkeypatch.setattr(sys, "argv", ["harness_onboard.py", "--key", "cc-runtime",
                                    "--spec", str(spec_path), "--build-only", "--install-timeout", "5"])
    code = module.main()
    report = json.loads(capsys.readouterr().out)
    assert install_calls and registry.load_catalog() == [entry]
    assert report["scope"] == "build_only" and report["runtime_acceptance"] == "not_run"
    assert bool(probe_calls) == (case == "exact")
    assert report["ok"] == (case == "exact")
    assert code == (0 if case == "exact" else 1)
    assert report["probe"]["status"] == ("built" if case == "exact" else "requires_managed_probe")
    if case == "managed_restore":
        assert "cc-runtime" in json.loads(registry.CATALOG.read_text())["native_managed_sources"]
    # Even the reviewed identity cannot bypass the existing catalog mode check.
    registry.CATALOG.chmod(0o666)
    assert registry.probe_build(entry)["status"] == "requires_managed_probe"
    assert len(probe_calls) == (1 if case == "exact" else 0)


@pytest.mark.parametrize("fault", ["ancestor", "symlink", "hardlink", "metadata", "ok"])
def test_cc_official_postinstall_cannot_borrow_ancestor_package(tmp_path, fault):
    import os
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable; no installation attempted")
    runtime = tmp_path.resolve() / "runtime"
    wrapper = runtime / "node_modules/@anthropic-ai/claude-code"
    wrapper.mkdir(parents=True)
    (wrapper / "install.cjs").write_text("throw Error('fixture-official-script-reached')")
    ancestor = tmp_path.resolve() / "node_modules/@anthropic-ai/claude-code-linux-x64"
    local = runtime / "node_modules/@anthropic-ai/claude-code-linux-x64"
    selected = ancestor if fault in ("ancestor", "symlink") else local
    selected.mkdir(parents=True)
    (selected / "package.json").write_text(json.dumps(dict(
        name="wrong" if fault == "metadata" else "@anthropic-ai/claude-code-linux-x64", version="2.1.317")))
    (selected / "claude").write_bytes(b"never-executed")
    if fault == "symlink":
        local.symlink_to(ancestor, target_is_directory=True)
    if fault == "hardlink":
        os.link(selected / "claude", tmp_path / "outside-native")
    entry = json.loads((SERVICE_DIR / "external_adapters/cc-runtime.spec.json").read_text())
    prefix = "Object.defineProperty(process,'platform', {value:'linux'}); Object.defineProperty(process,'arch', {value:'x64'}); process.report.getReport=()=>({header:{glibcVersionRuntime:'fixture'}}); "
    result = subprocess.run([node, "-e", prefix + entry["install"][3][2], str(runtime)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert ("fixture-official-script-reached" in result.stderr) == (fault == "ok")


@pytest.mark.parametrize("fault", ["short", "arch", "table", "segment", "content", "missing", "symlink", "hardlink", "metadata", "version", "ok"])
def test_cc_native_install_proof_rejects_false_evidence(tmp_path, monkeypatch, fault):
    import os
    import struct
    import external_agent_registry as registry
    entry = json.loads((SERVICE_DIR / "external_adapters/cc-runtime.spec.json").read_text())
    runtime = tmp_path.resolve() / "runtime"
    package = runtime / "node_modules/@anthropic-ai/claude-code"
    selected = runtime / "node_modules/@anthropic-ai/claude-code-linux-x64"
    (package / "bin").mkdir(parents=True)
    selected.mkdir()
    for directory, name in ((package, "@anthropic-ai/claude-code"), (selected, "@anthropic-ai/claude-code-linux-x64")):
        (directory / "package.json").write_text(json.dumps(dict(name=name, version="2.1.317")))
    (runtime / "package-lock.json").write_bytes((SERVICE_DIR / "external_adapters/cc-runtime-package-lock.json").read_bytes())
    # Structurally plausible fixture only; native execution is always mocked.
    header = b"\x7fELF\x02\x01\x01" + bytes(9) + struct.pack(
        "<HHIQQQIHHHHHH", 2, 183 if fault == "arch" else 62, 1, 0x400078,
        9999 if fault == "table" else 64, 0, 0, 64, 56, 1, 0, 0, 0)
    payload = header + struct.pack("<IIQQQQQQ", 1, 5, 0, 0x400000, 0,
                                   9999 if fault == "segment" else 121, 121, 4096) + b"x"
    if fault == "short":
        payload = payload[:20]
    binary, native = package / "bin/claude.exe", selected / "claude"
    binary.write_bytes(payload)
    native.write_bytes(payload + (b"different-tail" if fault == "content" else b""))
    binary.chmod(0o755)
    native.chmod(0o755)
    (runtime / "node_modules/.bin").mkdir()
    (runtime / "node_modules/.bin/claude").symlink_to(binary)
    if fault == "missing":
        native.unlink()
    elif fault == "symlink":
        native.unlink()
        native.symlink_to(binary)
    elif fault == "hardlink":
        os.link(native, tmp_path / "external-binary")
    elif fault == "metadata":
        (selected / "package.json").write_text(json.dumps(dict(name="wrong", version="2.1.317")))
    calls = []

    def probe(command, cwd, **kwargs):
        calls.append(command)
        if command[1] == "-p":
            return True, "linux-x64"
        assert kwargs["env"]["DISABLE_AUTOUPDATER"] == "1"
        assert kwargs["env"]["HOME"] != str(Path.home())
        return True, "wrong version" if fault == "version" else "2.1.317 (Claude Code)"

    monkeypatch.setattr(registry, "_run_probe_command", probe)
    monkeypatch.setattr(__import__("platform"), "system", lambda: "Linux")
    monkeypatch.setattr(sys, "argv", ["proof", str(runtime), "fixture-node", str(SERVICE_DIR / "external_adapters")])
    script = entry["build_probe"]["command"][2]
    if fault == "ok":
        exec(compile(script, "cc-build-proof", "exec"), {})
        assert len(calls) == 2
    else:
        with pytest.raises((AssertionError, FileNotFoundError)):
            exec(compile(script, "cc-build-proof", "exec"), {})
        assert len(calls) == (2 if fault == "version" else 1)


def test_missing_optional_matrix_preserves_passed_checks_but_not_full_pass(onboard, monkeypatch):
    code, report = invoke(onboard, monkeypatch)

    assert report["verdict"] == "PARTIAL"
    assert code == 2
    assert all(step["status"] == "PASS" for step in report["steps"][:6])
    matrix = report["steps"][6]
    assert matrix["status"] == "SKIP"
    assert matrix["reason"] == "not_included"


def test_real_failure_is_not_hidden_by_missing_matrix(onboard, monkeypatch):
    module, _ = onboard
    monkeypatch.setattr(module.R, "install", lambda *a, **kw: {
        "ok": False, "status": "failed", "error": "synthetic build failure",
    })

    code, report = invoke(onboard, monkeypatch)

    assert code == 1
    assert report["verdict"] == "FAIL"
    assert report["steps"][1]["status"] == "FAIL"
    assert report["steps"][6].get("reason") == "not_included"


def test_present_matrix_really_runs_and_saves_unique_report(onboard, monkeypatch):
    module, core = onboard
    write_modes(core)

    code, report = invoke(onboard, monkeypatch)

    assert code == 0
    assert report["verdict"] == "PASS"
    assert report["steps"][6]["status"] == "PASS"
    matrices = list(module.REPORT_DIR.glob("modes-*/matrix.json"))
    assert len(matrices) == 1
    assert json.loads(matrices[0].read_text()) == {
        "runs": [{"mode": mode} for mode in MODES]
    }


def test_no_output_cannot_reuse_previous_member_matrix(onboard, monkeypatch, tmp_path):
    _, core = onboard
    write_modes(core, write_report=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / f"onboard-modes-{KEY}.json"
    stale.write_text(json.dumps({"runs": [{"mode": mode} for mode in MODES]}))

    code, report = invoke(onboard, monkeypatch)

    assert code == 1
    assert report["verdict"] == "FAIL"
    assert report["steps"][6]["status"] == "FAIL"
    assert stale.is_file()


@pytest.mark.parametrize("exit_code,mode_names", [(3, MODES), (0, MODES[:1])])
def test_present_matrix_failure_remains_failure(onboard, monkeypatch, exit_code, mode_names):
    _, core = onboard
    write_modes(core, exit_code=exit_code, mode_names=mode_names)

    code, report = invoke(onboard, monkeypatch)

    assert code == 1
    assert report["verdict"] == "FAIL"
    assert report["steps"][6]["status"] == "FAIL"


def test_requested_skip_is_not_full_acceptance(onboard, monkeypatch):
    _, core = onboard
    write_modes(core)

    code, report = invoke(onboard, monkeypatch, "--skip-install")

    assert code == 2
    assert report["verdict"] == "PARTIAL"
    assert report["steps"][1].get("reason") == "requested_skip"


def test_build_only_keeps_existing_limited_result(onboard, monkeypatch, capsys):
    module, _ = onboard
    monkeypatch.setattr(sys, "argv", [
        "harness_onboard.py", "--key", KEY, "--build-only",
    ])

    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scope"] == "build_only"
    assert result["runtime_acceptance"] == "not_run"
    assert not module.REPORT_DIR.exists()


def build_spec(tmp_path, **changes):
    entry = {
        "key": KEY, "name": "Offline onboarding fixture", "kind": "agent",
        "execution_role": "agent", "url": "https://github.com/example/fixture",
        "version": "0.0.0-test", "revision": "a" * 40,
        "probe": {"method": "path", "target": "{source}/fixture"},
        "adapter_capabilities": {"tools": False},
    }
    entry.update(changes)
    path = tmp_path / "explicit-spec.json"
    path.write_text(json.dumps(entry))
    return path, entry


@pytest.mark.parametrize("existing", [False, True])
def test_build_only_explicit_spec_uses_locked_upsert_before_install(
    onboard, monkeypatch, tmp_path, capsys, existing
):
    from contextlib import contextmanager

    module, _ = onboard
    spec_path, entry = build_spec(tmp_path)
    catalog = tmp_path / "catalog" / "external_agents.json"
    catalog.parent.mkdir()
    other = {"key": "__ut_unrelated_member", "retained": True}
    items = [other, {"key": KEY, "version": "old"}] if existing else [other]
    catalog.write_text(json.dumps({"agents": items}))
    monkeypatch.setattr(module.R, "CATALOG", catalog)
    monkeypatch.setattr(module.R, "load_catalog", lambda: json.loads(catalog.read_text())["agents"])
    original_lock = module.R.catalog_lock
    events = []

    @contextmanager
    def observed_lock():
        with original_lock():
            events.append("locked")
            yield
        events.append("unlocked")

    monkeypatch.setattr(module.R, "catalog_lock", observed_lock)
    original_save = module.R._save_catalog

    def save(items):
        # _save_catalog also re-enters the same lock; require the outer read/update lock.
        assert events.count("locked") > events.count("unlocked")
        return original_save(items)

    monkeypatch.setattr(module.R, "_save_catalog", save)

    def install(key, **kwargs):
        assert events and events.count("locked") == events.count("unlocked")
        assert key == KEY
        assert kwargs == {"confirm": True, "timeout_s": 17, "build_only": True}
        assert module.R.load_catalog() == [other, entry]
        events.append("install")
        return {"ok": True, "status": "built", "runtime_acceptance": "not_run"}

    def unexpected(*args, **kwargs):
        pytest.fail("build-only must not run model, pool or runtime-acceptance stages")

    monkeypatch.setattr(module.R, "install", install)
    monkeypatch.setattr(module.R, "probe", unexpected)
    for name in ("_harness_pool_has", "_speed_runs", "_tool_run", "_capability_runs"):
        monkeypatch.setattr(module, name, unexpected)
    monkeypatch.setattr(sys, "argv", [
        "harness_onboard.py", "--key", KEY, "--spec", str(spec_path),
        "--build-only", "--install-timeout", "17",
    ])

    assert module.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scope"] == "build_only"
    assert result["runtime_acceptance"] == "not_run"
    assert events[-1] == "install"
    assert events.count("locked") == events.count("unlocked")
    assert not module.REPORT_DIR.exists()


@pytest.mark.parametrize("changes", [
    {"key": "__ut_another_key"},
    {"revision": "not-a-pinned-revision"},
])
def test_build_only_invalid_spec_never_installs(onboard, monkeypatch, tmp_path, changes):
    module, _ = onboard
    spec_path, _ = build_spec(tmp_path, **changes)
    monkeypatch.setattr(module.R, "install", lambda *a, **kw: pytest.fail("invalid spec installed"))
    monkeypatch.setattr(module.R, "_save_catalog", lambda *a: pytest.fail("invalid spec saved"))
    monkeypatch.setattr(sys, "argv", [
        "harness_onboard.py", "--key", KEY, "--spec", str(spec_path), "--build-only",
    ])
    with pytest.raises(ValueError):
        module.main()


@pytest.mark.parametrize("options", [
    ["--build-only", "--spec", "must-not-read.json"],
    ["--key", KEY, "--build-only", "--from-github", "https://github.com/example/fixture"],
    ["--key", KEY, "--build-only", "--skip-install"],
    ["--key", KEY, "--build-only", "--spec", "must-not-read.json", "--install-timeout", "0"],
])
def test_build_only_conflicts_still_reject_before_registration_or_install(onboard, monkeypatch, options):
    module, _ = onboard
    monkeypatch.setattr(module, "_upsert_catalog", lambda *a: pytest.fail("conflicting spec registered"))
    monkeypatch.setattr(module.R, "install", lambda *a, **kw: pytest.fail("conflicting flags installed"))
    monkeypatch.setattr(sys, "argv", ["harness_onboard.py", *options])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2


def test_build_only_spec_preserves_failed_build_without_runtime_acceptance(
    onboard, monkeypatch, tmp_path, capsys
):
    module, _ = onboard
    spec_path, _ = build_spec(tmp_path)
    calls = []
    monkeypatch.setattr(module, "_upsert_catalog", lambda *args: calls.append(args))
    monkeypatch.setattr(module.R, "install", lambda *a, **kw: {
        "ok": False, "status": "build_failed", "error": "synthetic build failure",
    })
    monkeypatch.setattr(sys, "argv", [
        "harness_onboard.py", "--key", KEY, "--spec", str(spec_path), "--build-only",
    ])
    assert module.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert calls == [(KEY, str(spec_path))]
    assert result["status"] == "build_failed"
    assert result["runtime_acceptance"] == "not_run"
    assert not module.REPORT_DIR.exists()
