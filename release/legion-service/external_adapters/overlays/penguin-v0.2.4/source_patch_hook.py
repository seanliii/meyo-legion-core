#!/usr/bin/env python3
"""Pinned source overlay hook for an existing install argv, not an installer.

No clone, fetch, dependency install, build, live runtime edit, registration, or launch.
Use an exclusive staging checkout, or stage a hash-locked official npm bundle
into a new generation. Never reset unknown edits or activate a generation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import shutil
import tempfile
import base64
import ctypes
import errno
import io
import sys
import tarfile
import urllib.request

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


def _publish_noreplace(parent_fd: int, staging: str, destination: str) -> None:
    # Same native primitive as jcode_native._publish_public_model; fail closed.
    if (PurePosixPath(staging).is_absolute() or ".." in PurePosixPath(staging).parts
            or destination in ("", ".", "..") or "/" in destination):
        raise ValueError("publication names must remain in the opened parent namespace")
    libc = ctypes.CDLL(None, use_errno=True)
    name, flags = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    rename = getattr(libc, name, None) if sys.platform in ("darwin", "linux") else None
    if rename is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace publication unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(parent_fd, os.fsencode(staging), parent_fd, os.fsencode(destination), flags):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def stage_npm_bundle(source: Path, destination: Path, overlay: Path,
                     archive: Path | None = None, *, fetch_official: bool = False) -> dict:
    """Copy an official fixed package, validate/apply overlay, publish atomically.

    The installed source is read-only. Destination must be new; unknown/live
    generations are never overwritten. The existing installer owns activation.
    """
    overlay = overlay.resolve(strict=True)
    manifest = json.loads((overlay / "source-overlay.json").read_text())
    if manifest.get("kind") != "npm_bundle":
        raise ValueError("npm bundle overlay required")
    patch = overlay / manifest["patch_file"]
    if patch.is_symlink() or patch.parent != overlay or sha(patch) != manifest["patch_sha256"]:
        raise ValueError("patch identity mismatch")
    source = source.resolve(strict=True)
    package = json.loads((source / "package.json").read_text())
    if (package.get("name"), package.get("version")) != (manifest["package"], manifest["version"]):
        raise ValueError("official npm package identity mismatch")
    if not (source / manifest["license_file"]).is_file():
        raise ValueError("upstream license must remain in package")
    targets = {}
    for name, hashes in manifest["files"].items():
        part = PurePosixPath(name)
        path = source / name
        if (part.is_absolute() or ".." in part.parts or path.is_symlink()
                or not path.resolve(strict=True).is_relative_to(source)
                or sha(path) != hashes["before_sha256"]):
            raise ValueError("official npm bundle hash/path mismatch")
        targets[name] = hashes
    if archive is None:
        if not fetch_official:
            raise ValueError("locked official archive required for complete package proof")
        url = manifest["official_tarball"]
        if not url.startswith("https://registry.npmjs.org/"):
            raise ValueError("official npm archive URL required")
        with urllib.request.urlopen(url, timeout=45) as response:
            if response.url != url:
                raise ValueError("official archive redirect rejected")
            archive_bytes = response.read()
    else:
        archive_bytes = archive.read_bytes()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(archive_bytes).digest()).decode()
    if integrity != manifest["official_integrity"]:
        raise ValueError("official archive SRI mismatch")
    contents = {}
    modes = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        for member in tar:
            part = PurePosixPath(member.name)
            if part.is_absolute() or ".." in part.parts or not part.parts or part.parts[0] != "package":
                raise ValueError("archive path escapes package")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError("archive links/special files rejected")
            relative = str(PurePosixPath(*part.parts[1:]))
            if relative in contents:
                raise ValueError("duplicate archive member")
            contents[relative] = tar.extractfile(member).read()
            modes[relative] = member.mode & 0o777
    expected_dirs = {str(parent) for name in contents for parent in PurePosixPath(name).parents
                     if str(parent) != "."}
    actual = {}
    actual_dirs = set()
    for path in source.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("installed package links/special files rejected")
        if path.is_dir():
            actual_dirs.add(str(path.relative_to(source)))
        if path.is_file():
            actual[str(path.relative_to(source))] = path.read_bytes()
    if actual != contents or actual_dirs != expected_dirs:
        raise ValueError("complete official package byte closure mismatch (extra/missing/changed files)")
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = destination.parent.resolve(strict=True) / destination.name
    if (destination.exists() or destination.is_symlink() or destination.is_relative_to(source)
            or source.is_relative_to(destination)):
        raise ValueError("destination must be a new separate generation")
    with tempfile.TemporaryDirectory(prefix=".native-overlay-", dir=destination.parent) as temp:
        staged = Path(temp) / "package"
        staged.mkdir()
        for name, content in contents.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(modes[name])
        git(staged, "apply", "--check", str(patch))
        git(staged, "apply", str(patch))
        for name, hashes in targets.items():
            if sha(staged / name) != hashes["after_sha256"]:
                raise ValueError("post-patch bundle identity mismatch")
        (staged / ".native-overlay.json").write_text(json.dumps(manifest, indent=2) + "\n")
        parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            _publish_noreplace(parent_fd, str(staged.relative_to(destination.parent)), destination.name)
        finally:
            os.close(parent_fd)
    return {"scope": "npm_staging_only", "destination": str(destination),
            "official_integrity_verified": integrity, "official_files_verified": len(contents),
            "package": manifest["package"], "version": manifest["version"],
            "patch_sha256": manifest["patch_sha256"], "source_changed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "apply", "revert", "stage-npm"))
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--fetch-official", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "stage-npm":
            if args.destination is None or args.overlay is None:
                raise ValueError("stage-npm requires destination and overlay")
            result = stage_npm_bundle(args.source, args.destination, args.overlay,
                                      args.archive, fetch_official=args.fetch_official)
        else:
            result = operate(args.source, args.action)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
