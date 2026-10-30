#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""legion_proc_registry · 军团进程组注册表(崩溃安全的孤儿回收单一事实源)。

根因(2026-09-02 用户钉死根修):常驻 server 被 SIGKILL(137) 时,atexit 和信号处理
都不会执行;而军团 spawn 的 CLI/worker 大多 start_new_session=True 自立进程组——
  · serve_guard 的 terminate_server_tree 只杀 server 自己的 pgid,够不到自立的 CLI;
  · worker 的 _orphan_guard killpg(自己组) 也够不到 CLI 组(CLI 又自立了一层);
  · server 的 _reap_children 在 SIGKILL 下根本不跑。
→ 正在运行的 Harness CLI 变成 PPID=1 孤儿(争 CPU/占端口/继续烧网关),越积越慢。

根修 = 把进程组账本放到进程外的磁盘上:spawn 时写登记文件,任何一层都能凭账本回收:
  · server 启动 sweep   :清扫历史任何一代死掉的 server/worker 留下的孤儿组(守护也死了也能收);
  · server 周期 sweep   :worker 被单独 SIGKILL(rc-9 常见)也在下一周期被收掉;
  · serve_guard sweep   :server 每次退出(含 137)后立即清扫;
  · worker _orphan_guard:父死自杀前先 kill_owned_by(自己) 级联收掉名下 CLI 组;
  · run_one 收尾        :worker 异常死亡(rc<0)当场 kill_owned_by+整组扫尾,不等周期。

正常退出的进程:pid 已死 → sweep 只当陈旧登记清文件,绝不误杀。
PID 复用防护:登记 comm(可执行名);sweep 杀之前核对 ps comm + 当前 pgid,任一不一致
(说明 pid 被新进程复用)只清登记不杀。多实例安全:owner(启动者)还活着的一律不动。
新登记以 PID namespace + PID 分键；旧无 namespace 记录只读保留，不猜来源、不补戳或回收。

速度底线:登记=一次小文件写(几十µs);sweep=O(登记数),ps 批量一发,周期 120s,
无孤儿时零成本。所有操作各自 try 包裹,sweep 永不抛异常(绝不拖垮调用方)。
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
import threading

try:
    import fcntl
except ImportError:
    fcntl = None

_ENTRY_LOCK = threading.RLock()
_DIRECTORY_SCOPE = threading.local()

try:
    import legion_paths as _lp
except Exception:  # 独立 CLI 场景兜底
    _lp = None


def _reg_dir() -> str:
    override = getattr(_DIRECTORY_SCOPE, "directory", None)
    if override is None:
        override = os.environ.get("LEGION_PROC_REGISTRY_DIR")
    if override:
        if not os.path.isabs(override) or os.path.realpath(override) == os.path.sep:
            raise ValueError("invalid native process registry directory")
        if os.path.islink(override):
            raise ValueError("native process registry directory is a symlink")
        d = os.path.realpath(override)
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d
    if _lp is not None:
        d = _lp.under("run", "pgids")
    else:
        d = os.path.join(os.environ.get("MEYO_LEGION_DIR")
                         or os.path.expanduser("~/.meyo/legion"), "run", "pgids")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


@contextmanager
def registry_directory_scope(directory=None):
    """可信调用方绑定本线程的登记目录；嵌套/异常恢复，不修改进程环境。"""
    previous = getattr(_DIRECTORY_SCOPE, "directory", None)
    try:
        if directory is not None:
            directory = os.fspath(directory)
            if not isinstance(directory, str) or not directory:
                raise ValueError("invalid native process registry directory")
            _DIRECTORY_SCOPE.directory = directory
        resolved = _reg_dir()  # 复用原目录校验，并在 spawn 前失败。
        _DIRECTORY_SCOPE.directory = resolved
        yield resolved
    finally:
        if previous is None:
            _DIRECTORY_SCOPE.__dict__.pop("directory", None)
        else:
            _DIRECTORY_SCOPE.directory = previous


def _entry_path(pid: int) -> str:
    namespace = _pid_namespace()
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("native process PID namespace is unavailable")
    # Full digest keeps filenames portable without collapsing namespace names.
    # Legacy PID.json (and its .tmp) cannot obstruct a new, attributable record.
    namespace_key = hashlib.sha256(namespace.encode()).hexdigest()
    return os.path.join(_reg_dir(), f"{int(pid)}.ns-{namespace_key}.json")


def _pid_namespace() -> str:
    """Registrar's kernel PID namespace, never an environment-supplied claim."""
    if not sys.platform.startswith("linux"):
        return sys.platform + ":host"  # No Linux PID namespaces on these hosts.
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return ""  # New unidentifiable registrations fail closed.


