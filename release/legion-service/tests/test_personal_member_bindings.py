"""Offline regressions for per-member model bindings: temporary instances, fake keys only."""
import copy
import importlib
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCHESTRATOR = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
MEMBERS = "secrets/member-models.json"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    for name in ("HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        folder = tmp_path / name.lower()
        folder.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(folder))
    for name in ("LEGION_MEMBER_MODEL_BINDINGS", "LEGION_CODEX_TRANSPORT",
                 "LEGION_CODEX_RESPONSES_BINDING", "LEGION_CCR_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    # Adapters copy the process environment; keep the developer's own provider
    # settings out of these fixtures so a failure can never print them.
    import os
    for name in list(os.environ):
        if any(mark in name.upper() for mark in ("KEY", "TOKEN", "SECRET", "PASSWORD", "APPID")):
            monkeypatch.delenv(name, raising=False)

    def offline(*args, **kwargs):
        raise AssertionError("This regression must not access the network")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket.socket, "connect_ex", offline)
    monkeypatch.setattr(socket, "getaddrinfo", offline)


@pytest.fixture
def runtime():
    return importlib.import_module("personal_runtime")


@pytest.fixture
def registry():
    return importlib.import_module("model_registry")


@pytest.fixture
def root(runtime, tmp_path):
    path = tmp_path / "instance"
    runtime.initialize(path, "member-test", 18080)
    return path


def key_file(tmp_path, name):
    path = tmp_path / f"{name}.key"
    path.write_text(f"fake-member-key-{name}\n")
    path.chmod(0o600)
    return path


def save(runtime, root, tmp_path, name, base_url):
    return runtime.save_model_profile(root, name, f"{name}-fixture", base_url, key_file(tmp_path, name))


def bound_instance(runtime, root, tmp_path):
    runtime.configure_model(root, "codex-fixture", "https://codex.example.invalid/v1",
                            key_file(tmp_path, "codex"))
    save(runtime, root, tmp_path, "claude", "https://claude.example.invalid/anthropic")
    save(runtime, root, tmp_path, "gemini", "https://gemini.example.invalid/openai/v1")
    runtime.bind_member_model(root, "cc", "claude", "anthropic-messages")
    runtime.bind_member_model(root, "pi", "gemini", "openai-completions")
    return runtime.environment(root, inherited={})[0]


def test_binding_names_a_candidate_and_never_stores_or_returns_a_key(runtime, root, tmp_path):
    save(runtime, root, tmp_path, "claude", "https://claude.example.invalid/anthropic")
    result = runtime.bind_member_model(root, "cc", "claude", "anthropic-messages")
    assert result["bound"] and result["protocol"] == "anthropic-messages"
    assert not result["provider_verified"]
    stored = (root / MEMBERS).read_text()
    assert json.loads(stored) == {"cc": {"profile": "claude", "protocol": "anthropic-messages"}}
    listed = runtime.list_member_models(root)
    assert listed["members"] == [{
        "member": "cc", "id": "claude", "protocol": "anthropic-messages",
        "model": "claude-fixture", "base_url": "https://claude.example.invalid/anthropic",
        "candidate_present": True}]
    assert "fake-member-key-claude" not in stored + json.dumps(result) + json.dumps(listed)
    assert (root / MEMBERS).stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("member,profile,protocol", [
    ("codex", "claude", "responses"),          # Codex keeps its own single binding.
    ("CC", "claude", "anthropic-messages"),
    ("../cc", "claude", "anthropic-messages"),
    ("cc", "missing", "anthropic-messages"),
    ("cc", "claude", "chat"),
])
def test_invalid_member_binding_changes_nothing(runtime, root, tmp_path, member, profile, protocol):
    save(runtime, root, tmp_path, "claude", "https://claude.example.invalid/anthropic")
    with pytest.raises(ValueError):
        runtime.bind_member_model(root, member, profile, protocol)
    assert not (root / MEMBERS).exists()


