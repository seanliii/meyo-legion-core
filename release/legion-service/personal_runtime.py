"""Local provisioning and startup for the existing Legion service, not a new runtime."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
KEY_ENV = "MEYO_PERSONAL_PROVIDER_KEY"
MEMBER_KEY_ENV = "MEYO_PERSONAL_MEMBER_KEY_"
MEMBER_BINDINGS_ENV = "LEGION_MEMBER_MODEL_BINDINGS"
MEMBER_PROTOCOLS = ("responses", "anthropic-messages", "openai-completions", "cursor-agent")
MODEL_PROFILE_ENV = ("MEYO_PERSONAL_STATE_DIR", "LEGION_PERSONAL_OWNER")
_MODEL_CONTEXT_OPTIONS = ("context_window", "model_auto_compact_token_limit")
_MODEL_PROFILE_OPTIONS = ("input_modalities", "image_file_cache", *_MODEL_CONTEXT_OPTIONS)
_STATE_DIRS = ("secrets", "home", "codex-home", "data", "legion-home",
               "registry", "repos", "runtimes", "tmp")
_PASSTHROUGH = {
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "no_proxy",
    "VIRTUAL_ENV", "SYSTEMROOT", "WINDIR",
    # Non-secret, optional public dependency reference. The native adapter pins
    # its bytes and still allocates each user's own home, memory and session.
    "LEGION_JCODE_PUBLIC_MODEL_CACHE",
}


def _root(value: Path | str, *, create=False) -> Path:
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("实例目录不能是符号链接")
    if path.resolve().is_relative_to(PROJECT.resolve()):
        raise ValueError("实例数据和凭据必须放在源码目录之外")
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
        raise ValueError("实例目录必须属于当前用户，且仅当前用户可访问（权限0700）")
    return path.resolve()


@contextmanager
def _directory(path: Path, *, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode)
                or (private and (info.st_mode & 0o077 or info.st_uid != os.getuid()))):
            raise ValueError("配置目录的身份或权限无效")
        yield fd
    finally:
        os.close(fd)


def _private(path: Path, *, limit=32768) -> bytes:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("配置及凭据不能是符号链接")
    with _directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_mode & 0o077 or info.st_size > limit
                or (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise ValueError("配置或凭据需为当前用户独占的普通文件（权限0600）")
        value = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("读取期间配置发生变化，请重新执行")
    if len(value) > limit:
        raise ValueError("配置或凭据过大")
    return value


def _read(path: Path) -> dict:
    try:
        value = json.loads(_private(path))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (UnicodeError, json.JSONDecodeError, TypeError):
        raise ValueError("配置文件格式无效") from None


def _write(path: Path, value: bytes):
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("拒绝覆盖符号链接")
    if path.exists():
        _private(path)
    with _directory(path.parent, private=True) as parent:
        temporary = ".meyo-" + secrets.token_hex(12)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(value)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


@contextmanager
def _locked(root: Path):
    import fcntl

    with _directory(root, private=True) as parent:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(".configure.lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        except FileExistsError:
            fd = os.open(".configure.lock", flags, dir_fd=parent)
    with os.fdopen(fd, "a") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("实例配置锁身份或权限无效")
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _configuration(root: Path) -> dict:
    value = _read(root / "config.json")
    if (value.get("schema_version") != 1 or value.get("host") != "127.0.0.1"
            or type(value.get("port")) is not int or not 1 <= value["port"] <= 65535
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", str(value.get("user", "")))):
        raise ValueError("实例配置无效；本地入口只支持回环监听")
    from native_capability_host import validate_ids
    validate_ids(value.get("native_capabilities", []))
    if "public_origin" in value:
        from personal_identity import canonical_public_origin
        if canonical_public_origin(value["public_origin"]) != value["public_origin"]:
            raise ValueError("保存的公网入口必须是规范化 HTTPS 来源")
    if "devices" in value:
        _device_configuration(value["devices"])
    if "mobile" in value:
        if value["mobile"] != {"provider": "agentbay",
                              "config_file": str(root / "secrets/agentbay/config.json")}:
            raise ValueError("手机配置必须属于本实例，不接受其他配置路径")
    for name in _STATE_DIRS:
        _root(root / name)
    return value


def _device_configuration(value: dict) -> dict:
    """An explicit local, non-root device connection; never inherit host Docker."""
    if (not isinstance(value, dict)
            or set(value) != {"docker_host", "desktop_port", "desktop_public_path"}):
        raise ValueError("设备连接配置无效")
    expected = f"unix:///run/user/{os.getuid()}/docker.sock"
    if value["docker_host"] != expected:
        raise ValueError("个人设备只允许当前运行用户的 rootless Docker socket")
    port, path = value["desktop_port"], value["desktop_public_path"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("桌面回环端口必须为1024至65535")
    if (not isinstance(path, str)
            or not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9_-]{0,63}", path)
            or path in {"/api", "/s", "/shared", "/health", "/monitor", "/outputs",
                        "/home-outputs", "/media", "/desktop-outputs", "/auto-outputs"}):
        raise ValueError("桌面入口需为独立的同源路径，不得覆盖已有服务路径")
    return dict(value)


def _login_token(root: Path, user: str) -> str:
    """Reuse the persisted identity, without publishing it or choosing a fallback."""
    # Retain the existing bounded file read; a token's line ending also takes bytes.
    token = _private(root / "login-token.txt").decode().strip()
    record = _read(root / "secrets/personal-tokens.json").get(token) if token else None
    if not isinstance(record, dict) or record.get("user") != user:
        raise ValueError("本地身份配置不一致")
    return token


def dsh_config(state_dir: Path | str) -> dict:
    """Nonsecret client wiring only; no environment mutation or provider request."""
    root = _root(state_dir)
    config = _configuration(root)
    _login_token(root, config["user"])
    return {
        "plugin": "dsh-plugin-legion",
        "config": {"baseUrl": f"http://127.0.0.1:{config['port']}",
                   "tokenFile": str(root / "login-token.txt")},
        "verification": "configuration_only_no_model_request",
    }


def initialize(state_dir: Path | str, user: str, port: int = 8000) -> dict:
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", user)
            or type(port) is not int or not 1 <= port <= 65535):
        raise ValueError("用户名或端口无效")
    root = _root(state_dir, create=True)
    with _locked(root):
        if (root / "config.json").exists():
            config = _configuration(root)
            if config["user"] != user or config["port"] != port:
                raise ValueError("已有实例身份或端口不同；没有修改原配置")
            _login_token(root, user)
        else:
            if any(path.name != ".configure.lock" for path in root.iterdir()):
                raise ValueError("目录已有其它内容或未完成初始化；不覆盖")
            for name in _STATE_DIRS:
                (root / name).mkdir(mode=0o700)
            token = secrets.token_urlsafe(32)
            _write(root / "secrets/personal-tokens.json", _json_bytes({token: {"user": user}}))
            _write(root / "login-token.txt", (token + "\n").encode())
            _write(root / "config.json", _json_bytes({
                "schema_version": 1, "user": user, "host": "127.0.0.1", "port": port,
            }))
    return {"state_dir": str(root), "user": user, "url": f"http://127.0.0.1:{port}",
            "login_token_file": str(root / "login-token.txt"),
            "note": "已准备本地身份；未发模型请求或创建远端账号"}


def _manual_goals(value: bytes) -> bytes:
    """Adopt only the operator-selected manual section, never generated history."""
    text = value.decode("utf-8")
    if text.count("[MANUAL]") != 1 or text.count("[/MANUAL]") != 1:
        raise ValueError("方向文件必须有唯一的 MANUAL 区")
    start, end = text.index("[MANUAL]"), text.index("[/MANUAL]")
    prefix, body = text[:start], text[start + len("[MANUAL]"):end].strip()
    if (end <= start or not body or "[AUTO]" in body or "[/AUTO]" in body
            or prefix.rfind("[AUTO]") > prefix.rfind("[/AUTO]")):
        raise ValueError("MANUAL 区必须非空，且不能嵌在 AUTO 区")
    result = ("[MANUAL]\n" + body + "\n[/MANUAL]\n").encode("utf-8")
    if len(value) > 32768 or len(result) > 32768:
        raise ValueError("方向文件过大")
    return result


def _chosen_goals(path: Path) -> bytes:
    """A selected template may be public-readable, but must be a bounded real file."""
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("方向来源不能是符号链接")
    with _directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 32768:
            raise ValueError("方向来源必须是有限大小的普通文件")
        value = stream.read(32769)
        after = os.fstat(stream.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("读取期间方向来源发生变化")
    return _manual_goals(value)


def _standing_direction(root: Path) -> dict:
    path = root / "pilot-goals.md"
    result = {"configured": False, "path": str(path),
              "source": "standing_user_direction", "is_user_message": False}
    if path.exists() or path.is_symlink():
        value = _private(path)
        _manual_goals(value)
        result.update(configured=True, sha256=hashlib.sha256(value).hexdigest())
    return result


def configure_goals(state_dir: Path | str, source_file: Path | str | None = None) -> dict:
    """Save or clear this instance's direction, without changing identity or authority."""
    root = _root(state_dir)
    with _locked(root):
        _configuration(root)
        path = root / "pilot-goals.md"
        present = path.exists() or path.is_symlink()
        if present:
            # Recover malformed text through the normal entry point, without
            # relaxing ownership, link, size or private-file checks.
            _private(path)
        if source_file is None:
            if present:
                with _directory(root, private=True) as parent:
                    os.unlink(path.name, dir_fd=parent)
                    os.fsync(parent)
        else:
            value = _chosen_goals(Path(source_file).expanduser().absolute())
            _write(path, value)
        return _standing_direction(root)


