"""Personal desktop wiring: real status/auth code, fake devices and local HTTP only."""
from contextlib import nullcontext
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest

from test_public_origin_entry import isolated, http_entry


AUTH_PATH = "/api/legion/cloud_desktop/display/auth"
PUBLIC_URL = (
    "/cloud-desktop/vnc.html?autoconnect=true&resize=scale"
    "&path=cloud-desktop/websockify"
)
DEVICE_KEYS = (
    "MEYO_DEVICE_DOCKER_HOST", "MEYO_DESKTOP_PUBLISH_PORT",
    "MEYO_DESKTOP_PUBLIC_PATH", "MEYO_DESKTOP_OWNER",
)


def device_host():
    return f"unix:///run/user/{os.getuid()}/docker.sock"


def configure(monkeypatch, owner="invited-test"):
    for key, value in zip(DEVICE_KEYS, (device_host(), "16080", "/cloud-desktop", owner)):
        monkeypatch.setenv(key, value)


def docker_info(security_options=None, data_root="/fixture/home/.local/share/docker"):
    return json.dumps({
        "server_version": "29.1.3",
        "security_options": (["name=rootless", "name=seccomp,profile=builtin", "name=cgroupns"]
                             if security_options is None else security_options),
        "docker_root_dir": data_root,
    })


