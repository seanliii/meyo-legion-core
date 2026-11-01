"""File facts are not content inspection or proof of a published URL."""
import builtins
import importlib
import io
import os
from pathlib import Path
import socket
import subprocess

import pytest


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        root = tmp_path / name
        root.mkdir()
        monkeypatch.setenv(name, str(root))

    def forbidden(*args, **kwargs):
        pytest.fail("File description must not connect, decode or launch a process")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)


@pytest.mark.parametrize("suffix,kind", [
    (".mp4", "video"), (".png", "image"), (".wav", "audio"),
    (".pdf", "pdf"), (".html", "web"), (".bin", "file"),
])
def test_local_file_metadata_neither_publishes_nor_decodes(tmp_path, suffix, kind):
    hub = importlib.import_module("file_hub")
    path = tmp_path / ("unpublished" + suffix)
    content = b"not a valid media container\n"
    path.write_bytes(content)

    result = hub.describe(str(path))

    assert result["ok"] is True, "ok refers to describing the local file"
    assert result["url"] == ""
    assert result["downloadable"] is False
    assert result["renderable"] is False
    assert result["path"] == str(path)
    assert result["kind"] == kind and result["bytes"] == len(content)
    assert result["format_inference"] == {
        "basis": "filename_extension", "extension": suffix,
    }
    assert result["content_processing"] == {
        "read": "not_performed", "preview": "not_performed",
        "media_decode": "not_performed",
    }
    assert result["link"] == {"source": "none", "availability": "not_provided"}
    assert path.read_bytes() == content


def test_metadata_only_does_not_open_media_bytes(tmp_path, monkeypatch):
    hub = importlib.import_module("file_hub")
    path = tmp_path / "unread.mp4"
    path.write_bytes(b"unread fixture")
    real_open, real_io_open = builtins.open, io.open

    def guarded(original):
        def read(file, *args, **kwargs):
            if not isinstance(file, int) and Path(file) == path:
                pytest.fail("Metadata-only description must not read media content")
            return original(file, *args, **kwargs)
        return read

    monkeypatch.setattr(builtins, "open", guarded(real_open))
    monkeypatch.setattr(io, "open", guarded(real_io_open))
    result = hub.describe(str(path))
    assert result["content_processing"]["read"] == "not_performed"
    assert result["content_processing"]["media_decode"] == "not_performed"


@pytest.mark.parametrize("url", [
    "/media/personal/fixture/upload.mp4",
    "/artifacts/fixture/movie.mp4?download=1",
    "https://example.invalid/unverified.mp4",
])
def test_caller_url_is_preserved_but_never_verified(tmp_path, url):
    hub = importlib.import_module("file_hub")
    path = tmp_path / "local.mp4"
    path.write_bytes(b"metadata only")

    result = hub.describe(str(path), url=url)

    assert result["url"] == url
    assert result["downloadable"] is True, "legacy flag means a supplied URL exists"
    assert result["renderable"] is True, "the UI may attempt this type of URL"
    assert result["link"] == {"source": "caller", "availability": "unverified"}
    assert result["content_processing"]["media_decode"] == "not_performed"


def test_identical_basenames_keep_distinct_local_paths(tmp_path):
    hub = importlib.import_module("file_hub")
    paths = [tmp_path / directory / "same.mp4" for directory in ("one", "two")]
    for path in paths:
        path.parent.mkdir()
        path.write_bytes(b"local")
    results = [hub.describe(str(path)) for path in paths]
    assert [result["path"] for result in results] == [str(path) for path in paths]
    assert all(result["url"] == "" and not result["downloadable"] for result in results)


@pytest.mark.parametrize("name,content", [
    ("note.txt", "readable text\n"),
    ("example.py", "value = 1\n"),
    ("table.csv", "name,value\nfirst,2\n"),
])
def test_inline_preview_is_readable_without_claiming_download(tmp_path, name, content):
    hub = importlib.import_module("file_hub")
    path = tmp_path / name
    path.write_text(content)

    result = hub.describe(str(path))

    assert result["preview"]["ok"] is True
    assert result["renderable"] is True
    assert result["url"] == "" and result["downloadable"] is False
    assert result["content_processing"] == {
        "read": "performed", "preview": "succeeded",
        "media_decode": "not_performed",
    }


def test_failed_preview_preserves_metadata_without_claiming_read_success(
        tmp_path, monkeypatch):
    hub = importlib.import_module("file_hub")
    path = tmp_path / "unreadable.py"
    path.write_bytes(b"file exists; preview fails")
    monkeypatch.setattr(hub, "_read_code",
                        lambda path: {"ok": False, "error": "fixture read/parse failure"})

    result = hub.describe(str(path))

    assert result["ok"] is True and result["preview"]["ok"] is False
    assert result["path"] == str(path)
    assert result["content_processing"] == {
        "read": "unknown", "preview": "failed",
        "media_decode": "not_performed",
    }
    assert result["renderable"] is False
    assert result["downloadable"] is False


def test_missing_file_is_not_an_available_link(tmp_path):
    hub = importlib.import_module("file_hub")
    result = hub.describe(str(tmp_path / "missing.mp4"))
    assert result["ok"] is False
    assert not result.get("url") and not result.get("downloadable")


def test_tool_description_explains_metadata_only_inspection(monkeypatch):
    toolbox = importlib.import_module("toolbox")
    monkeypatch.setattr(toolbox, "_BOOTSTRAP_MEMORY", {"data": None})
    items = toolbox._bootstrap_toolbox()["items"]
    description = next(row["description"] for row in items
                       if row["id"] == "runnable:media_inspect")
    assert "扩展名" in description
    assert "不解码" in description
    assert "不发布" in description