def test_environment_gives_each_member_only_its_own_declaration(runtime, registry, root, tmp_path):
    env = bound_instance(runtime, root, tmp_path)
    declared = json.loads(env[runtime.MEMBER_BINDINGS_ENV])
    assert set(declared) == {"cc", "pi"}
    assert "fake-member-key" not in env[runtime.MEMBER_BINDINGS_ENV]
    bindings = registry.explicit_member_bindings(env)
    assert bindings["cc"] == {
        "model": "claude-fixture", "base_url": "https://claude.example.invalid/anthropic",
        "api_key": "fake-member-key-claude", "protocol": "anthropic-messages"}
    assert bindings["pi"]["api_key"] == "fake-member-key-gemini"
    assert bindings["pi"]["protocol"] == "openai-completions"
    # Codex's own binding is unchanged and separate.
    assert registry.explicit_responses_binding(env)["api_key"] == "fake-member-key-codex"
    assert "codex" not in bindings


def test_unbind_removes_only_that_member(runtime, root, tmp_path):
    bound_instance(runtime, root, tmp_path)
    assert runtime.unbind_member_model(root, "cc")["unbound"]
    assert not runtime.unbind_member_model(root, "cc")["unbound"]
    env = runtime.environment(root, inherited={})[0]
    assert set(json.loads(env[runtime.MEMBER_BINDINGS_ENV])) == {"pi"}


@pytest.mark.parametrize("declared", [
    "[]", "{", json.dumps({"codex": {}}),
    json.dumps({"cc": {"model": "m", "base_url": "https://x.example.invalid", "api_key_env": "K"}}),
    json.dumps({"cc": {"model": "m", "base_url": "http://x.example.invalid",
                       "api_key_env": "K", "protocol": "anthropic-messages"}}),
    json.dumps({"cc": {"model": "m", "base_url": "https://x.example.invalid",
                       "api_key_env": "MISSING", "protocol": "anthropic-messages"}}),
])
def test_broken_declaration_is_an_error_not_a_fallback(registry, declared):
    with pytest.raises(ValueError):
        registry.explicit_member_bindings({"LEGION_MEMBER_MODEL_BINDINGS": declared, "K": "fake-key"})


def test_status_and_execution_binding_are_scoped_to_the_member(
        runtime, registry, root, tmp_path, monkeypatch):
    env = bound_instance(runtime, root, tmp_path)
    for name in ("LEGION_CODEX_TRANSPORT", "LEGION_CODEX_RESPONSES_BINDING",
                 runtime.MEMBER_BINDINGS_ENV, runtime.KEY_ENV,
                 *[name for name in env if name.startswith(runtime.MEMBER_KEY_ENV)]):
        monkeypatch.setenv(name, env[name])
    status = registry.explicit_responses_status("cc")
    assert status["configured"] and status["model"] == "claude-fixture"
    assert status["harnesses"] == ["cc"] and status["protocol"] == "anthropic-messages"
    assert "api_key" not in status and "fake-member-key" not in json.dumps(status)
    assert registry.explicit_responses_status("hermes") is None
    assert registry.explicit_responses_status("codex")["model"] == "codex-fixture"
    rows = registry.list_models("programming", harness_key="pi")["candidates"]
    assert [row["model"] for row in rows] == ["gemini-fixture"]

    core = importlib.import_module("legion_core")
    assert core._explicit_provider_binding("cc")["protocol"] == "anthropic-messages"
    assert core._explicit_provider_binding("cc")["api_key"] == "fake-member-key-claude"
    assert core._explicit_provider_binding("hermes") is None
    assert "protocol" not in core._explicit_provider_binding("codex")
    assert core._binding_protocol(core._explicit_provider_binding("codex")) == "responses"
    assert core._binding_protocol(core._explicit_provider_binding("pi")) == "openai-completions"