def _same_namespace(data: dict) -> bool:
    # Untagged legacy records remain readable, but no local PID operation can
    # be justified by a missing namespace. Never manufacture a migration stamp.
    current = _pid_namespace()
    return bool(current) and data.get("pid_namespace") == current


def _read_entry(path):
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return {}
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {}
            data = json.load(stream)
        return data if isinstance(data, dict) and data.get("pid") else {}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}  # Malformed/unknown provenance is kept, never unlinked here.


@contextmanager
def _entry_lock():
    """Serialize same-number register/unregister across processes/namespaces."""
    with _ENTRY_LOCK:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(os.path.join(_reg_dir(), ".entry.lock"), flags, 0o600)
        with os.fdopen(fd, "r+") as stream:
            if fcntl is not None:
                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(stream, fcntl.LOCK_UN)


def _unlink_local(path, expected=None):
    with _entry_lock():
        data = _read_entry(path)
        if data and _same_namespace(data) and (expected is None or data == expected):
            os.unlink(path)


def is_alive(pid: int) -> bool:
    """pid 是否还活着(EPERM=活着但不是我们的,也算活)。"""
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def process_comm(pid: int, fallback: str = "") -> str:
    """Read the kernel's executable identity immediately after spawn.

    Callers often know only an argv basename (python3.12/jcode), while macOS `ps comm` reports
    Python or an absolute executable path.  Persisting the caller guess makes sweep delete a live
    registration as a false PID-reuse mismatch, leaving no record after a later SIGKILL.
    """
    try:
        result = subprocess.run(["ps", "-o", "comm=", "-p", str(int(pid))],
                                capture_output=True, text=True, timeout=2)
        observed = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
        return observed or str(fallback or "")
    except Exception:
        return str(fallback or "")


def register(pid: int, pgid: int | None = None, owner: int | None = None,
             comm: str | None = None, meta: dict | None = None) -> bool:
    """登记一个(可能自立进程组的)子进程。

    pid   : 子进程 pid(登记键)。
    pgid  : 它的进程组(缺省=pid;start_new_session 的子进程 pgid==pid)。
    owner : 启动者 pid(通常 os.getpid();owner 还活着就绝不动它)。
    comm  : 可执行名(PID 复用防护)。
    """
    try:
        with _entry_lock():
            pid = int(pid)
            namespace = _pid_namespace()
            if pid <= 1 or not namespace:
                return False
            path = _entry_path(pid)
            previous = _read_entry(path)
            if previous is not None and (not previous or not _same_namespace(previous)):
                return False  # Do not probe or overwrite another namespace's same-number PID.
            if pgid is None:
                try:
                    pgid = os.getpgid(pid)
                except Exception:
                    pgid = pid
            requested_comm = str(comm or "")
            start_times = {}
            observed_comm = _comm_of_many([pid], start_times=start_times).get(pid, requested_comm)
            details = dict(meta or {})
            if requested_comm and requested_comm != observed_comm:
                details.setdefault("requested_comm", requested_comm[:200])
            data = {"pid": pid, "pgid": int(pgid), "owner": int(owner or 0),
                    "comm": observed_comm[:500], "ts": time.time(), "meta": details,
                    "pid_namespace": namespace, "start_time": start_times.get(pid)}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)   # 原子写:任何时刻 sweep 读到的都是完整 JSON
        return True
    except Exception:
        return False


def unregister(pid: int) -> None:
    """正常收尾时注销登记(可省;sweep 也会把死 pid 当陈旧清掉)。"""
    try:
        _unlink_local(_entry_path(pid))
        # 31831efe used PID.json with namespace metadata. Remove such a record
        # only if the existing metadata identifies this namespace; unknown stays.
        _unlink_local(os.path.join(_reg_dir(), f"{int(pid)}.json"))
    except Exception:
        pass


