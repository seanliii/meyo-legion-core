"""Identity-checked cleanup for one private native SDK instance, not a daemon manager."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time

import legion_proc_registry as registry


def _require_linux():
    if not sys.platform.startswith("linux"):
        raise RuntimeError("JCode SDK managed execution requires linux")


def _proc_status(root):
    return dict(line.split(":", 1) for line in (root / "status").read_text().splitlines()
                if ":" in line)


def _namespace_ids(status, key):
    ids = [int(value) for value in status.get(key, "").split()]
    if not ids or any(value <= 0 for value in ids):
        raise ValueError("JCode procfs namespace identity is unavailable")
    return ids


def _proc_view():
    root = Path("/proc/self")
    status = _proc_status(root)
    ids = _namespace_ids(status, "NSpid")
    if ids[-1] != os.getpid() or int(status["Pid"]) != ids[0]:
        raise ValueError("JCode caller namespace does not match procfs")
    return len(ids) - 1, os.readlink(root / "ns/pid")


def _pidfd_mapping(fd, pid, depth):
    info = dict(line.split(":", 1) for line in
                Path("/proc/self/fdinfo", str(fd)).read_text().splitlines() if ":" in line)
    outer = int(info.get("Pid", "0"))
    if outer == -1:
        raise ProcessLookupError("JCode pidfd process has exited")
    ids = _namespace_ids(info, "NSpid")
    if outer <= 0 or ids[0] != outer or len(ids) <= depth or ids[depth] != pid:
        raise ValueError("JCode pidfd namespace mapping does not match requested PID")
    return outer, ids


def _process(pid):
    """PID/PPID/PGID are caller-local; procfs_pid is only a verified read address."""
    pid, fd = int(pid), None
    if pid <= 0:
        raise ValueError("JCode process PID must be positive")
    depth, namespace = _proc_view()
    try:
        outer, mapped_ids = pid, None
        if depth:
            if not hasattr(os, "pidfd_open"):
                raise RuntimeError("JCode shifted procfs view requires pidfd_open")
            fd = os.pidfd_open(pid, 0)
            outer, mapped_ids = _pidfd_mapping(fd, pid, depth)
        root = Path("/proc") / str(outer)
        raw = (root / "stat").read_text()
        fields = raw.rsplit(") ", 1)[1].split()
        if int(raw.split(" ", 1)[0]) != outer:
            raise ValueError("JCode procfs stat PID does not match")
        # Like the old missing-exe path: a zombie is terminal, not a live
        # pidfd/procfs mismatch (zombies no longer have an executable link).
        if fields[0] == "Z":
            return None
        status = _proc_status(root)
        ids, groups = _namespace_ids(status, "NSpid"), _namespace_ids(status, "NSpgid")
        target_namespace = os.readlink(root / "ns/pid")
        if (int(raw.split(" ", 1)[0]) != outer or int(status["Pid"]) != outer
                or ids[0] != outer or len(ids) <= depth or ids[depth] != pid
                or (mapped_ids is not None and ids != mapped_ids)
                or len(groups) != len(ids) or groups[0] != int(fields[2])
                or (len(ids) == depth + 1 and target_namespace != namespace)):
            raise ValueError("JCode target procfs namespace identity does not match")
        parent = int(fields[1])
        if int(status["PPid"]) != parent:
            raise ValueError("JCode parent changed during procfs inspection")
        if depth and parent:
            try:
                parent_status = _proc_status(Path("/proc") / str(parent))
                parent_ids = _namespace_ids(parent_status, "NSpid")
            except FileNotFoundError as exc:
                raise ValueError("JCode parent namespace identity is unavailable") from exc
            if parent_ids[0] != parent or int(parent_status["Pid"]) != parent:
                raise ValueError("JCode parent procfs identity does not match")
            if len(parent_ids) <= depth:
                if pid != 1:
                    raise ValueError("JCode parent is outside the caller namespace")
                parent = 0
            else:
                parent = parent_ids[depth]
        result = {"pid": pid, "ppid": parent, "pgid": groups[depth], "procfs_pid": outer,
                  "uid": root.stat().st_uid, "birth": fields[19], "state": fields[0],
                  "exe": os.readlink(root / "exe"), "namespace": target_namespace}
        after = (root / "stat").read_text().rsplit(") ", 1)[1].split()
        if any(after[index] != fields[index] for index in (1, 2, 19)):
            raise ValueError("JCode process changed during procfs inspection")
        if fd is not None and _pidfd_mapping(fd, pid, depth) != (outer, ids):
            raise ValueError("JCode pidfd identity changed during procfs inspection")
        return result
    except ProcessLookupError:
        return None
    except FileNotFoundError as exc:
        if fd is not None:
            try:
                _pidfd_mapping(fd, pid, depth)
            except ProcessLookupError:
                return None
            raise ValueError("JCode live pidfd has no verifiable procfs identity") from exc
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _process_env(identity):
    keys = ("pid", "ppid", "pgid", "uid", "birth", "exe", "namespace")
    before = _process(identity["pid"])
    outer = identity.get("procfs_pid", identity["pid"])
    if (not before or before["procfs_pid"] != outer
            or any(before.get(key) != identity.get(key) for key in keys)):
        raise ValueError("JCode process identity changed before reading its environment")
    raw = Path("/proc", str(outer), "environ").read_bytes()
    after = _process(identity["pid"])
    if (not after or after["procfs_pid"] != outer
            or any(after.get(key) != before.get(key) for key in keys)):
        raise ValueError("JCode process changed while reading its environment")
    return {name.decode(): value.decode() for entry in raw.split(b"\0")
            for name, sep, value in [entry.partition(b"=")]
            if sep and name in {b"JCODE_HOME", b"JCODE_RUNTIME_DIR", b"LD_LIBRARY_PATH"}}


def _descendants(pid):
    rows, pending, seen = [], [pid], {pid}
    depth, _ = _proc_view()
    while pending:
        parent = pending.pop()
        parent_identity = _process(parent)
        if not parent_identity:
            continue
        root = Path("/proc") / str(parent_identity["procfs_pid"]) / "task"
        try:
            tasks = list(root.iterdir())
        except FileNotFoundError:
            continue
        for task in tasks:
            try:
                children = task.joinpath("children").read_text().split()
            except FileNotFoundError:
                continue
            for child in children:
                outer = int(child)
                try:
                    status = _proc_status(Path("/proc") / str(outer))
                except FileNotFoundError:
                    continue
                ids = _namespace_ids(status, "NSpid")
                if ids[0] != outer or len(ids) <= depth:
                    raise ValueError("JCode child namespace mapping is unavailable")
                child = ids[depth]
                current = _process(child)
                if current is None:
                    continue
                if current["procfs_pid"] != outer or current["ppid"] != parent:
                    raise ValueError("JCode child procfs identity or parent does not match")
                if not _same(parent_identity):
                    raise ValueError("JCode parent changed while observing its children")
                if child in seen:
                    continue
                seen.add(child)
                if len(seen) > 256:
                    raise ValueError("JCode owned process tree exceeds bounded inspection")
                rows.append(current)
                pending.append(child)
    return rows


def _same(identity, *, allow_gone=False):
    current = _process(identity["pid"])
    if current is None or current["state"] == "Z":
        return allow_gone
    return all(current.get(key) == identity.get(key)
               for key in ("pid", "birth", "pgid", "uid", "exe", "namespace"))


def _live(identity):
    current = _process(identity["pid"])
    return bool(current and current["state"] != "Z" and current["birth"] == identity["birth"])


def _directory(path, private=False):
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022)):
        raise ValueError("JCode native directory is not owned and protected")
    return {"dev": info.st_dev, "ino": info.st_ino}


def _scope(home, ipc):
    home, ipc = Path(home), Path(ipc)
    home_identity, ipc_identity = _directory(home, True), _directory(ipc, True)
    alias = ipc / "h"
    if not alias.is_symlink() or alias.lstat().st_uid != os.getuid() or alias.resolve() != home:
        raise ValueError("JCode IPC alias does not match its native home")
    return home_identity, ipc_identity


def _server_pid(home, ipc):
    path = Path(home) / "servers.json"
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_size > 1_000_000 or stat.S_IMODE(info.st_mode) & 0o022):
        raise ValueError("JCode native server registry is unsafe")
    rows = json.loads(path.read_text())
    socket_path = str(Path(ipc) / "h/run/jcode.sock")
    pids = [row["pid"] for row in rows.values() if isinstance(row, dict)
            and row.get("socket") == socket_path and isinstance(row.get("pid"), int)]
    if len(pids) != 1:
        raise ValueError("JCode has no unique registered daemon in this scope")
    return pids[0]


def _socket_identity(path):
    info = Path(path).lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("JCode native socket is not owned")
    return {"dev": info.st_dev, "ino": info.st_ino}


def register_runtime(worker_pid, node_pid, home, ipc_dir, binary, workdir,
                     daemon_pid=None, bridge_pid=None):
    _require_linux()
    home, ipc, binary, work = map(Path, (home, ipc_dir, binary, workdir))
    home, ipc, work = home.resolve(), ipc.resolve(), work.resolve()
    hi, ii = _scope(home, ipc)
    _directory(work)
    node = _process(node_pid)
    if (not node or node["state"] == "Z" or node["uid"] != os.getuid()
            or node["ppid"] != worker_pid or node["pgid"] != node_pid):
        raise ValueError("JCode SDK owner is not a direct owned process group")
    if not binary.is_absolute() or not binary.is_file():
        raise ValueError("JCode fixed executable is missing")
    cleanup = {"protocol": "jcode-sdk-v1", "home": str(home), "ipc_dir": str(ipc),
               "binary": str(binary), "workdir": str(work), "node": node,
               "home_identity": hi, "ipc_identity": ii,
               "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
               "phase": "starting"}
    if daemon_pid is not None or bridge_pid is not None:
        daemon, bridge = _process(daemon_pid), _process(bridge_pid)
        if (not daemon or not bridge or daemon["uid"] != os.getuid()
                or bridge["uid"] != os.getuid() or daemon["pgid"] != daemon_pid
                or bridge["ppid"] != node_pid or bridge["pgid"] != node_pid
                or _server_pid(home, ipc) != daemon_pid):
            raise ValueError("JCode native daemon or bridge ownership mismatch")
        for identity in (daemon, bridge):
            env = _process_env(identity)
            if (Path(env.get("JCODE_HOME", "")).resolve() != home
                    or Path(env.get("JCODE_RUNTIME_DIR", "")).resolve() != home / "run"):
                raise ValueError("JCode native process belongs to another home")
        cleanup.update(phase="verified", daemon=daemon, bridge=bridge,
                       socket=_socket_identity(ipc / "h/run/jcode.sock"))
    for identity in (node, cleanup.get("daemon")):
        if identity and not registry.register(
                identity["pid"], pgid=identity["pgid"], owner=worker_pid,
                meta={"kind": "jcode-sdk", "native_cleanup": cleanup}):
            raise RuntimeError("JCode native process registration failed")
    return registry._read_entry(registry._entry_path(node_pid))


def _stop_known(identity, signals):
    if not _same(identity, allow_gone=True):
        raise ValueError("JCode process identity changed before cleanup")
    if not _live(identity):
        return
    grouped = identity["pgid"] == identity["pid"]
    for signum in (signal.SIGTERM, signal.SIGKILL):
        if not _same(identity, allow_gone=True):
            raise ValueError("JCode process was reused during cleanup")
        if not _live(identity):
            return
        if grouped:
            os.killpg(identity["pgid"], signum)
        else:
            os.kill(identity["pid"], signum)
        signals.append({"pid": identity["pid"], "signal": signum.name, "group": grouped})
        deadline = time.monotonic() + 2
        while _live(identity) and time.monotonic() < deadline:
            time.sleep(.03)
    if _live(identity):
        raise ValueError("JCode owned process did not exit")


def reap_registered(record):
    result = {"completed": False, "protocol": "jcode-sdk-v1", "signals": []}
    lock = None
    try:
        _require_linux()
        data = record["meta"]["native_cleanup"]
        if data.get("protocol") != "jcode-sdk-v1" or data.get("phase") != "verified":
            raise ValueError("JCode starting daemon identity is not proven")
        home, ipc, binary, work = map(Path, (data["home"], data["ipc_dir"], data["binary"], data["workdir"]))
        hi, ii = _scope(home, ipc)
        if hi != data["home_identity"] or ii != data["ipc_identity"]:
            raise ValueError("JCode native scope identity changed")
        if hashlib.sha256(binary.read_bytes()).hexdigest() != data["binary_sha256"]:
            raise ValueError("JCode native executable changed")
        fd = os.open(home / ".meyo-sdk.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        lock = os.fdopen(fd, "r+")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identities = [data["node"], data["daemon"], data["bridge"]]
        if not all(_same(identity, allow_gone=True) for identity in identities):
            raise ValueError("JCode recorded process identity no longer matches")
        children = [*_descendants(data["node"]["pid"]), *_descendants(data["daemon"]["pid"]),
                    *_descendants(data["bridge"]["pid"])]
        if _live(data["daemon"]):
            if (_server_pid(home, ipc) != data["daemon"]["pid"]
                    or _socket_identity(ipc / "h/run/jcode.sock") != data["socket"]):
                raise ValueError("JCode native socket or server registration changed")
            environment = _process_env(data["daemon"])
            env = {"PATH": str(binary.parent) + os.pathsep + os.defpath,
                   "HOME": str(home), "JCODE_HOME": str(ipc / "h"),
                   "JCODE_RUNTIME_DIR": str(ipc / "h/run"),
                   "JCODE_SOCKET": str(ipc / "h/run/jcode.sock"),
                   "JCODE_NO_TELEMETRY": "1", "NO_PROXY": "*"}
            if environment.get("LD_LIBRARY_PATH"):
                env["LD_LIBRARY_PATH"] = environment["LD_LIBRARY_PATH"]
            from external_agent_registry import _run_probe_command
            ok, output = _run_probe_command(
                [str(binary), "--no-update", "--no-selfdev", "server", "stop", "--force", "--json"],
                work, timeout_s=10, env=env)
            result["signals"].append({"via": "native-server-stop", "pid": data["daemon"]["pid"]})
            if not ok:
                raise ValueError("JCode native server stop failed")
            deadline = time.monotonic() + 3
            while _live(data["daemon"]) and time.monotonic() < deadline:
                time.sleep(.03)
        if _live(data["daemon"]):
            raise ValueError("JCode daemon did not reach terminal state")
        _stop_known(data["node"], result["signals"])
        for identity in [data["bridge"], *children]:
            _stop_known(identity, result["signals"])
        result["completed"] = not any(_live(identity) for identity in identities + children)
        result["observed_descendants"] = len(children)
    except Exception as exc:
        result["error"] = str(exc)[:300]
    finally:
        if lock is not None:
            lock.close()
    return result
