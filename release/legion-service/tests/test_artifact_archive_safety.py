"""Synthetic archives only: no network, subprocess, service, model or extraction."""
from collections import OrderedDict
import gzip
import hashlib
import importlib
import io
import os
from pathlib import Path
import socket
import stat
import subprocess
import tarfile
import zipfile
import zlib

import pytest


SERVICE = Path(__file__).resolve().parents[1]
SECRET = b'api_key="fixture-credential-7a93c81f56dd"\n'


def archive_bytes(kind, entries):
    """Create actual USTAR/ZIP bytes, not a mocked scanner response."""
    if "-" in kind:
        outer, inner = kind.split("-", 1)
        return archive_bytes(outer, [(f"package/nested.{inner}", archive_bytes(inner, entries))])
    target = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, body in entries:
                archive.writestr(name, body)
    else:
        with tarfile.open(fileobj=target, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, body in entries:
                item = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
                item.size = len(body)
                archive.addfile(item, io.BytesIO(body))
    raw = target.getvalue()
    return gzip.compress(raw, mtime=0) if kind == "tgz" else raw


def replace_tar_header_field(raw, start, end, value):
    """Keep a real USTAR header/checksum while changing exactly one byte field."""
    assert len(value) <= end - start
    changed = bytearray(raw)
    changed[start:end] = value.ljust(end - start, b"\0")
    changed[148:156] = b" " * 8
    changed[148:156] = ("%06o\0 " % sum(changed[:512])).encode("ascii")
    return bytes(changed)


@pytest.fixture(autouse=True)
def safety(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_")):
            monkeypatch.delenv(name)
    for name in ("HOME", "CODEX_HOME", "TMPDIR", "MEYO_LEGION_DIR", "LEGION_HOME_DIR",
                 "LEGION_SESSIONS_DIR", "LEGION_ARTIFACT_STORE_DIR"):
        directory = tmp_path / name
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.syspath_prepend(str(SERVICE))

    def forbidden(*args, **kwargs):
        raise AssertionError("archive tests must not use network/subprocess/extraction")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extract", forbidden)
    monkeypatch.setattr(tarfile.TarFile, "extractall", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extract", forbidden)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbidden)
    module = importlib.import_module("artifact_content_safety")
    monkeypatch.setattr(module, "_CACHE", OrderedDict())
    return module


def inspect(safety, tmp_path, raw, name="package.tgz"):
    path = tmp_path / name
    path.write_bytes(raw)
    before = path.stat()
    result = safety.check_file(str(path))
    assert path.read_bytes() == raw
    assert (path.stat().st_mtime_ns, path.stat().st_size) == (before.st_mtime_ns, len(raw))
    return result, path


def denied(safety, tmp_path, raw, reason, name="package.tgz"):
    result, path = inspect(safety, tmp_path, raw, name)
    assert result == {"allowed": False, "reason": reason,
                      "scan_status": "blocked" if reason == "artifact_sensitive_content" else "not_scanned"}
    assert "7a93c81f56dd" not in repr(result) and str(path) not in repr(result)
    with pytest.raises(safety.ArtifactUnsafe, match=f"^{reason}$"):
        safety.read_public_file(str(path))


@pytest.mark.parametrize(("kind", "name"), [
    ("tar", "package.tar"), ("tgz", "package.tgz"), ("tgz", "package.tar.gz"),
    ("tgz", "package.gzip"), ("tar", "renamed.bin"), ("tgz", "renamed.bin"),
])
def test_clean_npm_shape_is_scanned_and_download_keeps_exact_original_bytes(safety, tmp_path, kind, name):
    entries = [("package/package.json", b'{"name":"offline-fixture","version":"0.1.0"}')]
    entries += [(f"package/lib/module-{i}.js", b'export const value = "public";\n') for i in range(17)]
    raw = archive_bytes(kind, entries)
    result, path = inspect(safety, tmp_path, raw, name)
    assert result["allowed"] is True, result
    assert result["scan_status"] == "scanned"
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["bytes"] == len(raw)
    assert safety.read_public_file(str(path)) == raw
    assert not (tmp_path / "package").exists()


@pytest.mark.parametrize("kind", ["zip-tar", "zip-tgz", "tar-zip", "tgz-zip", "tar-tar", "tgz-tgz"])
def test_mixed_nested_archives_are_inspected_without_extraction(safety, tmp_path, kind):
    raw = archive_bytes(kind, [("package/readme.txt", b"public document")])
    result, path = inspect(safety, tmp_path, raw, "nested." + kind.split("-")[0])
    assert result["allowed"] is True, result
    assert safety.read_public_file(str(path)) == raw


@pytest.mark.parametrize("kind", ["tar", "tgz", "zip-tar", "zip-tgz", "tar-zip", "tgz-zip"])
def test_credentials_in_expanded_members_remain_blocked(safety, tmp_path, kind):
    raw = archive_bytes(kind, [("package/settings.txt", SECRET)])
    denied(safety, tmp_path, raw, "artifact_sensitive_content", "secret." + kind.split("-")[0])


@pytest.mark.parametrize("body", [
    b'api_key = process.env.API_KEY\npassword="${PASSWORD}"\n',
    b'{"token":"identifier","key":"public-concept","signature":"method-signature"}',
    "合法中文说明，未声明安装或发布授权。\n".encode(),
])
def test_public_code_placeholders_and_chinese_text_are_not_secrets(safety, tmp_path, body):
    raw = archive_bytes("tgz", [("package/readme.txt", body)])
    result, _ = inspect(safety, tmp_path, raw)
    assert result["allowed"] is True, result


def test_cross_chunk_and_encoded_credentials_are_checked_after_expansion(safety, tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "CHUNK_BYTES", 512)
    body = b" " * 505 + b"api%5fkey=fixture-credential-7a93c81f56dd\n"
    denied(safety, tmp_path, archive_bytes("tgz", [("package/data.bin", body)]),
           "artifact_sensitive_content")


@pytest.mark.parametrize("field", ["filename", "uname", "gname", "gzip-name", "gzip-comment", "gzip-extra"])
def test_archive_metadata_is_not_a_credential_hiding_place(safety, tmp_path, field):
    item = tarfile.TarInfo("package/public.txt")
    if field == "filename":
        item.name = "package/api_key=fixture-credential.txt"
    elif field in {"uname", "gname"}:
        setattr(item, field, "api_key=fixture-credential")
    raw = archive_bytes("tgz", [(item, b"public")])
    if field.startswith("gzip-"):
        payload = b"api_key=fixture-credential"
        flag = {"gzip-name": 8, "gzip-comment": 16, "gzip-extra": 4}[field]
        metadata = len(payload).to_bytes(2, "little") + payload if flag == 4 else payload + b"\0"
        raw = raw[:3] + bytes([flag]) + raw[4:10] + metadata + raw[10:]
    denied(safety, tmp_path, raw, "artifact_sensitive_content")


@pytest.mark.parametrize("kind", ["tar", "tgz"])
@pytest.mark.parametrize(("start", "end", "value"), [
    pytest.param(265, 297, b"X\0api_key=fixture_value9876", id="uname"),
    pytest.param(297, 329, b"X\0api_key=fixture_value9876", id="gname"),
    pytest.param(0, 100, b"package/index.js\0X\0api_key=fixture_value9876", id="name"),
])
def test_ustar_nul_tail_fixture_is_not_publicly_readable(safety, tmp_path, kind, start, end, value):
    raw = replace_tar_header_field(
        archive_bytes("tar", [("package/index.js", b"export const n = 1;\n")]),
        start, end, value)
    if kind == "tgz":
        raw = gzip.compress(raw, mtime=0)
    result, path = inspect(safety, tmp_path, raw, "nul-tail." + kind)
    # Exercise both public boundaries even in the red run. Report only whether
    # original bytes escaped, never include the downloaded bytes in failures.
    downloaded, read_reason = None, None
    try:
        downloaded = safety.read_public_file(str(path))
    except safety.ArtifactUnsafe as exc:
        read_reason = str(exc)
    assert path.read_bytes() == raw
    assert (result, read_reason, downloaded == raw) == (
        {"allowed": False, "reason": "artifact_archive_invalid", "scan_status": "not_scanned"},
        "artifact_archive_invalid", False)


@pytest.mark.parametrize("kind", ["tar", "tgz"])
@pytest.mark.parametrize(("start", "end", "value"), [
    pytest.param(157, 257, b"\0X\0api_key=fixture_value9876", id="linkname"),
    pytest.param(345, 500, b"package\0X\0api_key=fixture_value9876", id="prefix"),
])
def test_ustar_other_string_fields_cannot_hide_nul_tails(safety, tmp_path, kind, start, end, value):
    raw = replace_tar_header_field(
        archive_bytes("tar", [("package/index.js", b"public")]), start, end, value)
    denied(safety, tmp_path, gzip.compress(raw, mtime=0) if kind == "tgz" else raw,
           "artifact_archive_invalid", "string-tail." + kind)


@pytest.mark.parametrize("kind", ["tar", "tgz"])
@pytest.mark.parametrize(("start", "end"), [
    pytest.param(100, 108, id="mode"), pytest.param(108, 116, id="uid"),
    pytest.param(116, 124, id="gid"), pytest.param(124, 136, id="size"),
    pytest.param(136, 148, id="mtime"), pytest.param(148, 156, id="checksum"),
    pytest.param(329, 337, id="devmajor"), pytest.param(337, 345, id="devminor"),
])
def test_ustar_octal_fields_cannot_hide_ignored_nul_tails(safety, tmp_path, kind, start, end):
    raw = archive_bytes("tar", [("package/index.js", b"public")])
    if start == 148:
        # The checksum sum excludes these bytes; the numeric value is unchanged.
        raw = raw[:155] + b"X" + raw[156:]
    else:
        prefix = raw[start:end].split(b"\0", 1)[0].lstrip(b"0") or b"0"
        raw = replace_tar_header_field(raw, start, end, prefix + b"\0X")
    denied(safety, tmp_path, gzip.compress(raw, mtime=0) if kind == "tgz" else raw,
           "artifact_archive_invalid", "numeric-tail." + kind)


@pytest.mark.parametrize("kind", ["tar", "tgz"])
def test_ustar_unused_header_padding_cannot_carry_ignored_bytes(safety, tmp_path, kind):
    raw = replace_tar_header_field(
        archive_bytes("tar", [("package/index.js", b"public")]), 500, 512, b"\0X")
    denied(safety, tmp_path, gzip.compress(raw, mtime=0) if kind == "tgz" else raw,
           "artifact_archive_invalid", "header-padding." + kind)


@pytest.mark.parametrize("kind", ["tar", "tgz"])
def test_ustar_full_width_strings_and_canonical_numeric_padding_remain_supported(safety, tmp_path, kind):
    raw = archive_bytes("tar", [("package/index.js", b"public")])
    # No NUL is required if a string occupies its whole fixed-width field.
    for start, end, value in ((0, 100, b"n" * 100), (265, 297, b"u" * 32),
                              (297, 329, b"g" * 32), (345, 500, b"p" * 155)):
        raw = replace_tar_header_field(raw, start, end, value)
    for start, end in ((100, 108), (108, 116), (116, 124), (124, 136),
                       (136, 148), (329, 337), (337, 345)):
        prefix = raw[start:end].split(b"\0", 1)[0].lstrip(b"0") or b"0"
        raw = replace_tar_header_field(raw, start, end, prefix + b"\0 ")
    # The real writer's checksum ends with NUL + space, not all-zero padding.
    assert raw[154:156] == b"\0 "
    raw = gzip.compress(raw, mtime=0) if kind == "tgz" else raw
    result, path = inspect(safety, tmp_path, raw, "full-width." + kind)
    assert result["allowed"] is True, result
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert safety.read_public_file(str(path)) == raw


@pytest.mark.parametrize("field", ["gzip-comment", "gzip-extra", "gzip-extra-subfield"])
def test_gzip_metadata_is_scanned_at_its_own_field_boundary(safety, tmp_path, field):
    raw = archive_bytes("tgz", [("package/public.txt", b"public")])
    payload = b"api_key=fixture-credential"
    if field in {"gzip-extra", "gzip-extra-subfield"}:
        # Length 65 has an ASCII word byte immediately before the field.
        payload = payload.ljust(65, b" ")
        if field == "gzip-extra-subfield":
            payload = b"QA" + len(payload).to_bytes(2, "little") + payload
        flag, metadata = 4, len(payload).to_bytes(2, "little") + payload
    else:
        flag, metadata = 16, payload + b"\0"
    # A valid but unknown OS byte must not merge with a comment's first word.
    raw = raw[:3] + bytes([flag]) + raw[4:9] + b"A" + metadata + raw[10:]
    denied(safety, tmp_path, raw, "artifact_sensitive_content")


@pytest.mark.parametrize("kind", ["tar", "tgz", "zip"])
@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "a/../../escape", "a\\escape",
                                 "C:/escape", "a/C:stream", "a/\x01bad"])
def test_unsafe_member_paths_are_refused(safety, tmp_path, kind, name):
    denied(safety, tmp_path, archive_bytes(kind, [(name, b"public")]),
           "artifact_internal_path", "paths." + kind)


@pytest.mark.parametrize("kind", ["tar", "tgz", "zip"])
@pytest.mark.parametrize("names", [
    ("package/a.txt", "package/a.txt"), ("package/a.txt", "./package/a.txt"),
    ("package/a.txt", "package//a.txt"), ("package/a.txt", "package/A.txt"),
    ("package", "package/a.txt"), ("package/a.txt", "package"),
])
def test_duplicate_effective_paths_and_file_directory_collisions_fail_closed(safety, tmp_path, kind, names):
    with pytest.warns(UserWarning) if kind == "zip" and names[0] == names[1] else _no_warning():
        raw = archive_bytes(kind, [(name, b"public") for name in names])
    denied(safety, tmp_path, raw, "artifact_archive_invalid", "duplicate." + kind)


def _no_warning():
    from contextlib import nullcontext
    return nullcontext()


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                                  tarfile.BLKTYPE, tarfile.FIFOTYPE, tarfile.GNUTYPE_SPARSE,
                                  tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK,
                                  tarfile.XHDTYPE, tarfile.XGLTYPE, b"?"])
