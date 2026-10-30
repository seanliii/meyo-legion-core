#!/usr/bin/env python3
"""Fixed official macOS bundle worker for harness_onboard -> registry.install.

Only installs/verifies files; never builds sources, runs the app or tests a model.
The unmodified bundle (including its plugins/licenses/signature) stays private.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib import request
from urllib.parse import urlsplit
import zipfile


VERSION = "0.0.0-beta.4.7"
SOURCE_SHA = "29bd5d4747064cf30432c09d64510e00629bced5"
ASSET_NAME = "OhMyGame-0.0.0-beta.4.7-arm64-mac.zip"
ASSET_URL = ("https://github.com/WhiteTowerAI/ohmygame/releases/download/"
             "v0.0.0-beta.4.7/" + ASSET_NAME)
ASSET_SIZE = 285462808
ASSET_SHA256 = "7f057310747ab821ef353b7a857926a8f83d5ece3993c9516f6a6f18223531e9"
APP_NAME = "OhMyGame.app"
RECEIPT = ".ohmygame-install.json"
CACHE = ".ohmygame-cache"
LOCK_PATH = Path("/tmp/.legion-install.lock")
LOCK_TIMEOUT = 240


class InstallError(RuntimeError):
    """A public error code, never raw subprocess output or a signed download URL."""


def _real(path, *, directory=False):
    try:
        info = path.lstat()
    except OSError:
        raise InstallError("missing_or_unsafe_path") from None
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise InstallError("missing_or_unsafe_path")
    if not directory and info.st_nlink != 1:
        raise InstallError("unsafe_hardlink")
    return info


def _runtime(value):
    path = Path(value).absolute()
    if path.is_symlink():
        raise InstallError("runtime_is_symlink")
    return path.resolve()


def _private_cache(runtime, *, create=False):
    path = runtime / CACHE
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = _real(path, directory=True)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise InstallError("cache_not_private")
    return path


def _sha(path):
    _real(path)
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _verify_archive(path):
    if _real(path).st_size != ASSET_SIZE or _sha(path) != ASSET_SHA256:
        raise InstallError("archive_integrity_mismatch")


def _official_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.fragment):
        return False
    return url == ASSET_URL or (
        parsed.hostname == "release-assets.githubusercontent.com"
        and parsed.path.startswith("/github-production-release-asset/"))


class _OfficialRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _official_url(newurl):
            raise InstallError("download_redirect_rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(cache):
    archive = cache / ASSET_NAME
    if os.path.lexists(archive):
        _verify_archive(archive)
        return archive
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache, prefix=".ohmygame-",
                                         suffix=".partial", delete=False) as stream:
            temporary = Path(stream.name)
            opener = request.build_opener(_OfficialRedirect())
            req = request.Request(ASSET_URL, headers={"Accept-Encoding": "identity",
                                                      "User-Agent": "meyo-ohmygame-installer/1"})
            digest, size = hashlib.sha256(), 0
            deadline = time.monotonic() + 900
            with opener.open(req, timeout=60) as response:
                if response.status != 200 or not _official_url(response.geturl()):
                    raise InstallError("download_response_rejected")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != ASSET_SIZE:
                    raise InstallError("archive_length_mismatch")
                while chunk := response.read(min(1024 * 1024, ASSET_SIZE - size + 1)):
                    size += len(chunk)
                    if size > ASSET_SIZE or time.monotonic() > deadline:
                        raise InstallError("download_limit_exceeded")
                    digest.update(chunk)
                    stream.write(chunk)
            if size != ASSET_SIZE or digest.hexdigest() != ASSET_SHA256:
                raise InstallError("archive_integrity_mismatch")
            stream.flush()
            os.fsync(stream.fileno())
        # Exclusive publication: never replace even a bad pre-existing cache file.
        os.link(temporary, archive)
        return archive
    except InstallError:
        raise
    except Exception:
        raise InstallError("download_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _canonical(name):
    return unicodedata.normalize("NFC", name).casefold()


def _validate_zip_extra(extra):
    while extra:
        if len(extra) < 4:
            raise InstallError("invalid_zip_extra")
        field, length = struct.unpack_from("<HH", extra)
        # Unicode Path / ASi Unix / PKWARE Unix link payloads can give ditto
        # another name or link target that zipfile's filename does not describe.
        if (len(extra) < length + 4 or field in (0x7075, 0x756E)
                or (field == 0x000D and length > 12)):
            raise InstallError("zip_alternate_path_metadata")
        extra = extra[length + 4:]


def _validate_zip(path):
    """Inspect names/local headers/link graph before ditto sees any archive bytes."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries, spellings, links = {}, {}, {}
            for entry in archive.infolist():
                name = entry.filename.removesuffix("/")
                parts = name.split("/")
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (entry.orig_filename != entry.filename or not name
                        or any(part in ("", ".", "..") for part in parts)
                        or any(char in name for char in ("\\", ":", "\x00"))
                        or kind not in (0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK)
                        or (entry.is_dir() and kind not in (0, stat.S_IFDIR))
                        or entry.flag_bits & 1
                        or _canonical(parts[0]) == _canonical(RECEIPT)):
                    raise InstallError("unsafe_zip_entry")
                _validate_zip_extra(entry.extra)
                archive.fp.seek(entry.header_offset + 26)
                name_size, extra_size = struct.unpack("<HH", archive.fp.read(4))
                archive.fp.seek(name_size, os.SEEK_CUR)
                local_extra = archive.fp.read(extra_size)
                if len(local_extra) != extra_size:
                    raise InstallError("invalid_zip_extra")
                _validate_zip_extra(local_extra)
                kind = stat.S_IFDIR if entry.is_dir() else kind or stat.S_IFREG
                key = _canonical(name)
                if key in entries:
                    raise InstallError("duplicate_zip_entry")
                entries[key] = kind
                for index in range(1, len(parts) + 1):
                    prefix = "/".join(parts[:index])
                    canonical = _canonical(prefix)
                    if spellings.setdefault(canonical, prefix) != prefix:
                        raise InstallError("aliased_zip_path")
                # open() also checks the local-header filename against the central
                # directory: validating only namelist() would miss this discrepancy.
                with archive.open(entry) as content:
                    if kind == stat.S_IFLNK:
                        target = content.read(4097).decode("utf-8")
                        if (not target or len(target.encode()) > 4096 or target.startswith("/")
                                or any(char in target for char in ("\\", ":", "\x00"))):
                            raise InstallError("unsafe_zip_link")
                        links[key] = target.split("/")
            for name in spellings.values():
                parts = name.split("/")
                for index in range(1, len(parts)):
                    parent = _canonical("/".join(parts[:index]))
                    if entries.get(parent, stat.S_IFDIR) != stat.S_IFDIR:
                        raise InstallError("zip_entry_below_file_or_link")

            def resolve(parts, chain=()):
                resolved = []
                for part in parts:
                    if part in ("", "."):
                        continue
                    if part == "..":
                        if not resolved:
                            raise InstallError("zip_link_escapes")
                        resolved.pop()
                        continue
                    resolved.append(part)
                    key = _canonical("/".join(resolved))
                    if key in links:
                        if key in chain or len(chain) >= 40:
                            raise InstallError("zip_link_cycle")
                        resolved = resolve(resolved[:-1] + links[key], chain + (key,))
                return resolved

            for name in links:
                target = resolve(spellings[name].split("/"))
                if target and _canonical("/".join(target)) not in spellings:
                    raise InstallError("zip_link_missing_target")
    except InstallError:
        raise
    except (OSError, ValueError, RuntimeError, struct.error, zipfile.BadZipFile):
        raise InstallError("invalid_zip") from None


