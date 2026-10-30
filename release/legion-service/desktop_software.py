#!/usr/bin/env python3
"""云电脑软件生命周期：可信搜索、版本、安装、升级、卸载与回滚事务。"""
from __future__ import annotations

import json
import os
import shlex
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import cloud_desktop
import desktop_fs

_LOCK = threading.RLock()
_ALLOWED_MANAGERS = {"apt", "pip", "npm"}

SOFTWARE = {
    "git": {"name": "Git", "manager": "apt", "package": "git", "binary": "git", "scenes": ["ide", "code"]},
    "vscode": {"name": "Visual Studio Code", "manager": "apt", "package": "code", "binary": "code", "scenes": ["ide", "code"], "optional": True},
    "geany": {"name": "Geany IDE", "manager": "apt", "package": "geany", "binary": "geany", "scenes": ["ide", "code"]},
    "chromium": {"name": "Chromium", "manager": "apt", "package": "chromium", "binary": "chromium", "scenes": ["browser", "research"]},
    "libreoffice": {"name": "LibreOffice", "manager": "apt", "package": "libreoffice", "binary": "libreoffice", "scenes": ["office", "spreadsheet", "slides", "pdf"]},
    "gimp": {"name": "GIMP", "manager": "apt", "package": "gimp", "binary": "gimp", "scenes": ["design", "image"]},
    "inkscape": {"name": "Inkscape", "manager": "apt", "package": "inkscape", "binary": "inkscape", "scenes": ["design", "image", "vector"]},
    "imagemagick": {"name": "ImageMagick", "manager": "apt", "package": "imagemagick", "binary": "convert", "scenes": ["design", "image"]},
    "poppler": {"name": "PDF 工具", "manager": "apt", "package": "poppler-utils", "binary": "pdftoppm", "scenes": ["pdf", "office"]},
    "browser-use": {"name": "Browser Use", "manager": "pip", "package": "browser-use", "binary": "/opt/meyo-agents/venvs/browser-use/bin/browser-use", "scenes": ["browser", "research", "agent"]},
    "qwen-code": {"name": "Qwen Code", "manager": "npm", "package": "@qwen-code/qwen-code", "binary": "qwen", "scenes": ["ide", "code", "agent"]},
    "aider": {"name": "Aider", "manager": "pip", "package": "aider-chat", "binary": "/opt/meyo-agents/venvs/aider/bin/aider", "scenes": ["ide", "code", "agent"]},
    "goose": {"name": "Goose", "manager": "apt", "package": "goose", "binary": "goose", "scenes": ["code", "agent"], "manual": True},
    "openclaw": {"name": "OpenClaw", "manager": "npm", "package": "openclaw", "binary": "openclaw", "scenes": ["agent"]},
    "gnhf": {"name": "GNHF", "manager": "npm", "package": "gnhf", "binary": "gnhf", "scenes": ["agent"]},
    "kimi": {"name": "Kimi Code", "manager": "npm", "package": "@moonshot-ai/kimi-code", "binary": "kimi", "scenes": ["ide", "code", "agent"]},
    "loopx": {"name": "LoopX", "manager": "pip", "package": "loopx", "binary": "/opt/meyo-agents/venvs/loopx/bin/loopx", "scenes": ["long_task", "agent"]},
}


def _safe(value: Any, fallback: str = "default") -> str:
    return desktop_fs._safe_id(str(value or fallback), fallback)


def _state_root(uid: str) -> Path:
    root = Path(desktop_fs.prepare_user_root(uid)["root"]) / ".meyo" / "software"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _state_path(uid: str) -> Path:
    return _state_root(uid) / "transactions.jsonl"


def _snapshots(uid: str) -> Path:
    root = _state_root(uid) / "snapshots"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _append(uid: str, event: dict) -> None:
    path = _state_path(uid)
    with _LOCK, path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.time(), **event}, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _item(software_id: str) -> dict | None:
    item = SOFTWARE.get(str(software_id or "").lower())
    return {"id": str(software_id or "").lower(), **item} if item else None


def _quote(value: str) -> str:
    return shlex.quote(str(value))


def _run(uid: str, command: str, timeout_s: int = 300) -> dict:
    return desktop_fs.desktop_exec(uid, command, timeout_s)


