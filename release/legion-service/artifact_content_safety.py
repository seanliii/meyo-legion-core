"""Bounded, read-only artifact credential screening shared by discovery and HTTP.

This is a disclosure boundary, not a content-quality judgement. It recognizes
credential-bearing text, including text embedded in binary media. It does not
validate credentials by using them, rewrite files, or inspect user key stores.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import html
import io
import json
import os
import pathlib
import re
import stat
import tarfile
import threading
import time
import struct
import unicodedata
import zipfile
import zlib
from urllib.parse import unquote

MAX_SCAN_BYTES = 128 * 1024 * 1024
SCAN_SECONDS = 3.0
# Startup allowance plus a deliberately conservative 1 MiB/s scan allowance.
# The 25 MiB upload-body / 64 MiB desktop-transfer limits must not inherit a
# single small-file deadline. At MAX_SCAN_BYTES the budget is still <=131s.
SCAN_BYTES_PER_SECOND = 1024 * 1024
CHUNK_BYTES = 64 * 1024
OVERLAP = 8192
CACHE_ENTRIES = 256
MAX_ARCHIVE_MEMBERS = 2048
MAX_ARCHIVE_DIRECTORY_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_DEPTH = 2
_CACHE = OrderedDict()
_LOCK = threading.RLock()


def sensitive_key(name: str) -> bool:
    # Shared with server._public_source_url's existing credential field policy.
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(name))
    key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return (key in {"token", "key", "sig", "signature", "auth", "authorization", "password",
                    "passwd", "secret", "apikey", "credential", "cookie", "set_cookie"}
            or key.endswith(("_token", "_key", "_signature", "_secret", "_credential", "_appid"))
            or key in {"appid", "aigc_appid"}
            or "password" in key or "client_secret" in key or "private_token" in key)


_PLACEHOLDER = re.compile(
    r"(?ix)^(?:\$\{[^{}\r\n]+\}|\{\{[^{}\r\n]+\}\}|<[^<>\r\n]+>|"
    r"\[(?:redacted|hidden|masked)\]|\$[A-Z_][A-Z_0-9]*|"
    r"(?:your|insert|replace|example|dummy|fake|test|placeholder)[_-](?:api[_-]?)?"
    r"(?:key|token|password|secret|signature|credential|here)|"
    r"redacted|hidden|masked|placeholder|changeme|example|dummy|fake|test|"
    r"signature|api_key|access_token|refresh_token|password|secret|token|key|"
    r"none|null|true|false|\*+|x{3,}|\.{3,})$")
# Ordinary JSON/code keys are not credentials: key/token/signature/appid also
# name model concepts, identifiers and public aliases. Only explicit credential
# labels belong here; URL query fields keep the broader policy below.
_CREDENTIAL_LABEL = (
    r"(?:(?:[a-z][a-z0-9]{0,31}[_-]){0,4}"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|private[_-]?key|secret[_-]?key|password|passwd)|"
    r"authorization|credential|cookie|set[_-]?cookie|aigc_appid|meyo_unified_appid)"
)
_PAIR = re.compile(
    r'''(?ix)(?<![\w])(''' + _CREDENTIAL_LABEL + r''')["']?\s*[:=]\s*'''
    r'''(?:"((?:\\.|[^"\\\r\n]){0,8192})"|'((?:\\.|[^'\\\r\n]){0,8192})'|'''
    r'''(\$\{[^{}\r\n]{1,256}\}|\{\{[^{}\r\n]{1,256}\}\}|<[^<>\r\n]{1,256}>|[^\s"'&<>`,;\\]{1,4096}))''')
_QUERY = re.compile(
    r'''(?ix)[?&#]([a-z][a-z0-9_.-]{0,95})='''
    r'''(\$\{[^{}\r\n]{1,256}\}|\{\{[^{}\r\n]{1,256}\}\}|<[^<>\r\n]{1,256}>|[^\s"'&<>`,;\\]{1,4096})''')
_AUTH = re.compile(
    r'''(?i)\b(?:proxy[-_]?)?authorization["']?\s*[:=]\s*["']?'''
    r'''(?:bearer|basic)\s+([^\s"'<>`,;]{1,4096})''')
_TOKEN = re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                    r"github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}|"
                    r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,})\b")
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")


def _literal(value: str, minimum: int = 8, *, code_references: bool = False) -> bool:
    value = value.strip().rstrip(").]}") if not value.startswith(("${", "{{", "<", "[")) else value.strip()
    if not value or _PLACEHOLDER.fullmatch(value):
        return False
    # Code references are not resolved credentials. No broad 'example host' bypass:
    # a real key embedded in a documentation URL is still a disclosure.
    if code_references and re.match(r"(?i)^(?:os\.environ|os\.getenv|process\.env|env\[|getenv\(|config\[|settings\.)", value):
        return False
    if code_references and re.match(r"^[A-Za-z_][\w.]*\[", value):
        return False
    if code_references and re.match(r"^[A-Za-z_][\w.]*\(", value):
        return False
    if code_references and re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", value):
        return False
    return len(value) >= minimum


def _normalized_text(text: str) -> str:
    # JSON escaped '/', HTML query escaping, percent encoding and UTF-16 ASCII
    # are common in tool logs. Bounded by the chunk/window supplied by the scanner.
    text = text.replace("\x00", "").replace("\\/", "/")
    if "\\u00" in text:
        text = re.sub(r"\\u00([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), text)
    for _ in range(2):
        if "%" in text:
            # Decode percent runs only. urllib.unquote over binary-decoded text
            # repeatedly splits every non-ASCII span, making ordinary MP4s slow.
            text = re.sub(r"(?:%[0-9a-fA-F]{2})+", lambda m: unquote(m.group()), text)
        if "&" in text:
            # Binary payloads contain random ampersands, not thousands of HTML
            # entities. Decode only entity candidates; keep the same two passes.
            text = re.sub(r"&(?:\#[xX][0-9a-fA-F]{1,8}|\#[0-9]{1,8}|[A-Za-z][A-Za-z0-9]{0,31});?",
                          lambda m: html.unescape(m.group()), text)
    return text


def sensitive_text(text: str) -> bool:
    text = _normalized_text(text)
    lower = text.lower()
    if ("PRIVATE KEY-----" in text and _PRIVATE_KEY.search(text)) or (
            any(prefix in text for prefix in ("sk-", "gh", "AKIA", "eyJ")) and _TOKEN.search(text)):
        return True
    if ("bearer" in lower or "basic" in lower) and any(
            _literal(m.group(1), 12) for m in _AUTH.finditer(text)):
        return True
    # An outer JSON field or "https:" match may consume the entire URL;
    # query parameters therefore need their own pass.
    pairs = _PAIR.finditer(text) if any(label in lower for label in (
        "key", "token", "secret", "password", "passwd", "authorization", "credential",
        "cookie", "appid")) else ()
    for match in pairs:
        key, double, single, bare = match.groups()
        value = double if double is not None else single if single is not None else bare
        if double is not None:
            try:
                value = json.loads('"' + double + '"')
            except ValueError:
                pass
        # These two runtime fields can also hold the public alias, not its key.
        if key.lower() in {"aigc_appid", "meyo_unified_appid"} and re.fullmatch(
                r"appid-[A-Za-z0-9_-]+", value):
            continue
        if _literal(value, 4 if "password" in key.lower() or key.lower() == "passwd" else 8,
                    code_references=bare is not None):
            return True
    for match in _QUERY.finditer(text):
        key, value = match.groups()
        if sensitive_key(key) and _literal(value, 4 if key.lower() in {"password", "passwd"} else 8):
            return True
    return False


def _sensitive_bytes(data: bytes) -> bool:
    # A cheap necessary-condition filter, not a media-type exemption: every
    # detector below requires one of these ASCII markers after normalization.
    # latin-1 preserves every byte and avoids utf-8 replacement expansion for
    # compressed/video payloads. Still examine every byte and every chunk.
    candidate = _normalized_text(data.decode("latin-1")).lower()
    if not any(marker in candidate for marker in (
            "key", "token", "secret", "password", "passwd", "authorization",
            "credential", "cookie", "appid", "sig", "auth", "bearer", "basic",
            "sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "akia", "eyj")):
        return False
    return sensitive_text(data.decode("utf-8", errors="replace"))


class ArtifactUnsafe(OSError):
    """Safe reason only: never include a path, URL, credential or file excerpt."""


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _open(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    return os.fdopen(fd, "rb")


_UNSUPPORTED_ARCHIVE_SUFFIXES = {".7z", ".rar", ".bz2", ".xz", ".zst", ".zstd"}
_ZIP_SUFFIXES = {".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub", ".jar", ".whl"}
_GZIP_SUFFIXES = {".gz", ".gzip", ".tgz"}

# The existing export path policy also applies inside every supported archive.
# Keep its single definition here: discovery, project export and HTTP all use
# this scanner. artifact_delivery retains aliases for its directory traversal.
_SKIP_DIRS = ("__pycache__", ".git", ".gnhf", "node_modules", ".venv", "venv",
              "sessions", ".sessions", ".cache", "state", "logs", ".search_fabric_runtime", "traces",
              "peer_sources", ".omniagent", "component_crx_cache", "BrowserMetrics",
              "GPUPersistentCache", "GPUCache", "Code Cache", "Crashpad")
_INTERNAL_DIR_NAMES = {name.casefold() for name in _SKIP_DIRS}
_INTERNAL_DIR_PREFIXES = (".aider", ".export-")
_INTERNAL_FILE_NAMES = {"preferences", "secure preferences", "local state", "variations",
                        "chromium.log", "deepagents-messages.json"}
_TERMINAL_SNAP_PREFIX = "_terminal_snapshot_"


def _is_internal_dir_name(name: str) -> bool:
    folded = str(name or "").casefold()
    return folded in _INTERNAL_DIR_NAMES or any(
        folded.startswith(prefix) for prefix in _INTERNAL_DIR_PREFIXES)


def _export_internal_path(relative) -> bool:
    parts = relative.parts
    return (any(p.startswith(".") or _is_internal_dir_name(p) for p in parts)
            or bool(parts and (
                parts[-1].casefold() in _INTERNAL_FILE_NAMES
                or parts[-1] == "state.json"
                or parts[-1].endswith((".progress.json", ".sidecar.json", ".meta.json"))
                or parts[-1].startswith(_TERMINAL_SNAP_PREFIX)))
            or sensitive_text(str(relative)))


def _archive_kind(head: bytes, name: str = "") -> str:
    suffix = os.path.splitext(name)[1].lower()
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")) or suffix in _ZIP_SUFFIXES:
        return "zip"
    if head.startswith(b"\x1f\x8b") or suffix in _GZIP_SUFFIXES:
        return "gzip"
    if head[257:262] == b"ustar" or suffix == ".tar":
        return "tar"
    if (suffix in _UNSUPPORTED_ARCHIVE_SUFFIXES
            or head.startswith((b"BZh", b"\xfd7zXZ\x00", b"7z\xbc\xaf\x27\x1c",
                                b"Rar!\x1a\x07", b"\x28\xb5\x2f\xfd"))):
        return "unsupported"
    # A renamed pre-USTAR header must not become an ordinary binary exemption.
    if len(head) == 512 and any(head):
        try:
            tarfile.TarInfo.frombuf(head, "utf-8", "strict")
            return "tar"
        except (tarfile.HeaderError, ValueError, OverflowError):
            pass
    return ""


def _archive_tick(state):
    if time.monotonic() >= state["deadline"]:
        raise ArtifactUnsafe("artifact_security_scan_timeout")


def _archive_allowance(state, additional=0):
    if SCAN_SECONDS > 0:
        state["deadline"] = max(state["deadline"], state["start"] + SCAN_SECONDS
            + (state["raw_size"] + min(MAX_SCAN_BYTES, state["expanded"] + additional))
            / SCAN_BYTES_PER_SECOND)


def _archive_metadata(state, size):
    _archive_tick(state)
    state["metadata"] += size
    if state["metadata"] > MAX_ARCHIVE_DIRECTORY_BYTES:
        raise ArtifactUnsafe("artifact_archive_metadata_limit")


def _archive_member_path(name, is_dir, seen):
    """Validate effective paths without following or materializing any member."""
    if sensitive_text(name):
        raise ArtifactUnsafe("artifact_sensitive_content")
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ArtifactUnsafe("artifact_internal_path")
    parts = [p for p in name.split("/") if p not in {"", "."}]
    if not parts or ".." in parts:
        raise ArtifactUnsafe("artifact_internal_path")
    key = unicodedata.normalize("NFC", "/".join(parts)).casefold()
    if (key in seen or any(seen.get(key[:i]) is False for i, c in enumerate(key) if c == "/")
            or (not is_dir and any(p.startswith(key + "/") for p in seen))):
        raise ArtifactUnsafe("artifact_archive_invalid")
    seen[key] = is_dir
    # OOXML relationships are package descriptors, not private dotfiles.
    if len(parts) > 1 and parts[-2:] == ["_rels", ".rels"]:
        parts[-1] = "relationships.xml"
    if _export_internal_path(pathlib.PurePosixPath(*parts)):
        raise ArtifactUnsafe("artifact_internal_path")


def _archive_chunk(chunk, state, tail):
    state["expanded"] += len(chunk)
    if state["expanded"] > MAX_SCAN_BYTES:
        raise ArtifactUnsafe("artifact_archive_expanded_limit")
    _archive_allowance(state)
    window = tail + chunk
    if _sensitive_bytes(window):
        raise ArtifactUnsafe("artifact_sensitive_content")
    return window[-OVERLAP:]


def _inspect_member(stream, size, name, state, depth):
    if size < 0:
        raise ArtifactUnsafe("artifact_archive_invalid")
    if size + state["expanded"] > MAX_SCAN_BYTES:
        raise ArtifactUnsafe("artifact_archive_expanded_limit")
    _archive_allowance(state, size)
    # Shared expansion bounds also bound retained buffers. Inspect the complete
    # member so renamed/self-extracting ZIPs cannot hide behind a missing magic.
    with io.BytesIO() as contents:
        remaining, tail = size, b""
        while remaining:
            _archive_tick(state)
            chunk = stream.read(min(CHUNK_BYTES, remaining))
            if not chunk:
                raise ArtifactUnsafe("artifact_archive_invalid")
            remaining -= len(chunk)
            tail = _archive_chunk(chunk, state, tail)
            contents.write(chunk)
        _inspect_archive(contents, name, state, depth + 1)


def _inspect_zip(stream, state, depth=0):
    """Inspect ZIP/OOXML members without extracting; bound metadata before ZipFile allocation."""
    if depth > MAX_ARCHIVE_DEPTH:
        raise ArtifactUnsafe("artifact_archive_depth_limit")
    _archive_tick(state)
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - 65557))
    tail = stream.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise ArtifactUnsafe("artifact_archive_invalid")
    _, disk, directory_disk, disk_count, count, directory_size, _, comment_size = struct.unpack_from(
        "<4s4H2LH", tail, offset)
    if (disk or directory_disk or disk_count != count or count == 65535
            or directory_size == 0xFFFFFFFF or offset + 22 + comment_size != len(tail)):
        raise ArtifactUnsafe("artifact_archive_unsupported")
    if offset >= 20 and tail[offset - 20:offset - 16] == b"PK\x06\x07":
        raise ArtifactUnsafe("artifact_archive_unsupported")  # ZIP64 needs separate bounded metadata support.
    if count + state["members"] > MAX_ARCHIVE_MEMBERS:
        raise ArtifactUnsafe("artifact_archive_metadata_limit")
    _archive_metadata(state, directory_size)
    try:
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            entries = archive.infolist()
            if len(entries) != count:
                raise ArtifactUnsafe("artifact_archive_invalid")
            state["members"] += len(entries)
            declared = sum(item.file_size for item in entries)
            if declared + state["expanded"] > MAX_SCAN_BYTES:
                raise ArtifactUnsafe("artifact_archive_expanded_limit")
            # Budget is finite for compressed + inspected expanded bytes, not
            # the tiny compressed size alone. Recursion shares all limits.
            _archive_allowance(state, declared)
            seen = {}
            for item in entries:
                _archive_tick(state)
                if item.flag_bits & 1:
                    raise ArtifactUnsafe("artifact_archive_encrypted")
                if item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ArtifactUnsafe("artifact_archive_unsupported")
                if _sensitive_bytes(item.comment) or _sensitive_bytes(item.extra):
                    raise ArtifactUnsafe("artifact_sensitive_content")
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ArtifactUnsafe("artifact_symlink_refused")
                if (stat.S_IFMT(mode) not in {0, stat.S_IFDIR if item.is_dir() else stat.S_IFREG}
                        or mode & 0o6000):
                    raise ArtifactUnsafe("artifact_archive_unsupported")
                _archive_member_path(item.orig_filename, item.is_dir(), seen)
                if item.is_dir():
                    if item.file_size:
                        raise ArtifactUnsafe("artifact_archive_invalid")
                    continue
                with archive.open(item) as member:
                    _inspect_member(member, item.file_size, item.filename, state, depth)
                    if member.read(1):
                        raise ArtifactUnsafe("artifact_archive_invalid")
    except ArtifactUnsafe:
        raise
    except (OSError, ValueError, RuntimeError, NotImplementedError, EOFError, zipfile.BadZipFile, zlib.error):
        raise ArtifactUnsafe("artifact_archive_unverifiable") from None


def _read_archive_metadata(stream, size, state):
    _archive_metadata(state, size)
    value = stream.read(size)
    if len(value) != size:
        raise ArtifactUnsafe("artifact_archive_invalid")
    return value


def _inspect_tar(stream, state, depth):
    """Only ordinary USTAR files/directories; never eager extension parsing."""
    size = stream.seek(0, os.SEEK_END)
    stream.seek(0)
    if size < 1024 or size % 512:
        raise ArtifactUnsafe("artifact_archive_invalid")
    seen = {}
    while True:
        header = _read_archive_metadata(stream, 512, state)
        if header == bytes(512):
            if _read_archive_metadata(stream, 512, state) != bytes(512):
                raise ArtifactUnsafe("artifact_archive_invalid")
            while stream.tell() < size:
                if any(_read_archive_metadata(stream, min(CHUNK_BYTES, size - stream.tell()), state)):
                    raise ArtifactUnsafe("artifact_archive_invalid")
            return
        # frombuf validates one fixed-size header only. TarFile.next would
        # eagerly allocate PAX/GNU extension bodies or silently skip them.
        try:
            item = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        except (tarfile.HeaderError, ValueError, OverflowError):
            raise ArtifactUnsafe("artifact_archive_invalid") from None
        # NUL-padded header fields must not merge into adjacent numeric/text
        # fields during the byte scanner's existing UTF-16 normalization.
        if sensitive_text(item.uname) or sensitive_text(item.gname):
            raise ArtifactUnsafe("artifact_sensitive_content")
        if item.type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
            raise ArtifactUnsafe("artifact_symlink_refused")
        if (header[257:265] != b"ustar\x0000"
                or header[156:157] not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}
                or item.linkname or item.devmajor or item.devminor or item.mode & 0o6000):
            raise ArtifactUnsafe("artifact_archive_unsupported")
        # TarInfo truncates these strings at the first NUL. Validate the whole
        # fixed-width field so ignored bytes cannot escape content screening.
        for start, end in ((0, 100), (157, 257), (265, 297), (297, 329), (345, 500)):
            if any(header[start:end].partition(b"\0")[2]):
                raise ArtifactUnsafe("artifact_archive_invalid")
        # Text-octal fields also stop at NUL. Their padding may contain spaces
        # (notably the checksum's NUL + space); binary numbers consume all bytes.
        for start, end in ((100, 108), (108, 116), (116, 124), (124, 136),
                           (136, 148), (148, 156), (329, 337), (337, 345)):
            field = header[start:end]
            if field[0] not in (0x80, 0xff) and field.partition(b"\0")[2].strip(b"\0 "):
                raise ArtifactUnsafe("artifact_archive_invalid")
        if any(header[500:512]):  # Reserved USTAR header padding is not payload.
            raise ArtifactUnsafe("artifact_archive_invalid")
        if item.size < 0 or (item.isdir() and item.size):
            raise ArtifactUnsafe("artifact_archive_invalid")
        state["members"] += 1
        if state["members"] > MAX_ARCHIVE_MEMBERS:
            raise ArtifactUnsafe("artifact_archive_metadata_limit")
        _archive_member_path(item.name, item.isdir(), seen)
        if not item.isdir():
            _inspect_member(stream, item.size, item.name, state, depth)
        if any(_read_archive_metadata(stream, -item.size % 512, state)):
            raise ArtifactUnsafe("artifact_archive_invalid")


def _gzip_header(stream, state):
    header = _read_archive_metadata(stream, 10, state)
    if header[:3] != b"\x1f\x8b\x08" or header[3] & 0xe0:
        raise ArtifactUnsafe("artifact_archive_invalid")
    flags = header[3]
    if flags & 4:
        size = int.from_bytes(_read_archive_metadata(stream, 2, state), "little")
        extra = _read_archive_metadata(stream, size, state)
        if _sensitive_bytes(extra):
            raise ArtifactUnsafe("artifact_sensitive_content")
        # Each extra subfield has a two-byte ID and a little-endian length.
        # Scan its payload separately: length bytes are not text separators.
        offset = 0
        while offset < len(extra):
            _archive_tick(state)
            if len(extra) - offset < 4:
                raise ArtifactUnsafe("artifact_archive_invalid")
            end = offset + 4 + int.from_bytes(extra[offset + 2:offset + 4], "little")
            if end > len(extra):
                raise ArtifactUnsafe("artifact_archive_invalid")
            if _sensitive_bytes(extra[offset + 4:end]):
                raise ArtifactUnsafe("artifact_sensitive_content")
            offset = end
    for flag in (8, 16):
        if not flags & flag:
            continue
        # Optional zero-terminated fields have no declared size. Read bounded
        # chunks, charge only header bytes, and rewind any compressed lookahead.
        field = bytearray()
        while True:
            _archive_tick(state)
            part = stream.read(min(CHUNK_BYTES, MAX_ARCHIVE_DIRECTORY_BYTES - state["metadata"] + 1))
            if not part:
                raise ArtifactUnsafe("artifact_archive_invalid")
            end = part.find(b"\0")
            used = len(part) if end < 0 else end + 1
            _archive_metadata(state, used)
            field.extend(part[:used])
            stream.seek(used - len(part), os.SEEK_CUR)
            if end >= 0:
                break
        if _sensitive_bytes(bytes(field[:-1])):
            raise ArtifactUnsafe("artifact_sensitive_content")
        if flag == 8 and len(field) > 1:
            _archive_member_path(field[:-1].decode("latin-1"), False, {})
    if flags & 2:
        _read_archive_metadata(stream, 2, state)  # zlib also verifies FHCRC.
    _archive_metadata(state, 8)  # Trailer CRC/ISIZE, checked by zlib below.


def _inspect_gzip(stream, state, depth):
    """Single gzip stream wrapping USTAR; CRC/ISIZE and all input must verify."""
    stream.seek(0)
    _gzip_header(stream, state)
    stream.seek(0)
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    tail = b""
    with io.BytesIO() as contents:
        try:
            while not inflater.eof:
                _archive_tick(state)
                compressed = inflater.unconsumed_tail or stream.read(CHUNK_BYTES)
                if not compressed:
                    raise ArtifactUnsafe("artifact_archive_invalid")
                chunk = inflater.decompress(
                    compressed, min(CHUNK_BYTES, MAX_SCAN_BYTES - state["expanded"] + 1))
                tail = _archive_chunk(chunk, state, tail)
                contents.write(chunk)
        except zlib.error:
            raise ArtifactUnsafe("artifact_archive_invalid") from None
        if inflater.unused_data or inflater.unconsumed_tail or stream.read(1):
            raise ArtifactUnsafe("artifact_archive_unsupported")
        contents.seek(0)
        kind = _archive_kind(contents.read(512))
        if kind not in {"", "tar"}:
            raise ArtifactUnsafe("artifact_archive_unsupported")
        # gzip is the transport for this TAR, not a fresh recursion budget.
        # Expanded bytes of both the envelope and inspected members are charged.
        _inspect_tar(contents, state, depth)


def _inspect_archive(stream, name, state, depth=0):
    _archive_tick(state)
    stream.seek(0)
    kind = _archive_kind(stream.read(512), name)
    if not kind and zipfile.is_zipfile(stream):
        kind = "zip"
    if not kind:
        return
    if depth > MAX_ARCHIVE_DEPTH:
        raise ArtifactUnsafe("artifact_archive_depth_limit")
    if kind == "unsupported":
        raise ArtifactUnsafe("artifact_archive_unsupported")
    if kind == "zip":
        _inspect_zip(stream, state, depth)
    elif kind == "tar":
        _inspect_tar(stream, state, depth)
    else:
        _inspect_gzip(stream, state, depth)
    _archive_tick(state)


def inspect_archive_bytes(raw: bytes, name: str, depth: int = 0) -> None:
    """Reuse the exact bounded walker for an already byte-verified export."""
    if len(raw) > MAX_SCAN_BYTES:
        raise ArtifactUnsafe("artifact_security_scan_limit")
    started = time.monotonic()
    budget = SCAN_SECONDS + len(raw) / SCAN_BYTES_PER_SECOND if SCAN_SECONDS > 0 else 0
    with io.BytesIO(raw) as stream:
        _inspect_archive(stream, name, {"start": started, "deadline": started + budget,
            "raw_size": len(raw), "expanded": 0, "members": 0, "metadata": 0}, depth)


def _inspect(stream, name: str = ""):
    before = os.fstat(stream.fileno())
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactUnsafe("artifact_security_not_regular")
    if before.st_size > MAX_SCAN_BYTES:
        raise ArtifactUnsafe("artifact_security_scan_limit")
    stamp = _stamp(before)
    suffix = os.path.splitext(name)[1].lower()
    cache_key = (stamp, suffix, MAX_SCAN_BYTES, SCAN_SECONDS, SCAN_BYTES_PER_SECOND,
                 CHUNK_BYTES, OVERLAP, MAX_ARCHIVE_MEMBERS, MAX_ARCHIVE_DIRECTORY_BYTES, MAX_ARCHIVE_DEPTH)
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached:
            _CACHE.move_to_end(cache_key)
            if cached[0]:
                raise ArtifactUnsafe(cached[0])
            return stamp, cached[1]
    budget = SCAN_SECONDS + before.st_size / SCAN_BYTES_PER_SECOND if SCAN_SECONDS > 0 else 0
    started = time.monotonic()
    deadline = started + budget
    digest, tail, scanned = hashlib.sha256(), b"", 0
    reason = ""
    stream.seek(0)
    while True:
        if time.monotonic() >= deadline:
            raise ArtifactUnsafe("artifact_security_scan_timeout")
        chunk = stream.read(min(CHUNK_BYTES, MAX_SCAN_BYTES - scanned + 1))
        if not chunk:
            break
        scanned += len(chunk)
        if scanned > MAX_SCAN_BYTES:
            raise ArtifactUnsafe("artifact_security_scan_limit")
        digest.update(chunk)
        window = tail + chunk
        if _sensitive_bytes(window):
            reason = "artifact_sensitive_content"
            break
        tail = window[-OVERLAP:]
    if _stamp(os.fstat(stream.fileno())) != stamp:
        raise ArtifactUnsafe("artifact_security_changed")
    if not reason and scanned != before.st_size:
        raise ArtifactUnsafe("artifact_security_incomplete")
    if not reason:
        try:
            _inspect_archive(stream, name, {"start": started, "deadline": deadline,
                "raw_size": before.st_size, "expanded": 0, "members": 0, "metadata": 0})
        except ArtifactUnsafe as exc:
            if str(exc) != "artifact_sensitive_content":
                raise
            reason = str(exc)
        if _stamp(os.fstat(stream.fileno())) != stamp:
            raise ArtifactUnsafe("artifact_security_changed")
    with _LOCK:
        _CACHE[cache_key] = (reason, digest.hexdigest())
        _CACHE.move_to_end(cache_key)
        while len(_CACHE) > CACHE_ENTRIES:
            _CACHE.popitem(last=False)
    if reason:
        raise ArtifactUnsafe(reason)
    return stamp, digest.hexdigest()


def check_file(path: str) -> dict:
    """No directory scans or raw error messages; unknown is not permission to publish."""
    try:
        with _open(path) as stream:
            stamp, digest = _inspect(stream, str(path))
        return {"allowed": True, "sha256": digest, "bytes": stamp[2],
                "mtime_ns": stamp[3], "scan_status": "scanned"}
    except ArtifactUnsafe as exc:
        return {"allowed": False, "reason": str(exc),
                "scan_status": "blocked" if str(exc) == "artifact_sensitive_content" else "not_scanned"}
    except (OSError, ValueError, RuntimeError):
        return {"allowed": False, "reason": "artifact_security_unavailable", "scan_status": "not_scanned"}


def read_public_file(path: str, limit: int | None = None) -> bytes:
    """Check and read the same descriptor; never check one file then serve its replacement."""
    try:
        with _open(path) as stream:
            stamp, digest = _inspect(stream, str(path))
            stream.seek(0)
            body = stream.read(stamp[2] + 1 if limit is None else min(limit, stamp[2]))
            if _stamp(os.fstat(stream.fileno())) != stamp:
                raise ArtifactUnsafe("artifact_security_changed")
            if limit is None and (len(body) != stamp[2] or hashlib.sha256(body).hexdigest() != digest):
                raise ArtifactUnsafe("artifact_security_changed")
            return body
    except ArtifactUnsafe:
        raise
    except (OSError, ValueError, RuntimeError):
        raise ArtifactUnsafe("artifact_security_unavailable") from None
