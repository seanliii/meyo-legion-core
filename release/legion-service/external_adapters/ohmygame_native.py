#!/usr/bin/env python3
"""Owned OhMyGame desktop connection for the existing capability tool bus.

The official app owns Pi, game_use, its model credentials and conversations.
This adapter only opens that app and forwards the caller's native API requests.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import legion_proc_registry as registry
from ohmygame_install import APP_NAME, RECEIPT, SOURCE_SHA, VERSION, ASSET_SHA256

KIND = "meyo-ohmygame-desktop-v1"


def private_directory(path):
    if path.is_symlink():
        raise ValueError("private profile must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private profile ownership or permissions do not match")
    return path


def run(command, timeout=30, env=None):
    child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
    try:
        if not registry.register(child.pid, pgid=child.pid, owner=os.getpid(),
                                 meta={"kind": "ohmygame-connection-operation"}):
            raise ValueError("native operation registration failed")
        stdout, stderr = child.communicate(timeout=timeout)
        return child.returncode, stdout, stderr
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate(timeout=3)
        registry.unregister(child.pid)


def process_identity(pid):
    if type(pid) is not int or pid <= 1:
        return None
    code, stdout, _ = run(["/bin/ps", "-ww", "-p", str(pid),
                           "-o", "lstart=", "-o", "uid=", "-o", "pgid=", "-o", "comm="],
                          timeout=5, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    if code or not stdout.strip():
        return None
    parts = stdout.strip().split(None, 7)
    if len(parts) != 8:
        raise ValueError("native process identity could not be read")
    return {"birth": " ".join(parts[:5]), "uid": int(parts[5]),
            "pgid": int(parts[6]), "comm": parts[7]}


def owned_state(record, executable):
    if not record:
        return None
    if record.get("kind") != KIND or record.get("app") != str(executable):
        raise ValueError("unrecognized existing desktop record")
    current = process_identity(record.get("pid"))
    if current is None:
        return None
    if (current["birth"] != record.get("birth") or current["comm"] != str(executable)
            or current["uid"] != os.getuid() or current["pgid"] != record["pid"]):
        raise ValueError("native process identity changed; no adoption or signal performed")
    return record


def read_record(path):
    if not os.path.lexists(path):
        return None
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 16384):
            raise ValueError("unsafe native connection record")
        return json.load(stream)


def save_record(path, record):
    fd, temporary = tempfile.mkstemp(prefix=".desktop-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def installed(runtime):
    """Cheap runtime metadata probe; full archive/signature checks belong to the installer."""
    app = runtime / "app" / APP_NAME
    for parent in (runtime, runtime / "app", app, app / "Contents"):
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError("OhMyGame is not installed in this runtime")
    receipt_path = runtime / "app" / RECEIPT
    if receipt_path.is_symlink():
        raise ValueError("unsafe install receipt")
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get("installer") != "ohmygame_install" or receipt.get("version") != VERSION
            or receipt.get("source_sha") != SOURCE_SHA or receipt.get("archive_sha256") != ASSET_SHA256):
        raise ValueError("native installation has a different declared identity")
    metadata = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    name = metadata.get("CFBundleExecutable")
    if (metadata.get("CFBundleShortVersionString") != VERSION or not isinstance(name, str)
            or name in ("", ".", "..") or Path(name).name != name):
        raise ValueError("native app metadata changed")
    executable = app / "Contents/MacOS" / name
    if not executable.is_file() or executable.is_symlink() or not os.access(executable, os.X_OK):
        raise ValueError("native executable is missing")
    return app, executable


def inspector_port(record, private):
    file = private / "chromium/DevToolsActivePort"
    if file.is_file() and not file.is_symlink():
        text = file.read_text()[:4096]
        line = text.splitlines()[0] if text.splitlines() else ""
        if line.isdigit() and 1024 <= int(line) <= 65535:
            return int(line)
    # Only inspect stdout from the app process launched by this adapter.
    logfile = Path(record["log"])
    if logfile.parent != private or logfile.is_symlink():
        raise ValueError("native log escaped its private profile")
    with logfile.open("rb") as stream:
        text = stream.read(262144).decode(errors="replace")
    matches = re.findall(r"(?m)^DevTools listening on ws://127\.0\.0\.1:(\d+)/devtools/browser/", text)
    return int(matches[-1]) if matches else None


def verify_listener(pid, port):
    code, stdout, _ = run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid),
                           "-iTCP:" + str(port), "-sTCP:LISTEN", "-Fpn"], timeout=8)
    lines = stdout.splitlines()
    addresses = [line[1:] for line in lines if line.startswith("n")]
    if code or "p" + str(pid) not in lines or addresses != [f"127.0.0.1:{port}"]:
        raise ValueError("native inspector is not the owned process's exact loopback listener")


def open_desktop(runtime, app, executable, private):
    lock_path = private / "start.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_file = private / "desktop.json"
        previous = read_record(state_file)
        current = owned_state(previous, executable)
        if current:
            return current
        for name in ("home", "tmp", "data", "pi-agent", "chromium"):
            private_directory(private / name)
        old_port = private / "chromium/DevToolsActivePort"
        if os.path.lexists(old_port):
            if not previous or old_port.is_symlink() or not old_port.is_file():
                raise ValueError("unowned existing browser profile; no second desktop started")
            old_port.unlink()
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
                          "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"}}
        env.update(HOME=str(private / "home"), TMPDIR=str(private / "tmp"),
                   OHMYGAME_DATA_DIR=str(private / "data"),
                   PI_CODING_AGENT_DIR=str(private / "pi-agent"))
        log = private / f"desktop-{time.time_ns()}.log"
        log_fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(log_fd, "wb") as output:
            process = subprocess.Popen(
                [str(executable), "--user-data-dir=" + str(private / "chromium"),
                 "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0"],
                env=env, cwd=str(private), stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True)
        # This is an intentionally user-facing, self-managed GUI, not a background
        # job whose lifetime ends with this short CLI call. The app owns its daemon.
        registry.register(process.pid, pgid=process.pid, owner=process.pid,
                          meta={"kind": "ohmygame-user-facing-desktop", "profile": str(private)})
        identity = process_identity(process.pid)
        if not identity:
            process.wait(timeout=3)
            registry.unregister(process.pid)
            raise ValueError("native app exited before startup; private log preserved")
        record = {"kind": KIND, "pid": process.pid, "birth": identity["birth"],
                  "app": str(executable), "log": str(log), "profile": str(private),
                  "started_at": time.time()}
        save_record(state_file, record)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if process.poll() is not None:
                registry.unregister(process.pid)
                raise ValueError("native app exited during startup; private log preserved")
            port = inspector_port(record, private)
            if port:
                verify_listener(process.pid, port)
                record["port"] = port
                save_record(state_file, record)
                registry.register(process.pid, pgid=process.pid, owner=process.pid,
                                  meta={"kind": "ohmygame-user-facing-desktop", "profile": str(private)})
                return record
            time.sleep(0.25)
        raise ValueError("desktop remains open but connection is not ready; no restart or model submission")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--node", default="node")
    parser.add_argument("action", choices=["installed", "open", "status", "models", "projects", "inspect",
                                          "create", "delegate", "continue", "result", "cancel"])
    parser.add_argument("--request-file", type=Path)
    args = parser.parse_args(argv)
    try:
        runtime = args.runtime_dir.expanduser().absolute()
        if runtime.is_symlink():
            raise ValueError("runtime may not be redirected")
        app, executable = installed(runtime)
        if args.action == "installed":
            print(json.dumps({"installed": True, "app_path": str(app), "version": VERSION,
                              "readiness_basis": "installed_bundle_metadata_not_model_execution"}))
            return 0
        private = private_directory(runtime / "operator")
        record = read_record(private / "desktop.json")
        current = (open_desktop(runtime, app, executable, private) if args.action == "open"
                   else owned_state(record, executable))
        if not current:
            print(json.dumps({"ok": args.action == "status", "installed": True,
                              "status": "not_running", "needs_open": True}))
            return 0 if args.action == "status" else 2
        port = current.get("port") or inspector_port(current, private)
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("native inspector is not ready; existing desktop preserved")
        verify_listener(current["pid"], port)
        command = [args.node, str(HERE / "ohmygame_protocol.mjs"), str(port), str(app),
                   "status" if args.action == "open" else args.action]
        command.append(str(args.request_file.expanduser().absolute()) if args.request_file else "")
        receipt = None
        if args.action in {"delegate", "continue", "create", "cancel"}:
            directory = private_directory(private / "operations")
            receipt = directory / (secrets.token_hex(16) + ".json")
            save_record(receipt, {"action": args.action, "stage": "connector_starting", "submission": "not_sent"})
            command.append(str(receipt))
        # No provider keys are forwarded to this connector. It only reads the
        # native app's short-lived token in memory and talks to that loopback app.
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
        env.update(HOME=str(private / "home"), TMPDIR=str(private / "tmp"))
        try:
            code, stdout, _ = run(command, timeout=50, env=env)
        except subprocess.TimeoutExpired:
            known = read_record(receipt) if receipt else None
            print(json.dumps({"ok": False, "status": "native_connector_timeout",
                              "native": known, "receipt_file": str(receipt) if receipt else None,
                              "next": "Inspect the same native IDs before retrying; the desktop/task was not cancelled."},
                             ensure_ascii=False))
            return 1
        try:
            result = json.loads(stdout.strip())
        except ValueError:
            raise ValueError("native connector did not return a complete JSON result") from None
        if receipt:
            result["receipt_file"] = str(receipt)
        print(json.dumps(result, ensure_ascii=False))
        return code
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "status": "desktop_connection_error", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