def _binary_check(binary: str) -> str:
    if binary.startswith("/"):
        return "test -x " + _quote(binary)
    return "command -v " + _quote(binary) + " >/dev/null 2>&1"


def _version_command(item: dict) -> str:
    binary = item["binary"]
    if item["manager"] == "apt":
        return "dpkg-query -W -f='${Version}' " + _quote(item["package"]) + " 2>/dev/null"
    if item["manager"] == "npm":
        return "npm list -g --depth=0 --json " + _quote(item["package"]) + " 2>/dev/null"
    if binary.startswith("/opt/meyo-agents/venvs/"):
        pip = str(Path(binary).parent / "pip")
        return _quote(pip) + " show " + _quote(item["package"]) + " 2>/dev/null | awk -F': ' '/^Version:/{print $2}'"
    return _quote(binary) + " --version 2>/dev/null | head -1"


def status(uid: str, software_id: str) -> dict:
    item = _item(software_id)
    if not item:
        return {"ok": False, "error": "unknown software"}
    probe = _run(uid, _binary_check(item["binary"]) + " && (" + _version_command(item) + ")", 30)
    installed = probe.get("exit_code") == 0
    version = str(probe.get("stdout") or "").strip()[:1000] if installed else ""
    return {"ok": True, "software": item, "installed": installed, "version": version,
            "probe": {"exit_code": probe.get("exit_code"), "stderr": str(probe.get("stderr") or "")[-500:]}}


def search(uid: str = "default", query: str = "", scene: str = "") -> dict:
    query = str(query or "").casefold().strip()
    scene = str(scene or "").casefold().strip()
    rows = []
    for software_id, item in SOFTWARE.items():
        hay = " ".join([software_id, item["name"], *item.get("scenes", [])]).casefold()
        if query and query not in hay:
            continue
        if scene and scene not in item.get("scenes", []):
            continue
        current = status(uid, software_id)
        rows.append({**item, "id": software_id, "installed": current.get("installed", False),
                     "version": current.get("version", "")})
    return {"ok": True, "uid": _safe(uid), "items": rows, "total": len(rows),
            "query": query, "scene": scene}


def _inventory(uid: str) -> dict:
    commands = {
        "apt": "dpkg-query -W -f='${binary:Package}\t${Version}\n' 2>/dev/null | sort",
        "pip": "find /opt/meyo-agents/venvs -maxdepth 3 -name pip -type f -exec {} freeze ';' 2>/dev/null | sort -u",
        "npm": "npm list -g --depth=0 --json 2>/dev/null",
    }
    out = {}
    for name, command in commands.items():
        result = _run(uid, command, 90)
        out[name] = str(result.get("stdout") or "")[-200000:]
    return out


def _snapshot(uid: str, software_id: str, action: str) -> dict:
    transaction_id = f"software-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    before = status(uid, software_id)
    payload = {"transaction_id": transaction_id, "software_id": software_id,
               "action": action, "before": before, "inventory": _inventory(uid),
               "created_at": time.time()}
    path = _snapshots(uid) / f"{transaction_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": transaction_id, "path": path, "data": payload}


def _command(item: dict, action: str) -> str | None:
    manager, package = item["manager"], item["package"]
    if item.get("manual"):
        return None
    if manager == "apt":
        if action == "install":
            return "DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y " + _quote(package)
        if action == "upgrade":
            return "DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade " + _quote(package)
        if action == "uninstall":
            return "DEBIAN_FRONTEND=noninteractive apt-get remove -y " + _quote(package)
    if manager == "npm":
        return ("npm install -g " if action in {"install", "upgrade"} else "npm uninstall -g ") + _quote(package)
    if manager == "pip":
        binary = item["binary"]
        pip = str(Path(binary).parent / "pip") if binary.startswith("/") else "python3 -m pip"
        if action == "uninstall":
            return _quote(pip) + " uninstall -y " + _quote(package) if pip.startswith("/") else pip + " uninstall -y " + _quote(package)
        flag = " --upgrade" if action == "upgrade" else ""
        return (_quote(pip) if pip.startswith("/") else pip) + " install" + flag + " " + _quote(package)
    return None


