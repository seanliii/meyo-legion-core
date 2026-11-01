#!/usr/bin/env python3
"""Pinned source overlay hook for an existing install argv, not an installer.

No clone, fetch, dependency install, build, runtime edit, registration, or launch.
Use only an exclusive staging checkout, before building. Never reset unknown edits.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess

HERE = Path(__file__).resolve().parent


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(source), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise ValueError("Git source operation failed: " + result.stderr[-800:])
    return result.stdout


def inspect(source: Path):
    manifest = json.loads((HERE / "source-overlay.json").read_text())
    patch = HERE / manifest["patch_file"]
    if patch.is_symlink() or sha(patch) != manifest["patch_sha256"]:
        raise ValueError("patch identity mismatch")
    source = source.resolve(strict=True)
    if not (source / ".git").exists():
        raise ValueError("an actual pinned upstream Git staging checkout is required")
    if source.name == ".penguin" or any(parent.name == ".penguin" for parent in source.parents):
        raise ValueError("installed Penguin homes are not source staging checkouts")
    if any((parent / ".materialized.json").exists() for parent in (source, *source.parents)):
        raise ValueError("published/materialized generations cannot be patched in place")
    if Path(git(source, "rev-parse", "--show-toplevel").strip()).resolve() != source:
        raise ValueError("source must be the Git root, not a vendored subdirectory")
    if git(source, "rev-parse", "HEAD").strip() != manifest["upstream_revision"]:
        raise ValueError("upstream revision mismatch")
    remote = git(source, "remote", "get-url", "origin").strip().removesuffix(".git")
    if remote != manifest["repository"].removesuffix(".git"):
        raise ValueError("upstream repository mismatch")
    paths = set(manifest["files"])
    dirty = set(filter(None, git(source, "diff", "--name-only", "-z", "HEAD", "--").split("\0")))
    untracked = list(filter(None, git(source, "ls-files", "--others", "--exclude-standard", "-z").split("\0")))
    if dirty - paths or untracked or git(source, "diff", "--cached", "--name-only").strip():
        raise ValueError("unrelated/staged source changes are preserved; use an exclusive clean stage")
    hashes = {}
    for relative in paths:
        part = PurePosixPath(relative)
        if part.is_absolute() or ".." in part.parts or ".git" in part.parts:
            raise ValueError("invalid source target")
        target = source / relative
        if target.is_symlink() or target.resolve(strict=True) != target or not target.is_file():
            raise ValueError("source target is not a plain in-root file")
        hashes[relative] = sha(target)
    before = {name: value["before_sha256"] for name, value in manifest["files"].items()}
    after = {name: value["after_sha256"] for name, value in manifest["files"].items()}
    state = "before" if hashes == before else "after" if hashes == after else "unrecognized"
    if state == "unrecognized":
        raise ValueError("source hashes are mixed or modified; no overwrite or reset")
    return source, manifest, patch, state


def operate(source: Path, action: str) -> dict:
    source, manifest, patch, state = inspect(source)
    target = "after" if action == "apply" else "before"
    result = {"scope": "source_only", "source": str(source), "action": action,
              "upstream_revision": manifest["upstream_revision"],
              "patch_sha256": manifest["patch_sha256"], "state": state,
              "changed": False, "runtime_installed_or_changed": False}
    if action == "check" or state == target:
        return result
    direction = ["--reverse"] if action == "revert" else []
    git(source, "apply", "--check", *direction, str(patch))
    git(source, "apply", *direction, str(patch))
    _, _, _, observed = inspect(source)
    if observed != target:
        raise ValueError("post-apply source identity mismatch; source preserved for inspection")
    return {**result, "state": observed, "changed": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "revert"))
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = operate(args.source, args.action)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
