#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高副作用工具的持久幂等账本；崩溃恢复时避免重复安装、付费生成或发布。"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

import legion_paths as _paths

_PATH = Path(os.environ.get("LEGION_SIDE_EFFECT_LEDGER") or _paths.under("side_effect_ledger.json"))
_LOCK = threading.RLock()
_TERMINAL = {"completed", "failed", "cancelled"}


class LedgerError(RuntimeError):
    """Fixed public IO error codes; no filesystem or request contents."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _io_error(exc: OSError, default: str = "ledger_unreadable") -> LedgerError:
    return LedgerError("ledger_unsafe_path" if exc.errno in {
        errno.ELOOP, errno.ENOTDIR, errno.EISDIR}
                       else default)


@contextmanager
def _scoped_parent(path):
    """Pin an existing parent by fd; never resolve/follow links or create directories."""
    try:
        target = Path(path)
        if (not target.is_absolute() or ".." in target.parts or not target.name
                or "\0" in str(target)):
            raise LedgerError("ledger_unsafe_path")
    except (TypeError, ValueError):
        raise LedgerError("ledger_unsafe_path") from None
    parent = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(target.anchor, flags)
        for part in target.parts[1:-1]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise LedgerError("ledger_unsafe_path")
        if info.st_mode & 0o700 != 0o700:
            raise LedgerError("ledger_unreadable")
        yield target, parent
    except OSError as exc:
        raise _io_error(exc) from None
    finally:
        if parent is not None:
            os.close(parent)


def _check_file(info, *, writable=False):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise LedgerError("ledger_unsafe_path")
    needed = 0o600 if writable else 0o400
    if info.st_mode & needed != needed:
        raise LedgerError("ledger_unreadable")


@contextmanager
def _ledger_lock(path=None):
    with _LOCK:
        if path is None:
            # Keep legacy default routing / monkeypatch(_PATH) callers unchanged.
            target = Path(_PATH)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.with_suffix(".lock").open("a+") as handle:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield target, None
                finally:
                    if fcntl is not None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        if fcntl is None:
            raise LedgerError("ledger_lock_unavailable")
        with _scoped_parent(path) as (target, parent):
            lock = None
            try:
                name = target.with_suffix(".lock").name
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                try:
                    # Separate creation from existing-file lookup: concurrent
                    # O_CREAT|O_NOFOLLOW produced ENOENT on the local filesystem.
                    lock = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600,
                                   dir_fd=parent)
                except FileExistsError:
                    lock = os.open(name, flags, dir_fd=parent)
                _check_file(os.fstat(lock), writable=True)
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield target, parent
            except OSError as exc:
                raise _io_error(exc) from None
            finally:
                if lock is not None:
                    # Closing the fd also releases flock, including on exceptions.
                    os.close(lock)


def _validate(data: dict) -> dict:
    if (not isinstance(data, dict) or type(data.get("version")) is not int
            or data["version"] != 1 or not isinstance(data.get("effects"), dict)):
        raise LedgerError("ledger_corrupt")
    for key, row in data["effects"].items():
        if (not isinstance(row, dict) or row.get("effect_id") != key
                or row.get("status") not in _TERMINAL | {"running", "unknown", "interrupted"}
                or type(row.get("attempts")) is not int or row["attempts"] < 1
                or type(row.get("safe_retry")) is not bool
                or type(row.get("lease_expires_at")) not in (int, float)
                or not math.isfinite(row["lease_expires_at"])
                or ("result" in row and not isinstance(row["result"], dict))
                or (row["status"] == "completed" and "result" not in row)):
            raise LedgerError("ledger_corrupt")
    return data


def _load(path=None, *, dir_fd=None) -> dict:
    target = Path(_PATH if path is None else path)
    if dir_fd is None:
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"version": 1, "effects": {}}
        except Exception:
            return {"version": 1, "effects": {}}
    try:
        fd = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=dir_fd)
    except FileNotFoundError:
        return {"version": 1, "effects": {}}
    except OSError as exc:
        raise _io_error(exc) from None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            _check_file(os.fstat(handle.fileno()))
            return _validate(json.load(handle))
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise LedgerError("ledger_corrupt") from None
    except OSError as exc:
        raise _io_error(exc) from None


def _save(value: dict, path=None, *, dir_fd=None) -> None:
    target = Path(_PATH if path is None else path)
    if dir_fd is None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    else:
        tmp = target.name + "." + secrets.token_hex(8)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=dir_fd)
        except OSError:
            raise LedgerError("ledger_write_failed") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        if dir_fd is None:
            os.replace(tmp, target)
        else:
            try:
                _check_file(os.stat(target.name, dir_fd=dir_fd, follow_symlinks=False))
            except FileNotFoundError:
                pass
            os.replace(tmp, target.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
    except (OSError, TypeError, ValueError, RecursionError):
        if dir_fd is not None:
            raise LedgerError("ledger_write_failed") from None
        raise
    finally:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def fingerprint(tool_id: str, params: dict, idempotency_key: str) -> str:
    safe = {str(key): value for key, value in (params or {}).items()
            if not str(key).startswith("_") and key not in {"confirm"}}
    canonical = json.dumps({"tool": str(tool_id), "params": safe,
                            "idempotency_key": str(idempotency_key)},
                           ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def begin(tool_id: str, params: dict, idempotency_key: str, *, lease_s: int = 900,
          safe_retry: bool = False, path: Path | str | None = None) -> dict:
    """领取副作用。显式 path 的父目录须已验证且存在；IO 失败绝不能当缓存 miss。"""
    if not idempotency_key:
        return {"decision": "untracked"}
    effect_id = fingerprint(tool_id, params, idempotency_key)
    with _ledger_lock(path) as (target, parent):
        data = _load(target, dir_fd=parent)
        now = time.time()
        row = (data.get("effects") or {}).get(effect_id)
        if row and row.get("status") == "completed":
            return {"decision": "reuse", "effect_id": effect_id,
                    "result": row.get("result") or {}, "record": row}
        if row and row.get("status") == "running" and float(row.get("lease_expires_at") or 0) > now:
            return {"decision": "in_progress", "effect_id": effect_id, "record": row}
        if row and row.get("status") == "running" and not bool(row.get("safe_retry")):
            # The lease expired, not the remote side effect: do not charge again.
            row.update({"status": "unknown", "lease_expires_at": 0, "recovered_at": now})
            _save(data, target, dir_fd=parent)
        if row and row.get("status") == "unknown" and (
                row.get("uncertain") or not bool(row.get("safe_retry"))):
            return {"decision": "requires_reconciliation", "effect_id": effect_id, "record": row}
        attempts = int((row or {}).get("attempts") or 0) + 1
        row = {"effect_id": effect_id, "tool_id": str(tool_id),
               "idempotency_key": str(idempotency_key), "status": "running",
               "attempts": attempts, "started_at": now, "safe_retry": bool(safe_retry),
               "lease_owner": os.getpid(), "lease_expires_at": now + max(1, int(lease_s)),
               "previous": row or None}
        data.setdefault("effects", {})[effect_id] = row
        _save(data, target, dir_fd=parent)
        return {"decision": "execute", "effect_id": effect_id,
                "attempt": attempts, "record": row}


def finish(effect_id: str, result: dict, *, path: Path | str | None = None,
           attempt: int | None = None, uncertain: bool = False) -> dict:
    """Return the claimed attempt to fence late replies; uncertain needs reconciliation."""
    with _ledger_lock(path) as (target, parent):
        data = _load(target, dir_fd=parent)
        row = (data.get("effects") or {}).get(str(effect_id))
        if not row:
            return {"ok": False, "error": "effect not found"}
        if attempt is not None:
            if type(attempt) is not int or attempt != row.get("attempts"):
                return {"ok": False, "error": "stale_attempt", "record": dict(row)}
            if row.get("status") != "running":
                return {"ok": False, "error": "effect_not_running", "record": dict(row)}
        ok = bool((result or {}).get("ok"))
        row.update({"status": "unknown" if uncertain else ("completed" if ok else "failed"),
                    "finished_at": time.time(),
                    "lease_expires_at": 0, "result": dict(result or {})})
        if uncertain:
            row["uncertain"] = True
        _save(data, target, dir_fd=parent)
        return {"ok": True, "record": dict(row)}


def recover_expired(now: float | None = None, *,
                    path: Path | str | None = None) -> list[dict]:
    current, recovered = float(now if now is not None else time.time()), []
    with _ledger_lock(path) as (target, parent):
        data = _load(target, dir_fd=parent)
        for row in (data.get("effects") or {}).values():
            if row.get("status") == "running" and float(row.get("lease_expires_at") or 0) <= current:
                row.update({"status": "interrupted" if row.get("safe_retry") else "unknown",
                            "lease_expires_at": 0, "recovered_at": current})
                recovered.append(dict(row))
        if recovered:
            _save(data, target, dir_fd=parent)
    return recovered


def reconcile(effect_id: str, status: str, result: dict | None = None, *,
              path: Path | str | None = None) -> dict:
    """用外部系统证据解决崩溃窗口；unknown 未对账前绝不自动重放。"""
    normalized = str(status or "").lower()
    if normalized not in {"completed", "failed", "cancelled"}:
        return {"ok": False, "error": "invalid reconciliation status"}
    with _ledger_lock(path) as (target, parent):
        data = _load(target, dir_fd=parent)
        row = (data.get("effects") or {}).get(str(effect_id))
        if not row:
            return {"ok": False, "error": "effect not found"}
        row.update({"status": normalized, "reconciled_at": time.time(),
                    "lease_expires_at": 0, "result": dict(result or row.get("result") or {})})
        _save(data, target, dir_fd=parent)
        return {"ok": True, "record": dict(row)}
