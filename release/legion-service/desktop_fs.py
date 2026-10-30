#!/usr/bin/env python3
"""Per-user persistent Meyo files and managed CUA desktop operations."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import shlex
import stat
import time
import uuid
from typing import Any

import legion_paths

CONTAINER_MEYO_ROOT = "/root/Meyo"
CATEGORIES = ("projects", "sessions", "evolution", "memory", "personal")
MAX_READ_BYTES = 512 * 1024
MAX_WRITE_BYTES = 2 * 1024 * 1024
MAX_EXEC_TIMEOUT_S = 300
CUA_GUI_INPUT_HELP = (
    ' 当前受管 Linux 桌面的特殊键使用 pynput 小写名称：'
    'press_key 的 params 如 {"key":"enter"}、{"key":"esc"}，单字符按原样输入。'
    '组合键用 hotkey，params 如 {"keys":["ctrl","s"]}，不是 press_key("ctrl+s")。'
    'type_text 的 params 为 {"text":"要输入的文字"}。'
    '本镜像逐键输入曾出现部分汉字丢失/重复，success 仅表示发送未报错。'
    '已有 set_clipboard 的 params 为 {"text":"要粘贴的文字"}，设置当前云桌面剪贴板，'
    '随后可用 hotkey 执行当前应用的粘贴快捷键。'
    'copy_to_clipboard 的 params 为 {}，实际是读取剪贴板内容，不是发送复制快捷键。'
    '当前 Linux get_accessibility_tree 仅返回占位树，不是真实界面元素；现场以真实截图为准。'
)

# ★安装类容器内串行化(OOM 根治·2026-09-02 q30d d22 血泪):两个 agent 并行各自
#   `python -m pip install jupyter`(各 ~1.4GB)→colima VM(8GiB)内存尖峰→macOS OOM
#   挑最肥的 server SIGKILL(当日 137x8)。抽成模块级纯函数=可单测(回归锁
#   tests/test_desktop_install_serialization.py·word boundary 防"adaptive update"误伤)。
# 锁等待上限 240s(超时 rc!=0·agent 自愈重试即可,绝不无限排队);非安装命令零改动零开销。
_INSTALL_CMD_RE = re.compile(
    r"\b(pip3?|pipx|npm|pnpm|yarn|apt-get|apt|uv|cargo|gem|composer)\s+"
    r"(-[A-Za-z]+[ =]\S*\s+)*(install|add|remove|uninstall|upgrade|update|require)\b"
    r"|\bgo\s+install\b")


def _is_install_command(command: str) -> bool:
    """安装类命令检测(pip/npm/apt/uv/cargo/gem/composer/go install·含 python -m pip 形态)。"""
    return bool(_INSTALL_CMD_RE.search(command or ""))


def _wrap_install_lock(command: str) -> str:
    """安装类命令包一层容器级 flock 全局锁(串行化·防并行安装内存尖峰 OOM)。"""
    import shlex
    return "flock -w 240 /tmp/.legion-install.lock sh -lc " + shlex.quote(command)
MAX_LIST_ENTRIES = 300

def _safe_id(value: Any, fallback: str = "default") -> str:
    """无碰撞稳定 ID:纯 ASCII 合法 ID 原样保留;其余给可读前缀 + sha256 摘要,
    保证 alice.b / alice/b / aliceb 不会共享同一份存储或容器。"""
    raw = str(value or fallback).strip() or fallback
    if len(raw) <= 80 and all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in raw):
        return raw
    readable = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "-" for ch in raw).strip("-")[:48] or "user"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{readable}-{digest}"

def _users_root() -> str:
    root = os.path.realpath(legion_paths.under("users"))
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root

def user_meyo_root(uid: str = "default") -> str:
    return os.path.join(_users_root(), _safe_id(uid), "desktop", "Meyo")

def prepare_user_root(uid: str = "default") -> dict:
    users_root = _users_root()
    logical_root = os.path.abspath(user_meyo_root(uid))
    if os.path.commonpath((users_root, logical_root)) != users_root:
        raise ValueError("user Meyo root escapes users directory")
    os.makedirs(logical_root, mode=0o700, exist_ok=True)
    root = os.path.realpath(logical_root)
    if os.path.commonpath((users_root, root)) != users_root:
        raise ValueError("user Meyo root resolves outside users directory")
    for category in CATEGORIES:
        directory = os.path.join(root, category)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"Meyo category is not a real directory: {category}")
    return {"ok": True, "uid": _safe_id(uid), "root": root, "container_root": CONTAINER_MEYO_ROOT, "categories": list(CATEGORIES)}

def _resolve(uid: str, path: Any = "", *, require_file: bool = False) -> tuple[str, str]:
    root = os.path.realpath(prepare_user_root(uid)["root"])
    raw = str(path or "").strip()
    if chr(0) in raw:
        raise ValueError("path contains a null byte")
    if raw == CONTAINER_MEYO_ROOT:
        raw = ""
    elif raw.startswith(CONTAINER_MEYO_ROOT + "/"):
        raw = raw[len(CONTAINER_MEYO_ROOT) + 1:]
    if not raw or raw == ".":
        if require_file:
            raise ValueError("a file path is required")
        return root, ""
    if raw.startswith("/") or chr(92) in raw:
        raise ValueError("path must be relative to Meyo")
    relative = posixpath.normpath(raw)
    if relative in ("", "."):
        if require_file:
            raise ValueError("a file path is required")
        return root, ""
    if relative == ".." or relative.startswith("../"):
        raise ValueError("path escapes Meyo")
    # 逐段拒绝符号链接:用户可写的卷内不允许任何 symlink 穿透。
    cursor = root
    for part in relative.split("/"):
        cursor = os.path.join(cursor, part)
        if os.path.lexists(cursor) and os.path.islink(cursor):
            raise ValueError("symbolic links are not allowed in Meyo paths")
    target = os.path.realpath(os.path.join(root, *relative.split("/")))
    if os.path.commonpath((root, target)) != root:
        raise ValueError("path escapes Meyo")
    return target, relative

def _entry(path: str, root: str) -> dict:
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        kind = "link"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    else:
        kind = "file"
    return {"name": os.path.basename(path), "path": os.path.relpath(path, root).replace(os.sep, "/"), "type": kind, "bytes": info.st_size if kind == "file" else 0, "modified_at": info.st_mtime}

def list_files(uid: str = "default", path: str = "") -> dict:
    root = os.path.realpath(prepare_user_root(uid)["root"])
    try:
        target, relative = _resolve(uid, path)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    if not os.path.isdir(target):
        return {"ok": False, "error": "path not found or not a directory", "path": relative}
    entries = []
    try:
        with os.scandir(target) as items:
            for item in items:
                if len(entries) >= MAX_LIST_ENTRIES:
                    break
                try:
                    entries.append(_entry(item.path, root))
                except OSError:
                    pass
    except OSError as error:
        return {"ok": False, "error": f"cannot list path: {error}", "path": relative}
    entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
    return {"ok": True, "uid": _safe_id(uid), "root": "Meyo", "path": relative, "parent": posixpath.dirname(relative) if relative else None, "entries": entries, "truncated": len(entries) >= MAX_LIST_ENTRIES}

def read_file(uid: str = "default", path: str = "", max_bytes: int = MAX_READ_BYTES) -> dict:
    try:
        target, relative = _resolve(uid, path, require_file=True)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    if not os.path.isfile(target):
        return {"ok": False, "error": "file not found", "path": relative}
    limit = max(1, min(int(max_bytes or MAX_READ_BYTES), MAX_READ_BYTES))
    try:
        # I/O 前再校验一次,中途被换成符号链接或逃逸路径会被拦下。
        target, relative = _resolve(uid, relative, require_file=True)
        if not os.path.isfile(target):
            return {"ok": False, "error": "file not found", "path": relative}
        size = os.path.getsize(target)
        with open(target, "rb") as handle:
            data = handle.read(limit)
    except (OSError, ValueError) as error:
        return {"ok": False, "error": f"cannot read file: {error}", "path": relative}
    binary = bytes([0]) in data
    return {"ok": True, "uid": _safe_id(uid), "path": relative, "bytes": size, "read_bytes": len(data), "truncated": size > len(data), "binary": binary, "content": "" if binary else data.decode("utf-8", "replace"), "content_base64": base64.b64encode(data).decode("ascii") if binary else ""}

def _atomic_write_file(target: str, data: bytes) -> int:
    """同目录临时文件 + fsync + replace 的原子覆盖;replace 不跟随目标名上的符号链接。"""
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    temporary = os.path.join(directory, f".meyo-write-{uuid.uuid4().hex}")
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return len(data)

def _read_source_bytes(source: str) -> bytes:
    with open(source, "rb") as handle:
        return handle.read()

def write_file(uid: str = "default", path: str = "", content: Any = "", *, append: bool = False, content_base64: str = "") -> dict:
    try:
        target, relative = _resolve(uid, path, require_file=True)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    try:
        data = base64.b64decode(content_base64, validate=True) if content_base64 else str(content).encode("utf-8")
    except (TypeError, ValueError) as error:
        return {"ok": False, "error": f"invalid content_base64: {error}"}
    if len(data) > MAX_WRITE_BYTES:
        return {"ok": False, "error": f"content exceeds {MAX_WRITE_BYTES} byte limit"}
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # 写前再校验一次,中途被换成符号链接会被拦下。
        target, relative = _resolve(uid, relative, require_file=True)
        if append:
            with open(target, "ab") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            size = os.path.getsize(target)
        else:
            size = _atomic_write_file(target, data)
    except (OSError, ValueError) as error:
        return {"ok": False, "error": f"cannot write file: {error}", "path": relative}
    return {"ok": True, "uid": _safe_id(uid), "path": relative, "bytes": size, "append": bool(append)}

def make_dir(uid: str = "default", path: str = "") -> dict:
    try:
        target, relative = _resolve(uid, path)
        os.makedirs(target, exist_ok=True)
        # 建完再校验一次,防止通过已存在的符号链接目录创建路径。
        _resolve(uid, relative)
    except (OSError, ValueError) as error:
        return {"ok": False, "error": str(error)}
    return {"ok": True, "uid": _safe_id(uid), "path": relative}

def _category(category: str, uid: str, path: str = "") -> dict:
    raw = str(path or "").strip().lstrip("/")
    if raw == category:
        raw = ""
    elif raw.startswith(category + "/"):
        raw = raw[len(category) + 1:]
    relative = posixpath.normpath(raw) if raw else ""
    if relative == ".." or relative.startswith("../"):
        return {"ok": False, "error": "path escapes category", "path": raw}
    return list_files(uid, posixpath.join(category, relative) if relative else category)

def session_archive(uid: str = "default", limit: int = 60) -> dict:
    """会话归档可视化:~/Meyo/sessions/<sid>/ 逐会话汇总(manifest+军团侧会话标题)。

    每个会话返回:sid、标题(军团侧 turns[0].question 前 80 字)、归档时间、
    交付物数与文件清单;按归档时间倒序。只读,不落任何状态。
    """
    try:
        root = prepare_user_root(uid)["root"]
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    sessions_root = os.path.join(root, "sessions")
    out = []
    if os.path.isdir(sessions_root):
        for sid in os.listdir(sessions_root):
            folder = os.path.join(sessions_root, sid)
            if not os.path.isdir(folder) or os.path.islink(folder):
                continue
            manifest = {}
            try:
                with open(os.path.join(folder, ".session.json"), encoding="utf-8") as handle:
                    manifest = json.load(handle)
            except (OSError, ValueError):
                manifest = {}
            files = []
            for name in sorted(os.listdir(folder)):
                if name.startswith("."):
                    continue
                full = os.path.join(folder, name)
                if os.path.isfile(full) and not os.path.islink(full):
                    files.append({"name": name, "path": f"sessions/{sid}/{name}",
                                  "bytes": os.path.getsize(full)})
            title = ""
            try:
                import sessions as _sessions
                src = _sessions._path(sid)
                with open(src, encoding="utf-8") as handle:
                    data = json.load(handle)
                turns = data.get("turns") or []
                title = str((turns[0] or {}).get("question") or "")[:80]
            except Exception:
                title = ""
            out.append({"sid": sid, "title": title, "at": manifest.get("materialized_at") or 0,
                        "files_count": len(files), "files": files})
    out.sort(key=lambda x: x.get("at") or 0, reverse=True)
    return {"ok": True, "uid": _safe_id(uid), "archive": out[:max(1, int(limit or 60))],
            "total": len(out)}


def list_sessions(uid: str = "default", path: str = "") -> dict:
    return _category("sessions", uid, path)

def list_projects(uid: str = "default", path: str = "") -> dict:
    return _category("projects", uid, path)

def list_evolution(uid: str = "default", path: str = "") -> dict:
    return _category("evolution", uid, path)

def list_memory(uid: str = "default", path: str = "") -> dict:
    return _category("memory", uid, path)

def list_personal(uid: str = "default", path: str = "") -> dict:
    return _category("personal", uid, path)

def _running_container(uid: str):
    import cloud_desktop
    docker = cloud_desktop._docker_env()
    if not docker.get("daemon_ready"):
        return cloud_desktop, docker, None, {"ok": False, "error": "Docker daemon unavailable: " + str(docker.get("reason") or "unknown")}
    container = cloud_desktop._container_state(uid, docker)
    if not container.get("verified") or not container.get("running"):
        ensured = cloud_desktop.ensure_desktop(uid)
        container = cloud_desktop._container_state(uid, docker)
        if not ensured.get("ok") or not container.get("verified") or not container.get("running"):
            return cloud_desktop, docker, container, {"ok": False, "error": "managed cloud desktop is unavailable", "desktop": container, "ensure": ensured}
    return cloud_desktop, docker, container, None

def _container_path(path: Any) -> str:
    path = str(path or "").strip()
    if not path or chr(0) in path:
        raise ValueError("a non-empty container path without null bytes is required")
    if not path.startswith("/"):
        path = CONTAINER_MEYO_ROOT + "/" + path
    normalized = posixpath.normpath(path)
    if normalized != CONTAINER_MEYO_ROOT and not normalized.startswith(CONTAINER_MEYO_ROOT + "/"):
        raise ValueError("container file access is limited to /root/Meyo")
    return normalized

def container_list(uid: str = "default", path: str = CONTAINER_MEYO_ROOT) -> dict:
    try:
        path = _container_path(path)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    cloud, docker, container, error = _running_container(uid)
    if error:
        return error
    result = cloud._docker_run(["exec", container["name"], "ls", "-la", "--", path], timeout=30, docker=docker, stdout_limit=12000)
    return {"ok": bool(result.get("ok")), "uid": _safe_id(uid), "container": container["name"], "path": path, "exit_code": result.get("exit_code"), "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""), "error": result.get("error", "")}

def container_read(uid: str = "default", path: str = CONTAINER_MEYO_ROOT, max_bytes: int = MAX_READ_BYTES) -> dict:
    try:
        path = _container_path(path)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    cloud, docker, container, error = _running_container(uid)
    if error:
        return error
    limit = max(1, min(int(max_bytes or MAX_READ_BYTES), MAX_READ_BYTES))
    result = cloud._docker_run(["exec", container["name"], "head", "-c", str(limit), "--", path], timeout=30, docker=docker, stdout_limit=limit)
    return {"ok": bool(result.get("ok")), "uid": _safe_id(uid), "container": container["name"], "path": path, "exit_code": result.get("exit_code"), "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""), "error": result.get("error", "")}

def desktop_exec(uid: str = "default", command: str = "", timeout_s: int = 60) -> dict:
    command = str(command or "")
    if not command.strip() or chr(0) in command:
        return {"ok": False, "error": "a non-empty command without null bytes is required"}
    try:
        timeout_s = max(1, min(int(timeout_s or 60), MAX_EXEC_TIMEOUT_S))
    except (TypeError, ValueError):
        return {"ok": False, "error": "timeout_s must be an integer"}
    # ★安装类串行化(OOM 根治):检测→flock 容器级全局锁串行(后到者等待·pip 幂等第二遍
    #   基本 no-op)+超时抬到上限(等待+安装都要时间)。检测逻辑在模块级
    #   _is_install_command(可单测·见 tests/test_desktop_install_serialization.py)。
    if _is_install_command(command):
        command = _wrap_install_lock(command)
        timeout_s = MAX_EXEC_TIMEOUT_S
    cloud, docker, container, error = _running_container(uid)
    if error:
        return error
    # 在用户工作区(容器内 Meyo 挂载点 /root/Meyo)里执行,并把 MEYO_ROOT 暴露给命令:
    # 否则 exec 默认从容器根 / 起、$MEYO_ROOT 为空,用户写的 `wc -l file.txt` 这类相对路径
    # 命令找不到 fs/write 落进 /root/Meyo 的文件(write 与 exec 明明共享同一存储)。
    result = cloud._docker_run(
        ["exec", "-w", "/root/Meyo", "-e", "MEYO_ROOT=/root/Meyo", container["name"], "sh", "-lc", command],
        timeout=timeout_s, docker=docker, stdout_limit=24000)
    ran = result.get("exit_code") is not None  # docker exec 真跑完(退出码已回流)=基础设施成功
    return {"ok": bool(ran), "command_failed": bool(ran and result.get("exit_code") != 0), "uid": _safe_id(uid), "container": container["name"], "meyo_root": container.get("meyo_mount") or prepare_user_root(uid)["root"], "command": command, "timeout_s": timeout_s, "exit_code": result.get("exit_code"), "stdout": result.get("stdout", ""), "stderr": result.get("stderr", ""), "error": result.get("error", "")}

def desktop_app(uid: str = "default", action: str = "list", app: str = "", args: Any = None) -> dict:
    action = str(action or "list").lower()
    if action == "list":
        return {**desktop_exec(uid, "ps -eo pid=,comm=,args= | sed -n 1,240p", 30), "action": "list"}
    app = str(app or "").strip()
    if not app or chr(0) in app:
        return {"ok": False, "error": "app is required"}
    if action == "open":
        command = [app] + ([str(item) for item in args] if isinstance(args, list) else ([str(args)] if args else []))
        chromium = any(part in os.path.basename(app).lower() for part in ("chromium", "chrome"))
        # Native Chromium enables its sandbox by default. Preserve argv/profile;
        # reject explicit bypasses, without a Playwright driver or a new context.
        if chromium:
            for index, argument in enumerate(command[1:], 1):
                switch, _, value = argument.partition("=")
                disabled = value or (command[index + 1] if index + 1 < len(command) else "")
                if (re.fullmatch(r"--(?:no-sandbox|disable-[a-z0-9-]*sandbox|single-process)", switch.lower())
                        or (switch.lower() == "--disable-features" and "sandbox" in disabled.lower())):
                    return {"ok": False, "action": "open", "app": app,
                            "error": "managed Chromium refuses a sandbox-disabling argument: " + switch}
        # 先校验应用存在(根修 2026-09-01:nohup 后台启动 exit 恒 0·firefox 不在镜像时
        # 打开交付物静默失败假阳性)。不存在→如实报错+给已装应用列表。
        if not _probe_container_apps(uid, [app]):
            installed = _probe_container_apps(uid, ["thunar", "chromium", "firefox", "mousepad", "gnumeric", "parole"])
            return {"ok": False, "action": "open", "app": app,
                    "error": "应用 " + app + " 未安装(已装:" + (",".join(installed) or "无") + ")",
                    "hint": "可先在真机控制台安装: apt-get install -y " + app}
        log_path = "/tmp/meyo-browser-" + uuid.uuid4().hex + ".log" if chromium else "/tmp/meyo-app.log"
        launch = "DISPLAY=:1 nohup " + " ".join(shlex.quote(item) for item in command) + " >" + shlex.quote(log_path) + " 2>&1 & printf %s " + chr(36) + "!"
        opened = desktop_exec(uid, launch, 30)
        # 启动后校验进程仍存活(等 1.5s 再查·浏览器 root/sandbox 秒退要躲过启动窗口)
        pid = ""
        stdout_text = str(opened.get("stdout") or "").strip()
        if stdout_text:
            pid = stdout_text.splitlines()[-1]
        if chromium:
            if not opened.get("ok") or opened.get("exit_code") != 0 or not re.fullmatch(r"[1-9][0-9]*", pid):
                return {**opened, "ok": False, "action": "open", "app": app, "pid": "",
                        "log_path": log_path, "startup_verified": False,
                        "error": opened.get("stderr") or opened.get("error") or "native Chromium launch returned no valid PID"}
            time.sleep(1.5)
            check = desktop_exec(uid, "kill -0 " + pid + " 2>/dev/null && ps -o stat= -p " + pid
                                 + " 2>/dev/null | grep -Eq '^[[:space:]]*[^[:space:]Z]' && echo alive", 10)
            alive = check.get("exit_code") == 0 and str(check.get("stdout") or "").strip() == "alive"
            result = {**opened, "action": "open", "app": app, "pid": pid, "log_path": log_path,
                      "ok": alive, "startup_verified": alive, "sandbox_policy": "native_default"}
            if not alive:
                tail = desktop_exec(uid, "tail -c 8000 " + shlex.quote(log_path) + " 2>/dev/null", 10)
                result.update(error=tail.get("stdout") or tail.get("stderr") or check.get("error")
                              or "native Chromium launcher exited; existing-window handoff is unverified",
                              log=tail.get("stdout") or "", startup_check=check)
            return result
        if pid and opened.get("ok"):
            import time as _time
            _time.sleep(1.5)
            # 启动器退出≠应用退出:浏览器把新窗口转交既有实例时启动器即退
            # (日志 Opening in existing browser session),所以应用进程还活着就算成功。
            check = desktop_exec(uid, "kill -0 " + pid + " 2>/dev/null && echo alive || (pgrep -f " + shlex.quote(app) + " >/dev/null 2>&1 && echo alive)", 10)
            if "alive" not in str(check.get("stdout") or ""):
                log_tail = desktop_exec(uid, "tail -c 300 /tmp/meyo-app.log 2>/dev/null", 10)
                return {"ok": False, "action": "open", "app": app, "pid": pid,
                        "error": "应用 " + app + " 启动后立即退出(日志:容器内 /tmp/meyo-app.log)",
                        "log": str(log_tail.get("stdout") or "")[:300]}
        return {**opened, "action": "open", "app": app, "pid": pid}
    if action == "close":
        pattern = ("[" + app[0] + "]" + app[1:]) if len(app) > 1 else app
        result = desktop_exec(uid, "pkill -f -- " + shlex.quote(pattern), 30)
        if result.get("exit_code") in (0, 1, 143):
            result["ok"] = True
        return {**result, "action": "close", "app": app}
    return {"ok": False, "error": "action must be list, open, or close"}

def bootstrap(uid: str = "default") -> dict:
    prepared = prepare_user_root(uid)
    packages = ("vim", "git", "htop", "tmux", "ripgrep", "fzf", "thunar")
    check = "missing=; for p in " + " ".join(packages) + "; do command -v \"$p\" >/dev/null 2>&1 || missing=\"$missing $p\"; done; printf \"%s\" \"$missing\""
    before = desktop_exec(uid, check, 30)
    if not before.get("ok"):
        return {"ok": False, "action": "bootstrap", "prepared": prepared, "check": before}
    missing = str(before.get("stdout") or "").split()
    install = None
    if missing:
        install = desktop_exec(uid, "DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y " + " ".join(shlex.quote(item) for item in missing), MAX_EXEC_TIMEOUT_S)
        if not install.get("ok") or install.get("command_failed") or install.get("exit_code") not in (0, None):
            return {"ok": False, "action": "bootstrap_pending", "prepared": prepared,
                    "missing": missing, "install": install,
                    "error": "required desktop packages were not installed",
                    "recoverable": True}
    marker = {"uid": _safe_id(uid), "installed_at": time.time(), "packages": list(packages), "missing_before": missing}
    try:
        _atomic_write_file(os.path.join(prepared["root"], ".meyo-bootstrap.json"), json.dumps(marker, ensure_ascii=False, indent=2).encode("utf-8"))
    except OSError as error:
        return {"ok": False, "action": "bootstrap", "error": f"cannot save bootstrap marker: {error}"}
    return {"ok": True, "action": "installed" if missing else "already_ready", "prepared": prepared, "missing_before": missing, "marker": marker, "install": install}

def _allowed_artifact(path: Any) -> str | None:
    try:
        import artifact_delivery
        checked = artifact_delivery._safe(str(path or ""))
        return checked[0] if checked else None
    except Exception:
        return None

def materialize_session(uid: str, sid: str, result: dict) -> dict:
    prepared = prepare_user_root(uid)
    safe_sid = _safe_id(sid, "session")
    destination_prefix = f"sessions/{safe_sid}"
    made = make_dir(uid, destination_prefix)
    if not made.get("ok"):
        return {"ok": False, "uid": _safe_id(uid), "sid": safe_sid, "error": made.get("error", "cannot create session directory")}
    candidates = list((result or {}).get("discovered_artifacts") or [])
    for member in (result or {}).get("results") or []:
        if isinstance(member, dict):
            candidates.extend(member.get("artifacts") or [])
    copied, seen = [], set()
    for item in candidates:
        source = _allowed_artifact(item.get("path") if isinstance(item, dict) else "")
        if not source or source in seen or not os.path.isfile(source):
            continue
        seen.add(source)
        filename = os.path.basename(source)
        stem, suffix = os.path.splitext(filename)
        ordinal = 1
        while True:
            output_name = filename if ordinal == 1 else f"{stem}-{ordinal}{suffix}"
            relative = f"{destination_prefix}/{output_name}"
            target = os.path.join(prepared["root"], *relative.split("/"))
            if source == target:
                copied.append({"name": output_name, "path": relative, "source": source, "bytes": os.path.getsize(target)})
                break
            if ordinal > 999:
                copied.append({"name": filename, "source": source, "error": "too many duplicate names in session directory"})
                break
            if os.path.lexists(target):
                # 目标名已被占用(含符号链接):不覆盖也不穿透,换下一个名字。
                ordinal += 1
                continue
            try:
                size = _atomic_write_file(target, _read_source_bytes(source))
                copied.append({"name": output_name, "path": relative, "source": source, "bytes": size})
            except OSError as error:
                copied.append({"name": output_name, "source": source, "error": str(error)})
            break
    manifest = {"uid": _safe_id(uid), "sid": safe_sid, "materialized_at": time.time(), "files": copied}
    try:
        _atomic_write_file(os.path.join(prepared["root"], "sessions", safe_sid, ".session.json"), json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
    except OSError as error:
        return {"ok": False, "error": f"cannot save session manifest: {error}", "uid": _safe_id(uid), "sid": safe_sid}
    return {"ok": True, "uid": _safe_id(uid), "sid": safe_sid, "path": destination_prefix, "files": copied, "copied_count": sum(1 for item in copied if not item.get("error"))}



# -- 真机交互三件套(网页控制真机+交付物真机打开+个人数据进卷) --


# GUI 应用映射:每扩展名给候选列表(按优先级)。CUA 官方镜像实测只有 chromium/thunar
# (2026-09-01:firefox/mousepad/gnumeric/parole 都不在镜像·旧映射写死 firefox→"打开交付物html"
# 静默失败·nohup 后台 exit0 假阳性)。open_in_desktop 逐个探测容器里真实存在的应用。
_EXT_APP_CANDIDATES = {
    ".html": ["firefox", "chromium", "chromium-browser", "google-chrome"],
    ".htm": ["firefox", "chromium", "chromium-browser", "google-chrome"],
    ".pdf": ["firefox", "chromium", "chromium-browser", "google-chrome"],
    ".png": ["thunar"], ".jpg": ["thunar"], ".jpeg": ["thunar"], ".gif": ["thunar"], ".webp": ["thunar"],
    ".md": ["mousepad", "firefox", "chromium"],
    ".txt": ["mousepad", "firefox", "chromium"],
    ".csv": ["gnumeric", "firefox", "chromium"],
    ".xlsx": ["gnumeric", "firefox", "chromium"],
    ".mp4": ["parole", "firefox", "chromium"],
    ".mp3": ["parole", "firefox", "chromium"], ".wav": ["parole", "firefox", "chromium"],
}
# 兼容旧引用:单值映射(取首选候选)
_EXT_APP_MAP = {ext: cands[0] for ext, cands in _EXT_APP_CANDIDATES.items()}


def _probe_container_apps(uid: str, apps: list[str]) -> list[str]:
    """在当前用户运行中的容器里探测哪些应用真实存在(command -v)。容器没起返回空。

    注意:多候选串联时 exit code 取决于最后一条(127),不能当失败信号——只解析 stdout。
    """
    apps = [str(a).strip() for a in apps if str(a).strip()]
    if not apps:
        return []
    probe_cmd = "; ".join(f"command -v {shlex.quote(a)}" for a in apps)
    result = desktop_exec(uid, probe_cmd, 20)
    if result.get("error") and not result.get("stdout"):
        return []
    found = [line.strip() for line in str(result.get("stdout") or "").splitlines() if line.strip()]
    present = []
    for path in found:
        name = os.path.basename(path)
        if name in apps and name not in present:
            present.append(name)
    return present

def open_in_desktop(uid: str = "default", path: str = "", app: str = "") -> dict:
    """把 ~/Meyo 里的交付物在真机桌面上真实打开(app 不传则按扩展名探测;失败如实回流)。"""
    try:
        target, relative = _resolve(uid, path, require_file=True)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    if not os.path.isfile(target):
        return {"ok": False, "error": "file not found", "path": relative}
    ext = os.path.splitext(relative)[1].lower()
    candidates = ([str(app).strip()] if str(app or "").strip() else _EXT_APP_CANDIDATES.get(ext, ["thunar"]))
    present = _probe_container_apps(uid, candidates)
    if not present:
        installed = _probe_container_apps(uid, ["thunar", "chromium", "firefox", "mousepad", "gnumeric", "parole"])
        return {"ok": False, "action": "open_in_desktop", "path": relative,
                "error": "容器里没有可打开该文件的应用,候选:" + ",".join(candidates) + ";已装:" + (",".join(installed) or "无"), 
                "hint": "可在真机控制台先安装,如: apt-get install -y firefox"}
    app_name = present[0]
    container_path = CONTAINER_MEYO_ROOT + "/" + relative
    out = desktop_app(uid, "open", app_name, [container_path])
    return {**out, "action": "open_in_desktop", "path": relative,
            "container_path": container_path, "app": app_name,
            "app_candidates": candidates, "app_present": present}


def _sync_file(source: str, target: str) -> dict:
    """把军团侧文件镜像进个人卷(原子写·只覆盖镜像,不动事实源)。"""
    try:
        size = _atomic_write_file(target, _read_source_bytes(source))
        return {"name": os.path.basename(source), "bytes": size, "ok": True}
    except OSError as error:
        return {"name": os.path.basename(source), "error": str(error)}


def _sync_tree(source: str, target: str, limit: int = 200) -> list[dict]:
    out = []
    count = 0
    for base, _dirs, files in os.walk(source):
        for fname in files:
            if count >= limit:
                out.append({"name": "...", "error": "sync limit reached"})
                return out
            count += 1
            src = os.path.join(base, fname)
            rel = os.path.relpath(src, source)
            out.append(_sync_file(src, os.path.join(target, rel)))
    return out


def sync_personal_data(uid: str = "default") -> dict:
    """把用户的军团个人数据同步进 ~/Meyo/personal(记忆/配置/会话)只读镜像。

    单一事实源仍在军团侧(legion_paths 系目录);幂等可重跑,不删任何已有文件。
    """
    prepared = prepare_user_root(uid)
    root = prepared["root"]
    report = {"ok": True, "uid": _safe_id(uid), "synced_at": time.time(), "sections": {}}
    import legion_paths
    legion_root = legion_paths.legion_dir()
    mem_dir = os.path.join(root, "personal", "memory")
    os.makedirs(mem_dir, exist_ok=True)
    mem_file = os.path.join(legion_root, "experience.jsonl")
    report["sections"]["memory"] = ([_sync_file(mem_file, os.path.join(mem_dir, "experience.jsonl"))]
                                    if os.path.isfile(mem_file) else [])
    cfg_dir = os.path.join(root, "personal", "configs")
    os.makedirs(cfg_dir, exist_ok=True)
    cfgs = []
    prefs = os.path.join(legion_root, "model_prefs.json")
    if os.path.isfile(prefs):
        cfgs.append(_sync_file(prefs, os.path.join(cfg_dir, "model_prefs.json")))
    evolve_dir = os.path.join(legion_root, "evolve")
    if os.path.isdir(evolve_dir):
        cfgs.extend(_sync_tree(evolve_dir, cfg_dir, limit=50))
    report["sections"]["configs"] = cfgs
    import sessions as _sessions
    sess_dir = os.path.join(root, "personal", "sessions")
    os.makedirs(sess_dir, exist_ok=True)
    sess_files = []
    try:
        for item in _sessions.list_sessions(limit=100, user_id=str(uid or "default")):
            sid = str(item.get("sid") or "")
            if not sid:
                continue
            src = _sessions._path(sid)
            if os.path.isfile(src):
                sess_files.append(_sync_file(src, os.path.join(sess_dir, sid + ".json")))
    except Exception as error:
        sess_files = [{"error": type(error).__name__ + ": " + str(error)}]
    report["sections"]["sessions"] = sess_files
    report["files_total"] = sum(len(v) for v in report["sections"].values())
    return report


def desktop_screenshot(uid: str = "default") -> dict:
    """真机当前桌面截屏(import/scrot/xwd→CUA原生screenshot兜底)→ 持久卷 personal/screenshots/。"""
    cloud, docker, container, error = _running_container(uid)
    if error:
        return error
    name = "shot-" + str(int(time.time())) + ".png"
    rel = "personal/screenshots/" + name
    run = cloud._docker_run(
        ["exec", "-w", "/root/Meyo", "-e", "DISPLAY=:1", container["name"], "sh", "-lc",
         "mkdir -p personal/screenshots && (import -window root " + rel +
         " 2>/dev/null || scrot " + rel + " 2>/dev/null || xwd -root -out " + rel + ")"],
        timeout=30, docker=docker, stdout_limit=2000)
    host = os.path.join(prepare_user_root(uid)["root"], *rel.split("/"))
    used = "imagemagick/scrot/xwd"
    # 兜底1(2026-09-01实测最可靠):CUA 镜像无 import/scrot/xwd;但 computer-server
    # 自带 venv(含 PIL)→ DISPLAY=:1 ImageGrab 直接抓 X 桌面(1024x768 PNG)。
    if not (run.get("ok") and os.path.isfile(host) and os.path.getsize(host) > 0):
        pil = cloud._docker_run(
            ["exec", "-e", "DISPLAY=:1", container["name"], "/opt/computer-server/venv/bin/python", "-c",
             "from PIL import ImageGrab; im=ImageGrab.grab(); im.save(\"/root/Meyo/" + rel + "\"); print(im.size)"],
            timeout=30, docker=docker, stdout_limit=500)
        if pil.get("ok") and os.path.isfile(host) and os.path.getsize(host) > 0:
            used = "pil-imagegrab"
    # 兜底2:CUA computer-server 原生 screenshot(成功时 image_data 字段是 base64)。
    if not (os.path.isfile(host) and os.path.getsize(host) > 0):
        try:
            native = cloud._local_computer_command(uid, "screenshot", {})
            payload = (native or {}).get("result") or {}
            best = str(payload.get("image_data") or "")
            if len(best) > 256:
                raw = base64.b64decode(best + "=" * (-len(best) % 4))
                if raw[:8] == b"\\x89PNG\\r\\n\\x1a\\n" or raw[:2] == b"\\xff\\xd8":
                    _atomic_write_file(host, raw)
                    used = "cua-native-screenshot"
        except Exception:
            pass
    ok = bool(os.path.isfile(host) and os.path.getsize(host) > 0)
    return {"ok": ok, "action": "screenshot", "uid": _safe_id(uid), "path": rel,
            "container": container["name"], "exit_code": run.get("exit_code"),
            "stderr": run.get("stderr", ""), "method": used,
            "bytes": os.path.getsize(host) if os.path.isfile(host) else 0}

def local_mcp_tools() -> list[dict]:
    # 身份由调用方绑定(bound_uid / MEYO_LEGION_UID),工具参数不再暴露 uid。
    path = {"type": "string"}
    return [
        {"name": "desktop_fs_list", "description": "列出当前会话绑定用户持久 ~/Meyo 卷中的真实文件。", "input_schema": {"type": "object", "properties": {"path": path}}},
        {"name": "desktop_fs_read", "description": "读取当前会话绑定用户 ~/Meyo 卷中的文件。", "input_schema": {"type": "object", "properties": {"path": path}, "required": ["path"]}},
        {"name": "desktop_fs_write", "description": "写入当前会话绑定用户 ~/Meyo 卷。", "input_schema": {"type": "object", "properties": {"path": path, "content": {"type": "string"}, "append": {"type": "boolean"}}, "required": ["path", "content"]}},
        {"name": "desktop_fs_mkdir", "description": "创建当前会话绑定用户 ~/Meyo 卷中的目录。", "input_schema": {"type": "object", "properties": {"path": path}, "required": ["path"]}},
        {"name": "desktop_fs_list_sessions", "description": "列出当前会话绑定用户 ~/Meyo/sessions 真实交付物。", "input_schema": {"type": "object", "properties": {"path": path}}},
        {"name": "desktop_fs_list_projects", "description": "列出当前会话绑定用户 ~/Meyo/projects 真实项目文件。", "input_schema": {"type": "object", "properties": {"path": path}}},
        {"name": "desktop_fs_list_evolution", "description": "列出当前会话绑定用户 ~/Meyo/evolution 真实自进化文件。", "input_schema": {"type": "object", "properties": {"path": path}}},
        {"name": "desktop_fs_list_memory", "description": "列出当前会话绑定用户 ~/Meyo/memory 真实记忆文件。", "input_schema": {"type": "object", "properties": {"path": path}}},
        {"name": "desktop_exec", "description": "在当前会话绑定用户已运行的受管 CUA 容器内执行任意 shell 命令。", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "timeout_s": {"type": "integer"}}, "required": ["command"]}},
        {"name": "desktop_app", "description": "列出、打开或关闭当前会话绑定用户 CUA 桌面软件。", "input_schema": {"type": "object", "properties": {"action": {"type": "string"}, "app": {"type": "string"}, "args": {"type": "array", "items": {"type": "string"}}}}},
{"name": "desktop_open_file", "description": "把 ~/Meyo 里的交付物在当前用户云真机桌面上真实打开(默认按扩展名选 GUI 应用)。", "input_schema": {"type": "object", "properties": {"path": path, "app": {"type": "string"}}, "required": ["path"]}},
        {"name": "desktop_sync_personal", "description": "把当前用户的军团个人数据(记忆/配置/会话历史)同步进 ~/Meyo/personal 只读镜像。", "input_schema": {"type": "object"}},
        {"name": "desktop_screenshot", "description": "截取当前用户云真机桌面屏幕,保存进 ~/Meyo/personal/screenshots。", "input_schema": {"type": "object"}},
        {"name": "desktop_control", "description": "直接控制云真机桌面GUI:鼠标移动/左右双击/拖拽/输入文字/按键/滚动。command 可为 move_cursor(x,y)/left_click(x,y)/right_click(x,y)/double_click(x,y)/mouse_down/mouse_up/type_text(text)/press_key(key)/hotkey(keys)/scroll(x,y,direction,amount)。返回执行结果;截图用 desktop_screenshot。" + CUA_GUI_INPUT_HELP, "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "params": {"type": "object"}}, "required": ["command"]}},

    ]

def _bound_identity(arguments: dict, bound_uid: str | None) -> tuple[str | None, dict | None]:
    """绑定身份时参数里的 uid 不允许改写;未绑定时保持既有调用方式(兼容现有军团链路)。"""
    env_bound = (os.environ.get("MEYO_LEGION_UID") or os.environ.get("MEYO_USER_ID") or "").strip()
    bound = str(bound_uid).strip() if bound_uid is not None else (env_bound or None)
    if bound is None:
        return None, None
    supplied = arguments.get("uid")
    if supplied is not None and _safe_id(supplied) != _safe_id(bound):
        return None, {"server": "cua-local-desktop", "status": "err:identity",
                      "result": {"ok": False, "error": "desktop identity is bound by the caller and cannot be overridden"}}
    return bound, None

def _desktop_control(uid: str, command: str, params=None) -> dict:
    """GUI 控制(movement/click/type/key/scroll)直达 CUA computer-server·用户 2026-09-01 要求的鼠标控制。"""
    import cloud_desktop
    return cloud_desktop._local_computer_command(uid, command, params or {})

def local_mcp_call(name: str, arguments: dict | None = None, *, bound_uid: str | None = None) -> dict:
    arguments = arguments or {}
    bound, denied = _bound_identity(arguments, bound_uid)
    if denied:
        return {**denied, "tool": name}
    uid = bound if bound is not None else str(arguments.get("uid") or "default")
    calls = {
        "desktop_fs_list": lambda: list_files(uid, arguments.get("path") or ""),
        "desktop_fs_read": lambda: read_file(uid, arguments.get("path") or "", arguments.get("max_bytes") or MAX_READ_BYTES),
        "desktop_fs_write": lambda: write_file(uid, arguments.get("path") or "", arguments.get("content") or "", append=bool(arguments.get("append")), content_base64=str(arguments.get("content_base64") or "")),
        "desktop_fs_mkdir": lambda: make_dir(uid, arguments.get("path") or ""),
        "desktop_fs_list_sessions": lambda: list_sessions(uid, arguments.get("path") or ""),
        "desktop_fs_list_projects": lambda: list_projects(uid, arguments.get("path") or ""),
        "desktop_fs_list_evolution": lambda: list_evolution(uid, arguments.get("path") or ""),
        "desktop_fs_list_memory": lambda: list_memory(uid, arguments.get("path") or ""),
        "desktop_exec": lambda: desktop_exec(uid, arguments.get("command") or "", arguments.get("timeout_s") or 60),
        "desktop_app": lambda: desktop_app(uid, arguments.get("action") or "list", arguments.get("app") or "", arguments.get("args")),
        "desktop_open_file": lambda: open_in_desktop(uid, arguments.get("path") or "", arguments.get("app") or ""),
        "desktop_sync_personal": lambda: sync_personal_data(uid),
        "desktop_screenshot": lambda: desktop_screenshot(uid),
        "desktop_control": lambda: _desktop_control(uid, arguments.get("command") or "", arguments.get("params")),
    }
    if name not in calls:
        return {"server": "cua-local-desktop", "tool": name, "status": "unknown_tool", "available": [item["name"] for item in local_mcp_tools()]}
    result = calls[name]()
    return {"server": "cua-local-desktop", "tool": name, "status": "ok" if result.get("ok") else "err:desktop_fs", "result": result}
