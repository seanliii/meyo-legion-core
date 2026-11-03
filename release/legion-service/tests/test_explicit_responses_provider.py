"""Explicit personal/provider transport, no real accounts or external requests."""
from __future__ import annotations

import importlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    for key, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        root = tmp_path / leaf
        root.mkdir()
        monkeypatch.setenv(key, str(root))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    monkeypatch.setenv("MEYO_UNIFIED_APPID", "wrong-inherited-key")
    monkeypatch.setenv("AIGC_APPID", "wrong-inherited-key")
    monkeypatch.setenv("MEYO_UNIFIED_GATEWAY", "https://unrelated.example.invalid/v1")
    monkeypatch.delenv("MEYO_APPID_ALIASES", raising=False)
    monkeypatch.syspath_prepend(str(ORCH))
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("real network/native process is not allowed in this test")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    hr = importlib.import_module("harness_registry")
    mr = importlib.import_module("model_registry")
    calls = []
    monkeypatch.setattr(hr._sh, "which", lambda name: "/fixture/codex" if name == "codex" else None)
    monkeypatch.setattr(hr, "_codex_bridge_ctx", forbidden)
    monkeypatch.setattr(hr, "_legion_prompt", lambda question, ctx: question)

    def capture(command, *_args, **kwargs):
        calls.append((command, kwargs))
        Path(command[command.index("-o") + 1]).write_text("offline fixture, not a delivered task")
        return SimpleNamespace(returncode=0, stderr="", stdout="\n".join((
            json.dumps({"type": "thread.started", "thread_id": "fixture-original-native"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        )))

    monkeypatch.setattr(hr, "_run_task", capture)
    return SimpleNamespace(hr=hr, mr=mr, calls=calls, root=tmp_path, monkeypatch=monkeypatch)


def checkpoint_for(case, key):
    return {
        "session_id": "fixture-original-native",
        "provider_binding": case.mr.ccr_binding_key(
            "fixture-model", key, "https://models.example.invalid/v1"),
    }


@pytest.mark.parametrize("url,expected", [
    ("https://models.example.invalid/v1", "https://models.example.invalid/v1"),
    ("https://models.example.invalid/v1/responses/", "https://models.example.invalid/v1"),
    ("https://models.example.invalid/personal/api", "https://models.example.invalid/personal/api"),
    ("http://127.0.0.1:48123/v1", "http://127.0.0.1:48123/v1"),
    ("http://[::1]:48123/v1/responses", "http://[::1]:48123/v1"),
])
def test_explicit_endpoint_is_a_real_responses_base(isolated, url, expected):
    route = isolated.mr.responses_endpoint("fixture-model", url, explicit=True)
    assert route == {"base_url": expected, "http_headers": {}}


@pytest.mark.parametrize("url", [
    "", "http://models.example.invalid/v1", "http://127.0.0.1.evil.invalid/v1",
    "https://user:secret@models.example.invalid/v1",
    "https://models.example.invalid/v1?api_key=value",
    "https://models.example.invalid/v1#fragment",
    "https://models.example.invalid:bad/v1",
    "https://models.example.invalid/v1/chat/completions",
    "file:///tmp/responses", "https://models.example.invalid/\nheader",
])
def test_invalid_explicit_endpoint_never_falls_back(isolated, url):
    assert isolated.mr.responses_endpoint("fixture-model", url, explicit=True) is None


@pytest.mark.parametrize("resume", [False, True])
def test_actual_adapter_uses_explicit_provider_without_chat_bridge(isolated, resume):
    ctx = isolated.hr.HarnessCtx(
        model="fixture-model", appid="request-specific-fixture-key",
        gateway="https://models.example.invalid/v1",
        user_id="owner-a", session_id="original-session",
        workdir=str(isolated.root / "work"), execution_depth="quick",
        task_memory={"native_checkpoint": checkpoint_for(
            isolated, "request-specific-fixture-key")} if resume else {},
    )
    result = isolated.hr._codex_adapter("__ut_explicit_response", "keep the original task", ctx=ctx)
    assert len(isolated.calls) == 1, result
    command, kwargs = isolated.calls[0]
    config = tomllib.loads("\n".join(
        command[i + 1] for i, value in enumerate(command) if value == "-c"))
    provider = config["model_providers"][config["model_provider"]]
    assert provider["base_url"] == ctx.gateway
    assert provider["wire_api"] == "responses"
    assert kwargs["env"][provider["env_key"]] == ctx.appid
    assert "request-specific-fixture-key" not in repr(command) + repr(result)
    assert "Mt-Session-Id" not in repr(provider)
    assert "x-context-provider" not in repr(provider)
    assert "X-Source" not in repr(provider)
    assert kwargs["cwd"] == ctx.workdir
    assert config["disable_response_storage"] is True
    assert command[command.index("-s") + 1] == "workspace-write"
    assert command[-1] == "-" and kwargs["input"].startswith("keep the original task\n")
    assert kwargs["input"].count("keep the original task") == 1
    assert ("resume" in command) is resume
    if resume:
        assert command[command.index("resume") + 1] == "fixture-original-native"


@pytest.mark.parametrize("gateway,appid", [
    ("", "request-fixture-key"),
    ("https://models.example.invalid/v1", ""),
    ("http://remote.example.invalid/v1", "request-fixture-key"),
])
def test_explicit_request_missing_binding_does_not_borrow_global_values(isolated, gateway, appid):
    result = isolated.hr._codex_adapter(
        "__ut_missing_explicit_binding", "original request",
        ctx=isolated.hr.HarnessCtx(
            model="fixture-model", gateway=gateway, appid=appid,
            workdir=str(isolated.root / "work")),
    )
    assert result["status"] == "unavailable"
    assert not isolated.calls
    assert "wrong-inherited-key" not in repr(result)
    assert "request-fixture-key" not in repr(result)


def test_current_auto_unknown_provider_still_requires_explicit_opt_in(isolated):
    assert isolated.mr.responses_endpoint("fixture-model", "https://models.example.invalid/v1") is None


def test_explicit_mode_without_context_uses_no_import_time_company_fallback(isolated):
    for key in ("MEYO_UNIFIED_GATEWAY", "MEYO_UNIFIED_APPID", "AIGC_APPID"):
        isolated.monkeypatch.delenv(key, raising=False)
    isolated.monkeypatch.setattr(isolated.hr, "_UNIFIED_APPID", "old-company-key")
    isolated.monkeypatch.setattr(isolated.hr, "_UNIFIED_GATEWAY", "https://aigc.sankuai.com/v1")
    result = isolated.hr._codex_adapter("__ut_explicit_no_configuration", "original request")
    assert result["status"] == "unavailable"
    assert not isolated.calls
    assert "old-company-key" not in repr(result)


@pytest.mark.parametrize("key_name", ["AIGC_APPID", "PRIVATE_PROVIDER_KEY"])
def test_ctxless_binding_reads_operator_environment_before_legacy_fallback(isolated, key_name):
    mp = isolated.monkeypatch
    mp.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps({
        "model": "fixture-model", "base_url": "https://models.example.invalid/v1",
        "api_key_env": key_name,
    }))
    mp.setenv("MEYO_UNIFIED_APPID", "company-sentinel-not-for-this-provider")
    mp.setenv(key_name, "personal-sentinel-selected-by-operator")
    result = isolated.hr._codex_adapter("__ut_ctxless_original_env", "original task")
    assert len(isolated.calls) == 1, result
    command, kwargs = isolated.calls[0]
    config = tomllib.loads("\n".join(
        command[i + 1] for i, value in enumerate(command) if value == "-c"))
    provider = config["model_providers"][config["model_provider"]]
    assert provider["base_url"] == "https://models.example.invalid/v1"
    assert kwargs["env"][provider["env_key"]] == "personal-sentinel-selected-by-operator"
    assert "company-sentinel-not-for-this-provider" not in repr(provider)
    assert "personal-sentinel-selected-by-operator" not in repr(command) + repr(result)


def test_explicit_key_is_not_reinterpreted_as_a_company_alias(isolated):
    isolated.monkeypatch.setenv("MEYO_APPID_ALIASES", "appid-mine=alias-fixture-value")
    result = isolated.hr._codex_adapter(
        "__ut_explicit_alias", "original task",
        ctx=isolated.hr.HarnessCtx(
            model="fixture-model", gateway="https://models.example.invalid/v1",
            appid="appid-mine", workdir=str(isolated.root / "work"),
        ),
    )
    assert len(isolated.calls) == 1, result
    command, kwargs = isolated.calls[0]
    # Explicit keys are opaque caller values, not company-pool aliases.
    assert kwargs["env"]["AIGC_APPID"] == "appid-mine"
    assert "alias-fixture-value" not in repr(command) + repr(result) + repr(kwargs["env"]["AIGC_APPID"])


def test_explicit_managed_linux_retains_native_owner_and_checkpoint(isolated):
    import native_process_sandbox

    root, mp, hr = isolated.root, isolated.monkeypatch, isolated.hr
    app, runtime = root / "app", root / "runtime"
    runtime.mkdir()
    anchor = app / "legion/orchestrator/harness_registry.py"
    anchor.parent.mkdir(parents=True)
    (app / "legion/legion-service").mkdir()
    anchor.write_text("# Only the published layout is simulated; real adapter code is used.")
    mp.setattr(hr, "__file__", str(anchor))
    mp.setattr(hr, "_sys", SimpleNamespace(**{**vars(sys), "platform": "linux"}))
    mp.setenv("MEYO_RUNTIME_ROOT", str(runtime))
    calls = []
    config = root / "owned-config"
    config.mkdir()

    @contextmanager
    def prepared(env, workdir, **options):
        calls.append({"workdir": workdir, "options": options})
        yield SimpleNamespace(
            executable="/fixture/native-codex", env=dict(env), config_dir=str(config),
            native_args=["-s", "workspace-write"], wrap=lambda command: command,
        )

    mp.setattr(native_process_sandbox, "prepare_codex_native", prepared)
    result = hr._codex_adapter(
        "__ut_explicit_managed", "original managed task",
        ctx=hr.HarnessCtx(
            model="fixture-model", gateway="https://models.example.invalid/v1",
            appid="managed-fixture-key", user_id="owner-a",
            session_id="original-session", execution_depth="quick",
            workdir=str(root / "work"),
            task_memory={"native_checkpoint": checkpoint_for(isolated, "managed-fixture-key")},
        ),
    )
    assert len(calls) == len(isolated.calls) == 1, result
    command, kwargs = isolated.calls[0]
    settings = tomllib.loads("\n".join(
        command[i + 1] for i, value in enumerate(command) if value == "-c"))
    provider = settings["model_providers"][settings["model_provider"]]
    assert provider["base_url"] == "https://models.example.invalid/v1"
    assert "Mt-Session-Id" not in repr(provider)
    assert command[command.index("resume") + 1] == "fixture-original-native"
    assert kwargs["env"]["AIGC_APPID"] == "managed-fixture-key"
    assert calls[0]["workdir"] == str(root / "work")
    assert app in calls[0]["options"]["protected_roots"]
    assert runtime in calls[0]["options"]["protected_roots"]
    assert result["native_facts"]["managed_native"]["native_cli_started"] is True


def test_operator_binding_resolves_only_the_named_credential(isolated):
    source = {
        "LEGION_CODEX_RESPONSES_BINDING": json.dumps({
            "model": "fixture-model", "base_url": "https://models.example.invalid/v1",
            "api_key_env": "PRIVATE_PROVIDER_KEY",
        }),
        "PRIVATE_PROVIDER_KEY": "private-fixture-key",
        "MEYO_UNIFIED_APPID": "wrong-company-fixture",
    }
    assert isolated.mr.explicit_responses_binding(source) == {
        "model": "fixture-model", "base_url": "https://models.example.invalid/v1",
        "api_key": "private-fixture-key",
    }
    del source["PRIVATE_PROVIDER_KEY"]
    with pytest.raises(ValueError, match="显式 Responses") as error:
        isolated.mr.explicit_responses_binding(source)
    assert "wrong-company-fixture" not in str(error.value)


@pytest.fixture
def explicit_run_one(isolated):
    from contextlib import nullcontext
    import legion_core
    import task_memory
    import external_agent_registry
    import legion_concurrency

    case, mp = isolated, isolated.monkeypatch
    mp.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps({
        "model": "fixture-model", "base_url": "https://models.example.invalid/v1",
        "api_key_env": "PRIVATE_PROVIDER_KEY",
    }))
    mp.setenv("PRIVATE_PROVIDER_KEY", "private-fixture-key")
    mp.setattr(legion_core, "_ORCH_OK", True)
    mp.setattr(legion_core, "_harness_keys", lambda: {"codex"})
    mp.setattr(legion_core, "_CORE_INPROC_KEYS", {"codex"})
    case.real_dynamic_max_attempts = legion_core._dynamic_max_attempts
    mp.setattr(legion_core, "_dynamic_max_attempts", lambda _pool: 3)
    mp.setattr(legion_core, "record_model_health", lambda *a, **kw: None)
    mp.setattr(legion_core, "_record_gateway_call", lambda *a, **kw: None)
    mp.setattr(external_agent_registry, "load_catalog", lambda: [])
    mp.setattr(legion_concurrency, "slot", lambda *a, **kw: nullcontext())
    mp.setattr(task_memory, "ROOT", case.root / "task-memory")
    selections = []

    def select(*args, **kwargs):
        selections.append("company-pool")
        return {"model": "company-fixture-model", "appid": "company-pool-fixture-key"}

    mp.setattr(legion_core, "select_usable_model", select)
    mp.setattr(legion_core, "_appid_for_model", lambda model: "company-pool-fixture-key")
    mp.setattr(legion_core, "_fallback_candidates", lambda *a, **kw: [])
    case.core, case.selections = legion_core, selections
    return case


