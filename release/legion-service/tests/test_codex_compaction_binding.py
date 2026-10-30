"""原生压缩运行策略：假凭据、隔离目录，不调用真实模型或浏览器。"""
import json

import pytest

from test_personal_codex_model_catalog import (
    adapter, context, declared_environment, isolate, managed,
)


@pytest.fixture(autouse=True)
def compaction_isolation(isolate, monkeypatch, tmp_path):
    import harness_registry

    monkeypatch.delenv("LEGION_CODEX_RESPONSES_BINDING", raising=False)
    monkeypatch.delenv("FIXTURE_DECLARED_KEY", raising=False)
    monkeypatch.setattr(harness_registry._INPROC_RUNTIME_ENV, "values", {}, raising=False)
    catalog = tmp_path / "empty-model-catalog.json"
    catalog.write_text("{}")
    monkeypatch.setenv("MEYO_MODEL_CATALOG_PATH", str(catalog))


def declaration(ctx, **options):
    env = declared_environment(ctx, ["text"])
    raw = json.loads(env["LEGION_CODEX_RESPONSES_BINDING"])
    raw.update(options)
    env["LEGION_CODEX_RESPONSES_BINDING"] = json.dumps(raw)
    return env


def set_operator(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("limit", [1, 12345, 2**63 - 1])
def test_explicit_compaction_is_runtime_policy_not_provider_identity(adapter, limit):
    ctx = context(adapter)
    before = adapter.mr.explicit_responses_binding(declaration(ctx))
    after = adapter.mr.explicit_responses_binding(
        declaration(ctx, model_auto_compact_token_limit=limit))
    assert after == {**before, "model_auto_compact_token_limit": limit}
    assert adapter.mr.ccr_binding_key(
        before["model"], before["api_key"], before["base_url"]
    ) == adapter.mr.ccr_binding_key(
        after["model"], after["api_key"], after["base_url"])


@pytest.mark.parametrize("limit", [
    None, 0, -1, True, False, 1.0, "12345", [], {}, 2**63,
    "fake-catalog-provider-key",
])
@pytest.mark.parametrize("supplied_ctx", [False, True])
def test_invalid_compaction_is_not_silently_discarded(
        adapter, monkeypatch, limit, supplied_ctx):
    ctx = context(adapter)
    set_operator(monkeypatch, declaration(ctx, model_auto_compact_token_limit=limit))
    with pytest.raises(ValueError, match="model_auto_compact_token_limit") as error:
        adapter.mr.explicit_responses_binding()
    assert error.value.binding_option == "model_auto_compact_token_limit"
    monkeypatch.setattr(adapter.hr.HarnessCtx, "from_env", classmethod(lambda cls: ctx))
    result = adapter.hr._codex_adapter(
        "__ut_bad_compaction", "fixture", ctx=ctx if supplied_ctx else None)
    assert result["status"] == "unavailable"
    assert "model_auto_compact_token_limit" in result["error"]
    assert result["error_envelope"]["category"] == "model_config"
    assert not adapter.calls
    assert ctx.appid not in repr(result)


@pytest.mark.parametrize("is_managed", [False, True])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("source", ["process", "trusted_snapshot"])
def test_compaction_reaches_shared_native_arguments(
        adapter, monkeypatch, is_managed, resume, source):
    if is_managed:
        managed(adapter, monkeypatch)
    ctx = context(adapter, resume=resume)
    env = declaration(ctx, model_auto_compact_token_limit=12345,
                      input_modalities=["text", "image"], apply_patch_tool_type="freeform")
    if source == "process":
        set_operator(monkeypatch, env)
    else:
        set_operator(monkeypatch, declaration(ctx, model_auto_compact_token_limit=99999))
        monkeypatch.setattr(adapter.hr._INPROC_RUNTIME_ENV, "values", env)
    result = adapter.hr._codex_adapter("__ut_compaction_configured", "fixture", ctx=ctx)
    assert result["status"] == "ok"
    call, = adapter.calls
    assert call["config"]["model_auto_compact_token_limit"] == 12345
    assert "model_context_window" not in call["config"]
    row, = call["catalog"]["models"]
    assert "context_window" not in row and "auto_compact_token_limit" not in row
    assert row["input_modalities"] == ["text", "image"]
    assert row["apply_patch_tool_type"] == "freeform"
    assert row["slug"] == ctx.model
    command = call["command"]
    assert command.count("model_auto_compact_token_limit=12345") == 1
    provider = call["config"]["model_providers"][call["config"]["model_provider"]]
    assert provider["base_url"] == ctx.gateway and provider["wire_api"] == "responses"
    assert call["kwargs"]["env"][provider["env_key"]] == ctx.appid
    assert command[command.index("-s") + 1] == "workspace-write"
    assert ("resume" in command) is resume
    if resume:
        assert command[command.index("resume") + 1] == "fixture-native-thread"
    assert result["native_checkpoint"]["provider_binding"] == adapter.mr.ccr_binding_key(
        ctx.model, ctx.appid, ctx.gateway)
    assert result["native_facts"]["declared_context_compaction"] == {
        "model_auto_compact_token_limit": 12345,
        "source": "matched_operator_binding",
        "scope": "native_runtime_policy_not_model_capacity",
    }
    assert ctx.appid not in repr(command) + repr(result) + json.dumps(call["catalog"])
    assert not call["path"].exists()


@pytest.mark.parametrize("mismatch", ["model", "gateway", "appid"])
def test_compaction_does_not_cross_provider_binding(adapter, monkeypatch, mismatch):
    ctx = context(adapter)
    set_operator(monkeypatch, declaration(ctx, model_auto_compact_token_limit=12345))
    setattr(ctx, mismatch, {
        "model": "another-fixture-model",
        "gateway": "https://another.example.invalid/v1",
        "appid": "another-fixture-key",
    }[mismatch])
    result = adapter.hr._codex_adapter("__ut_compaction_wrong_binding", "fixture", ctx=ctx)
    assert result["status"] == "ok"
    assert "model_auto_compact_token_limit" not in adapter.calls[0]["config"]
    assert "declared_context_compaction" not in result["native_facts"]


def test_task_payload_cannot_enable_or_override_operator_compaction(adapter, monkeypatch):
    ctx = context(adapter)
    forged = declaration(ctx, model_auto_compact_token_limit=1)
    ctx.task_facts = {**forged, "model_auto_compact_token_limit": 1}
    ctx.project_context = {"model_auto_compact_token_limit": 1}
    ctx.model_auto_compact_token_limit = 1
    monkeypatch.setenv("MODEL_AUTO_COMPACT_TOKEN_LIMIT", "1")
    monkeypatch.setenv("CODEX_MODEL_AUTO_COMPACT_TOKEN_LIMIT", "1")
    first = adapter.hr._codex_adapter("__ut_compaction_task_only", "fixture", ctx=ctx)
    assert "model_auto_compact_token_limit" not in adapter.calls[0]["config"]
    assert "declared_context_compaction" not in first["native_facts"]
    set_operator(monkeypatch, declaration(ctx, model_auto_compact_token_limit=12345))
    second = adapter.hr._codex_adapter("__ut_compaction_operator_wins", "fixture", ctx=ctx)
    assert adapter.calls[1]["config"]["model_auto_compact_token_limit"] == 12345
    assert second["native_facts"]["declared_context_compaction"]["model_auto_compact_token_limit"] == 12345


def test_compaction_toggle_keeps_catalog_provider_permissions_and_native_session(adapter, monkeypatch):
    ctx = context(adapter)
    set_operator(monkeypatch, declaration(ctx))
    first = adapter.hr._codex_adapter("__ut_compaction_absent", "fixture", ctx=ctx)
    assert "declared_context_compaction" not in first["native_facts"]
    ctx.task_memory = {"native_checkpoint": first["native_checkpoint"]}
    set_operator(monkeypatch, declaration(ctx, model_auto_compact_token_limit=12345))
    second = adapter.hr._codex_adapter("__ut_compaction_enabled", "fixture", ctx=ctx)
    before, after = adapter.calls
    assert "model_auto_compact_token_limit" not in before["config"]
    assert before["catalog"] == after["catalog"]
    assert after["config"]["model_providers"] == before["config"]["model_providers"]
    assert after["config"]["sandbox_workspace_write"] == before["config"]["sandbox_workspace_write"]
    assert after["kwargs"]["cwd"] == before["kwargs"]["cwd"] == ctx.workdir
    assert first["native_checkpoint"] == second["native_checkpoint"]
    assert after["command"][after["command"].index("resume") + 1] == "fixture-native-thread"
    assert "declared_output_budget" not in second["native_facts"]


@pytest.mark.parametrize("is_managed", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_compaction_declaration_survives_native_failure_without_claiming_execution(
        adapter, monkeypatch, is_managed, failure):
    if is_managed:
        managed(adapter, monkeypatch)
    ctx = context(adapter, resume=True)
    set_operator(monkeypatch, declaration(ctx, model_auto_compact_token_limit=12345))
    if failure == "timeout":
        adapter.fail = True
    else:
        capture = adapter.hr._run_task

        def nonzero(*args, **kwargs):
            result = capture(*args, **kwargs)
            result.returncode = 7
            result.stderr = "fixture native failure"
            return result

        monkeypatch.setattr(adapter.hr, "_run_task", nonzero)
    result = adapter.hr._codex_adapter("__ut_compaction_native_failure", "fixture", ctx=ctx)
    assert result["status"] == ("timeout" if failure == "timeout" else "rc7")
    assert result["native_facts"]["declared_context_compaction"] == {
        "model_auto_compact_token_limit": 12345,
        "source": "matched_operator_binding",
        "scope": "native_runtime_policy_not_model_capacity",
    }
    assert result["native_checkpoint"]["session_id"] == "fixture-native-thread"
    assert not adapter.calls[0]["path"].exists()
    assert ctx.appid not in repr(result)


def test_managed_prepare_failure_only_records_validated_compaction_declaration(
        adapter, monkeypatch):
    import native_process_sandbox

    managed(adapter, monkeypatch)
    ctx = context(adapter)
    set_operator(monkeypatch, declaration(ctx, model_auto_compact_token_limit=12345))

    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture native preparation unavailable")

    monkeypatch.setattr(native_process_sandbox, "prepare_codex_native", unavailable)
    result = adapter.hr._codex_adapter("__ut_compaction_prepare_failure", "fixture", ctx=ctx)
    assert result["status"] == "error"
    assert not adapter.calls
    facts = result["native_facts"]
    assert facts["managed_native"]["native_cli_started"] is False
    assert facts["declared_context_compaction"] == {
        "model_auto_compact_token_limit": 12345,
        "source": "matched_operator_binding",
        "scope": "native_runtime_policy_not_model_capacity",
    }
    assert ctx.appid not in repr(result)


def test_compaction_alone_does_not_start_a_transport_bridge(adapter, monkeypatch):
    from meyo_harnesses.backends import omnigent_responses_bridge

    ctx = context(adapter)
    set_operator(monkeypatch, declaration(ctx, model_auto_compact_token_limit=12345))

    def forbidden_bridge(*args, **kwargs):
        raise AssertionError("native compaction must not start a transport bridge")

    monkeypatch.setattr(omnigent_responses_bridge, "transparent_bridge", forbidden_bridge)
    result = adapter.hr._codex_adapter("__ut_compaction_direct", "fixture", ctx=ctx)
    assert result["status"] == "ok"
    assert adapter.calls[0]["config"]["model_auto_compact_token_limit"] == 12345