@pytest.fixture(autouse=True)
def case(isolated, monkeypatch):
    safe_env = {
        "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "PYTHONDONTWRITEBYTECODE",
        "HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_SESSIONS_DIR",
        "LEGION_TASK_MEMORY_DIR", "LEGION_TEAM_DIR", "LEGION_PROJECT_CONTEXT_DIR",
        "LEGION_PROC_REGISTRY_DIR", "LEGION_ALLOW_REMOTE_BIND",
        "LEGION_ALLOW_UNAUTHENTICATED_LOCAL_CLI", "LEGION_EXT_AUTO_UPDATE",
    }
    for key in list(os.environ):
        if key not in safe_env:
            monkeypatch.delenv(key)
    monkeypatch.setenv("CODEX_HOME", str(isolated.root / "codex-home"))
    monkeypatch.setenv("TMPDIR", str(isolated.root / "tmp"))
    for key in (*DEVICE_KEYS, "DOCKER_HOST", "DOCKER_CONTEXT"):
        monkeypatch.delenv(key, raising=False)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("real process/device/model execution is forbidden")

    connect = socket.create_connection

    def local_only(address, *args, **kwargs):
        assert address[0] in ("127.0.0.1", "::1"), "remote network is forbidden"
        return connect(address, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", local_only)
    desktop = importlib.import_module("cloud_desktop")
    monkeypatch.setattr(desktop, "_command", forbidden)
    monkeypatch.setattr(desktop, "_STATUS_CACHE", {})
    monkeypatch.setattr(desktop, "_STATUS_REFRESHING", set())
    monkeypatch.setattr(desktop, "_COMMON_CACHE", None)
    return SimpleNamespace(desktop=desktop, monkeypatch=monkeypatch, forbidden=forbidden)


@pytest.fixture
def device(case):
    desktop, patch = case.desktop, case.monkeypatch
    fs = importlib.import_module("desktop_fs")
    state = SimpleNamespace(port=16080, exists=True, commands=[], probes=[])
    docker = {"cli_present": True, "daemon_ready": True,
              "docker_host": device_host(), "env": {}}
    common = {
        "docker": docker,
        "image": {"available": True, "command": {}},
        "control": {"ready": True, "cua": {}, "serve_mcp": {}},
    }

    def container(uid="default", docker=None):
        return {
            "name": desktop._cua_name(uid), "exists": state.exists,
            "verified": state.exists, "running": state.exists, "status": "running",
            "host_port": state.port, "meyo_mount": os.path.realpath(fs.user_meyo_root(uid)),
        }

    def probe(port, **_kwargs):
        state.probes.append(port)
        return {"ok": True, "url": f"http://127.0.0.1:{port}/vnc.html"}

    def command(args, **_kwargs):
        state.commands.append(list(args))
        assert args[0] == "run", f"unexpected lifecycle operation: {args}"
        published = args[args.index("--publish") + 1]
        state.port = int(published.split(":")[-2] or "49155")
        state.exists = True
        return {"ok": True, "exit_code": 0, "stdout": "fixture-container"}

    patch.setattr(desktop, "_docker_env", lambda: docker)
    patch.setattr(desktop, "_common_snapshot", lambda **_kwargs: common)
    patch.setattr(desktop, "_container_state", container)
    patch.setattr(desktop, "_probe_novnc", probe)
    patch.setattr(desktop, "_image_status", lambda _docker: {
        "available": True, "image": desktop.CUA_XFCE_IMAGE})
    patch.setattr(desktop, "_docker_run", command)
    patch.setattr(fs, "bootstrap", lambda _uid: {"ok": True})
    # Keep the real status computation; no asynchronous cache refresh can outlive a test.
    status = desktop.status
    patch.setattr(desktop, "status", lambda uid="default", force=False: status(uid, force=True))
    state.desktop = desktop
    state.monkeypatch = patch
    return state


def test_status_session_and_tool_summary_share_the_protected_public_url(device):
    configure(device.monkeypatch)
    status = device.desktop.status("invited-test")
    assert status["desktop"]["display_url"] == PUBLIC_URL
    assert device.desktop.session_open("invited-test")["session_url"] == PUBLIC_URL
    assert device.desktop._local_mcp_summary("invited-test")["display_url"] == PUBLIC_URL
    assert status["desktop"]["probe"]["url"] == "http://127.0.0.1:16080/vnc.html"
    assert status["desktop"]["connection"]["host"] == "127.0.0.1"
    assert status["ready"] is True


def test_other_user_never_gets_the_fixed_desktop_url(device):
    configure(device.monkeypatch)
    status = device.desktop.status("another-user")
    assert status["desktop"]["display_url"] == ""
    assert status["ready"] is False
    assert not device.desktop.session_open("another-user")["ok"]
    assert device.desktop._local_mcp_summary("another-user")["display_url"] == ""


def test_real_mapped_port_must_match_the_configured_public_target(device):
    configure(device.monkeypatch)
    device.port = 16081
    status = device.desktop.status("invited-test")
    assert status["desktop"]["display_url"] == ""
    assert status["ready"] is False
    assert status["desktop"]["probe"]["url"] == "http://127.0.0.1:16081/vnc.html"
    assert not device.desktop.session_open("invited-test")["ok"]


@pytest.mark.parametrize("key,value", [
    ("MEYO_DESKTOP_OWNER", ""),
    ("MEYO_DESKTOP_PUBLIC_PATH", "/api"),
    ("MEYO_DESKTOP_PUBLIC_PATH", "/s"),
    ("MEYO_DESKTOP_PUBLIC_PATH", "/cloud-desktop/"),
    ("MEYO_DESKTOP_PUBLISH_PORT", "0"),
    ("MEYO_DEVICE_DOCKER_HOST", "unix:///var/run/docker.sock"),
    ("MEYO_DEVICE_DOCKER_HOST", None),
])
def test_partial_or_invalid_configuration_never_falls_back_to_a_localhost_link(device, key, value):
    configure(device.monkeypatch)
    if value is None:
        device.monkeypatch.delenv(key)
    else:
        device.monkeypatch.setenv(key, value)
    result = device.desktop.status("invited-test")
    assert result["desktop"]["display_url"] == ""
    assert result["ready"] is False
    assert not device.desktop.session_open("invited-test")["ok"]


def test_unconfigured_local_desktop_keeps_its_legacy_dynamic_url(device):
    device.port = 49155
    status = device.desktop.status("local-user")
    expected = "http://127.0.0.1:49155/vnc.html?autoconnect=true&resize=scale"
    assert status["desktop"]["display_url"] == expected
    assert device.desktop.session_open("local-user")["session_url"] == expected
    assert status["ready"] is True


def test_http_readiness_probe_stays_on_the_actual_loopback_port(case):
    configure(case.monkeypatch)
    requests = []

    def read(request, timeout):
        requests.append((request.full_url, timeout))
        return nullcontext(SimpleNamespace(status=200, headers={"Content-Type": "text/html"}))

    case.monkeypatch.setattr(case.desktop.urllib.request, "urlopen", read)
    assert case.desktop._probe_novnc(16081)["ok"]
    assert requests == [("http://127.0.0.1:16081/vnc.html", 3)]


@pytest.mark.parametrize("personal,publish", [
    (True, "127.0.0.1:16080:6080"),
    (False, "127.0.0.1::6080"),
])
def test_launch_uses_the_fixed_port_only_when_personally_configured(device, personal, publish):
    if personal:
        configure(device.monkeypatch)
    device.exists = False
    result = device.desktop.start_session("invited-test")
    assert result["ok"], result
    assert len(device.commands) == 1
    command = device.commands[0]
    assert command[command.index("--publish") + 1] == publish
    assert command[command.index("--label") + 1] == "cua.sandbox=true"
    assert command[command.index("--volume") + 1].endswith(":/root/Meyo")


def test_other_owner_cannot_launch_a_container_on_the_fixed_port(device):
    configure(device.monkeypatch)
    device.exists = False
    result = device.desktop.start_session("another-user")
    assert not result["ok"]
    assert device.commands == []


def test_existing_wrong_port_is_reported_without_recreating_the_container(device):
    configure(device.monkeypatch)
    device.port = 16081
    result = device.desktop.start_session("invited-test")
    assert not result["ok"]
    assert device.commands == []


def test_bound_tool_owner_is_not_replaced_by_its_storage_directory_id(device):
    configure(device.monkeypatch, owner="owner.with-dot")
    result = device.desktop.local_mcp_call("desktop_status", {}, bound_uid="owner.with-dot")
    assert result["result"]["display_url"] == PUBLIC_URL


@pytest.mark.parametrize("reachable", [False, True])
def test_explicit_device_socket_is_the_only_probe_even_with_rootful_defaults(case, reachable):
    patch, desktop = case.monkeypatch, case.desktop
    patch.setenv("MEYO_DEVICE_DOCKER_HOST", device_host())
    patch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    patch.setenv("DOCKER_CONTEXT", "rootful-context")
    patch.setattr(desktop.shutil, "which", lambda _name: "/fixture/docker")
    patch.setattr(desktop.os.path, "exists", lambda _path: True)
    calls = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"ok": reachable, "stdout": docker_info() if reachable else "", "stderr": "unavailable"}

    patch.setattr(desktop, "_command", command)
    result = desktop._docker_env()
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[:3] == ["/fixture/docker", "--host", device_host()]
    assert options["env"]["DOCKER_HOST"] == device_host()
    assert "DOCKER_CONTEXT" not in options["env"]
    assert options["timeout"] == 5
    assert result["daemon_ready"] is reachable


