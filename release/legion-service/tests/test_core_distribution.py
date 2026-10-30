"""Offline distribution contract, exercised against disposable Git repositories."""

import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "package_core.py"
MANIFEST = "CORE-PACKAGE.json"


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for variable, leaf in (
        ("HOME", "home"),
        ("MEYO_LEGION_DIR", "meyo"),
        ("LEGION_HOME_DIR", "legion"),
    ):
        directory = tmp_path / leaf
        directory.mkdir()
        monkeypatch.setenv(variable, str(directory))
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("LC_ALL", "C")


@pytest.fixture
def package_core():
    assert SCRIPT.is_file(), "missing private core build/verify/extract implementation"
    spec = importlib.util.spec_from_file_location("core_distribution_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo, *args):
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
         "-C", str(repo), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=15,
    )
    return completed.stdout.decode().strip()


def commit(repo):
    git(repo, "add", "-f", "--all")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "微型源码 fixture")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-q")
    sources = {
        "README.md": b"Private source, no public license.\n",
        "LOCAL-CANDIDATE.md": b"Not proof of any member execution.\n",
        "meyo.py": b"print('fixture only')\n",
        "Makefile": b"check:\n\t@echo fixture\n",
        ".gitignore": b".env\n.venv/\nsnapshot_failures/\n",
        "release/legion-service/token_telemetry.py": b"def count(): return 1\n",
        "runtime/core-sources/qa_system/state/__init__.py": b"VALUE = 'source'\n",
        "runtime/core-sources/qa_system/state/schema.json": b'{"type":"object"}\n',
        "harness_baselines/example/engine.py": b"def run(): return 'not executed'\n",
        "deploy/linux/start.sh": b"#!/bin/sh\nexit 0\n",
        "docs/example.md": "仅私有本地分发。\n".encode(),
        "new-unlisted-module/feature.py": b"ANSWER = 42\n",
        "docs/.env.example": b"API_KEY=\n",
    }
    for name, content in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (root / "deploy/linux/start.sh").chmod(0o755)
    revision = commit(root)
    return root, revision, sources


def build(package_core, repository, tmp_path, filename="core.tar"):
    root, revision, _ = repository
    target = tmp_path / filename
    package_core.build_package(root, revision, target)
    return target


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rewrite_archive(original, destination, change):
    """Re-sign malicious fixtures so inner checks, not only the outer hash, run."""
    with tarfile.open(original, "r:") as archive:
        members = [
            (copy.copy(item), archive.extractfile(item).read() if item.isfile() else b"")
            for item in archive
        ]
    change(members)
    with tarfile.open(destination, "w", format=tarfile.USTAR_FORMAT) as archive:
        for item, content in members:
            archive.addfile(item, io.BytesIO(content) if item.isfile() else None)
    Path(str(destination) + ".sha256").write_text(checksum(destination) + "\n")
    return destination


def test_cli_help_is_available():
    completed = subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert all(command in completed.stdout for command in ("build", "verify", "extract"))


def test_same_commit_produces_identical_bytes_and_all_source(package_core, repository, tmp_path):
    root, revision, sources = repository
    first = build(package_core, repository, tmp_path, "one.tar")
    os.utime(root / "meyo.py", (42, 42))
    (root / "meyo.py").chmod(0o600)
    git(root, "config", "tar.umask", "0077")
    second = build(package_core, repository, tmp_path, "two.tar")
    assert first.read_bytes() == second.read_bytes()
    assert Path(str(first) + ".sha256").read_text() == checksum(first) + "\n"
    assert Path(str(first) + ".sha256").read_bytes() == Path(str(second) + ".sha256").read_bytes()
    manifest = package_core.verify_package(first)
    assert manifest["source_commit"] == revision
    assert manifest["distribution"] == "private-local-only"
    assert manifest["public_license_granted"] is False
    assert manifest["member_execution_verified"] is False
    assert {entry["path"] for entry in manifest["files"]} == set(sources)
    for entry in manifest["files"]:
        assert entry["sha256"] == hashlib.sha256(sources[entry["path"]]).hexdigest()
        assert entry["size"] == len(sources[entry["path"]])
    with tarfile.open(first, "r:") as archive:
        assert set(archive.getnames()) == set(sources) | {MANIFEST}
        for entry in archive:
            assert entry.isreg() and entry.uid == entry.gid == entry.mtime == 0
            assert entry.uname == entry.gname == ""