def test_run_one_missing_explicit_config_stops_before_native_or_company_pool(explicit_run_one):
    case = explicit_run_one
    case.monkeypatch.delenv("LEGION_CODEX_RESPONSES_BINDING", raising=False)
    result = case.core.run_one(
        "codex", "original request", qhash="__ut_missing_private_binding",
        user_id="owner-a", conversation_id="original-session", execution_depth="quick")
    assert result["status"] == "error"
    assert result["error_envelope"]["category"] == "model_config"
    assert result["error_envelope"]["retryable"] is False
    assert not case.calls and not case.selections
    assert "company-pool-fixture-key" not in repr(result)


def test_run_one_reaches_actual_adapter_with_explicit_pair(explicit_run_one):
    case = explicit_run_one
    result = case.core.run_one(
        "codex", "original request", qhash="__ut_private_binding",
        user_id="owner-a", conversation_id="original-session", execution_depth="quick")
    assert len(case.calls) == 1, result
    command, kwargs = case.calls[0]
    config = tomllib.loads("\n".join(
        command[i + 1] for i, part in enumerate(command) if part == "-c"))
    provider = config["model_providers"][config["model_provider"]]
    assert provider["base_url"] == "https://models.example.invalid/v1"
    assert kwargs["env"]["AIGC_APPID"] == "private-fixture-key"
    assert not case.selections
    assert "private-fixture-key" not in repr(result)