def test_tar_links_devices_sparse_and_unimplemented_extensions_are_refused(safety, tmp_path, kind):
    item = tarfile.TarInfo("package/member")
    item.type = kind
    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        item.linkname = "package/target"
    reason = "artifact_symlink_refused" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else "artifact_archive_unsupported"
    denied(safety, tmp_path, archive_bytes("tgz", [(item, b"")]), reason)


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFIFO])
def test_nested_zip_links_and_devices_are_not_hidden_by_tar(safety, tmp_path, kind):
    item = zipfile.ZipInfo("package/member")
    item.create_system = 3
    item.external_attr = (kind | 0o644) << 16
    raw = archive_bytes("tgz-zip", [(item, b"public")])
    reason = "artifact_symlink_refused" if kind == stat.S_IFLNK else "artifact_archive_unsupported"
    denied(safety, tmp_path, raw, reason)


def test_ustar_directories_are_supported_but_not_data_bearing_directories(safety, tmp_path):
    directory = tarfile.TarInfo("package/")
    directory.type = tarfile.DIRTYPE
    good = archive_bytes("tgz", [(directory, b""), ("package/a.txt", b"public")])
    result, _ = inspect(safety, tmp_path, good)
    assert result["allowed"] is True, result
    denied(safety, tmp_path, archive_bytes("tgz", [(directory, b"hidden body")]),
           "artifact_archive_invalid", "directory-body.tgz")