def test_installed_but_unbound_members_stay_out_of_the_deployment_pool(
        runtime, root, tmp_path, monkeypatch):
    env = bound_instance(runtime, root, tmp_path)
    core = importlib.import_module("legion_core")
    clis = {"codex": True, "cc": True, "pi": True, "hermes": True, "jcode": False}
    # Outside a deployment instance nothing is filtered.
    monkeypatch.setattr(core, "_personal_responses_planner_enabled", lambda: False)
    assert core._deployment_unbound_members(clis) == []
    monkeypatch.setattr(core, "_personal_responses_planner_enabled", lambda: True)
    for name in ("LEGION_CODEX_TRANSPORT", "LEGION_CODEX_RESPONSES_BINDING",
                 runtime.MEMBER_BINDINGS_ENV, runtime.KEY_ENV,
                 *[name for name in env if name.startswith(runtime.MEMBER_KEY_ENV)]):
        monkeypatch.setenv(name, env[name])
    assert core._deployment_unbound_members(clis) == ["hermes"]


def test_claude_code_member_binding_connects_directly_with_only_its_own_key(
        runtime, root, tmp_path, monkeypatch):
    env = bound_instance(runtime, root, tmp_path)
    for name, value in env.items():
        if name.startswith(("MEYO_PERSONAL_", "LEGION_CODEX_", "LEGION_MEMBER_")):
            monkeypatch.setenv(name, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-inherited-direct-key")
    monkeypatch.syspath_prepend(str(ORCHESTRATOR))
    registry = importlib.import_module("harness_registry")
    seen = {}

    class StopBeforeNative(Exception):
        """Raised without arguments so no command or environment reaches test output."""

    def fake_run_live(cmd, timeout, env=None, cwd=None, input=None, tag="", **kwargs):
        seen["cmd"], seen["env"] = list(cmd), dict(env or {})
        raise StopBeforeNative()

    monkeypatch.setattr(registry, "_run_live", fake_run_live)
    workdir = tmp_path / "work"
    workdir.mkdir()
    ctx = registry.HarnessCtx(
        model="claude-fixture", appid="fake-member-key-claude",
        gateway="https://claude.example.invalid/anthropic/", session_id="cc:member-binding",
        workdir=str(workdir), model_protocol="anthropic-messages")
    with pytest.raises(StopBeforeNative):
        registry._cc_adapter("member-binding", "只回复 OK", ctx=ctx)
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == "claude-fixture"
    child = seen["env"]
    assert child["ANTHROPIC_BASE_URL"] == "https://claude.example.invalid/anthropic"
    assert child["ANTHROPIC_AUTH_TOKEN"] == "fake-member-key-claude"
    assert "ANTHROPIC_API_KEY" not in child
    leaked = [name for name in child if name.startswith("MEYO_PERSONAL_MEMBER_KEY_")
              or name in ("MEYO_PERSONAL_PROVIDER_KEY", "LEGION_CODEX_RESPONSES_BINDING",
                          "LEGION_MEMBER_MODEL_BINDINGS")]
    assert leaked == []


def test_claude_code_incomplete_member_binding_stops_before_native(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ORCHESTRATOR))
    registry = importlib.import_module("harness_registry")
    monkeypatch.setattr(registry, "_run_live", lambda *a, **k: pytest.fail("native CLI must not start"))
    ctx = registry.HarnessCtx(model="claude-fixture", appid="", gateway="https://claude.example.invalid/anthropic",
                              session_id="cc:incomplete", workdir=str(tmp_path),
                              model_protocol="anthropic-messages")
    result = registry._cc_adapter("incomplete", "只回复 OK", ctx=ctx)
    assert result["status"] == "error" and result["fail_kind"] == "model_config"


def member_environment(runtime, root, tmp_path, monkeypatch):
    env = bound_instance(runtime, root, tmp_path)
    for name in ("LEGION_CODEX_TRANSPORT", "LEGION_CODEX_RESPONSES_BINDING",
                 runtime.MEMBER_BINDINGS_ENV, runtime.KEY_ENV,
                 *[name for name in env if name.startswith(runtime.MEMBER_KEY_ENV)]):
        monkeypatch.setenv(name, env[name])
    return env