def test_extract_really_reads_back_without_git_or_old_source(package_core, repository, tmp_path, monkeypatch):
    archive = build(package_core, repository, tmp_path)
    destination = tmp_path / "fresh"
    repository[0].rename(tmp_path / "old-source-no-longer-at-original-path")
    monkeypatch.setenv("PATH", "/no/git/or/runner")
    manifest = package_core.extract_package(archive, destination)
    assert manifest["source_commit"] == repository[1]
    assert {str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file()} == (
        set(repository[2]) | {MANIFEST}
    )
    for name, content in repository[2].items():
        assert (destination / name).read_bytes() == content
    assert (destination / "deploy/linux/start.sh").stat().st_mode & 0o777 == 0o755
    assert (destination / "meyo.py").stat().st_mode & 0o777 == 0o644
    assert not (destination / ".git").exists()
    assert not (destination / ".env").exists()
    assert destination.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("kind", ["unstaged", "staged", "untracked", "deleted"])
def test_build_refuses_dirty_tree(package_core, repository, tmp_path, kind):
    root, revision, _ = repository
    if kind == "untracked":
        (root / "snapshot-new.txt").write_text("local-only")
    elif kind == "deleted":
        (root / "meyo.py").unlink()
    else:
        (root / "meyo.py").write_text("changed")
        if kind == "staged":
            git(root, "add", "meyo.py")
    target = tmp_path / "refused.tar"
    with pytest.raises(package_core.PackageError, match="dirty|脏"):
        package_core.build_package(root, revision, target)
    assert not target.exists()
    assert not Path(str(target) + ".sha256").exists()


def test_ignored_local_data_never_enters_archive(package_core, repository, tmp_path):
    root, _, sources = repository
    for name in (".env", ".venv/bin/python", "snapshot_failures/local.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture data must not be opened by the packager")
        path.chmod(0)
    try:
        archive = build(package_core, repository, tmp_path)
        assert {entry["path"] for entry in package_core.verify_package(archive)["files"]} == set(sources)
    finally:
        for name in (".env", ".venv/bin/python", "snapshot_failures/local.json"):
            (root / name).chmod(0o600)


@pytest.mark.parametrize("name", [
    ".env", ".env.production", ".codex/auth.json", ".aws/credentials",
    ".meyo/user.json", ".legion/sessions/run.json", "secrets/provider.json",
    "login-token.txt", "credentials.json", ".venv/bin/python", "venv/pyvenv.cfg",
    "node_modules/pkg/index.js", "__pycache__/main.pyc",
    "models/weights.safetensors", "weights/model.gguf", "model.ckpt",
    "snapshot_failures/trace.json", "state/config.json", "sessions/run.jsonl",
    "logs/server.log", "history/old-run.json", "results/run.json", ".qa/evidence.json",
    "nested/.git/config", "nested/id_ed25519", "deploy/private.pem",
])
def test_build_refuses_committed_runtime_credentials_and_dependencies(
    package_core, repository, tmp_path, name,
):
    root, _, _ = repository
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("synthetic fixture, not a credential")
    if "/.git/" in name:
        # Git normally protects .git paths; exercise the same policy at verification below.
        with pytest.raises(package_core.PackageError):
            package_core.validate_source_path(name)
        return
    revision = commit(root)
    target = tmp_path / "forbidden.tar"
    with pytest.raises(package_core.PackageError, match="runtime|credential|dependency|运行|凭据|依赖"):
        package_core.build_package(root, revision, target)
    assert not target.exists()


@pytest.mark.parametrize("kind", ["symlink", "gitlink"])
def test_build_refuses_nonregular_git_entries(package_core, repository, tmp_path, kind):
    root, revision, _ = repository
    if kind == "symlink":
        (root / "linked.py").symlink_to("meyo.py")
        revision = commit(root)
    else:
        git(root, "update-index", "--add", "--cacheinfo", f"160000,{revision},vendor/submodule")
        git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "gitlink fixture")
        revision = git(root, "rev-parse", "HEAD")
    with pytest.raises(package_core.PackageError, match="symlink|gitlink|regular|链接|普通"):
        package_core.build_package(root, revision, tmp_path / "bad.tar")