@pytest.mark.parametrize("corrupt_crc", [False, True])
def test_gzip_optional_fields_require_valid_header_crc(safety, tmp_path, corrupt_crc):
    raw = archive_bytes("tgz", [("package/a.txt", b"public")])
    extra = b"QA\x06\x00public"
    header = (raw[:3] + b"\x1e" + raw[4:10] + len(extra).to_bytes(2, "little")
              + extra + b"archive.tar\0public metadata\0")
    checksum = (zlib.crc32(header) & 0xffff) ^ int(corrupt_crc)
    raw = header + checksum.to_bytes(2, "little") + raw[10:]
    if corrupt_crc:
        denied(safety, tmp_path, raw, "artifact_archive_invalid")
    else:
        result, path = inspect(safety, tmp_path, raw)
        assert result["allowed"] is True, result
        assert safety.read_public_file(str(path)) == raw


@pytest.mark.parametrize("extra", [b"QA", b"QA\x08\x00x", b"QA\x00\x00x"])
def test_gzip_malformed_extra_subfields_are_not_silently_skipped(safety, tmp_path, extra):
    raw = archive_bytes("tgz", [("package/a.txt", b"public")])
    raw = raw[:3] + b"\x04" + raw[4:10] + len(extra).to_bytes(2, "little") + extra + raw[10:]
    denied(safety, tmp_path, raw, "artifact_archive_invalid")


