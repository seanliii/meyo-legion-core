"""Offline source/native argv contracts; no native executable or model runs."""
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("MEYO_", "LEGION_")) or name == "CODEX_HOME":
            monkeypatch.delenv(name)
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "TMPDIR"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        raise AssertionError("offline sandbox test must not execute or connect")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    store = tmp_path / "store"
    runtime = store / "generation/runtime"
    binary = runtime / "node-tools/bin/codex"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 97\n")
    binary.chmod(0o755)
    app = tmp_path / "app"
    source = app / "source"
    source.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    tokens = control / "tokens.json"
    tokens.write_text(json.dumps({"fixture-token": {"user": "alice", "project_grants": [
        {"root": str(source), "export_paths": [], "source_write": True}]}}))
    tokens.chmod(0o600)
    monkeypatch.setenv("LEGION_PERSONAL_TOKENS_FILE", str(tokens))
    env = {"MEYO_RUNTIME_ROOT": str(runtime), "MEYO_RUNTIME_STORE": str(store),
           "HOME": os.environ["HOME"], "MEYO_USER_ID": "alice", "PATH": "/usr/bin:/bin"}
    return dict(store=store, runtime=runtime, binary=binary, app=app, source=source,
                state=state, tokens=tokens, env=env)


def sandbox():
    return importlib.import_module("native_process_sandbox")


def prepare(scene, **kwargs):
    return sandbox().prepare_native_process(scene["env"], scene["source"], "codex",
        [scene["runtime"], scene["app"]], [scene["state"]], **kwargs)


@pytest.mark.parametrize("native", [False, True])
def test_exact_source_authority_and_live_launch_revocation(scene, native):
    context = (sandbox().prepare_codex_native(scene["env"], scene["source"],
               [scene["runtime"], scene["app"]], [scene["state"]]) if native else prepare(scene))
    with context as launch:
        assert launch.source_write == {"user_id": "alice", "root": str(scene["source"]),
                                       "source_write": True}
        assert launch.wrap(["/native/member", "--resume", "fixture"])
        assert "fixture-token" not in repr(launch)
        scene["tokens"].write_text("{}")
        with pytest.raises(sandbox().NativeProcessSandboxError, match="revoked"):
            launch.wrap(["/native/member"])
    with pytest.raises(sandbox().NativeProcessSandboxError, match="closed"):
        launch.wrap(["/native/member"])
    assert not launch.config_dir.exists()


@pytest.mark.parametrize("permission", [False, "true", 1, None])
def test_ungranted_source_does_not_relax_broad_protection(scene, permission):
    records = json.loads(scene["tokens"].read_text())
    records["fixture-token"]["project_grants"][0]["source_write"] = permission
    scene["tokens"].write_text(json.dumps(records))
    with pytest.raises(sandbox().NativeProcessSandboxError, match="overlap"):
        with prepare(scene):
            pytest.fail("not authorized")


def test_source_cannot_relax_other_state_or_protected_descendant(scene):
    protected = scene["source"] / "protected-runtime"
    protected.mkdir()
    with pytest.raises(sandbox().NativeProcessSandboxError, match="separately protected"):
        sandbox().source_workspace_grant(scene["env"], scene["source"], [protected])
    with pytest.raises(sandbox().NativeProcessSandboxError, match="overlap"):
        sandbox()._validate_write_scope(scene["env"], scene["source"],
                                       [scene["app"]], [scene["source"], scene["app"]])


def test_read_only_public_native_interface_remains_read_only(scene):
    with sandbox().prepare_codex_native(scene["env"], scene["source"],
            [scene["runtime"], scene["app"]], [scene["state"]], read_only=True) as launch:
        assert launch.native_args == ("-s", "read-only")
        assert launch.source_write is None
        assert launch.env["HOME"] == scene["env"]["HOME"]


def test_explicit_host_binary_and_network_restriction(scene):
    host = scene["store"] / "host-generation/bin"
    host.mkdir(parents=True)
    binary = host / "codex"
    binary.write_text("#!/bin/sh\nexit 97\n")
    binary.chmod(0o755)
    with sandbox().prepare_native_process(scene["env"], scene["source"], "codex",
            [host, scene["app"]], [scene["state"]], sandbox_executable=binary,
            network_access=False) as launch:
        argv = launch.wrap(["/native/build"])
        assert argv[:2] == [str(binary), "sandbox"]
        assert "sandbox_workspace_write.network_access=false" in argv
    alias = host / "alias"
    alias.symlink_to(binary)
    with pytest.raises(sandbox().NativeProcessSandboxError):
        sandbox()._sandbox_binary(alias, [scene["source"]])
    with pytest.raises(sandbox().NativeProcessSandboxError):
        sandbox()._sandbox_binary(binary, [host])


@pytest.mark.parametrize("kwargs", [{"network_access": "false"}, {"sandbox_executable": Path("/bin/sh")}])
def test_host_arguments_fail_closed_without_backend(scene, kwargs):
    with pytest.raises(sandbox().NativeProcessSandboxError):
        with sandbox().prepare_native_process(scene["env"], scene["source"], **kwargs):
            pytest.fail("invalid host input")


def test_disabled_backend_preserves_native_home_and_credentials(scene):
    env = {**scene["env"], "ANTHROPIC_AUTH_TOKEN": "fixture-secret"}
    with sandbox().prepare_native_process(env, scene["source"]) as launch:
        assert launch.env == env
        assert launch.wrap(["/native/member", "--bare"]) == ["/native/member", "--bare"]
        assert not list(launch.config_dir.iterdir())


@pytest.mark.parametrize("value", ["", "relative.json", " /tmp/catalog.json", "/tmp/a/../catalog.json", "/tmp/a\n"])
def test_external_catalog_invalid_host_override(value):
    paths = importlib.import_module("legion_paths")
    with pytest.raises(ValueError):
        paths.external_catalog_path(environment={"LEGION_EXTERNAL_CATALOG_PATH": value})


def test_external_catalog_default_and_alias_contract(tmp_path):
    paths = importlib.import_module("legion_paths")
    path = tmp_path / "catalog.json"
    path.write_text("{}")
    assert paths.external_catalog_path(path, environment={}) == path
    assert paths.external_catalog_path(environment={"LEGION_EXTERNAL_CATALOG_PATH": str(path)}) == path
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(ValueError):
        paths.external_catalog_path(environment={"LEGION_EXTERNAL_CATALOG_PATH": str(alias)})