def _run(command, timeout):
    # Resolve the existing registry, including when invoked as a standalone file.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    try:
        import legion_proc_registry as registry
    finally:
        sys.path.pop(0)
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    registered = finished = False
    try:
        registered = registry.register(proc.pid, pgid=proc.pid, owner=os.getpid(),
                                       meta={"kind": "ohmygame-install"})
        if not registered:
            raise InstallError("process_registration_failed")
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise InstallError("command_timeout") from None
        finished = True
        if proc.returncode:
            raise InstallError("command_failed")
        return stdout + stderr
    finally:
        if not finished:
            # Also reclaim children retaining pipes after the group leader exited.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate(timeout=5)
        if registered:
            registry.unregister(proc.pid)


@contextmanager
def _install_lock():
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise InstallError("unsafe_install_lock")
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise InstallError("install_lock_timeout") from None
                time.sleep(0.05)
        yield
    finally:
        # Closing releases our lock. Never unlink a shared lock inode.
        os.close(fd)


def _tree_digest(root):
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            path = Path(directory) / name
            if path == root / RECEIPT:
                continue
            info = path.lstat()
            row = [path.relative_to(root).as_posix(), stat.S_IMODE(info.st_mode)]
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                try:
                    contained = not os.path.isabs(target) and path.resolve(strict=True).is_relative_to(root)
                except (OSError, RuntimeError):
                    contained = False
                if not contained:
                    raise InstallError("installed_link_escapes_or_is_broken")
                row += ["link", target]
            elif stat.S_ISREG(info.st_mode):
                row += ["file", _sha(path)]
            elif stat.S_ISDIR(info.st_mode):
                row += ["directory"]
            else:
                raise InstallError("unsupported_installed_entry")
            digest.update(json.dumps(row, ensure_ascii=True).encode() + b"\n")
    return digest.hexdigest()


