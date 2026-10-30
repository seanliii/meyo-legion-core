"""Phone-only command boundaries; fake paths, no devices, models or network."""
from __future__ import annotations

import copy
import importlib
import os
from pathlib import Path
import socket
import subprocess

import pytest

from test_native_device_permissions import ENV, HOME, STATE, TMP, WORK, common, profile, settings
from test_personal_codex_model_catalog import adapter, context, isolate


CONFIG = "/var/lib/meyo-legion/secrets/agentbay/config.json"
PRIVATE = str(Path(CONFIG).parent)
PHONE_ENV = {"MEYO_AGENTBAY_CONFIG": CONFIG}


@pytest.fixture(autouse=True)
def offline(isolate, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("MEYO_DEVICE_", "MEYO_DESKTOP_", "MEYO_AGENTBAY_", "MEYO_NATIVE_")):
            monkeypatch.delenv(name)

    def forbidden(*args, **kwargs):
        raise AssertionError("real network/device/process access in phone permission tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def restrict():
    return importlib.import_module("native_device_permissions").restrict_codex_device_control


def test_phone_only_converts_legacy_scope_without_requiring_docker_or_home(restrict):
    original, environment = common(), dict(PHONE_ENV)
    saved = copy.deepcopy(original)
    result = restrict(original, environment)
    assert result is not original, "phone config must activate the existing command boundary"
    assert original == saved and environment == PHONE_ENV
    fs = profile(result)["filesystem"]
    assert {path for path, mode in fs.items() if mode == "none"} == {PRIVATE}
    assert fs[WORK] == fs[STATE] == fs[":tmpdir"] == fs[":slash_tmp"] == "write"
    assert fs["/"] == "read" and profile(result)["network"] == {"enabled": True}
    assert HOME not in fs and not any("docker" in path for path in fs)
    for name in (".git", ".agents", ".codex"):
        assert fs[WORK + "/" + name] == "read"
    for name in ("model_provider", "model_providers", "mcp_servers", "approval_policy"):
        assert settings(result)[name] == settings(original)[name]
    assert "-s" not in result and "sandbox_workspace_write" not in settings(result)


def test_phone_adds_only_private_parent_to_existing_docker_boundary(restrict):
    docker = profile(restrict(common(), ENV))
    both = profile(restrict(common(), {**ENV, **PHONE_ENV}))
    assert both == {
        **docker, "filesystem": {**docker["filesystem"], PRIVATE: "none"},
    }


@pytest.mark.parametrize("excluded_tmpdir,excluded_slash", [
    (False, False), (False, True), (True, False), (True, True),
])
def test_phone_preserves_original_temporary_directory_permissions(
        restrict, excluded_tmpdir, excluded_slash):
    argv = common(
        "-c", f"sandbox_workspace_write.exclude_tmpdir_env_var={str(excluded_tmpdir).lower()}",
        "-c", f"sandbox_workspace_write.exclude_slash_tmp={str(excluded_slash).lower()}",
    )
    fs = profile(restrict(argv, {**PHONE_ENV, "TMPDIR": TMP}))["filesystem"]
    assert (":tmpdir" not in fs) is excluded_tmpdir
    assert (":slash_tmp" not in fs) is excluded_slash
    assert fs[PRIVATE] == "none"


@pytest.mark.parametrize("value", [
    "", "relative/config.json", "/", "/config.json", "//private/config.json",
    "/private/../config.json", "/private/./config.json", "/private//config.json",
    "/private/config.json/", "/private/*/config.json", "/private/%61/config.json",
    "/private/config.json\nsecret", "https://provider.invalid/?APIKEY=fixture",
])
def test_invalid_config_path_fails_instead_of_silently_dropping_protection(restrict, value):
    with pytest.raises(ValueError) as error:
        restrict(common(), {"MEYO_AGENTBAY_CONFIG": value})
    assert value not in str(error.value) if len(value) > 1 else True


@pytest.mark.parametrize("extra", [
    ["--permission-profile", "already-managed"],
    ["-P", "already-managed"],
    ["--sandbox", "danger-full-access"],
    ["-c", 'default_permissions="already-managed"'],
    ["-c", "features.use_linux_sandbox_bwrap=false"],
])
def test_phone_does_not_translate_new_or_managed_linux_permission_shapes(restrict, extra):
    with pytest.raises(ValueError):
        restrict(common(*extra), PHONE_ENV)


@pytest.mark.parametrize("path", [
    PRIVATE, PRIVATE + "/control", PRIVATE + "/api-key.txt", PRIVATE + "/config.json",
])
def test_explicit_write_root_cannot_reopen_any_private_phone_material(restrict, path):
    with pytest.raises(ValueError, match="reopen"):
        restrict(common(roots=[STATE, path]), PHONE_ENV)


def test_private_temp_write_is_rejected_but_excluding_it_preserves_no_access(restrict):
    environment = {**PHONE_ENV, "TMPDIR": PRIVATE + "/control/tmp"}
    with pytest.raises(ValueError, match="reopen"):
        restrict(common(), environment)
    argv = common("-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true")
    assert profile(restrict(argv, environment))["filesystem"][PRIVATE] == "none"


def test_phone_transform_never_reads_config_or_key_files(restrict, monkeypatch):
    def no_read(*args, **kwargs):
        raise AssertionError("argv conversion must not read phone configuration or credentials")

    monkeypatch.setattr(Path, "open", no_read)
    monkeypatch.setattr(Path, "resolve", no_read)
    assert profile(restrict(common(), PHONE_ENV))["filesystem"][PRIVATE] == "none"


@pytest.mark.parametrize("resume", [False, True])
def test_actual_codex_adapter_activates_phone_only_boundary_without_new_routing(
        adapter, monkeypatch, resume):
    private = adapter.root / "secrets/agentbay"
    # The adapter/argv conversion uses a path, not credential contents.
    monkeypatch.setenv("MEYO_AGENTBAY_CONFIG", str(private / "config.json"))
    ctx = context(adapter, resume=resume)
    result = adapter.hr._codex_adapter("__ut_agentbay_native_scope", "original task", ctx=ctx)
    assert result["status"] == "ok", result
    call, = adapter.calls
    config = call["config"]
    selected = config.get("default_permissions")
    assert selected, "actual production adapter did not activate phone-only restriction"
    fs = config["permissions"][selected]["filesystem"]
    assert fs[str(private)] == "none"
    assert fs[ctx.workdir] == "write"
    assert {path for path, mode in fs.items() if mode == "none"} == {str(private)}
    assert call["kwargs"]["env"]["MEYO_AGENTBAY_CONFIG"] == str(private / "config.json")
    assert "MEYO_DEVICE_DOCKER_HOST" not in call["kwargs"]["env"]
    assert ("resume" in call["command"]) is resume
    assert call["catalog"]["models"][0]["slug"] == ctx.model
    assert call["kwargs"]["input"].startswith("original task\n")
    assert ctx.appid not in repr(config) + repr(result)