@pytest.mark.parametrize("first", ["ordinary_failure", "native_budget"])
def test_run_one_recovery_never_changes_explicit_pair(explicit_run_one, first):
    case = explicit_run_one
    attempts = []
    work = case.root / "work"
    work.mkdir()

    def execute(key, task, qhash, model_override=None, appid_override=None, **kwargs):
        binding = kwargs.get("provider_binding")
        attempts.append(dict(binding) if binding else None)
        if len(attempts) == 1:
            # A later process-environment change must not mutate this request's pair.
            case.monkeypatch.setenv("PRIVATE_PROVIDER_KEY", "different-fixture-key")
            if first == "ordinary_failure":
                return {"status": "rc1", "final": "", "error": "HTTP 502 fixture",
                        "workdir": str(work)}
            return {
                "status": "incomplete", "final": "partial fixture",
                "native_facts": {"completed": False, "failed": False, "interrupted": False,
                                 "turn_exit_reason": "max_iterations_reached(1/2)"},
                "native_checkpoint": {"session_id": "fixture-original-native"},
                "workdir": str(work),
            }
        return {"status": "ok", "final": "completed fixture", "workdir": str(work)}

    case.monkeypatch.setattr(case.core, "_run_core_inprocess", execute)
    result = case.core.run_one(
        "codex", "original request", qhash="__ut_bound_" + first,
        user_id="owner-a", conversation_id="original-session", execution_depth="quick",
        task_facts={"project_context": {"repo_root": str(work), "user_id": "owner-a"}},
    )
    assert attempts and all(row == {
        "model": "fixture-model", "base_url": "https://models.example.invalid/v1",
        "api_key": "private-fixture-key",
    } for row in attempts), (attempts, result)
    assert len(attempts) == (2 if first == "native_budget" else 1)
    assert not case.selections
    assert "private-fixture-key" not in repr(result)