def test_external_member_binding_counts_as_its_credential(runtime, root, tmp_path, monkeypatch):
    member_environment(runtime, root, tmp_path, monkeypatch)
    external = importlib.import_module("external_agent_registry")
    catalog = json.loads((SERVICE / "external_agents.json").read_text())["agents"]
    pi = next(entry for entry in catalog if entry["key"] == "pi")
    other = next(entry for entry in catalog if entry["key"] == "opencode")
    bound = external._credentials(pi)
    assert bound["ready"] and bound["source"] == "member_binding" and bound["missing"] == []
    # An installed member without its own binding still reports the missing credential.
    assert not external._credentials(other)["ready"]


def test_external_member_runs_with_declared_protocol_and_only_its_own_key(
        runtime, root, tmp_path, monkeypatch):
    member_environment(runtime, root, tmp_path, monkeypatch)
    external = importlib.import_module("external_agent_registry")
    catalog = json.loads((SERVICE / "external_agents.json").read_text())["agents"]
    item = next(entry for entry in catalog if entry["key"] == "pi")
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(external, "load_catalog", lambda: [copy.deepcopy(item)])
    monkeypatch.setattr(external, "_probe_one", lambda *a, **kw: {
        "execution_ready": True, "status": "ready", "status_detail": ""})
    monkeypatch.setattr(external, "source_dir", lambda entry: source)
    monkeypatch.setattr(external, "_deployment_paths", lambda entry: None)
    monkeypatch.setattr(external, "_node_bin_dirs", lambda entry: [])
    monkeypatch.setitem(sys.modules, "legion_sandbox", SimpleNamespace(
        prepare_peer_sources=lambda *a, **kw: ""))
    seen = {}

    def transport(spec, values, environment, timeout_s, event_cb=None):
        agent_dir = Path(environment["PI_CODING_AGENT_DIR"])
        seen["models"] = json.loads((agent_dir / "models.json").read_text())
        seen["env"] = dict(environment)
        return {"status": "ok", "final": "SYNTHETIC_TRANSPORT_ONLY"}

    monkeypatch.setattr(external, "run_transport", transport)
    work = tmp_path / "pi-work"
    work.mkdir()
    result = external.run(
        "pi", "Synthetic member binding check", workdir=str(work), model="gemini-fixture",
        session_id="member-binding", timeout_s=5,
        env={"MEYO_UNIFIED_APPID": "fake-member-key-gemini", "AIGC_APPID": "fake-member-key-gemini",
             "MEYO_UNIFIED_GATEWAY": "https://gemini.example.invalid/openai/v1",
             "MEYO_MODEL_PROTOCOL": "responses"},
        runtime_context={"user_id": "fixture-owner"})
    assert result["status"] == "ok", result
    provider = seen["models"]["providers"]["meyo"]
    assert provider["api"] == "openai-responses" and provider["models"][0]["api"] == "openai-responses"
    assert provider["baseUrl"] == "https://gemini.example.invalid/openai/v1"
    child = seen["env"]
    leaked = [name for name in child if name.startswith("MEYO_PERSONAL_MEMBER_KEY_")
              or name in ("MEYO_PERSONAL_PROVIDER_KEY", "LEGION_CODEX_RESPONSES_BINDING",
                          "LEGION_MEMBER_MODEL_BINDINGS")]
    assert leaked == []
    assert "fake-member-key-codex" not in json.dumps(child)
    assert "fake-member-key-claude" not in json.dumps(child)


def union_core(runtime, root, tmp_path, monkeypatch, runnable):
    member_environment(runtime, root, tmp_path, monkeypatch)
    core = importlib.import_module("legion_core")
    monkeypatch.setattr(core, "_personal_responses_planner_enabled", lambda: True)
    monkeypatch.setattr(core, "_harness_keys", lambda force=False: {"codex", "cc", "pi", "hermes"})
    monkeypatch.setattr(core, "_aigc_ready", lambda *a, **k: {"runnable_clis": list(runnable)})
    monkeypatch.delenv("LEGION_AUTO_UNION_AUTHORS", raising=False)
    return core


