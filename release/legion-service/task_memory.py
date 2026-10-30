#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""请求级工作记忆：记录阶段、产物、失败和原生 checkpoint。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import legion_paths as _paths

ROOT = Path(os.environ.get("LEGION_TASK_MEMORY_DIR") or _paths.under("task_memory"))
_LOCK = threading.RLock()


def _safe(value: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(value or "task"))[:120]


def _path(team_id: str, key: str) -> Path:
    return ROOT / _safe(team_id) / f"{_safe(key)}.json"


def request_storage_id(qhash: str, origin: dict) -> str:
    """Keep the existing request identity encoding shared by writer and reader."""
    import hashlib
    return "request_memory_" + hashlib.sha256(
        json.dumps([qhash, origin], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def context_read_directory(qhash: str, key: str, user_id: str, project: dict,
                           facts: dict, refs: dict, *, expected_identity=None) -> dict | None:
    """Validate one request's entire memory directory for native read-only access.

    This does not grant a per-JSON-node ACL. The directory remains maintained by
    the trusted store; native write roots must exclude it and its ancestors.
    """
    if not refs or facts.get("native_session_scope") != "request":
        return None  # Legacy/direct adapters acquire no new access by inference.
    import hashlib
    import re
    import stat
    from contextlib import ExitStack
    import project_context

    origin = facts.get("native_context_origin")
    if (not isinstance(project, dict) or not project.get("user_id")
            or not isinstance(origin, dict) or key != "cc"):
        raise ValueError("context read requires the current owned CC request")
    request_id = str(facts.get("native_session_request_id") or facts.get("team_id") or qhash)
    logical = str(origin.get("logical_session_id") or "")
    parent = str(project.get("conversation_id") or "")
    caller = (parent if parent and (
        logical.startswith(parent + ":task:") or logical == parent + ":captain-control"
        or logical.startswith(parent + ":contract-repair:")) else logical)
    project_context.require_owner(project, user_id, caller)
    if (not logical or origin.get("user_id") != str(project.get("user_id") or user_id)
            or origin.get("harness") != key or origin.get("request_id") != request_id):
        raise ValueError("context read origin does not match this call")
    memory_id = request_storage_id(qhash, origin)
    if facts.get("native_request_memory_id") != memory_id:
        raise ValueError("context read belongs to another request")
    allowed = {"MEYO_TASK_FACTS_JSON", "MEYO_TASK_MEMORY_JSON", "MEYO_PROJECT_CONTEXT_JSON",
               "MEYO_PRIOR_ATTEMPTS", "MEYO_RUNTIME_BRIEF", "MEYO_CAPABILITY_POLICY_JSON"}
    if not isinstance(refs, dict) or not set(refs) <= allowed:
        raise ValueError("unknown context reference channel")
    if ROOT.is_symlink():
        raise ValueError("context store root must not be a symlink")
    root = ROOT.resolve(strict=True)
    path = root / _safe(memory_id) / f"{_safe(key)}.json"
    for ref in refs.values():
        digest = ref.get("sha256") if isinstance(ref, dict) else None
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or ref.get("path") != str(path)
                or ref.get("json_pointer") != "/runtime_context/" + digest
                or ref.get("encoding") != "json-utf8" or type(ref.get("bytes")) is not int):
            raise ValueError("context reference is not the current store node")

    def trusted(info, directory=False):
        kind = stat.S_ISDIR if directory else stat.S_ISREG
        if (not kind(info.st_mode) or info.st_mode & 0o022
                or info.st_uid not in (0, os.getuid())):
            raise ValueError("context store is not operator-controlled")

    with _LOCK, ExitStack() as stack:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, root_fd)
        root_info = os.fstat(root_fd)
        trusted(root_info, True)
        directory_fd = os.open(path.parent.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=root_fd)
        stack.callback(os.close, directory_fd)
        directory_info = os.fstat(directory_fd)
        trusted(directory_info, True)
        identity = (root_info.st_dev, root_info.st_ino,
                    directory_info.st_dev, directory_info.st_ino)
        if expected_identity is not None and tuple(expected_identity) != identity:
            raise ValueError("context directory identity changed before native launch")

        def read_record(name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
            with os.fdopen(fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                trusted(before)
                if not 0 < before.st_size <= 64 * 1024 * 1024:
                    raise ValueError("context record exceeds bounded read")
                raw = stream.read(before.st_size + 1)
                after = os.fstat(stream.fileno())
                if (len(raw) != before.st_size or
                        (before.st_size, before.st_mtime_ns, before.st_ino) !=
                        (after.st_size, after.st_mtime_ns, after.st_ino)):
                    raise ValueError("context record changed during read")
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("team_id") != memory_id
                    or value.get("key") != key):
                raise ValueError("context directory contains another request's data")
            return value

        names = os.listdir(directory_fd)
        if path.name not in names or len(names) > 32:
            raise ValueError("unexpected context directory contents")
        record = None
        for name in names:
            if name != path.name and not re.fullmatch(re.escape(_safe(key)) + r"\.tmp\.\d+", name):
                raise ValueError("context directory includes an unrelated entry")
            value = read_record(name)
            if name == path.name:
                record = value
        snapshots = record.get("runtime_context")
        if not isinstance(snapshots, dict):
            raise ValueError("context store has no snapshot table")
        for ref in refs.values():
            if ref["sha256"] not in snapshots:
                raise ValueError("context snapshot is absent")
            value = snapshots[ref["sha256"]]
            encoded = json.dumps(value, ensure_ascii=False).encode()
            if (len(encoded) != ref["bytes"]
                    or hashlib.sha256(encoded).hexdigest() != ref["sha256"]):
                raise ValueError("context snapshot no longer matches its reference")
    return {"directory": str(path.parent), "identity": identity, "memory_request_id": memory_id}


def load(team_id: str, key: str) -> dict:
    try:
        return json.loads(_path(team_id, key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def update(team_id: str, key: str, **facts) -> dict:
    with _LOCK:
        state = load(team_id, key)
        for name, value in facts.items():
            if value is not None:
                state[name] = value
        state.update({"team_id": team_id, "key": key, "updated_at": time.time()})
        path = _path(team_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return state


def context(team_id: str, key: str) -> dict:
    """返回可直接放入 TaskFacts 的结构化现场，不拼指导语。"""
    state = load(team_id, key)
    if not state:
        return {}
    return {name: state.get(name) for name in
            ("phase", "completed_steps", "artifacts", "error_envelope", "native_checkpoint", "workdir", "native_facts", "native_resume_error",
             "checkpoint_writes", "checkpoint_storage_error", "native_conversation_id",
             "native_session_scope", "native_context_origin", "native_request_memory_id")
            if state.get(name) not in (None, "", [], {})}