@pytest.mark.parametrize("override", [
    {"preferred_appid": "company-pool-fixture-key"},
    {"preferred_model": "different-fixture-model"},
])
def test_run_one_conflicting_selection_cannot_replace_operator_binding(explicit_run_one, override):
    case = explicit_run_one
    result = case.core.run_one(
        "codex", "original request", qhash="__ut_conflicting_private_binding",
        user_id="owner-a", conversation_id="original-session",
        execution_depth="quick", **override)
    assert result["error_envelope"]["category"] == "model_config"
    assert not case.calls and not case.selections
    assert "private-fixture-key" not in repr(result)


def test_explicit_error_output_does_not_publish_the_model_key(isolated):
    def failed(command, *_args, **_kwargs):
        Path(command[command.index("-o") + 1]).write_text("")
        return SimpleNamespace(
            returncode=1, stdout="",
            stderr="fixture echoed request-specific-fixture-key",
        )

    isolated.monkeypatch.setattr(isolated.hr, "_run_task", failed)
    result = isolated.hr._codex_adapter(
        "__ut_explicit_error", "original task",
        ctx=isolated.hr.HarnessCtx(
            model="fixture-model", gateway="https://models.example.invalid/v1",
            appid="request-specific-fixture-key", workdir=str(isolated.root / "work")),
    )
    assert "request-specific-fixture-key" not in repr(result)
    assert "[REDACTED]" in result["error"]