def test_auto_union_takes_members_bound_to_different_models(runtime, root, tmp_path, monkeypatch):
    core = union_core(runtime, root, tmp_path, monkeypatch, ["codex", "cc", "pi"])
    # Two authors by default; the third runnable member stays free to audit.
    assert core.auto_union_authors({}) == ["codex", "cc"]
    assert core.auto_union_authors({}, {"cc", "pi"}) == ["cc", "pi"]
    monkeypatch.setenv("LEGION_AUTO_UNION_AUTHORS", "3")
    assert core.auto_union_authors({}) == ["codex", "cc", "pi"]


@pytest.mark.parametrize("setting,plan,runnable,enabled", [
    ("1", {}, ["codex", "cc", "pi"], True),                              # switched off
    ("2", {"team_policy": {"max_agents": 1}}, ["codex", "cc"], True),    # caller caps the team
    ("2", {"solo": True}, ["codex", "cc"], True),
    ("2", {}, ["codex"], True),                                          # nobody to be different from
    ("2", {}, ["codex", "hermes"], True),                                # hermes has no model of its own
    ("2", {}, ["codex", "cc", "pi"], False),                             # not a deployment instance
])
def test_auto_union_keeps_a_single_author_when_there_is_no_real_heterogeneity(
        runtime, root, tmp_path, monkeypatch, setting, plan, runnable, enabled):
    core = union_core(runtime, root, tmp_path, monkeypatch, runnable)
    monkeypatch.setattr(core, "_personal_responses_planner_enabled", lambda: enabled)
    monkeypatch.setenv("LEGION_AUTO_UNION_AUTHORS", setting)
    assert core.auto_union_authors(plan) == []


def test_auto_union_does_not_count_the_same_model_twice(runtime, root, tmp_path, monkeypatch):
    runtime.configure_model(root, "codex-fixture", "https://codex.example.invalid/v1",
                            key_file(tmp_path, "codex"))
    save(runtime, root, tmp_path, "shared", "https://shared.example.invalid/v1")
    runtime.bind_member_model(root, "cc", "shared", "anthropic-messages")
    runtime.bind_member_model(root, "pi", "shared", "responses")
    env = runtime.environment(root, inherited={})[0]
    for name in ("LEGION_CODEX_TRANSPORT", "LEGION_CODEX_RESPONSES_BINDING",
                 runtime.MEMBER_BINDINGS_ENV, runtime.KEY_ENV,
                 *[name for name in env if name.startswith(runtime.MEMBER_KEY_ENV)]):
        monkeypatch.setenv(name, env[name])
    core = importlib.import_module("legion_core")
    monkeypatch.setattr(core, "_personal_responses_planner_enabled", lambda: True)
    monkeypatch.setattr(core, "_harness_keys", lambda force=False: {"codex", "cc", "pi"})
    monkeypatch.setattr(core, "_aigc_ready", lambda *a, **k: {"runnable_clis": ["cc", "pi"]})
    assert core.auto_union_authors({}) == []


def test_install_member_installs_into_the_instance_without_binding(
        runtime, root, tmp_path, monkeypatch, capsys):
    registry = importlib.import_module("external_agent_registry")
    runtime.configure_model(root, "codex-fixture", "https://codex.example.invalid/v1",
                            key_file(tmp_path, "codex"))
    seen = {}

    def install(key, confirm=False, **_):
        import os
        seen.update(key=key, confirm=confirm,
                    runtime_root=os.environ.get("LEGION_EXTERNAL_RUNTIME_ROOT"))
        return {"ok": True}

    monkeypatch.setattr(registry, "install", install)
    import os
    # The real entry replaces this process's environment and umask. Restore both
    # after every call, or every later test in the session inherits the instance.
    environment, umask = dict(os.environ), os.umask(0o077)
    try:
        installed = runtime.main(["install-member", "--state-dir", str(root), "--member", "pi"])
        report = json.loads(capsys.readouterr().out)
        refused = runtime.main(["install-member", "--state-dir", str(root), "--member", "codex"])
    finally:
        os.environ.clear()
        os.environ.update(environment)
        os.umask(umask)
    assert installed == 0 and refused == 2
    assert report["ok"] is True and report["member"] == "pi"
    assert seen == {"key": "pi", "confirm": True, "runtime_root": str(Path(root) / "runtimes")}
    assert runtime.list_member_models(root)["members"] == []
