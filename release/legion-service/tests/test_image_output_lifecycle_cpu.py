"""Image output lifecycle: real temporary files, fake requests, no network/models."""
from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
from types import SimpleNamespace
import zlib

import pytest


SERVICE = Path(__file__).resolve().parents[1]


def png():
    def chunk(kind, raw):
        return (struct.pack(">I", len(raw)) + kind + raw
                + struct.pack(">I", zlib.crc32(kind + raw)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "OPENAI_", "OPENROUTER_",
                           "AIGC_", "ANTHROPIC_", "CODEX_")):
            monkeypatch.delenv(key)
    root = tmp_path.resolve()
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        directory = root / key
        directory.mkdir(mode=0o700)
        monkeypatch.setenv(key, str(directory))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("LEGION_EXT_AUTO_UPDATE", "0")
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        raise AssertionError("image lifecycle tests must not use network or spawn processes")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    cli = importlib.import_module("media_model_cli")
    native = importlib.import_module("native_capability_host")
    monkeypatch.setattr(cli.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(cli.urllib.request, "build_opener", forbidden)
    work = root / "work"
    work.mkdir(mode=0o700)
    info = work.stat()
    env = {
        "MEYO_WORKDIR": str(work),
        "MEYO_MODEL_WORKSPACE_ID": json.dumps([info.st_dev, info.st_ino]),
        "MEYO_CAPABILITY_POLICY_JSON": json.dumps({
            "billable": True, "network": True, "write": True, "read": True,
        }),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    value = SimpleNamespace(
        cli=cli, native=native, root=root, work=work, env=env, patch=monkeypatch,
        raw=png(), calls=[], on_request=None, original_request=cli._request,
    )
    value.response = {"data": [{"b64_json": base64.b64encode(value.raw).decode("ascii")}]}

    def request(direction, endpoint, payload, timeout_s, **kwargs):
        value.calls.append((direction, endpoint, payload, timeout_s))
        if value.on_request:
            value.on_request()
        return value.response

    monkeypatch.setattr(cli, "_request", request)
    return value


def public_failure(case):
    return pytest.raises(case.cli.MediaError, match=r"^media_(?:image|output)_[a-z_]+$")


def test_default_image_reserves_before_request_and_writes_the_same_inode(case, capsys):
    output = case.work / "new.png"
    reserved = []
    opens = []
    real_open = os.open

    def tracked_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if os.fspath(path) == output.name and kwargs.get("dir_fd") is not None:
            opens.append((flags, fd))
        return fd

    def inspect_reservation():
        assert output.is_file(), "the target must already be reserved before the paid request"
        info = output.stat()
        assert info.st_size == 0 and stat.S_IMODE(info.st_mode) == 0o600
        reserved.append((info.st_dev, info.st_ino))

    case.patch.setattr(os, "open", tracked_open)
    case.on_request = inspect_reservation
    before = dict(os.environ)
    case.cli._image("fixture image", str(output), 3)
    assert capsys.readouterr().out == str(output) + "\n"
    assert dict(os.environ) == before
    assert output.read_bytes() == case.raw
    info = output.stat()
    assert reserved == [(info.st_dev, info.st_ino)]
    assert len(opens) == 1, "output write/readback must not reopen the path"
    assert opens[0][0] & os.O_EXCL and opens[0][0] & os.O_NOFOLLOW
    assert len(case.calls) == 1
    assert case.calls[0] == ("image_generation", "/images/generations",
                             {"prompt": "fixture image", "n": 1, "size": "1024x1024"}, 3)


def test_image_json_returns_only_verified_file_facts(case, capsys):
    output = case.work / "result.png"
    assert case.cli.main(["image", "fixture", "--out", str(output), "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "ok": True, "path": str(output), "bytes": len(case.raw),
        "sha256": hashlib.sha256(case.raw).hexdigest(), "request_started": True,
    }
    assert len(case.calls) == 1


@pytest.mark.parametrize("kind", [
    "existing", "directory", "symlink", "dangling_symlink", "parent_symlink",
    "missing_parent", "outside", "traversal", "private", "dotfile",
    "readonly_parent", "writable_by_others",
])
def test_bad_output_never_reaches_paid_request_or_changes_existing_files(case, kind):
    output = case.work / "frame.png"
    outside = case.root / "outside"
    outside.mkdir()
    sentinel = outside / "frame.png"
    sentinel.write_bytes(b"unrelated-file")
    restore = None
    if kind == "existing":
        output.write_bytes(b"existing-file")
    elif kind == "directory":
        output.mkdir()
    elif kind in ("symlink", "dangling_symlink"):
        output.symlink_to(sentinel if kind == "symlink" else outside / "missing.png")
    elif kind == "parent_symlink":
        (case.work / "linked").symlink_to(outside, target_is_directory=True)
        output = case.work / "linked/frame.png"
    elif kind == "missing_parent":
        output = case.work / "absent/frame.png"
    elif kind == "outside":
        output = outside / "new.png"
    elif kind == "traversal":
        output = case.work / "../outside/new.png"
    elif kind == "private":
        (case.work / "secrets").mkdir()
        output = case.work / "secrets/frame.png"
    elif kind == "dotfile":
        output = case.work / ".frame.png"
    elif kind in ("readonly_parent", "writable_by_others"):
        parent = case.work / "parent"
        parent.mkdir(mode=0o700)
        parent.chmod(0o500 if kind == "readonly_parent" else 0o777)
        output = parent / "frame.png"
        restore = parent
    try:
        with public_failure(case):
            case.cli._image("fixture", str(output), 3)
        assert case.calls == []
        assert sentinel.read_bytes() == b"unrelated-file"
        if kind == "existing":
            assert output.read_bytes() == b"existing-file"
    finally:
        if restore:
            restore.chmod(0o700)


@pytest.mark.parametrize("field", ["billable", "network", "write"])
@pytest.mark.parametrize("value", [False, "false", None, 0])
def test_explicit_generation_denials_or_malformed_policy_precede_request(case, field, value):
    policy = {"billable": True, "network": True, "write": True, "read": True}
    policy[field] = value
    case.patch.setenv("MEYO_CAPABILITY_POLICY_JSON", json.dumps(policy))
    output = case.work / "denied.png"
    with public_failure(case):
        case.cli._image("fixture", str(output), 3)
    assert case.calls == [] and not output.exists()


@pytest.mark.parametrize("policy", ["", "null", "[]", "{broken", '{"write":"fixture-secret"}'])
def test_invalid_policy_is_fixed_failure_without_request_or_secret(case, policy, capsys):
    case.patch.setenv("MEYO_CAPABILITY_POLICY_JSON", policy)
    output = case.work / "no.png"
    assert case.cli.main(["image", "fixture", "--out", str(output)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "media_image_policy_invalid\n"
    assert case.calls == [] and not output.exists()


def test_generation_does_not_borrow_read_permission_but_facts_require_it(case):
    policy = {"billable": True, "network": True, "write": True, "read": False}
    env = {**case.env, "MEYO_CAPABILITY_POLICY_JSON": json.dumps(policy)}
    case.patch.setenv("MEYO_CAPABILITY_POLICY_JSON", env["MEYO_CAPABILITY_POLICY_JSON"])
    output = case.work / "own-output.png"
    case.cli._image("fixture", str(output), 3)
    assert output.read_bytes() == case.raw and len(case.calls) == 1
    with pytest.raises(case.cli.MediaError, match="^media_image_read_denied$"):
        case.cli.image_file_facts(output, env)


@pytest.mark.parametrize("fault", [
    "missing_path_with_identity", "empty", "relative", "symlink",
    "bad_identity", "mismatching_identity", "public_directory",
])
def test_invalid_bound_workspace_precedes_any_request(case, fault):
    if fault == "missing_path_with_identity":
        case.patch.delenv("MEYO_WORKDIR")
    elif fault == "empty":
        case.patch.setenv("MEYO_WORKDIR", "")
    elif fault == "relative":
        case.patch.setenv("MEYO_WORKDIR", "work")
    elif fault == "symlink":
        link = case.root / "workspace-link"
        link.symlink_to(case.work, target_is_directory=True)
        case.patch.setenv("MEYO_WORKDIR", str(link))
    elif fault == "bad_identity":
        case.patch.setenv("MEYO_MODEL_WORKSPACE_ID", "[false, 1]")
    elif fault == "mismatching_identity":
        case.patch.setenv("MEYO_MODEL_WORKSPACE_ID", "[0, 0]")
    elif fault == "public_directory":
        case.work.chmod(0o777)
    try:
        with public_failure(case):
            case.cli._image("fixture", str(case.work / "no.png"), 3)
        assert case.calls == []
    finally:
        case.work.chmod(0o700)


def test_public_validator_is_readonly_and_uses_only_explicit_environment(case):
    before = dict(os.environ)
    entries = sorted(str(p) for p in case.root.rglob("*"))
    assert case.cli.validate_image_generation(case.env) == case.work
    assert case.cli.validate_image_generation({}) is None
    assert dict(os.environ) == before
    assert sorted(str(p) for p in case.root.rglob("*")) == entries
    assert not case.calls


@pytest.mark.parametrize("relative", [True, False])
def test_operator_explicit_out_keeps_path_stdout_without_workspace(case, capsys, relative):
    case.patch.delenv("MEYO_WORKDIR")
    case.patch.delenv("MEYO_MODEL_WORKSPACE_ID")
    case.patch.chdir(case.root)
    output = Path("operator.png") if relative else case.root / "operator.png"
    reservations = []
    case.on_request = lambda: reservations.append(output.is_file())
    case.cli._image("fixture", str(output), 3)
    assert reservations == [True]
    assert output.read_bytes() == case.raw
    assert capsys.readouterr().out == str(output) + "\n"
    assert len(case.calls) == 1


@pytest.mark.parametrize("replacement", ["symlink", "regular", "hardlink"])
def test_file_replaced_during_request_fails_without_writing_or_deleting_replacement(case, replacement):
    output = case.work / "result.png"
    held = case.work / "reserved-held"
    outside = case.root / "external.png"
    outside.write_bytes(b"outside-stays")

    def replace():
        if output.exists():
            output.rename(held)
        if replacement == "symlink":
            output.symlink_to(outside)
        elif replacement == "hardlink":
            os.link(outside, output)
        else:
            output.write_bytes(b"replacement-stays")

    case.on_request = replace
    with public_failure(case):
        case.cli._image("fixture", str(output), 3)
    assert len(case.calls) == 1
    assert outside.read_bytes() == b"outside-stays"
    assert os.path.lexists(output)
    assert output.read_bytes() == (b"replacement-stays" if replacement == "regular" else b"outside-stays")
    assert held.read_bytes() == b""


@pytest.mark.parametrize("replacement", ["symlink", "directory", "workspace"])
def test_parent_replaced_during_request_fails_and_cleans_only_original_reservation(case, replacement):
    parent = case.work / "images"
    parent.mkdir()
    output = parent / "result.png"
    outside = case.root / "outside"
    outside.mkdir()
    sentinel = outside / output.name
    sentinel.write_bytes(b"external-stays")
    moved = case.root / "moved-original"

    def replace():
        original = case.work if replacement == "workspace" else parent
        original.rename(moved)
        if replacement == "symlink":
            parent.symlink_to(outside, target_is_directory=True)
        elif replacement == "directory":
            parent.mkdir()
            output.write_bytes(b"replacement-stays")
        else:
            case.work.mkdir()
            parent.mkdir()
            output.write_bytes(b"replacement-stays")

    case.on_request = replace
    with public_failure(case):
        case.cli._image("fixture", str(output), 3)
    assert len(case.calls) == 1 and sentinel.read_bytes() == b"external-stays"
    assert output.read_bytes() == (b"external-stays" if replacement == "symlink" else b"replacement-stays")
    original_output = moved / ("images/result.png" if replacement == "workspace" else "result.png")
    assert not original_output.exists()


def test_new_hardlink_to_reservation_is_not_written_after_request(case):
    output = case.work / "result.png"
    external_link = case.root / "new-external-link"

    def link_reservation():
        assert output.exists()
        os.link(output, external_link)

    case.on_request = link_reservation
    with public_failure(case):
        case.cli._image("fixture", str(output), 3)
    assert len(case.calls) == 1
    assert external_link.read_bytes() == b""
    assert not output.exists()


@pytest.mark.parametrize("kind", ["timeout", "interrupt", "http"])
def test_request_failure_removes_only_its_own_reservation(case, kind, capsys):
    output = case.work / "failed.png"
    observed = []

    def fail():
        observed.append(output.exists())
        if kind == "interrupt":
            raise KeyboardInterrupt()
        if kind == "http":
            raise case.cli.urllib.error.HTTPError(
                "https://fixture.invalid/?key=fixture-secret", 502, "fixture-secret", {}, None)
        raise TimeoutError("fixture-secret")

    case.on_request = fail
    if kind == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            case.cli._image("fixture", str(output), 3)
    else:
        assert case.cli.main(["image", "fixture", "--out", str(output)]) == 1
    captured = capsys.readouterr()
    assert "fixture-secret" not in captured.out + captured.err
    assert observed == [True] and not output.exists()
    assert len(case.calls) == 1


@pytest.mark.parametrize("route", ["base64", "url"])
@pytest.mark.parametrize("oversized", [False, True])
def test_image_transport_is_bounded_and_does_not_reencode(case, route, oversized):
    limit = len(case.raw)
    case.patch.setattr(case.native, "_MAX_IMAGE_BYTES", limit)
    raw = case.raw + (b"x" if oversized else b"")
    reads = []
    if route == "base64":
        case.response = {"data": [{"b64_json": base64.b64encode(raw).decode()}]}
    else:
        case.response = {"data": [{"url": "https://fixture.invalid/image.png"}]}

        class Response(io.BytesIO):
            def read(self, size=-1):
                reads.append(size)
                return super().read(size)

        def fetch(request, timeout):
            assert request.get_header("Authorization") is None
            return Response(raw)

        case.patch.setattr(case.cli.urllib.request, "urlopen", fetch)
    output = case.work / "sized.png"
    if oversized:
        with pytest.raises(case.cli.MediaError, match="^media_output_limit$"):
            case.cli._image("fixture", str(output), 3)
        assert not output.exists()
    else:
        case.cli._image("fixture", str(output), 3)
        assert output.read_bytes() == case.raw
    assert len(case.calls) == 1
    if route == "url":
        assert reads == [limit + 1]


@pytest.mark.parametrize("raw", [b"not-png", b"", b"\x89PNG\r\n\x1a\n"])
def test_invalid_generated_png_is_not_left_as_a_success_file(case, raw):
    case.response = {"data": [{"b64_json": base64.b64encode(raw).decode()}]}
    output = case.work / "invalid.png"
    with public_failure(case):
        case.cli._image("fixture", str(output), 3)
    assert not output.exists() and len(case.calls) == 1


def test_kernel_preflight_denial_precedes_request_even_when_mode_looks_writable(case):
    output = case.work / "denied-by-sandbox.png"
    original = os.open

    def deny(path, flags, *args, **kwargs):
        if os.fspath(path) == output.name and flags & os.O_CREAT:
            raise PermissionError("fixture-secret kernel denial")
        return original(path, flags, *args, **kwargs)

    case.patch.setattr(os, "open", deny)
    with public_failure(case):
        case.cli._image("fixture", str(output), 3)
    assert not output.exists() and not case.calls


def test_reservation_replaced_before_request_is_local_failure_and_preserves_replacement(case):
    output = case.work / "preflight-race.png"
    held = case.work / "held-reservation"
    original = os.fchmod

    def replace_after_chmod(fd, mode):
        original(fd, mode)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            output.rename(held)
            output.write_bytes(b"replacement-stays")

    case.patch.setattr(os, "fchmod", replace_after_chmod)
    with pytest.raises(case.cli.MediaError) as failure:
        case.cli._image("fixture", str(output), 3)
    assert failure.value.request_started is False
    assert not case.calls
    assert output.read_bytes() == b"replacement-stays" and held.read_bytes() == b""


def test_output_permissions_changed_during_write_cannot_be_reported_as_success(case):
    output = case.work / "changed-permissions.png"
    original = os.fsync

    def change_permissions(fd):
        original(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            os.fchmod(fd, 0o666)

    case.patch.setattr(os, "fsync", change_permissions)
    with pytest.raises(case.cli.MediaError) as failure:
        case.cli._image("fixture", str(output), 3)
    assert failure.value.request_started is True
    assert len(case.calls) == 1 and not output.exists()


def test_output_write_failure_is_fixed_and_cleans_own_file_without_retry(case, capsys):
    output = case.work / "disk-full.png"

    def fail_sync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("fixture-secret disk full")

    case.patch.setattr(os, "fsync", fail_sync)
    assert case.cli.main(["image", "fixture", "--out", str(output)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "fixture-secret" not in captured.err
    assert not output.exists() and len(case.calls) == 1


@pytest.mark.parametrize("oversized", [False, True])
def test_generation_response_json_read_is_bounded(case, oversized):
    case.patch.setattr(case.native, "_MAX_IMAGE_BYTES", len(case.raw))
    case.patch.setattr(case.cli, "_select", lambda direction: ("fixture-image", "fixture-secret"))
    reads = []
    encoded = json.dumps(case.response).encode()

    class Response(io.BytesIO):
        headers = {}

        def read(self, size=-1):
            reads.append(size)
            assert size > 0, "image generation JSON must have a bounded transport read"
            return b"x" * size if oversized else super().read(size)

    case.patch.setattr(case.cli.urllib.request, "build_opener",
                      lambda *args: SimpleNamespace(open=lambda *a, **k: Response(encoded)))
    if oversized:
        with pytest.raises(case.cli.MediaError, match="^media_output_limit$"):
            case.original_request("image_generation", "/images/generations", {}, 3)
    else:
        result = case.original_request("image_generation", "/images/generations", {}, 3)
        assert result["data"] == case.response["data"]
    assert len(reads) == 1 and not case.calls


@pytest.mark.parametrize("failure", ["existing", "policy", "workspace", "timeout_value"])
def test_image_json_local_failure_has_request_started_false(case, capsys, failure):
    output = case.work / "not-started.png"
    command = ["image", "fixture", "--out", str(output), "--json"]
    if failure == "existing":
        output.write_bytes(b"existing-stays")
    elif failure == "policy":
        case.patch.setenv("MEYO_CAPABILITY_POLICY_JSON", '{"write": false}')
    elif failure == "workspace":
        case.patch.setenv("MEYO_MODEL_WORKSPACE_ID", "[0,0]")
    else:
        command.extend(["--timeout", "nan"])
    assert case.cli.main(command) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert set(result) == {"ok", "error", "request_started"}
    assert result["ok"] is False and result["request_started"] is False
    assert result["error"].startswith("media_") and captured.err == ""
    assert not case.calls
    if failure == "existing":
        assert output.read_bytes() == b"existing-stays"


@pytest.mark.parametrize("failure", ["timeout", "http", "invalid_png", "write"])
def test_image_json_post_request_failure_has_request_started_true(case, capsys, failure):
    output = case.work / "attempted.png"
    if failure == "timeout":
        def fail():
            raise TimeoutError("fixture-secret")
        case.on_request = fail
    elif failure == "http":
        def fail():
            raise case.cli.urllib.error.HTTPError(
                "https://fixture.invalid/?key=fixture-secret", 503, "fixture-secret", {}, None)
        case.on_request = fail
    elif failure == "invalid_png":
        case.response = {"data": [{"b64_json": base64.b64encode(b"not-png").decode()}]}
    else:
        def fail_sync(fd):
            raise OSError("fixture-secret")
        case.patch.setattr(os, "fsync", fail_sync)
    assert case.cli.main(["image", "fixture", "--out", str(output), "--json"]) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert set(result) == {"ok", "error", "request_started"}
    assert result["ok"] is False and result["request_started"] is True
    assert result["error"].startswith("media_") and captured.err == ""
    assert "fixture-secret" not in captured.out
    assert len(case.calls) == 1 and not output.exists()


def test_media_error_phase_is_per_image_attempt_not_shared_with_other_calls(case):
    existing = case.work / "existing.png"
    existing.write_bytes(case.raw)
    with pytest.raises(case.cli.MediaError) as local:
        case.cli._image("fixture", str(existing), 3)
    assert local.value.request_started is False
    case.response = {"data": [{"b64_json": "broken-png"}]}
    with pytest.raises(case.cli.MediaError) as attempted:
        case.cli._image("fixture", str(case.work / "attempted.png"), 3)
    assert attempted.value.request_started is True
    assert local.value.request_started is False and len(case.calls) == 1


def test_chat_error_contract_is_not_changed_by_image_phase_json(case, capsys):
    def fail(*args, **kwargs):
        raise case.cli.MediaError("media_gateway_error")
    case.patch.setattr(case.cli, "_chat", fail)
    assert case.cli.main(["chat", "fixture", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "media_gateway_error\n"
    assert not case.calls


def test_public_facts_reads_once_and_hashes_exact_validated_bytes(case):
    output = case.work / "image.png"
    output.write_bytes(case.raw)
    calls = []
    original = case.native._read_regular_at
    before = dict(os.environ)

    def reader(*args, **kwargs):
        raw = original(*args, **kwargs)
        calls.append(raw)
        return raw

    case.patch.setattr(case.native, "_read_regular_at", reader)
    policy = {"read": True, "billable": False, "network": False, "write": False}
    env = {**case.env, "MEYO_CAPABILITY_POLICY_JSON": json.dumps(policy)}
    result = case.cli.image_file_facts("image.png", env)
    assert result == {"path": str(output), "bytes": len(case.raw),
                      "sha256": hashlib.sha256(case.raw).hexdigest()}
    assert calls == [case.raw] and not case.calls
    assert dict(os.environ) == before


def test_public_facts_never_borrows_ambient_workspace_or_cwd(case):
    output = case.work / "image.png"
    output.write_bytes(case.raw)
    case.patch.chdir(case.work)
    with public_failure(case):
        case.cli.image_file_facts("image.png", {})
    assert case.cli.image_file_facts(output, {})["sha256"] == hashlib.sha256(case.raw).hexdigest()
    other = case.root / "other"
    other.mkdir()
    with public_failure(case):
        case.cli.image_file_facts(output, {"MEYO_WORKDIR": str(other)})
    assert not case.calls


def test_operator_facts_can_use_legacy_media_root_without_granting_a_workspace(case):
    legacy = case.root / ".meyo/legion/media"
    legacy.mkdir(parents=True)
    output = legacy / "frame.png"
    output.write_bytes(case.raw)
    assert case.cli.image_file_facts(output, {})["path"] == str(output)
    assert not case.calls


@pytest.mark.parametrize("fault", [
    "read_denied", "malformed_policy", "outside", "private", "dotfile",
    "symlink", "parent_symlink", "hardlink", "fifo", "oversized", "invalid_png",
])
def test_facts_preserves_image_read_boundaries(case, fault):
    output = case.work / "image.png"
    output.write_bytes(case.raw)
    env = dict(case.env)
    outside = case.root / "outside.png"
    outside.write_bytes(case.raw)
    if fault == "read_denied":
        env["MEYO_CAPABILITY_POLICY_JSON"] = '{"read": false}'
    elif fault == "malformed_policy":
        env["MEYO_CAPABILITY_POLICY_JSON"] = '{"read": "false"}'
    elif fault == "outside":
        output = outside
    elif fault in ("private", "parent_symlink"):
        directory = case.work / ("secrets" if fault == "private" else "linked")
        if fault == "private":
            directory.mkdir()
            (directory / "image.png").write_bytes(case.raw)
            output = directory / "image.png"
        else:
            directory.symlink_to(case.root, target_is_directory=True)
            output = directory / "outside.png"
    elif fault == "dotfile":
        output = case.work / ".image.png"
        output.write_bytes(case.raw)
    elif fault in ("symlink", "hardlink", "fifo"):
        output.unlink()
        if fault == "symlink":
            output.symlink_to(outside)
        elif fault == "hardlink":
            os.link(outside, output)
        else:
            os.mkfifo(output)
    elif fault == "oversized":
        case.patch.setattr(case.native, "_MAX_IMAGE_BYTES", len(case.raw) - 1)
    elif fault == "invalid_png":
        output.write_bytes(b"not-png")
    with public_failure(case):
        case.cli.image_file_facts(output, env)
    assert not case.calls and outside.read_bytes() == case.raw


def test_facts_rejects_replacement_after_its_single_read_without_reading_the_replacement(case):
    output = case.work / "image.png"
    output.write_bytes(case.raw)
    outside = case.root / "outside-secret.png"
    outside.write_bytes(b"must-never-be-read")
    original = case.native._read_regular_at
    seen = []

    def swap(*args, **kwargs):
        raw = original(*args, **kwargs)
        seen.append(raw)
        output.unlink()
        output.symlink_to(outside)
        return raw

    case.patch.setattr(case.native, "_read_regular_at", swap)
    with public_failure(case):
        case.cli.image_file_facts(output, case.env)
    assert seen == [case.raw] and outside.read_bytes() == b"must-never-be-read"


@pytest.mark.parametrize("denied", [False, True])
def test_existing_vision_reader_permissions_are_unchanged(case, denied):
    output = case.work / "frame.png"
    output.write_bytes(case.raw)
    if denied:
        case.patch.setenv("MEYO_CAPABILITY_POLICY_JSON", '{"read": false, "billable": true}')
        with pytest.raises(case.cli.MediaError, match="^media_image_read_denied$"):
            case.cli._checked_image_bytes(str(output))
    else:
        assert case.cli._checked_image_bytes(str(output)) == ("png", case.raw)
    assert not case.calls
