"""Render Markdown with a shared, offline-capable parser, never task-time pip."""
from __future__ import annotations

from functools import lru_cache
import hashlib
import importlib
import json
from pathlib import Path
import sys
import threading

_IMPORT_LOCK = threading.Lock()
_BUNDLE = Path(__file__).resolve().parent / "tools" / "markdown-runtime"


def _dependency_rows() -> list[dict]:
    manifest = json.loads((_BUNDLE / "manifest.json").read_text())
    rows = manifest["packages"]
    if {row["name"] for row in rows} != {"markdown-it-py", "mdurl"} or len(rows) != 2:
        raise RuntimeError("Markdown preview dependency manifest is invalid")
    return rows


def _bundled_paths(rows: list[dict]) -> list[str]:
    paths = []
    for row in rows:
        name = row["file"]
        path = _BUNDLE / name
        if Path(name).name != name or not name.endswith("-py3-none-any.whl") or path.is_symlink():
            raise RuntimeError("Markdown preview dependency path is invalid")
        data = path.read_bytes()
        if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise RuntimeError("Markdown preview dependency checksum mismatch")
        paths.append(str(path))
    return paths


def _checked_parser(rows: list[dict]):
    modules = {
        "markdown-it-py": importlib.import_module("markdown_it"),
        "mdurl": importlib.import_module("mdurl"),
    }
    for row in rows:
        module = modules[row["name"]]
        version = str(getattr(module, "__version__", ""))
        if version != row["version"] or not getattr(module, "__file__", ""):
            # Never evict another consumer's imported modules to force a match.
            raise RuntimeError(
                f"Markdown preview dependency version conflict: {row['name']} "
                f"{version or 'unknown'}; expected {row['version']}")
    return modules["markdown-it-py"].MarkdownIt


@lru_cache(maxsize=1)
def _parser_class():
    with _IMPORT_LOCK:
        rows = _dependency_rows()
        try:
            return _checked_parser(rows)
        except ModuleNotFoundError as exc:
            if exc.name not in {"markdown_it", "mdurl"}:
                raise
        # Matching declared versions may come from the environment or bundle.
        # Archive checks do not claim byte identity for an existing installation.
        for path in _bundled_paths(rows):
            if path not in sys.path:
                sys.path.append(path)
        importlib.invalidate_caches()
        return _checked_parser(rows)


def render_markdown(text: str) -> str:
    # A fresh parser per request avoids mutable option/plugin state crossing users.
    # These fragments are inserted into the product DOM: raw HTML must stay text.
    parser = _parser_class()("commonmark", {"html": False, "linkify": False})
    parser.enable("table")
    return parser.render(text)
