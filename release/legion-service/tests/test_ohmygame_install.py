"""Offline installer tests: real fixture files, mocked HTTP/macOS command boundaries."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import stat
import struct
import subprocess
import sys
from types import SimpleNamespace
import warnings
import zipfile

import pytest


WORKER = Path(__file__).resolve().parents[1] / "external_adapters/ohmygame_install.py"
VERSION = "0.0.0-beta.4.7"
SOURCE_SHA = "29bd5d4747064cf30432c09d64510e00629bced5"
URL = ("https://github.com/WhiteTowerAI/ohmygame/releases/download/"
       "v0.0.0-beta.4.7/OhMyGame-0.0.0-beta.4.7-arm64-mac.zip")
APP = "OhMyGame.app"
REGULAR = stat.S_IFREG | 0o644
LINK = stat.S_IFLNK | 0o777


def forbidden(*args, **kwargs):
    raise AssertionError("offline test attempted network or an unmocked OS command")


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    for variable in ("MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR", "HOME"):
        monkeypatch.setenv(variable, str(tmp_path / variable.lower()))
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.fixture
def load_worker(tmp_path, monkeypatch):
    def load():
        # An assertion rather than a collection/import error makes the initial
        # red run explicit; all subsequent assertions exercise the implementation.
        assert WORKER.is_file(), "the requested installation worker is not implemented"
        spec = importlib.util.spec_from_file_location("_ohmygame_install_test", WORKER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "LOCK_PATH", tmp_path / ".legion-install.lock")
        monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
        return module
    return load


def bundle(extra=()):
    info = {
        "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
        "CFBundleExecutable": "OhMyGame", "CFBundleIdentifier": "test.ohmygame",
        "CFBundlePackageType": "APPL",
    }
    entries = [
        (f"{APP}/Contents/Info.plist", plistlib.dumps(info), REGULAR),
        (f"{APP}/Contents/MacOS/OhMyGame", b"fixture, never executed", stat.S_IFREG | 0o755),
        (f"{APP}/Contents/Resources/app.asar", b"complete fixture application", REGULAR),
        (f"{APP}/Contents/_CodeSignature/CodeResources", b"fixture seal", REGULAR),
        (f"{APP}/Contents/Frameworks/F.framework/Versions/A/F", b"framework", REGULAR),
        (f"{APP}/Contents/Frameworks/F.framework/Versions/Current", b"A", LINK),
        (f"{APP}/Contents/Frameworks/F.framework/F", b"Versions/Current/F", LINK),
        ("LICENSE", b"upstream fixture license\n", REGULAR),
        *extra,
    ]
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # Deliberate duplicate ZIP entries.
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data, mode in entries:
                entry = zipfile.ZipInfo(name)
                entry.create_system = 3
                entry.external_attr = mode << 16
                archive.writestr(entry, data)
    return stream.getvalue()


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, *, length=None, url=URL):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}
        self.url = url

    def geturl(self):
        return self.url


def offline_release(module, monkeypatch, data=None):
    data = bundle() if data is None else data
    monkeypatch.setattr(module, "ASSET_SIZE", len(data))
    monkeypatch.setattr(module, "ASSET_SHA256", hashlib.sha256(data).hexdigest())
    calls = SimpleNamespace(downloads=[], commands=[], response=lambda: Response(data))

    def open_fixture(req, timeout):
        assert req.full_url == URL
        assert timeout > 0
        calls.downloads.append(req.full_url)
        return calls.response()

    monkeypatch.setattr(module.request, "build_opener",
                        lambda *handlers: SimpleNamespace(open=open_fixture))

    def run_fixture(command, timeout):
        calls.commands.append(command)
        assert timeout > 0
        if command[:3] == ["/usr/bin/ditto", "-x", "-k"]:
            assert len(command) == 5
            destination = Path(command[4])
            with zipfile.ZipFile(command[3]) as archive:
                for entry in archive.infolist():
                    path = destination / entry.filename
                    assert path.absolute().is_relative_to(destination.absolute())
                    path.parent.mkdir(parents=True, exist_ok=True)
                    mode = entry.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        path.symlink_to(archive.read(entry).decode())
                    elif entry.is_dir():
                        path.mkdir(exist_ok=True)
                    else:
                        path.write_bytes(archive.read(entry))
                        path.chmod(stat.S_IMODE(mode))
            return ""
        if command[:4] == ["/usr/bin/codesign", "--verify", "--deep", "--strict"]:
            return ""
        if command[:3] == ["/usr/bin/codesign", "--display", "--verbose=4"]:
            return ("Identifier=test.ohmygame\nCDHash=" + "a" * 40
                    + "\nTeamIdentifier=FIXTURE\n")
        raise AssertionError(f"installer tried to execute an application: {command}")

    monkeypatch.setattr(module, "_run", run_fixture)
    return calls


def test_cli_requires_runtime_dir(load_worker):
    module = load_worker()
    with pytest.raises(SystemExit) as error:
        module.main([])
    assert error.value.code == 2


@pytest.mark.parametrize("damage", ["hash", "short", "long", "header", "redirect"])
def test_download_integrity_failure_never_reaches_ditto(load_worker, monkeypatch, tmp_path, damage):
    module = load_worker()
    data = bundle()
    calls = offline_release(module, monkeypatch, data)
    if damage == "hash":
        monkeypatch.setattr(module, "ASSET_SHA256", "0" * 64)
    elif damage == "short":
        calls.response = lambda: Response(data[:-1], length=len(data))
    elif damage == "long":
        calls.response = lambda: Response(data + b"x", length=len(data))
    elif damage == "header":
        calls.response = lambda: Response(data, length=len(data) + 1)
    else:
        calls.response = lambda: Response(data, url="https://evil.invalid/payload?secret=hidden")
    runtime = tmp_path / "runtime"
    with pytest.raises(module.InstallError):
        module.install(runtime)
    assert not calls.commands
    assert not (runtime / "app").exists()
    assert not list(runtime.rglob("*.partial"))
    assert not list(runtime.glob(".ohmygame-stage-*"))


@pytest.mark.parametrize("extra", [
    [("../outside", b"bad", REGULAR)],
    [("/absolute", b"bad", REGULAR)],
    [("C:/outside", b"bad", REGULAR)],
    [("a\\..\\outside", b"bad", REGULAR)],
    [("a//b", b"bad", REGULAR)],
    [("a/./b", b"bad", REGULAR)],
    [("LICENSE", b"duplicate", REGULAR)],
    [("license", b"case alias", REGULAR)],
    [("café", b"one", REGULAR), ("cafe\u0301", b"two", REGULAR)],
    [("file", b"parent", REGULAR), ("file/child", b"bad", REGULAR)],
    [("escape", b"../outside", LINK)],
    [("escape", b"/tmp/outside", LINK)],
    [("door", b".", LINK), ("escape", b"door/../outside", LINK)],
    [("a", b"b", LINK), ("b", b"a", LINK)],
    [("alias", b"OhMyGame.app", LINK), ("alias/child", b"bad", REGULAR)],
    [("disguised-directory/", b"../outside", LINK)],
    [("broken", b"missing", LINK)],
    [("pipe", b"", stat.S_IFIFO | 0o600)],
])
def test_zip_unsafe_paths_duplicates_and_links_stop_before_ditto(
        load_worker, monkeypatch, tmp_path, extra):
    module = load_worker()
    calls = offline_release(module, monkeypatch, bundle(extra))
    with pytest.raises(module.InstallError):
        module.install(tmp_path / "runtime")
    assert not calls.commands
    assert not (tmp_path / "runtime/app").exists()


def test_zip_local_header_must_agree_with_directory(load_worker, monkeypatch, tmp_path):
    module = load_worker()
    data = bundle([("safe-name", b"data", REGULAR)])
    data = data.replace(b"safe-name", b"../escape", 1)
    calls = offline_release(module, monkeypatch, data)
    with pytest.raises(module.InstallError):
        module.install(tmp_path / "runtime")
    assert not calls.commands


@pytest.mark.parametrize("field", [0x7075, 0x756E, 0x000D])
@pytest.mark.parametrize("local_only", [False, True])
def test_zip_alternate_path_or_link_metadata_is_rejected(
        load_worker, monkeypatch, tmp_path, field, local_only):
    module = load_worker()
    stream = io.BytesIO(bundle())
    entry = zipfile.ZipInfo("safe-name")
    body = b"\x01\x00\x00\x00\x00../escape"
    entry.extra = struct.pack("<HH", field, len(body)) + body
    with zipfile.ZipFile(stream, "a") as archive:
        archive.writestr(entry, b"data")
    data = stream.getvalue()
    if local_only:
        offset = data.rfind(entry.extra)
        # Leave dangerous metadata only in the local header, not the directory.
        data = data[:offset] + struct.pack("<H", 0x6375) + data[offset + 2:]
    calls = offline_release(module, monkeypatch, data)
    with pytest.raises(module.InstallError):
        module.install(tmp_path / "runtime")
    assert not calls.commands


@pytest.mark.parametrize("failure", ["download", "signature"])
def test_cleanup_only_removes_this_invocations_staging_and_partial(
        load_worker, monkeypatch, tmp_path, failure):
    module = load_worker()
    calls = offline_release(module, monkeypatch)
    runtime = tmp_path / "runtime"
    old_stage = runtime / ".ohmygame-stage-other"
    old_stage.mkdir(parents=True)
    (old_stage / "keep").write_text("other installation")
    cache = runtime / module.CACHE
    cache.mkdir(mode=0o700)
    old_partial = cache / "other.partial"
    old_partial.write_bytes(b"other download")
    if failure == "download":
        monkeypatch.setattr(module, "ASSET_SHA256", "0" * 64)
    else:
        previous = module._run

        def fail_signature(command, timeout):
            if command[0] == "/usr/bin/codesign":
                raise module.InstallError("signature_invalid")
            return previous(command, timeout)

        monkeypatch.setattr(module, "_run", fail_signature)
    with pytest.raises(module.InstallError):
        module.install(runtime)
    assert (old_stage / "keep").read_text() == "other installation"
    assert old_partial.read_bytes() == b"other download"
    assert list(runtime.glob(".ohmygame-stage-*")) == [old_stage]
    assert list(cache.glob("*.partial")) == [old_partial]
    assert not (runtime / "app").exists()
    assert len(calls.downloads) == 1


@pytest.mark.parametrize("kind", ["empty", "populated", "file", "symlink"])
def test_unowned_target_is_never_overwritten(load_worker, monkeypatch, tmp_path, kind):
    module = load_worker()
    calls = offline_release(module, monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    target = runtime / "app"
    outside = tmp_path / "outside"
    outside.write_text("keep outside")
    if kind == "file":
        target.write_text("keep target")
    elif kind == "symlink":
        target.symlink_to(outside)
    else:
        target.mkdir()
        if kind == "populated":
            (target / "keep").write_text("keep target")
    before = target.lstat()
    with pytest.raises(module.InstallError):
        module.install(runtime)
    assert target.lstat().st_ino == before.st_ino
    assert outside.read_text() == "keep outside"
    if kind == "file":
        assert target.read_text() == "keep target"
    if kind == "populated":
        assert (target / "keep").read_text() == "keep target"
    assert not calls.commands and not calls.downloads


def test_install_then_reuse_and_check_preserve_full_bundle(
        load_worker, monkeypatch, tmp_path, capsys):
    module = load_worker()
    calls = offline_release(module, monkeypatch)
    runtime = tmp_path / "runtime"
    expected = {"installed": True, "app_path": str(runtime / "app" / APP),
                "version": VERSION, "source_sha": SOURCE_SHA}
    assert module.install(runtime) == expected
    app = runtime / "app" / APP
    assert (app / "Contents/Resources/app.asar").read_bytes() == b"complete fixture application"
    assert (runtime / "app/LICENSE").read_text() == "upstream fixture license\n"
    assert (app / "Contents/Frameworks/F.framework/F").read_bytes() == b"framework"
    assert (app / "Contents/Frameworks/F.framework/F").is_symlink()
    assert (app / "Contents/_CodeSignature/CodeResources").read_bytes() == b"fixture seal"
    receipt = runtime / "app" / module.RECEIPT
    original_receipt = receipt.read_bytes()
    calls.downloads.clear()
    calls.commands.clear()
    assert module.install(runtime) == expected
    assert module.main(["--runtime-dir", str(runtime), "--check"]) == 0
    assert json.loads(capsys.readouterr().out) == expected
    assert receipt.read_bytes() == original_receipt
    assert not calls.downloads
    assert calls.commands and all(command[0] == "/usr/bin/codesign" for command in calls.commands)
    assert not list(runtime.glob(".ohmygame-stage-*"))


@pytest.mark.parametrize("damage", [
    "archive", "source", "receipt_digest", "receipt_missing", "receipt_symlink",
    "version", "executable", "executable_mode", "resource", "license", "link",
    "signature", "signature_failure",
])
def test_idempotency_requires_provenance_signature_and_actual_files(
        load_worker, monkeypatch, tmp_path, damage):
    module = load_worker()
    calls = offline_release(module, monkeypatch)
    runtime = tmp_path / "runtime"
    module.install(runtime)
    app = runtime / "app" / APP
    receipt_path = runtime / "app" / module.RECEIPT
    if damage == "archive":
        next(runtime.rglob("*.zip")).write_bytes(b"tampered original download")
    elif damage in {"source", "receipt_digest"}:
        receipt = json.loads(receipt_path.read_text())
        receipt["source_sha" if damage == "source" else "archive_sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(receipt))
    elif damage == "receipt_missing":
        receipt_path.unlink()
    elif damage == "receipt_symlink":
        elsewhere = tmp_path / "copied-receipt"
        elsewhere.write_bytes(receipt_path.read_bytes())
        receipt_path.unlink()
        receipt_path.symlink_to(elsewhere)
    elif damage == "version":
        path = app / "Contents/Info.plist"
        info = plistlib.loads(path.read_bytes())
        info["CFBundleShortVersionString"] = "99.0"
        path.write_bytes(plistlib.dumps(info))
    elif damage == "executable":
        (app / "Contents/MacOS/OhMyGame").unlink()
    elif damage == "executable_mode":
        (app / "Contents/MacOS/OhMyGame").chmod(0o644)
    elif damage == "resource":
        (app / "Contents/Resources/app.asar").write_bytes(b"unsigned change")
    elif damage == "license":
        (runtime / "app/LICENSE").write_bytes(b"changed")
    elif damage == "link":
        link = app / "Contents/Frameworks/F.framework/F"
        link.unlink()
        link.symlink_to(tmp_path)
    elif damage == "signature":
        previous = module._run
        monkeypatch.setattr(module, "_run",
                            lambda *args: previous(*args).replace("a" * 40, "b" * 40))
    else:
        def invalid_signature(*args):
            raise module.InstallError("signature_invalid")
        monkeypatch.setattr(module, "_run", invalid_signature)
    calls.downloads.clear()
    calls.commands.clear()
    with pytest.raises(module.InstallError):
        module.check(runtime)
    with pytest.raises(module.InstallError):
        module.install(runtime)
    assert not calls.downloads
    assert all(command[0] == "/usr/bin/codesign" for command in calls.commands)
    assert (runtime / "app").exists()  # Never repair/delete an unverified existing target.


def test_check_missing_is_offline_and_does_not_create_runtime(
        load_worker, monkeypatch, tmp_path, capsys):
    module = load_worker()
    monkeypatch.setattr(module.request, "build_opener", forbidden)
    runtime = tmp_path / "missing"
    assert module.main(["--runtime-dir", str(runtime), "--check"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["installed"] is False
    assert result["source_sha"] == SOURCE_SHA
    assert not runtime.exists()
    assert not module.LOCK_PATH.exists()


@pytest.mark.parametrize("url", [
    "http://release-assets.githubusercontent.com/file",
    "https://github.com/other/repo/releases/download/tag/file",
    "https://release-assets.githubusercontent.com.evil.invalid/file",
    "https://user:secret@release-assets.githubusercontent.com/file",
    "file:///tmp/archive.zip",
])
def test_redirect_is_rejected_before_following(load_worker, url):
    module = load_worker()
    with pytest.raises(module.InstallError):
        module._OfficialRedirect().redirect_request(
            module.request.Request(URL), None, 302, "Found", {}, url)


def test_official_asset_cdn_redirect_is_allowed(load_worker):
    module = load_worker()
    url = "https://release-assets.githubusercontent.com/github-production-release-asset/id/file?sig=test"
    req = module._OfficialRedirect().redirect_request(
        module.request.Request(URL), None, 302, "Found", {}, url)
    assert req.full_url == url


@pytest.mark.parametrize("mode", ["ok", "timeout", "registration_failure", "exit_failure"])
def test_command_lifecycle_registers_and_only_reaps_its_own_group(
        load_worker, monkeypatch, mode):
    module = load_worker()
    events = []

    class Process:
        pid = 41337
        returncode = None

        def communicate(self, timeout):
            events.append(("communicate", timeout))
            if mode == "timeout" and self.returncode is None:
                raise subprocess.TimeoutExpired(["codesign"], timeout)
            self.returncode = self.returncode if self.returncode is not None else (
                3 if mode == "exit_failure" else 0)
            return "public stdout", "private diagnostic?token=DO_NOT_LEAK"

        def poll(self):
            return self.returncode

    process = Process()

    def popen(command, **kwargs):
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE
        events.append(("spawn", command))
        return process

    def register(pid, **kwargs):
        assert pid == kwargs["pgid"] == process.pid
        assert kwargs["owner"] == os.getpid()
        events.append(("register", pid))
        return mode != "registration_failure"

    def killpg(pgid, sig):
        assert pgid == process.pid and sig == signal.SIGKILL
        events.append(("kill", pgid))
        process.returncode = -9

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module.os, "killpg", killpg)
    monkeypatch.setitem(sys.modules, "legion_proc_registry", SimpleNamespace(
        register=register, unregister=lambda pid: events.append(("unregister", pid))))
    if mode == "ok":
        assert "public stdout" in module._run(["/usr/bin/codesign", "--verify"], 2)
    else:
        with pytest.raises(module.InstallError) as error:
            module._run(["/usr/bin/codesign", "--verify"], 2)
        assert "DO_NOT_LEAK" not in str(error.value)
    assert events[0][0] == "spawn" and events[1][0] == "register"
    if mode == "registration_failure":
        assert not any(event[0] == "unregister" for event in events)
    else:
        assert events[-1] == ("unregister", process.pid)
    assert bool([event for event in events if event[0] == "kill"]) == (
        mode in {"timeout", "registration_failure"})


def test_cli_does_not_print_raw_transport_errors(load_worker, monkeypatch, tmp_path, capsys):
    module = load_worker()

    def fail(_runtime):
        raise OSError("https://private.invalid/?token=DO_NOT_LEAK")

    monkeypatch.setattr(module, "install", fail)
    assert module.main(["--runtime-dir", str(tmp_path / "runtime")]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["installed"] is False
    assert "DO_NOT_LEAK" not in captured.out + captured.err


def test_lock_conflict_is_bounded_and_does_not_unlink_shared_lock(load_worker, monkeypatch):
    module = load_worker()
    import fcntl
    monkeypatch.setattr(module, "LOCK_TIMEOUT", 0.01)
    with module.LOCK_PATH.open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(module.InstallError, match="lock_timeout"):
            with module._install_lock():
                pytest.fail("contending installer entered locked section")
    assert module.LOCK_PATH.is_file()


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin exclusive rename primitive")
def test_atomic_publish_does_not_replace_even_an_empty_existing_directory(
        load_worker, tmp_path):
    module = load_worker()
    source, destination = tmp_path / "stage", tmp_path / "existing"
    source.mkdir()
    (source / "keep").write_text("staged")
    destination.mkdir()
    with pytest.raises(OSError):
        module._publish(source, destination)
    assert (source / "keep").read_text() == "staged"
    assert list(destination.iterdir()) == []