def environment(state_dir: Path | str, inherited: dict | None = None) -> tuple[dict, dict]:
    """Read persisted settings into a new environment, without modifying this process."""
    root = _root(state_dir)
    config = _configuration(root)
    _standing_direction(root)  # Validate an existing file; absent means no adopted direction.
    source = os.environ if inherited is None else inherited
    env = {name: value for name, value in source.items() if name in _PASSTHROUGH}
    env.update({
        "HOME": str(root / "home"), "CODEX_HOME": str(root / "codex-home"),
        "TMPDIR": str(root / "tmp"), "MEYO_LEGION_DIR": str(root / "data"),
        "LEGION_HOME_DIR": str(root / "legion-home"),
        "LEGION_SESSIONS_DIR": str(root / "legion-home/sessions"),
        "LEGION_TASK_MEMORY_DIR": str(root / "data/task_memory"),
        "LEGION_TEAM_DIR": str(root / "data/teams"),
        "LEGION_PROJECT_CONTEXT_DIR": str(root / "data/project_context"),
        "LEGION_PROC_REGISTRY_DIR": str(root / "registry"),
        "LEGION_PERSONAL_TOKENS_FILE": str(root / "secrets/personal-tokens.json"),
        "LEGION_PERSONAL_ACCOUNTS_DIR": str(root / "secrets/accounts"),
        "LEGION_PERSONAL_OWNER": config["user"],
        # Always bind to this instance, including after clear. Do not adopt a
        # shell setting or fall back to a previous checkout's owner direction.
        "LEGION_PILOT_GOALS": str(root / "pilot-goals.md"),
        # A non-secret source reference for host tools, not a task/model grant.
        "MEYO_PERSONAL_STATE_DIR": str(root),
        "LEGION_EXTERNAL_SOURCE_ROOT": str(root / "repos"),
        "LEGION_EXTERNAL_RUNTIME_ROOT": str(root / "runtimes"),
        "MEYO_ORCH_DIR": str(PROJECT / "harness_baselines/meyo-orchestrator"),
        "LEGION_CODEX_TRANSPORT": "responses",
        # A configured provider is not a user choice of worker. Auto requests
        # reach the existing native planner; explicit candidate choices still bind.
        "MEYO_PERSONAL_DEFAULT_HARNESS": "",
        "LEGION_ALLOW_UNAUTHENTICATED_LOCAL_CLI": "0", "LEGION_ALLOW_REMOTE_BIND": "0",
        "LEGION_EXT_AUTO_UPDATE": "0", "PYTHONDONTWRITEBYTECODE": "1",
    })
    if config.get("native_capabilities"):
        env["MEYO_NATIVE_CAPABILITY_IDS"] = json.dumps(config["native_capabilities"])
    if "public_origin" in config:
        env["LEGION_PUBLIC_ORIGIN"] = config["public_origin"]
    if "devices" in config:
        devices = _device_configuration(config["devices"])
        env.update({
            "MEYO_DEVICE_DOCKER_HOST": devices["docker_host"],
            "MEYO_DEVICE_DOCKER_DATA_ROOT": str(root / "home/.local/share/docker"),
            "MEYO_DESKTOP_PUBLISH_PORT": str(devices["desktop_port"]),
            "MEYO_DESKTOP_PUBLIC_PATH": devices["desktop_public_path"],
            "MEYO_DESKTOP_OWNER": config["user"],
        })
    if "mobile" in config:
        env["MEYO_AGENTBAY_CONFIG"] = config["mobile"]["config_file"]
        from agentbay_mobile import _configuration as _mobile_configuration
        _mobile_configuration(config["user"], environment=env)
    _login_token(root, config["user"])
    model_path = root / "secrets/model.json"
    if model_path.exists() or model_path.is_symlink():
        provider = _profile_binding(_read(model_path))
        env[KEY_ENV] = provider["api_key"]
        env["LEGION_CODEX_RESPONSES_BINDING"] = json.dumps({
            "model": provider["model"], "base_url": provider["base_url"], "api_key_env": KEY_ENV,
            **{name: provider[name] for name in _MODEL_PROFILE_OPTIONS if name in provider},
        })
    members = _member_models(root)
    if members:
        # Each member gets only its own declared candidate. Keys stay in private
        # environment slots; the declaration itself carries names, not values.
        profiles = _model_profiles(root)
        declared = {}
        for index, (member, choice) in enumerate(sorted(members.items())):
            if choice["profile"] not in profiles:
                raise ValueError("成员模型绑定引用了不存在的候选；未启动")
            provider = _profile_binding(profiles[choice["profile"]])
            slot = f"{MEMBER_KEY_ENV}{index}"
            env[slot] = provider["api_key"]
            declared[member] = {"model": provider["model"], "base_url": provider["base_url"],
                                "api_key_env": slot, "protocol": choice["protocol"]}
        env[MEMBER_BINDINGS_ENV] = json.dumps(declared)
    return env, config


