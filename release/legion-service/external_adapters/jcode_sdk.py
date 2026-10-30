"""Connect the existing JCode SDK to a task; all decisions remain native JCode."""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from uuid import uuid4

SDK_VERSION = "1.1.0"


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise ValueError("JCode task directory is not private")
    return path


def _native_home(ctx, checkpoint: dict) -> Path:
    if checkpoint.get("kind") not in (None, "jcode_session"):
        raise ValueError("JCode checkpoint belongs to a different runtime")
    root = Path(ctx.memory_dir or str(Path(ctx.workdir) / ".sessions")).expanduser()
    if not root.is_absolute():
        raise ValueError("JCode memory root must be absolute")
    root = root.resolve()
    identity = hashlib.sha256(
        json.dumps([ctx.user_id, ctx.session_id], ensure_ascii=False).encode()).hexdigest()[:24]
    home = root / "jcode" / identity
    if checkpoint:
        recorded = checkpoint.get("jcode_home")
        if not isinstance(recorded, str) or not Path(recorded).is_absolute():
            raise ValueError("JCode checkpoint has no native home")
        previous = Path(recorded)
        if (previous.is_symlink() or not previous.resolve().is_relative_to(root)
                or (checkpoint.get("runtime_transport") == "sdk" and previous.resolve() != home)):
            raise ValueError("JCode checkpoint home differs from the current task")
        home = previous.resolve()
        sid = checkpoint.get("session_id")
        if not isinstance(sid, str) or not sid or Path(sid).name != sid or sid in (".", ".."):
            raise ValueError("JCode checkpoint has no safe session ID")
        path = home / "sessions" / (sid + ".json")
        if path.is_symlink() or not path.resolve().is_relative_to(home):
            raise ValueError("JCode native checkpoint file is unsafe")
        stored = json.loads(path.read_text())
        if stored.get("id") != sid or (stored.get("working_dir") and
                Path(stored["working_dir"]).resolve() != Path(ctx.workdir).resolve()):
            raise ValueError("JCode native session or project binding differs from the task")
    return _private_dir(home)


def _runtime() -> tuple[Path, Path]:
    import external_agent_registry as registry
    row = next((x for x in registry.load_catalog() if x.get("key") == "jcode-runtime"), None)
    if row is None:
        raise ValueError("JCode managed native runtime is not registered")
    runtime = Path(registry._context(row)["runtime_dir"]).resolve()
    binary = runtime / "bin/jcode"
    sdk = runtime / "sdk/node_modules/@1jehuang/jcode-sdk"
    metadata = json.loads((sdk / "package.json").read_text())
    if (metadata.get("name") != "@1jehuang/jcode-sdk"
            or metadata.get("version") != SDK_VERSION
            or not all((sdk / "dist" / name).is_file() for name in ("client.js", "launch.js"))
            or not binary.is_file() or not os.access(binary, os.X_OK)):
        raise ValueError("JCode fixed native SDK dependency is incomplete")
    return binary, sdk


def _safe(value, env: dict, key: str = ""):
    secrets = [str(v) for k, v in env.items()
               if len(str(v)) >= 8 and any(s in k.upper() for s in ("API_KEY", "AUTH_TOKEN", "PASSWORD"))]
    secrets += [pair.split("=", 1)[1] for pair in env.get("MEYO_APPID_ALIASES", "").split(",")
                if "=" in pair]
    if key:
        secrets.append(key)
    def clean(item):
        if isinstance(item, str):
            for secret in secrets:
                if secret:
                    item = item.replace(secret, "[REDACTED]")
            return item
        if isinstance(item, list):
            return [clean(x) for x in item]
        if isinstance(item, dict):
            return {k: clean(v) for k, v in item.items()}
        return item
    return clean(value)


def _records(raw) -> list[dict]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    rows = []
    for line in str(raw or "").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("type") in {
                "jcode_sdk_result", "jcode_sdk_snapshot"}:
            rows.append(value)
    return rows


