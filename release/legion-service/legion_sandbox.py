#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为不同 harness 准备真实可达的源码与持久工作现场。"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

import legion_paths


_SOURCE_PATHS = (
    "seed-users-system/qa_system/agent",
    "harness_baselines/codex/codex-rs",
    "harness_baselines/deepseek-harness",
    "harness_baselines/hermes-agent",
    "harness_baselines/penguin-harness",
    "harness_baselines/ecosystem/omnigent",
    "harness_baselines/meyo-orchestrator/harness_registry.py",
    "release/legion-service/legion_core.py",
    # 发布包与开发仓库共享入口发现逻辑；仅暴露包内实际存在的源码。
    "legion/orchestrator/harness_registry.py",
    "legion/legion-service/legion_core.py",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def workdir(key: str, qhash: str, project_context: dict | None = None,
            user_id: str = "") -> str:
    """Resolve the caller's project without changing native sandbox permissions."""
    context = project_context if isinstance(project_context, dict) else {}
    requested = str(context.get("repo_root") or context.get("workspace_root") or "").strip()
    output_root = Path(legion_paths.under("outputs")).resolve()
    if requested:
        owner = str(context.get("user_id") or "")
        if owner and user_id and owner != str(user_id):
            raise PermissionError("project workspace belongs to another user")
        path = Path(requested).expanduser()
        if not path.is_absolute():
            raise ValueError("project workspace must be absolute")
        resolved = path.resolve(strict=True)
        # A link inside our managed output tree cannot escape that tree.
        managed = any(parent.resolve() == output_root for parent in path.parents)
        if managed and not resolved.is_relative_to(output_root):
            raise PermissionError("project workspace symlink escapes managed outputs")
        if not resolved.is_dir() or not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
            raise PermissionError("project workspace is not an accessible directory")
        return str(resolved)
    path = output_root / f"{key}_{qhash}"
    if not path.resolve().is_relative_to(output_root):
        raise PermissionError("task workspace escapes managed outputs")
    path.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())


def workdir_allocation_facts(team_id: str, user_id: str, conversation_id: str) -> list[dict]:
    """Read scoped allocation facts, including receipts for replaced directories.

    Neither a caller's packet nor an outputs prefix is allocation evidence.
    Full identities and directory inodes are retained: truncated storage names,
    a rebound team or a replaced directory must not transfer this authority.
    An allocation with current=False is a refusal fact, never a directory grant.
    """
    import personal_identity
    import team_state

    if not all(isinstance(v, str) and v for v in (team_id, user_id, conversation_id)):
        return []
    session = personal_identity.owned_session(conversation_id, user_id)
    if (not session or session.get("sid") != conversation_id or session.get("user_id") != user_id
            or not any(isinstance(turn, dict) and turn.get("team_id") == team_id
                       for turn in session.get("turns") or [])):
        return []
    team = team_state.load(team_id) or {}
    if (team.get("team_id") != team_id or team.get("user_id") != user_id
            or team.get("conversation_id") != conversation_id):
        return []
    outputs = Path(legion_paths.under("outputs")).resolve()
    found = []
    for task_id, task in (team.get("tasks") or {}).items():
        for row in task.get("workdir_allocations") or []:
            if (not isinstance(row, dict) or row.get("user_id") != user_id
                    or row.get("conversation_id") != conversation_id or row.get("team_id") != team_id
                    or row.get("task_id") != task_id or not row.get("harness_key") or not row.get("qhash")):
                continue
            fact = {"allocation": dict(row), "current": False}
            found.append(fact)
            try:
                path = personal_identity._absolute_unlinked_path(row.get("root"))
                expected = outputs / f"{row['harness_key']}_{row['qhash']}"
                if path != expected or path.parent != outputs or path.name == "exports":
                    continue
                info = path.stat()
                fact["current"] = (info.st_dev, info.st_ino) == (row.get("device"), row.get("inode"))
            except (OSError, ValueError, TypeError, RuntimeError):
                continue
    return found