@pytest.mark.parametrize("attribute", ["export-ignore", "export-subst"])
def test_archive_attributes_cannot_silently_drop_or_rewrite_source(
    package_core, repository, tmp_path, attribute,
):
    root, _, _ = repository
    (root / ".gitattributes").write_text(f"meyo.py {attribute}\n")
    (root / "meyo.py").write_text("# $Format:%H$\n")
    revision = commit(root)
    with pytest.raises(package_core.PackageError, match="archive|blob|文件集"):
        package_core.build_package(root, revision, tmp_path / "bad.tar")


def test_build_pins_full_commit_and_ignores_ambient_git_variables(
    package_core, repository, tmp_path, monkeypatch,
):
    root, revision, sources = repository
    (root / "meyo.py").write_text("a later committed version\n")
    commit(root)
    monkeypatch.setenv("GIT_DIR", "/does-not-exist")
    monkeypatch.setenv("GIT_WORK_TREE", "/does-not-exist")
    archive = build(package_core, (root, revision, sources), tmp_path)
    assert package_core.verify_package(archive)["source_commit"] == revision
    with tarfile.open(archive, "r:") as stream:
        assert stream.extractfile("meyo.py").read() == sources["meyo.py"]
    with pytest.raises(package_core.PackageError):
        package_core.build_package(root, "HEAD", tmp_path / "moving-ref.tar")


@pytest.mark.parametrize("mutation", ["bytes", "missing", "extra", "duplicate", "manifest-duplicate"])
def test_verify_rejects_tampering_even_with_recomputed_outer_hash(
    package_core, repository, tmp_path, mutation,
):
    original = build(package_core, repository, tmp_path)

    def change(items):
        index = next(i for i, (item, _) in enumerate(items) if item.name == "meyo.py")
        if mutation == "bytes":
            item, content = items[index]
            items[index] = (item, bytes([content[0] ^ 1]) + content[1:])
        elif mutation == "missing":
            del items[index]
        elif mutation == "extra":
            item = tarfile.TarInfo("extra.py")
            item.size = 1
            items.append((item, b"x"))
        elif mutation == "duplicate":
            items.append(copy.deepcopy(items[index]))
        else:
            m = next(i for i, (item, _) in enumerate(items) if item.name == MANIFEST)
            item, content = items[m]
            # Duplicate JSON object keys must not be silently last-key-wins.
            content = content.replace(b"{", b'{"source_commit":"invalid",', 1)
            item.size = len(content)
            items[m] = item, content

    bad = rewrite_archive(original, tmp_path / "changed.tar", change)
    with pytest.raises(package_core.PackageError):
        package_core.verify_package(bad)
    destination = tmp_path / "must-not-appear"
    with pytest.raises(package_core.PackageError):
        package_core.extract_package(bad, destination)
    assert not destination.exists()


@pytest.mark.parametrize("name", [
    "../escape.py", "/absolute.py", "a/../../escape.py", "a//b.py",
    "./meyo.py", "a\\escape.py", "C:/escape.py", "a/./b.py", "AUX.txt",
    "nested/.git/config", "trailing./file.py",
])
def test_verify_refuses_unsafe_paths(package_core, repository, tmp_path, name):
    original = build(package_core, repository, tmp_path)

    def change(items):
        item = tarfile.TarInfo(name)
        item.size = 1
        items.append((item, b"x"))

    bad = rewrite_archive(original, tmp_path / "path.tar", change)
    with pytest.raises(package_core.PackageError):
        package_core.extract_package(bad, tmp_path / "fresh")
    assert not (tmp_path / "fresh").exists()
    assert not (tmp_path / "escape.py").exists()


