#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""长上下文的内容寻址存储。

Team/mailbox 只保存小摘要和引用；完整报告、代码和恢复现场写独立文件，按需读取。
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from pathlib import Path

import legion_paths as _paths


ROOT = Path(os.environ.get("LEGION_ARTIFACT_STORE_DIR") or _paths.under("task_artifacts"))
_MAX_READ = int(os.environ.get("LEGION_ARTIFACT_READ_MAX", "2000000"))


def _safe(value: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(value or "artifact"))[:100]
    # 路径穿越防护(军团 codex dogfood 揪出:_safe("..") 返回 ".." · . 和 - 在白名单里→
    # folder=ROOT/".."/… 逃出产物根目录写文件)。纯 . / .. / 空 一律兜底成安全值。
    if cleaned in ("", ".", "..") or set(cleaned) <= {"."}:
        return "artifact"
    return cleaned


def put_text(team_id: str, task_id: str, text: str, name: str = "result.txt") -> dict:
    raw = str(text or "").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    folder = ROOT / _safe(team_id) / _safe(task_id)
    # 写入前再校验 folder 确实在 ROOT 内(双保险·任何清洗漏网都拦下)
    root_r = ROOT.resolve()
    if not str(folder.resolve()).startswith(str(root_r) + os.sep) and folder.resolve() != root_r:
        raise ValueError(f"artifact 路径越界(拒绝写出根目录): {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(name).suffix or ".txt"
    path = folder / f"{digest}{ext}"
    if not path.exists():
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        tmp.write_bytes(raw)
        os.replace(tmp, path)
    return {
        "artifact_id": digest,
        "path": str(path),
        "sha256": digest,
        "mime": mimetypes.guess_type(name)[0] or "text/plain",
        "bytes": len(raw),
        "name": name,
    }


def put_json(team_id: str, task_id: str, value, name: str = "state.json") -> dict:
    return put_text(team_id, task_id, json.dumps(value, ensure_ascii=False, indent=2), name=name)


def put_bytes(team_id: str, task_id: str, raw: bytes, name: str = "artifact.bin") -> dict:
    """Persist exact bytes without following untrusted directory/file links.

    ROOT is the configured, trusted data root and may itself be canonicalized.
    Every descendant lookup/write is relative to a pinned, no-follow directory
    descriptor. Directory locks coordinate writers, not privileged outsiders:
    the owner must protect ROOT against arbitrary directory relocation/mounts.
    """
    display_name = str(name or "artifact.bin")
    suffix = Path(display_name).suffix.lower()
    if not (2 <= len(suffix) <= 17 and suffix.startswith(".")
            and suffix[1:].isascii() and suffix[1:].isalnum()):
        suffix = ".bin"
    filename = hashlib.sha256(raw).hexdigest() + suffix
    return _put_bytes_at(ROOT, (_safe(team_id), _safe(task_id), filename), raw, display_name)


def _put_bytes_at(root: Path, parts: tuple, raw: bytes, display_name: str,
                  *, _root_fd: int | None = None, blocking: bool = True) -> dict:
    """Exact-byte writer; optional copies may decline a contended directory."""
    if (not parts or any(not isinstance(p, str) or p in ("", ".", "..")
                         or "/" in p or "\\" in p or "\x00" in p for p in parts)):
        raise ValueError("invalid artifact relative path")
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    raw = bytes(raw)  # Keep an immutable builtin buffer, including for subclasses.
    if not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")):
        raise NotImplementedError("secure binary artifacts require no-follow directory descriptors")
    from contextlib import ExitStack
    import fcntl
    import secrets
    import stat

    digest = hashlib.sha256(raw).hexdigest()
    filename = parts[-1]
    if _root_fd is None:
        root = root.resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    close_on_exec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | close_on_exec
    read_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | close_on_exec

    def open_directory(parent_fd, component):
        try:
            os.mkdir(component, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        else:
            os.fsync(parent_fd)
        return os.open(component, directory_flags, dir_fd=parent_fd)

    def identity(info):
        return info.st_dev, info.st_ino

    def version(info):
        return identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def verify_file(directory_fd, leaf):
        try:
            fd = os.open(leaf, read_flags, dir_fd=directory_fd)
        except FileNotFoundError:
            return False
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("artifact is not a regular file")
            if before.st_size != len(raw):
                raise ValueError("existing artifact size does not match its content address")
            observed = hashlib.sha256()
            offset = 0
            while True:
                chunk = os.read(fd, min(1024 * 1024, len(raw) - offset + 1))
                if not chunk:
                    break
                if chunk != raw[offset:offset + len(chunk)]:
                    raise ValueError("existing artifact bytes do not match its content address")
                observed.update(chunk)
                offset += len(chunk)
            if offset != len(raw) or observed.hexdigest() != digest:
                raise ValueError("existing artifact SHA does not match its content address")
            after = os.fstat(fd)
            entry = os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
            if (not stat.S_ISREG(entry.st_mode) or version(before) != version(after)
                    or version(after) != version(entry)):
                raise ValueError("artifact changed during byte verification")
            return True
        finally:
            os.close(fd)

    with ExitStack() as opened:
        root_fd = os.open(root, directory_flags) if _root_fd is None else os.dup(_root_fd)
        opened.callback(os.close, root_fd)
        task_fd = root_fd
        chain = []
        for component in parts[:-1]:
            child_fd = open_directory(task_fd, component)
            opened.callback(os.close, child_fd)
            chain.append((task_fd, component, child_fd))
            task_fd = child_fd
        fcntl.flock(task_fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))

        def check_directories():
            for parent_fd, component, fd in chain:
                entry = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISDIR(entry.st_mode) or identity(entry) != identity(os.fstat(fd)):
                    raise ValueError("artifact directory changed during write")

        check_directories()
        if not verify_file(task_fd, filename):
            temporary = f".{digest}.{secrets.token_hex(16)}.tmp"
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | close_on_exec,
                0o600, dir_fd=task_fd)
            opened.callback(os.close, fd)
            try:
                os.fchmod(fd, 0o600)
                remaining = memoryview(raw)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("artifact write made no progress")
                    remaining = remaining[written:]
                os.fsync(fd)
                check_directories()
                entry = os.stat(temporary, dir_fd=task_fd, follow_symlinks=False)
                if not stat.S_ISREG(entry.st_mode) or identity(entry) != identity(os.fstat(fd)):
                    raise ValueError("artifact temporary file changed during write")
                # A non-cooperating writer may have published while we wrote.
                # Never silently accept/repair its mismatching content.
                if not verify_file(task_fd, filename):
                    os.rename(temporary, filename, src_dir_fd=task_fd, dst_dir_fd=task_fd)
                    temporary = None
                os.fsync(task_fd)
                if not verify_file(task_fd, filename):
                    raise ValueError("artifact disappeared after publication")
            finally:
                if temporary is not None:
                    try:
                        entry = os.stat(temporary, dir_fd=task_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        # Do not unlink somebody else's replacement under our
                        # random temporary name; never follow it during cleanup.
                        if identity(entry) == identity(os.fstat(fd)):
                            os.unlink(temporary, dir_fd=task_fd)
                            os.fsync(task_fd)
        check_directories()
    return {
        "artifact_id": digest,
        "path": str(root.joinpath(*parts)),
        "sha256": digest,
        "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream",
        "bytes": len(raw),
        "name": display_name,
    }


def put_export_bundle(files: dict[str, bytes], manifest: dict) -> dict:
    """Publish one immutable, relative-layout snapshot under existing outputs.

    Admission/content checks belong to artifact_delivery. This writer pins all
    export descendants, stages privately, verifies existing snapshots instead
    of repairing them, and publishes by one directory rename. The manifest
    stays in the existing private artifact store, not the downloadable tree.
    """
    from contextlib import ExitStack
    import fcntl
    import secrets
    import shutil
    import stat

    if not files:
        raise ValueError("empty export")
    for relative, raw in files.items():
        path = Path(relative)
        if (not isinstance(raw, bytes) or path.is_absolute() or relative != path.as_posix()
                or any(p in ("", ".", "..") or "\\" in p or "\x00" in p for p in path.parts)):
            raise ValueError("invalid export file")
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
    snapshot = hashlib.sha256(encoded).hexdigest()
    scope = hashlib.sha256(json.dumps(
        [manifest[k] for k in ("user_id", "conversation_id", "team_id", "task_id", "source_root")],
        ensure_ascii=False).encode()).hexdigest()
    base = Path(_paths.legion_dir()).resolve()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    chain = []

    def identity(info):
        return info.st_dev, info.st_ino

    def check_chain():
        for parent, name, fd in chain:
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode) or identity(entry) != identity(os.fstat(fd)):
                raise ValueError("export directory changed")

    def verify_tree(fd, prefix=""):
        seen = set()
        for name in os.listdir(fd):
            relative = prefix + name
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(entry.st_mode):
                child = os.open(name, flags, dir_fd=fd)
                try:
                    seen.update(verify_tree(child, relative + "/"))
                finally:
                    os.close(child)
            elif stat.S_ISREG(entry.st_mode) and relative in files:
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                with os.fdopen(child, "rb") as stream:
                    from artifact_content_safety import _inspect, _stamp
                    stamp, digest = _inspect(stream, relative)
                    if (digest != hashlib.sha256(files[relative]).hexdigest()
                            or stamp != _stamp(os.stat(name, dir_fd=fd, follow_symlinks=False))):
                        raise ValueError("export bytes changed")
                seen.add(relative)
            else:
                raise ValueError("unexpected export entry")
        return seen

    folder = base / "outputs" / "exports" / scope
    with ExitStack() as opened:
        parent = os.open(base, flags)
        opened.callback(os.close, parent)
        for name in ("outputs", "exports", scope):
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(name, flags, dir_fd=parent)
            opened.callback(os.close, child)
            chain.append((parent, name, child))
            parent = child
        fcntl.flock(parent, fcntl.LOCK_EX)
        check_chain()
        try:
            published_fd = os.open(snapshot, flags, dir_fd=parent)
        except FileNotFoundError:
            published_fd = None
        if published_fd is not None:
            try:
                if verify_tree(published_fd) != set(files):
                    raise ValueError("incomplete export snapshot")
            finally:
                os.close(published_fd)
            reference = put_bytes(scope, snapshot, encoded, "export-manifest.json")
        else:
            temporary = ".export-" + secrets.token_hex(16)
            os.mkdir(temporary, mode=0o700, dir_fd=parent)
            staging_fd = os.open(temporary, flags, dir_fd=parent)
            try:
                for relative, raw in sorted(files.items()):
                    _put_bytes_at(folder / temporary, Path(relative).parts, raw,
                                  Path(relative).name, _root_fd=staging_fd)
                check_chain()
                if verify_tree(staging_fd) != set(files):
                    raise ValueError("incomplete export staging")
                if identity(os.stat(temporary, dir_fd=parent, follow_symlinks=False)) != identity(os.fstat(staging_fd)):
                    raise ValueError("export staging changed")
                # Manifest failure must not publish an unregistered snapshot.
                reference = put_bytes(scope, snapshot, encoded, "export-manifest.json")
                os.rename(temporary, snapshot, src_dir_fd=parent, dst_dir_fd=parent)
                temporary = None
                os.fsync(parent)
            finally:
                if temporary is not None:
                    entry = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                    if identity(entry) == identity(os.fstat(staging_fd)):
                        shutil.rmtree(temporary, dir_fd=parent)
                os.close(staging_fd)
        check_chain()
    return {"root": str(folder / snapshot), "snapshot_id": snapshot, "manifest": reference}


def read_text(ref: dict, max_bytes: int | None = None) -> str:
    if not isinstance(ref, dict):
        return ""
    try:
        path = Path(str(ref.get("path") or "")).resolve()
        root = ROOT.resolve()
        if not (path == root or str(path).startswith(str(root) + os.sep)) or not path.is_file():
            return ""
        data = path.read_bytes()[:max(1, min(int(max_bytes or _MAX_READ), _MAX_READ))]
        if ref.get("sha256") and hashlib.sha256(path.read_bytes()).hexdigest() != ref["sha256"]:
            return ""
        return data.decode("utf-8", "replace")
    except (OSError, ValueError, TypeError):
        return ""