@pytest.mark.parametrize("host", ["", "tcp://remote.invalid:2375", "unix:///var/run/docker.sock"])
def test_invalid_explicit_device_socket_never_probes_a_default_daemon(case, host):
    case.monkeypatch.setenv("MEYO_DEVICE_DOCKER_HOST", host)
    case.monkeypatch.setattr(case.desktop.shutil, "which", lambda _name: "/fixture/docker")
    case.monkeypatch.setattr(case.desktop.os.path, "exists", lambda _path: True)
    assert case.desktop._docker_env()["daemon_ready"] is False


def test_legacy_docker_host_resolution_is_unchanged_without_device_configuration(case):
    case.monkeypatch.setenv("DOCKER_HOST", "unix:///legacy/colima/docker.sock")
    case.monkeypatch.setattr(case.desktop.shutil, "which", lambda _name: "/fixture/docker")
    case.monkeypatch.setattr(case.desktop.os.path, "exists", lambda _path: False)
    calls = []
    case.monkeypatch.setattr(case.desktop, "_command", lambda argv, **kw: (
        calls.append((argv, kw)) or {"ok": True, "stdout": "legacy-daemon"}))
    result = case.desktop._docker_env()
    assert result["source"] == "existing_docker_host"
    assert calls[0][1]["env"]["DOCKER_HOST"] == "unix:///legacy/colima/docker.sock"


def test_device_commands_keep_the_selected_socket_after_the_probe(case):
    patch, desktop = case.monkeypatch, case.desktop
    patch.setenv("MEYO_DEVICE_DOCKER_HOST", device_host())
    patch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    patch.setenv("DOCKER_CONTEXT", "rootful-context")
    patch.setattr(desktop.shutil, "which", lambda _name: "/fixture/docker")
    calls = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        if "version" in argv or "info" in argv:
            return {"ok": True, "stdout": docker_info()}
        return {"ok": False, "stderr": "selected daemon stopped"}

    patch.setattr(desktop, "_command", command)
    result = desktop._docker_run(["container", "inspect", "fixture-desktop"])
    assert not result["ok"]
    assert len(calls) == 2
    for argv, options in calls:
        assert argv[:3] == ["/fixture/docker", "--host", device_host()]
        assert options["env"]["DOCKER_HOST"] == device_host()
        assert "DOCKER_CONTEXT" not in options["env"]


def test_display_auth_uses_existing_personal_identity_without_probing_devices(case, http_entry):
    configure(case.monkeypatch)
    case.monkeypatch.setattr(case.desktop, "status", case.forbidden)
    case.monkeypatch.setattr(case.desktop, "_docker_env", case.forbidden)
    status, _headers, body = http_entry.request("GET", AUTH_PATH, origin=None)
    assert status == 200 and body["ok"] is True
    assert http_entry.seen == []


@pytest.mark.parametrize("configuration,authenticated", [
    ("wrong-owner", True), ("forged-owner-query", True),
    ("missing", True), ("invalid", True), ("valid", False),
])
def test_display_auth_denies_nonowner_anonymous_and_unconfigured_requests(
    case, http_entry, configuration, authenticated,
):
    if configuration != "missing":
        configure(case.monkeypatch, owner="another-owner"
                  if configuration in {"wrong-owner", "forged-owner-query"}
                  else "invited-test")
    if configuration == "invalid":
        case.monkeypatch.setenv("MEYO_DESKTOP_PUBLISH_PORT", "0")
    case.monkeypatch.setattr(case.desktop, "status", case.forbidden)
    target = AUTH_PATH + ("?uid=another-owner" if configuration == "forged-owner-query" else "")
    status, _headers, _body = http_entry.request(
        "GET", target, authenticated=authenticated, origin=None)
    assert status == 403
    assert http_entry.seen == []