def _result(rows, ctx, home, checkpoint, binding, *, failed=False, timed_out=False):
    latest = rows[-1] if rows else {}
    out = {"status": "timeout" if timed_out else "harness_runtime", "final": "",
           "model_used": None, "requested_model": ctx.model, "resumed_native": False,
           "native_checkpoint": checkpoint or None, "usage": None,
           "error": "JCode SDK did not report a complete native result"}
    sid = latest.get("session_id")
    if isinstance(sid, str) and sid and Path(sid).name == sid and sid not in (".", ".."):
        path = home / "sessions" / (sid + ".json")
        old = checkpoint.get("session_id")
        if old and old != sid:
            out["error"] = "JCode SDK changed the requested native session"
            return out
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(home):
            data = json.loads(path.read_text())
            if data.get("id") == sid:
                out["native_checkpoint"] = {
                    "kind": "jcode_session", "session_id": sid, "jcode_home": str(home),
                    "session_path": str(path), "runtime_transport": "sdk",
                    "ccr_binding": dict(binding), "sdk_version": SDK_VERSION,
                }
                out["resumed_native"] = bool(old)
    native_model = latest.get("model")
    out["model_used"] = (ctx.model if native_model == binding["wire_model"]
                         else native_model if isinstance(native_model, str) and native_model else None)
    out["native_facts"] = {
        "transport": "official-jcode-sdk", "sdk_version": SDK_VERSION,
        "wire_model": native_model, "model_binding_matches": latest.get("model_binding_matches"),
        "session_binding_matches": latest.get("session_binding_matches"),
        "cleanup": latest.get("cleanup"), "tool_results": latest.get("tool_results", []),
    }
    if timed_out:
        return out
    if latest.get("type") == "jcode_sdk_result":
        out["final"] = latest.get("text") if isinstance(latest.get("text"), str) else ""
        usage = latest.get("usage")
        if isinstance(usage, dict):
            out["usage"] = {"input_tokens": usage.get("input"), "output_tokens": usage.get("output"),
                            "cache_read_tokens": usage.get("cacheReadInput"),
                            "source": "jcode_native_sdk", "granularity": "turn"}
        if (not failed and latest.get("status") == "ok"
                and out["native_checkpoint"] and out["native_checkpoint"].get("session_id") == sid
                and latest.get("model_binding_matches") is True
                and latest.get("session_binding_matches") is True
                and (latest.get("cleanup") or {}).get("completed") is True):
            out["status"], out["error"] = "ok", ""
        else:
            out["error"] = str(latest.get("error") or "JCode SDK runtime binding or cleanup is incomplete")
            if latest.get("status") == "cancelled":
                out["status"] = "cancelled"
                out["fail_kind"] = "user_cancel"
    return out