def owned_workdir_allocations(team_id: str, user_id: str, conversation_id: str) -> list[dict]:
    """Only currently valid receipts may resolve a workspace or authorize reuse."""
    return [fact["allocation"] for fact in workdir_allocation_facts(
        team_id, user_id, conversation_id) if fact["current"]]


def allocate_task_workdir(key: str, qhash: str, *, team_id: str, task_id: str,
                          attempt_id: int, user_id: str, native_conversation_id: str) -> str:
    """Bind a NEW default directory at the trusted execution admission boundary.

    This is not a root/grant supplied by a task. The existing allocator chooses
    the name; the owned live session and current attempt authorize its creation.
    Old unbound directories are never claimed, even if their name happens to fit.
    Legacy internal calls without a live owned session acquire no new authority.
    """
    import personal_identity
    import project_context
    import team_state

    packet = project_context.load(team_id)
    conversation = packet.get("conversation_id")
    task = next((row for row in packet.get("tasks") or []
                 if isinstance(row, dict) and row.get("task_id") == task_id), None)
    if (not user_id or not conversation or packet.get("team_id") != team_id
            or packet.get("user_id") != user_id or task is None
            or task.get("assigned_harness") != key
            or native_conversation_id not in (conversation, f"{conversation}:task:{task_id}")):
        raise PermissionError("default workspace project binding mismatch")
    session = personal_identity.owned_session(conversation, user_id)
    if not session:
        return ""  # Old internal/maintenance calls are not new directory grants.
    if (session.get("sid") != conversation or session.get("user_id") != user_id
            or not any(isinstance(turn, dict) and turn.get("team_id") == team_id
                       and turn.get("status") == "running" for turn in session.get("turns") or [])):
        raise PermissionError("default workspace requires its owned live session")
    outputs = Path(legion_paths.under("outputs")).resolve()
    path = outputs / f"{key}_{qhash}"
    if path.parent != outputs or "\\" in path.name or "\x00" in path.name:
        raise PermissionError("default workspace is not an exact task directory")
    with team_state._state_lock(team_id):
        state = team_state.load(team_id) or {}
        node = (state.get("tasks") or {}).get(task_id) or {}
        if (state.get("team_id") != team_id or state.get("user_id") != user_id
                or state.get("conversation_id") != conversation or node.get("assigned_harness") != key
                or type(attempt_id) is not int or attempt_id <= 0
                or node.get("active_attempt") != attempt_id or node.get("status") != "running"
                or team_state._cancel_request_for(state, task_id, node)):
            raise PermissionError("default workspace requires its current uncancelled attempt")
        existing = owned_workdir_allocations(team_id, user_id, conversation)
        if any(row["root"] == str(path) and row["task_id"] == task_id for row in existing):
            return str(path)
        outputs.mkdir(parents=True, exist_ok=True)
        try:
            path.mkdir()  # No exist_ok: a historical/unowned directory is not a fresh allocation.
        except FileExistsError:
            raise PermissionError("default workspace already exists without an owned allocation") from None
        info = None
        try:
            personal_identity._absolute_unlinked_path(str(path))
            info = path.stat()
            receipt = {
                "root": str(path), "device": info.st_dev, "inode": info.st_ino,
                "user_id": user_id, "conversation_id": conversation, "team_id": team_id,
                "task_id": task_id, "harness_key": key, "qhash": qhash, "attempt_id": attempt_id,
            }
            node.setdefault("workdir_allocations", []).append(receipt)
            team_state._write(state)
        except Exception:
            # No native execution has started. Roll back only our still-empty
            # directory; never delete files or adopt an unrecorded old workspace.
            try:
                current = path.lstat()
                if info is not None and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                    path.rmdir()
            except OSError:
                pass
            raise
    return str(path)


def _task_workdir(destination: str) -> Path | None:
    """只接受 outputs 的直接子目录，避免清理函数越过军团任务边界。"""
    if not destination:
        return None
    output_root = Path(legion_paths.under("outputs")).resolve()
    candidate = Path(destination).expanduser().resolve()
    return candidate if candidate.parent == output_root else None