def test_display_auth_is_not_a_prefix_route_or_a_post_proxy(case, http_entry):
    configure(case.monkeypatch)
    assert http_entry.request("GET", AUTH_PATH + "/other", origin=None)[0] == 404
    assert http_entry.request("POST", AUTH_PATH, body={})[0] == 404
    assert http_entry.seen == []


@pytest.mark.parametrize("security_options,actual_root,expected_root,ready", [
    (["name=rootless", "name=seccomp,profile=builtin"], "/fixture/new-root", None, True),
    (["name=rootless", "name=cgroupns"], "/fixture/new-root", "/fixture/new-root", True),
    (["name=seccomp,profile=builtin"], "/fixture/new-root", "/fixture/new-root", False),
    (["name=rootless"], "/fixture/other-root", "/fixture/new-root", False),
])
def test_selected_daemon_requires_rootless_and_the_optional_explicit_data_root(
    case, security_options, actual_root, expected_root, ready,
):
    patch, desktop = case.monkeypatch, case.desktop
    patch.setenv("MEYO_DEVICE_DOCKER_HOST", device_host())
    if expected_root is not None:
        patch.setenv("MEYO_DEVICE_DOCKER_DATA_ROOT", expected_root)
    patch.setattr(desktop.shutil, "which", lambda _name: "/fixture/docker")
    calls = []

    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        return {"ok": True, "stdout": docker_info(security_options, actual_root)}

    patch.setattr(desktop, "_command", command)
    result = desktop._docker_env()
    assert result["daemon_ready"] is ready
    assert result["rootless"] is ("name=rootless" in security_options)
    assert result["docker_root_dir"] == actual_root
    assert result["version"] == "29.1.3"
    assert len(calls) == 1
    assert calls[0][0][:4] == ["/fixture/docker", "--host", device_host(), "info"]
    assert ".SecurityOptions" in calls[0][0][-1] and ".DockerRootDir" in calls[0][0][-1]
    assert ".ServerVersion" in calls[0][0][-1]
    assert "{{json .}}" not in calls[0][0][-1], "do not dump unrelated daemon metadata"


@pytest.mark.parametrize("raw", ["29.1.3", "not-json", "{}", docker_info("name=rootless")])
def test_incomplete_daemon_facts_cannot_be_ready(case, raw):
    patch, desktop = case.monkeypatch, case.desktop
    patch.setenv("MEYO_DEVICE_DOCKER_HOST", device_host())
    patch.setattr(desktop.shutil, "which", lambda _name: "/fixture/docker")
    patch.setattr(desktop, "_command", lambda *_args, **_kwargs: {"ok": True, "stdout": raw})
    assert desktop._docker_env()["daemon_ready"] is False


@pytest.mark.parametrize("data_root", ["", "relative/docker"])
def test_invalid_explicit_data_root_is_rejected_before_probing(case, data_root):
    case.monkeypatch.setenv("MEYO_DEVICE_DOCKER_HOST", device_host())
    case.monkeypatch.setenv("MEYO_DEVICE_DOCKER_DATA_ROOT", data_root)
    case.monkeypatch.setattr(case.desktop.shutil, "which", lambda _name: "/fixture/docker")
    assert case.desktop._docker_env()["daemon_ready"] is False


def test_existing_32768_desktop_and_local_control_do_not_require_the_cua_cli(device):
    configure(device.monkeypatch)
    device.monkeypatch.setenv("MEYO_DESKTOP_PUBLISH_PORT", "32768")
    device.port = 32768
    device.desktop._common_snapshot()["control"]["ready"] = False
    status = device.desktop.status("invited-test")
    assert status["control_ready"] is False and status["desktop_ready"] is True
    assert status["desktop"]["display_url"] == PUBLIC_URL
    assert device.desktop.start_session("invited-test")["ok"]
    assert device.commands == [], "do not recreate the existing desktop or change its port"

    def local_command(args, **_kwargs):
        assert args[:2] == ["exec", "meyo-desktop-invited-test"]
        assert args[-1] == "http://127.0.0.1:8000/cmd"
        assert json.loads(args[args.index("-d") + 1]) == {"command": "screenshot", "params": {}}
        return {"ok": True, "stdout": 'data: {"success":true,"image_data":"fixture-image"}\n\n'}

    device.monkeypatch.setattr(device.desktop, "_docker_run", local_command)
    result = device.desktop._local_computer_command("invited-test", "screenshot", {})
    assert result["ok"] and result["result"]["image_data"] == "fixture-image"
