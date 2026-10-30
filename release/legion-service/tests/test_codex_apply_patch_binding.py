"""Operator-only native patch declaration; fake bindings, no model or process."""
import hashlib
import json

import pytest

from test_personal_codex_model_catalog import (
    FALLBACK_SHA256, adapter, context, declared_environment, isolate, managed,
)


@pytest.fixture(autouse=True)
def patch_isolation(isolate, monkeypatch, tmp_path):
    import harness_registry

    monkeypatch.delenv("LEGION_CODEX_RESPONSES_BINDING", raising=False)
    monkeypatch.delenv("FIXTURE_DECLARED_KEY", raising=False)
    monkeypatch.setattr(harness_registry._INPROC_RUNTIME_ENV, "values", {}, raising=False)
    empty_catalog = tmp_path / "empty-model-catalog.json"
    empty_catalog.write_text("{}")
    monkeypatch.setenv("MEYO_MODEL_CATALOG_PATH", str(empty_catalog))


def declaration(ctx, *, patch="freeform", **options):
    env = declared_environment(ctx, ["text"])
    binding = json.loads(env["LEGION_CODEX_RESPONSES_BINDING"])
    binding.update(options, apply_patch_tool_type=patch)
    env["LEGION_CODEX_RESPONSES_BINDING"] = json.dumps(binding)
    return env