@pytest.mark.parametrize("kind", [
    tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE,
    tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.GNUTYPE_SPARSE,
])
def test_verify_refuses_links_and_nonregular_members(package_core, repository, tmp_path, kind):
    original = build(package_core, repository, tmp_path)

    def change(items):
        item = tarfile.TarInfo("unsafe")
        item.type = kind
        if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            item.linkname = "../escape"
        items.append((item, b""))

    bad = rewrite_archive(original, tmp_path / "link.tar", change)
    with pytest.raises(package_core.PackageError):
        package_core.verify_package(bad)


def test_independent_hash_is_required_and_checked(package_core, repository, tmp_path):
    archive = build(package_core, repository, tmp_path)
    sidecar = Path(str(archive) + ".sha256")
    trusted_hash = checksum(archive)
    sidecar.unlink()
    with pytest.raises(package_core.PackageError):
        package_core.verify_package(archive)
    assert package_core.verify_package(archive, expected_sha256=trusted_hash)["files"]
    with pytest.raises(package_core.PackageError):
        package_core.verify_package(archive, expected_sha256="0" * 64)
    sidecar.write_text("0" * 64 + "\n")
    with pytest.raises(package_core.PackageError):
        package_core.verify_package(archive)


@pytest.mark.parametrize("damage", ["truncate", "trailing", "padding", "declared-size"])
def test_tar_structure_and_bounds_are_checked(package_core, repository, tmp_path, damage):
    archive = build(package_core, repository, tmp_path)
    content = bytearray(archive.read_bytes())
    if damage == "truncate":
        content = content[:-512]
    elif damage == "trailing":
        content.extend(b"extra archive or nonzero junk")
    elif damage == "padding":
        with tarfile.open(archive, "r:") as stream:
            member = stream.getmember("meyo.py")
            content[member.offset_data + member.size] = 1
    else:
        header = tarfile.TarInfo(MANIFEST)
        header.size = 1 << 30  # Within USTAR's numeric range, beyond our read limit.
        content[:512] = header.tobuf(format=tarfile.USTAR_FORMAT)
    bad = tmp_path / "bad-structure.tar"
    bad.write_bytes(content)
    with pytest.raises(package_core.PackageError):
        package_core.verify_package(bad, expected_sha256=checksum(bad))


@pytest.mark.parametrize("kind", ["empty", "populated", "symlink", "under-git", "under-package"])
def test_extract_refuses_overwrite_and_embedding_in_existing_source(
    package_core, repository, tmp_path, kind,
):
    archive = build(package_core, repository, tmp_path)
    destination = tmp_path / "destination"
    if kind in ("empty", "populated"):
        destination.mkdir()
        if kind == "populated":
            (destination / "keep.txt").write_text("do not touch")
    elif kind == "symlink":
        destination.symlink_to(repository[0], target_is_directory=True)
    elif kind == "under-git":
        destination = repository[0] / "nested"
    else:
        old = tmp_path / "old-package"
        package_core.extract_package(archive, old)
        destination = old / "nested"
    with pytest.raises(package_core.PackageError):
        package_core.extract_package(archive, destination)
    if kind == "populated":
        assert (destination / "keep.txt").read_text() == "do not touch"
    if kind in ("under-git", "under-package"):
        assert not destination.exists()