def test_plain_gzip_is_not_accepted_as_a_tar_archive(safety, tmp_path):
    denied(safety, tmp_path, gzip.compress(b"public but not a tar archive", mtime=0),
           "artifact_archive_invalid", "plain.gz")


@pytest.mark.parametrize("damage", ["header", "crc", "isize", "truncate", "concat", "trailing", "zero-tail"])
def test_gzip_integrity_and_complete_single_stream_are_required(safety, tmp_path, damage):
    raw = bytearray(archive_bytes("tgz", [("package/a.txt", b"public")]))
    if damage == "header":
        raw[3] |= 32
    elif damage in ("crc", "isize"):
        raw[-8 if damage == "crc" else -1] ^= 1
    elif damage == "truncate":
        del raw[-3:]
    elif damage == "concat":
        raw.extend(gzip.compress(b"unchecked second stream", mtime=0))
    else:
        raw.extend(b"\0" if damage == "zero-tail" else b"trailing bytes")
    reason = "artifact_archive_unsupported" if damage in ("concat", "trailing", "zero-tail") else "artifact_archive_invalid"
    denied(safety, tmp_path, bytes(raw), reason)


@pytest.mark.parametrize("damage", ["checksum", "padding", "short-header", "short-body", "short-footer",
                                   "nonzero-footer", "trailing", "concat"])
