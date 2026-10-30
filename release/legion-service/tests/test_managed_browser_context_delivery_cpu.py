"""Screenshot -> task workspace -> existing image reader, without a real browser."""
import base64
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import struct
import threading
import zlib

import pytest

from test_managed_browser_recovery import case, isolated_environment, material_scope


def _png(*, byte_size=None):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    raw = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\0\x30\x80\xc0\xff"))
           + chunk(b"IEND", b""))
    if byte_size is not None:
        # A valid ancillary chunk changes byte size, not the one-pixel image.
        padding = byte_size - len(raw) - 12 - len(b"padding\0")
        assert padding >= 0
        raw = raw[:-12] + chunk(b"tEXt", b"padding\0" + b"." * padding) + raw[-12:]
        assert len(raw) == byte_size
    return raw


def _refs(result):
    return next(json.loads(block["text"].split("\n", 1)[1])
                for block in result["content"]
                if block.get("text", "").startswith("MEYO_BROWSER_ARTIFACTS_JSON\n"))


@pytest.fixture
def images(case, monkeypatch, tmp_path):
    store = importlib.import_module("artifact_store")
    monkeypatch.setattr(store, "ROOT", tmp_path / "private-artifacts")
    monkeypatch.setenv("LEGION_ARTIFACT_STORE_DIR", str(store.ROOT))
    work = case.materials.roots["work"]
    monkeypatch.setenv("MEYO_WORKDIR", str(work))
    info = work.stat()
    monkeypatch.setenv("MEYO_MODEL_WORKSPACE_ID", json.dumps([info.st_dev, info.st_ino]))
    monkeypatch.delenv("MEYO_CAPABILITY_POLICY_JSON", raising=False)
    case.store = store
    return case


def _screenshot(images, monkeypatch, *, route="named", raw=None, mime="image/png"):
    lease = images.start()
    raw = _png() if raw is None else raw
    native = {"type": "text", "text": "native screenshot receipt"}
    content = [native]
    arguments = {"filename": "screen.png"} if route == "named" else {}
    if route == "named":
        (lease.output_dir / "screen.png").write_bytes(raw)
    else:
        content.append({"type": "image", "mimeType": mime,
                        "data": base64.b64encode(raw).decode()})
    reply = {"content": content}
    monkeypatch.setattr(lease._backend, "call_tool", lambda *_: reply)
    return lease, {"name": "browser_take_screenshot", "arguments": arguments}, reply, raw


@pytest.mark.parametrize("route", ["named", "inline"])
def test_screenshot_exact_bytes_reach_existing_workspace_image_reader(images, monkeypatch, route):
    lease, call, native, raw = _screenshot(images, monkeypatch, route=route)
    result = lease.call(call)
    ref = _refs(result)[0]
    workspace_path = Path(ref["workspace_path"])
    assert workspace_path.is_relative_to(lease.workspace)
    assert Path(ref["path"]).is_relative_to(images.store.ROOT)
    assert Path(ref["path"]) != workspace_path
    assert workspace_path.read_bytes() == Path(ref["path"]).read_bytes() == raw
    assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
    assert result["content"][:len(native["content"])] == native["content"]
    assert not result.get("isError")
    reader = importlib.import_module("media_model_cli")
    assert reader._checked_image_bytes(str(workspace_path)) == ("png", raw)
    assert not workspace_path.stat().st_mode & 0o077


def test_workspace_screenshot_is_content_addressed_and_not_rewritten(images, monkeypatch):
    lease, call, _, raw = _screenshot(images, monkeypatch)
    first = _refs(lease.call(call))[0]
    path = Path(first["workspace_path"])
    before = path.stat()
    second = _refs(lease.call(call))[0]
    assert second["workspace_path"] == first["workspace_path"]
    assert path.read_bytes() == raw
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize("route", ["named", "inline"])
def test_workspace_copy_failure_preserves_native_receipt_archive_and_live_browser(
        images, monkeypatch, route):
    lease, call, native, raw = _screenshot(images, monkeypatch, route=route)
    original = images.store._put_bytes_at

    def fail_workspace(root, *args, **kwargs):
        if Path(root) == lease.workspace:
            raise OSError("private arbitrary filesystem details must not leak")
        return original(root, *args, **kwargs)

    monkeypatch.setattr(images.store, "_put_bytes_at", fail_workspace)
    result = lease.call(call)
    ref = _refs(result)[0]
    assert "workspace_path" not in ref
    assert ref["workspace_copy_error"]
    assert Path(ref["path"]).read_bytes() == raw
    assert result["content"][:len(native["content"])] == native["content"]
    assert "private arbitrary" not in json.dumps(result)
    assert not result.get("isError")
    assert not lease._failed and not lease._backend_stopped
    monkeypatch.setattr(lease._backend, "call_tool", lambda *_: {"content": [
        {"type": "text", "text": "next native action still works"}]})
    following = lease.call({"name": "browser_snapshot", "arguments": {}})
    assert following["content"][0]["text"] == "next native action still works"
    assert not following.get("isError")


