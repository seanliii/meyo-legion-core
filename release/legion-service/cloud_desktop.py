#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local visible desktop backed only by the official CUA XFCE image.

The selected official image exposes VNC on 5901 and noVNC on 6080. CUA SDK
computer-server readiness is intentionally not used to declare display readiness.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import threading

import legion_paths

CUA_XFCE_IMAGE = "public.ecr.aws/k5j5w0x5/cua-ubuntu-24.04:docker-latest"
_NOVNC_CONTAINER_PORT = "6080/tcp"
_NOVNC_PATH = "/vnc.html"
_DOCKER_TIMEOUT_S = 20
_START_TIMEOUT_S = 55
_PULL_TIMEOUT_S = 900
_STATUS_TTL_S = 30.0
_STATUS_CACHE: dict[str, tuple[float, dict]] = {}
_STATUS_REFRESHING: set[str] = set()
_STATUS_LOCK = threading.RLock()
_COMMON_TTL_S = 300.0
_COMMON_CACHE: tuple[float, dict] | None = None
_COMMON_LOCK = threading.RLock()
_SCENE_TTL_S = 10.0
_SCENE_CACHE: dict[str, tuple[float, dict]] = {}
_SCENE_REFRESHING: set[str] = set()
_SCENE_LOCK = threading.RLock()


def _device_docker_host(environment=None):
    """An explicit personal device connection never falls back to host Docker."""
    environment = os.environ if environment is None else environment
    if "MEYO_DEVICE_DOCKER_HOST" not in environment:
        return None
    host = environment["MEYO_DEVICE_DOCKER_HOST"]
    if host != f"unix:///run/user/{os.getuid()}/docker.sock":
        raise ValueError("个人设备必须使用当前运行用户的 rootless Docker socket")
    return host


def _device_docker_data_root(environment=None):
    environment = os.environ if environment is None else environment
    if "MEYO_DEVICE_DOCKER_DATA_ROOT" not in environment:
        return None
    root = environment["MEYO_DEVICE_DOCKER_DATA_ROOT"]
    if ("MEYO_DEVICE_DOCKER_HOST" not in environment or not os.path.isabs(root)
            or any(ord(char) < 32 or ord(char) == 127 for char in root)):
        raise ValueError("个人 Docker 数据目录必须是显式绝对路径，且已指定设备 socket")
    return root


def _personal_desktop_config(environment=None):
    """Validate the launcher's projection without reading credentials or probing devices."""
    environment = os.environ if environment is None else environment
    keys = ("MEYO_DEVICE_DOCKER_HOST", "MEYO_DEVICE_DOCKER_DATA_ROOT", "MEYO_DESKTOP_PUBLISH_PORT",
            "MEYO_DESKTOP_PUBLIC_PATH", "MEYO_DESKTOP_OWNER")
    if not any(key in environment for key in keys):
        return None
    host = _device_docker_host(environment)
    _device_docker_data_root(environment)
    port = environment.get("MEYO_DESKTOP_PUBLISH_PORT", "")
    path = environment.get("MEYO_DESKTOP_PUBLIC_PATH", "")
    owner = environment.get("MEYO_DESKTOP_OWNER", "")
    if (host is None or not re.fullmatch(r"[0-9]{1,5}", port)
            or not 1024 <= int(port) <= 65535
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", owner)
            or not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", path)
            or path in {"/api", "/s", "/shared", "/health", "/monitor", "/outputs",
                        "/home-outputs", "/media", "/desktop-outputs", "/auto-outputs"}):
        raise ValueError("个人桌面连接配置不完整或无效")
    return {"docker_host": host, "port": int(port), "path": path, "owner": owner}


def _desktop_config_for(uid, environment=None):
    config = _personal_desktop_config(environment)
    if config is not None and str(uid) != config["owner"]:
        raise ValueError("当前用户不是这台个人桌面的配置归属用户")
    return config


def display_authorized(uid):
    """Configuration/owner authorization only; no Docker, status, or lifecycle calls."""
    try:
        return _desktop_config_for(uid) is not None
    except ValueError:
        return False


def _unavailable_status(uid, reason):
    return {"ok": True, "uid": uid, "sandbox_name": _cua_name(uid),
            "control_ready": False, "desktop_ready": False, "display_ready": False,
            "installed": False, "ready": False, "image_status": "unavailable",
            "image_available": False, "desktop": {"status": "configuration_unavailable",
            "active": False, "display_url": "", "display_ready": False, "hint": reason},
            "from_cache": False, "hint": reason}


def invalidate_status(uid: str | None = None) -> None:
    """启动/停止/安装等状态变更后精确失效展示快照。"""
    with _STATUS_LOCK:
        if uid is None:
            _STATUS_CACHE.clear()
        else:
            _STATUS_CACHE.pop(str(uid), None)


def _cua_uid(uid):
    # 身份归一:与 desktop_fs._safe_id 同源(无碰撞),保证容器名/绑定文件/文件卷三处同 uid。
    import desktop_fs
    return desktop_fs._safe_id(uid)


def _cua_name(uid):
    return "meyo-desktop-" + _cua_uid(uid)


def _cua_binding(uid):
    return legion_paths.under("users", _cua_uid(uid), "cloud_desktop.json")


