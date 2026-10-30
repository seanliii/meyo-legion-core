"""Prime 1eee2938 的请求级前台 supervisor scope。

enter 只锁 IPC scope，caller 可在锁内写配置；start 才启动并核验直接子进程。
正常关闭使用原生 DaemonClient 的同一连接核 hello/send shutdown，Python wait
负责回收直接子进程。不宣称 SIGKILL 恢复或全部 detached worker 已回收。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import hashlib
from uuid import uuid4

import legion_proc_registry as registry
from external_agent_transports import run_transport

START_TIMEOUT = 10.0
STOP_TIMEOUT = 5.0

# 本模块仅执行固定发行物已有 SDK；不导入 cli.js、不创建 session、不发模型请求。
_CONTROL_JS = r"""
import {lstatSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const [dist, endpoint, action, raw] = process.argv.slice(1);
const expected = JSON.parse(raw);
let client;
const fail = code => { const e = new Error(code); e.code = code; throw e; };
const same = (a,b) => a.dev === b.dev && a.ino === b.ino;
const identity = () => {
  const s = lstatSync(endpoint, {bigint:true});
  if (!s.isSocket() || Number(s.uid) !== process.getuid()) fail('identity_mismatch');
  return {dev:String(s.dev), ino:String(s.ino)};
};
try {
  const {DaemonClient} = await import(pathToFileURL(join(dist, 'modes/daemon/daemon-client.js')));
  const {getProcessStartId} = await import(pathToFileURL(join(dist, 'core/session-lease.js')));
  const deadline = Date.now() + expected.timeout_ms;
  let before, hello;
  while (true) {
    try {
      before = identity();
      if (expected.socket && !same(before, expected.socket)) fail('identity_mismatch');
      client = new DaemonClient(endpoint);
      await client.connect(250);
      hello = await client.waitForHello(500);
      break;
    } catch (error) {
      client?.close(); client = undefined;
      if (error.code === 'identity_mismatch') throw error;
      if (Date.now() >= deadline) fail('control_connect_failed');
      await new Promise(resolve => setTimeout(resolve, 50));
    }
  }
  const birth = getProcessStartId(expected.pid);
  const after = identity();
  if (!birth || hello.supervisorPid !== expected.pid ||
      hello.supervisorProcessStartId !== birth || hello.appVersion !== '0.9.4' ||
      hello.supervisorSocketPath !== endpoint ||
      !same(before, after) || (expected.birth && expected.birth !== birth) ||
      (expected.socket && !same(after, expected.socket))) fail('identity_mismatch');
  let accepted = false;
  if (action === 'shutdown') {
    // 不调用顶层 shutdown/runShutdownAll，也不等待尚未被 Python reap 的 zombie。
    const reply = await client.request({type:'shutdown'}, expected.timeout_ms);
    if (reply.success !== true) fail('shutdown_rejected');
    accepted = true;
  } else if (action !== 'hello') fail('invalid_control_action');
  console.log(JSON.stringify({
    type:'prime_daemon_control', status:'ok', text:'ok',
    identity:{pid:expected.pid, birth, socket:after},
    shutdown_accepted:accepted
  }));
} catch (error) {
  console.log(JSON.stringify({
    type:'prime_daemon_control', status:'error', text:'',
    error: error.code === 'identity_mismatch' ? error.code :
      ['control_connect_failed','shutdown_rejected'].includes(error.message) ?
        error.message : 'native_control_failed'
  }));
  process.exitCode = 1;
} finally {
  client?.close();
}
"""


class PrimeDaemonError(RuntimeError):
    pass


def _pid_namespace():
    return os.readlink("/proc/self/ns/pid") if sys.platform.startswith("linux") else sys.platform + ":host"


def _control_request(node, dist, cwd, env, endpoint, action, expected, timeout):
    argv = [node, "--input-type=module", "-e", _CONTROL_JS, str(dist),
            str(endpoint), action, json.dumps(expected)]
    context = {"workdir": cwd, **{f"arg_{i}": value for i, value in enumerate(argv)}}
    spec = {
        "type": "cli", "command": [f"{{arg_{i}}}" for i in range(len(argv))],
        "output_contract": {
            "type": "json", "record_match": {"type": "prime_daemon_control"},
            "text_field": "text", "status_field": "status", "completed_statuses": ["ok"],
        },
    }
    result = run_transport(spec, context, env, timeout + 1)
    event = next((row for row in reversed(result.get("events") or [])
                  if row.get("type") == "prime_daemon_control"), {})
    if result.get("status") != "ok" or event.get("status") != "ok":
        raise PrimeDaemonError(event.get("error") or "prime_daemon_control_transport_failed")
    return event


def _linux_process(pid):
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
        return {"pid": int(pid), "birth": fields[19], "state": fields[0]}
    except FileNotFoundError:
        return None


def _active_identity(identity):
    if sys.platform.startswith("linux"):
        now = _linux_process(identity["pid"])
        return bool(now and now["state"] != "Z" and now["birth"] == identity["birth"])
    result = subprocess.run(["ps", "-o", "state=", "-p", str(identity["pid"])],
                            capture_output=True, text=True, timeout=2)
    return bool(result.stdout.strip() and not result.stdout.strip().startswith("Z"))


def _known_descendants(pid):
    """Only walk the confirmed supervisor tree; no full-machine scan or signals."""
    if not sys.platform.startswith("linux"):
        return []
    found, pending, seen = [], [pid], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if len(seen) > 256:
            raise PrimeDaemonError("prime_cleanup_tree_exceeds_scope")
        identity = _linux_process(current)
        if not identity:
            continue
        if current != pid:
            found.append(identity)
        try:
            for task in (Path("/proc") / str(current) / "task").iterdir():
                pending.extend(int(value) for value in (task / "children").read_text().split())
        except FileNotFoundError:
            pass
    return found


def _descriptor_paths(config, endpoint):
    if not config:
        raise PrimeDaemonError("prime_cleanup_config_missing")
    directory = Path(config) / "daemon-workers" / hashlib.sha256(os.path.realpath(endpoint).encode()).hexdigest()[:12]
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PrimeDaemonError("prime_cleanup_descriptor_directory_unsafe")
    paths = [path for path in directory.iterdir() if path.name.endswith(".json")]
    if len(paths) > 256:
        raise PrimeDaemonError("prime_cleanup_descriptors_exceed_scope")
    return paths


def _reap_registered_locked(record: dict) -> dict:
    """Protocol-first cleanup of one attributable orphan; never signal a guessed PID."""
    try:
        meta = record["meta"]
        cleanup, identity = meta["native_cleanup"], meta["native_identity"]
        pid = record["pid"]
        if (cleanup.get("protocol") != "prime-daemon-v1" or not isinstance(pid, int)
                or pid <= 1 or record.get("pgid") != pid or identity.get("pid") != pid
                or not identity.get("birth") or cleanup.get("pid_namespace") != _pid_namespace()):
            raise PrimeDaemonError("prime_cleanup_identity_unverified")
        if not registry.is_alive(pid):
            # A dead supervisor alone does not establish that detached workers died.
            raise PrimeDaemonError("prime_supervisor_gone_worker_cleanup_unverified")
        if sys.platform.startswith("linux") and os.readlink(f"/proc/{pid}/ns/pid") != cleanup["pid_namespace"]:
            raise PrimeDaemonError("prime_cleanup_namespace_mismatch")
        if os.getpgid(pid) != pid:
            raise PrimeDaemonError("prime_cleanup_group_mismatch")
        source, node, cwd, config = (Path(cleanup[key]) for key in ("source", "node", "workdir", "config_dir"))
        if any(not path.is_absolute() for path in (source, node, cwd, config)):
            raise PrimeDaemonError("prime_cleanup_paths_unverified")
        endpoint = Path(meta["socket_path"])
        for directory in (endpoint.parent, config):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise PrimeDaemonError("prime_cleanup_scope_not_private")
        descendants = _known_descendants(pid)
        # Fixed native SDK validates stored birth + socket dev/ino + same-connection
        # hello again before sending shutdown. No task/model environment is replayed.
        env = {"PATH": str(node.parent) + os.pathsep + os.defpath, "HOME": str(cwd),
               "TMPDIR": str(endpoint.parent), "NO_PROXY": "*", "no_proxy": "*"}
        response = _control_request(str(node), source / "packages/coding-agent/dist", str(cwd),
                                    env, endpoint, "shutdown",
                                    {**identity, "timeout_ms": int(STOP_TIMEOUT * 1000)}, STOP_TIMEOUT)
        if response.get("shutdown_accepted") is not True:
            raise PrimeDaemonError("prime_cleanup_not_accepted")
        root = {"pid": pid, "birth": str(identity["birth"]).removeprefix("proc:")}
        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline:
            if not _active_identity(root) and not any(_active_identity(item) for item in descendants):
                break
            time.sleep(.05)
        if (os.path.lexists(endpoint) or _active_identity(root)
                or any(_active_identity(item) for item in descendants)
                or _descriptor_paths(config, endpoint)):
            raise PrimeDaemonError("prime_cleanup_not_terminal")
        return {"completed": True, "protocol": "prime-daemon-v1",
                "observed_descendants": len(descendants), "signals": []}
    except Exception as exc:
        return {"completed": False, "protocol": "prime-daemon-v1",
                "error": str(exc)[:300], "signals": []}


def reap_registered(record: dict) -> dict:
    """Serialize with the existing native slot lock, never the registry-wide lock."""
    fd = None
    try:
        import fcntl
        endpoint = Path(record["meta"]["socket_path"])
        if not endpoint.is_absolute():
            raise PrimeDaemonError("prime_cleanup_socket_not_absolute")
        parent = endpoint.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) & 0o077):
            raise PrimeDaemonError("prime_cleanup_socket_directory_not_private")
        fd = os.open(str(endpoint) + ".meyo-scope.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PrimeDaemonError("prime_cleanup_lock_not_owned")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _reap_registered_locked(record)
    except Exception as exc:
        return {"completed": False, "protocol": "prime-daemon-v1",
                "error": str(exc)[:300], "signals": []}
    finally:
        if fd is not None:
            os.close(fd)


class OwnedPrimeDaemon:
    def __init__(self, command: list[str], source: str, env: dict, log_dir: Path):
        self.source = Path(source)
        self.env = dict(env)
        self.log_dir = Path(log_dir)
        self._process = None
        self._lock_fd = None
        self._entered = False
        self._finished = False
        self._registered = False
        self._identity = None
        self._clean_start = False
        self.facts = {
            "state": "new", "registered": False, "signals": [],
            "all_workers_reaped": "not_verified", "sigkill_recovery": "not_registered",
        }
        if not self.source.is_absolute() or len(command) < 2:
            raise PrimeDaemonError("prime_daemon_invalid_source")
        self.node, self.cli = command[:2]
        self.dist = self.source / "packages/coding-agent/dist"
        if Path(self.cli) != self.dist / "bundle/cli.js" or not Path(self.cli).is_file():
            raise PrimeDaemonError("prime_daemon_invalid_cli")
        # User prompt and appended system prompt are never copied into the supervisor command.
        options = command[2:command.index("--")] if "--" in command else command[2:]
        values = {}
        for flag in ("--cwd", "--session-dir", "--provider", "--model", "--daemon-socket"):
            if options.count(flag) != 1:
                raise PrimeDaemonError("prime_daemon_missing_or_duplicate_option:" + flag)
            index = options.index(flag)
            if index + 1 >= len(options) or not options[index + 1]:
                raise PrimeDaemonError("prime_daemon_missing_value:" + flag)
            values[flag] = options[index + 1]
        self.cwd = values["--cwd"]
        self.socket = Path(values["--daemon-socket"])
        if any(not Path(values[k]).is_absolute() for k in ("--cwd", "--session-dir", "--daemon-socket")):
            raise PrimeDaemonError("prime_daemon_paths_must_be_absolute")
        self.command = [self.node, self.cli, "--mode", "daemon"]
        for flag, value in values.items():
            self.command.extend((flag, value))
        # Fixed native launch roles must not turn this explicit supervisor into an owned worker.
        for name in list(self.env):
            if name.startswith(("PRIME_AGENT_INTERNAL_OWNED_", "PRIME_AGENT_INTERNAL_DAEMON_WORKER")):
                self.env.pop(name)
        self.env.pop("PRIME_AGENT_INTERNAL_DAEMON_SUPERVISOR_SOCKET", None)
        self.facts["socket_path"] = str(self.socket)

    def __enter__(self):
        if self._entered or self._finished:
            raise PrimeDaemonError("prime_daemon_scope_not_reentrant")
        if os.name != "posix":
            raise PrimeDaemonError("prime_daemon_requires_unix")
        import fcntl
        parent = self.socket.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700):
            raise PrimeDaemonError("prime_daemon_socket_directory_not_private")
        try:
            self._lock_fd = os.open(
                str(self.socket) + ".meyo-scope.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
            )
            lock = os.fstat(self._lock_fd)
            if not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.getuid():
                raise PrimeDaemonError("prime_daemon_unsafe_scope_lock")
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PrimeDaemonError("prime_daemon_scope_locked") from exc
            self._reject_endpoint()
            self._entered = True
            self.facts["state"] = "locked"
            return self
        except BaseException:
            self.facts["state"] = "refused"
            self._release_lock()
            raise

    def _reject_endpoint(self):
        if os.path.lexists(self.socket):
            raise PrimeDaemonError("prime_daemon_existing_endpoint_not_owned")

    def start(self):
        if not self._entered or self._finished:
            raise PrimeDaemonError("prime_daemon_scope_not_entered")
        if self._process is not None:
            raise PrimeDaemonError("prime_daemon_already_started")
        self._reject_endpoint()
        # A new supervisor can recover old descriptors before any new client.
        # Only a genuinely empty scope permits startup-only signal cleanup.
        self._clean_start = not _descriptor_paths(self.env.get("PRIME_AGENT_CODING_AGENT_DIR"), self.socket)
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.log_dir.is_symlink():
            raise PrimeDaemonError("prime_daemon_unsafe_log_directory")
        tag = uuid4().hex
        paths = [self.log_dir / f"prime-supervisor-{tag}.{stream}.log" for stream in ("stdout", "stderr")]
        try:
            with self._new_log(paths[0]) as stdout, self._new_log(paths[1]) as stderr:
                self._process = subprocess.Popen(
                    self.command, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr, start_new_session=True,
                )
                self.facts.update(pid=self._process.pid, logs=[str(p) for p in paths])
                # This PID is our direct child, not an inference from an existing socket.
                # Register before any potentially slow native hello/SDK import.
                self._register("starting")
            observed = self._control("hello")
            if self._process.poll() is not None:
                raise PrimeDaemonError("prime_daemon_exited_during_start")
            self._identity = observed["identity"]
            self.facts["identity"] = dict(self._identity)
            self._register("verified")
            self.facts["state"] = "ready"
            return self
        except BaseException as exc:
            self.facts.update(state="start_failed", start_error=str(exc)[:500])
            try:
                if self._identity is not None:
                    self._shutdown_owned()
                elif self._clean_start:
                    self._stop_direct()
                else:
                    self.facts["cleanup_error"] = "prime_startup_recovery_not_verified"
            except Exception as cleanup:
                self.facts["cleanup_error"] = str(cleanup)[:500]
            self._safe_unregister_exited()
            if isinstance(exc, PrimeDaemonError) or not isinstance(exc, Exception):
                raise
            raise PrimeDaemonError("prime_daemon_start_failed:" + type(exc).__name__) from exc

    def _register(self, phase):
        meta = {"kind": "prime-supervisor", "phase": phase,
                "socket_path": str(self.socket), "native_identity": self._identity}
        if phase == "verified" or not self._clean_start:
            meta["native_cleanup"] = {
                "protocol": "prime-daemon-v1", "source": str(self.source.resolve()),
                "node": str(Path(self.node).resolve()), "workdir": self.cwd,
                "config_dir": self.env.get("PRIME_AGENT_CODING_AGENT_DIR", ""),
                "pid_namespace": _pid_namespace(),
            }
        accepted = registry.register(
            self._process.pid, pgid=self._process.pid, owner=os.getpid(),
            meta=meta,
        )
        if accepted is False:
            raise PrimeDaemonError("prime_daemon_registration_failed")
        self._registered = True
        self.facts.update(registered=True, registration_phase=phase)
        if phase == "verified":
            self.facts["sigkill_recovery"] = "native_shutdown_via_registry"

    @staticmethod
    def _new_log(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        return os.fdopen(fd, "wb")

    def _control(self, action):
        timeout = START_TIMEOUT if action == "hello" else STOP_TIMEOUT
        expected = {**(self._identity or {}), "pid": self._process.pid,
                    "timeout_ms": max(1, int(timeout * 1000))}
        return _control_request(self.node, self.dist, self.cwd, self.env,
                                self.socket, action, expected, timeout)

    def _socket_matches(self):
        if self._identity is None:
            return True  # Startup cleanup can target only our direct, unreaped Popen child.
        try:
            current = self.socket.lstat()
            expected = self._identity["socket"]
            return (stat.S_ISSOCK(current.st_mode) and current.st_uid == os.getuid()
                    and str(current.st_dev) == expected["dev"] and str(current.st_ino) == expected["ino"])
        except OSError:
            return False

    def _stop_direct(self):
        process = self._process
        if process is None or process.poll() is not None:
            return
        # Do not signal a PID obtained from a socket. This is our unreaped direct child.
        if not self._socket_matches() or os.getpgid(process.pid) != process.pid:
            raise PrimeDaemonError("prime_daemon_identity_changed_no_signal")
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if process.poll() is not None:
                break
            os.killpg(process.pid, sig)
            self.facts["signals"].append(sig.name)
            try:
                process.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.poll() is None:
            raise PrimeDaemonError("prime_daemon_direct_child_still_alive")

    def _unregister_exited(self):
        if self._process is None or self._process.poll() is None:
            return
        self.facts["returncode"] = self._process.returncode
        safe = self.facts.get("state") == "closed" or (
            self._identity is None and self._clean_start and self.facts.get("state") == "start_failed")
        if self._registered and safe:
            registry.unregister(self._process.pid)
            self._registered = False
            self.facts["registered"] = False

    def _safe_unregister_exited(self):
        try:
            self._unregister_exited()
        except Exception as exc:
            self.facts["cleanup_error"] = str(exc)[:500]
            if self.facts["state"] != "start_failed":
                self.facts["state"] = "cleanup_failed"

    def _release_lock(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        # Keep the lock inode: unlinking it would let another caller lock a different inode.

    def _shutdown_owned(self):
        descendants = _known_descendants(self._process.pid)
        result = self._control("shutdown")
        self.facts["shutdown_accepted"] = result["shutdown_accepted"]
        self._process.wait(timeout=STOP_TIMEOUT)
        if self._process.returncode != 0:
            raise PrimeDaemonError("prime_daemon_nonzero_shutdown")
        if (os.path.lexists(self.socket) or any(_active_identity(item) for item in descendants)
                or _descriptor_paths(self.env.get("PRIME_AGENT_CODING_AGENT_DIR"), self.socket)):
            raise PrimeDaemonError("prime_daemon_cleanup_not_terminal")
        self.facts["state"] = "closed"

    def __exit__(self, exc_type, exc_value, traceback):
        if self._finished:
            return False
        self._finished = True
        try:
            if self.facts["state"] == "start_failed":
                return False
            if self._process is None:
                self.facts["state"] = "not_started"
            elif self._process.poll() is not None:
                self.facts.update(state="cleanup_unverified", cleanup_error="supervisor_exited_before_native_shutdown")
            else:
                self._shutdown_owned()
        except Exception as cleanup:
            self.facts.update(state="cleanup_failed", cleanup_error=str(cleanup)[:500])
            # A verified supervisor is the worker cleaner, not an ordinary CLI.
            # Keep its record for the same native retry rather than killing it.
        finally:
            self._safe_unregister_exited()
            try:
                self._release_lock()
            except Exception as cleanup:
                self.facts.update(state="cleanup_failed", cleanup_error=str(cleanup)[:500])
            self.facts["socket_present_after"] = os.path.lexists(self.socket)
        # Never suppress/replace the caller's exception, final, or checkpoint.
        return False
