"""Bind JCode's native named profile to an existing CCR route, without requests.

The returned argv selects the initial native route; it does not prove that a
later native restore kept that model. Untagged checkpoints are left to the caller.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import stat
import tomllib
from urllib.parse import urlsplit

_LOCK_NAME = ".meyo-ccr-profile.lock"
_CONFIG = "config.toml"


def _loopback_base(value) -> str:
    if (not isinstance(value, str) or not value
            or any(ord(char) <= 32 for char in value)
            or any(char in value for char in ("\\", "?", "#"))):
        raise ValueError("JCode CCR base must be a plain loopback URL")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme not in {"http", "https"} or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            raise ValueError
        if host != "localhost":
            address = ipaddress.ip_address(host)
            address = getattr(address, "ipv4_mapped", None) or address
            if not address.is_loopback:
                raise ValueError
    except ValueError:
        raise ValueError("JCode CCR base must be a plain loopback URL") from None
    return value.rstrip("/")


def _stamp(info):
    if info is None:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _owned_regular(info, label: str, *, private: bool = False) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022
            or (private and stat.S_IMODE(info.st_mode) & 0o077)):
        raise ValueError(f"JCode {label} must be an owned regular non-shared file")


def _read_config(directory_fd: int):
    try:
        fd = os.open(_CONFIG, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
    except FileNotFoundError:
        return b"", None
    except OSError:
        raise ValueError("JCode config.toml cannot be safely opened") from None
    try:
        before = os.fstat(fd)
        _owned_regular(before, "config.toml")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            content = stream.read()
        if _stamp(os.fstat(fd)) != _stamp(before):
            raise ValueError("JCode config.toml changed while reading")
        return content, before
    finally:
        os.close(fd)


def _unchanged(directory_fd: int, before) -> None:
    try:
        current = os.stat(_CONFIG, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        current = None
    if current is not None:
        _owned_regular(current, "config.toml")
    if _stamp(current) != _stamp(before):
        raise ValueError("JCode config.toml changed before profile update")


def _same_home(home: Path, directory_fd: int) -> None:
    current, opened = home.lstat(), os.fstat(directory_fd)
    if (not stat.S_ISDIR(current.st_mode) or current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) & 0o022
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
        raise ValueError("JCode home must remain the same owned directory")


def _profile_matches(existing, declaration: dict) -> bool:
    if not isinstance(existing, dict):
        return False
    connection = {key: value for key, value in declaration.items() if key != "models"}
    if {key: value for key, value in existing.items() if key != "models"} != connection:
        return False
    requested = declaration.get("models")
    if requested is None:
        # An older/unknown catalog must not erase native model settings.
        return True
    current = existing.get("models")
    if not isinstance(current, list):
        return False
    for model in requested:
        matches = [row for row in current
                   if isinstance(row, dict) and row.get("id") == model["id"]]
        if len(matches) != 1:
            return False
        inputs = matches[0].get("input")
        if (not isinstance(inputs, list) or any(not isinstance(item, str) for item in inputs)
                or set(inputs) != set(model["input"])):
            return False
    return True


def _ensure_profile(home: Path, profile: str, declaration: dict) -> None:
    import fcntl

    if not home.is_absolute() or home.is_symlink():
        raise ValueError("JCode home must be an absolute non-symlink directory")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = None
    try:
        _same_home(home, directory_fd)
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                lock_fd = os.open(_LOCK_NAME, flags | os.O_CREAT | os.O_EXCL,
                                  0o600, dir_fd=directory_fd)
            except FileExistsError:
                lock_fd = os.open(_LOCK_NAME, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise ValueError(f"JCode profile lock cannot be safely opened (errno={exc.errno})") from None
        _owned_regular(os.fstat(lock_fd), "profile lock", private=True)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_path = os.stat(_LOCK_NAME, dir_fd=directory_fd, follow_symlinks=False)
        _owned_regular(lock_path, "profile lock", private=True)
        if (lock_path.st_dev, lock_path.st_ino) != (
                os.fstat(lock_fd).st_dev, os.fstat(lock_fd).st_ino):
            raise ValueError("JCode profile lock changed")
        original, before = _read_config(directory_fd)
        try:
            document = tomllib.loads(original.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            raise ValueError("JCode config.toml is not valid UTF-8 TOML") from None
        providers = document.get("providers", {})
        if not isinstance(providers, dict):
            raise ValueError("JCode provider configuration conflicts with a named profile")
        connection = {key: value for key, value in declaration.items() if key != "models"}
        extending_models = False
        if profile in providers:
            if _profile_matches(providers[profile], declaration):
                _same_home(home, directory_fd)
                _unchanged(directory_fd, before)
                return
            # Only add missing model metadata to this exact managed connection.
            # Never rewrite an existing native model table or alter its route.
            if declaration.get("models") and providers[profile] == connection:
                extending_models = True
            else:
                raise ValueError("JCode CCR named profile conflict")
        fragment = "" if extending_models else "\n[providers." + profile + "]\n" + "".join(
            key + " = " + (str(value).lower() if isinstance(value, bool)
                          else json.dumps(value, ensure_ascii=False))
            + "\n" for key, value in connection.items())
        for model in declaration.get("models", []):
            fragment += (
                "\n[[providers." + profile + ".models]]\n"
                "id = " + json.dumps(model["id"], ensure_ascii=False) + "\n"
                "input = " + json.dumps(model["input"], ensure_ascii=False) + "\n")
        payload = original + fragment.encode("utf-8")
        # Inline/otherwise sealed tables cannot be extended without rewriting
        # user bytes. Reject them rather than silently serializing all config.
        try:
            combined = tomllib.loads(payload.decode("utf-8"))
        except tomllib.TOMLDecodeError:
            raise ValueError("JCode config.toml does not permit an additive profile") from None
        if combined.get("providers", {}).get(profile) != declaration:
            raise ValueError("JCode CCR profile declaration conflict")
        temporary = ".meyo-ccr-" + secrets.token_hex(12) + ".tmp"
        created = False
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            created = True
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                _same_home(home, directory_fd)
                _unchanged(directory_fd, before)
                os.replace(temporary, _CONFIG, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            if created:
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
    finally:
        # Keep the lock inode: unlinking it could split concurrent waiters.
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)


def configure(ctx, env: dict, home: Path, checkpoint: dict) -> tuple[dict, list[str], dict]:
    """Prepare only the native profile; never send, restore, or certify a model.

    A present checkpoint.ccr_binding must equal the returned four-field metadata.
    No inference or migration is made for a checkpoint without that field.
    """
    base = _loopback_base(env.get("LEGION_CCR_BASE_URL"))
    token = env.get("LEGION_CCR_AUTH_TOKEN")
    if not isinstance(token, str) or not token or any(ord(char) <= 32 for char in token):
        raise ValueError("JCode CCR authentication token is missing or invalid")
    import model_registry
    wire = model_registry.ccr_route(ctx.model, ctx.appid, ctx.gateway, environment=env)
    profile = "meyo-ccr-" + hashlib.sha256((base + "|" + wire).encode()).hexdigest()[:16]
    metadata = {"wire_model": wire, "profile": profile, "transport": "ccr",
                "ccr_binding_key": model_registry.ccr_binding_key(ctx.model, ctx.appid, ctx.gateway)}
    if not isinstance(checkpoint, dict):
        raise ValueError("JCode checkpoint must be a mapping")
    if "ccr_binding" in checkpoint and checkpoint["ccr_binding"] != metadata:
        raise ValueError("JCode checkpoint CCR binding differs from the current request")
    declaration = {
        "type": "anthropic-compatible", "base_url": base + "/v1",
        "api_key_env": "LEGION_CCR_AUTH_TOKEN", "auth": "bearer",
        "default_model": wire, "requires_api_key": True,
    }
    inputs = model_registry.input_modalities(ctx.model)
    if inputs is not None:
        declaration["models"] = [{"id": wire, "input": inputs}]
    _ensure_profile(Path(home), profile, declaration)
    new_env = dict(env)
    new_env.update(
        JCODE_PROVIDER_PROFILE_NAME=profile, JCODE_NAMED_PROVIDER_PROFILE=profile,
        JCODE_PROVIDER_PROFILE_ACTIVE="1", ANTHROPIC_API_KEY=token, ANTHROPIC_AUTH_TOKEN=token,
        ANTHROPIC_BASE_URL=declaration["base_url"], JCODE_ANTHROPIC_API_BASE=declaration["base_url"],
    )
    return new_env, ["--provider", "auto", "-m", profile + ":" + wire], metadata
