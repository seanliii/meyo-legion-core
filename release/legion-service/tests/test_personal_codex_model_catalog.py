"""Personal Responses catalogs: fake bindings, isolated files, no model requests."""
from contextlib import contextmanager, nullcontext
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import tomllib

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
RESOURCE = ORCH / "resources/codex-0.153.4-fallback.json"
FALLBACK_SHA256 = "ac8ae107a0d72fe3476b430afb161ea4e67da2e446d778aefc44828160559807"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    for name, leaf in (
        ("HOME", "home"), ("CODEX_HOME", "codex-home"), ("TMPDIR", "tmp"),
        ("MEYO_LEGION_DIR", "data"), ("LEGION_HOME_DIR", "legion"),
        ("LEGION_PROC_REGISTRY_DIR", "registry"),
    ):
        path = tmp_path / leaf
        path.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(path))
    for name in ("MEYO_NATIVE_CAPABILITY_IDS", "LEGION_MANAGED_BROWSER",
                 "MEYO_UNIFIED_APPID", "AIGC_APPID", "MEYO_UNIFIED_GATEWAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "responses")
    monkeypatch.syspath_prepend(str(ORCH))
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))

    def no_network(*args, **kwargs):
        raise AssertionError("network calls are forbidden")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    hr = importlib.import_module("harness_registry")
    mr = importlib.import_module("model_registry")
    case = SimpleNamespace(hr=hr, mr=mr, root=tmp_path, calls=[], fail=False)
    monkeypatch.setattr(hr._sh, "which", lambda _: "/fixture/codex")
    monkeypatch.setattr(hr, "_codex_bridge_ctx", lambda *a, **k: nullcontext(True))
    monkeypatch.setattr(hr, "_legion_prompt", lambda question, ctx: question)

    def no_process(*args, **kwargs):
        raise AssertionError("real native/model execution is forbidden in adapter tests")

    monkeypatch.setattr(subprocess, "Popen", no_process)

    def capture(command, *args, **kwargs):
        config = tomllib.loads("\n".join(
            command[i + 1] for i, arg in enumerate(command) if arg == "-c"))
        call = {"command": command, "kwargs": kwargs, "config": config}
        case.calls.append(call)
        if "model_catalog_json" in config:
            path = Path(config["model_catalog_json"])
            call.update(path=path, catalog=json.loads(path.read_text()),
                        mode=stat.S_IMODE(path.stat().st_mode),
                        parent_mode=stat.S_IMODE(path.parent.stat().st_mode))
        if case.fail:
            raise subprocess.TimeoutExpired(command, 1)
        Path(command[command.index("-o") + 1]).write_text("offline fixture result")
        return SimpleNamespace(returncode=0, stderr="", stdout="\n".join((
            json.dumps({"type": "thread.started", "thread_id": "fixture-native-thread"}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        )))

    monkeypatch.setattr(hr, "_run_task", capture)
    return case


def managed(case, monkeypatch):
    import native_process_sandbox

    app = case.root / "app"
    anchor = app / "legion/orchestrator/harness_registry.py"
    anchor.parent.mkdir(parents=True)
    (app / "legion/legion-service").mkdir()
    anchor.write_text("# Fixture location only; real adapter remains in use.")
    runtime = case.root / "runtime"
    runtime.mkdir()
    control = case.root / "native-control"
    control.mkdir(mode=0o700)
    monkeypatch.setenv("MEYO_RUNTIME_ROOT", str(runtime))
    monkeypatch.setattr(case.hr, "__file__", str(anchor))
    monkeypatch.setattr(case.hr, "_sys", SimpleNamespace(**{**vars(sys), "platform": "linux"}))

    @contextmanager
    def prepare(env, cwd, **options):
        yield SimpleNamespace(
            executable="/fixture/native-codex", env=dict(env), config_dir=control,
            native_args=["-s", "workspace-write"], wrap=lambda command: command,
        )

    monkeypatch.setattr(native_process_sandbox, "prepare_codex_native", prepare)
    return control


def context(case, *, resume=False, model="fixture-personal-model", depth="standard"):
    key = "fake-catalog-provider-key"
    gateway = "https://personal.example.invalid/v1"
    checkpoint = {"kind": "codex_thread", "session_id": "fixture-native-thread",
                  "provider_binding": case.mr.ccr_binding_key(model, key, gateway)}
    return case.hr.HarnessCtx(
        model=model, appid=key, gateway=gateway, model_protocol="responses",
        user_id="fixture-owner", session_id="fixture-logical-session",
        workdir=str(case.root / "workspace"), execution_depth=depth,
        task_memory={"native_checkpoint": checkpoint} if resume else {},
    )


def declared_environment(ctx, inputs, key_name="FIXTURE_DECLARED_KEY"):
    binding = {"model": ctx.model, "base_url": ctx.gateway, "api_key_env": key_name}
    if inputs is not None:
        binding["input_modalities"] = inputs
    return {"LEGION_CODEX_TRANSPORT": "responses",
            "LEGION_CODEX_RESPONSES_BINDING": json.dumps(binding), key_name: ctx.appid}


@pytest.mark.parametrize("is_managed", [False, True])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("depth", ["quick", "standard"])
def test_bound_catalog_is_shared_by_normal_managed_first_and_resume(
        adapter, monkeypatch, is_managed, resume, depth):
    control = managed(adapter, monkeypatch) if is_managed else None
    ctx = context(adapter, resume=resume, depth=depth)
    result = adapter.hr._codex_adapter("__ut_personal_catalog", "original task", ctx=ctx)
    assert len(adapter.calls) == 1, result
    call = adapter.calls[0]
    assert "model_catalog_json" in call["config"], "explicit binding needs a native model catalog"
    assert result["status"] == "ok"
    row, = call["catalog"]["models"]
    assert row["slug"] == row["display_name"] == ctx.model
    assert call["command"][call["command"].index("-m") + 1] == ctx.model
    provider = call["config"]["model_providers"][call["config"]["model_provider"]]
    assert provider["base_url"] == ctx.gateway and provider["wire_api"] == "responses"
    assert call["kwargs"]["env"][provider["env_key"]] == ctx.appid
    assert ("resume" in call["command"]) is resume
    if resume:
        assert call["command"][call["command"].index("resume") + 1] == "fixture-native-thread"
    assert result["native_checkpoint"]["session_id"] == "fixture-native-thread"
    assert call["command"][call["command"].index("-s") + 1] == "workspace-write"
    assert "features" not in call["config"] and "agents" not in call["config"]
    assert call["mode"] == 0o600 and call["parent_mode"] == 0o700
    assert not call["path"].is_relative_to(Path(ctx.workdir))
    if control is not None:
        assert call["path"].is_relative_to(control)
    assert not call["path"].exists(), "derived catalog must be cleaned after the native process"
    assert ctx.appid not in json.dumps(call["catalog"]) + repr(call["command"]) + repr(result)
    assert ctx.appid not in call["kwargs"]["input"]
    assert "gpt-" not in json.dumps({k: v for k, v in row.items() if k != "base_instructions"})


def test_catalog_keeps_exact_generic_fallback_without_inventing_model_capabilities(adapter):
    catalog = adapter.hr._codex_bound_model_catalog('provider/custom-"quoted"')
    row, = catalog["models"]
    assert row["slug"] == row["display_name"] == 'provider/custom-"quoted"'
    assert hashlib.sha256(row["base_instructions"].encode()).hexdigest() == FALLBACK_SHA256
    assert row["supported_reasoning_levels"] == []
    assert row["input_modalities"] == ["text"]
    assert row["experimental_supported_tools"] == []
    assert row["support_verbosity"] is False
    assert row.get("context_window") is None
    assert row.get("max_context_window") is None
    assert row.get("auto_compact_token_limit") is None
    assert not row.get("prefer_websockets")
    assert not row.get("supports_search_tool")
    assert not row.get("supports_image_detail_original")
    assert row.get("tool_mode") is None and row.get("multi_agent_version") is None
    # Native executor/truncation defaults, not claims about a provider's context window.
    assert row["shell_type"] == "unified_exec"
    assert row["truncation_policy"] == {"mode": "bytes", "limit": 10000}


def test_resource_provenance_license_and_native_content_digest():
    resource = json.loads(RESOURCE.read_text())
    assert resource["source_ref"] == "rust-v0.153.4"
    assert resource["source_url"] == (
        "https://raw.githubusercontent.com/openai/codex/rust-v0.153.4/"
        "codex-rs/models-manager/prompt.md")
    assert resource["instructions_sha256"] == FALLBACK_SHA256
    raw = resource["instructions"].encode()
    assert hashlib.sha256(raw).hexdigest() == FALLBACK_SHA256
    assert len(raw) == resource["instructions_bytes"] == 20903
    assert resource["license"]["spdx_id"] == "Apache-2.0"
    assert "Apache License" in resource["license"]["text"]
    assert hashlib.sha256(resource["license"]["text"].encode()).hexdigest() == (
        resource["license"]["sha256"])
    assert resource["native_verification"]["version"] == "0.153.4"


@pytest.mark.parametrize("transport", ["auto", "bridge"])
@pytest.mark.parametrize("resume", [False, True])
def test_other_transports_do_not_receive_the_personal_catalog(adapter, transport, resume):
    ctx = context(adapter)
    ctx.model_protocol = transport
    ctx.gateway = "https://aigc.sankuai.com/v1/openai/native"
    if resume:
        ctx.task_memory = {"native_checkpoint": {"session_id": "fixture-legacy-native"}}
    result = adapter.hr._codex_adapter("__ut_nonpersonal_catalog", "legacy task", ctx=ctx)
    assert result["status"] == "ok", result
    call, = adapter.calls
    assert "model_catalog_json" not in call["config"]
    assert ("resume" in call["command"]) is resume


@pytest.mark.parametrize("is_managed", [False, True])
def test_catalog_is_cleaned_on_native_timeout(adapter, monkeypatch, is_managed):
    if is_managed:
        managed(adapter, monkeypatch)
    adapter.fail = True
    adapter.hr._codex_adapter("__ut_catalog_timeout", "original task", ctx=context(adapter))
    call, = adapter.calls
    assert "model_catalog_json" in call["config"]
    assert not call["path"].exists()


def test_invalid_resource_fails_before_native_without_leaking_credentials(adapter, monkeypatch):
    resource = json.loads(RESOURCE.read_text())
    resource["instructions"] += "\nnot the original fallback"
    bad = adapter.root / "invalid-resource.json"
    bad.write_text(json.dumps(resource))
    monkeypatch.setattr(adapter.hr, "_CODEX_FALLBACK_RESOURCE", bad)
    result = adapter.hr._codex_adapter("__ut_bad_catalog_resource", "task", ctx=context(adapter))
    assert not adapter.calls
    assert result["status"] != "ok"
    assert "fake-catalog-provider-key" not in repr(result)


def test_each_call_gets_its_own_catalog_and_does_not_change_the_process_environment(adapter):
    original = dict(os.environ)
    for index in range(2):
        adapter.hr._codex_adapter(f"__ut_catalog_{index}", "task", ctx=context(adapter))
    paths = [call["path"] for call in adapter.calls]
    assert len(paths) == len(set(paths)) == 2
    assert not any(path.exists() for path in paths)
    assert dict(os.environ) == original


def test_exact_local_native_cli_accepts_the_catalog_without_network_or_model(tmp_path):
    binary = os.environ.get("MEYO_TEST_CODEX_BINARY")
    if not binary:
        pytest.skip("Set MEYO_TEST_CODEX_BINARY to an independently located native 0.153.4 CLI")
    if sys.platform != "darwin":
        pytest.skip("This local native check requires macOS sandbox-exec to deny all networking")
    hr = importlib.import_module("harness_registry")
    env = {name: os.environ[name] for name in ("HOME", "CODEX_HOME", "TMPDIR")}
    env.update(PATH="/usr/bin:/bin", LANG="C")

    def native(*args):
        process = subprocess.Popen(
            ["/usr/bin/sandbox-exec", "-p", "(version 1) (allow default) (deny network*)",
             binary, *args], env=env, cwd=tmp_path, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        try:
            out, err = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        return process.returncode, out, err

    code, out, err = native("--version")
    assert code == 0 and out.strip() == "codex-cli 0.153.4", (out, err)
    catalog = hr._codex_bound_model_catalog("fixture-native-bound-model")
    path = tmp_path / "native-models.json"
    path.write_text(json.dumps(catalog))
    path.chmod(0o600)
    code, out, err = native("-c", "model_catalog_json=" + json.dumps(str(path)), "debug", "models")
    assert code == 0, err
    row, = json.loads(out)["models"]
    assert row["slug"] == "fixture-native-bound-model"
    assert row["model_messages"]["instructions_template"] == catalog["models"][0]["base_instructions"]
    assert row["input_modalities"] == ["text"]
    assert not row.get("context_window")
    del catalog["models"][0]["base_instructions"]
    path.write_text(json.dumps(catalog))
    code, out, err = native("-c", "model_catalog_json=" + json.dumps(str(path)), "debug", "models")
    assert code != 0 and "missing both" in err and "instructions_template" in err


@pytest.mark.parametrize("is_managed", [False, True])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("declared,expected", [
    (["image", "text"], ["text", "image"]),
    (["text"], ["text"]),
    (None, ["text"]),
])
def test_declared_modalities_reach_both_native_paths_and_resume(
        adapter, monkeypatch, is_managed, resume, declared, expected):
    if is_managed:
        managed(adapter, monkeypatch)
    ctx = context(adapter, resume=resume, model="fixture-bound-model")
    snapshot = declared_environment(ctx, declared)
    for name, value in snapshot.items():
        monkeypatch.setenv(name, value)
    requested = []

    def inputs(model, environment=None):
        requested.append(model)
        assert environment is not None
        assert all(environment[name] == value for name, value in snapshot.items())
        return declared

    monkeypatch.setattr(adapter.mr, "input_modalities", inputs)
    result = adapter.hr._codex_adapter("__ut_declared_modalities", "original task", ctx=ctx)
    assert result["status"] == "ok", result
    assert requested == [ctx.model]
    call, = adapter.calls
    row, = call["catalog"]["models"]
    assert row["slug"] == ctx.model and row["input_modalities"] == expected
    assert call["command"][call["command"].index("-m") + 1] == ctx.model
    assert ("resume" in call["command"]) is resume
    assert result["resumed_native"] is resume
    assert result["native_checkpoint"]["session_id"] == "fixture-native-thread"
    assert call["command"][call["command"].index("-s") + 1] == "workspace-write"
    assert "features" not in call["config"] and "agents" not in call["config"]
    assert hashlib.sha256(row["base_instructions"].encode()).hexdigest() == FALLBACK_SHA256
    assert not row.get("supports_image_detail_original")
    assert not call["path"].exists()
    assert ctx.appid not in json.dumps(call["catalog"]) + repr(call["command"]) + repr(result)


@pytest.mark.parametrize("declared", [
    None, [], "image", ["text", None], ["text", 1], ["text", "IMAGE"],
    ["text", "image", "video"], ["other-model"],
    {"model": "other-model", "input_modalities": ["text", "image"]},
])
def test_declared_modalities_missing_or_invalid_stay_text_only(adapter, monkeypatch, declared):
    monkeypatch.setattr(adapter.mr, "input_modalities", lambda model: declared)
    row, = adapter.hr._codex_bound_model_catalog("fixture-image-in-name")["models"]
    assert row["input_modalities"] == ["text"]


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
def test_declared_modalities_invalid_registry_declaration_stays_text_only(
        adapter, monkeypatch, error_type):
    def invalid(model):
        raise error_type("invalid fixture declaration")

    monkeypatch.setattr(adapter.mr, "input_modalities", invalid)
    row, = adapter.hr._codex_bound_model_catalog("fixture-bound-model")["models"]
    assert row["input_modalities"] == ["text"]


def test_declared_modalities_older_registry_without_api_stays_text_only(adapter, monkeypatch):
    monkeypatch.delattr(adapter.mr, "input_modalities")
    row, = adapter.hr._codex_bound_model_catalog("fixture-bound-model")["models"]
    assert row["input_modalities"] == ["text"]


def test_declared_modalities_never_borrow_another_models_declaration(adapter, monkeypatch):
    requested = []

    def inputs(model):
        requested.append(model)
        return ["text", "image"] if model == "other-current-model" else None

    def no_default_model(*args, **kwargs):
        raise AssertionError("A missing declaration must not trigger another model selection")

    monkeypatch.setattr(adapter.mr, "input_modalities", inputs)
    monkeypatch.setattr(adapter.mr, "resolve_model", no_default_model)
    row, = adapter.hr._codex_bound_model_catalog("fixture-bound-model")["models"]
    assert requested == ["fixture-bound-model"]
    assert row["slug"] == "fixture-bound-model" and row["input_modalities"] == ["text"]


def test_declared_modalities_are_fresh_and_change_no_other_catalog_fields(adapter, monkeypatch):
    declarations = {"fixture-a": ["text", "image"], "fixture-b": ["text"]}
    monkeypatch.setattr(adapter.mr, "input_modalities", declarations.get)
    image_row, = adapter.hr._codex_bound_model_catalog("fixture-a")["models"]
    text_row, = adapter.hr._codex_bound_model_catalog("fixture-b")["models"]
    assert image_row["input_modalities"] == ["text", "image"]
    assert text_row["input_modalities"] == ["text"]
    declarations.pop("fixture-a")
    cleared_row, = adapter.hr._codex_bound_model_catalog("fixture-a")["models"]
    assert cleared_row["input_modalities"] == ["text"]
    assert {k: v for k, v in image_row.items() if k != "input_modalities"} == {
        k: v for k, v in cleared_row.items() if k != "input_modalities"}


@pytest.mark.parametrize("is_managed", [False, True])
@pytest.mark.parametrize("supplied_ctx", [False, True])
@pytest.mark.parametrize("resume", [False, True])
def test_modality_snapshot_uses_current_thread_not_worker_global(
        adapter, monkeypatch, is_managed, supplied_ctx, resume):
    if is_managed:
        managed(adapter, monkeypatch)
    selected = context(adapter, resume=resume, model="fixture-selected")
    current = declared_environment(selected, ["text", "image"], key_name="AIGC_APPID")
    worker = context(adapter, model="fixture-worker-global")
    worker.appid = "fake-worker-global-key"
    for name, value in declared_environment(worker, ["text"]).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(adapter.hr._INPROC_RUNTIME_ENV, "values", current, raising=False)
    if not supplied_ctx:
        # The legacy key rewrite must not alter the captured AIGC_APPID binding.
        selected.appid = "fake-stale-context-key"
        monkeypatch.setattr(adapter.hr.HarnessCtx, "from_env",
                            classmethod(lambda cls: selected))
    result = adapter.hr._codex_adapter(
        "__ut_modality_snapshot", "original task", ctx=selected if supplied_ctx else None)
    assert result["status"] == "ok", result
    call, = adapter.calls
    row, = call["catalog"]["models"]
    assert row["slug"] == "fixture-selected"
    assert row["input_modalities"] == ["text", "image"]
    assert call["kwargs"]["env"]["AIGC_APPID"] == current["AIGC_APPID"]
    assert ("resume" in call["command"]) is resume
    assert adapter.hr._INPROC_RUNTIME_ENV.values == current
    assert "fake-worker-global-key" not in json.dumps(row)


@pytest.mark.parametrize("is_managed", [False, True])
@pytest.mark.parametrize("mismatch", ["gateway", "key"])
def test_modality_snapshot_same_model_other_binding_is_not_borrowed(
        adapter, monkeypatch, is_managed, mismatch):
    if is_managed:
        managed(adapter, monkeypatch)
    selected = context(adapter, model="fixture-shared-model-name")
    other = context(adapter, model=selected.model)
    if mismatch == "gateway":
        other.gateway = "https://other-provider.example.invalid/v1"
    else:
        other.appid = "fake-other-account-key"
    # Using AIGC_APPID catches a mistaken comparison after the key is rewritten.
    for name, value in declared_environment(
            other, ["text", "image"], key_name="AIGC_APPID").items():
        monkeypatch.setenv(name, value)
    result = adapter.hr._codex_adapter("__ut_modality_other_binding", "task", ctx=selected)
    assert result["status"] == "ok", result
    call, = adapter.calls
    row, = call["catalog"]["models"]
    assert row["slug"] == selected.model and row["input_modalities"] == ["text"]
    assert call["kwargs"]["env"]["AIGC_APPID"] == selected.appid


@pytest.mark.parametrize("snapshot", [{}, {"LEGION_CODEX_TRANSPORT": "auto"}])
def test_modality_snapshot_missing_explicit_binding_never_reads_company_catalog(
        adapter, monkeypatch, snapshot):
    def forbidden(*args, **kwargs):
        raise AssertionError("explicit catalog snapshot must not use the company catalog")

    monkeypatch.setattr(adapter.mr, "_raw_candidate", forbidden)
    row, = adapter.hr._codex_bound_model_catalog(
        "fixture-bound-model", environment=snapshot)["models"]
    assert row["input_modalities"] == ["text"]


def test_modality_snapshot_older_registry_is_not_retried_without_environment(adapter, monkeypatch):
    calls = []

    def legacy_inputs(model):
        calls.append(model)
        return ["text", "image"]

    monkeypatch.setattr(adapter.mr, "input_modalities", legacy_inputs)
    snapshot = declared_environment(context(adapter), ["text"])
    row, = adapter.hr._codex_bound_model_catalog(
        "fixture-personal-model", environment=snapshot)["models"]
    assert row["input_modalities"] == ["text"] and calls == []