def test_symlink_parent_is_not_followed(package_core, repository, tmp_path):
    archive = build(package_core, repository, tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "parent-link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(package_core.PackageError):
        package_core.extract_package(archive, link / "nested")
    assert list(real.iterdir()) == []


def test_build_does_not_overwrite_or_write_inside_checkout(package_core, repository, tmp_path):
    root, revision, _ = repository
    output = tmp_path / "existing.tar"
    output.write_bytes(b"keep existing bytes")
    with pytest.raises(package_core.PackageError):
        package_core.build_package(root, revision, output)
    assert output.read_bytes() == b"keep existing bytes"
    with pytest.raises(package_core.PackageError):
        package_core.build_package(root, revision, root / "nested.tar")
    assert not (root / "nested.tar").exists()


def test_size_limits_apply_to_build_and_verify(package_core, repository, tmp_path, monkeypatch):
    archive = build(package_core, repository, tmp_path)
    monkeypatch.setattr(package_core, "MAX_FILE_BYTES", 4)
    with pytest.raises(package_core.PackageError):
        package_core.build_package(repository[0], repository[1], tmp_path / "too-large.tar")
    with pytest.raises(package_core.PackageError):
        package_core.verify_package(archive)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_index_flags_do_not_hide_dirty_bytes(package_core, repository, tmp_path, flag):
    root, revision, _ = repository
    git(root, "update-index", flag, "meyo.py")
    (root / "meyo.py").write_text("hidden mutation\n")
    with pytest.raises(package_core.PackageError, match="dirty"):
        package_core.build_package(root, revision, tmp_path / "hidden.tar")


def test_git_timeout_fails_without_a_package(package_core, repository, tmp_path, monkeypatch):
    monkeypatch.setattr(package_core, "GIT_TIMEOUT_SECONDS", 0)
    with pytest.raises(package_core.PackageError):
        package_core.build_package(repository[0], repository[1], tmp_path / "timeout.tar")
    assert not (tmp_path / "timeout.tar").exists()


@pytest.mark.parametrize("kind", ["deep-json", "invalid-json", "false-license", "false-execution"])
def test_malformed_manifest_is_a_clean_cli_refusal(package_core, repository, tmp_path, kind):
    original = build(package_core, repository, tmp_path)

    def change(items):
        index = next(i for i, (item, _) in enumerate(items) if item.name == MANIFEST)
        item, content = items[index]
        if kind == "deep-json":
            content = b"[" * 20_000 + b"0" + b"]" * 20_000
        elif kind == "invalid-json":
            content = b"{not JSON"
        else:
            key = b'"public_license_granted":false' if kind == "false-license" else b'"member_execution_verified":false'
            content = content.replace(key, key.replace(b"false", b"true"))
        item.size = len(content)
        items[index] = item, content

    bad = rewrite_archive(original, tmp_path / "bad-manifest.tar", change)
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(SCRIPT), "verify", str(bad)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 2, result.stderr
    assert result.stdout == "" and "Traceback" not in result.stderr


@pytest.mark.parametrize("kind", ["case", "unicode", "reserved", "parent-symlink"])
def test_build_rejects_ambiguous_or_linked_source_paths(package_core, repository, tmp_path, kind):
    root, revision, _ = repository
    if kind == "parent-symlink":
        directory = root / "new-unlisted-module"
        directory.rename(tmp_path / "elsewhere")
        directory.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    elif kind == "case":
        # Case-insensitive macOS cannot create both names; Git's index can.
        oid = git(root, "rev-parse", "HEAD:meyo.py")
        git(root, "update-index", "--add", "--cacheinfo", f"100644,{oid},MEYO.py")
        git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "-qm", "case collision")
        revision = git(root, "rev-parse", "HEAD")
    elif kind == "unicode":
        # NFD filenames are not portable; no Unicode normalization may change a path silently.
        git(root, "config", "core.precomposeunicode", "false")
        (root / "cafe\u0301.py").write_text("x")
        revision = commit(root)
    else:
        (root / MANIFEST).write_text("a conflicting source file")
        revision = commit(root)
    with pytest.raises(package_core.PackageError):
        package_core.build_package(root, revision, tmp_path / "ambiguous.tar")


@pytest.mark.parametrize("which", ["archive", "checksum"])
def test_input_symlink_or_oversized_checksum_is_rejected(package_core, repository, tmp_path, which):
    original = build(package_core, repository, tmp_path)
    if which == "archive":
        linked = tmp_path / "linked.tar"
        linked.symlink_to(original)
        with pytest.raises(package_core.PackageError):
            package_core.verify_package(linked, expected_sha256=checksum(original))
    else:
        sidecar = Path(str(original) + ".sha256")
        sidecar.write_text(checksum(original) + "\n" * 200)
        with pytest.raises(package_core.PackageError):
            package_core.verify_package(original)
        sidecar.unlink()
        sidecar.symlink_to(original)
        with pytest.raises(package_core.PackageError):
            package_core.verify_package(original)