@pytest.mark.parametrize("path", ["core_inproc", "external_inproc", "isolate"])
def test_private_binding_reaches_each_execution_boundary(explicit_run_one, path):
    """Native argv is real adapter output; process/discovery remain offline."""
    import external_agent_registry
    import github_access
    import legion_proc_registry
    import legion_sandbox

    case, mp = explicit_run_one, explicit_run_one.monkeypatch
    binding = {"model": "fixture-model", "base_url": "https://models.example.invalid/v1",
               "api_key": "private-fixture-key"}
    work = case.root / "work"
    work.mkdir()
    options = {
        "provider_binding": binding, "user_id": "owner-a",
        "session_id": "original-session", "execution_depth": "quick",
        "project_context": {"repo_root": str(work), "user_id": "owner-a"},
        "task_memory": {"native_checkpoint": checkpoint_for(case, "private-fixture-key")},
    }
    forwarded = []
    default_pool_reads = []

    def default_pool(*args, **kwargs):
        default_pool_reads.append(True)
        return ["company-fallback-fixture"]

    mp.setattr(case.core, "_fallback_candidates", default_pool)
    mp.setenv("MEYO_FALLBACK_MODELS", '["inherited-company-fallback-fixture"]')
    mp.setattr(case.hr, "_legion_prompt", lambda question, ctx, **kw: question)
    mp.setattr(case.hr, "_external_adapter_capabilities", lambda key: {"tools": True})
    mp.setattr(legion_sandbox, "prepare_peer_sources", lambda *a, **kw: "")
    mp.setattr(github_access, "shared_environment", lambda env: env)
    mp.setattr(legion_proc_registry, "register", lambda *a, **kw: True)
    mp.setattr(legion_proc_registry, "unregister", lambda *a, **kw: None)
    mp.setattr(legion_proc_registry, "kill_owned_by", lambda *a, **kw: None)

    def decode_and_invoke(env, question):
        forwarded.append(dict(env))
        with mp.context() as child:
            for name in list(os.environ):
                child.delenv(name, raising=False)
            for name, value in env.items():
                child.setenv(name, str(value))
            ctx = case.hr.HarnessCtx.from_env()
            return case.hr._codex_adapter("__ut_boundary_leaf", question, ctx=ctx)

    if path == "core_inproc":
        result = case.core._run_core_inprocess(
            "codex", "original boundary task", "__ut_core_boundary", timeout_s=20, **options)
    elif path == "external_inproc":
        def external_run(key, task, *, env, runtime_context, **kw):
            assert "private-fixture-key" not in task + repr(runtime_context)
            return decode_and_invoke({**os.environ, **env}, task)

        mp.setattr(external_agent_registry, "run", external_run)
        result = case.core._run_external_inprocess(
            "codex", "original boundary task", "__ut_external_boundary", timeout_s=20, **options)
    else:
        class Process:
            pid = 2_000_000_000

            def __init__(self, command, *, env, **kw):
                result = decode_and_invoke(env, "original boundary task")
                self.stdout = io.StringIO(json.dumps(result) + "\n")
                self.stderr = io.StringIO("")
                self.returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        actual_getpgid = os.getpgid

        def getpgid(pid):
            if pid == Process.pid:
                raise ProcessLookupError()
            return actual_getpgid(pid)

        mp.setattr(os, "getpgid", getpgid)
        mp.setattr(subprocess, "Popen", Process)
        result = case.core._run_harness_isolated(
            "codex", "original boundary task", "__ut_isolated_boundary", timeout_s=20, **options)
    assert len(case.calls) == 1, result
    command, kwargs = case.calls[0]
    config = tomllib.loads("\n".join(
        command[i + 1] for i, part in enumerate(command) if part == "-c"))
    provider = config["model_providers"][config["model_provider"]]
    assert provider["base_url"] == binding["base_url"]
    assert kwargs["env"]["AIGC_APPID"] == binding["api_key"]
    assert command[command.index("-m") + 1] == binding["model"]
    assert kwargs["cwd"] == str(work)
    assert "private-fixture-key" not in repr(command) + kwargs["input"] + repr(result)
    assert not default_pool_reads
    assert json.loads(kwargs["env"].get("MEYO_FALLBACK_MODELS", "[]")) == []
    for env in forwarded:
        assert env["MEYO_MODEL_PROTOCOL"] == "responses"
        assert "private-fixture-key" not in env["MEYO_TASK_FACTS_JSON"]


