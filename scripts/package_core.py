#!/usr/bin/env python3
"""Offline, fixed-commit source distribution; no installer or member runner.

Python 3.11+ on macOS/Linux. Git is needed only for build. The uncompressed
USTAR format, manifest, modes and ordering are deliberately canonical.
"""

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unicodedata


MANIFEST = "CORE-PACKAGE.json"
FORMAT = "meyo-private-core-v1"
PUBLIC_FORMAT = "meyo-public-core-v1"
CHUNK_BYTES = 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + MAX_METADATA_BYTES + 64 * 1024 * 1024
MAX_FILES = 20_000
GIT_TIMEOUT_SECONDS = 60

# Path policy, NOT a substring scan for "token", "state", "runtime" or "model".
# Source modules and schema files in qa_system/state remain ordinary source.
_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "virtualenv", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".meyo", ".legion", ".qa", ".ssh", ".aws", ".gnupg", "secrets",
    "snapshot_failures",
}
_CREDENTIAL_FILES = {
    ".netrc", ".npmrc", ".pypirc", "auth.json", "credentials", "credentials.json",
    "credentials.yaml", "credentials.yml", "secrets.json", "secret.json",
    "tokens.json", "token.json", "token.txt", "login-token.txt",
    "personal-tokens.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
_RUNTIME_FILES = {
    "model_health.json", "session_index.json", "process_registry.json",
    "run_history.json", "sessions.jsonl",
}
_DATA_DIRS = {"state", "sessions", "logs", "history", "results", "traces", "checkpoints", "evidence"}
_DATA_SUFFIXES = {".json", ".jsonl", ".log", ".txt", ".csv", ".tsv", ".yaml", ".yml"}
_BINARY_DATA_SUFFIXES = {
    ".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".db-wal", ".db-shm",
    ".pkl", ".pickle", ".safetensors", ".gguf", ".ggml", ".ckpt", ".pt", ".pth",
}
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(1, 10)
}


class PackageError(ValueError):
    """Rejected input or failed local I/O; never a partial verification success."""


@contextmanager
def _errors():
    try:
        yield
    except PackageError:
        raise
    except (OSError, ValueError, tarfile.TarError, UnicodeError) as error:
        raise PackageError(str(error)) from error


def _path_parts(name):
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 255:
        raise PackageError("invalid or overlong source path")
    parts = name.split("/")
    if unicodedata.normalize("NFC", name) != name or any(
        part in ("", ".", "..") or part.endswith((" ", "."))
        or part.split(".")[0].casefold() in _WINDOWS_RESERVED
        or any(c in '\\:<>"|?*' or unicodedata.category(c).startswith("C") for c in part)
        for part in parts
    ):
        raise PackageError(f"unsafe or non-portable path: {name!r}")
    # Fail explicitly rather than introducing PAX extensions or truncating names.
    try:
        tarfile.TarInfo(name).tobuf(format=tarfile.USTAR_FORMAT, encoding="utf-8", errors="strict")
    except (ValueError, UnicodeError) as error:
        raise PackageError(f"path cannot be represented in USTAR: {name!r}") from error
    return parts


def validate_source_path(name):
    """Reject known data locations before reading blobs; do not guess at secrets."""
    parts = _path_parts(name)
    folded = [part.casefold() for part in parts]
    leaf = folded[-1]
    suffix = Path(leaf).suffix
    env_file = leaf == ".env" or (
        leaf.startswith(".env.") and leaf not in {".env.example", ".env.sample", ".env.template"}
    )
    data_location = any(
        part in _DATA_DIRS
        and not (part == "state" and i > 0 and folded[i - 1] == "qa_system")
        for i, part in enumerate(folded[:-1])
    )
    forbidden = (
        any(part in _EXCLUDED_DIRS for part in folded)
        or leaf in _CREDENTIAL_FILES or leaf in _RUNTIME_FILES or env_file
        or suffix in {".pem", ".key", ".p12", ".pfx", ".keystore"}
        or suffix in _BINARY_DATA_SUFFIXES
        or (suffix in {".bin", ".onnx", ".h5"} and any(p in {"models", "weights", "checkpoints"} for p in folded[:-1]))
        or (data_location and suffix in _DATA_SUFFIXES)
    )
    if forbidden:
        raise PackageError(f"runtime/credential/dependency data is not source: {name!r}")
    if folded[0] == MANIFEST.casefold():
        raise PackageError(f"reserved package manifest path: {name!r}")
    return parts