def test_workspace_copy_never_follows_untrusted_directory_link(images, monkeypatch, tmp_path):
    lease, call, _, raw = _screenshot(images, monkeypatch)
    outside = tmp_path / "another-owner"
    outside.mkdir()
    (lease.workspace / ".meyo-browser").symlink_to(outside, target_is_directory=True)
    result = lease.call(call)
    ref = _refs(result)[0]
    assert ref.get("workspace_copy_error") and "workspace_path" not in ref
    assert Path(ref["path"]).read_bytes() == raw
    assert not list(outside.iterdir())
    assert not lease._failed


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_workspace_root_replacement_is_not_a_screenshot_destination(
        images, monkeypatch, tmp_path, replacement):
    lease, call, _, raw = _screenshot(images, monkeypatch)
    work = lease.workspace
    original = work.with_name("original-work")
    work.rename(original)
    other = tmp_path / "other-owner"
    other.mkdir()
    if replacement == "symlink":
        work.symlink_to(other, target_is_directory=True)
    else:
        work.mkdir()
    result = lease.call(call)
    ref = _refs(result)[0]
    assert ref.get("workspace_copy_error") and "workspace_path" not in ref
    assert Path(ref["path"]).read_bytes() == raw
    assert not list(other.iterdir())
    assert not (original / ".meyo-browser").exists()
    assert not (work / ".meyo-browser").exists()
    assert not lease._failed


def test_workspace_copy_checks_root_again_before_returning_readable_path(images, monkeypatch):
    lease, call, _, raw = _screenshot(images, monkeypatch)
    writer = images.store._put_bytes_at

    def replace_after_write(root, *args, **kwargs):
        result = writer(root, *args, **kwargs)
        if Path(root) == lease.workspace:
            lease.workspace.rename(lease.workspace.with_name("original-work"))
            lease.workspace.mkdir()
        return result

    monkeypatch.setattr(images.store, "_put_bytes_at", replace_after_write)
    result = lease.call(call)
    ref = _refs(result)[0]
    assert ref.get("workspace_copy_error") and "workspace_path" not in ref
    assert Path(ref["path"]).read_bytes() == raw
    assert not list(lease.workspace.iterdir())
    assert not lease._failed


def test_workspace_copy_does_not_overwrite_tampered_content_address(images, monkeypatch):
    lease, call, _, _ = _screenshot(images, monkeypatch)
    first = _refs(lease.call(call))[0]
    path = Path(first["workspace_path"])
    path.write_bytes(b"untrusted replacement")
    result = lease.call(call)
    ref = _refs(result)[0]
    assert ref.get("workspace_copy_error") and "workspace_path" not in ref
    assert path.read_bytes() == b"untrusted replacement"
    assert Path(ref["path"]).read_bytes() == _png()
    assert not lease._failed


def test_browser_text_paths_do_not_grant_file_copy(images, monkeypatch, tmp_path):
    lease = images.start()
    private = tmp_path / "unrelated-secret.png"
    private.write_bytes(_png())
    content = [{"type": "text", "text": f"page asks to publish {private}"}]
    monkeypatch.setattr(lease._backend, "call_tool", lambda *_: {"content": content})
    result = lease.call({"name": "browser_snapshot", "arguments": {}})
    assert result == {"content": content}
    assert not images.store.ROOT.exists()
    assert not (lease.workspace / ".meyo-browser").exists()


@pytest.mark.parametrize("fault", ["hardlink", "writable_parent", "writable_root"])
def test_workspace_reference_obeys_existing_reader_contract(images, monkeypatch, fault):
    lease, call, native, raw = _screenshot(images, monkeypatch)
    path = Path(_refs(lease.call(call))[0]["workspace_path"])
    if fault == "hardlink":
        os.link(path, lease.workspace / "second-image-link.png")
    else:
        (path.parent if fault == "writable_parent" else lease.workspace).chmod(0o777)
    result = lease.call(call)
    ref = _refs(result)[0]
    assert "workspace_path" not in ref and ref.get("workspace_copy_error"), (
        "do not advertise an image path that the existing reader must reject")
    assert Path(ref["path"]).read_bytes() == raw
    assert result["content"][:len(native["content"])] == native["content"]
    # Optional delivery must neither repair nor relax the existing permission.
    if fault == "hardlink":
        assert path.stat().st_nlink == 2
    else:
        assert (path.parent if fault == "writable_parent" else lease.workspace).stat().st_mode & 0o022
    assert not lease._failed