@pytest.mark.parametrize("change", ["same", "key", "url", "model", "auto", "unknown_checkpoint"])
def test_two_run_one_calls_bind_native_restore_to_original_provider(explicit_run_one, change):
    import task_memory

    case, mp = explicit_run_one, explicit_run_one.monkeypatch
    work = case.root / "work"
    work.mkdir()
    options = {
        "user_id": "owner-a", "conversation_id": "original-session",
        "execution_depth": "quick",
        "task_facts": {"project_context": {"repo_root": str(work), "user_id": "owner-a"}},
    }
    first = case.core.run_one("codex", "first request", qhash="__ut_pair_first", **options)
    assert len(case.calls) == 1 and first.get("native_checkpoint", {}).get("session_id"), first
    first_checkpoint = dict(first["native_checkpoint"])
    if change == "key":
        mp.setenv("PRIVATE_PROVIDER_KEY", "other-private-fixture-key")
    elif change in ("url", "model"):
        binding = json.loads(os.environ["LEGION_CODEX_RESPONSES_BINDING"])
        binding["base_url" if change == "url" else "model"] = (
            "https://other-provider.example.invalid/v1" if change == "url" else "another-model")
        mp.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps(binding))
    elif change == "auto":
        mp.setenv("LEGION_CODEX_TRANSPORT", "auto")
    elif change == "unknown_checkpoint":
        first_checkpoint.pop("provider_binding", None)
        task_memory.update("conv:codex:original-session", "codex",
                           native_checkpoint=first_checkpoint)
    result = case.core.run_one("codex", "continue original request", qhash="__ut_pair_second", **options)
    if change == "same":
        assert len(case.calls) == 2 and result.get("resumed_native"), result
        command, kwargs = case.calls[1]
        assert command[command.index("resume") + 1] == "fixture-original-native"
        assert kwargs["env"]["AIGC_APPID"] == "private-fixture-key"
        expected = case.mr.ccr_binding_key(
            "fixture-model", "private-fixture-key", "https://models.example.invalid/v1")
        assert result["native_checkpoint"].get("provider_binding") == expected
    else:
        assert len(case.calls) == 1, (change, result)
        assert result.get("error_envelope", {}).get("category") == "native_session_unavailable"
        kept = task_memory.load("conv:codex:original-session", "codex")["native_checkpoint"]
        assert kept == first_checkpoint
    assert "private-fixture-key" not in repr(result)