def _cua_read(uid):
    try:
        with open(_cua_binding(uid), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {"name": _cua_name(uid), "local": True}


def _cua_write(uid, **changes):
    data = {**_cua_read(uid), **changes, "name": _cua_name(uid), "local": True, "updated_at": int(time.time())}
    path = _cua_binding(uid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp." + str(os.getpid())
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    return data


def _command(argv, *, env=None, timeout=_DOCKER_TIMEOUT_S, stdin=None, stdout_limit=3000):
    limit = max(1, int(stdout_limit or 3000))
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, input=stdin)
        return {"ok": result.returncode == 0, "argv": argv, "exit_code": result.returncode, "stdout": (result.stdout or "")[-limit:], "stderr": (result.stderr or "")[-limit:]}
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout.decode("utf-8", "replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode("utf-8", "replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        return {"ok": False, "argv": argv, "error": f"command timed out ({timeout}s)", "stdout": str(stdout)[-limit:], "stderr": str(stderr)[-limit:]}
    except OSError as error:
        return {"ok": False, "argv": argv, "error": f"{type(error).__name__}: {error}"}


def _command_error(command):
    return str(command.get("stderr") or command.get("stdout") or command.get("error") or "command returned no diagnostics").strip()


def _docker_env():
    """Verify a personal rootless daemon; otherwise retain legacy Docker discovery."""
    executable = shutil.which("docker")
    base_env = os.environ.copy()
    if not executable:
        return {"cli_present": False, "daemon_ready": False, "reason": "Docker CLI not found", "env": base_env}
    try:
        device_host = _device_docker_host()
        expected_root = _device_docker_data_root()
    except ValueError as error:
        return {"cli_present": True, "daemon_ready": False, "source": "configured_device",
                "reason": str(error), "env": base_env}
    if device_host is not None:
        env = base_env.copy()
        env["DOCKER_HOST"] = device_host
        env.pop("DOCKER_CONTEXT", None)
        fields = ('{"server_version":{{json .ServerVersion}},'
                  '"security_options":{{json .SecurityOptions}},'
                  '"docker_root_dir":{{json .DockerRootDir}}}')
        check = _command([executable, "--host", device_host, "info", "--format", fields],
                         env=env, timeout=5)
        try:
            facts = json.loads(check.get("stdout") or "")
        except (ValueError, TypeError):
            facts = {}
        facts = facts if isinstance(facts, dict) else {}
        version = facts.get("server_version")
        root = facts.get("docker_root_dir")
        options = facts.get("security_options")
        facts_valid = (isinstance(version, str) and bool(version.strip())
                       and isinstance(root, str) and os.path.isabs(root)
                       and isinstance(options, list) and all(isinstance(item, str) for item in options))
        rootless = isinstance(options, list) and "name=rootless" in options
        if not check.get("ok"):
            reason = "指定的个人 Docker 服务不可用：" + _command_error(check)
        elif not facts_valid:
            reason = "指定 Docker 未返回完整的身份与数据目录信息"
        elif not rootless:
            reason = "指定 Docker 未报告 rootless 身份"
        elif expected_root is not None and root != expected_root:
            reason = "指定 Docker 数据目录与个人配置不匹配"
        else:
            reason = ""
        ready = not reason
        return {"cli_present": True, "daemon_ready": ready, "docker_host": device_host,
                "source": "configured_device", "version": version if isinstance(version, str) else "",
                "rootless": rootless, "docker_root_dir": root if isinstance(root, str) else "",
                "security_options": options if isinstance(options, list) else [],
                "env": env, "check": check, "reason": reason}
    candidates = []
    inherited = base_env.get("DOCKER_HOST", "").strip()
    if inherited:
        candidates.append(("existing_docker_host", inherited))
    for source, socket in (("docker_default", "/var/run/docker.sock"), ("colima_default", os.path.expanduser("~/.colima/default/docker.sock")), ("colima_root", "/var/root/.colima/default/docker.sock")):
        if os.path.exists(socket):
            candidates.append((source, f"unix://{socket}"))
    errors, seen = [], set()
    for source, docker_host in candidates:
        if docker_host in seen:
            continue
        seen.add(docker_host)
        env = base_env.copy()
        env["DOCKER_HOST"] = docker_host
        check = _command([executable, "version", "--format", "{{.Server.Version}}"], env=env, timeout=5)
        version = (check.get("stdout") or "").strip()
        if check.get("ok") and version:
            return {"cli_present": True, "daemon_ready": True, "docker_host": docker_host, "source": source, "version": version, "env": env, "check": check}
        errors.append(f"{source}: {_command_error(check)}")
    return {"cli_present": True, "daemon_ready": False, "reason": "; ".join(errors) if errors else "no working DOCKER_HOST or Colima socket found", "env": base_env}


def _docker_run(args, *, timeout=_DOCKER_TIMEOUT_S, docker=None, stdin=None, stdout_limit=3000):
    docker = docker or _docker_env()
    executable = shutil.which("docker")
    argv = [executable or "docker", *args]
    if docker.get("source") == "configured_device" and docker.get("docker_host"):
        argv = [executable or "docker", "--host", docker["docker_host"], *args]
    if not executable:
        return {"ok": False, "argv": argv, "error": "Docker CLI not found"}
    if not docker.get("daemon_ready"):
        return {"ok": False, "argv": argv, "error": docker.get("reason") or "Docker daemon unavailable"}
    return _command(argv, env=docker["env"], timeout=timeout, stdin=stdin, stdout_limit=stdout_limit)


def _cua_run(args, timeout=20, docker=None):
    executable = shutil.which("cua")
    argv = [executable or "cua", *args]
    if not executable:
        return {"ok": False, "argv": argv, "error": "cua CLI not found"}
    docker = docker or _docker_env()
    return _command(argv, env=docker.get("env"), timeout=timeout)


def _cua_control(docker=None):
    docker = docker or _docker_env()
    cli = _cua_run(["--help"], docker=docker)
    mcp = _cua_run(["serve-mcp", "--help"], docker=docker)
    return {"ready": bool(cli.get("ok") and mcp.get("ok")), "cua": cli, "serve_mcp": mcp}


def _cua_docker():
    return {key: value for key, value in _docker_env().items() if key != "env"}


def _image_status(docker):
    command = _docker_run(["image", "inspect", CUA_XFCE_IMAGE], docker=docker)
    return {"available": bool(command.get("ok")), "image": CUA_XFCE_IMAGE, "command": command}


def _container_state(uid="default", docker=None):
    docker = docker or _docker_env()
    name = _cua_name(uid)
    inspect_format = '{"image":{{json .Config.Image}},"labels":{{json .Config.Labels}},"bindings":{{json (index .NetworkSettings.Ports "6080/tcp")}},"mounts":{{json .Mounts}},"running":{{json .State.Running}},"status":{{json .State.Status}}}'
    command = _docker_run(["container", "inspect", "--format", inspect_format, name], docker=docker)
    if not command.get("ok"):
        error = _command_error(command).lower()
        return {"name": name, "exists": False, "verified": False, "command": command, "not_found": "no such container" in error or "not found" in error}
    try:
        item = json.loads(command.get("stdout") or "{}")
    except (json.JSONDecodeError, IndexError, TypeError):
        return {"name": name, "exists": True, "verified": False, "command": command, "reason": "Docker inspect returned invalid container data"}
    labels = item.get("labels") or {}
    image = str(item.get("image") or "")
    mounts = item.get("mounts") or []
    meyo_mount = next((str(mount.get("Source") or "") for mount in mounts if isinstance(mount, dict) and mount.get("Destination") == "/root/Meyo"), "")
    verified = image in (CUA_XFCE_IMAGE, "trycua/cua-xfce:latest", "public.ecr.aws/k5j5w0x5/cua-ubuntu-24.04:docker-latest") and labels.get("cua.sandbox") == "true" and labels.get("cua.sandbox.name") == name
    bindings = item.get("bindings") or []
    host_port = None
    for binding in bindings:
        if str(binding.get("HostIp") or "") == "127.0.0.1":
            try:
                candidate = int(str(binding.get("HostPort") or ""))
                if 1 <= candidate <= 65535:
                    host_port = candidate
                    break
            except ValueError:
                pass
    reason = "" if verified else "同名容器不是当前用户受管的 CUA 云端主机，系统不会对它执行操作。"
    if verified and not host_port:
        reason = "CUA 云端主机缺少本机回环 noVNC 端口映射。"
    return {"name": name, "exists": True, "verified": verified, "image": image, "labels": {"cua.sandbox": labels.get("cua.sandbox"), "cua.sandbox.name": labels.get("cua.sandbox.name")}, "running": bool(item.get("running")), "status": str(item.get("status") or "unknown"), "host_port": host_port, "meyo_mount": meyo_mount, "reason": reason, "command": command}


def _novnc_url(port, uid="default"):
    try:
        config = _desktop_config_for(uid)
    except ValueError:
        return ""
    if config is not None:
        if port != config["port"]:
            return ""
        path = config["path"]
        return (f"{path}{_NOVNC_PATH}?autoconnect=true&resize=scale"
                f"&path={path[1:]}/websockify")
    return f"http://127.0.0.1:{int(port)}{_NOVNC_PATH}?autoconnect=true&resize=scale"


def _probe_novnc(port, timeout=3):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "missing valid mapped noVNC port"}
    if not 1 <= port <= 65535:
        return {"ok": False, "reason": "mapped noVNC port is outside valid range"}
    url = f"http://127.0.0.1:{port}{_NOVNC_PATH}"
    request = urllib.request.Request(url, headers={"User-Agent": "Meyo-Legion-CUA-Readiness/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"ok": 200 <= response.status < 400, "url": url, "status_code": response.status, "content_type": response.headers.get("Content-Type", "")}
    except urllib.error.HTTPError as error:
        return {"ok": False, "url": url, "status_code": error.code, "reason": f"HTTP {error.code}"}
    except (urllib.error.URLError, OSError) as error:
        return {"ok": False, "url": url, "reason": str(getattr(error, "reason", error))}


def _wait_for_novnc(uid, docker, timeout=_START_TIMEOUT_S):
    deadline = time.monotonic() + timeout
    latest = {"ok": False, "reason": "waiting for noVNC readiness"}
    while time.monotonic() < deadline:
        container = _container_state(uid, docker)
        if not container.get("exists") or not container.get("verified") or not container.get("running"):
            return container, {"ok": False, "reason": container.get("reason") or "CUA container is not running"}
        latest = _probe_novnc(container.get("host_port"))
        if latest.get("ok"):
            return container, latest
        time.sleep(1)
    return _container_state(uid, docker), latest


def _common_snapshot(force: bool = False):
    """Docker/CUA/image 是全机公共事实，不能为每个 uid 重复跑四个子进程。"""
    global _COMMON_CACHE
    with _COMMON_LOCK:
        hit = _COMMON_CACHE
        if not force and hit and time.time() - hit[0] < _COMMON_TTL_S:
            return hit[1]
        docker = _docker_env()
        image = (_image_status(docker) if docker.get("daemon_ready") else
                 {"available": False, "image": CUA_XFCE_IMAGE, "command": {}})
        control = _cua_control(docker)
        value = {"docker": docker, "image": image, "control": control}
        _COMMON_CACHE = (time.time(), value)
        return value


def _desktop_snapshot(uid):
    common = _common_snapshot()
    docker = common["docker"]
    binding = _cua_read(uid)
    image = common["image"]
    container = _container_state(uid, docker)
    probe = _probe_novnc(container.get("host_port")) if container.get("verified") and container.get("running") else {"ok": False, "reason": container.get("reason") or "container is not running"}
    display_ready = bool(container.get("verified") and container.get("running") and probe.get("ok"))
    image_state = "image_available" if image.get("available") else ("image_pull_failed" if binding.get("image_state") == "image_pull_failed" else "image_pending")
    if display_ready:
        status, hint = "display_ready", "官方 CUA 云端主机已就绪，可直接打开交互桌面。"
    elif not docker.get("daemon_ready"):
        status, hint = "docker_unavailable", "未检测到可用 Docker 服务：已检查 DOCKER_HOST 与 Colima socket。"
    elif container.get("exists") and not container.get("verified"):
        status, hint = "container_untrusted", container.get("reason") or "Same-name container is unmanaged."
    elif container.get("verified") and container.get("running"):
        status, hint = "display_starting", f"CUA XFCE is running but {_NOVNC_PATH} has not passed HTTP probing: {probe.get('reason', 'not ready')}"
    elif container.get("verified"):
        status, hint = "stopped", "CUA 云端主机已停止；点击“启动/恢复桌面”将直接复用它。"
    elif image_state == "image_pull_failed":
        status, hint = "image_pull_failed", str(binding.get("last_error") or "CUA image pull failed; retry.")
    elif image_state == "image_available":
        status, hint = "image_ready", "CUA 镜像已就绪；点击“启动/恢复桌面”即可启动。"
    else:
        status, hint = "image_pending", "No CUA image locally; start will pull it."
    return docker, binding, image, container, probe, display_ready, image_state, status, hint


def _probe_status(uid):
    docker, binding, image, container, probe, display_ready, image_state, desktop_status, hint = _desktop_snapshot(uid)
    control = _common_snapshot()["control"]
    port = container.get("host_port") if display_ready else None
    display_url = _novnc_url(port, uid) if port else ""
    if display_ready and not display_url:
        display_ready = False
        desktop_status = "display_mapping_mismatch"
        hint = "实际 noVNC 端口与个人桌面配置不匹配，未提供访问地址。"
    connection = {"host": "127.0.0.1" if container.get("host_port") else "", "mapped_novnc_port": container.get("host_port"), "novnc_path": _NOVNC_PATH, "vnc_port": 5901, "api_url": ""}
    return {"ok": True, "uid": uid, "sandbox_name": _cua_name(uid), "control_ready": control["ready"], "desktop_ready": display_ready, "display_ready": display_ready, "installed": bool(docker.get("daemon_ready")), "ready": display_ready, "image_status": image_state, "image_available": bool(image.get("available")), "image": {"name": CUA_XFCE_IMAGE, "available": bool(image.get("available")), "inspect": image.get("command")}, "control": {"status": "ready" if control["ready"] else "unavailable", "hint": "CUA 控制端已校验；可视桌面的就绪状态独立由 noVNC HTTP 检测确认。" if control["ready"] else "CUA 控制端与可视云端主机独立校验。", "checks": {"cua": control["cua"], "serve_mcp": control["serve_mcp"]}}, "desktop": {"status": desktop_status, "active": bool(container.get("verified") and container.get("running")), "display_url": display_url, "display_ready": display_ready, "connection": connection, "container": {key: value for key, value in container.items() if key != "command"}, "probe": probe, "hint": hint}, "binding": binding, "docker": {key: value for key, value in docker.items() if key != "env"}, "hint": hint, "from_cache": False}


def _refresh_status(uid):
    try:
        result = _probe_status(uid)
        with _STATUS_LOCK:
            _STATUS_CACHE[uid] = (time.time(), result)
    except Exception:
        pass
    finally:
        with _STATUS_LOCK:
            _STATUS_REFRESHING.discard(uid)


def status(uid="default", force: bool = False):
    uid = str(uid or "default")
    try:
        _desktop_config_for(uid)
    except ValueError as error:
        return _unavailable_status(uid, str(error))
    with _STATUS_LOCK:
        cached = _STATUS_CACHE.get(uid)
        stale = bool(cached and time.time() - cached[0] >= _STATUS_TTL_S)
        if not force and cached and not stale:
            return {**cached[1], "from_cache": True, "stale": False}
        if not force and uid not in _STATUS_REFRESHING:
            _STATUS_REFRESHING.add(uid)
            threading.Thread(target=_refresh_status, args=(uid,), daemon=True,
                             name="cloud-desktop-status-refresh").start()
    if not force:
        if cached:
            return {**cached[1], "from_cache": True, "stale": True, "warming": True}
        return {"ok": True, "uid": uid, "sandbox_name": _cua_name(uid),
                "control_ready": False, "desktop_ready": False, "display_ready": False,
                "installed": False, "ready": False, "image_status": "warming",
                "image_available": False, "desktop": {"status": "warming", "active": False,
                "display_url": "", "display_ready": False}, "warming": True,
                "stale": False, "from_cache": False, "hint": "云端主机状态正在后台更新"}
    result = _probe_status(uid)
    with _STATUS_LOCK:
        _STATUS_CACHE[uid] = (time.time(), result)
    return result


def install_control(uid="default"):
    """Install/check the CUA CLI only; never create or start a desktop here."""
    import install_registry
    result = install_registry.install("pip:cua-cli-mcp", confirm=None, timeout_s=900)
    invalidate_status(uid)
    current = status(uid, force=True)
    control_ready = bool(current.get("control_ready"))
    return {**result, "action": "install_control", "control_ready": control_ready, "next_step": ("open_desktop" if current.get("desktop_ready") else "start_desktop") if control_ready else "retry_install", "message": "CUA 控制端已就绪；本操作只安装/校验控制能力，不会创建、重启或停止已有云端主机。" if control_ready else "CUA 控制端尚未通过验证；请查看安装错误后重试。", "desktop_status": current}


def _start_failure(uid, action, reason, command=None, image_state=None):
    _cua_write(uid, last_action=action, image_state=image_state or "image_pull_failed", last_error=reason, last_command=(command or {}).get("argv"))
    return {"ok": False, "action": action, "reason": reason, "command": command or {}, "status": status(uid, force=True)}


def start_session(uid="default"):
    import desktop_fs
    try:
        config = _desktop_config_for(uid)
    except ValueError as error:
        return {"ok": False, "action": "start", "reason": str(error)}
    invalidate_status(uid)
    prepared = desktop_fs.prepare_user_root(uid)
    docker = _docker_env()
    if not docker.get("daemon_ready"):
        reason = "Docker daemon unavailable: " + str(docker.get("reason") or "unknown reason")
        return _start_failure(uid, "start", reason, image_state="image_pending")
    current = _container_state(uid, docker)
    if current.get("exists") and not current.get("verified"):
        return _start_failure(uid, "start", current.get("reason") or "refusing unverified same-name container", current.get("command"), "image_pending")
    if (config is not None and current.get("running")
            and current.get("host_port") != config["port"]):
        return {"ok": False, "action": "start", "reason": "已有桌面的端口与个人配置不匹配，未重建容器。",
                "status": status(uid, force=True)}
    migration = None
    expected_mount = os.path.realpath(prepared["root"])
    if current.get("exists") and current.get("verified") and current.get("meyo_mount") != expected_mount:
        was_running = bool(current.get("running"))
        if not was_running:
            revive = _docker_run(["container", "start", current["name"]], timeout=120, docker=docker)
            if not revive.get("ok"):
                return _start_failure(uid, "migrate_meyo_volume", "旧 CUA 云端主机无法启动以迁移文件：" + _command_error(revive), revive, "image_available")
        initialize = _docker_run(["exec", current["name"], "mkdir", "-p", "/root/Meyo"], timeout=30, docker=docker)
        archive = _docker_run(["cp", current["name"] + ":/root/Meyo/.", prepared["root"]], timeout=120, docker=docker) if initialize.get("ok") else initialize
        if not archive.get("ok"):
            return _start_failure(uid, "migrate_meyo_volume", "无法安全迁移旧云真机文件：" + _command_error(archive), archive, "image_available")
        # 迁移安全:只在【目标目录为空】时才继续销毁重建,避免旧主机文件倒进已有用户数据。
        if os.listdir(prepared["root"]):
            return _start_failure(uid, "migrate_meyo_volume", "目标 Meyo 卷非空,拒绝合并覆盖;请人工核对后迁移", None, "image_available")
        stopped = _docker_run(["container", "stop", current["name"]], timeout=120, docker=docker)
        removed = _docker_run(["container", "rm", current["name"]], timeout=120, docker=docker) if stopped.get("ok") else stopped
        if not removed.get("ok"):
            return _start_failure(uid, "migrate_meyo_volume", "旧云真机迁移后无法重建：" + _command_error(removed), removed, "image_available")
        migration = {"ok": True, "from_mount": current.get("meyo_mount") or "ephemeral", "to_mount": expected_mount, "preserved_files": True}
        current = _container_state(uid, docker)
    if current.get("exists"):
        action = "already_running" if current.get("running") else "restart"
        command = {"ok": True, "argv": []} if current.get("running") else _docker_run(["container", "start", current["name"]], timeout=120, docker=docker)
        if not command.get("ok"):
            return _start_failure(uid, action, "CUA 云端主机启动失败：" + _command_error(command), command, "image_available")
    else:
        image = _image_status(docker)
        image_name = image.get("image") or CUA_XFCE_IMAGE
        if not image.get("available"):
            pull = None
            for img_name in [CUA_XFCE_IMAGE, "trycua/cua-xfce:latest"]:
                pull = _docker_run(["pull", img_name], timeout=_PULL_TIMEOUT_S, docker=docker)
                if pull.get("ok"):
                    image_name = img_name
                    break
            if not pull or not pull.get("ok"):
                return _start_failure(uid, "image_pull", "CUA 镜像拉取失败：" + _command_error(pull or {}), pull, "image_pull_failed")
        action = "launch"
        publish = f"127.0.0.1:{config['port']}:6080" if config else "127.0.0.1::6080"
        command = _docker_run(["run", "-d", "--name", _cua_name(uid), "--label", "cua.sandbox=true", "--label", "cua.sandbox.name=" + _cua_name(uid), "--volume", prepared["root"] + ":/root/Meyo", "--publish", publish, image_name], timeout=180, docker=docker)
        if not command.get("ok"):
            return _start_failure(uid, "launch", "CUA 云端主机创建失败：" + _command_error(command), command, "image_available")
    container, probe = _wait_for_novnc(uid, docker)
    if not probe.get("ok"):
        reason = "CUA 桌面已启动但 noVNC 尚未就绪: " + str(probe.get("reason", "probe timed out"))
        _cua_write(uid, last_action=action, image_state="image_available", last_error=reason, last_command=command.get("argv"))
        return {"ok": False, "action": action, "reason": reason, "container": container, "probe": probe, "status": status(uid, force=True)}
    if config is not None and container.get("host_port") != config["port"]:
        return {"ok": False, "action": action, "reason": "实际 noVNC 端口与个人配置不匹配。",
                "container": container, "probe": probe, "status": status(uid, force=True)}
    _cua_write(uid, last_action=action, image_state="image_available", last_error="", last_command=command.get("argv"))
    # noVNC 刚通过探测时，启动前的失败状态仍可能留在短时缓存里。
    invalidate_status(uid)
    opened = session_open(uid)
    try:
        bootstrap = desktop_fs.bootstrap(uid)
    except Exception as error:
        bootstrap = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    invalidate_status(uid)
    final_status = status(uid, force=True)
    desktop_ready = bool(final_status.get("ready") and final_status.get("display_ready"))
    return {"ok": bool(opened.get("ok") or desktop_ready), "action": action,
            "migration": migration, "session": opened, "bootstrap": bootstrap,
            "bootstrap_pending": not bool(bootstrap.get("ok")), "status": final_status}


def ensure_desktop(uid="default"):
    """Return a ready desktop when present, otherwise create this users managed desktop."""
    opened = session_open(uid)
    if opened.get("ok"):
        return {"ok": True, "action": "already_ready", "session": opened, "status": opened.get("status")}
    return start_session(uid)

def stop_session(uid="default"):
    import desktop_fs
    invalidate_status(uid)
    docker = _docker_env()
    if not docker.get("daemon_ready"):
        return {"ok": False, "action": "stop", "reason": "Docker daemon unavailable: " + str(docker.get("reason") or "unknown reason"), "status": status(uid, force=True)}
    current = _container_state(uid, docker)
    if not current.get("exists") or not current.get("running"):
        return {"ok": True, "action": "already_stopped", "status": status(uid, force=True)}
    if not current.get("verified"):
        return {"ok": False, "action": "stop", "reason": current.get("reason") or "refusing unverified same-name container", "status": status(uid, force=True)}
    # 停止前先把容器内新文件同步回持久卷(容器退出即丢,不拉回=真丢):只复制,不删除任何已有文件。
    try:
        prepared = desktop_fs.prepare_user_root(uid)
        sync = _docker_run(["cp", current["name"] + ":/root/Meyo/.", prepared["root"]], timeout=120, docker=docker)
        synced = bool(sync.get("ok"))
        sync_error = "" if synced else _command_error(sync)
    except Exception as error:
        synced, sync_error = False, f"{type(error).__name__}: {error}"
    command = _docker_run(["container", "stop", current["name"]], timeout=120, docker=docker)
    if not command.get("ok"):
        return _start_failure(uid, "stop", "CUA 云端主机停止失败：" + _command_error(command), command, "image_available")
    _cua_write(uid, last_action="stop", image_state="image_available", last_error="", last_command=command.get("argv"))
    invalidate_status(uid)
    out = {"ok": True, "action": "stop", "status": status(uid, force=True)}
    if not synced:
        out["sync_warning"] = "容器文件未能同步回持久卷：" + sync_error
    return out


def session_open(uid="default"):
    current = status(uid)
    desktop = current["desktop"]
    if not current["desktop_ready"]:
        return {"ok": False, "reason": desktop.get("status") or "desktop_not_ready",
                "hint": desktop.get("hint") or current.get("hint") or "云端主机尚未就绪。",
                "status": current}
    url = desktop.get("display_url") or ""
    if not url:
        return {"ok": False, "reason": "display_url_unavailable", "hint": "noVNC HTTP probe did not complete; no desktop URL is fabricated.", "status": current}
    return {"ok": True, "session_url": url, "display_url": url, "connection": desktop["connection"], "status": current}


def install_guide(uid="default"):
    return {"ok": True, "uid": uid, "install": "python3.12 -m pip install --break-system-packages --extra-index-url https://wheels.cua.ai/simple cua-cli[mcp]", "local_note": "可视桌面使用本机官方 CUA 镜像，且仅暴露动态本机回环 noVNC 端口。", "cloud_note": "Visible readiness is only HTTP /vnc.html, not computer-server.", "status": status(uid, force=True)}


def check_can_dispatch(uid="default"):
    current = status(uid)
    return {"can": bool(current["desktop_ready"]), "reason": "" if current["desktop_ready"] else current["desktop"]["hint"], "hint": "官方 CUA 可视桌面已就绪。" if current["desktop_ready"] else "", "status": current}


# ── 智能意图路由 v2(学 Lumi 意图路由·用户 2026-09-01 纠正:模型判断·非关键词表)──
# context not control:把【用户原话+本机直执行能力清单+军团说明】交给模型,让它自己路由。
# 父层零关键词表。结构性直通包括纯 shell，以及调用方显式给出的原子 action 协议；自然语言
# 仍由模型判断。模型超时/挂→一律派军团(保底不白卷)。

def _probe_desktop_scene(uid: str) -> dict:
    import desktop_fs
    scene = {"sessions_recent": [], "apps_running": []}
    try:
        listing = desktop_fs.list_sessions(uid, "")
        names = [str(e.get("name")) for e in (listing.get("entries") or []) if e.get("type") == "directory"]
        scene["sessions_recent"] = names[:8]
    except Exception:
        pass
    try:
        procs = desktop_fs.desktop_exec(uid, "ps -eo comm= | sort | uniq -c | sort -rn | sed -n 1,8p", 15)
        scene["apps_running"] = [ln.strip() for ln in str(procs.get("stdout") or "").splitlines() if ln.strip()][:8]
    except Exception:
        pass
    return scene


def _refresh_desktop_scene(uid: str) -> None:
    try:
        scene = _probe_desktop_scene(uid)
        with _SCENE_LOCK:
            _SCENE_CACHE[str(uid)] = (time.monotonic(), scene)
    finally:
        with _SCENE_LOCK:
            _SCENE_REFRESHING.discard(str(uid))


def _desktop_scene(uid: str) -> dict:
    """立即返回最近真机现场；无快照返回 warming 并后台刷新，绝不阻塞路由。"""
    key, now = str(uid), time.monotonic()
    with _SCENE_LOCK:
        cached = _SCENE_CACHE.get(key)
        fresh = bool(cached and now - cached[0] < _SCENE_TTL_S)
        if not fresh and key not in _SCENE_REFRESHING:
            _SCENE_REFRESHING.add(key)
            threading.Thread(target=_refresh_desktop_scene, args=(key,), daemon=True,
                             name="desktop-scene-refresh").start()
    if cached:
        return {**cached[1], "stale": not fresh, "warming": not fresh}
    return {"sessions_recent": [], "apps_running": [], "stale": False, "warming": True}


def _llm_env() -> dict:
    """网关安全 env:aigc 直连(剔除可致 Connection error 的 socks 代理)。"""
    env = dict(os.environ)
    hosts = "aigc.sankuai.com,127.0.0.1,localhost"
    existing = env.get("NO_PROXY", "") or env.get("no_proxy", "")
    env["NO_PROXY"] = (existing + "," + hosts).strip(",") if existing else hosts
    env["no_proxy"] = env["NO_PROXY"]
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(key, None)
    return env


def _route_llm(text: str) -> dict | None:
    """把现场交给模型,让它自己判断 direct/legion(不写关键词规则)。"""
    import re as _re
    import subprocess
    import sys
    from planner_runtime import tool_path, model_env
    cm = tool_path()
    if not os.path.isfile(cm):
        return None
    prompt = (
        "用户在云真机控制台输入了一句话,判断它是【简单即时指令】还是【复杂任务】。\\n"
        "本机直执行能力(毫秒级,无需启动agent):\\n"
        "- shell: 在真机容器内执行一条shell命令(工作目录/root/Meyo)\\n"
        "- screenshot: 给真机当前屏幕截一张图\\n"
        "- open: 用真机桌面应用打开一个已存在的文件(需~/Meyo内相对路径)\\n"
        "- list: 列出~/Meyo下的文件\\n"
        "军团全链路(数十秒到几分钟,什么都能做:多步任务/生成内容/安装部署/研究/编排)。\\n"
        "判断标准:这句话是否等于一个可立即执行完的单一原子动作?是→direct;"
        "只要包含多个步骤/需要创造内容/需要研究或判断/是闲聊提问→legion(军团完整执行并回答)。\\n"
        "只输出一个JSON对象,字段:route(direct或legion)/action(shell|screenshot|open|list)/"
        "command(action=shell时要执行的完整命令)/path(action=open时的文件路径)/reason(一句话)。"
        "示例:{\"route\":\"direct\",\"action\":\"shell\",\"command\":\"ls ~/Meyo\",\"path\":\"\",\"reason\":\"列出文件\"}\\n"
        "用户输入:" + text)
    r = None
    for _attempt in (1, 2):  # 偶发抖动重试一次:direct秒回是产品承诺·fallback虽不错但慢
        try:
            r = subprocess.run([sys.executable, cm, "chat", prompt, "--timeout", "10"],
                               capture_output=True, text=True, timeout=15,
                               env=model_env("kimi-k2.7-code-highspeed", _llm_env()))
            if (r.stdout or "").strip():
                break
        except Exception:
            r = None
    if r is None or not (r.stdout or "").strip():
        return None
    m = _re.search(r"\{[\s\S]*\}", r.stdout or "")
    if not m:
        return None
    try:
        import json as _json
        return _json.loads(m.group(0))
    except Exception:
        return None


def route_intent(text: str, uid: str = "default", action: str = "", params: dict | None = None) -> dict:
    """云真机一句话分诊:模型判断 direct/legion·直通仅限纯shell命令形态。"""
    import desktop_fs
    text = str(text or "").strip()
    action = str(action or "").strip().lower()
    if not text and action not in {"screenshot", "list"}:
        return {"ok": False, "error": "empty text"}
    base = {"ok": True, "uid": uid, "text": text}
    params = params if isinstance(params, dict) else {}
    if action in {"screenshot", "open", "list", "shell"}:
        if action == "screenshot":
            out, act = desktop_fs.desktop_screenshot(uid), "desktop_screenshot"
        elif action == "open":
            out, act = desktop_fs.open_in_desktop(uid, str(params.get("path") or text)), "open_in_desktop"
        elif action == "list":
            out, act = desktop_fs.list_files(uid, str(params.get("path") or "")), "desktop_fs_list"
        else:
            out, act = desktop_fs.desktop_exec(uid, str(params.get("command") or text), 30), "desktop_exec"
        return {**base, "route": "direct", "reason": "explicit atomic action",
                "action": act, "result": out}
    # 结构性直通:整条输入=纯shell命令(ASCII命令词开头·无中文·短)。格式识别非意图猜测。
    words = text.split()
    looks_cmd = (
        0 < len(text) <= 160
        and words
        and words[0].isascii()
        and words[0].replace("-", "").replace("_", "").isalnum()
        and words[0][0].isalpha()
        and not any(chr(0x4E00) <= ch <= chr(0x9FFF) for ch in text)
    )
    if looks_cmd:
        out = desktop_fs.desktop_exec(uid, text, 30)
        return {**base, "route": "direct", "reason": "pure shell", "action": "desktop_exec", "result": out}
    # 模型判断(context not control):给现场让它自己路由
    verdict = _route_llm(text)
    if verdict and str(verdict.get("route") or "").lower() == "direct":
        action = str(verdict.get("action") or "shell").lower()
        if action == "screenshot":
            out, act = desktop_fs.desktop_screenshot(uid), "desktop_screenshot"
        elif action == "open":
            out, act = desktop_fs.open_in_desktop(uid, str(verdict.get("path") or "")), "open_in_desktop"
        elif action == "list":
            out, act = desktop_fs.list_files(uid, ""), "desktop_fs_list"
        else:
            out, act = desktop_fs.desktop_exec(uid, str(verdict.get("command") or ""), 30), "desktop_exec"
        return {**base, "route": "direct", "reason": str(verdict.get("reason") or "model"), "action": act, "result": out}
    # 模型判legion/模型不可用→一律派军团(保底不白卷)
    reason = str((verdict or {}).get("reason") or "model unavailable, fallback legion")
    return {**base, "route": "legion", "reason": reason, "scene": _desktop_scene(uid)}

def suggest_for_brief(brief="", uid="default"):
    return {"ok": True, "uid": uid, "suggest": False, "brief_present": bool(str(brief).strip()), "context": status(uid), "hint": "Cloud desktop state is context only; the legion decides whether to use GUI tools."}

def _local_computer_command(uid, command, params=None):
    docker = _docker_env()
    if not docker.get("daemon_ready"):
        return {"ok": False, "error": "Docker 服务不可用：" + str(docker.get("reason") or "未知原因")}
    container = _container_state(uid, docker)
    if not container.get("verified") or not container.get("running"):
        return {"ok": False, "error": "本机 CUA 云端主机未运行或不受当前用户管理", "desktop": container}
    payload = json.dumps({"command": str(command), "params": params or {}}, ensure_ascii=False)
    request = _docker_run(["exec", container["name"], "curl", "-s", "-m", "25", "-X", "POST", "-H", "Content-Type: application/json", "-d", payload, "http://127.0.0.1:8000/cmd"], timeout=30, docker=docker, stdout_limit=4000000)
    raw = str(request.get("stdout") or "").strip()
    data_line = next((line[5:].strip() for line in raw.splitlines() if line.startswith("data:")), raw)
    try:
        result = json.loads(data_line)
    except json.JSONDecodeError:
        result = {"success": False, "error": _command_error(request)}
    return {"ok": bool(request.get("ok") and result.get("success")), "sandbox": container["name"], "command": command, "result": result, "transport": "managed_local_cua_computer_server"}


def _local_mcp_summary(uid):
    current = status(uid)
    desktop = current.get("desktop") or {}
    return {"uid": uid, "sandbox": current.get("sandbox_name"), "control_ready": bool(current.get("control_ready")), "desktop_ready": bool(current.get("desktop_ready")), "desktop_status": desktop.get("status"), "display_url": desktop.get("display_url") or "", "hint": desktop.get("hint") or current.get("hint") or ""}


def local_mcp_tools():
    return [
        {"name": "desktop_status", "description": "读取当前用户受管本机 CUA 云端主机的真实就绪状态与可视桌面地址。", "input_schema": {"type": "object", "properties": {"uid": {"type": "string"}}}},
        {"name": "desktop_start", "description": "创建或恢复当前用户的本机 CUA 云端主机，并等待 noVNC 真实就绪。", "input_schema": {"type": "object", "properties": {"uid": {"type": "string"}}}},
        {"name": "desktop_ensure", "description": "默认取得当前用户独立云真机；已就绪则直接复用，否则创建并等待 noVNC。", "input_schema": {"type": "object", "properties": {"uid": {"type": "string"}}}},
        *(__import__("desktop_fs").local_mcp_tools()),
        {"name": "desktop_stop", "description": "停止当前用户受管的本机 CUA 云端主机。", "input_schema": {"type": "object", "properties": {"uid": {"type": "string"}}}},
        {"name": "desktop_open", "description": "取得已通过 HTTP 核验的本机 noVNC 桌面地址；不会创建或重启桌面。", "input_schema": {"type": "object", "properties": {"uid": {"type": "string"}}}},
        {"name": "computer_command", "description": "对当前用户已运行的本机 CUA computer-server 执行真实命令。command 可为 screenshot、left_click、type_text、press_key、hotkey、run_command、open 等 CUA 原生命令；params 为该命令参数。" + __import__("desktop_fs").CUA_GUI_INPUT_HELP, "input_schema": {"type": "object", "properties": {"uid": {"type": "string"}, "command": {"type": "string"}, "params": {"type": "object"}}, "required": ["command"]}},
    ]


def local_mcp_call(name, arguments=None, *, bound_uid=None):
    arguments = arguments or {}
    # 身份绑定(与 desktop_fs 同源):进程携带 MEYO_LEGION_UID/MEYO_USER_ID 时,
    # 参数里的 uid 只能一致不能改写,防止 harness 冒充其他用户操作其云真机。
    import desktop_fs as _dfs
    bound = str(bound_uid).strip() if bound_uid is not None else ((_dfs.os.environ.get("MEYO_LEGION_UID") or _dfs.os.environ.get("MEYO_USER_ID") or "").strip() or None)
    if bound is not None:
        supplied = arguments.get("uid")
        if supplied is not None and _dfs._safe_id(supplied) != _dfs._safe_id(bound):
            return {"server": "cua-local-desktop", "tool": name, "status": "err:identity",
                    "result": {"ok": False, "error": "desktop identity is bound by the caller and cannot be overridden"}}
        # Storage paths normalize separately; authorization keeps the original subject.
        uid = bound
    else:
        uid = str(arguments.get("uid") or "default")
    if name == "desktop_status":
        return {"server": "cua-local-desktop", "tool": name, "status": "ok", "result": _local_mcp_summary(uid)}
    if name == "desktop_ensure":
        result = ensure_desktop(uid)
        return {"server": "cua-local-desktop", "tool": name, "status": "ok" if result.get("ok") else "err:desktop_ensure", "result": _local_mcp_summary(uid), "detail": result}
    if name == "desktop_start":
        result = start_session(uid)
        return {"server": "cua-local-desktop", "tool": name, "status": "ok" if result.get("ok") else "err:desktop_start", "result": _local_mcp_summary(uid), "detail": result}
    if name == "desktop_stop":
        result = stop_session(uid)
        return {"server": "cua-local-desktop", "tool": name, "status": "ok" if result.get("ok") else "err:desktop_stop", "result": _local_mcp_summary(uid), "detail": result}
    if name == "desktop_open":
        result = session_open(uid)
        return {"server": "cua-local-desktop", "tool": name, "status": "ok" if result.get("ok") else "err:desktop_open", "result": {"session_url": result.get("session_url") or "", "hint": result.get("hint") or ""}}
    if name == "computer_command":
        result = _local_computer_command(uid, arguments.get("command") or "", arguments.get("params") or {})
        return {"server": "cua-local-desktop", "tool": name, "status": "ok" if result.get("ok") else "err:computer_command", "result": result}
    import desktop_fs
    forwarded = desktop_fs.local_mcp_call(name, arguments)
    if forwarded.get("status") != "unknown_tool":
        return forwarded
    return {"server": "cua-local-desktop", "tool": name, "status": "unknown_tool", "available": [item["name"] for item in local_mcp_tools()]}