def test_extraction_cleans_only_its_new_directory_after_io_failure(
    package_core, repository, tmp_path, monkeypatch,
):
    archive = build(package_core, repository, tmp_path)
    actual_open = package_core._open_relative
    calls = 0

    def fail_after_one_file(root_fd, name, *, create=False):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic full disk")
        return actual_open(root_fd, name, create=create)

    monkeypatch.setattr(package_core, "_open_relative", fail_after_one_file)
    with pytest.raises(package_core.PackageError, match="full disk"):
        package_core.extract_package(archive, tmp_path / "partial")
    assert not (tmp_path / "partial").exists()
    assert (repository[0] / "meyo.py").read_bytes() == repository[2]["meyo.py"]


def test_cli_build_verify_extract_and_failure_exit_codes(package_core, repository, tmp_path):
    archive = tmp_path / "cli.tar"
    destination = tmp_path / "cli-source"

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), *map(str, args)],
            capture_output=True, text=True, timeout=30,
        )

    made = cli("build", "--repo", repository[0], "--commit", repository[1], "--output", archive)
    assert made.returncode == 0, made.stderr
    assert json.loads(made.stdout)["source_commit"] == repository[1]
    verified = cli("verify", archive)
    assert verified.returncode == 0, verified.stderr
    extracted = cli("extract", archive, "--dest", destination)
    assert extracted.returncode == 0, extracted.stderr
    assert (destination / "meyo.py").read_bytes() == repository[2]["meyo.py"]
    refused = cli("extract", archive, "--dest", destination)
    assert refused.returncode == 2
    assert refused.stdout == "" and "Traceback" not in refused.stderr


PUBLIC_OPTIONS = {
    "distribution": "public-source",
    "license_id": "LicenseRef-QA-Fixture",
    "license_file": "LICENSE",
    "notice_files": ["THIRD_PARTY_NOTICES.md", "legal/peer-NOTICE.txt"],
}


