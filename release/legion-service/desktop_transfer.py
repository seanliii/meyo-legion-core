#!/usr/bin/env python3
"""云电脑上传、下载与跨应用剪贴板；所有文件限定在用户 Meyo 卷。"""
from __future__ import annotations

import base64
import mimetypes
import os
import shlex
import time
from pathlib import Path
from typing import Any

import desktop_fs

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_CLIPBOARD_CHARS = 200_000


def upload(uid: str, project_id: str, filename: str, *, content_base64: str,
           destination: str = "inputs") -> dict:
    filename = os.path.basename(str(filename or ""))
    destination = str(destination or "inputs").strip("/")
    if not filename or filename in {".", ".."}:
        return {"ok": False, "error": "invalid filename"}
    if not destination or destination == ".." or destination.startswith("../") or "\\" in destination:
        return {"ok": False, "error": "invalid destination"}
    try:
        payload = base64.b64decode(content_base64, validate=True)
    except (ValueError, TypeError) as error:
        return {"ok": False, "error": f"invalid base64: {error}"}
    if len(payload) > MAX_UPLOAD_BYTES:
        return {"ok": False, "error": f"upload exceeds {MAX_UPLOAD_BYTES} bytes"}
    relative = f"projects/{desktop_fs._safe_id(project_id, 'project')}/{destination}/{filename}"
    try:
        target, normalized = desktop_fs._resolve(uid, relative, require_file=True)
        desktop_fs._atomic_write_file(target, payload)
    except (OSError, ValueError) as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True, "path": normalized, "bytes": len(payload),
            "mime": mimetypes.guess_type(filename)[0] or "application/octet-stream"}


def download(uid: str, path: str, max_bytes: int = MAX_UPLOAD_BYTES) -> dict:
    try:
        target, relative = desktop_fs._resolve(uid, path, require_file=True)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    if not os.path.isfile(target):
        return {"ok": False, "error": "file not found"}
    size = os.path.getsize(target)
    if size > max(1, min(int(max_bytes or MAX_UPLOAD_BYTES), MAX_UPLOAD_BYTES)):
        return {"ok": False, "error": "file exceeds download limit", "bytes": size}
    payload = Path(target).read_bytes()
    return {"ok": True, "path": relative, "name": os.path.basename(relative), "bytes": size,
            "mime": mimetypes.guess_type(relative)[0] or "application/octet-stream",
            "content_base64": base64.b64encode(payload).decode("ascii")}


def clipboard_write(uid: str, text: str) -> dict:
    text = str(text or "")
    if len(text) > MAX_CLIPBOARD_CHARS:
        return {"ok": False, "error": "clipboard text too large"}
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    command = "printf %s " + shlex.quote(encoded) + " | base64 -d | DISPLAY=:1 xclip -selection clipboard"
    result = desktop_fs.desktop_exec(uid, command, 30)
    return {"ok": bool(result.get("ok") and result.get("exit_code") == 0),
            "action": "clipboard_write", "chars": len(text), "exit_code": result.get("exit_code"),
            "error": result.get("stderr") or result.get("error") or ""}


def clipboard_read(uid: str) -> dict:
    result = desktop_fs.desktop_exec(uid, "DISPLAY=:1 xclip -selection clipboard -o", 30)
    text = str(result.get("stdout") or "")[:MAX_CLIPBOARD_CHARS]
    return {"ok": bool(result.get("ok") and result.get("exit_code") == 0),
            "action": "clipboard_read", "text": text, "chars": len(text),
            "error": result.get("stderr") or result.get("error") or ""}
