"""Owner-bound mobile backend using AgentBay's existing MCP tools.

No answer planning, provider model, SDK installation, implicit device start, or
credential in task context. An operator grants finite device time; one durable
record is shared by HTTP, native tools and the independent expiry reaper.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://agentbay.wuying.aliyuncs.com/v2/mcp"
IMAGE = "mobile-use-android-12"
_MAX_RESPONSE = 12 * 1024 * 1024
_SID = re.compile(r"s-[A-Za-z0-9_-]{4,178}")
_ACTIONS = ["screenshot", "tap", "swipe", "press", "type", "start", "ui", "apps"]


def configured(*, environment=None):
    env = os.environ if environment is None else environment
    return "MEYO_AGENTBAY_CONFIG" in env


def _configuration(uid, environment=None):
    from personal_runtime import _read, _root
    env = os.environ if environment is None else environment
    path = Path(env.get("MEYO_AGENTBAY_CONFIG") or "")
    if not path.is_absolute() or path.name != "config.json" or path.is_symlink():
        raise ValueError("mobile configuration must be a private absolute file")
    parent = _root(path.parent)
    value = _read(path)
    if (set(value) != {"schema_version", "owner", "api_key_file", "control_dir",
                      "session_seconds", "total_seconds"}
            or value["schema_version"] != 1
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", str(value["owner"]))
            or value["api_key_file"] != str(parent / "api-key.txt")
            or value["control_dir"] != str(parent / "control")
            or type(value["session_seconds"]) is not int
            or type(value["total_seconds"]) is not int
            or not 60 <= value["session_seconds"] <= 300
            or not value["session_seconds"] <= value["total_seconds"] <= 1200):
        raise ValueError("invalid mobile provider configuration or authorized duration")
    if not uid or uid != value["owner"]:
        raise PermissionError("mobile device belongs to a different verified owner")
    return value


def _write(path, value):
    from personal_runtime import _json_bytes, _write as write
    write(path, _json_bytes(value))


@contextmanager
def control_lock(control_dir):
    from personal_runtime import _directory, _root
    directory = _root(control_dir, create=True)
    with _directory(directory, private=True) as parent:
        fd = os.open("phone.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(fd, "a") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("invalid private mobile lifecycle lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield directory


@contextmanager
def _locked(config):
    from personal_runtime import _private
    with control_lock(config["control_dir"]) as directory:
        # The configuration writer takes this same lock. Read again after the
        # wait so a caller never operates on a pre-update key/budget snapshot.
        fresh = _configuration(config["owner"], {
            "MEYO_AGENTBAY_CONFIG": str(directory.parent / "config.json")})
        config.clear()
        config.update(fresh)
        path = directory / "phone.json"
        if path.exists() or path.is_symlink():
            state = json.loads(_private(path))
            if (not isinstance(state, dict) or state.get("owner") != config["owner"]
                    or state.get("schema_version") != 1
                    or type(state.get("reserved_seconds")) is not int
                    or state["reserved_seconds"] < 0
                    or (state.get("sandbox_id") and not _SID.fullmatch(str(state["sandbox_id"])))):
                raise ValueError("invalid mobile lifecycle record; not reset")
        else:
            state = {"schema_version": 1, "owner": config["owner"], "phase": "idle",
                     "reserved_seconds": 0, "sandbox_id": None}
        yield state, lambda: _write(path, state)


def _view(config, state):
    left = max(0, int((state.get("expires_at") or 0) - time.time()))
    ready = state["phase"] == "running" and left > 0
    phase = "expired" if state["phase"] == "running" and not ready else state["phase"]
    return {
        "ok": True, "provider": "agentbay", "configured": True, "ready": ready,
        "phase": phase, "expires_at": state.get("expires_at"), "remaining_seconds": left,
        "remaining_budget_seconds": max(0, config["total_seconds"] - state["reserved_seconds"]),
        "session_seconds": config["session_seconds"], "screen_size": state.get("screen_size", ""),
        "reaper_ready": 0 <= time.time() - state.get("reaper_seen_at", 0) < 90,
        "actions": list(_ACTIONS), "supports_app_install": False,
        "note": "按需启动；到时自动释放。截图与状态查询不会自动购买或重开手机。",
    }


def _error(code, message, config, state):
    return {**_view(config, state), "ok": False, "error_code": code, "error": message}


def _safe(value, limit=4000):
    text = str(value)
    text = re.sub(r"akm-[A-Za-z0-9_-]+", "[private key]", text)
    text = re.sub(r"https?://[^\s\"'<>]+", "[private URL]", text)
    return text[:limit]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError("mobile provider redirected; credentials not forwarded")


def _rpc_reply(raw, content_type, request_id):
    """Parse bounded ordinary JSON or MCP SSE, including a multi-line data field."""
    if "text/event-stream" in content_type:
        messages = []
        for event in raw.decode().replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in event.splitlines()
                             if line.startswith("data:"))
            if data:
                messages.append(json.loads(data))
    else:
        messages = [json.loads(raw)]
    message = next((item for item in messages
                    if isinstance(item, dict) and item.get("id") == request_id), None)
    if not message or "error" in message or not isinstance(message.get("result"), dict):
        raise ValueError("mobile MCP response did not complete the requested call")
    return message["result"]


def _exchange(config, method, params):
    """Use observed protocol 2025-06-18; never replay a failed modifying request."""
    from personal_runtime import _private
    key = _private(Path(config["api_key_file"]), limit=4096).decode().strip()
    if not re.fullmatch(r"akm-[A-Za-z0-9_-]{16,180}", key):
        raise ValueError("invalid private mobile key")
    url = ENDPOINT + "?" + urllib.parse.urlencode({"APIKEY": key, "IMAGEID": IMAGE})
    opener = urllib.request.build_opener(_NoRedirect())
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    deadline = time.monotonic() + 45

    def request(body):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("mobile MCP exchange deadline")
        data = json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with opener.open(req, timeout=min(25, remaining)) as response:
            session = response.headers.get("Mcp-Session-Id")
            if session:
                if len(session) > 200 or any(ord(c) < 32 for c in session):
                    raise ValueError("invalid MCP session identity")
                headers["Mcp-Session-Id"] = session
            if "id" not in body:
                return {}
            content_type = response.headers.get("Content-Type", "")
            raw = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("mobile MCP exchange deadline")
                # read1 returns available bytes rather than waiting for a full
                # buffer, so slow-drip responses cannot hold a lock indefinitely.
                part = response.read1(min(65536, _MAX_RESPONSE + 1 - len(raw)))
                if not part:
                    break
                raw.extend(part)
                if len(raw) > _MAX_RESPONSE:
                    raise ValueError("mobile MCP response exceeds the transport limit")
                if "text/event-stream" in content_type and b"\n\n" in raw.replace(b"\r\n", b"\n"):
                    try:
                        return _rpc_reply(bytes(raw), content_type, body["id"])
                    except (ValueError, UnicodeError):
                        pass
            return _rpc_reply(bytes(raw), content_type, body["id"])

    initialized = request({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "meyo-mobile", "version": "1"}},
    })
    protocol = initialized.get("protocolVersion")
    if protocol not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
        raise ValueError("unsupported mobile MCP protocol")
    headers["MCP-Protocol-Version"] = protocol
    request({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return request({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})


def _tool(config, name, arguments):
    return _exchange(config, "tools/call", {"name": name, "arguments": arguments})


def _texts(reply):
    return [block["text"] for block in reply.get("content", [])
            if block.get("type") == "text" and isinstance(block.get("text"), str)]


def status(uid):
    config = _configuration(uid)
    with _locked(config) as (state, _):
        return _view(config, state)


def ensure(uid):
    config = _configuration(uid)
    with _locked(config) as (state, save):
        if state["phase"] == "running":
            if time.time() < state["expires_at"]:
                return _view(config, state)
            return _error("mobile_session_expired", "手机已到期，等待释放完成后可重新启动。", config, state)
        if state["phase"] not in {"idle", "released"}:
            return _error("mobile_state_requires_recovery", "上次创建或释放尚未确认；没有重开手机。", config, state)
        if config["total_seconds"] - state["reserved_seconds"] < config["session_seconds"]:
            return _error("mobile_budget_exhausted", "本次授权手机时长已用完；需所有者增加预算。", config, state)
        if not _view(config, state)["reaper_ready"]:
            return _error("mobile_reaper_unavailable", "自动释放任务未就绪，暂不开机，避免产生失控费用。", config, state)
        # Reserve before the remote write. A crash or unknown response cannot reset the cap.
        state.update(phase="creating", sandbox_id=None, created_at=time.time(),
                     expires_at=time.time() + config["session_seconds"])
        state["reserved_seconds"] += config["session_seconds"]
        save()
        try:
            reply = _tool(config, "create_sandbox", {})
            texts = _texts(reply)
            if reply.get("isError") or len(texts) != 1 or not _SID.fullmatch(texts[0].strip()):
                raise ValueError("mobile creation did not return its unique resource ID")
            state.update(sandbox_id=texts[0].strip(), phase="running")
            save()
            return _view(config, state)
        except Exception as exc:
            state.update(phase="creation_unknown", last_error=type(exc).__name__)
            save()
            return _error("mobile_creation_unknown",
                          "创建结果未确认，已保留预算和现场；不会自动重复开机。", config, state)


def _release(config, state, save):
    if state["phase"] == "released":
        return {**_view(config, state), "released": True}
    if not state.get("sandbox_id"):
        if state["phase"] in {"creating", "creation_unknown"}:
            return _error("mobile_creation_unknown", "上次创建结果未知，需从提供方核实后恢复。", config, state)
        return {**_view(config, state), "released": state["phase"] == "released"}
    state["phase"] = "releasing"
    save()
    try:
        reply = _tool(config, "kill_sandbox", {"sandbox_id": state["sandbox_id"]})
        if reply.get("isError") or "release resource success" not in _texts(reply):
            raise ValueError("provider did not confirm release")
        state.update(phase="released", released_at=time.time(), expires_at=None)
        save()
        return {**_view(config, state), "released": True}
    except Exception as exc:
        state.update(phase="release_failed", last_error=type(exc).__name__)
        save()
        return _error("mobile_release_failed",
                      "提供方尚未确认释放；已保留原设备ID并阻止新开机，回收任务将继续处理。", config, state)


def stop(uid):
    config = _configuration(uid)
    with _locked(config) as (state, save):
        return _release(config, state, save)


def reap(uid):
    config = _configuration(uid)
    with _locked(config) as (state, save):
        state["reaper_seen_at"] = time.time()
        save()
        if state["phase"] in {"creating", "creation_unknown"} and not state.get("sandbox_id"):
            return _error("mobile_creation_unknown",
                          "上次创建结果未知；需在提供方核实并释放，未再次开机。", config, state)
        if (state.get("sandbox_id") and state["phase"] != "released"
                and (state["phase"] in {"releasing", "release_failed"}
                     or time.time() >= (state.get("expires_at") or 0))):
            return _release(config, state, save)
        return {**_view(config, state), "released": state["phase"] == "released"}


def _arguments(name, params, width=16384, height=16384):
    schemas = {
        "tap": ({"x", "y"}, set()), "swipe": ({"x1", "y1", "x2", "y2"}, {"duration_ms"}),
        "press": ({"key"}, set()), "type": ({"text"}, set()), "start": ({"package"}, set()),
        "ui": (set(), set()), "apps": (set(), set()), "screenshot": (set(), set()),
    }
    if name not in schemas or not isinstance(params, dict):
        raise ValueError("mobile action is not supported")
    required, optional = schemas[name]
    if not required <= params.keys() <= required | optional:
        raise ValueError("mobile action parameters do not match; identity overrides rejected")
    for key in ("x", "x1", "x2", "y", "y1", "y2"):
        if key in params and (type(params[key]) is not int or not 0 <= params[key] < (
                width if key.startswith("x") else height)):
            raise ValueError("mobile coordinate outside screen")
    if name == "tap":
        return "tap", dict(params)
    if name == "swipe":
        duration = params.get("duration_ms", 350)
        if type(duration) is not int or not 50 <= duration <= 3000:
            raise ValueError("mobile swipe duration is invalid")
        return "swipe", {"start_x": params["x1"], "start_y": params["y1"],
                         "end_x": params["x2"], "end_y": params["y2"], "duration_ms": duration}
    if name == "press":
        value = {"HOME": 3, "BACK": 4, "ENTER": 66, "DELETE": 67}.get(str(params["key"]).upper())
        if value is None:
            raise ValueError("unsupported mobile navigation key")
        return "send_key", {"key": value}
    if name == "type":
        if not isinstance(params["text"], str) or len(params["text"].encode()) > 8192:
            raise ValueError("mobile input text is invalid or too long")
        return "input_text", dict(params)
    if name == "start":
        package = params["package"]
        if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
            raise ValueError("mobile application package is invalid")
        return "start_app", {"start_cmd": f"monkey -p {package} -c android.intent.category.LAUNCHER 1"}
    return {"ui": ("get_all_ui_elements", {"timeout_ms": 1000}),
            "apps": ("get_installed_apps", {"ignore_system_app": False}),
            "screenshot": ("screenshot", {"format": "png"})}[name]


def _screenshot(config, state, reply):
    from native_capability_host import _validated_screenshot
    from personal_runtime import _directory, _root
    images = []
    for block in reply.get("content", []):
        if block.get("type") == "image":
            images.append(_validated_screenshot(block.get("data"), "png"))
        elif block.get("type") == "text":
            value = json.loads(block["text"])
            if isinstance(value, dict) and value.get("type") == "image" and value.get("mime_type") == "image/png":
                images.append(_validated_screenshot(value.get("data"), "png"))
    if len(images) != 1:
        raise ValueError("mobile screenshot did not contain one valid PNG")
    png = images[0]
    root = _root(os.environ["MEYO_LEGION_DIR"])
    # Existing desktop/user containers may be readable directories. Never
    # chmod them just to enable mobile; require own, non-writable-by-others
    # intermediate directories, and a private mobile leaf.
    for leaf in ("users", config["owner"]):
        root = root / leaf
        if root.is_symlink():
            raise ValueError("mobile output parent cannot be a symbolic link")
        root.mkdir(mode=0o700, exist_ok=True)
        info = root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("mobile output parent is not owned")
    root = _root(root / "mobile", create=True)
    name = "phone-" + secrets.token_hex(12) + ".png"
    with _directory(root, private=True) as parent:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(fd, "wb") as output:
        output.write(png)
    width, height = struct.unpack(">II", png[16:24])
    state["screen_size"] = f"{width}x{height}"
    return {"ok": True, "provider": "agentbay", "path": str(root / name), "bytes": len(png),
            "width_height": state["screen_size"], "sha256": hashlib.sha256(png).hexdigest()}


def action(uid, name, params=None):
    config = _configuration(uid)
    with _locked(config) as (state, save):
        dimensions = str(state.get("screen_size") or "16384x16384").split("x")
        tool, arguments = _arguments(name, params or {}, *(int(x) for x in dimensions))
        if state["phase"] != "running" or time.time() >= state["expires_at"]:
            return _error("mobile_not_running", "手机未启动或已到期；本操作没有自动开机。", config, state)
        sandbox = state["sandbox_id"]
        generation = state["created_at"]
    # Ordinary device work never owns the lock required to stop the billable
    # resource. A release/expiry can proceed while this network call is pending.
    try:
        reply = _tool(config, tool, {**arguments, "sandbox_id": sandbox})
    except Exception as exc:
        with _locked(config) as (state, _):
            return _error("mobile_transport_failed",
                          "本次手机操作未确认：" + type(exc).__name__ + "；未自动重试。", config, state)
    with _locked(config) as (state, save):
        if (state.get("sandbox_id") != sandbox or state.get("created_at") != generation
                or state["phase"] != "running" or time.time() >= state["expires_at"]):
            return _error("mobile_session_changed", "手机已释放、到期或切换；旧操作结果不再显示。", config, state)
        try:
            if reply.get("isError"):
                return _error("mobile_tool_failed", _safe("\n".join(_texts(reply))), config, state)
            if name == "screenshot":
                output = _screenshot(config, state, reply)
                save()
                return output
            return {"ok": True, "provider": "agentbay", "action": name,
                    "stdout": _safe("\n".join(_texts(reply)), limit=50_000)}
        except Exception as exc:
            return _error("mobile_transport_failed",
                          "本次手机操作未确认：" + type(exc).__name__ + "；未自动重试。", config, state)


def screenshot(uid):
    return action(uid, "screenshot", {})


def read_screenshot(uid, value):
    """Only an existing screenshot from this owner's mobile root, never arbitrary files."""
    _configuration(uid)
    root = Path(os.environ["MEYO_LEGION_DIR"])
    path = Path(value)
    folder = root / "users" / uid / "mobile"
    if path.parent != folder or not re.fullmatch(r"phone-[a-f0-9]{24}\.png", path.name):
        raise PermissionError("mobile screenshot is outside this owner's file area")
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for leaf in ("users", uid, "mobile"):
            child = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or not 0 < info.st_size <= 8 * 1024 * 1024):
            raise ValueError("invalid mobile screenshot file")
        data = stream.read(8 * 1024 * 1024 + 1)
    from native_capability_host import _validated_screenshot
    return _validated_screenshot(base64.b64encode(data).decode(), "png")