@pytest.mark.parametrize("memory_source", ["request", "conversation"])
def test_restore_refusal_retains_actionable_context_without_exporting_binding(explicit_run_one, memory_source):
    import task_memory

    case, mp = explicit_run_one, explicit_run_one.monkeypatch
    work = case.root / "original-work"
    work.mkdir()
    original = work / "result.txt"
    original.write_text("original user result")
    options = {
        "user_id": "owner-a", "conversation_id": "original-session", "execution_depth": "quick",
        "task_facts": {"project_context": {"repo_root": str(work), "user_id": "owner-a"}},
    }
    first = case.core.run_one("codex", "first request", qhash="__ut_context_first", **options)
    checkpoint = dict(first["native_checkpoint"])
    old_binding = checkpoint["provider_binding"]
    old_request = task_memory.load("__ut_context_first", "codex")
    old_conversation = task_memory.load("conv:codex:original-session", "codex")
    mp.setenv("PRIVATE_PROVIDER_KEY", "changed-private-fixture-key")
    qhash = "__ut_context_first" if memory_source == "request" else "__ut_context_next"
    events = []
    result = case.core.run_one("codex", "continue original request", qhash=qhash, cb=events.append, **options)
    assert result["attempts"] == 0 and result["blocked_before_native"] is True
    context = result["error_envelope"]["recovery_context"]
    assert context["memory_source"] == memory_source
    assert context["native_thread_id"] == checkpoint["session_id"]
    assert context["native_conversation_id"] == "original-session"
    assert context["workdir"] == str(work)
    assert context["workspace_validated"] is False  # Binding fails before the workspace check.
    assert context["binding"]["expected_present"] and context["binding"]["actual_present"]
    assert context["binding"]["matches"] is False
    assert context["binding"]["mismatch_fields"] == ["credential"]
    assert any(event.get("t") == "native_resume_blocked" for event in events)
    shown = next(event["x"] for event in events if event.get("t") == "native_resume_blocked")
    assert "凭据不一致" in shown and "原目录尚未核验" in shown
    assert ("本轮执行记忆" if memory_source == "request" else "原会话记忆") in shown
    assert "凭据不一致" in result["error"]  # Existing final error card, not a new UI.
    exported = json.dumps([result, events])
    assert old_binding not in exported
    assert "changed-private-fixture-key" not in exported and "private-fixture-key" not in exported
    assert task_memory.load("__ut_context_first", "codex") == old_request
    assert task_memory.load("conv:codex:original-session", "codex") == old_conversation
    assert original.read_text() == "original user result"
    assert len(case.calls) == 1


def test_old_checkpoint_missing_descriptor_remains_unknown_not_filled_from_current(explicit_run_one):
    import task_memory

    case, mp = explicit_run_one, explicit_run_one.monkeypatch
    options = {"user_id": "owner-a", "conversation_id": "original-session", "execution_depth": "quick"}
    first = case.core.run_one("codex", "first request", qhash="__ut_legacy_first", **options)
    checkpoint = dict(first["native_checkpoint"])
    checkpoint.pop("provider_descriptor", None)
    task_memory.update("conv:codex:original-session", "codex", native_checkpoint=checkpoint)
    mp.setenv("PRIVATE_PROVIDER_KEY", "changed-private-fixture-key")
    result = case.core.run_one("codex", "continue original request", qhash="__ut_legacy_second", **options)
    binding = result["error_envelope"]["recovery_context"]["binding"]
    assert binding["mismatch_fields"] is None
    assert binding["expected_descriptor_known"] is False
    assert "差异字段未知" in result["error"]
    assert task_memory.load("conv:codex:original-session", "codex")["native_checkpoint"] == checkpoint


def test_legacy_memory_without_checkpoint_starts_a_clean_native_turn(explicit_run_one):
    """A nonempty legacy memory is not a checkpoint and must not crash restoration."""
    import task_memory

    case = explicit_run_one
    task_memory.update("conv:codex:original-session", "codex",
                       native_conversation_id="original-session",
                       native_session_scope="conversation")
    result = case.core.run_one(
        "codex", "continue after a native environment failure", qhash="__ut_missing_checkpoint",
        user_id="owner-a", conversation_id="original-session", execution_depth="quick")
    assert result["status"] == "done", result.get("error") or result
    assert result["native_checkpoint"]["session_id"] == "fixture-original-native"
    assert len(case.calls) == 1


def test_corrected_binding_resumes_original_thread_and_updates_original_artifact(explicit_run_one):
    case, mp = explicit_run_one, explicit_run_one.monkeypatch
    work = case.root / "original-work"
    work.mkdir()
    artifact = work / "user-result.txt"
    artifact.write_text("before")
    options = {
        "user_id": "owner-a", "conversation_id": "original-session", "execution_depth": "quick",
        "task_facts": {"project_context": {"repo_root": str(work), "user_id": "owner-a"}},
    }
    case.core.run_one("codex", "first request", qhash="__ut_recover_first", **options)
    mp.setenv("PRIVATE_PROVIDER_KEY", "changed-private-fixture-key")
    blocked = case.core.run_one("codex", "continue", qhash="__ut_recover_blocked", **options)
    assert blocked["attempts"] == 0
    mp.setenv("PRIVATE_PROVIDER_KEY", "private-fixture-key")
    original_runner = case.hr._run_task

    def execute_fixture(command, *args, **kwargs):
        assert command[command.index("resume") + 1] == "fixture-original-native"
        assert kwargs["cwd"] == str(work)
        artifact.write_text("after - isolated fixture, not a model-generated improvement")
        return original_runner(command, *args, **kwargs)

    mp.setattr(case.hr, "_run_task", execute_fixture)
    result = case.core.run_one("codex", "apply feedback to original", qhash="__ut_recover_next", **options)
    assert result["resumed_native"] is True and len(case.calls) == 2
    assert result["workdir"] == str(work)
    assert artifact.read_text().startswith("after")