def configure_mobile(state_dir: Path | str, key_file: Path | str, *,
                     session_seconds: int = 300, total_seconds: int = 1200) -> dict:
    """Configure the existing mobile entry, with a finite operator-granted time budget."""
    root = _root(state_dir)
    _configuration(root)
    if (type(session_seconds) is not int or type(total_seconds) is not int
            or not 60 <= session_seconds <= 300
            or not session_seconds <= total_seconds <= 1200):
        raise ValueError("当前手机单次授权需60至300秒，总授权不超过1200秒")
    key = _private(Path(key_file).expanduser().absolute(), limit=4096).decode().strip()
    if not re.fullmatch(r"akm-[A-Za-z0-9_-]{16,180}", key):
        raise ValueError("手机专用 Key 格式无效；没有保存")
    with _locked(root):
        config = _configuration(root)
        folder = _root(root / "secrets/agentbay", create=True)
        control = _root(folder / "control", create=True)
        from agentbay_mobile import control_lock
        with control_lock(control):
            if (control / "phone.json").exists():
                running = _read(control / "phone.json")
                if running.get("phase") not in {"idle", "released"}:
                    raise ValueError("手机尚未确认释放，不能更换提供方配置")
                if running.get("owner") != config["user"]:
                    raise ValueError("已有手机归属与实例不一致，未重置")
            _write(folder / "api-key.txt", (key + "\n").encode())
            _write(folder / "config.json", _json_bytes({
                "schema_version": 1, "owner": config["user"],
                "api_key_file": str(folder / "api-key.txt"), "control_dir": str(control),
                "session_seconds": session_seconds, "total_seconds": total_seconds,
            }))
            config["mobile"] = {"provider": "agentbay", "config_file": str(folder / "config.json")}
            _write(root / "config.json", _json_bytes(config))
    return {"configured": True, "provider": "agentbay", "session_seconds": session_seconds,
            "total_seconds": total_seconds, "runtime_verified": False,
            "note": "已保存显式手机授权；未启动手机，未重置既有时长使用记录"}