def _validate_path_set(names):
    aliases = {}
    files = set(names)
    for name in names:
        parts = validate_source_path(name)
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            key = prefix.casefold()
            if key in aliases and aliases[key] != prefix:
                raise PackageError("case-folding path collision")
            aliases[key] = prefix
            if i < len(parts) and prefix in files:
                raise PackageError("file/directory path collision")


def _algorithm(commit):
    if not isinstance(commit, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit):
        raise PackageError("commit must be a full lowercase 40/64-character object ID, not HEAD/a branch")
    return "sha1" if len(commit) == 40 else "sha256"


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _git(repo, arguments, output=None, limit=MAX_METADATA_BYTES):
    """Bound stdout, time and child lifetime; no inherited credentials or network."""
    env = {
        "PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    command = [
        "git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "core.attributesFile=/dev/null", "-c", "protocol.allow=never",
        "-C", str(repo), *arguments,
    ]
    buffer = output if output is not None else io.BytesIO()
    with subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, env=env, start_new_session=True,
    ) as process:
        timed_out = threading.Event()

        def stop():
            if process.poll() is None:
                timed_out.set()
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        timer = threading.Timer(GIT_TIMEOUT_SECONDS, stop)
        timer.daemon = True
        timer.start()
        try:
            total = 0
            while True:
                chunk = process.stdout.read(min(CHUNK_BYTES, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise PackageError("git output exceeds bounded size")
                buffer.write(chunk)
            process.communicate(timeout=GIT_TIMEOUT_SECONDS)
            if timed_out.is_set() or process.returncode:
                raise PackageError(
                    f"local git {arguments[0]} failed (exit {process.returncode}); "
                    "required objects must already be available offline"
                )
        except subprocess.TimeoutExpired as error:
            raise PackageError("local git exceeded its time bound") from error
        finally:
            timer.cancel()
            timer.join()
            if process.poll() is None:
                stop()
                process.communicate(timeout=5)
    return buffer.getvalue() if output is None else None


def _read_tree(repo, commit):
    algorithm = _algorithm(commit)
    resolved = _git(repo, ["rev-parse", "--verify", f"{commit}^{{commit}}"]).decode().strip()
    if resolved != commit:
        raise PackageError("source object is not the specified commit")
    result = {}
    total = 0
    raw = _git(repo, ["ls-tree", "-r", "-l", "-z", "--full-tree", commit])
    for line in raw.split(b"\0"):
        if not line:
            continue
        metadata, encoded_name = line.split(b"\t", 1)
        mode, kind, oid, size = metadata.decode("ascii").split()
        name = encoded_name.decode("utf-8")
        validate_source_path(name)
        if mode not in {"100644", "100755"} or kind != "blob":
            raise PackageError(f"symlink/gitlink/non-regular source entry: {name!r}")
        size = int(size)
        total += size
        if size < 0 or size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES or len(result) >= MAX_FILES:
            raise PackageError("source exceeds file/count/total size bounds")
        if name in result or len(oid) != hashlib.new(algorithm).digest_size * 2:
            raise PackageError("invalid or repeated git tree entry")
        result[name] = {"path": name, "mode": mode, "git_blob": oid, "size": size}
    if not result:
        raise PackageError("empty source commit")
    _validate_path_set(result)
    return result


def _digest_file(stream, size, algorithm):
    sha = hashlib.sha256()
    blob = hashlib.new(algorithm)
    blob.update(f"blob {size}\0".encode("ascii"))
    remaining = size
    while remaining:
        chunk = stream.read(min(CHUNK_BYTES, remaining))
        if not chunk:
            raise PackageError("truncated file content")
        remaining -= len(chunk)
        sha.update(chunk)
        blob.update(chunk)
    return sha.hexdigest(), blob.hexdigest()


def _open_relative(root_fd, name, *, create=False):
    """Walk via directory descriptors, never through a source/archive symlink."""
    parts = _path_parts(name)
    directory = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY
        fd = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise PackageError("not a regular source file")
        return fd
    finally:
        os.close(directory)


def _assert_clean(repo, head, entries):
    # Do not use working-tree filters/status hooks: inspect index + raw source bytes.
    # This also catches dirty files hidden by assume-unchanged / skip-worktree flags.
    if _git(repo, ["rev-parse", "--verify", "HEAD"]).decode().strip() != head:
        raise PackageError("dirty or changed checkout: HEAD moved during build")
    index = {}
    for line in _git(repo, ["ls-files", "--stage", "-z"]).split(b"\0"):
        if line:
            metadata, name = line.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            index[name.decode("utf-8")] = (mode, oid, stage)
    expected = {name: (entry["mode"], entry["git_blob"], "0") for name, entry in entries.items()}
    if index != expected or _git(repo, ["ls-files", "--others", "--exclude-standard", "-z"]):
        raise PackageError("dirty tree: staged or untracked files; ignored local data is never packaged")
    root_fd = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name, entry in entries.items():
            try:
                with os.fdopen(_open_relative(root_fd, name), "rb") as source:
                    metadata = os.fstat(source.fileno())
                    mode = "100755" if metadata.st_mode & stat.S_IXUSR else "100644"
                    if metadata.st_size != entry["size"] or mode != entry["mode"]:
                        raise PackageError("dirty tree: source size/mode changed")
                    _, oid = _digest_file(source, metadata.st_size, _algorithm(head))
                    if oid != entry["git_blob"]:
                        raise PackageError("dirty tree: source bytes changed")
            except OSError as error:
                raise PackageError("dirty tree: missing or linked working-tree source") from error
    finally:
        os.close(root_fd)


def _archive_observations(stream, entries, algorithm):
    seen = {}
    offsets = {}
    with tarfile.open(fileobj=stream, mode="r:", encoding="utf-8", errors="strict") as archive:
        count = 0
        for member in archive:
            count += 1
            if count > MAX_FILES * 4:
                raise PackageError("git archive has too many entries")
            if member.isdir():
                _path_parts(member.name.rstrip("/"))
                continue
            if not member.isreg() or member.name not in entries or member.name in seen:
                raise PackageError("git archive file set/type mismatch")
            entry = entries[member.name]
            if member.size != entry["size"]:
                raise PackageError("git archive changed blob size (export-subst is unsupported)")
            with archive.extractfile(member) as content:
                digest, oid = _digest_file(content, member.size, algorithm)
            if oid != entry["git_blob"]:
                raise PackageError("git archive changed source blob (export-subst is unsupported)")
            seen[member.name] = {**entry, "sha256": digest}
            offsets[member.name] = member.offset_data
    if set(seen) != set(entries):
        raise PackageError("git archive file set is incomplete (export-ignore is unsupported)")
    return [seen[name] for name in sorted(seen)], offsets


def _member(name, size, mode=0o644):
    member = tarfile.TarInfo(name)
    member.size = size
    member.mode = mode
    # TarInfo defaults: uid/gid/mtime=0, names="", regular file, no link or PAX.
    return member


def _absolute(path):
    path = Path(path)
    if ".." in path.parts:
        raise PackageError("destination must not contain parent traversal")
    return Path(os.path.abspath(path))


def _has_source_marker(fd):
    for marker in (".git", MANIFEST, "meyo.py"):
        try:
            os.stat(marker, dir_fd=fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            pass
    return False


@contextmanager
def _destination_parent(path, *, forbid_source=False):
    """Parent must already exist; a fresh leaf may not be embedded in old source."""
    path = _absolute(path)
    if path == Path(path.anchor):
        raise PackageError("destination cannot be a filesystem root")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parent.parts[1:]:
            if forbid_source and _has_source_marker(fd):
                raise PackageError("refusing to embed source inside an existing checkout/package")
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
        if forbid_source and _has_source_marker(fd):
            raise PackageError("refusing to embed source inside an existing checkout/package")
        yield path, fd
    finally:
        os.close(fd)


def _license_metadata(identifier, license_file, notice_files):
    """Validate an explicit declaration, not ownership or the legal text's meaning."""
    if (not isinstance(identifier, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,127}", identifier)
            or identifier.upper() in {"NONE", "NOASSERTION"}):
        raise PackageError("public-source requires an explicit license identifier (1-128 characters)")
    validate_source_path(license_file)
    if not isinstance(notice_files, list) or not 0 < len(notice_files) <= MAX_FILES:
        raise PackageError("public-source requires a nonempty list of bundled notice files")
    for name in notice_files:
        validate_source_path(name)
    if license_file in notice_files or len(set(notice_files)) != len(notice_files):
        raise PackageError("license and notice files must be distinct, without duplicate notices")
    return {"identifier": identifier, "file": license_file,
            "third_party_notices": sorted(notice_files)}


def _validate_license_files(license_metadata, entries):
    for name in [license_metadata["file"], *license_metadata["third_party_notices"]]:
        if name not in entries or entries[name]["size"] == 0:
            raise PackageError(f"license/notice must be a nonempty bundled file: {name!r}")


def _manifest_summary(manifest):
    keys = ("source_commit", "distribution", "member_execution_verified")
    keys += (("public_license_granted",) if manifest["format"] == FORMAT
             else ("format", "license", "rights_verified"))
    return {**{key: manifest[key] for key in keys}, "files": len(manifest["files"])}


def build_package(repo, commit, output, *, distribution="private-local-only",
                  license_id=None, license_file=None, notice_files=None):
    """Build every committed source file, or reject; no allowlist/working copy overlay."""
    with _errors():
        if distribution == "private-local-only":
            if any(value is not None for value in (license_id, license_file, notice_files)):
                raise PackageError("license options require explicit public-source distribution")
            facts = {"format": FORMAT, "public_license_granted": False}
        elif distribution == "public-source":
            facts = {"format": PUBLIC_FORMAT, "rights_verified": False,
                     "license": _license_metadata(license_id, license_file, notice_files)}
        else:
            raise PackageError("unknown distribution; use private-local-only or public-source")
        _algorithm(commit)
        repo = Path(repo).resolve(strict=True)
        if Path(_git(repo, ["rev-parse", "--show-toplevel"]).decode().strip()).resolve() != repo:
            raise PackageError("--repo must be the Git worktree root")
        head = _git(repo, ["rev-parse", "--verify", "HEAD"]).decode().strip()
        head_entries = _read_tree(repo, head)
        entries = head_entries if head == commit else _read_tree(repo, commit)
        if distribution == "public-source":
            _validate_license_files(facts["license"], entries)
        _assert_clean(repo, head, head_entries)
        with _destination_parent(output) as (output, parent_fd):
            if output.is_relative_to(repo):
                raise PackageError("package output must be outside the source checkout")
            sidecar = output.with_name(output.name + ".sha256")
            if os.path.lexists(output) or os.path.lexists(sidecar):
                raise PackageError("refusing to overwrite an existing package/hash")
            with tempfile.TemporaryDirectory(prefix=".core-package-", dir=output.parent) as temporary:
                raw = Path(temporary) / "git.tar"
                with raw.open("w+b") as stream:
                    _git(repo, ["archive", "--format=tar", commit], output=stream, limit=MAX_ARCHIVE_BYTES)
                    stream.seek(0)
                    files, offsets = _archive_observations(stream, entries, _algorithm(commit))
                    manifest = {
                        **facts, "source_commit": commit, "distribution": distribution,
                        "member_execution_verified": False, "files": files,
                    }
                    encoded = _json_bytes(manifest)
                    if len(encoded) > MAX_METADATA_BYTES:
                        raise PackageError("manifest exceeds size bound")
                    packed = Path(temporary) / "package.tar"
                    with packed.open("xb") as target:
                        with tarfile.open(
                            fileobj=target, mode="w", format=tarfile.USTAR_FORMAT,
                            encoding="utf-8", errors="strict",
                        ) as archive:
                            archive.addfile(_member(MANIFEST, len(encoded)), io.BytesIO(encoded))
                            for entry in files:
                                stream.seek(offsets[entry["path"]])
                                archive.addfile(
                                    _member(entry["path"], entry["size"], int(entry["mode"][-3:], 8)),
                                    stream,
                                )
                    packed.chmod(0o600)
                digest = _hash_regular(packed)
                # Use the exact offline verifier before publishing either output.
                verify_package(packed, expected_sha256=digest)
                _assert_clean(repo, head, head_entries)
                hash_path = Path(temporary) / "package.sha256"
                hash_path.write_text(digest + "\n", encoding="ascii")
                hash_path.chmod(0o600)
                os.link(packed, output.name, dst_dir_fd=parent_fd, follow_symlinks=False)
                try:
                    os.link(hash_path, sidecar.name, dst_dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    # The final path was created by this invocation, not pre-existing.
                    if os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False).st_ino == packed.stat().st_ino:
                        os.unlink(output.name, dir_fd=parent_fd)
                    raise
        return {"package": str(output), "sha256_file": str(sidecar), "sha256": digest,
                **_manifest_summary(manifest)}


@contextmanager
def _regular(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise PackageError("input is not a regular file or exceeds size bound")
        stream = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with stream:
        yield stream


def _copy_hash(source, destination=None, limit=MAX_ARCHIVE_BYTES):
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = source.read(min(CHUNK_BYTES, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise PackageError("input exceeds bounded size")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    return digest.hexdigest()


def _hash_regular(path):
    with _regular(path, MAX_ARCHIVE_BYTES) as stream:
        return _copy_hash(stream)


def _expected_hash(path, expected):
    if expected is None:
        with _regular(str(path) + ".sha256", 128) as stream:
            expected = stream.read(129).decode("ascii").strip()
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise PackageError("expected SHA256 must be exactly 64 hexadecimal characters")
    return expected.lower()


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageError("duplicate JSON manifest key")
        result[key] = value
    return result


def _parse_manifest(encoded):
    try:
        manifest = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_json)
    except RecursionError as error:
        raise PackageError("manifest nesting exceeds the JSON parser bound") from error
    if not isinstance(manifest, dict) or manifest.get("format") not in (FORMAT, PUBLIC_FORMAT):
        raise PackageError("unknown manifest format")
    public = manifest["format"] == PUBLIC_FORMAT
    required = {"format", "source_commit", "distribution", "member_execution_verified", "files"}
    required |= {"license", "rights_verified"} if public else {"public_license_granted"}
    if set(manifest) != required:
        raise PackageError("invalid manifest fields")
    if manifest["member_execution_verified"] is not False:
        raise PackageError("package format cannot certify member execution")
    if public:
        if manifest["distribution"] != "public-source" or manifest["rights_verified"] is not False:
            raise PackageError("invalid public-source declaration; rights are not verified")
        license_metadata = manifest["license"]
        if (not isinstance(license_metadata, dict)
                or set(license_metadata) != {"identifier", "file", "third_party_notices"}):
            raise PackageError("invalid license declaration fields")
        canonical = _license_metadata(
            license_metadata["identifier"], license_metadata["file"],
            license_metadata["third_party_notices"],
        )
        if license_metadata != canonical:
            raise PackageError("license notice paths must be unique and sorted")
    elif (manifest["distribution"] != "private-local-only"
          or manifest["public_license_granted"] is not False):
        raise PackageError("invalid private-local-only facts")
    algorithm = _algorithm(manifest["source_commit"])
    files = manifest["files"]
    if not isinstance(files, list) or not 0 < len(files) <= MAX_FILES:
        raise PackageError("invalid manifest file count")
    names = []
    total = 0
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "mode", "size", "sha256", "git_blob"}:
            raise PackageError("invalid manifest file record")
        validate_source_path(entry["path"])
        size = entry["size"]
        if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise PackageError("manifest file exceeds size bound")
        total += size
        if total > MAX_TOTAL_BYTES or entry["mode"] not in ("100644", "100755"):
            raise PackageError("manifest exceeds total size/mode bounds")
        for key, length in (("sha256", 64), ("git_blob", hashlib.new(algorithm).digest_size * 2)):
            if not isinstance(entry[key], str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", entry[key]):
                raise PackageError("invalid manifest digest")
        names.append(entry["path"])
    if names != sorted(set(names)):
        raise PackageError("manifest paths must be unique and sorted")
    _validate_path_set(names)
    if public:
        _validate_license_files(license_metadata, {entry["path"]: entry for entry in files})
    if encoded != _json_bytes(manifest):
        raise PackageError("manifest must use the canonical JSON encoding")
    return manifest


def _inspect(stream):
    """Parse only bounded, canonical regular USTAR records; never tar.extractall."""
    length = stream.seek(0, os.SEEK_END)
    stream.seek(0)
    if length > MAX_ARCHIVE_BYTES or length % tarfile.RECORDSIZE:
        raise PackageError("invalid archive length")
    manifest = None
    expected = {}
    offsets = {}
    while True:
        position = stream.tell()
        header = stream.read(512)
        if len(header) != 512:
            raise PackageError("truncated tar header/footer")
        if header == bytes(512):
            footer_length = 1024 + (-(position + 1024) % tarfile.RECORDSIZE)
            if length - position != footer_length or any(stream.read(footer_length - 512)):
                raise PackageError("noncanonical/truncated footer or appended archive")
            break
        member = tarfile.TarInfo.frombuf(header, encoding="utf-8", errors="strict")
        if (header[156:157] != tarfile.REGTYPE or member.linkname
                or header[257:265] != b"ustar\x0000"
                or member.uid != 0 or member.gid != 0 or member.mtime != 0
                or member.uname or member.gname or member.devmajor or member.devminor
                or member.mode not in (0o644, 0o755)
                or member.tobuf(format=tarfile.USTAR_FORMAT, encoding="utf-8", errors="strict") != header):
            raise PackageError("non-regular/link/noncanonical tar member")
        _path_parts(member.name)
        if member.name == MANIFEST:
            if manifest is not None or offsets or not 0 < member.size <= MAX_METADATA_BYTES or member.mode != 0o644:
                raise PackageError("duplicate, misplaced or oversized manifest")
            encoded = stream.read(member.size)
            if len(encoded) != member.size:
                raise PackageError("truncated manifest")
            manifest = _parse_manifest(encoded)
            expected = {entry["path"]: entry for entry in manifest["files"]}
        else:
            validate_source_path(member.name)
            if manifest is None or member.name not in expected or member.name in offsets:
                raise PackageError("missing manifest, extra file or duplicate tar member")
            entry = expected[member.name]
            if member.size != entry["size"] or member.mode != int(entry["mode"][-3:], 8):
                raise PackageError("file metadata does not match manifest")
            offsets[member.name] = stream.tell()
            digest, oid = _digest_file(stream, member.size, _algorithm(manifest["source_commit"]))
            if digest != entry["sha256"] or oid != entry["git_blob"]:
                raise PackageError("source SHA256/git blob hash mismatch")
        padding_length = -member.size % 512
        padding = stream.read(padding_length)
        if len(padding) != padding_length or any(padding):
            raise PackageError("invalid file padding")
    if manifest is None or set(offsets) != set(expected):
        raise PackageError("missing source files/manifest")
    return manifest, offsets


@contextmanager
def _verified_snapshot(path, expected_sha256):
    expected = _expected_hash(path, expected_sha256)
    # Hash while copying once to a private snapshot: path swaps/in-place writes
    # cannot change the bytes later inspected and extracted.
    with tempfile.TemporaryFile() as snapshot:
        with _regular(path, MAX_ARCHIVE_BYTES) as source:
            digest = _copy_hash(source, snapshot)
        if digest != expected:
            raise PackageError("independent package SHA256 mismatch")
        snapshot.flush()
        manifest, offsets = _inspect(snapshot)
        yield snapshot, manifest, offsets


def verify_package(package, *, expected_sha256=None):
    with _errors(), _verified_snapshot(package, expected_sha256) as (_, manifest, _offsets):
        return manifest


def extract_package(package, destination, *, expected_sha256=None):
    """Validate everything before creating a fresh root; never overlay old source."""
    with _errors(), _verified_snapshot(package, expected_sha256) as (source, manifest, offsets):
        with _destination_parent(destination, forbid_source=True) as (destination, parent_fd):
            os.mkdir(destination.name, mode=0o700, dir_fd=parent_fd)
            root_fd = os.open(destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            original_inode = os.fstat(root_fd).st_ino
            try:
                for entry in manifest["files"]:
                    source.seek(offsets[entry["path"]])
                    with os.fdopen(_open_relative(root_fd, entry["path"], create=True), "w+b") as target:
                        remaining = entry["size"]
                        while remaining:
                            chunk = source.read(min(CHUNK_BYTES, remaining))
                            if not chunk:
                                raise PackageError("snapshot truncated during extraction")
                            target.write(chunk)
                            remaining -= len(chunk)
                        target.flush()
                        target.seek(0)
                        digest, _ = _digest_file(target, entry["size"], _algorithm(manifest["source_commit"]))
                        if digest != entry["sha256"]:
                            raise PackageError("extracted source readback mismatch")
                        os.fchmod(target.fileno(), int(entry["mode"][-3:], 8))
                with os.fdopen(_open_relative(root_fd, MANIFEST, create=True), "wb") as target:
                    target.write(_json_bytes(manifest))
            except BaseException:
                current = os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
                if current.st_ino == original_inode and stat.S_ISDIR(current.st_mode):
                    shutil.rmtree(destination.name, dir_fd=parent_fd)
                raise
            finally:
                os.close(root_fd)
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build", help="从干净工作树中的固定提交构建源码包（默认私有）")
    builder.add_argument("--repo", required=True, type=Path)
    builder.add_argument("--commit", required=True, help="完整的源码 commit SHA，不接受分支名")
    builder.add_argument("--output", required=True, type=Path)
    builder.add_argument("--distribution", choices=("private-local-only", "public-source"),
                         default="private-local-only", help="公开格式仅记录声明，不认证权属或执行发布")
    builder.add_argument("--license-id", help="公开格式必填：显式许可标识，不自动选择或验证法律含义")
    builder.add_argument("--license-file", help="公开格式必填：指定提交中实际随包的许可文件相对路径")
    builder.add_argument("--notice-file", action="append",
                         help="公开格式必填且可重复：随包第三方通知文件相对路径")
    for name in ("verify", "extract"):
        command = commands.add_parser(name, help="离线校验" if name == "verify" else "校验并提取到全新目录")
        command.add_argument("package", type=Path)
        command.add_argument("--sha256", help="可信渠道的预期 SHA256；省略则读同名 .sha256 文件")
        if name == "extract":
            command.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            result = build_package(
                args.repo, args.commit, args.output, distribution=args.distribution,
                license_id=args.license_id, license_file=args.license_file,
                notice_files=args.notice_file,
            )
        else:
            manifest = (
                verify_package(args.package, expected_sha256=args.sha256)
                if args.command == "verify" else
                extract_package(args.package, args.dest, expected_sha256=args.sha256)
            )
            result = _manifest_summary(manifest)
            result["operation"] = args.command
            if args.command == "extract":
                result["destination"] = str(_absolute(args.dest))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except PackageError as error:
        print(f"拒绝：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