@pytest.fixture
def licensed_repository(repository):
    """Fictional legal text in a disposable repo, not a license for this product."""
    root, _, sources = repository
    sources = {
        **sources,
        "LICENSE": b"Fictional QA license; conveys no rights to any real product.\n",
        "THIRD_PARTY_NOTICES.md": b"Fictional fixture notices only; no rights certification.\n",
        "legal/peer-NOTICE.txt": b"Fictional peer attribution preserved verbatim.\n",
    }
    for name in PUBLIC_OPTIONS["notice_files"] + ["LICENSE"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(sources[name])
    return root, commit(root), sources


def test_public_format_roundtrip_uses_fixed_commit_legal_bytes(
    package_core, licensed_repository, tmp_path, monkeypatch,
):
    root, revision, sources = licensed_repository
    # Later committed text must not replace the selected commit's legal documents.
    (root / "LICENSE").write_text("Later fictional terms, not selected.\n")
    commit(root)
    first, second = tmp_path / "public.tar", tmp_path / "public-again.tar"
    made = package_core.build_package(root, revision, first, **PUBLIC_OPTIONS)
    package_core.build_package(
        root, revision, second,
        **{**PUBLIC_OPTIONS, "notice_files": list(reversed(PUBLIC_OPTIONS["notice_files"]))},
    )
    assert first.read_bytes() == second.read_bytes()
    manifest = package_core.verify_package(first)
    assert manifest["format"] == "meyo-public-core-v1"
    assert manifest["source_commit"] == revision
    assert manifest["distribution"] == "public-source"
    assert manifest["rights_verified"] is False
    assert manifest["member_execution_verified"] is False
    assert "public_license_granted" not in manifest
    assert manifest["license"] == {
        "identifier": "LicenseRef-QA-Fixture", "file": "LICENSE",
        "third_party_notices": ["THIRD_PARTY_NOTICES.md", "legal/peer-NOTICE.txt"],
    }
    assert {entry["path"] for entry in manifest["files"]} == set(sources)
    for path in ["LICENSE", *PUBLIC_OPTIONS["notice_files"]]:
        record = next(entry for entry in manifest["files"] if entry["path"] == path)
        assert record["sha256"] == hashlib.sha256(sources[path]).hexdigest()
    for key in ("format", "distribution", "license", "rights_verified", "member_execution_verified"):
        assert made[key] == manifest[key]
    assert "public_license_granted" not in made
    root.rename(tmp_path / "old-repo")
    monkeypatch.setenv("PATH", "/no/git/or/runner")
    destination = tmp_path / "public-source"
    assert package_core.extract_package(first, destination) == manifest
    for path, content in sources.items():
        assert (destination / path).read_bytes() == content


@pytest.mark.parametrize("change", [
    {"license_id": None}, {"license_id": ""}, {"license_id": "NOASSERTION"},
    {"license_id": "not\nan identifier"}, {"license_file": None},
    {"license_file": "missing-LICENSE"}, {"license_file": "../LICENSE"},
    {"notice_files": []}, {"notice_files": ["missing-NOTICE"]},
    {"notice_files": ["LICENSE"]}, {"notice_files": ["THIRD_PARTY_NOTICES.md"] * 2},
    {"distribution": "unknown"}, {"distribution": "private-local-only"},
])
def test_public_format_requires_explicit_bundled_legal_metadata(
    package_core, licensed_repository, tmp_path, change,
):
    target = tmp_path / "refused.tar"
    with pytest.raises(package_core.PackageError):
        package_core.build_package(
            *licensed_repository[:2], target, **{**PUBLIC_OPTIONS, **change},
        )
    assert not target.exists() and not Path(str(target) + ".sha256").exists()


def test_public_format_cannot_borrow_license_from_later_commit(
    package_core, repository, licensed_repository, tmp_path,
):
    # The checkout has legal files, but the selected earlier commit does not.
    target = tmp_path / "unlicensed-commit.tar"
    with pytest.raises(package_core.PackageError, match="license|notice"):
        package_core.build_package(
            repository[0], repository[1], target, **PUBLIC_OPTIONS,
        )
    assert not target.exists()


@pytest.mark.parametrize("path", ["LICENSE", "THIRD_PARTY_NOTICES.md"])
def test_public_format_rejects_empty_license_or_notice(
    package_core, licensed_repository, tmp_path, path,
):
    root = licensed_repository[0]
    (root / path).write_bytes(b"")
    revision = commit(root)
    with pytest.raises(package_core.PackageError, match="license|notice"):
        package_core.build_package(root, revision, tmp_path / "empty.tar", **PUBLIC_OPTIONS)
    assert not (tmp_path / "empty.tar").exists()


@pytest.mark.parametrize("damage", [
    "license-bytes", "missing-notice", "missing-license-reference", "missing-notice-reference",
    "empty-identifier", "rights-claim", "legacy-grant-claim", "extra-license-field",
    "duplicate-notice", "unsorted-notices", "old-format",
])
def test_public_format_rejects_tampering_before_extraction(
    package_core, licensed_repository, tmp_path, damage,
):
    original = tmp_path / "public.tar"
    package_core.build_package(*licensed_repository[:2], original, **PUBLIC_OPTIONS)

    def change(items):
        if damage in {"license-bytes", "missing-notice"}:
            name = "LICENSE" if damage == "license-bytes" else "THIRD_PARTY_NOTICES.md"
            index = next(i for i, (item, _) in enumerate(items) if item.name == name)
            if damage == "missing-notice":
                del items[index]
            else:
                item, content = items[index]
                items[index] = item, bytes([content[0] ^ 1]) + content[1:]
            return
        index = next(i for i, (item, _) in enumerate(items) if item.name == MANIFEST)
        item, content = items[index]
        manifest = json.loads(content)
        if damage == "missing-license-reference":
            manifest["license"]["file"] = "not-in-commit"
        elif damage == "missing-notice-reference":
            manifest["license"]["third_party_notices"] = ["not-in-commit"]
        elif damage == "empty-identifier":
            manifest["license"]["identifier"] = ""
        elif damage == "rights-claim":
            manifest["rights_verified"] = True
        elif damage == "legacy-grant-claim":
            manifest["public_license_granted"] = True
        elif damage == "extra-license-field":
            manifest["license"]["ownership_certified"] = True
        elif damage == "duplicate-notice":
            manifest["license"]["third_party_notices"] *= 2
        elif damage == "unsorted-notices":
            manifest["license"]["third_party_notices"].reverse()
        else:
            manifest["format"] = "meyo-private-core-v1"
        content = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        item.size = len(content)
        items[index] = item, content

    bad = rewrite_archive(original, tmp_path / "tampered-public.tar", change)
    with pytest.raises(package_core.PackageError):
        package_core.verify_package(bad)
    with pytest.raises(package_core.PackageError):
        package_core.extract_package(bad, tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


def test_public_format_cannot_be_asserted_by_relabeling_private_v1(
    package_core, licensed_repository, tmp_path,
):
    original = build(package_core, licensed_repository, tmp_path)

    def change(items):
        item, content = items[0]
        manifest = json.loads(content)
        manifest["distribution"] = "public-source"
        manifest["public_license_granted"] = True
        content = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        item.size = len(content)
        items[0] = item, content

    bad = rewrite_archive(original, tmp_path / "relabeled.tar", change)
    with pytest.raises(package_core.PackageError):
        package_core.verify_package(bad)


def test_default_still_matches_legacy_v1_bytes_even_with_license_files(
    package_core, licensed_repository, tmp_path,
):
    """Independent old-v1 fixture locks compatibility, not the new builder's helpers."""
    root, revision, sources = licensed_repository
    records = [{
        "path": name, "mode": "100755" if name == "deploy/linux/start.sh" else "100644",
        "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
        "git_blob": hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest(),
    } for name, content in sorted(sources.items())]
    manifest = {
        "format": "meyo-private-core-v1", "source_commit": revision,
        "distribution": "private-local-only", "public_license_granted": False,
        "member_execution_verified": False, "files": records,
    }
    old = tmp_path / "old-private-v1.tar"
    encoded = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with tarfile.open(old, "w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content, mode in [(MANIFEST, encoded, 0o644)] + [
            (entry["path"], sources[entry["path"]], int(entry["mode"][-3:], 8)) for entry in records
        ]:
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(content), mode
            archive.addfile(item, io.BytesIO(content))
    Path(str(old) + ".sha256").write_text(checksum(old) + "\n")
    new = tmp_path / "default-private.tar"
    summary = package_core.build_package(root, revision, new)
    assert new.read_bytes() == old.read_bytes()
    assert set(summary) == {
        "package", "sha256_file", "sha256", "source_commit", "files",
        "distribution", "public_license_granted", "member_execution_verified",
    }
    assert package_core.verify_package(old) == manifest
    assert package_core.extract_package(old, tmp_path / "old-unpacked") == manifest
    assert (tmp_path / "old-unpacked/LICENSE").read_bytes() == sources["LICENSE"]


def test_public_format_cli_reports_declaration_not_rights_certification(
    licensed_repository, tmp_path,
):
    target = tmp_path / "public-cli.tar"

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), *map(str, args)],
            capture_output=True, text=True, timeout=30,
        )

    args = ["build", "--repo", licensed_repository[0], "--commit", licensed_repository[1],
            "--output", target, "--distribution", "public-source",
            "--license-id", "LicenseRef-QA-Fixture", "--license-file", "LICENSE",
            "--notice-file", "THIRD_PARTY_NOTICES.md", "--notice-file", "legal/peer-NOTICE.txt"]
    made = cli(*args)
    assert made.returncode == 0, made.stderr
    verified = cli("verify", target)
    extracted = cli("extract", target, "--dest", tmp_path / "cli-public-source")
    for result in (made, verified, extracted):
        assert result.returncode == 0, result.stderr
        facts = json.loads(result.stdout)
        assert facts["format"] == "meyo-public-core-v1"
        assert facts["distribution"] == "public-source" and facts["rights_verified"] is False
        assert facts["license"]["identifier"] == "LicenseRef-QA-Fixture"
        assert "public_license_granted" not in facts
    refused = cli("build", "--repo", licensed_repository[0], "--commit", licensed_repository[1],
                  "--output", tmp_path / "missing-options.tar", "--distribution", "public-source")
    assert refused.returncode == 2 and refused.stdout == "" and "Traceback" not in refused.stderr
    assert not (tmp_path / "missing-options.tar").exists()