def _restore_command(item: dict, before: dict) -> str | None:
    installed = bool(before.get("installed"))
    version = str(before.get("version") or "").strip()
    if not installed:
        return _command(item, "uninstall")
    package = item["package"]
    if item["manager"] == "apt" and version:
        return "DEBIAN_FRONTEND=noninteractive apt-get install -y " + _quote(package + "=" + version.splitlines()[0])
    if item["manager"] == "npm" and version:
        try:
            parsed = json.loads(version)
            version = str(((parsed.get("dependencies") or {}).get(package) or {}).get("version") or "")
        except json.JSONDecodeError:
            version = ""
        return "npm install -g " + _quote(package + ("@" + version if version else ""))
    if item["manager"] == "pip" and version:
        binary = item["binary"]
        pip = str(Path(binary).parent / "pip") if binary.startswith("/") else "python3 -m pip"
        return (_quote(pip) if pip.startswith("/") else pip) + " install " + _quote(package + "==" + version.splitlines()[0])
    return _command(item, "install")


def transact(uid: str, software_id: str, action: str, *, confirm: bool = False,
             rollback_on_failure: bool = True) -> dict:
    item = _item(software_id)
    action = str(action or "").lower()
    if not item or item.get("manager") not in _ALLOWED_MANAGERS:
        return {"ok": False, "error": "unknown or unsupported software"}
    if action not in {"install", "upgrade", "uninstall"}:
        return {"ok": False, "error": "action must be install/upgrade/uninstall"}
    command = _command(item, action)
    if not command:
        return {"ok": False, "status": "manual", "error": "software requires a reviewed manual installer", "software": item}
    if not confirm:
        return {"ok": True, "status": "planned", "software": item, "action": action, "command": command}
    snapshot = _snapshot(uid, software_id, action)
    _append(uid, {"event": "started", "transaction_id": snapshot["id"], "software_id": software_id, "action": action})
    result = _run(uid, command, desktop_fs.MAX_EXEC_TIMEOUT_S)
    after = status(uid, software_id)
    wanted = action != "uninstall"
    verified = bool(after.get("installed")) == wanted
    rollback = None
    if not verified and rollback_on_failure:
        restore = _restore_command(item, snapshot["data"]["before"])
        if restore:
            rollback_run = _run(uid, restore, desktop_fs.MAX_EXEC_TIMEOUT_S)
            restored = status(uid, software_id)
            rollback = {"ok": bool(restored.get("installed")) == bool(snapshot["data"]["before"].get("installed")),
                        "command": restore, "run": rollback_run, "status": restored}
    output = {"ok": verified, "status": "completed" if verified else "failed",
              "transaction_id": snapshot["id"], "software": item, "action": action,
              "before": snapshot["data"]["before"], "after": after,
              "run": result, "rollback": rollback}
    _append(uid, {"event": "finished", "transaction_id": snapshot["id"],
                  "software_id": software_id, "action": action, "ok": verified,
                  "rollback_ok": None if rollback is None else rollback.get("ok")})
    cloud_desktop.invalidate_status(uid)
    return output


def rollback(uid: str, transaction_id: str, *, confirm: bool = False) -> dict:
    transaction_id = Path(str(transaction_id or "")).name
    path = _snapshots(uid) / f"{transaction_id}.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "error": "transaction snapshot not found"}
    item = _item(snapshot.get("software_id", ""))
    if not item:
        return {"ok": False, "error": "software definition no longer exists"}
    command = _restore_command(item, snapshot.get("before") or {})
    if not command:
        return {"ok": False, "error": "rollback command unavailable"}
    if not confirm:
        return {"ok": True, "status": "planned", "transaction_id": transaction_id, "command": command}
    run = _run(uid, command, desktop_fs.MAX_EXEC_TIMEOUT_S)
    current = status(uid, item["id"])
    expected = bool((snapshot.get("before") or {}).get("installed"))
    ok = bool(current.get("installed")) == expected
    _append(uid, {"event": "rollback", "transaction_id": transaction_id, "ok": ok})
    return {"ok": ok, "status": "rolled_back" if ok else "rollback_failed",
            "transaction_id": transaction_id, "run": run, "software": current}


def history(uid: str = "default", limit: int = 100) -> dict:
    rows = []
    try:
        for line in _state_path(uid).read_text(encoding="utf-8").splitlines():
            rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        pass
    return {"ok": True, "transactions": rows[-max(1, min(int(limit or 100), 500)):], "total": len(rows)}
