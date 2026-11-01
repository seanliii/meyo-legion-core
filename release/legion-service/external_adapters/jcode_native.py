"""JCode 原生 CLI 的运行位置、请求授权和会话事实；不实现它的解题循环。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import ctypes
import errno
import hashlib
import stat
import sys
import uuid


_PUBLIC_MODEL = "all-MiniLM-L6-v2"
_PUBLIC_MODEL_RECEIPT = ".legion-public-model-cache.json"
# Public assets matched against a native JCode run that loaded this exact pair.
# These are deployment dependencies, not a task-selected cache or user memory.
_PUBLIC_MODEL_ASSETS = {
    "model.onnx": {
        "bytes": 90405214,
        "sha256": "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452",
    },
    "tokenizer.json": {
        "bytes": 466247,
        "sha256": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
    },
}


def _publish_public_model(parent_fd: int, staging_name: str) -> None:
    """Atomically publish the pair without replacing even an existing empty directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    name, flags = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    rename = getattr(libc, name, None) if sys.platform in ("darwin", "linux") else None
    if rename is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace directory publication unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(staging_name), parent_fd, os.fsencode(_PUBLIC_MODEL), flags):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _seed_public_model(home: Path, configured: str) -> dict:
    """Copy only the pinned public pair into this private home; never download."""
    facts = {"status": "not_configured", "model": _PUBLIC_MODEL, "assets": []}
    if not configured:
        return facts
    source = Path(configured).expanduser()
    models = home / "models"
    target = models / _PUBLIC_MODEL
    facts.update(source=str(source), destination=str(target))
    if not source.is_absolute():
        return {**facts, "status": "not_seeded", "reason": "cache_path_must_be_absolute"}
    # Existing native/custom/partial resources belong to JCode; do not repair,
    # overwrite or inspect them. Only a new private model directory is seeded.
    if models.is_symlink() or os.path.lexists(target):
        return {**facts, "status": "preserved_existing"}
    source_fd = parent_fd = stage_fd = None
    staging_name = ".legion-model-" + uuid.uuid4().hex
    published = False
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        source_fd = os.open(source, directory_flags)
        models.mkdir(mode=0o700, exist_ok=True)
        parent_fd = os.open(models, directory_flags)
        os.mkdir(staging_name, mode=0o700, dir_fd=parent_fd)
        stage_fd = os.open(staging_name, directory_flags, dir_fd=parent_fd)
        assets = []
        for name, pin in _PUBLIC_MODEL_ASSETS.items():
            # Open fixed asset names relative to the pinned source directory.
            # O_NOFOLLOW prevents an asset-name symlink from reading other state.
            with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd), "rb") as src:
                info = os.fstat(src.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != pin["bytes"]:
                    raise ValueError("public_asset_size_or_type_mismatch:" + name)
                with os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                       0o600, dir_fd=stage_fd), "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
            # Verify the actual private bytes, not just source metadata or a marker.
            with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=stage_fd), "rb") as dst:
                digest = hashlib.file_digest(dst, "sha256").hexdigest()
                size = os.fstat(dst.fileno()).st_size
            if digest != pin["sha256"] or size != pin["bytes"]:
                raise ValueError("public_asset_hash_mismatch:" + name)
            assets.append({"name": name, "bytes": size, "sha256": digest})
        receipt = {**facts, "status": "seeded", "method": "copy", "assets": assets}
        with os.fdopen(os.open(_PUBLIC_MODEL_RECEIPT, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                               0o600, dir_fd=stage_fd), "w") as file:
            json.dump(receipt, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        _publish_public_model(parent_fd, staging_name)
        published = True
        return receipt
    except FileExistsError:
        return {**facts, "status": "preserved_existing"}
    except (OSError, ValueError) as exc:
        # Optional deployment cache preparation must not disable native fallback.
        return {**facts, "status": "not_seeded", "reason": f"{type(exc).__name__}:{exc}"}
    finally:
        if stage_fd is not None and not published:
            for name in (*_PUBLIC_MODEL_ASSETS, _PUBLIC_MODEL_RECEIPT):
                try:
                    os.unlink(name, dir_fd=stage_fd)
                except OSError:
                    pass
        if stage_fd is not None:
            os.close(stage_fd)
        if parent_fd is not None:
            if not published:
                try:
                    os.rmdir(staging_name, dir_fd=parent_fd)
                except OSError:
                    pass
            os.close(parent_fd)
        if source_fd is not None:
            os.close(source_fd)


def executable() -> str:
    """先用受管理依赖；本地开发仍可使用已有原生安装。"""
    import external_agent_registry as registry
    item = next((r for r in registry.load_catalog() if r.get("key") == "jcode-runtime"), None)
    if item:
        path = Path(registry._context(item)["runtime_dir"]) / "bin/jcode"
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    local = shutil.which("jcode") or str(Path.home() / ".local/bin/jcode")
    return local if Path(local).is_file() and os.access(local, os.X_OK) else ""


def prepare(ctx, checkpoint: dict) -> tuple[dict, Path, str]:
    """只改变本请求的 provider 绑定；不覆盖原生配置、MCP 或能力开关。"""
    if not isinstance(checkpoint, dict) or checkpoint.get("kind") not in (None, "jcode_session"):
        raise ValueError("JCode checkpoint belongs to a different native runtime")
    resume_id = str(checkpoint.get("session_id") or "")
    if resume_id and (Path(resume_id).name != resume_id or resume_id in (".", "..")):
        raise ValueError("JCode native session id is not a file name")
    env = dict(os.environ)
    # 当前用户的执行 Context 是目录权威；旧 checkpoint 也不能借恢复跳去其他用户目录。
    # 无用户目录的本地调用仍沿用原生 JCODE_HOME/当前用户 .jcode。
    location = ctx.memory_dir or env.get("JCODE_HOME") or str(Path.home() / ".jcode")
    home = Path(location).expanduser()
    if not home.is_absolute():
        raise ValueError("JCode state home must be absolute")
    home = home.resolve()
    if checkpoint.get("jcode_home") and Path(checkpoint["jcode_home"]).expanduser().resolve() != home:
        raise ValueError("JCode checkpoint home differs from the current user context")
    if resume_id:
        saved = home / "sessions" / (resume_id + ".json")
        if saved.is_symlink() or not saved.resolve().is_relative_to(home) or not saved.is_file():
            raise ValueError("JCode native session file is missing or unsafe; original checkpoint preserved")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    # named-profile lock 会遮住显式 API base；只清连接选择，不改工具/用户配置。
    for name in tuple(env):
        if name.startswith("JCODE_OPENROUTER_") or name in {
            "JCODE_PROVIDER_PROFILE_ACTIVE", "JCODE_PROVIDER_PROFILE_NAME",
            "JCODE_NAMED_PROVIDER_PROFILE",
        }:
            env.pop(name)
    env.update(
        JCODE_HOME=str(home),
        JCODE_NO_TELEMETRY="1",
        JCODE_OPENAI_COMPAT_API_BASE=(ctx.gateway or env.get("MEYO_UNIFIED_GATEWAY")
                                     or "https://aigc.sankuai.com/v1/openai/native"),
        JCODE_OPENAI_COMPAT_API_KEY_NAME="OPENAI_COMPAT_API_KEY",
        JCODE_OPENAI_COMPAT_DEFAULT_MODEL=ctx.model,
        OPENAI_COMPAT_API_KEY=(ctx.appid or env.get("MEYO_UNIFIED_APPID")
                              or env.get("AIGC_APPID") or env.get("OPENAI_COMPAT_API_KEY", "")),
    )
    env["LEGION_JCODE_PUBLIC_MODEL_CACHE_RESULT"] = json.dumps(
        _seed_public_model(home, env.get("LEGION_JCODE_PUBLIC_MODEL_CACHE", "")),
        ensure_ascii=False)
    return env, home, resume_id


def result_from_native(process, checkpoint: dict, home: Path, requested_model: str) -> dict:
    """Consume native start/done/error and the matching session; never infer an ID."""
    result = {"final": "", "status": f"rc{process.returncode}" if process.returncode else "harness_runtime",
              "model_used": None, "requested_model": requested_model, "usage": None,
              "native_checkpoint": checkpoint or None, "resumed_native": False}
    def text(value):
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    stdout, stderr = text(process.stdout), text(process.stderr)
    try:
        try:
            whole = json.loads(stdout)
        except ValueError:
            whole = None
        legacy = isinstance(whole, dict) and not whole.get("type") and "text" in whole
        records = [whole] if isinstance(whole, dict) else []
        if not records:
            for line in stdout.splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    records.append(row)
        identities = [row for row in records
                      if legacy or row.get("type") in {"start", "session", "done", "error"}]
        ids = {row.get("session_id") for row in identities
               if isinstance(row.get("session_id"), str) and row["session_id"]}
        if len(ids) != 1:
            raise ValueError("JCode native output has no single received session identity")
        sid = ids.pop()
        if not isinstance(sid, str) or not sid or Path(sid).name != sid or sid in (".", ".."):
            raise ValueError("JCode native JSON report has no valid session id")
        previous_id = checkpoint.get("session_id")
        if previous_id and sid != previous_id:
            raise ValueError("JCode native session changed during resume")
        session_path = home / "sessions" / (sid + ".json")
        if (session_path.is_symlink() or not session_path.resolve().is_relative_to(home)
                or not session_path.is_file()):
            raise ValueError("JCode reported session has no native file")
        stored = json.loads(session_path.read_text())
        if not isinstance(stored, dict) or stored.get("id") != sid:
            raise ValueError("JCode native session file identity mismatch")
        result.update(
            native_checkpoint={"kind": "jcode_session", "session_id": sid,
                               "jcode_home": str(home), "session_path": str(session_path)},
            resumed_native=bool(previous_id),
        )
        terminal = [row for row in records if legacy or row.get("type") in {"done", "error"}]
        report = terminal[-1] if terminal else identities[-1]
        reported_model = report.get("model")
        result.update(model_used=reported_model if isinstance(reported_model, str) and reported_model else None,
                      native_provider=report.get("provider"),
                      native_facts={"protocol": "json" if legacy else "ndjson",
                                    "terminal_event": report.get("type") if terminal else None,
                                    "session_received": True})
        if process.returncode or report.get("type") == "error":
            result["error"] = text(report.get("message") or stderr or "JCode native command failed")[-1500:]
            return result
        if not terminal or not isinstance(report.get("text"), str):
            raise ValueError("JCode native output ended before a complete done report")
        result.update(final=report["text"], status="ok", usage=report.get("usage"))
    except (OSError, ValueError, TypeError) as exc:
        result["error"] = (stderr or str(exc))[-1500:]
    return result