def _app_facts(root):
    app = root / APP_NAME
    for path in (root, app, app / "Contents", app / "Contents/MacOS"):
        _real(path, directory=True)
    info_path = app / "Contents/Info.plist"
    _real(info_path)
    try:
        info = plistlib.loads(info_path.read_bytes())
    except (ValueError, plistlib.InvalidFileException):
        raise InstallError("invalid_app_plist") from None
    if not isinstance(info, dict) or info.get("CFBundleShortVersionString") != VERSION:
        raise InstallError("app_version_mismatch")
    executable = info.get("CFBundleExecutable")
    if (not isinstance(executable, str) or executable in ("", ".", "..")
            or any(char in executable for char in ("/", "\\", ":", "\x00"))):
        raise InstallError("invalid_app_executable")
    if not _real(app / "Contents/MacOS" / executable).st_mode & 0o111:
        raise InstallError("app_executable_not_executable")
    tree = _tree_digest(root)
    _run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)], 120)
    details = _run(["/usr/bin/codesign", "--display", "--verbose=4", str(app)], 30)
    signature = dict(line.split("=", 1) for line in details.splitlines()
                     if line.startswith(("Identifier=", "CDHash=", "TeamIdentifier=")))
    cdhash = signature.get("CDHash", "")
    if (len(cdhash) != 40 or any(char not in "0123456789abcdef" for char in cdhash)
            or not signature.get("Identifier")
            or signature["Identifier"] != info.get("CFBundleIdentifier")):
        raise InstallError("app_signature_identity_missing")
    return {"tree_sha256": tree, "signature": signature}


def _identity():
    return {"installer": "ohmygame_install", "schema": 1, "version": VERSION,
            "source_sha": SOURCE_SHA, "archive_url": ASSET_URL,
            "archive_size": ASSET_SIZE, "archive_sha256": ASSET_SHA256}


def _public(runtime, installed=True):
    return {"installed": installed, "app_path": str(runtime / "app" / APP_NAME),
            "version": VERSION, "source_sha": SOURCE_SHA}


def check(runtime_dir):
    """Offline verification only; codesign is inspected, the application is not run."""
    runtime = _runtime(runtime_dir)
    _real(runtime, directory=True)
    target = runtime / "app"
    _real(target, directory=True)
    receipt = target / RECEIPT
    if _real(receipt).st_size > 32768:
        raise InstallError("invalid_install_receipt")
    try:
        record = json.loads(receipt.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise InstallError("invalid_install_receipt") from None
    if not isinstance(record, dict) or any(record.get(key) != value for key, value in _identity().items()):
        raise InstallError("unverified_existing_install")
    _verify_archive(_private_cache(runtime) / ASSET_NAME)
    facts = _app_facts(target)
    if any(record.get(key) != value for key, value in facts.items()):
        raise InstallError("existing_install_changed")
    return _public(runtime)


def _publish(source, destination):
    # macOS renamex_np(RENAME_EXCL): ordinary os.rename can overwrite an empty
    # directory created by another actor after an existence check.
    rename = ctypes.CDLL(None, use_errno=True).renamex_np
    rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(os.fsencode(source), os.fsencode(destination), 0x00000004):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def install(runtime_dir):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise InstallError("requires_macos_arm64")
    runtime = _runtime(runtime_dir)
    with _install_lock():
        if os.path.lexists(runtime / "app"):
            return check(runtime)  # No download, deletion or repair of an unknown target.
        runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
        _real(runtime, directory=True)
        archive = _download(_private_cache(runtime, create=True))
        _validate_zip(archive)
        with tempfile.TemporaryDirectory(prefix=".ohmygame-stage-", dir=runtime) as staging:
            payload = Path(staging) / "app"
            payload.mkdir(mode=0o700)
            _run(["/usr/bin/ditto", "-x", "-k", str(archive), str(payload)], 240)
            record = {**_identity(), **_app_facts(payload)}
            # Outside the signed .app. Keep all upstream files and metadata as-is.
            fd = os.open(payload / RECEIPT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            _publish(payload, runtime / "app")
        return _public(runtime)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="verify existing files; no network/app launch")
    args = parser.parse_args(argv)
    try:
        result = check(args.runtime_dir) if args.check else install(args.runtime_dir)
    except Exception as error:
        result = _public(args.runtime_dir.absolute(), installed=False)
        result["error"] = str(error) if isinstance(error, InstallError) else "installation_failed"
        print(json.dumps(result, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