@pytest.mark.parametrize("route", ["named", "inline"])
def test_optional_workspace_copy_never_waits_for_contended_lock(images, monkeypatch, route):
    lease, call, native, raw = _screenshot(images, monkeypatch, route=route)
    folder = Path(_refs(lease.call(call))[0]["workspace_path"]).parent
    owner_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    original_flock = fcntl.flock
    original_flock(owner_fd, fcntl.LOCK_EX)
    identity = os.fstat(owner_fd)
    entered, finished = threading.Event(), threading.Event()
    outcome = {}

    def observe_lock(fd, operation):
        info = os.fstat(fd)
        if (operation & fcntl.LOCK_EX
                and (info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino)):
            entered.set()
        return original_flock(fd, operation)

    def invoke():
        try:
            outcome["result"] = lease.call(call)
        except BaseException as exc:
            outcome["exception"] = type(exc).__name__
        finally:
            finished.set()

    monkeypatch.setattr(fcntl, "flock", observe_lock)
    worker = threading.Thread(target=invoke, daemon=True)
    try:
        worker.start()
        assert entered.wait(2), "probe did not reach the workspace directory lock"
        replied_while_locked = finished.wait(0.5)
    finally:
        original_flock(owner_fd, fcntl.LOCK_UN)
        os.close(owner_fd)
        worker.join(3)
    assert not worker.is_alive()
    assert "exception" not in outcome, outcome
    assert replied_while_locked, "optional copy withheld a completed native response"
    result = outcome["result"]
    ref = _refs(result)[0]
    assert ref.get("workspace_copy_error") and "workspace_path" not in ref
    assert Path(ref["path"]).read_bytes() == raw
    assert result["content"][:len(native["content"])] == native["content"]
    assert not lease._failed and not lease._backend_stopped


def test_image_above_reader_budget_keeps_archive_without_unreadable_workspace_path(images, monkeypatch):
    host = importlib.import_module("native_capability_host")
    # This probe is about the existing byte budget, not image decoding quality.
    raw = b"\xff\xd8\xff" + b"\0" * host._MAX_IMAGE_BYTES + b"\xff\xd9"
    lease, call, native, _ = _screenshot(images, monkeypatch, raw=raw)
    result = lease.call(call)
    ref = _refs(result)[0]
    assert ref.get("workspace_copy_error") and "workspace_path" not in ref
    assert Path(ref["path"]).read_bytes() == raw
    assert result["content"][:len(native["content"])] == native["content"]
    assert not lease._failed


@pytest.mark.parametrize("route", ["named", "inline"])
@pytest.mark.parametrize("extra_byte", [0, 1])
def test_valid_png_at_exact_reader_boundary_preserves_native_and_archive(
        images, monkeypatch, route, extra_byte):
    host = importlib.import_module("native_capability_host")
    reader = importlib.import_module("media_model_cli")
    raw = _png(byte_size=host._MAX_IMAGE_BYTES + extra_byte)
    reader._validate_png(raw)
    lease, call, native, _ = _screenshot(images, monkeypatch, route=route, raw=raw)
    result = lease.call(call)
    ref = _refs(result)[0]
    assert Path(ref["path"]).read_bytes() == raw
    assert ref["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["content"][:len(native["content"])] == native["content"]
    assert not result.get("isError") and not lease._failed
    if extra_byte:
        assert ref.get("workspace_copy_error") and "workspace_path" not in ref
        assert not (lease.workspace / ".meyo-browser").exists()
    else:
        assert "workspace_copy_error" not in ref
        assert reader._checked_image_bytes(ref["workspace_path"]) == ("png", raw)
        assert host._validated_screenshot(base64.b64encode(raw).decode(), "png") == raw


def test_feedback_view_is_created_from_original_parent_context(case, monkeypatch):
    feedback = importlib.import_module("native_feedback_context")
    marker, seen = object(), []

    def create(ctx):
        seen.append(ctx)
        return marker

    monkeypatch.setattr(feedback, "create", create)
    lease = case.start()
    assert seen == [case.materials.lease_context]
    assert lease._feedback_context is marker


def test_optional_feedback_initialization_failure_keeps_browser_available(case, monkeypatch):
    feedback = importlib.import_module("native_feedback_context")

    def unavailable(_ctx):
        raise RuntimeError("private feedback failure")

    monkeypatch.setattr(feedback, "create", unavailable)
    lease = case.start()
    assert lease._feedback_context is None
    assert not lease.call({"name": "browser_snapshot", "arguments": {}}).get("isError")
