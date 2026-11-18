"""Onboarding reports must distinguish absent distribution checks from failures.

Slow/member/provider boundaries are test doubles. The report branch and any
temporary mode-check script execute for real; this is not member acceptance.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
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


@pytest.fixture
def member_run(onboard, monkeypatch, tmp_path):
    import agent_tool_provider as provider
    import personal_runtime as runtime

    module, _ = onboard
    root = tmp_path / "personal"
    runtime.initialize(root, "fixture-owner")
    work = tmp_path / "member-work"
    work.mkdir()
    keyfile = tmp_path / "fixture-key"
    keyfile.write_text("fake-target-key")
    keyfile.chmod(0o600)
    runtime.save_model_profile(root, "target", "target-model", "https://target.example.invalid/v1", keyfile)
    keyfile.write_text("fake-default-key")
    runtime.save_model_profile(root, "default", "wrong-default-model", "https://default.example.invalid/v1", keyfile)
    runtime.activate_model_profile(root, "default")
    runtime.bind_member_model(root, "pi", "target", "responses")
    runtime.bind_member_model(root, "cc", "default", "anthropic-messages")
    env = {"MEYO_PERSONAL_STATE_DIR": str(root), "LEGION_PERSONAL_OWNER": "fixture-owner",
           "MEYO_WORKDIR": str(work), "MEYO_SESSION_ID": "fixture-session",
           "MEYO_DIRECTION": "research"}
    monkeypatch.syspath_prepend(str(SERVICE_DIR.parents[1] / "harness_baselines/meyo-orchestrator"))
    import harness_registry as registry
    cancel = lambda: False
    monkeypatch.setattr(module, "_native_onboard_lease", lambda: None)
    seen = {}

    def run(key, task, **kwargs):
        seen.update(key=key, task=task, **kwargs)
        return {"status": "ok", "final": "MOCK_ONLY"}

    monkeypatch.setattr(module.R, "run", run)
    with provider._runtime_scope(env, cancel, inherit_process_context=False):
        env = registry._INPROC_RUNTIME_ENV.values
        yield module, runtime, provider, root, work, env, registry, seen


@pytest.mark.parametrize("override", ["", "wrong-override", "target-model"])
def test_onboard_target_profile_overrides_model_and_default(member_run, override):
    module, _, _, _, _, _, _, seen = member_run
    assert module._run_agent("pi", "mock", timeout_s=5, model=override)["status"] == "ok"
    assert seen["model"] == "target-model"
    child = seen["env"]
    assert child["MEYO_UNIFIED_APPID"] == child["AIGC_APPID"] == child["OPENAI_API_KEY"] == "fake-target-key"
    assert child["MEYO_UNIFIED_GATEWAY"] == "https://target.example.invalid/v1"
    assert child["MEYO_MODEL_PROTOCOL"] == "responses"
    assert child["MEYO_DIRECTION"] == "research"
    assert child["MEYO_USER_ID"] == seen["runtime_context"]["user_id"] == "fixture-owner"
    assert "fake-default-key" not in json.dumps(child)
    assert set(json.loads(child["LEGION_MEMBER_MODEL_BINDINGS"])) == {"pi"}


@pytest.mark.parametrize("fault", ["missing", "bad_profile", "bad_protocol", "owner", "instance_owner"])
def test_onboard_invalid_binding_never_uses_default(member_run, fault):
    module, runtime, _, root, _, env, _, seen = member_run
    if fault == "missing":
        runtime.unbind_member_model(root, "pi")
    elif fault == "bad_profile":
        profiles = json.loads((root / "secrets/model-profiles.json").read_text())
        profiles["target"]["api_key"] = ""
        (root / "secrets/model-profiles.json").write_text(json.dumps(profiles))
    elif fault == "bad_protocol":
        choices = json.loads((root / "secrets/member-models.json").read_text())
        choices["pi"]["protocol"] = "wrong"
        (root / "secrets/member-models.json").write_text(json.dumps(choices))
    elif fault == "owner":
        env["MEYO_USER_ID"] = "forged-owner"
    else:
        env["LEGION_PERSONAL_OWNER"] = "forged-owner"
    with pytest.raises(ValueError):
        module._run_agent("pi", "mock", timeout_s=5, model="wrong-default-model")
    assert seen == {}


def test_onboard_lease_binding_and_narrow_parent_survive(member_run, monkeypatch):
    from types import SimpleNamespace
    module, _, provider, _, work, env, _, seen = member_run
    env["MEYO_USER_ID"] = "ignored-request-owner"
    env["MEYO_CAPABILITY_POLICY_JSON"] = json.dumps({"network": False, "install": True})
    lease = SimpleNamespace(user_id="fixture-owner", session_id="lease-session", workdir=work,
                            project_context={"marker": "parent-project"},
                            check=lambda: {"network": True, "install": True})
    monkeypatch.setattr(module, "_native_onboard_lease", lambda: lease)
    monkeypatch.setattr(provider, "_bound_personal_token", lambda: "fake-parent-token")

    module._run_agent("pi", "mock", timeout_s=5, model="wrong-parent-model")
    assert seen["model"] == "target-model"
    assert seen["session_id"] == "lease-session"
    context = seen["runtime_context"]
    assert context["user_id"] == "fixture-owner"
    assert context["capability_policy"]["network"] is False
    assert json.loads(seen["env"]["MEYO_CAPABILITY_POLICY_JSON"])["network"] is False
    assert context["project_context"] == {"marker": "parent-project", "workspace_root": str(work)}
    assert env["MEYO_USER_ID"] == "ignored-request-owner"  # Nested scope restored.


@pytest.mark.parametrize("inherit", [False, True])
def test_onboard_explicit_env_is_target_only(member_run, monkeypatch, inherit):
    module, _, _, _, _, env, registry, seen = member_run
    env.pop("MEYO_PERSONAL_STATE_DIR")
    env.update(MEYO_USER_ID="trusted-runtime-owner", LEGION_CODEX_TRANSPORT="responses")
    declared = {"pi": {"model": "env-target", "base_url": "https://target.example.invalid/v1",
                       "api_key_env": "TARGET_SLOT", "protocol": "openai-completions"},
                "cc": {"model": "foreign", "base_url": "https://foreign.example.invalid/v1",
                       "api_key_env": "FOREIGN_SLOT", "protocol": "anthropic-messages"}}
    monkeypatch.setenv("LEGION_MEMBER_MODEL_BINDINGS", json.dumps(declared))
    monkeypatch.setenv("TARGET_SLOT", "fake-env-target-key")
    monkeypatch.setenv("FOREIGN_SLOT", "fake-foreign-key")
    monkeypatch.setenv("MEYO_PERSONAL_MEMBER_KEY_99", "fake-other-profile-key")
    monkeypatch.setenv("MEYO_PERSONAL_PROVIDER_KEY", "fake-default-key")
    registry._INPROC_RUNTIME_ENV.inherit_process_context = inherit
    if not inherit:
        env.update(LEGION_MEMBER_MODEL_BINDINGS=json.dumps(declared), TARGET_SLOT="fake-env-target-key")
    module._run_agent("pi", "mock", timeout_s=5, model="wrong")
    child = seen["env"]
    assert seen["model"] == "env-target"
    assert child["MEYO_MODEL_PROTOCOL"] == "openai-completions"
    assert child["MEYO_USER_ID"] == "trusted-runtime-owner"
    assert not child["FOREIGN_SLOT"] and not child["MEYO_PERSONAL_MEMBER_KEY_99"]
    assert "fake-foreign-key" not in json.dumps(child)
    assert "fake-other-profile-key" not in json.dumps(child)
    assert "fake-default-key" not in json.dumps(child)
    assert set(json.loads(child["LEGION_MEMBER_MODEL_BINDINGS"])) == {"pi"}


@pytest.mark.parametrize("source", ["active_instance", "private_tls"])
@pytest.mark.parametrize("protocol", ["responses", "anthropic-messages", "openai-completions", "cursor-agent"])
def test_onboard_protocol_reaches_existing_registry_worker(member_run, monkeypatch, protocol, source):
    module, runtime, provider, root, work, env, _, _ = member_run
    if protocol == "cursor-agent":
        keyfile = work / "key-input"
        keyfile.write_text("fake-target-key")
        keyfile.chmod(0o600)
        runtime.save_model_profile(root, "cursor", "target-model", "https://api.cursor.com", keyfile)
    runtime.bind_member_model(root, "pi", "cursor" if protocol == "cursor-agent" else "target", protocol)
    if source == "private_tls":
        env.pop("MEYO_PERSONAL_STATE_DIR")
        env.update(MEYO_USER_ID="fixture-owner", TARGET_PRIVATE_SLOT="fake-target-key",
                   LEGION_CODEX_TRANSPORT="responses", LEGION_MEMBER_MODEL_BINDINGS=json.dumps({
                       "pi": {"model": "target-model", "api_key_env": "TARGET_PRIVATE_SLOT",
                              "base_url": "https://api.cursor.com" if protocol == "cursor-agent" else
                                          "https://target.example.invalid/v1", "protocol": protocol}}))
        assert "LEGION_MEMBER_MODEL_BINDINGS" not in provider._runtime_binding()[1]
    spec = importlib.util.spec_from_file_location("__ut_onboard_worker_registry", SERVICE_DIR / "external_agent_registry.py")
    external = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(external)
    entry = {"key": "pi", "transports": [{"type": "cli", "command": ["never-executed"]}]}
    monkeypatch.setattr(external, "load_catalog", lambda: [entry])
    monkeypatch.setattr(external, "_probe_one", lambda *a: {"execution_ready": True})
    monkeypatch.setattr(external, "_strip_dead_proxy", lambda env: env)
    monkeypatch.setattr(external, "_node_bin_dirs", lambda item: [])
    monkeypatch.setattr(external, "_context", lambda item, cwd, values: {"runtime_dir": str(work), **values})
    monkeypatch.setitem(sys.modules, "legion_sandbox", __import__("types").SimpleNamespace(
        prepare_peer_sources=lambda *a, **kw: ""))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-wrong-host-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "fake-wrong-host-key")
    monkeypatch.setenv("LEGION_MEMBER_MODEL_BINDINGS", json.dumps({"cc": {
        "model": "host-other", "base_url": "https://host.example.invalid/v1",
        "api_key_env": "HOST_OTHER_SLOT", "protocol": "responses"}}))
    monkeypatch.setenv("HOST_OTHER_SLOT", "fake-host-other-key")
    monkeypatch.setenv("ARBITRARY_UNLISTED_AUTH", "fake-unlisted-host-key")

    def transport(selected, context, child, timeout_s, **kwargs):
        assert context["model"] == "target-model" and context["appid"] == "fake-target-key"
        assert json.loads(context["runtime_context_json"])["user_id"] == "fixture-owner"
        assert child["MEYO_MODEL_PROTOCOL"] == protocol
        assert "LEGION_MEMBER_MODEL_BINDINGS" not in child
        assert not child["HOST_OTHER_SLOT"]
        assert "fake-host-other-key" not in json.dumps(child)
        assert not child["ARBITRARY_UNLISTED_AUTH"]
        assert "fake-unlisted-host-key" not in json.dumps(child)
        assert "fake-wrong-host-key" not in json.dumps(child)
        if protocol == "anthropic-messages":
            assert child["ANTHROPIC_AUTH_TOKEN"] == "fake-target-key"
        if protocol == "cursor-agent":
            assert child["CURSOR_API_KEY"] == "fake-target-key"
        return {"status": "ok", "final": "MOCK_TRANSPORT_ONLY"}

    monkeypatch.setattr(external, "run_transport", transport)
    monkeypatch.setattr(module, "R", external)
    result = module._run_agent("pi", "mock", timeout_s=5, model="wrong-override")
    assert result["final"] == "MOCK_TRANSPORT_ONLY"
    if source == "private_tls":
        with pytest.raises(ValueError):
            module._run_agent("cc", "counterexample", timeout_s=5, model="host-other")
        with provider._runtime_scope({"MEYO_USER_ID": "fixture-owner"}, None,
                                     inherit_process_context=False):
            with pytest.raises(ValueError):
                module._run_agent("pi", "missing-binding", timeout_s=5, model="host-other")


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
    # This host-identity case intentionally has a synthetic empty .git directory;
    # the separate real-Git fixtures below exercise strict local-source verification.
    monkeypatch.setattr(registry, "_verify_local_git_reuse", lambda *a: (True, ""))
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


def _local_install_case(tmp_path, monkeypatch, *, bundled=False):
    import external_agent_registry as registry

    source = tmp_path / "source"
    source.mkdir()
    revision = "a" * 40
    git = None
    if bundled:
        payload = b"upstream\n"
        (source / "tracked.txt").write_bytes(payload)
        (source / ".meyo-source.json").write_text(json.dumps({
            "revision": revision,
            "files": {"tracked.txt": {"sha256": hashlib.sha256(payload).hexdigest()}},
        }))
    else:
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull,
               "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"}
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                     "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
            env.pop(name, None)
        subprocess.run(["git", "init", "--quiet", str(source)], check=True,
                       capture_output=True, text=True, env=env, timeout=10)

        def git_command(*args):
            return subprocess.run(
                ["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(source), *args],
                check=True, capture_output=True, text=True, env=env, timeout=10)

        git = git_command
        git("config", "user.name", "Install Fixture")
        git("config", "user.email", "fixture@example.invalid")
        (source / "tracked.txt").write_text("upstream\n")
        (source / ".gitignore").write_text("*.ignored\n")
        git("add", "tracked.txt", ".gitignore")
        git("commit", "--quiet", "-m", "pinned upstream")
        revision = git("rev-parse", "HEAD").stdout.strip()
        git("remote", "add", "origin", "https://github.com/example/fixture.git")

    item = {
        "key": "__ut_local_git_reuse", "name": "Local install fixture", "kind": "agent",
        "url": "https://github.com/example/fixture", "version": "test", "revision": revision,
        "install": [[sys.executable, "-c", "dependency_sentinel = True"]],
        "probe": {"method": "path", "target": "{source}/tracked.txt"},
    }
    state = tmp_path / "install-state"
    monkeypatch.setenv("LEGION_EXTERNAL_RUNTIME_ROOT", str(tmp_path / "runtimes"))
    monkeypatch.delenv("MEYO_RUNTIME_STORE", raising=False)
    monkeypatch.delenv("MEYO_RUNTIME_ROOT", raising=False)
    monkeypatch.setattr(registry, "_STATE_DIR", state)
    monkeypatch.setattr(registry, "_STATE_PATH", state / "state.json")
    monkeypatch.setattr(registry, "_LOG_DIR", state / "logs")
    monkeypatch.setattr(registry, "_native_onboard_lease", lambda: None)
    monkeypatch.setattr(registry, "load_catalog", lambda: [item])
    monkeypatch.setattr(registry, "source_dir", lambda _: source)
    steps, probes, git_commands = [], [], []

    def install_step(command, *_args, **_kwargs):
        steps.append(command)
        return {"command": command, "exit_code": 0, "stdout": "", "stderr": "",
                "timed_out": False, "log_path": str(tmp_path / "dependency.log")}

    monkeypatch.setattr(registry, "_run_install_step", install_step)
    real_probe_one = registry._probe_one
    monkeypatch.setattr(registry, "_probe_one", lambda *a, **k: probes.append(True) or {
        "available": True, "revision_ok": True, "status": "ready", "status_detail": ""})
    real_probe_command = registry._run_probe_command

    def observe_git(command, *args, **kwargs):
        if "git" in command:
            git_commands.append(command)
        return real_probe_command(command, *args, **kwargs)

    monkeypatch.setattr(registry, "_run_probe_command", observe_git)
    return {"registry": registry, "item": item, "source": source, "git": git,
            "steps": steps, "probes": probes, "git_commands": git_commands,
            "real_probe_one": real_probe_one}


def test_verified_local_git_reuse_skips_only_source_sync(tmp_path, monkeypatch):
    case = _local_install_case(tmp_path, monkeypatch)
    result = case["registry"].install(case["item"]["key"], confirm=True, timeout_s=5)

    assert result["ok"] is True and result["source_preserved"] is True
    assert len(case["steps"]) == 1 and "dependency_sentinel" in case["steps"][0][2]
    assert case["probes"]
    assert case["git_commands"]
    assert all("GIT_NO_LAZY_FETCH=1" in command for command in case["git_commands"])
    assert all("core.fsmonitor=false" in command for command in case["git_commands"])
    assert not any({"clone", "fetch", "checkout"} & set(command)
                   for command in case["git_commands"] + case["steps"])


@pytest.mark.parametrize("fault", [
    "dirty", "staged", "untracked", "ignored", "origin", "wrong_revision", "git_symlink",
])
def test_unverified_local_git_is_preserved_without_dependency_steps(tmp_path, monkeypatch, fault):
    case = _local_install_case(tmp_path, monkeypatch)
    source, git = case["source"], case["git"]
    if fault in {"dirty", "staged", "wrong_revision"}:
        (source / "tracked.txt").write_text(fault + "\n")
        if fault in {"staged", "wrong_revision"}:
            git("add", "tracked.txt")
        if fault == "wrong_revision":
            git("commit", "--quiet", "-m", "different local revision")
    elif fault == "untracked":
        (source / "untracked.txt").write_text("preserve me")
    elif fault == "ignored":
        (source / "local.ignored").write_text("preserve me")
    elif fault == "origin":
        git("remote", "set-url", "origin", "https://github.com/example/wrong.git")
    else:
        (source / ".git").rename(source / "git-metadata")
        (source / ".git").symlink_to(source / "git-metadata", target_is_directory=True)
    before = (source / "tracked.txt").read_text()

    result = case["registry"].install(case["item"]["key"], confirm=True, timeout_s=5)

    assert result["ok"] is False and result["status"] == "requires_independent_source_upgrade"
    assert result["source_preserved"] is True and case["steps"] == []
    assert case["probes"] == [] and result["runtime_acceptance"] == "not_run"
    assert (source / "tracked.txt").read_text() == before
    assert (source / ".git").exists()
    if fault == "wrong_revision":
        assert "HEAD does not equal" in result["error"]
    elif fault == "origin":
        assert "origin" in result["error"]
    elif fault == "git_symlink":
        assert "non-symlink" in result["error"]
    else:
        assert "HEAD/index/worktree" in result["error"]


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
@pytest.mark.parametrize("long_listing", [False, True])
def test_hidden_tracked_changes_reject_reuse_without_truncating_index(
    tmp_path, monkeypatch, flag, long_listing,
):
    case = _local_install_case(tmp_path, monkeypatch)
    source, git = case["source"], case["git"]
    if long_listing:
        for index in range(150):
            (source / f"zzz-padding-{index:04d}.txt").write_text("clean\n")
        git("add", ".")
        git("commit", "--quiet", "-m", "long tracked listing")
        case["item"]["revision"] = git("rev-parse", "HEAD").stdout.strip()
    git("update-index", flag, "tracked.txt")
    (source / "tracked.txt").write_text("real dirty source hidden from status\n")
    assert git("status", "--porcelain=v1").stdout == ""
    listing = git("ls-files", "-v", "-z").stdout
    prefix = "h" if flag == "--assume-unchanged" else "S"
    dangerous = prefix + " tracked.txt\0"
    assert dangerous in listing
    if long_listing:
        assert len(listing) > 2000 and dangerous not in listing[-2000:]
    index_before = (source / ".git/index").read_bytes()

    result = case["registry"].install(case["item"]["key"], confirm=True, timeout_s=5)

    assert result["ok"] is False and result["source_preserved"] is True
    assert "assume-unchanged or skip-worktree" in result["error"]
    assert case["steps"] == [] and case["probes"] == []
    assert (source / "tracked.txt").read_text() == "real dirty source hidden from status\n"
    assert (source / ".git/index").read_bytes() == index_before
    assert git("ls-files", "-v", "-z").stdout == listing


def test_probe_output_limit_defaults_to_tail_and_allows_private_full_output(tmp_path, monkeypatch):
    import external_agent_registry as registry

    monkeypatch.setattr(registry, "_native_onboard_lease", lambda: None)
    command = [sys.executable, "-c", "print('prefix-' + 'x' * 2500, end='')"]
    ok, output = registry._run_probe_command(command, tmp_path, timeout_s=5)
    assert ok and output == "x" * 2000
    ok, output = registry._run_probe_command(command, tmp_path, timeout_s=5, output_limit=None)
    assert ok and output == "prefix-" + "x" * 2500


def test_bundled_source_still_skips_git_and_runs_dependencies(tmp_path, monkeypatch):
    case = _local_install_case(tmp_path, monkeypatch, bundled=True)
    marker = (case["source"] / ".meyo-source.json").read_text()

    result = case["registry"].install(case["item"]["key"], confirm=True, timeout_s=5)

    assert result["ok"] is True and len(case["steps"]) == 1
    assert case["git_commands"] == []
    assert (case["source"] / ".meyo-source.json").read_text() == marker


@pytest.mark.parametrize("fault", ["replace", "alternates", "grafts", "stat_cache", "symlink", "hardlink"])
def test_local_reuse_checks_actual_bytes_and_independent_objects(tmp_path, monkeypatch, fault):
    case = _local_install_case(tmp_path, monkeypatch)
    source, git = case["source"], case["git"]
    tracked = source / "tracked.txt"
    if fault == "replace":
        original = case["item"]["revision"]
        tracked.write_text("replacement\n")
        git("add", ".")
        git("commit", "--quiet", "-m", "replacement tree")
        replacement = git("rev-parse", "HEAD").stdout.strip()
        git("update-ref", "HEAD", original)
        git("replace", original, replacement)
        assert git("status", "--porcelain=v1").stdout == ""
    elif fault == "alternates":
        (source / ".git/objects/info/alternates").write_text(str(tmp_path / "external-objects") + "\n")
    elif fault == "grafts":
        (source / ".git/info/grafts").write_text(case["item"]["revision"] + "\n")
    elif fault == "stat_cache":
        git("update-index", "--refresh")
        info = tracked.stat()
        tracked.write_bytes(b"tampered\n")  # Same size, restored timestamp; index is untouched.
        os.utime(tracked, ns=(info.st_atime_ns, info.st_mtime_ns))
    elif fault == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("upstream\n")
        tracked.unlink()
        tracked.symlink_to(outside)
    else:
        os.link(tracked, tmp_path / "outside-link")
    index_before = (source / ".git/index").read_bytes()
    result = case["registry"].install(case["item"]["key"], confirm=True, timeout_s=5)
    assert not result["ok"] and result["status"] == "requires_independent_source_upgrade"
    assert case["steps"] == case["probes"] == []
    assert (source / ".git/index").read_bytes() == index_before
    if fault in {"replace", "alternates", "grafts"}:
        assert fault in result["error"]
    else:
        assert "tracked" in result["error"]


def _upgrade_fixture(case):
    git, source = case["git"], case["source"]
    old = case["item"]["revision"]
    (source / "tracked.txt").write_text("new upstream\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "new upstream")
    new = git("rev-parse", "HEAD").stdout.strip()
    git("checkout", "--detach", old)
    return old, new


@pytest.mark.parametrize("fetch_failure", [False, True])
def test_managed_revision_upgrade_preserves_repository_and_uses_sync(tmp_path, monkeypatch, fetch_failure):
    case = _local_install_case(tmp_path, monkeypatch)
    registry, item = case["registry"], case["item"]
    old, new = _upgrade_fixture(case)
    registry._write_state(item["key"], {"source": str(case["source"]), "source_revision": old})
    item["revision"] = new
    sentinel = case["source"] / ".git/preserved-local-history"
    sentinel.write_text("never delete\n")

    def sync(command, *_args):
        case["steps"].append(command)
        if "fetch" in command and fetch_failure:
            return dict(exit_code=1, timed_out=False, stdout="TLS (-110)", stderr="")
        if "checkout" in command:
            case["git"]("checkout", "--detach", new)
        return dict(exit_code=0, timed_out=False, stdout="", stderr="")

    monkeypatch.setattr(registry, "_run_install_step", sync)
    result = registry.install(item["key"], confirm=True, timeout_s=5)
    assert result["ok"] is not fetch_failure
    assert sentinel.read_text() == "never delete\n"
    assert "fetch" in case["steps"][0] and not any("clone" in c for c in case["steps"])
    assert case["git"]("rev-parse", "HEAD").stdout.strip() == (old if fetch_failure else new)
    if fetch_failure:
        assert case["probes"] == [] and len(case["steps"]) == 1
    else:
        assert "checkout" in case["steps"][1] and len(case["steps"]) == 3
        assert registry._read_state()[item["key"]]["source_revision"] == new


def test_ignored_postbuild_residue_is_not_laundered_by_install_state(tmp_path, monkeypatch):
    case = _local_install_case(tmp_path, monkeypatch)
    registry, item = case["registry"], case["item"]
    assert registry.install(item["key"], confirm=True, timeout_s=5)["ok"]
    (case["source"] / "build.ignored").write_text("legitimate but not independently verified")
    case["steps"].clear()
    case["probes"].clear()
    result = registry.install(item["key"], confirm=True, timeout_s=5)
    assert result["status"] == "requires_independent_source_upgrade" and not result["ok"]
    assert case["steps"] == case["probes"] == []
    assert (case["source"] / "build.ignored").exists()


def test_apply_update_admits_verified_old_source_through_existing_interface(tmp_path, monkeypatch):
    case = _local_install_case(tmp_path, monkeypatch)
    registry, item = case["registry"], case["item"]
    old, new = _upgrade_fixture(case)
    item["update"] = {"strategy": "github_commit"}
    monkeypatch.setattr(registry, "catalog_lock", __import__("contextlib").nullcontext)
    monkeypatch.setattr(registry, "_save_catalog", lambda rows: item.update(rows[0]))

    def sync(command, *_args):
        case["steps"].append(command)
        if "checkout" in command:
            case["git"]("checkout", "--detach", new)
        return dict(exit_code=0, timed_out=False, stdout="", stderr="")

    monkeypatch.setattr(registry, "_run_install_step", sync)
    result = registry.apply_update(item["key"], target_tag="new", target_revision=new, timeout_s=5)
    assert result["status"] == "updated" and result["ok"]
    assert result["from"]["revision"] == old and item["revision"] == new
    assert "fetch" in case["steps"][0] and "checkout" in case["steps"][1]


def test_apply_update_dirty_source_does_not_change_catalog_or_reinstall(tmp_path, monkeypatch):
    case = _local_install_case(tmp_path, monkeypatch)
    registry, item = case["registry"], case["item"]
    item["update"] = {"strategy": "github_commit"}
    (case["source"] / "tracked.txt").write_text("dirty\n")
    before = json.dumps(item, sort_keys=True)
    result = registry.apply_update(item["key"], target_tag="new", target_revision="b" * 40, timeout_s=5)
    assert not result["ok"] and result["status"] == "requires_independent_source_upgrade"
    assert json.dumps(item, sort_keys=True) == before
    assert case["steps"] == case["probes"] == []


@pytest.mark.parametrize("caller_uid", [999, os.getuid()])
def test_source_owner_uses_existing_operator_boundary_not_fixed_uid_blacklist(tmp_path, monkeypatch, caller_uid):
    case = _local_install_case(tmp_path, monkeypatch)
    original_stat = Path.stat

    def owned_stat(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path.is_relative_to(case["source"]):
            values = list(info)
            values[4] = 999
            return os.stat_result(values)
        return info

    monkeypatch.setattr(Path, "stat", owned_stat)
    monkeypatch.setattr(os, "getuid", lambda: caller_uid)
    result = case["registry"].install(case["item"]["key"], confirm=True, timeout_s=5)
    assert result["ok"] == (caller_uid == 999)
    if caller_uid != 999:
        assert "host trust boundary" in result["error"]
        assert case["steps"] == case["probes"] == []


def test_dependency_failure_never_runs_probe(tmp_path, monkeypatch):
    case = _local_install_case(tmp_path, monkeypatch)
    monkeypatch.setattr(case["registry"], "_run_install_step", lambda *a: dict(
        exit_code=1, timed_out=False, stdout="dependency failed", stderr=""))
    result = case["registry"].install(case["item"]["key"], confirm=True, timeout_s=5)
    assert not result["ok"] and result["status"] == "build_failed"
    assert case["probes"] == []


def test_reuse_keeps_real_install_subprocess_and_probe(tmp_path, monkeypatch):
    case = _local_install_case(tmp_path, monkeypatch)
    registry, item = case["registry"], case["item"]
    # No package/network work: exercise the production spawn/registry/lock and
    # probe plumbing with a real isolated Python sentinel.
    sentinel = tmp_path / "dependency-ran"
    item["execution_role"] = "capability"  # Build/path acceptance, not a fake answer transport.
    item["probe"] = {"method": "command", "command": [sys.executable, "-I", "-c",
                     f"from pathlib import Path; assert Path({str(sentinel)!r}).read_text() == 'built'"]}
    item["install"] = [[sys.executable, "-I", "-c",
                        f"from pathlib import Path; Path({str(sentinel)!r}).write_text('built')"]]
    monkeypatch.setenv("LEGION_INSTALL_LOCK_FILE", str(tmp_path / "install.lock"))
    monkeypatch.setattr(registry, "_run_install_step", registry._run_install_step_locked)
    monkeypatch.setattr(registry, "_host_probe_trusted", lambda _: True)
    monkeypatch.setattr(registry, "_probe_one", case["real_probe_one"])
    result = registry.install(item["key"], confirm=True, timeout_s=5)
    assert result["ok"], result
    assert sentinel.read_text() == "built"
    assert result["probe"]["revision_ok"] and result["probe"]["available"]


def test_leased_source_reuses_existing_path_authority_not_host_owner_rule(tmp_path, monkeypatch):
    from types import SimpleNamespace
    case = _local_install_case(tmp_path, monkeypatch)
    registry = case["registry"]
    checks = []
    lease = SimpleNamespace(check=lambda: checks.append("live"),
                            install_paths=lambda *args: checks.append("paths"))
    monkeypatch.setattr(registry, "_native_onboard_lease", lambda: lease)
    real_command = registry._run_probe_command

    def host_git_only_for_fixture(command, *args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(registry, "_native_onboard_lease", lambda: None)
            return real_command(command, *args, **kwargs)

    monkeypatch.setattr(registry, "_run_probe_command", host_git_only_for_fixture)
    import native_process_sandbox
    monkeypatch.setattr(native_process_sandbox, "_trusted", lambda *a: pytest.fail(
        "leased path must not inherit the host-owner rule"))
    result = registry.install(case["item"]["key"], confirm=True, timeout_s=5)
    assert result["ok"] and "paths" in checks


def test_apply_update_rejection_restores_catalog_without_second_install(tmp_path, monkeypatch):
    import copy
    import contextlib
    case = _local_install_case(tmp_path, monkeypatch)
    registry = case["registry"]
    case["item"]["update"] = {"strategy": "github_commit"}
    rows = [copy.deepcopy(case["item"])]
    before = copy.deepcopy(rows)
    monkeypatch.setattr(registry, "load_catalog", lambda: copy.deepcopy(rows))
    monkeypatch.setattr(registry, "catalog_lock", contextlib.nullcontext)
    monkeypatch.setattr(registry, "_save_catalog", lambda value: rows.__setitem__(slice(None), copy.deepcopy(value)))
    calls = []

    def rejected(key, **kwargs):
        calls.append(key)
        return dict(ok=False, status="requires_independent_source_upgrade",
                    error="requires independent source upgrade", source_preserved=True,
                    runtime_acceptance="not_run")

    monkeypatch.setattr(registry, "install", rejected)
    result = registry.apply_update(case["item"]["key"], target_tag="new", target_revision="b" * 40, timeout_s=5)
    assert result["status"] == "requires_independent_source_upgrade" and result["catalog_restored"]
    assert rows == before and calls == [case["item"]["key"]]
    assert case["probes"] == []
