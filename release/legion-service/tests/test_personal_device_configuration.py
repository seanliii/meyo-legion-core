"""The personal runtime persists only explicit, owner-bound device connections."""
import importlib
import json
import os
from pathlib import Path
import socket

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "CODEX_HOME"):
        folder = tmp_path / name
        folder.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(folder))

    def offline(*_args, **_kwargs):
        raise AssertionError("configuration does not probe networks or devices")

    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket.socket, "connect_ex", offline)


@pytest.fixture
def runtime():
    return importlib.import_module("personal_runtime")


@pytest.fixture
def instance(runtime, tmp_path):
    root = tmp_path / "instance"
    runtime.initialize(root, "device-owner", 18080)
    return root


def own_host():
    return f"unix:///run/user/{os.getuid()}/docker.sock"


def test_device_config_is_explicit_persistent_and_does_not_change_identity(runtime, instance):
    token = (instance / "login-token.txt").read_bytes()
    output = runtime.configure_devices(instance, own_host(), 16080)
    env, config = runtime.environment(instance, {
        "PATH": "/usr/bin:/bin",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "MEYO_DESKTOP_OWNER": "somebody-else",
        "MEYO_DEVICE_DOCKER_HOST": "tcp://remote.invalid:2375",
    })
    assert env["MEYO_DEVICE_DOCKER_HOST"] == own_host()
    assert env["MEYO_DEVICE_DOCKER_DATA_ROOT"] == str(instance / "home/.local/share/docker")
    assert env["MEYO_DESKTOP_OWNER"] == "device-owner"
    assert env["MEYO_DESKTOP_PUBLISH_PORT"] == "16080"
    assert env["MEYO_DESKTOP_PUBLIC_PATH"] == "/cloud-desktop"
    assert "DOCKER_HOST" not in env
    assert config["devices"]["docker_host"] == own_host()
    assert runtime.environment(instance, {})[0]["MEYO_DEVICE_DOCKER_HOST"] == own_host()
    assert (instance / "login-token.txt").read_bytes() == token
    assert output["runtime_verified"] is False
    assert token.decode().strip() not in repr(output)


def test_unconfigured_instance_does_not_inherit_device_management(runtime, instance):
    env, _ = runtime.environment(instance, {
        "DOCKER_HOST": own_host(), "MEYO_DEVICE_DOCKER_HOST": own_host(),
        "MEYO_DESKTOP_PUBLISH_PORT": "16080", "MEYO_DESKTOP_OWNER": "other",
    })
    assert not any(k.startswith("MEYO_DESKTOP_") for k in env)
    assert "MEYO_DEVICE_DOCKER_HOST" not in env
    assert "DOCKER_HOST" not in env


@pytest.mark.parametrize("host,port,path", [
    ("tcp://127.0.0.1:2375", 16080, "/cloud-desktop"),
    ("unix:///var/run/docker.sock", 16080, "/cloud-desktop"),
    ("unix:///run/user/999999/docker.sock", 16080, "/cloud-desktop"),
    ("unix:///run/user/../docker.sock", 16080, "/cloud-desktop"),
    (None, 80, "/cloud-desktop"),
    (None, 65536, "/cloud-desktop"),
    (None, True, "/cloud-desktop"),
    (None, 16080, "https://other.invalid/"),
    (None, 16080, "/api/legion"),
    (None, 16080, "/s"),
    (None, 16080, "/cloud-desktop?upstream=other"),
    (None, 16080, "/cloud-desktop/../shared"),
])
def test_invalid_connection_cannot_replace_existing_config(runtime, instance, host, port, path):
    runtime.configure_devices(instance, own_host(), 16080)
    before = (instance / "config.json").read_bytes()
    with pytest.raises(ValueError):
        runtime.configure_devices(instance, host or own_host(), port, path)
    assert (instance / "config.json").read_bytes() == before


def test_hand_edited_invalid_connection_fails_without_rootful_fallback(runtime, instance):
    config = json.loads((instance / "config.json").read_text())
    config["devices"] = {
        "docker_host": "unix:///var/run/docker.sock", "desktop_port": 16080,
        "desktop_public_path": "/cloud-desktop",
    }
    (instance / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError):
        runtime.environment(instance, {"DOCKER_HOST": own_host()})


def test_device_cli_preserves_callers_environment(runtime, instance, capsys):
    before = dict(os.environ)
    previous_mask = os.umask(0o077)
    try:
        assert runtime.main([
            "configure-devices", "--state-dir", str(instance),
            "--docker-host", own_host(), "--desktop-port", "16080",
        ]) == 0
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["owner"] == "device-owner"
        assert receipt["runtime_verified"] is False
        assert os.environ == before
    finally:
        os.umask(previous_mask)
