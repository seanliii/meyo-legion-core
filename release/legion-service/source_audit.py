#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团源码审核账本：准备上下文、核验源码引用、持久化可复用结论。"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any

import legion_paths as _paths

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows fallback
    _fcntl = None

_LOCK = threading.RLock()
_REPO = pathlib.Path(__file__).resolve().parents[2]
_SKILL = _REPO / ".claude" / "skills" / "source-audit-evolve" / "SKILL.md"
_BASELINE = _REPO / "dev" / "legion-today-optimizations-for-peers-2026-09-02.md"
_STATUS = {"claimed", "verified", "contradicted", "unverified"}
_PRIOR_ART_FILE_BYTES = 2_000_000
_PRIOR_ART_TOTAL_BYTES = 32 * 1024 * 1024
_PRIOR_ART_SECONDS = 5


def _safe(value: str, fallback: str = "default") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or fallback)).strip("-.")[:100]
    return cleaned or fallback


def _root(uid: str) -> pathlib.Path:
    root = pathlib.Path(_paths.under("users", _safe(uid), "source-audits"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def evidence_path(uid: str = "default") -> str:
    """返回可传给骨架 Guard 的源码根因账本路径。"""
    return str(_root(uid) / "evidence.json")


@contextlib.contextmanager
def _locked(path: pathlib.Path):
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        with lock_path.open("a+", encoding="utf-8") as handle:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                if _fcntl is not None:
                    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


def _read_entries(path: pathlib.Path) -> list[dict]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _write_entries(path: pathlib.Path, value: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _command_lines(command: list[str], timeout: int = 20) -> list[str]:
    """安全执行只读索引命令；无匹配和浅克隆都返回空，不阻断源码审核。"""
    try:
        completed = subprocess.run(command, cwd=_REPO, capture_output=True,
                                   text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode not in (0, 1):
        return []
    return [line for line in completed.stdout.splitlines() if line.strip()]


def _git_lines(arguments: list[str], timeout: int = 20) -> list[str]:
    return _command_lines(["git", *arguments], timeout)


def _existing_search_roots(roots: list[str]) -> list[str]:
    present = []
    for root in roots:
        path = _REPO
        for part in pathlib.PurePosixPath(root).parts:
            path = path / part
            if path.is_symlink() or not path.is_dir():
                break
        else:
            present.append(root)
    return present


def _current_asset_paths(terms: list[str], limit: int) -> list[str]:
    """秒级搜索高信号源码/设计区；历史大产物只列路径，不全文扫描。"""
    roots = _existing_search_roots([
        "release/legion-service", "dev", ".claude/plans",
        "harness_baselines/comparison", "harness_baselines/fair_bench_40",
    ])
    if not roots:
        return []  # 不给 rg 空路径列表，避免意外扫描整个工作目录。
    args = ["rg", "-l", "-i", "-F"]
    for term in terms:
        args.extend(["-e", term])
    args.extend([
        "-g", "!dev/ultimate-interview-2026-09-03/deliverables/**",
        "-g", "!dev/frontend-journeys-2026-09-04/**",
        "-g", "!**/results/**", "-g", "!**/traces/**", *roots,
    ])
    rows = _command_lines(args, timeout=5)
    if not rows:  # 极简环境没有 rg 时退 Git 索引；不扫未跟踪构建树。
        git_args = ["grep", "-I", "-l", "-i", "-F"]
        for term in terms:
            git_args.extend(["-e", term])
        rows = _git_lines([*git_args, "--", *roots], timeout=8)
    return [row[2:] if row.startswith("./") else row for row in rows[:limit]]


def _evidence_paths(limit: int) -> list[str]:
    """按文件名列出已有结果/证据，不读取 GB 级交付物正文。"""
    roots = _existing_search_roots([
        "dev", "harness_baselines/comparison", "harness_baselines/fair_bench_40",
    ])
    if not roots:
        return []
    rows = _command_lines([
        "rg", "--files", *roots, "-g", "*benchmark*", "-g", "*arena*",
        "-g", "*evidence*", "-g", "*result*.json", "-g", "*summary*.json",
        "-g", "!**/node_modules/**", "-g", "!**/.build/**",
    ], timeout=5)
    return [row[2:] if row.startswith("./") else row for row in rows[:limit]]


def _packaged_prior_art(terms: list[str], limit: int) -> dict:
    """从已安装源码包的原清单检索；不猜 Git 身份、不读清单外的文件。"""
    search = {
        "backend": "core_package_manifest", "status": "failed",
        "indexed_files": 0, "scanned_files": 0, "matched_files": 0,
        "skipped_files": 0, "failed_files": 0, "bytes_read_upper_bound": 0,
        "result_truncated": False, "issues": [], "issues_truncated": False,
        "limits": {"file_bytes": _PRIOR_ART_FILE_BYTES,
                   "total_bytes": _PRIOR_ART_TOTAL_BYTES, "seconds": _PRIOR_ART_SECONDS},
    }
    result = {
        "ok": True, "current_source_and_entrypoints": [],
        "existing_results_and_evidence": [], "checkpoint_candidates": [],
        "observations": {
            "source_search": search,
            "git_history": {
                "status": "not_in_package",
                "reason": "源码包保留固定提交与文件校验值，不附带 Git 历史；空列表不表示没有历史。",
            },
            "existing_results_and_evidence": {
                "status": "filename_index_only", "truncated": False,
                "reason": "仅列清单中的文件名候选，不代表包含历史运行结果或已读取正文。",
            },
            "checkpoint_candidates": {"status": "filename_index_only", "truncated": False},
        },
    }

    def issue(reason: str, path: str = "", error: Exception | None = None) -> None:
        row = {"reason": reason}
        if path:
            row["path"] = path
        if error:
            row["error_type"] = type(error).__name__
            if isinstance(error, OSError):
                row["errno"] = error.errno
        # 不保存任意异常正文、主机绝对路径或源文件内容。
        if len(search["issues"]) < 20:
            search["issues"].append(row)
        else:
            search["issues_truncated"] = True

    started = time.monotonic()
    try:
        with _core_package_index(_REPO) as (validator, root_fd, manifest):
            files = manifest["files"]
            revision = manifest["source_commit"]
            search.update(indexed_files=len(files), revision=revision)
            names = [entry["path"] for entry in files]
            evidence, checkpoints = [], []
            for name in names:
                leaf = pathlib.PurePosixPath(name).name.casefold()
                if (any(word in leaf for word in ("benchmark", "arena", "evidence"))
                        or (leaf.endswith(".json") and
                            any(word in leaf for word in ("result", "summary")))):
                    evidence.append({"path": name, "source": "package_filename_index"})
                if leaf.endswith(".md") and (
                    any(word in leaf for word in ("plan", "context", "summary", "handoff", "roadmap"))
                    or name.startswith((".claude/plans/", "docs/plans/"))
                ):
                    checkpoints.append(name)
            result["existing_results_and_evidence"] = evidence[:limit]
            result["checkpoint_candidates"] = checkpoints[:limit]
            result["observations"]["existing_results_and_evidence"]["truncated"] = len(evidence) > limit
            result["observations"]["checkpoint_candidates"]["truncated"] = len(checkpoints) > limit
            for index, entry in enumerate(files):
                if time.monotonic() - started >= _PRIOR_ART_SECONDS:
                    search["skipped_files"] += len(files) - index
                    issue("达到本次源码读取时间预算；未扫描部分不作无匹配判断。")
                    break
                # 包含一次读尾字节，计入失败尝试；坏文件不能绕过总读取预算。
                cost = entry["size"] + 1
                if (entry["size"] > _PRIOR_ART_FILE_BYTES or
                        search["bytes_read_upper_bound"] + cost > _PRIOR_ART_TOTAL_BYTES):
                    search["skipped_files"] += 1
                    issue("达到单文件或总读取字节预算；此文件尚未扫描。", entry["path"])
                    continue
                search["bytes_read_upper_bound"] += cost
                try:
                    raw, digest = _core_package_file(validator, root_fd, revision, entry)
                except (OSError, ValueError) as exc:
                    result["ok"] = False
                    search["failed_files"] += 1
                    reason = ("文件无法安全读取（不存在、权限或符号链接等），未返回其匹配。"
                              if isinstance(exc, OSError) else
                              "文件类型、大小或内容校验值与源码包不一致，未返回其匹配。")
                    issue(reason, entry["path"], exc)
                    continue
                search["scanned_files"] += 1
                content = raw.decode("utf-8", errors="replace").casefold()
                matches = [term for term in terms if term.casefold() in content]
                if matches:
                    search["matched_files"] += 1
                    if len(result["current_source_and_entrypoints"]) < limit:
                        result["current_source_and_entrypoints"].append({
                            "path": entry["path"], "queries": matches, "revision": revision,
                            "content_source": "core-package:" + revision, "content_sha256": digest,
                        })
            search["result_truncated"] = search["matched_files"] > limit
            search["status"] = ("partial" if search["skipped_files"] or search["failed_files"]
                                else "complete")
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        code = ("core_package_manifest_unavailable" if isinstance(exc, (ImportError, OSError))
                else "core_package_manifest_invalid")
        result.update(ok=False, error_code=code,
                      error="核心包清单无法安全读取或校验；未退回到无版本、无边界的文件扫描。")
        issue(result["error"], error=exc)
    return result


def discover_prior_art(queries: Any, limit: int = 40) -> dict:
    """开工前检索现有实现、证据、Git 演进和交接候选，避免重复造轮子。

    这里只返回仓库事实，不替开发者判断采用哪一版。调用方读完证据后必须显式选择
    reuse / extend / replace；replace 还要说明旧实现为何不适用或为何曾被撤销。
    """
    values = [queries] if isinstance(queries, str) else list(queries or [])
    terms = list(dict.fromkeys(str(value).strip()[:160] for value in values
                              if str(value or "").strip()))[:8]
    if not terms:
        return {"ok": False, "error": "queries 不能为空；请提供功能名、入口或历史术语"}
    cap = max(1, min(int(limit or 40), 200))
    result = {
        "ok": True, "queries": terms,
        "git_history": [], "matching_commit_subjects": [],
        "matched_path_history": [], "deleted_or_reverted_paths": [],
        "decision_contract": {
            "required": True, "choices": ["reuse", "extend", "replace"],
            "rule": ("实现前选择一项并写明依据；replace 引用旧 revision、证据及不适用原因。"
                     "未提供的历史应保留为未知，必要时请求关联证据；不得当成没有旧实现。"),
        },
    }
    if (os.path.lexists(_REPO / "CORE-PACKAGE.json")
            and not os.path.lexists(_REPO / ".git")):
        result.update(_packaged_prior_art(terms, cap))
        return result
    # `git log -S` 会逐 commit 读取 blob；大仓×多词可慢到分钟级，不能成为默认开工税。
    # 当前树只扫高信号资产目录；历史只看命中路径和提交标题，都是索引级操作。
    current_paths = _current_asset_paths(terms, cap)
    current_rows: list[dict] = []
    for path in current_paths:
        matches: list[str] = []
        try:
            target = (_REPO / path).resolve()
            if target.is_file() and target.stat().st_size <= 2_000_000:
                content = target.read_text(encoding="utf-8", errors="replace").casefold()
                matches = [term for term in terms if term.casefold() in content]
        except OSError:
            pass
        current_rows.append({"path": path, "queries": matches or list(terms)})

    def parse_history(rows: list[str], query: str = "") -> list[dict]:
        parsed: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        commit: dict[str, Any] | None = None
        for line in rows:
            if line.startswith("COMMIT\t"):
                parts = line.split("\t", 3)
                commit = {"commit": parts[1], "date": parts[2],
                          "subject": parts[3] if len(parts) > 3 else "", "query": query}
                continue
            if commit and "\t" in line:
                status, path = line.split("\t", 1)
                key = (commit["commit"], status, path)
                if key not in seen:
                    seen.add(key)
                    parsed.append({**commit, "status": status, "path": path})
        return parsed

    subject_regex = "|".join(re.escape(term) for term in terms)
    subject_history = parse_history(_git_lines(
        ["log", "--all", "--date=short", "--pretty=format:COMMIT\t%H\t%ad\t%s",
         "--name-status", "--regexp-ignore-case", "--extended-regexp", "--grep", subject_regex,
         "-n", str(cap)], timeout=6), "matching_commit_subject")[:cap]
    path_history = parse_history(_git_lines(
        ["log", "--all", "--date=short", "--pretty=format:COMMIT\t%H\t%ad\t%s",
         "--name-status", "-n", str(cap), "--", *current_paths[:24]], timeout=6),
        "matched_path_history")[:cap] if current_paths else []
    deleted_rows = parse_history(_git_lines(
        ["log", "--all", "--diff-filter=D", "--date=short",
         "--pretty=format:COMMIT\t%H\t%ad\t%s", "--name-status", "-n", "200"], timeout=6),
        "recent_deleted_paths")
    lowered = [term.casefold() for term in terms]
    deleted = [row for row in deleted_rows if any(term in (str(row.get("path")) + " " +
               str(row.get("subject"))).casefold() for term in lowered)][:cap]
    history: list[dict] = []
    seen_history: set[tuple[str, str, str]] = set()
    for row in [*subject_history, *path_history]:
        key = (row["commit"], row["status"], row["path"])
        if key not in seen_history:
            seen_history.add(key)
            history.append(row)
        if len(history) >= cap:
            break
    evidence = [{"path": path, "source": "filename_index"}
                for path in _evidence_paths(cap)]
    checkpoint_patterns = (
        ".claude/plans/*.md", "dev/*context*.md", "dev/*summary*.md",
        "dev/*handoff*.md", "dev/*roadmap*.md",
    )
    checkpoints: list[str] = []
    for pattern in checkpoint_patterns:
        for path in _REPO.glob(pattern):
            if path.is_file():
                checkpoints.append(path.relative_to(_REPO).as_posix())
    checkpoints = sorted(set(checkpoints), key=lambda value: value.lower())[:cap]
    result.update({
        "current_source_and_entrypoints": current_rows,
        "existing_results_and_evidence": evidence,
        "git_history": history[:cap],
        "matching_commit_subjects": subject_history,
        "matched_path_history": path_history,
        "deleted_or_reverted_paths": deleted,
        "checkpoint_candidates": checkpoints,
    })
    return result


def _declared_source_entry(root: pathlib.Path) -> dict | None:
    """Match only directories declared by the existing peer-source inventory."""
    try:
        from legion_sandbox import peer_source_entries
        entries = peer_source_entries(root=_REPO)
    except (ImportError, OSError, ValueError, RuntimeError):
        return None
    matched = None
    depth = -1
    for entry in entries:
        try:
            logical = pathlib.Path(entry["path"])
            physical = pathlib.Path(entry["realpath"])
            if (not logical.is_absolute() or not physical.is_absolute()
                    or logical.resolve(strict=True) != physical or not physical.is_dir()):
                continue  # A declared file never grants access to its parent.
            if root.is_relative_to(physical) and len(physical.parts) > depth:
                matched, depth = entry, len(physical.parts)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            continue
    return matched


def _resolve_source_root(source_root: str) -> pathlib.Path | None:
    if not str(source_root or "").strip():
        return None
    candidate = pathlib.Path(source_root).expanduser()
    if not candidate.is_absolute():
        candidate = _REPO / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    allowed = [_REPO.resolve(), pathlib.Path(_paths.repos_dir()).resolve()]
    if (not any(resolved == root or resolved.is_relative_to(root) for root in allowed)
            and _declared_source_entry(resolved) is None):
        return None
    return resolved


def _read_regular_source_bytes(path: pathlib.Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("源码或 manifest 不是普通文件")
        return stream.read()


def _archived_reference(entry: dict, target: pathlib.Path, revision: str) -> tuple[str, str, str]:
    """Verify the requested file, not every file in the archived source tree."""
    source = pathlib.Path(entry["realpath"])
    marker_value = entry.get("manifest")
    if not marker_value:
        raise ValueError("归档缺少可验证的 .meyo-source.json")
    marker = pathlib.Path(marker_value)
    if (not marker.is_absolute() or marker.is_symlink()
            or marker.resolve(strict=True) != source / ".meyo-source.json"):
        raise ValueError("归档 manifest 不属于声明的源码根或是符号链接")
    data = json.loads(_read_regular_source_bytes(marker))
    if not isinstance(data, dict):
        raise ValueError("归档 manifest 格式无效")
    actual_revision = data.get("revision")
    files = data.get("files")
    if (not isinstance(actual_revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", actual_revision)
            or not isinstance(files, dict) or not files):
        raise ValueError("归档 manifest 的 revision/files 无效")
    if revision != actual_revision:
        raise ValueError("revision 与归档 manifest 不匹配")
    if entry.get("revision") and entry["revision"] != actual_revision:
        raise ValueError("catalog revision 与归档 manifest 不匹配")
    record = files.get(target.relative_to(source).as_posix())
    if (not isinstance(record, dict) or "symlink" in record
            or not isinstance(record.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
        raise ValueError("归档引用文件缺少可验证的 sha256")
    raw = _read_regular_source_bytes(target)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != record["sha256"]:
        raise ValueError("归档引用文件 sha256 不匹配")
    # Hash and line extraction use the same bytes, including original CRLF.
    return raw.decode("utf-8", errors="replace"), actual_revision, digest


@functools.lru_cache(maxsize=1)
def _core_package_validator():
    """Load the installed validator, never executable code from an audit input."""
    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "package_core.py"
    spec = importlib.util.spec_from_file_location("_legion_core_package_validator", path)
    if spec is None or spec.loader is None:
        raise ImportError("独立核心包校验器不可用")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _core_package_index(source: pathlib.Path):
    """Share the installed validator and no-follow manifest read with citations."""
    validator = _core_package_validator()
    root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with os.fdopen(validator._open_relative(root_fd, validator.MANIFEST), "rb") as stream:
            if os.fstat(stream.fileno()).st_size > validator.MAX_METADATA_BYTES:
                raise ValueError("核心包清单超过大小限制")
            encoded = stream.read(validator.MAX_METADATA_BYTES + 1)
        if len(encoded) > validator.MAX_METADATA_BYTES:
            raise ValueError("核心包清单超过大小限制")
        manifest = validator._parse_manifest(encoded)
        yield validator, root_fd, manifest
    finally:
        os.close(root_fd)


def _core_package_file(validator, root_fd: int, revision: str, record: dict) -> tuple[bytes, str]:
    with os.fdopen(validator._open_relative(root_fd, record["path"]), "rb") as stream:
        if os.fstat(stream.fileno()).st_size != record["size"]:
            raise ValueError("引用文件大小与核心包清单不匹配")
        raw = stream.read(record["size"] + 1)
    if len(raw) != record["size"]:
        raise ValueError("读取时引用文件大小发生变化")
    digest, blob = validator._digest_file(io.BytesIO(raw), len(raw), validator._algorithm(revision))
    if digest != record["sha256"] or blob != record["git_blob"]:
        raise ValueError("引用文件 hash 与核心包清单不匹配")
    return raw, digest


def _core_package_reference(source: pathlib.Path, target: pathlib.Path,
                            revision: str) -> tuple[str, str, str]:
    """Bind a citation to the package's declared version and exact file bytes."""
    with _core_package_index(source) as (validator, root_fd, manifest):
        canonical_revision = manifest["source_commit"]
        if revision != canonical_revision:
            raise ValueError("revision 与核心包固定提交不匹配")
        relative = target.relative_to(source).as_posix()
        record = next((item for item in manifest["files"] if item["path"] == relative), None)
        if record is None:
            raise ValueError("引用文件不在核心包清单中")
        raw, digest = _core_package_file(validator, root_fd, canonical_revision, record)
    # This proves consistency with the manifest, not remote authorship.
    return raw.decode("utf-8", errors="replace"), canonical_revision, digest


def verify_reference(source_root: str, file: str, line: int, quote: str = "",
                     revision: str = "") -> dict:
    root = _resolve_source_root(source_root)
    if root is None or not root.is_dir():
        return {"ok": False, "error": "source_root 不存在或不在允许的源码目录"}
    rel = pathlib.Path(str(file or ""))
    if rel.is_absolute() or ".." in rel.parts:
        return {"ok": False, "error": "file 必须是 source_root 内的相对路径"}
    try:
        target = (root / rel).resolve()
    except (OSError, RuntimeError):
        return {"ok": False, "error": "引用文件不存在"}
    if not target.is_relative_to(root) or not target.is_file():
        return {"ok": False, "error": "引用文件不存在", "path": str(target)}
    try:
        number = int(line)
    except (TypeError, ValueError):
        return {"ok": False, "error": "line 必须是正整数"}
    revision = str(revision or "").strip()
    content_source = "worktree"
    canonical_revision = ""
    content_digest = ""
    entry = _declared_source_entry(root) if revision else None
    archived = bool(entry and entry.get("manifest")
                    and not (pathlib.Path(entry["realpath"]) / ".git").exists())
    own_source = _REPO.resolve()
    packaged = (bool(revision) and root.is_relative_to(own_source)
                and os.path.lexists(own_source / "CORE-PACKAGE.json")
                # A nested installed peer retains its own Git identity.
                and not any(os.path.lexists(parent / ".git")
                            for parent in (root, *root.parents)
                            if parent.is_relative_to(own_source)))
    if revision and archived:
        try:
            content, canonical_revision, content_digest = _archived_reference(entry, target, revision)
            content_source = "archive:" + canonical_revision
        except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            return {"ok": False, "error": f"归档revision核验失败:{type(exc).__name__}:{exc}"}
    elif revision and packaged:
        try:
            # Keep the logical path: resolving it would hide source symlinks.
            content, canonical_revision, content_digest = _core_package_reference(
                own_source, root / rel, revision
            )
            content_source = "core-package:" + canonical_revision
        except (ImportError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
            return {"ok": False, "error": f"核心包revision核验失败:{type(exc).__name__}:{exc}"}
    elif revision:
        try:
            repo_result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root,
                                         capture_output=True, text=True, timeout=10)
            if repo_result.returncode != 0:
                return {"ok": False, "error": "source_root 不属于可验证的 git 仓库"}
            git_root = pathlib.Path(repo_result.stdout.strip()).resolve()
            relative = target.relative_to(git_root).as_posix()
            resolved = subprocess.run(["git", "rev-parse", "--verify", f"{revision}^{{commit}}"],
                                      cwd=git_root, capture_output=True, text=True, timeout=10)
            if resolved.returncode != 0:
                return {"ok": False, "error": "revision 不是当前仓库可验证的 commit", "revision": revision}
            canonical_revision = resolved.stdout.strip()
            completed = subprocess.run(
                ["git", "show", f"{canonical_revision}:{relative}"], cwd=git_root,
                capture_output=True, text=True, timeout=10)
            if completed.returncode != 0:
                return {"ok": False, "error": "revision 中找不到引用文件",
                        "revision": canonical_revision, "file": relative}
            content = completed.stdout
            content_source = "git:" + canonical_revision
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"ok": False, "error": f"revision 核验失败:{type(exc).__name__}:{exc}"}
    else:
        content = target.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()
    if number < 1 or number > len(lines):
        return {"ok": False, "error": "line 超出文件范围", "line_count": len(lines)}
    actual = lines[number - 1]
    if quote and str(quote).strip() not in actual:
        return {"ok": False, "error": "quote 与指定行不匹配", "actual": actual}
    return {"ok": True, "path": str(target), "line": number, "line_count": len(lines),
            "actual": actual, "content_source": content_source, "revision": canonical_revision,
            "content_sha256": content_digest or hashlib.sha256(content.encode("utf-8")).hexdigest()}


def prepare(goal: str, scope: Any = None, dimensions: Any = None,
            mode: str = "full-loop", uid: str = "default") -> dict:
    goal = str(goal or "").strip()
    if not goal:
        return {"ok": False, "error": "goal 不能为空"}
    # 只观察可选指南的路径类型，不读内容，也不因缺指南阻断已有审核入口。
    try:
        skill_status = "present" if stat.S_ISREG(_SKILL.lstat().st_mode) else "not_regular"
    except FileNotFoundError:
        skill_status = "missing"
    except OSError:
        skill_status = "unavailable"
    skill = str(_SKILL) if skill_status == "present" else None
    return {
        "ok": True,
        "goal": goal,
        "scope": scope or "self",
        "dimensions": dimensions or [],
        "mode": mode if mode in {"audit", "compare", "improve", "full-loop"} else "full-loop",
        "skill": skill,
        "skill_status": skill_status,
        "baseline": str(_BASELINE) if _BASELINE.is_file() else None,
        "prior_art_required": True,
        "prior_art_action": {
            "tool": "runnable:source_audit", "action": "prior_art",
            "required_layers": ["current_source_and_entrypoints", "existing_results_and_evidence",
                                "git_history", "checkpoint_candidates"],
            "decision_required": ["reuse", "extend", "replace"],
            "rule": "四层资产考古完成并明确复用决策前，不得新建同类实现。",
        },
        "source_search": [
            "先查当前源码/入口与现有结果证据，再查Git全历史（含删除/撤销）和上次checkpoint",
            "读取manifest的url/local_source/source_override/revision",
            "检查which/symlink/包metadata/binary strings/--version",
            "官方GitHub或GitLab clone/fetch运行版本的精确revision",
        ],
        "evidence_contract": ["source_root", "revision", "file", "line", "quote",
                              "runtime_evidence", "verifier", "status"],
        "ecosystem_loop": [
            "接入:统一runtime manifest登记身份/版本/协议/场景/模型兼容/速度档位",
            "优化:用真实trace和性能基线定位链路根因，修通用机制并横查同类",
            "共享:自动继承模型/appid/工具/Skill/记忆/cache key/事件/产物/恢复",
            "协作:通过fork/need_peer/handoff/补位接力参与完整军团编排",
            "交付:L0-L4分层真验，干净final、真工具、真文件、跨轮恢复均留证据",
            "进化:把根因、修法、前后数据写回源码账本并沉淀到Wiki/Skill",
        ],
        "next": (
            "先调用 prior_art action 并选择 reuse/extend/replace，"
            + ("再读取 skill 建立待证事实矩阵；" if skill else
               "skill 指南当前不可用（见 skill_status），现有源码检索和 evidence_contract 仍保留；")
            + "需要同伴时使用现有 fork/need_peer/handoff。"
        ),
        "uid": _safe(uid),
    }


def record(entry: dict, uid: str = "default") -> dict:
    if not isinstance(entry, dict):
        return {"ok": False, "error": "entry 必须是对象"}
    required = ("member", "dimension", "source_root", "revision", "file", "line", "quote", "verifier", "status")
    missing = [key for key in required if entry.get(key) in (None, "")]
    if missing:
        return {"ok": False, "error": "缺少证据字段", "missing": missing}
    status = str(entry.get("status"))
    if status not in _STATUS:
        return {"ok": False, "error": f"status 必须是 {sorted(_STATUS)}"}
    checked = verify_reference(str(entry["source_root"]), str(entry["file"]),
                               int(entry["line"]), str(entry["quote"]), str(entry["revision"]))
    if not checked.get("ok"):
        return {"ok": False, "error": "源码引用核验失败", "verification": checked}
    if status == "verified" and not str(entry.get("verifier") or "").strip():
        return {"ok": False, "error": "verified 结论必须记录 verifier"}
    audit_id = _safe(str(entry.get("audit_id") or f"audit-{int(time.time() * 1000)}"), "audit")
    normalized = {
        "audit_id": audit_id,
        "ts": time.time(),
        "member": str(entry["member"]),
        "dimension": str(entry["dimension"]),
        "finding_id": str(entry.get("finding_id") or "")[:160],
        "target": str(entry.get("target") or "")[:700],
        "claim": str(entry.get("claim") or ""),
        "source_root": str(_resolve_source_root(str(entry["source_root"]))),
        "revision": str(checked.get("revision") or entry["revision"]),
        "file": str(entry["file"]),
        "line": int(entry["line"]),
        "quote": str(entry["quote"]),
        "runtime_evidence": entry.get("runtime_evidence") or "runtime_not_tested",
        "verifier": str(entry["verifier"]),
        "status": status,
        "improvement": entry.get("improvement") or None,
        "verification": {"path": checked["path"], "actual": checked["actual"],
                         "content_source": checked.get("content_source"),
                         "content_sha256": checked.get("content_sha256")},
    }
    ledger = pathlib.Path(evidence_path(uid))
    with _locked(ledger):
        entries = _read_entries(ledger)
        entries.append(normalized)
        _write_entries(ledger, entries)
    learned = {"recorded": False, "reason": "only verified findings enter the shared evolution layer"}
    if status == "verified":
        try:
            import skill_wiki
            pattern = {
                "key": _safe(f"source-{normalized['member']}-{normalized['dimension']}", "source-pattern"),
                "title": f"{normalized['member']} · {normalized['dimension']}",
                "kind": "success" if normalized.get("improvement") else "failure",
                "trigger": normalized.get("claim") or normalized["dimension"],
                "root_cause": normalized.get("claim") or normalized["quote"],
                "workaround": str(normalized.get("improvement") or "保留该源码证据，下一次接入或修改时先核验同类机制"),
                "evidence": (f"{normalized['source_root']}@{normalized['revision']}:"
                             f"{normalized['file']}:{normalized['line']} · {normalized['runtime_evidence']}"),
            }
            learned = skill_wiki.record_verified_pattern(uid, pattern,
                                                         source=f"source_audit:{audit_id}")
        except Exception as exc:
            learned = {"recorded": False, "reason": f"{type(exc).__name__}:{exc}"}
    return {"ok": True, "entry": normalized, "ledger": str(ledger), "learning": learned}


def list_entries(uid: str = "default", member: str = "", status: str = "", limit: int = 50) -> dict:
    entries = _read_entries(pathlib.Path(evidence_path(uid)))
    if member:
        entries = [row for row in entries if row.get("member") == member]
    if status:
        entries = [row for row in entries if row.get("status") == status]
    entries = entries[-max(1, min(int(limit or 50), 200)):]
    return {"ok": True, "uid": _safe(uid), "count": len(entries), "entries": entries}


def show(audit_id: str, uid: str = "default") -> dict:
    for row in reversed(_read_entries(pathlib.Path(evidence_path(uid)))):
        if row.get("audit_id") == audit_id:
            return {"ok": True, "entry": row}
    return {"ok": False, "error": "audit_id 不存在"}