def _comm_of_many(pids: list, *, start_times=None) -> dict:
    """复用一次 ps 同时取 comm 和跨 exec 不变的 UTC 启动时间。"""
    out = {}
    if not pids:
        return out
    try:
        r = subprocess.run(["ps", "-o", "pid=,lstart=,comm=", "-p", ",".join(str(p) for p in pids)],
                           capture_output=True, text=True, timeout=5,
                           env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"})
        for ln in (r.stdout or "").splitlines():
            parts = ln.strip().split(None, 6)
            if len(parts) == 7:
                try:
                    pid = int(parts[0])
                    out[pid] = parts[6].strip()
                    if start_times is not None:
                        start_times[pid] = " ".join(parts[1:6])
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _comm_matches(stored: str, actual: str) -> bool:
    """comm 对齐比较(都截前 15 字符,兼容 Linux TASK_COMM_LEN 截断)。"""
    return (stored or "")[:15] == (actual or "")[:15]


def _kill_groups(pgids: list, grace_s: float = 0.4) -> list:
    """SIGTERM 宽限→SIGKILL 整组杀,返回真正发过信号的 pgid。"""
    if os.name == "nt" or not pgids:
        return []
    hit = []
    for g in pgids:
        try:
            os.killpg(int(g), signal.SIGTERM)
            hit.append(int(g))
        except Exception:
            pass
    if hit and grace_s > 0:
        time.sleep(grace_s)
    for g in hit:
        try:
            os.killpg(int(g), signal.SIGKILL)
        except Exception:
            pass
    return hit


def _load_entries() -> list:
    items = []
    try:
        for name in os.listdir(_reg_dir()):
            if not name.endswith(".json"):
                continue
            p = os.path.join(_reg_dir(), name)
            data = _read_entry(p)
            if data:
                items.append((p, data))
    except Exception:
        pass
    return items


# fork→exec 窗口宽限:Popen 返回≠exec 完成(动态链接程序可达百毫秒+),
# 窗口内登记的 comm 是过渡态,立即 sweep 会误删活登记。
_REUSE_GRACE_S = 5.0


def _identity_reused(data, actual_comm, actual_start):
    """True=出生时间已变；False=仍可核验；None=无法确认，不猜杀或删账。"""
    if data.get("start_time"):
        return data["start_time"] != actual_start if actual_start else None
    # 兼容旧登记；仅有 comm 时，长期 mismatch 也可能是合法 exec，而非 PID 复用。
    stored_comm = str(data.get("comm") or "")
    if actual_comm is None or (
            stored_comm and not _comm_matches(stored_comm, actual_comm)
            and time.time() - float(data.get("ts") or 0) > _REUSE_GRACE_S):
        return None
    return False


def _needs_native_cleanup(data: dict) -> bool:
    meta = data.get("meta") or {}
    # Do not treat malformed/old verified daemon records as ordinary CLI groups.
    return "native_cleanup" in meta or (meta.get("kind") == "prime-supervisor" and meta.get("phase") == "verified")


def _cleanup_native(data: dict) -> dict:
    meta = data.get("meta") or {}
    try:
        if (meta.get("native_cleanup") or {}).get("protocol") == "jcode-sdk-v1":
            from external_adapters.jcode_sdk_lifecycle import reap_registered
            return reap_registered(data)
        if (meta.get("native_cleanup") or {}).get("protocol") != "prime-daemon-v1":
            return {"completed": False, "error": "native_cleanup_protocol_unavailable"}
        from external_adapters.prime_daemon import reap_registered
        return reap_registered(data)
    except Exception as exc:
        return {"completed": False, "error": "native_cleanup_failed:" + type(exc).__name__}


def _unlink_unchanged_native(path, expected):
    with _entry_lock():
        current = _read_entry(path)
        if current == expected and _same_namespace(current):
            os.unlink(path)


def sweep(kill: bool = True) -> dict:
    """清扫注册表:死 pid 清登记;owner 死了的活进程=孤儿,整组回收。

    返回 {"kept": N, "removed_stale": N, "removed_reused": N, "killed": [pgid...]}。
    绝不抛异常;owner 活着的一律保留(多实例安全)。
    """
    summary = {"kept": 0, "removed_stale": 0, "removed_reused": 0, "killed": []}
    try:
        entries = _load_entries()
        local = [(p, d) for p, d in entries if _same_namespace(d)]
        summary["skipped_namespace"] = len(entries) - len(local)
        summary["kept"] += summary["skipped_namespace"]
        entries = local
        if not entries:
            return summary
        protected_pgids = {int(data.get("pgid") or data["pid"])
                           for _, data in entries if _needs_native_cleanup(data)}
        # 一次批量 ps 拿所有活 pid 的 comm(少 subprocess 调用·sweep 快)
        live_pids = [int(d["pid"]) for _, d in entries if is_alive(int(d["pid"]))]
        start_times = {}
        comms = _comm_of_many(live_pids, start_times=start_times)
        victims = []   # owner 已死的孤儿(待杀)
        for path, d in entries:
            pid = int(d["pid"])
            if _needs_native_cleanup(d):
                owner = int(d.get("owner") or 0)
                if not kill or (owner and is_alive(owner)):
                    summary["kept"] += 1
                    continue
                result = _cleanup_native(d)
                summary.setdefault("native_cleanup", []).append({"pid": pid, **result})
                if result.get("completed") is True:
                    _unlink_unchanged_native(path, d)
                else:
                    summary["kept"] += 1
                continue  # Protocol refusal must never enter the generic signal batch.
            if not is_alive(pid):
                try:
                    _unlink_local(path)
                except Exception:
                    pass
                summary["removed_stale"] += 1
                continue
            reused = _identity_reused(d, comms.get(pid), start_times.get(pid))
            if reused is None:
                summary["kept"] += 1
                continue
            if reused:
                try:
                    _unlink_local(path, expected=d)
                except Exception:
                    pass
                summary["removed_reused"] += 1
                continue
            owner = int(d.get("owner") or 0)
            if owner and is_alive(owner):
                summary["kept"] += 1
                continue
            victims.append((path, d))
        if kill and victims:
            pgids = []
            for path, d in victims:
                pid, pgid = int(d["pid"]), int(d.get("pgid") or d["pid"])
                if pgid in protected_pgids:
                    continue  # An old duplicate CLI row cannot bypass native refusal.
                # 第二重防护:当前 pgid 必须仍等于登记值(pid 复用后组号会变)
                try:
                    if os.getpgid(pid) != pgid:
                        try:
                            _unlink_local(path)
                        except Exception:
                            pass
                        summary["removed_reused"] += 1
                        continue
                except Exception:
                    continue
                pgids.append(pgid)
            summary["killed"] = _kill_groups(pgids)
            for path, _ in victims:
                try:
                    _unlink_local(path)
                except Exception:
                    pass
        elif victims and not kill:
            summary["kept"] += len(victims)
        return summary
    except Exception:
        return summary


def kill_owned_by(owner_pid: int, *, strict: bool = False) -> list:
    """杀掉指定 owner 名下的全部登记进程组(worker 自杀前的级联回收)。

    不看 owner 死活——调用者就是 owner 自己(或它的父层在收尾),名下登记=它的孩子。
    失败保留登记；strict 供同步收尾方使用，不能将未完成回收当作正常返回。
    """
    try:
        entries = _load_entries()
        protected_pgids = {int(data.get("pgid") or data["pid"]) for _, data in entries
                           if _same_namespace(data) and _needs_native_cleanup(data)}
        owned = [(p, d) for p, d in entries if _same_namespace(d)
                 and int(d.get("owner") or 0) == int(owner_pid)]
        if not owned:
            return []
        ordinary, native_stopped = [], []
        incomplete = False
        for path, data in owned:
            if not _needs_native_cleanup(data):
                ordinary.append((path, data))
                continue
            result = _cleanup_native(data)
            if result.get("completed") is True:
                native_stopped.append(int(data.get("pgid") or data["pid"]))
                _unlink_unchanged_native(path, data)
            else:
                incomplete = True
            # A failed native shutdown retains the original evidence; no bulk kill.
        owned = ordinary
        live_pids = [int(d["pid"]) for _, d in owned if is_alive(int(d["pid"]))]
        start_times = {}
        comms = _comm_of_many(live_pids, start_times=start_times)
        pgids, victims = [], []
        for path, d in owned:
            pid, pgid = int(d["pid"]), int(d.get("pgid") or d["pid"])
            if pgid in protected_pgids:
                if pgid in native_stopped and not is_alive(pid):
                    _unlink_local(path, expected=d)
                else:
                    incomplete = True
                continue
            if not is_alive(pid):
                try:
                    _unlink_local(path, expected=d)
                except Exception:
                    pass
                continue
            reused = _identity_reused(d, comms.get(pid), start_times.get(pid))
            if reused is None:
                incomplete = True
                continue
            if reused:
                try:
                    _unlink_local(path, expected=d)
                except Exception:
                    pass
                continue
            try:
                if os.getpgid(pid) != pgid:
                    incomplete = True
                    continue
            except Exception:
                incomplete = True
                continue
            pgids.append(pgid)
            victims.append((path, d))
        killed = _kill_groups(list(dict.fromkeys(pgids)))
        # 发过 TERM 不等于 KILL 成功，更不等于组已消失。等待真实退出后才删账；
        # EPERM、原生协议拒绝、身份无法核实均保留，供原有 sweep 重试。
        pending = set(pgids)
        deadline = time.monotonic() + 2
        while pending:
            for pgid in tuple(pending):
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    pending.discard(pgid)
                except OSError:
                    pass
            if not pending.intersection(killed) or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        for path, data in victims:
            if int(data.get("pgid") or data["pid"]) not in pending:
                try:
                    _unlink_local(path, expected=data)
                except Exception:
                    pass
        if strict and (incomplete or pending):
            raise RuntimeError("process_group_cleanup_incomplete")
        return native_stopped + killed
    except Exception:
        if strict:
            raise
        return []


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="军团进程组注册表:孤儿清扫")
    ap.add_argument("action", choices=["sweep", "list"], nargs="?", default="sweep")
    ap.add_argument("--dry", action="store_true", help="只看不杀")
    args = ap.parse_args()
    if args.action == "list":
        for _, d in _load_entries():
            print(json.dumps(d, ensure_ascii=False))
        return 0
    s = sweep(kill=not args.dry)
    print(json.dumps(s, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