def run_harness(ctx, prompt: str, task_runner, *, environment=None, timeout=0) -> dict:
    checkpoint = (ctx.task_memory or {}).get("native_checkpoint") or {}
    env = dict(os.environ if environment is None else environment)
    home = None
    started = time.monotonic()
    try:
        binary, sdk = _runtime()
        home = _native_home(ctx, checkpoint)
        scoped = copy.copy(ctx)
        scoped.memory_dir = str(home)
        from external_adapters import jcode_native, jcode_ccr
        # The current user/task root remains authoritative even for a legacy native ID.
        native_env, _, sid = jcode_native.prepare(scoped, checkpoint)
        native_env.update(env)
        env, arguments, binding = jcode_ccr.configure(ctx, native_env, home, checkpoint)
        requests = _private_dir(home / ".meyo-requests")
        request = {
            "home": str(home), "binary": str(binary), "sdk": str(sdk),
            "workdir": str(Path(ctx.workdir).resolve()), "task": prompt,
            "native_session_id": sid or None, "model_argument": arguments[-1],
            "wire_model": binding["wire_model"], "requested_model": ctx.model,
            "auto_approve": env.get("LEGION_JCODE_ALLOW_TOOLS", "1") == "1",
        }
        path = requests / (uuid4().hex + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(request, stream, ensure_ascii=False)
        command = [sys.executable, "-B", "-s", str(Path(__file__).resolve()), "--worker", str(path)]
        import external_agent_registry as registry
        from native_process_sandbox import prepare_native_process
        # The native SDK launches its own daemon and tool descendants. Put the
        # whole SDK worker inside the existing kernel write boundary, not only
        # its foreground CLI. Native private IPC is the sole extra /tmp root.
        ipc = registry._session_socket_dir(ctx.workdir, "jcode-sdk", str(home))
        fixed_addition = sdk.parents[3].parent
        protected = [str(fixed_addition), str(Path(__file__).resolve().parents[1])]
        if env.get("MEYO_RUNTIME_ROOT"):
            protected.append(env["MEYO_RUNTIME_ROOT"])
        try:
            with prepare_native_process(
                    env, ctx.workdir, backend="codex", protected_roots=protected,
                    writable_state_roots=[str(home), ipc]) as launch:
                process = task_runner(launch.wrap(command), timeout, env=launch.env,
                                      cwd=ctx.workdir, tag="jcode")
                records = _records(process.stdout)
                result = _result(records, ctx, home, checkpoint, binding,
                                 failed=process.returncode != 0)
                if process.returncode != 0 and not records and process.stderr:
                    result["error"] = str(process.stderr)[-2000:]
        except subprocess.TimeoutExpired as exc:
            result = _result(_records(getattr(exc, "legion_stdout", None) or exc.output),
                             ctx, home, checkpoint, binding, timed_out=True)
            # Only explicit runner facts classify the timeout; native evidence stays intact.
            from harness_registry import _runtime_timeout
            result.update(_runtime_timeout(exc))
        finally:
            _reap_dead_workers(home)
        result["elapsed"] = round(time.monotonic() - started, 3)
        return _safe(result, env, ctx.appid)
    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
        return _safe({"status": "harness_runtime", "final": "", "error": str(exc),
                      "model_used": None, "requested_model": ctx.model,
                      "native_checkpoint": checkpoint or None, "resumed_native": False,
                      "elapsed": round(time.monotonic() - started, 3)}, env, ctx.appid)


def _reap_dead_workers(home):
    import legion_proc_registry as registry
    for path, record in registry._load_entries():
        cleanup = (record.get("meta") or {}).get("native_cleanup") or {}
        if (cleanup.get("protocol") != "jcode-sdk-v1" or cleanup.get("home") != str(home)
                or registry.is_alive(record.get("owner", 0))):
            continue
        outcome = registry._cleanup_native(record)
        if outcome.get("completed") is True:
                registry._unlink_unchanged_native(path, record)


def _registration_ack(record):
    """Only the Python identity verifier authorizes native SDK shutdown IDs."""
    cleanup = (record.get("meta") or {}).get("native_cleanup") or {}
    if cleanup.get("phase") != "verified" or not all(
            isinstance(cleanup.get(key), dict) for key in ("bridge", "daemon")):
        raise ValueError("JCode runtime registration did not establish process identities")
    return {"type": "registered", "identities": [cleanup["bridge"], cleanup["daemon"]]}


def _socket_unused(path: Path):
    if not path.exists():
        return
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise ValueError("Unexpected native socket object")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(.3)
        try:
            client.connect(str(path))
        except OSError as exc:
            import errno
            if exc.errno in (errno.ECONNREFUSED, errno.ENOENT):
                return
            raise
    raise ValueError("JCode native home already has a live instance")


def worker_main(request_path: Path) -> int:
    sys.dont_write_bytecode = True
    import external_agent_registry as registry
    import legion_proc_registry as processes
    from external_adapters.jcode_sdk_lifecycle import register_runtime

    request_fd = os.open(request_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(request_fd, "r") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 8_000_000):
            raise ValueError("JCode SDK request file is not a private regular file")
        request = json.load(stream)
    home = Path(request["home"])
    _private_dir(home)
    _reap_dead_workers(home)
    lock_fd = os.open(home / ".meyo-sdk.lock",
                      os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    lock_info = os.fstat(lock_fd)
    if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.geteuid()
            or lock_info.st_nlink != 1 or stat.S_IMODE(lock_info.st_mode) & 0o077):
        os.close(lock_fd)
        raise ValueError("JCode SDK lock is not a private regular file")
    child = None
    owned = []
    final = {"type": "jcode_sdk_result", "status": "harness_runtime", "text": "",
             "error": "native SDK did not finish", "cleanup": {"completed": False}}
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ipc = Path(registry._session_socket_dir(request["workdir"], "jcode-sdk", str(home)))
        alias = ipc / "h"
        if alias.is_symlink():
            if alias.resolve() != home.resolve():
                raise ValueError("JCode IPC alias belongs to another native home")
        elif alias.exists():
            raise ValueError("JCode IPC alias is not managed")
        else:
            alias.symlink_to(home, target_is_directory=True)
        for name in ("jcode.sock", "jcode-api.sock"):
            _socket_unused(alias / "run" / name)
        env = dict(os.environ, MEYO_JCODE_NATIVE=request["binary"],
                   JCODE_HOME=str(alias), JCODE_NO_TELEMETRY="1",
                   MEYO_JCODE_IPC=str(ipc), MEYO_JCODE_HOME=str(home))
        script = Path(__file__).with_name("jcode_sdk_client.mjs")
        node = registry._context({"key": "jcode-runtime"})["node"]
        child = subprocess.Popen([node, str(script), str(request_path)], cwd=request["workdir"],
                                 env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        owned.append(child.pid)
        register_runtime(os.getpid(), child.pid, str(home), str(ipc),
                         request["binary"], request["workdir"])
        messages = queue.Queue()
        stderr = []
        def read_stdout():
            for line in child.stdout:
                messages.put(line)
            messages.put(None)
        def read_stderr():
            for line in child.stderr:
                stderr.append(line)
                if len(stderr) > 40:
                    del stderr[0]
        readers = [threading.Thread(target=read_stdout, daemon=True),
                   threading.Thread(target=read_stderr, daemon=True)]
        for thread in readers:
            thread.start()
        interrupted = False
        def interrupt(signum, frame):
            nonlocal interrupted
            interrupted = True
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)
        while True:
            try:
                line = messages.get(timeout=.2)
            except queue.Empty:
                if child.poll() is not None:
                    break
                continue
            if line is None:
                break
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if value.get("type") == "jcode_runtime_ready":
                registered = register_runtime(
                    os.getpid(), child.pid, str(home), str(ipc),
                    request["binary"], request["workdir"],
                    daemon_pid=value["daemon_pid"], bridge_pid=value["bridge_pid"])
                owned.append(value["daemon_pid"])
                child.stdin.write(json.dumps(_registration_ack(registered)) + "\n")
                child.stdin.flush()
                child.stdin.close()
            elif value.get("type") == "jcode_sdk_result":
                final = value
            elif value.get("type") in {"jcode_sdk_snapshot", "tool_done", "runtime_activity", "text_delta"}:
                print(json.dumps(_safe(value, env), ensure_ascii=False), flush=True)
        child.wait(timeout=10)
        for thread in readers:
            thread.join(timeout=1)
        if child.returncode and not final.get("error"):
            final["error"] = "JCode SDK child exited without successful completion"
        if final.get("status") != "ok" and stderr:
            final["runtime_error_tail"] = _safe("".join(stderr)[-1800:], env)
        if interrupted:
            final["status"] = "cancelled"
    except Exception as exc:
        final.update(status="harness_runtime", error=str(exc))
    finally:
        # Official SDK normally stops its instance. Unexpected survivors remain
        # registered for identity-checked native cleanup, never a blind pkill.
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=25)
            except subprocess.TimeoutExpired:
                final["cleanup_error"] = "SDK owner did not finish graceful cleanup"
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        from external_adapters.jcode_sdk_lifecycle import _live, _same
        for path, record in processes._load_entries():
            if record.get("owner") == os.getpid():
                cleanup = (record.get("meta") or {}).get("native_cleanup") or {}
                identities = [cleanup.get(key) for key in ("node", "daemon", "bridge")]
                verified = cleanup.get("phase") == "verified" and all(identities)
                if verified and all(_same(row, allow_gone=True) and not _live(row) for row in identities):
                    processes._unlink_unchanged_native(path, record)
                else:
                    outcome = processes._cleanup_native(record)
                    if outcome.get("completed") is True:
                        processes._unlink_unchanged_native(path, record)
        remains = [record["pid"] for _, record in processes._load_entries()
                   if record.get("owner") == os.getpid()]
        child_gone = child is None or child.poll() is not None
        final["cleanup"] = {"completed": not remains and child_gone,
                            "remaining_registered_pids": remains,
                            "scope": "registered SDK owner, daemon and bridge"}
        safe = _safe(final, os.environ)
        request_path.with_suffix(".result.json").write_text(json.dumps(safe, ensure_ascii=False, indent=2))
        print(json.dumps(safe, ensure_ascii=False), flush=True)
    return 0 if final.get("status") == "ok" and final["cleanup"]["completed"] else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("Use the existing JCode adapter")
    raise SystemExit(worker_main(Path(sys.argv[2])))