def configure_model(state_dir: Path | str, model: str, base_url: str, key_file: Path | str,
                    *, input_modalities: list[str] | None = None,
                    context_window: int | None = None,
                    model_auto_compact_token_limit: int | None = None,
                    image_file_cache: dict | None = None) -> dict:
    root = _root(state_dir)
    _configuration(root)
    key = _private(Path(key_file).expanduser().absolute(), limit=4096).decode().strip()
    from model_registry import explicit_responses_binding

    validated = explicit_responses_binding({
        "LEGION_CODEX_RESPONSES_BINDING": json.dumps({
            "model": model, "base_url": base_url, "api_key_env": KEY_ENV,
            **({"input_modalities": input_modalities} if input_modalities is not None else {}),
            **({"context_window": context_window} if context_window is not None else {}),
            **({"model_auto_compact_token_limit": model_auto_compact_token_limit}
               if model_auto_compact_token_limit is not None else {}),
            **({"image_file_cache": image_file_cache} if image_file_cache is not None else {}),
        }), KEY_ENV: key,
    })
    with _locked(root):
        _configuration(root)
        _write(root / "secrets/model.json", _json_bytes(validated))
    return {"configured": True, "model": validated["model"],
            "provider_verified": False, "note": "配置已保存；未验证模型调用"}


def _profile_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("模型配置 ID 需为1至64位字母、数字、下划线、点或横线，且以字母数字开头")
    return value


def _profile_binding(value: dict) -> dict:
    """Preserve optional input facts in the same private candidate/active binding."""
    required = {"model", "base_url", "api_key"}
    if (not isinstance(value, dict)
            or not required <= set(value) <= required | set(_MODEL_PROFILE_OPTIONS)):
        raise ValueError("模型配置格式无效")
    from model_registry import explicit_responses_binding

    return explicit_responses_binding({
        "LEGION_CODEX_RESPONSES_BINDING": json.dumps({
            "model": value["model"], "base_url": value["base_url"], "api_key_env": KEY_ENV,
            **{name: value[name] for name in _MODEL_PROFILE_OPTIONS if name in value},
        }), KEY_ENV: value["api_key"],
    })


def _model_profiles(root: Path) -> dict:
    path = root / "secrets/model-profiles.json"
    if not path.exists() and not path.is_symlink():
        return {}
    profiles = _read(path)
    for profile_id, binding in profiles.items():
        _profile_id(profile_id)
        _profile_binding(binding)
    return profiles


class ModelProfileError(ValueError):
    """Fixed public errors only; never include paths, configuration or credentials."""