def test_tar_structure_padding_and_full_termination_are_verified(safety, tmp_path, damage):
    raw = bytearray(archive_bytes("tar", [("package/a.txt", b"hello")]))
    if damage == "checksum":
        raw[100] ^= 1
    elif damage == "padding":
        raw[517] = 1
    elif damage == "short-header":
        raw = raw[:128]
    elif damage == "short-body":
        raw = raw[:515]
    elif damage == "short-footer":
        raw = raw[:1536]  # Only one end-of-archive zero block.
    elif damage == "nonzero-footer":
        raw[1536] = 1
    elif damage == "trailing":
        raw.extend(b"junk")
    else:
        raw.extend(archive_bytes("tar", [("package/unchecked.txt", b"second tar")]))
    denied(safety, tmp_path, gzip.compress(bytes(raw), mtime=0), "artifact_archive_invalid")


@pytest.mark.parametrize("kind", ["tar", "tgz", "zip"])
def test_archive_suffix_is_not_an_allowlist(safety, tmp_path, kind):
    denied(safety, tmp_path, b"not an archive", "artifact_archive_invalid", "fake." + kind)


@pytest.mark.parametrize("kind", ["tar", "tgz", "zip-tgz"])
def test_member_budget_is_shared_across_mixed_nesting(safety, tmp_path, monkeypatch, kind):
    monkeypatch.setattr(safety, "MAX_ARCHIVE_MEMBERS", 2)
    raw = archive_bytes(kind, [(f"package/{i}.txt", b"") for i in range(3)])
    denied(safety, tmp_path, raw, "artifact_archive_metadata_limit", "many." + kind.split("-")[0])


def test_gzip_expansion_is_bounded_by_actual_bytes_not_isize(safety, tmp_path, monkeypatch):
    raw = archive_bytes("tgz", [("package/large.bin", b"x" * (256 * 1024))])
    monkeypatch.setattr(safety, "MAX_SCAN_BYTES", 32 * 1024)
    assert len(raw) < safety.MAX_SCAN_BYTES
    denied(safety, tmp_path, raw, "artifact_archive_expanded_limit")