def test_rejected_native_result_keeps_accounting_but_not_rejected_checkpoint(explicit_run_one):
    case, mp = explicit_run_one, explicit_run_one.monkeypatch
    raw = {
        "key": "codex", "status": "done", "final": "must not claim delivery",
        "model": "fixture-model", "elapsed": 1,
        "usage": {"available": True, "input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
        "native_facts": {"completed": True, "process_returncode": 0,
                         "private_unknown_field": "must-not-export"},
        "native_checkpoint": {"session_id": "unexpected-thread", "provider_binding": "wrong-binding"},
    }
    mp.setattr(case.core, "_run_core_inprocess", lambda *args, **kwargs: dict(raw))
    result = case.core.run_one("codex", "first request", qhash="__ut_bad_native_binding",
                               user_id="owner-a", conversation_id="original-session")
    assert result["attempts"] == result["execution_calls"] == 1
    assert result["blocked_before_native"] is False
    assert result["error_envelope"]["phase"] == "native_result_binding"
    assert result["native_facts"] == {"completed": True, "process_returncode": 0}
    assert result["usage"]["total_tokens"] == 150
    assert result["model"] == "fixture-model"
    assert not result.get("native_checkpoint")
    assert result["final"] == ""
    assert "wrong-binding" not in json.dumps(result)
    assert "must-not-export" not in json.dumps(result)


@pytest.mark.parametrize("prior_binding", [None, "different-provider-fingerprint"])
def test_direct_adapter_does_not_send_history_to_an_unmatched_provider(isolated, prior_binding):
    checkpoint = {"session_id": "fixture-original-native"}
    if prior_binding:
        checkpoint["provider_binding"] = prior_binding
    result = isolated.hr._codex_adapter(
        "__ut_wrong_provider_restore", "continue original request",
        ctx=isolated.hr.HarnessCtx(
            model="fixture-model", gateway="https://models.example.invalid/v1",
            appid="private-fixture-key", workdir=str(isolated.root / "work"),
            task_memory={"native_checkpoint": checkpoint}),
    )
    assert not isolated.calls
    assert result["error_envelope"]["category"] == "native_session_unavailable"
    assert result["native_checkpoint"] == checkpoint
    assert "private-fixture-key" not in repr(result)


def test_single_authorized_pair_does_not_reduce_native_continuation_budget(explicit_run_one):
    case, mp = explicit_run_one, explicit_run_one.monkeypatch
    mp.setattr(case.core, "_dynamic_max_attempts", case.real_dynamic_max_attempts)
    mp.setattr(case.core, "_MAX_ATTEMPTS", 2)
    mp.setattr(case.core, "_HARD_ATTEMPT_CEIL", 6)
    mp.setattr(case.core, "_gateway_fail_rate", lambda: 0.75)
    work = case.root / "work"
    work.mkdir()
    calls = []

    def execute(key, task, qhash, model_override=None, appid_override=None, **kwargs):
        calls.append(kwargs.get("provider_binding"))
        if len(calls) <= 2:
            return {
                "status": "incomplete", "final": f"unfinished fixture {len(calls)}",
                "native_facts": {"completed": False, "failed": False, "interrupted": False,
                                 "turn_exit_reason": "max_iterations_reached(1/2)"},
                "native_checkpoint": {"session_id": "fixture-original-native"},
                "workdir": str(work),
            }
        return {"status": "ok", "final": "completed fixture", "workdir": str(work)}

    mp.setattr(case.core, "_run_core_inprocess", execute)
    result = case.core.run_one(
        "codex", "original multi-step request", qhash="__ut_same_pair_budget",
        user_id="owner-a", conversation_id="original-session", execution_depth="quick",
        task_facts={"project_context": {"repo_root": str(work), "user_id": "owner-a"}},
    )
    assert len(calls) == 3, result
    assert result["status"] == "done"
    assert all(row == calls[0] for row in calls)
    assert not case.selections