def set_operator(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_existing_validator_retains_only_explicit_native_patch_type(adapter):
    ctx = context(adapter)
    ordinary = declared_environment(ctx, ["text"])
    before = adapter.mr.explicit_responses_binding(ordinary)
    after = adapter.mr.explicit_responses_binding(declaration(ctx))
    assert "apply_patch_tool_type" not in before
    assert after == {**before, "apply_patch_tool_type": "freeform"}
    assert adapter.mr.ccr_binding_key(
        before["model"], before["api_key"], before["base_url"]
    ) == adapter.mr.ccr_binding_key(
        after["model"], after["api_key"], after["base_url"]
    )


@pytest.mark.parametrize("value", [None, "", "function", "FREEFORM", True, [], {}])
def test_invalid_patch_declaration_is_rejected_before_native(adapter, monkeypatch, value):
    ctx = context(adapter)
    env = declaration(ctx, patch=value)
    set_operator(monkeypatch, env)
    with pytest.raises(ValueError, match="apply_patch_tool_type"):
        adapter.mr.explicit_responses_binding()
    result = adapter.hr._codex_adapter("__ut_bad_native_patch", "fixture", ctx=ctx)
    assert result["status"] == "unavailable"
    assert not adapter.calls
    assert ctx.appid not in repr(result)


@pytest.mark.parametrize("is_managed", [False, True])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("source", ["process", "trusted_snapshot"])
def test_explicit_patch_reaches_local_managed_and_resume_without_changing_identity(
        adapter, monkeypatch, is_managed, resume, source):
    if is_managed:
        managed(adapter, monkeypatch)
    ctx = context(adapter, resume=resume)
    env = declaration(ctx)
    if source == "process":
        set_operator(monkeypatch, env)
    else:
        # Per-call operator snapshot wins over unrelated ambient process state.
        set_operator(monkeypatch, declared_environment(ctx, ["text"]))
        monkeypatch.setattr(adapter.hr._INPROC_RUNTIME_ENV, "values", env)
    result = adapter.hr._codex_adapter("__ut_native_patch_catalog", "fixture", ctx=ctx)
    assert result["status"] == "ok"
    call, = adapter.calls
    row, = call["catalog"]["models"]
    assert row["apply_patch_tool_type"] == "freeform"
    assert row["slug"] == ctx.model
    assert hashlib.sha256(row["base_instructions"].encode()).hexdigest() == FALLBACK_SHA256
    assert row["input_modalities"] == ["text"]
    assert row["experimental_supported_tools"] == []
    config, command = call["config"], call["command"]
    provider = config["model_providers"][config["model_provider"]]
    assert provider["base_url"] == ctx.gateway and provider["wire_api"] == "responses"
    assert call["kwargs"]["env"][provider["env_key"]] == ctx.appid
    assert call["kwargs"]["cwd"] == ctx.workdir
    assert command[command.index("-s") + 1] == "workspace-write"
    assert ("resume" in command) is resume
    if resume:
        assert command[command.index("resume") + 1] == "fixture-native-thread"
    assert result["native_checkpoint"]["session_id"] == "fixture-native-thread"
    assert result["native_checkpoint"]["provider_binding"] == adapter.mr.ccr_binding_key(
        ctx.model, ctx.appid, ctx.gateway)
    assert "declared_output_budget" not in result["native_facts"]
    assert not call["path"].exists()
    assert ctx.appid not in repr(command) + repr(result) + json.dumps(call["catalog"])


@pytest.mark.parametrize("is_managed", [False, True])
@pytest.mark.parametrize("resume", [False, True])
def test_task_payload_cannot_self_declare_patch_capability(adapter, monkeypatch, is_managed, resume):
    if is_managed:
        managed(adapter, monkeypatch)
    ctx = context(adapter, resume=resume)
    env = declaration(ctx)
    ctx.task_facts = {
        "apply_patch_tool_type": "freeform",
        "native_capabilities": {"apply_patch_tool_type": "freeform"},
        **env,
    }
    ctx.project_context = {"apply_patch_tool_type": "freeform"}
    result = adapter.hr._codex_adapter("__ut_untrusted_patch_payload", "fixture", ctx=ctx)
    assert result["status"] == "ok"
    row, = adapter.calls[0]["catalog"]["models"]
    assert "apply_patch_tool_type" not in row


@pytest.mark.parametrize("mismatch", ["model", "gateway", "appid"])
def test_patch_declaration_does_not_transfer_to_another_binding(adapter, monkeypatch, mismatch):
    ctx = context(adapter)
    set_operator(monkeypatch, declaration(ctx))
    setattr(ctx, mismatch, {
        "model": "another-fixture-model",
        "gateway": "https://another.example.invalid/v1",
        "appid": "different-fixture-key",
    }[mismatch])
    result = adapter.hr._codex_adapter("__ut_patch_binding_mismatch", "fixture", ctx=ctx)
    assert result["status"] == "ok"
    row, = adapter.calls[0]["catalog"]["models"]
    assert "apply_patch_tool_type" not in row


def test_patch_toggle_preserves_catalog_defaults_permissions_and_resume(adapter, monkeypatch):
    ctx = context(adapter)
    set_operator(monkeypatch, declared_environment(ctx, ["text"]))
    first = adapter.hr._codex_adapter("__ut_patch_before", "fixture", ctx=ctx)
    ctx.task_memory = {"native_checkpoint": first["native_checkpoint"]}
    set_operator(monkeypatch, declaration(ctx))
    second = adapter.hr._codex_adapter("__ut_patch_after", "fixture", ctx=ctx)
    before, after = adapter.calls
    expected = dict(before["catalog"]["models"][0], apply_patch_tool_type="freeform")
    assert after["catalog"]["models"] == [expected]
    assert after["config"]["sandbox_workspace_write"] == before["config"]["sandbox_workspace_write"]
    assert after["config"]["model_providers"] == before["config"]["model_providers"]
    assert after["kwargs"]["cwd"] == before["kwargs"]["cwd"] == ctx.workdir
    assert second["native_checkpoint"] == first["native_checkpoint"]
    assert after["command"][after["command"].index("resume") + 1] == "fixture-native-thread"


def test_patch_field_does_not_change_validated_budget_or_input_fields(adapter):
    ctx = context(adapter)
    env = declaration(ctx, max_output_tokens=4096, stream_idle_timeout_ms=600000)
    with_patch = adapter.mr.explicit_responses_binding(env)
    raw = json.loads(env["LEGION_CODEX_RESPONSES_BINDING"])
    raw.pop("apply_patch_tool_type")
    env["LEGION_CODEX_RESPONSES_BINDING"] = json.dumps(raw)
    without_patch = adapter.mr.explicit_responses_binding(env)
    assert with_patch == {**without_patch, "apply_patch_tool_type": "freeform"}
    assert with_patch["max_output_tokens"] == 4096
    assert with_patch["stream_idle_timeout_ms"] == 600000