def model_tool_workspace(environment: dict, *, require_bound=False) -> Path:
    """Resolve a parent-bound workspace, optionally pinned across a host/child boundary."""
    value = environment.get("MEYO_WORKDIR")
    if value is None and not require_bound:
        value = os.getcwd()
    if (not isinstance(value, str) or not value or not Path(value).is_absolute()
            or ".." in Path(value).parts or Path(value).is_symlink()):
        raise ValueError("invalid model tool workspace")
    workspace = Path(value).resolve(strict=True)
    info = workspace.stat()
    if (not stat.S_ISDIR(info.st_mode) or workspace == Path(workspace.anchor)
            or info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("invalid model tool workspace")
    if "MEYO_MODEL_WORKSPACE_ID" in environment:
        identity = json.loads(environment["MEYO_MODEL_WORKSPACE_ID"])
        if (not isinstance(identity, list) or len(identity) != 2
                or any(type(item) is not int or item < 0 for item in identity)
                or identity != [info.st_dev, info.st_ino]):
            raise ValueError("model tool workspace was replaced")
    return workspace


def model_profile_context(environment: dict) -> tuple[Path, Path]:
    """Validate the OS owner AND business caller before reading any candidate key.

    Only the trusted parent supplies this environment. In particular, a profile
    ID is not authority and the service's owner is not a substitute for a caller.
    """
    required = (*MODEL_PROFILE_ENV, "MEYO_USER_ID", "MEYO_WORKDIR")
    if any(not isinstance(environment.get(key), str) or not environment[key] for key in required):
        raise ModelProfileError("media_profile_binding_required")
    try:
        declared = Path(environment["MEYO_PERSONAL_STATE_DIR"])
        if not declared.is_absolute() or ".." in declared.parts:
            raise ValueError()
        root = _root(declared)
        if root != declared:
            raise ValueError()  # The launcher exports a canonical, non-linked source.
        config = _configuration(root)
    except (OSError, ValueError, TypeError):
        raise ModelProfileError("media_profile_configuration_invalid") from None
    if not config["user"] == environment["LEGION_PERSONAL_OWNER"] == environment["MEYO_USER_ID"]:
        raise ModelProfileError("media_profile_owner_mismatch")
    try:
        workspace = model_tool_workspace(environment, require_bound=True)
        # Tasks may live under instance data/home, but never contain the private
        # root, enter secrets, or make the private configuration task-writable.
        if root.is_relative_to(workspace) or workspace.is_relative_to(root / "secrets"):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise ModelProfileError("media_profile_workspace_invalid") from None
    return root, workspace


def resolve_model_profile(profile_id: str, *, environment: dict) -> dict:
    """One read-only private snapshot; no default, lock, activation or company fallback."""
    try:
        profile_id = _profile_id(profile_id)
    except ValueError:
        raise ModelProfileError("media_profile_id_invalid") from None
    root, _ = model_profile_context(environment)
    try:
        profiles = _model_profiles(root)
        if profile_id not in profiles:
            raise ModelProfileError("media_profile_unknown")
        return _profile_binding(profiles[profile_id])
    except ModelProfileError:
        raise
    except (OSError, ValueError, TypeError):
        raise ModelProfileError("media_profile_configuration_invalid") from None


def _write_model_profiles(root: Path, profiles: dict):
    content = _json_bytes(profiles)
    # Never persist a library that the existing bounded private reader cannot reload.
    if len(content) > 32768:
        raise ValueError("模型候选库过大；未修改配置")
    _write(root / "secrets/model-profiles.json", content)


def _current_model(root: Path) -> dict | None:
    path = root / "secrets/model.json"
    if not path.exists() and not path.is_symlink():
        return None
    return _profile_binding(_read(path))


def save_model_profile(state_dir: Path | str, profile_id: str, model: str,
                       base_url: str, key_file: Path | str,
                       *, input_modalities: list[str] | None = None,
                       context_window: int | None = None,
                       model_auto_compact_token_limit: int | None = None,
                       image_file_cache: dict | None = None) -> dict:
    """Append one private candidate; never change the actual runtime binding."""
    root = _root(state_dir)
    profile_id = _profile_id(profile_id)
    with _locked(root):
        _configuration(root)
        profiles = _model_profiles(root)
        if profile_id in profiles:
            raise ValueError("模型配置 ID 已存在；请另选 ID，不覆盖已有候选")
        key = _private(Path(key_file).expanduser().absolute(), limit=4096).decode().strip()
        binding = _profile_binding({
            "model": model, "base_url": base_url, "api_key": key,
            **({"input_modalities": input_modalities} if input_modalities is not None else {}),
            **({"context_window": context_window} if context_window is not None else {}),
            **({"model_auto_compact_token_limit": model_auto_compact_token_limit}
               if model_auto_compact_token_limit is not None else {}),
            **({"image_file_cache": image_file_cache} if image_file_cache is not None else {}),
        })
        profiles[profile_id] = binding
        _write_model_profiles(root, profiles)
    return {"saved": True, "id": profile_id, "model": binding["model"],
            "base_url": binding["base_url"], "provider_verified": False,
            "note": "仅保存候选，未改变当前配置；未验证模型调用"}


def list_model_profiles(state_dir: Path | str) -> dict:
    """Current is derived from model.json, including key equality; never a second pointer."""
    root = _root(state_dir)
    with _locked(root):
        _configuration(root)
        profiles = _model_profiles(root)
        current = _current_model(root)
        return {"profiles": [
            {"id": profile_id, "model": binding["model"], "base_url": binding["base_url"],
             "current": _profile_binding(binding) == current}
            for profile_id, binding in sorted(profiles.items())
        ]}


def activate_model_profile(state_dir: Path | str, profile_id: str) -> dict:
    """Persist rollback first, then atomically replace the sole binding model.json."""
    root = _root(state_dir)
    profile_id = _profile_id(profile_id)
    with _locked(root):
        _configuration(root)
        profiles = _model_profiles(root)
        if profile_id not in profiles:
            raise ValueError("模型配置 ID 不存在；未修改当前配置")
        binding = _profile_binding(profiles[profile_id])
        current = _current_model(root)
        changed = current != binding
        rollback = None
        if changed:
            if current is not None:
                rollback = next((name for name, value in sorted(profiles.items())
                                 if _profile_binding(value) == current), None)
                if rollback is None:
                    rollback = "previous-" + secrets.token_hex(8)
                    while rollback in profiles:
                        rollback = "previous-" + secrets.token_hex(8)
                    profiles[rollback] = current
                    _write_model_profiles(root, profiles)
            _write(root / "secrets/model.json", _json_bytes(binding))
    return {"activated": True, "id": profile_id, "model": binding["model"],
            "base_url": binding["base_url"], "changed": changed, "rollback_profile": rollback,
            "provider_verified": False,
            "note": "仅保存运行绑定；须在无活任务的维护窗口按原方式重启生效；未验证模型调用"}


def _member_id(value: str) -> str:
    if (not isinstance(value, str) or value == "codex"
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", value)):
        raise ValueError("成员 ID 需为1至64位小写字母、数字、下划线、点或横线；"
                         "Codex 使用当前模型配置，不在此绑定")
    return value


def _member_models(root: Path) -> dict:
    """Member -> saved candidate + wire protocol. It names candidates, never keys."""
    path = root / "secrets/member-models.json"
    if not path.exists() and not path.is_symlink():
        return {}
    members = _read(path)
    for member, choice in members.items():
        _member_id(member)
        if (not isinstance(choice, dict) or set(choice) != {"profile", "protocol"}
                or choice["protocol"] not in MEMBER_PROTOCOLS):
            raise ValueError("成员模型绑定格式无效")
        _profile_id(choice["profile"])
    return members


def bind_member_model(state_dir: Path | str, member: str, profile_id: str, protocol: str) -> dict:
    """Give one member its own saved candidate; no other member or default changes."""
    root = _root(state_dir)
    member, profile_id = _member_id(member), _profile_id(profile_id)
    if protocol not in MEMBER_PROTOCOLS:
        raise ValueError("协议需为 responses、anthropic-messages、openai-completions 或 cursor-agent")
    with _locked(root):
        _configuration(root)
        profiles = _model_profiles(root)
        if profile_id not in profiles:
            raise ValueError("模型配置 ID 不存在；未修改成员绑定")
        binding = _profile_binding(profiles[profile_id])
        from model_registry import CURSOR_AGENT_BASE_URL, member_protocol_matches_address
        if not member_protocol_matches_address(protocol, binding["base_url"]):
            raise ValueError(f"cursor-agent 只能绑定地址为 {CURSOR_AGENT_BASE_URL} 的候选，"
                             "该地址的候选也只能用 cursor-agent；未修改成员绑定")
        members = _member_models(root)
        members[member] = {"profile": profile_id, "protocol": protocol}
        _write(root / "secrets/member-models.json", _json_bytes(members))
    return {"bound": True, "member": member, "id": profile_id, "model": binding["model"],
            "base_url": binding["base_url"], "protocol": protocol, "provider_verified": False,
            "note": "仅保存成员绑定；须在无活任务的维护窗口按原方式重启生效；未验证模型调用"}


def unbind_member_model(state_dir: Path | str, member: str) -> dict:
    root = _root(state_dir)
    member = _member_id(member)
    with _locked(root):
        _configuration(root)
        members = _member_models(root)
        removed = members.pop(member, None) is not None
        if removed:
            _write(root / "secrets/member-models.json", _json_bytes(members))
    return {"unbound": removed, "member": member,
            "note": "须在无活任务的维护窗口按原方式重启生效" if removed else "该成员没有绑定；未修改"}


def list_member_models(state_dir: Path | str) -> dict:
    """Names and addresses only; never a key."""
    root = _root(state_dir)
    with _locked(root):
        _configuration(root)
        profiles = _model_profiles(root)
        rows = []
        for member, choice in sorted(_member_models(root).items()):
            saved = profiles.get(choice["profile"])
            binding = _profile_binding(saved) if saved is not None else None
            rows.append({"member": member, "id": choice["profile"], "protocol": choice["protocol"],
                         "model": binding["model"] if binding else None,
                         "base_url": binding["base_url"] if binding else None,
                         "candidate_present": binding is not None})
        return {"members": rows}


def configure_capabilities(state_dir: Path | str, tool_ids: list[str]) -> dict:
    """Explicit local-owner grant; never enable future/unlisted actions implicitly."""
    from native_capability_host import declared_capability_actions, validate_ids
    root = _root(state_dir)
    selected = validate_ids(tool_ids)
    declared = {row["id"] for row in declared_capability_actions()["actions"]}
    if not set(selected) <= declared:
        raise ValueError("只能授权现有目录已声明的能力动作")
    with _locked(root):
        config = _configuration(root)
        config["native_capabilities"] = selected
        _write(root / "config.json", _json_bytes(config))
    return {"native_capabilities": selected, "runtime_verified": False,
            "note": "仅保存显式本机能力授权，未执行工具或提交模型任务"}


def configure_public_origin(state_dir: Path | str, origin: str | None) -> dict:
    """Keep the server loopback-only; authorize one operator-managed TLS proxy."""
    from personal_identity import canonical_public_origin
    root = _root(state_dir)
    validated = canonical_public_origin(origin) if origin is not None else None
    with _locked(root):
        config = _configuration(root)
        if validated is None:
            config.pop("public_origin", None)
        else:
            config["public_origin"] = validated
        _write(root / "config.json", _json_bytes(config))
    return {"public_origin": validated, "host": config["host"], "runtime_verified": False,
            "note": "配置已保存，服务重启后生效；未部署代理、公开服务或改变个人凭据"}


def configure_devices(state_dir: Path | str, docker_host: str, desktop_port: int,
                      desktop_public_path: str = "/cloud-desktop") -> dict:
    root = _root(state_dir)
    devices = _device_configuration({
        "docker_host": docker_host, "desktop_port": desktop_port,
        "desktop_public_path": desktop_public_path,
    })
    with _locked(root):
        config = _configuration(root)
        config["devices"] = devices
        _write(root / "config.json", _json_bytes(config))
    return {"devices": devices, "owner": config["user"], "runtime_verified": False,
            "note": "仅保存已有设备连接，重启服务后生效；未安装、启动设备或修改身份"}


def main(argv=None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="军团本地实例：初始化、配置和启动原服务")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "configure-model", "save-model-profile", "list-model-profiles",
                 "activate-model-profile", "bind-member-model", "unbind-member-model",
                 "list-member-models", "install-member", "configure-capabilities", "configure-goals",
                 "configure-public-origin", "configure-devices", "configure-mobile",
                 "reap-devices", "dsh-config", "check", "serve"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", type=Path, required=True)
        if name == "init":
            command.add_argument("--user", required=True)
            command.add_argument("--port", type=int, default=8000)
        elif name in ("configure-model", "save-model-profile"):
            if name == "save-model-profile":
                command.add_argument("--id", required=True, help="候选名称；已有 ID 不覆盖")
            command.add_argument("--model", required=True)
            command.add_argument("--base-url", required=True)
            command.add_argument("--input-modality", action="append", choices=("text", "image"),
                                 help="已确认的模型输入能力，可重复；省略则保持未知，不推断视觉能力")
            command.add_argument("--api-key-file", type=Path, required=True)
            command.add_argument("--context-window", type=int,
                                 help="该绑定已确认的模型容量；省略则保持未知")
            command.add_argument("--model-auto-compact-token-limit", type=int,
                                 help="明确选择的原生上下文整理阈值；未指定不替用户决定")
            command.add_argument("--image-file-cache-ttl-seconds", type=int,
                                 help="显式启用同提供方 Files 图片复用，TTL 3600..2592000 秒；默认关闭")
            command.add_argument("--image-file-cache-default-detail", choices=("high", "original", "auto"),
                                 help="已核实的省略 detail 语义；不声明则省略 detail 的图片保持 inline")
        elif name == "activate-model-profile":
            command.add_argument("--id", required=True, help="明确选择已保存的候选")
        elif name == "install-member":
            command.add_argument("--member", required=True, help="成员目录里的 ID，例如 pi、qwen-code")
        elif name in ("bind-member-model", "unbind-member-model"):
            command.add_argument("--member", required=True, help="成员 ID，例如 cc、pi；不含 codex")
            if name == "bind-member-model":
                command.add_argument("--id", required=True, help="已保存的候选 ID")
                command.add_argument("--protocol", required=True, choices=MEMBER_PROTOCOLS,
                                     help="该候选地址实际提供的协议；不猜测、不转换")
        elif name == "configure-capabilities":
            command.add_argument("--tool", action="append", default=[],
                                 help="明确授权的 external_capability 或 local_mcp 工具 ID，可重复")
        elif name == "configure-goals":
            choice = command.add_mutually_exclusive_group(required=True)
            choice.add_argument("--file", type=Path, help="明确采用该文件唯一的 MANUAL 区")
            choice.add_argument("--clear", action="store_true", help="仅清除本实例长期方向")
        elif name == "configure-public-origin":
            choice = command.add_mutually_exclusive_group(required=True)
            choice.add_argument("--origin", help="由同机反向代理提供的唯一 HTTPS 来源")
            choice.add_argument("--clear", action="store_true", help="移除公网入口，重启后仅本机可用")
        elif name == "configure-devices":
            command.add_argument("--docker-host", required=True,
                                 help="当前用户的 unix:///run/user/<uid>/docker.sock")
            command.add_argument("--desktop-port", type=int, required=True,
                                 help="仅监听127.0.0.1的noVNC固定端口")
            command.add_argument("--desktop-public-path", default="/cloud-desktop",
                                 help="同源反向代理路径；不是外部站点URL")
        elif name == "configure-mobile":
            command.add_argument("--api-key-file", type=Path, required=True)
            command.add_argument("--session-seconds", type=int, default=300)
            command.add_argument("--total-seconds", type=int, default=1200)
    args = parser.parse_args(argv)
    try:
        # Resolve caller-relative paths before replacing HOME with the instance.
        # Keep symlinks unresolved so the existing no-follow checks still apply.
        args.state_dir = args.state_dir.expanduser().absolute()
        if args.command in ("configure-model", "save-model-profile"):
            args.api_key_file = args.api_key_file.expanduser().absolute()
            args.image_file_cache = None
            if args.image_file_cache_default_detail and args.image_file_cache_ttl_seconds is None:
                raise ValueError("默认图片语义必须与显式 Files TTL 一起声明")
            if args.image_file_cache_ttl_seconds is not None:
                args.image_file_cache = {"profile": "deepseek-files-v1",
                                        "ttl_seconds": args.image_file_cache_ttl_seconds}
                if args.image_file_cache_default_detail:
                    args.image_file_cache["default_detail"] = args.image_file_cache_default_detail
        if args.command == "init":
            result = initialize(args.state_dir, args.user, args.port)
        elif args.command == "dsh-config":
            result = dsh_config(args.state_dir)
        elif args.command == "configure-goals":
            result = configure_goals(args.state_dir, None if args.clear else args.file)
        elif args.command == "configure-public-origin":
            result = configure_public_origin(args.state_dir, None if args.clear else args.origin)
        elif args.command == "configure-devices":
            result = configure_devices(args.state_dir, args.docker_host,
                                       args.desktop_port, args.desktop_public_path)
        elif args.command == "configure-mobile":
            result = configure_mobile(args.state_dir, args.api_key_file,
                                      session_seconds=args.session_seconds,
                                      total_seconds=args.total_seconds)
        elif args.command == "save-model-profile":
            result = save_model_profile(args.state_dir, args.id, args.model,
                                        args.base_url, args.api_key_file,
                                        input_modalities=args.input_modality,
                                        context_window=args.context_window,
                                        model_auto_compact_token_limit=args.model_auto_compact_token_limit,
                                        image_file_cache=args.image_file_cache)
        elif args.command == "list-model-profiles":
            result = list_model_profiles(args.state_dir)
        elif args.command == "activate-model-profile":
            result = activate_model_profile(args.state_dir, args.id)
        elif args.command == "bind-member-model":
            result = bind_member_model(args.state_dir, args.member, args.id, args.protocol)
        elif args.command == "unbind-member-model":
            result = unbind_member_model(args.state_dir, args.member)
        elif args.command == "list-member-models":
            result = list_member_models(args.state_dir)
        else:
            env, config = environment(args.state_dir)
            # Set all state/credential sources before importing the existing service.
            os.environ.clear()
            os.environ.update(env)
            if args.command == "install-member":
                # 装进本实例自己的 repos/ 与 runtimes/；不调用模型，也不替它绑定模型。
                import external_agent_registry
                report = external_agent_registry.install(_member_id(args.member), confirm=True)
                result = {"member": args.member, "ok": bool(report.get("ok")),
                          "error": str(report.get("error") or "")[:400],
                          "note": "只完成安装；仍需 bind-member-model 并重启服务后才进入派单池"}
            elif args.command == "reap-devices":
                if "mobile" in config:
                    import agentbay_mobile
                    result = agentbay_mobile.reap(config["user"])
                else:
                    result = {"ok": True, "configured": False}
            elif args.command == "configure-model":
                result = configure_model(args.state_dir, args.model, args.base_url,
                                         args.api_key_file, input_modalities=args.input_modality,
                                         context_window=args.context_window,
                                         model_auto_compact_token_limit=args.model_auto_compact_token_limit,
                                         image_file_cache=args.image_file_cache)
            elif args.command == "configure-capabilities":
                result = configure_capabilities(args.state_dir, args.tool)
            else:
                import model_registry
                status = model_registry.explicit_responses_status("codex")
                if "LEGION_CODEX_RESPONSES_BINDING" in env and not status["configured"]:
                    raise ValueError("保存的模型配置无效；未启动")
                if args.command == "check":
                    import shutil
                    result = {**status, "user": config["user"],
                              "standing_direction": _standing_direction(args.state_dir),
                              "url": config.get("public_origin") or f"http://127.0.0.1:{config['port']}",
                              "listen_url": f"http://127.0.0.1:{config['port']}",
                              "codex_executable_found": bool(shutil.which("codex")),
                              "readiness_basis": "configuration_only_no_model_request"}
                else:
                    import server
                    sys.argv = [str(HERE / "server.py"), "--host", config["host"], "--port", str(config["port"])]
                    server.main()
                    return 0
        print(json.dumps(result, ensure_ascii=False))
        return 1 if (args.command in ("reap-devices", "install-member")
                     and result.get("ok") is False) else 0
    except (ValueError, OSError, UnicodeError):
        print("本地配置不可用：请核对实例目录、用户、端口和私有文件权限；未改用其它凭据。",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