def test_declared_tar_size_is_rejected_before_body_allocation(safety, tmp_path, monkeypatch):
    item = tarfile.TarInfo("package/huge")
    item.size = 1 << 30
    raw = gzip.compress(item.tobuf(format=tarfile.USTAR_FORMAT) + b"\0" * 1024, mtime=0)
    monkeypatch.setattr(safety, "MAX_SCAN_BYTES", 32 * 1024)
    denied(safety, tmp_path, raw, "artifact_archive_expanded_limit")


@pytest.mark.parametrize("kind", ["tar", "tgz", "zip-tgz"])
def test_archive_metadata_has_a_shared_budget(safety, tmp_path, monkeypatch, kind):
    monkeypatch.setattr(safety, "MAX_ARCHIVE_DIRECTORY_BYTES", 512)
    raw = archive_bytes(kind, [("package/a.txt", b"public"), ("package/b.txt", b"public")])
    denied(safety, tmp_path, raw, "artifact_archive_metadata_limit", "metadata." + kind.split("-")[0])


def test_gzip_optional_header_cannot_exceed_metadata_budget(safety, tmp_path, monkeypatch):
    raw = archive_bytes("tgz", [("package/a.txt", b"public")])
    raw = raw[:3] + b"\x10" + raw[4:10] + b"x" * 2048 + b"\0" + raw[10:]
    monkeypatch.setattr(safety, "MAX_ARCHIVE_DIRECTORY_BYTES", 1024)
    denied(safety, tmp_path, raw, "artifact_archive_metadata_limit")


def test_mixed_nesting_preserves_depth_limit(safety, tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "MAX_ARCHIVE_DEPTH", 1)
    raw = archive_bytes("zip-tgz-tar", [("package/a.txt", b"public")])
    denied(safety, tmp_path, raw, "artifact_archive_depth_limit", "deep.zip")


def test_archive_deadline_is_not_reset_after_decompression(safety, tmp_path, monkeypatch):
    raw = archive_bytes("tgz", [("package/a.txt", b"public")])
    original = safety._sensitive_bytes
    clock = 0.0

    def scan(data):
        nonlocal clock
        if b"ustar" in data:
            clock += 10000.0
        return original(data)

    monkeypatch.setattr(safety, "_sensitive_bytes", scan)
    monkeypatch.setattr(safety.time, "monotonic", lambda: clock)
    denied(safety, tmp_path, raw, "artifact_security_scan_timeout")


def test_scan_cache_does_not_survive_changed_bytes_or_tighter_limits(safety, tmp_path, monkeypatch):
    raw = archive_bytes("tgz", [("package/a.txt", b"public")])
    result, path = inspect(safety, tmp_path, raw)
    assert result["allowed"] is True, result
    monkeypatch.setattr(safety, "MAX_ARCHIVE_MEMBERS", 0)
    assert safety.check_file(str(path))["reason"] == "artifact_archive_metadata_limit"
    monkeypatch.setattr(safety, "MAX_ARCHIVE_MEMBERS", 2048)
    path.write_bytes(archive_bytes("tgz", [("package/a.txt", SECRET)]))
    assert safety.check_file(str(path))["reason"] == "artifact_sensitive_content"


def test_same_descriptor_change_and_filesystem_symlink_still_fail_closed(safety, tmp_path, monkeypatch):
    raw = archive_bytes("tgz", [("package/a.txt", b"public")])
    path = tmp_path / "original.tgz"
    path.write_bytes(raw)
    link = tmp_path / "alias.tgz"
    link.symlink_to(path)
    assert safety.check_file(str(link))["allowed"] is False
    original = safety._sensitive_bytes
    changed = False

    def scan(data):
        nonlocal changed
        if not changed:
            changed = True
            path.write_bytes(raw + b"changed during scan")
        return original(data)

    monkeypatch.setattr(safety, "_sensitive_bytes", scan)
    assert safety.check_file(str(path))["reason"] == "artifact_security_changed"