def cleanup_peer_sources(destination: str) -> bool:
    """迁移旧版任务镜像；只删任务目录内精确命名的 peer_sources。"""
    task_root = _task_workdir(destination)
    if task_root is None:
        return False
    peer_root = task_root / "peer_sources"
    try:
        if peer_root.is_symlink() or peer_root.is_file():
            peer_root.unlink()
        elif peer_root.is_dir():
            shutil.rmtree(peer_root)
        else:
            return False
        return True
    except OSError:
        return False


@lru_cache(maxsize=8)
def _cleanup_legacy_peer_sources_once(output_root_value: str) -> int:
    """每个 outputs 根只扫一次旧镜像；新实现不再创建，故无需年龄窗口。"""
    output_root = Path(output_root_value)
    if not output_root.is_dir():
        return 0
    removed = 0
    for task_root in output_root.iterdir():
        if task_root.is_dir() and cleanup_peer_sources(str(task_root)):
            removed += 1
    return removed


def cleanup_legacy_peer_sources() -> int:
    """清除升级前遗留的所有逐任务源码副本。"""
    return _cleanup_legacy_peer_sources_once(str(Path(legion_paths.under("outputs")).resolve()))

def peer_source_entries(root: Path | None = None) -> list[dict]:
    """List only declared, readable source roots; do not scan or copy a store."""
    root = Path(root) if root is not None else repo_root()
    entries: dict[str, dict] = {}
    families = {"auto-tools": "auto", "hermes-agent": "hermes",
                "deepseek-harness": "deepseek", "omnigent": "eco:omnigent"}

    def add(source: Path, family: str, revision: str = "") -> None:
        try:
            resolved = source.resolve(strict=True)
            if not os.access(resolved, os.R_OK):
                return
            if not (resolved.is_dir() or resolved.is_file()):
                return
            marker = resolved / ".meyo-source.json" if resolved.is_dir() else None
            manifest = (str(marker) if marker is not None and marker.is_file()
                        and not marker.is_symlink() else "")
            entries.setdefault(str(resolved), {
                "family": family, "path": str(source.absolute()), "realpath": str(resolved),
                "revision": str(revision or ""), "manifest": manifest,
            })
        except (OSError, RuntimeError, ValueError):
            return

    for relative in _SOURCE_PATHS:
        parts = Path(relative).parts
        if parts[0] == "harness_baselines":
            family = ("eco:" + parts[2] if parts[1] == "ecosystem"
                      else families.get(parts[1], parts[1]))
        elif parts[0] == "seed-users-system":
            family = "auto"
        else:
            family = "meyo-orchestrator" if "orchestrator" in parts else "legion"
        add(root / relative, family)
    core_sources = root / "runtime/core-sources"
    if core_sources.is_dir():
        for source in sorted(core_sources.iterdir()):
            if (not source.is_symlink() and source.is_dir()
                    and (source / ".meyo-source.json").is_file()
                    and not (source / ".meyo-source.json").is_symlink()):
                add(source, families.get(source.name, source.name))
    try:
        import external_agent_registry as registry
        catalog = registry.load_catalog()
    except (ImportError, OSError, ValueError, TypeError):
        catalog = []
    for item in catalog:
        try:
            source = registry.source_dir(item)
            add(source, str(item["key"]), item.get("revision", ""))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            # One broken binding must not hide all subsequent installed members.
            continue
    return list(entries.values())


def prepare_peer_sources(key: str, destination: str, *, preserve_project: bool = False) -> str:
    """返回仓库唯一源码的已验证只读入口，绝不向任务目录复制源码树。"""
    # 普通任务准备不清理任何项目；复用相同枚举供检索和源码引用核验使用。
    del key, destination, preserve_project
    paths = [entry["realpath"] for entry in peer_source_entries()]
    if not paths:
        return "Peer source access：当前布局未发现已核验可读的规范源码入口。"
    return (
        "Peer source access (规范源码单一事实源；下列路径已核验可直接只读访问，"
        "不会复制进任务产物目录)：\n"
        + "\n".join(f"- {path}" for path in paths)
    )
