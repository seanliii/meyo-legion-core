"""CPU processes only: registry scopes, detached children and real flock release."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import importlib
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

import pytest


SERVICE = Path(__file__).resolve().parents[1]
THIS = Path(__file__).resolve()


def _wait(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate(), "CPU fixture did not reach the expected state"


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _entries(directory):
    return {p.name: json.loads(p.read_text()) for p in Path(directory).glob("*.json")}


def _group_signal(pid, sig):
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
    except ProcessLookupError:
        pass


class _Case:
    def __init__(self, root, env, registry, media):
        self.root, self.env = root, env
        self.registry, self.media = registry, media
        self.global_registry = Path(os.environ["LEGION_PROC_REGISTRY_DIR"])
        self.observed = {}

    def ready(self):
        return json.loads((self.root / "ready.json").read_text())

    def call(self, mode="return", ready_hook=None, original=None, timeout_s=4):
        wrapper_mode = mode if mode in ("return", "sigkill", "exec-return") else "wait"
        command = [sys.executable, "-u", str(THIS), "_wrapper",
                   str(self.root), wrapper_mode]

        def check():
            if (self.root / "ready.json").exists() and not self.observed:
                data = self.ready()
                self.observed.update(
                    data=data, global_env=os.environ["LEGION_PROC_REGISTRY_DIR"],
                    global_entries=_entries(self.global_registry),
                    child_entries=_entries(self.env["LEGION_PROC_REGISTRY_DIR"]),
                )
                if ready_hook:
                    ready_hook(data)
                (self.root / "release").touch()
            if self.observed and original is not None:
                raise original
            return bool(self.observed) and mode == "cancel"

        return self.media._bounded_media_process(
            command, 1.2 if mode == "timeout" else timeout_s, env=self.env, cwd=self.root,
            cancel_check=check, term_grace_s=0.08,
        )

    def assert_cleaned(self):
        data = self.ready()
        assert data["leaf_pgid"] == data["leaf"] != data["wrapper_pgid"]
        assert not _alive(data["wrapper"]), "wrapper was not reaped"
        assert not _alive(data["leaf"]), "registered independent child is still alive"
        with (self.root / "resource.lock").open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream, fcntl.LOCK_UN)
        entries = _entries(self.env["LEGION_PROC_REGISTRY_DIR"])
        assert not [d for d in entries.values()
                    if d.get("owner") == data["wrapper"]
                    and self.registry._same_namespace(d)]
        assert not [d for d in entries.values() if d["pid"] == data["wrapper"]]


@pytest.fixture(autouse=True)
def cpu(tmp_path, monkeypatch):
    env = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONDONTWRITEBYTECODE": "1",
           "LEGION_EXT_AUTO_UPDATE": "0", "LEGION_GATEWAY_RETRIES": "0"}
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        path = tmp_path / name
        path.mkdir()
        monkeypatch.setenv(name, str(path))
        env[name] = str(path)
    monkeypatch.syspath_prepend(str(SERVICE))
    registry = importlib.import_module("legion_proc_registry")
    media = importlib.import_module("media_tools")
    cases, sentinels = [], []

    def new_case(name="call"):
        root = tmp_path / name
        root.mkdir()
        child_env = dict(env, LEGION_PROC_REGISTRY_DIR=str(root / "registry"))
        case = _Case(root, child_env, registry, media)
        cases.append(case)
        return case

    def sentinel(case):
        root = case.root / "sentinel"
        root.mkdir()
        proc = subprocess.Popen(
            [sys.executable, "-u", str(THIS), "_sentinel", str(root), str(os.getpid())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=case.env,
        )
        sentinels.append(proc)
        _wait(lambda: (root / "leaf-ready.json").exists())
        return proc

    new_case.sentinel = sentinel
    new_case.registry = registry
    new_case.global_dir = Path(env["LEGION_PROC_REGISTRY_DIR"])
    try:
        yield new_case
    finally:
        # The red suite deliberately leaves detached children. Clean only PIDs
        # created by these fixtures, never sweep the machine's process table.
        for case in cases:
            manifest = case.root / "pids.json"
            if manifest.exists():
                for pid in json.loads(manifest.read_text()).values():
                    _group_signal(pid, signal.SIGKILL)
                for pid in json.loads(manifest.read_text()).values():
                    _wait(lambda pid=pid: not _alive(pid))
        for proc in sentinels:
            _group_signal(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)


def test_env_registry_binds_outer_and_inner_without_changing_global(cpu):
    case = cpu()
    assert case.call() == (0, "cpu-result\n")
    data = case.observed["data"]
    local = case.observed["child_entries"].values()
    assert {data["wrapper"], data["leaf"]} <= {d["pid"] for d in local}
    assert not case.observed["global_entries"], "outer registration used global env"
    assert case.observed["global_env"] == str(cpu.global_dir)
    assert os.environ["LEGION_PROC_REGISTRY_DIR"] == str(cpu.global_dir)
    case.assert_cleaned()


@pytest.mark.parametrize("mode", ["return", "sigkill", "timeout", "cancel"])
def test_owned_detached_group_is_reclaimed_and_other_owners_survive(cpu, mode):
    case = cpu()
    sentinel = cpu.sentinel(case)
    before = _entries(case.env["LEGION_PROC_REGISTRY_DIR"])
    foreign = Path(case.env["LEGION_PROC_REGISTRY_DIR"]) / "foreign-namespace.json"

    def ready(data):
        entry = next(d for d in before.values() if d["pid"] == sentinel.pid)
        foreign.write_text(json.dumps(dict(
            entry, owner=data["wrapper"], pid_namespace="__ut_foreign_pid_namespace")))

    if mode == "timeout":
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            case.call(mode, ready_hook=ready)
        assert caught.value.timeout == 1.2
    elif mode == "cancel":
        with pytest.raises(InterruptedError, match="media_call_cancelled"):
            case.call(mode, ready_hook=ready)
    else:
        rc, output = case.call(mode, ready_hook=ready)
        assert rc == (0 if mode == "return" else -signal.SIGKILL)
        assert output == ("cpu-result\n" if mode == "return" else "")
    case.assert_cleaned()
    assert sentinel.poll() is None, "another owner's group was killed"
    after = _entries(case.env["LEGION_PROC_REGISTRY_DIR"])
    assert {name: after[name] for name in before} == before
    assert foreign.exists(), "a foreign namespace's record was removed"


def test_two_real_calls_keep_distinct_thread_registries(cpu):
    first, second = cpu("first"), cpu("second")
    barrier = threading.Barrier(2)

    def run(case):
        return case.call(ready_hook=lambda _: barrier.wait(timeout=3))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, case) for case in (first, second)]
        assert [f.result(timeout=8) for f in futures] == [(0, "cpu-result\n")] * 2
    for case in (first, second):
        data = case.observed["data"]
        assert {d["pid"] for d in case.observed["child_entries"].values()} == {
            data["wrapper"], data["leaf"]}
        assert not case.observed["global_entries"]
        assert case.observed["global_env"] == str(cpu.global_dir)
        case.assert_cleaned()
    assert Path(cpu.registry._reg_dir()) == cpu.global_dir


def test_registry_scope_nests_restores_on_exception_and_does_not_change_env(cpu):
    registry = cpu.registry
    one, two = cpu("one"), cpu("two")
    original = RuntimeError("scope body failure")
    with registry.registry_directory_scope(one.env["LEGION_PROC_REGISTRY_DIR"]) as directory:
        assert Path(directory) == Path(one.env["LEGION_PROC_REGISTRY_DIR"])
        with pytest.raises(RuntimeError) as caught:
            with registry.registry_directory_scope(two.env["LEGION_PROC_REGISTRY_DIR"]):
                assert Path(registry._reg_dir()) == Path(two.env["LEGION_PROC_REGISTRY_DIR"])
                raise original
        assert caught.value is original
        assert Path(registry._reg_dir()) == Path(directory)
        assert os.environ["LEGION_PROC_REGISTRY_DIR"] == str(cpu.global_dir)
    assert Path(registry._reg_dir()) == cpu.global_dir


def test_fresh_real_exec_transition_does_not_discard_a_live_owned_group(cpu):
    case = cpu()
    actual = {}
    assert case.call("exec-return", ready_hook=lambda data: actual.update(
        comm=cpu.registry.process_comm(data["leaf"]))) == (0, "cpu-result\n")
    leaf = case.ready()["leaf"]
    entry = next(d for d in case.observed["child_entries"].values() if d["pid"] == leaf)
    assert not cpu.registry._comm_matches(entry["comm"], actual["comm"])
    case.assert_cleaned()


def test_real_exec_transition_still_cleans_after_the_exec_grace(cpu):
    case = cpu()

    def observe(data):
        time.sleep(cpu.registry._REUSE_GRACE_S + 0.2)
        assert cpu.registry.sweep(kill=False)["removed_reused"] == 0
        assert {d["pid"] for d in _entries(case.env["LEGION_PROC_REGISTRY_DIR"]).values()} == {
            data["wrapper"], data["leaf"]}

    assert case.call("exec-return", timeout_s=9, ready_hook=observe) == (0, "cpu-result\n")
    case.assert_cleaned()


def test_start_time_mismatch_does_not_signal_a_real_unrelated_process(cpu):
    case = cpu()
    case.env["LEGION_PROC_REGISTRY_DIR"] = str(cpu.global_dir)
    sentinel = cpu.sentinel(case)
    name, entry = next(iter(_entries(cpu.global_dir).items()))
    assert entry.get("start_time"), "registry has no exec-stable process identity"
    # Simulate a stale entry referring to a recycled PID, not a fake process.
    path = cpu.global_dir / name
    path.write_text(json.dumps(dict(entry, start_time="Thu Jan 1 00:00:00 1970")))
    assert cpu.registry.kill_owned_by(os.getpid(), strict=True) == []
    assert sentinel.poll() is None
    assert not path.exists()


def test_missing_start_time_observation_keeps_live_registration(cpu, monkeypatch):
    case = cpu()
    case.env["LEGION_PROC_REGISTRY_DIR"] = str(cpu.global_dir)
    sentinel = cpu.sentinel(case)
    before = _entries(cpu.global_dir)
    with monkeypatch.context() as patch:
        patch.setattr(cpu.registry, "_comm_of_many", lambda *_args, **_kwargs: {})
        with pytest.raises(RuntimeError, match="process_group_cleanup_incomplete"):
            cpu.registry.kill_owned_by(os.getpid(), strict=True)
    assert sentinel.poll() is None
    assert _entries(cpu.global_dir) == before


@pytest.mark.parametrize("invalid", ["relative-registry", "/", ""])
def test_invalid_registry_scope_fails_before_spawn_and_restores(cpu, invalid):
    case = cpu()
    case.env["LEGION_PROC_REGISTRY_DIR"] = invalid
    with pytest.raises(ValueError, match="registry directory"):
        case.call()
    assert not (case.root / "pids.json").exists()
    assert Path(cpu.registry._reg_dir()) == cpu.global_dir


@pytest.mark.parametrize("original_error", [False, True])
def test_signal_failure_retains_live_evidence_without_masking_error(cpu, monkeypatch, original_error):
    """Real wrapper/child/lock; only EPERM is injected at the OS signal boundary."""
    case = cpu()
    original = RuntimeError("original callback failure") if original_error else None
    real_killpg = os.killpg
    denied = []

    def deny_own_child(pgid, sig):
        if (case.root / "ready.json").exists() and pgid == case.ready()["leaf"] and sig:
            denied.append(sig)
            raise PermissionError("CPU injected signal denial")
        return real_killpg(pgid, sig)

    with monkeypatch.context() as patch:
        patch.setattr(os, "killpg", deny_own_child)
        with pytest.raises(RuntimeError) as caught:
            case.call(original=original)
    if original is not None:
        assert caught.value is original
    else:
        assert str(caught.value) == "media_process_cleanup_failed"
    assert signal.SIGTERM in denied
    data = case.ready()
    assert _alive(data["leaf"])
    assert any(d["pid"] == data["leaf"] and d["owner"] == data["wrapper"]
               for d in _entries(case.env["LEGION_PROC_REGISTRY_DIR"]).values())


def test_native_cleanup_refusal_keeps_protected_duplicate_records(cpu):
    case = cpu()
    case.env["LEGION_PROC_REGISTRY_DIR"] = str(cpu.global_dir)
    sentinel = cpu.sentinel(case)
    directory = Path(case.env["LEGION_PROC_REGISTRY_DIR"])
    entry = next(d for d in _entries(directory).values() if d["pid"] == sentinel.pid)
    protected = directory / "native-refusal.json"
    protected.write_text(json.dumps(dict(
        entry, meta={"native_cleanup": {"protocol": "__ut_no_cleanup_protocol"}})))
    before = _entries(directory)
    assert cpu.registry.kill_owned_by(os.getpid()) == []
    assert sentinel.poll() is None
    assert _entries(directory) == before


def test_registry_signal_denial_keeps_the_actual_registered_child(cpu, monkeypatch):
    case = cpu()
    case.env["LEGION_PROC_REGISTRY_DIR"] = str(cpu.global_dir)
    sentinel = cpu.sentinel(case)
    before = _entries(cpu.global_dir)
    real_killpg = os.killpg

    def deny(pgid, sig):
        if pgid == sentinel.pid and sig:
            raise PermissionError("CPU injected signal denial")
        return real_killpg(pgid, sig)

    with monkeypatch.context() as patch:
        patch.setattr(os, "killpg", deny)
        assert cpu.registry.kill_owned_by(os.getpid()) == []
    assert sentinel.poll() is None
    assert _entries(cpu.global_dir) == before


def _diagnostic_call(case, source, diagnostics, staging, **kwargs):
    call = case.media._bounded_media_process
    # The legacy red path must actually spawn and lose stderr, not stop at an
    # unexpected-keyword TypeError before exercising the process boundary.
    optional = {"diagnostics": diagnostics} if "diagnostics" in inspect.signature(call).parameters else {}
    return call(
        [sys.executable, "-I", "-S", "-u", "-c", source, str(case.root)],
        env=case.env, cwd=staging, term_grace_s=0.2, **optional, **kwargs,
    )


@pytest.mark.parametrize("returncode, stderr", [
    (0, ""),
    (0, "cpu-warning\n"),
    (9, "cpu-wrapper-crash\n"),
    (11, "x" * 16384 + "cpu-crash-tail\n"),
], ids=["empty", "warning", "failure", "truncated"])
def test_stderr_diagnostics_survive_deleted_staging_without_polluting_stdout(cpu, returncode, stderr):
    case = cpu()
    diagnostics = {"stderr_tail": "stale", "stderr_truncated": True,
                   "stdout_tail": "stale", "stdout_truncated": True, "caller_tag": "preserved"}
    source = f"""
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
(root / "pids.json").write_text(json.dumps({{"wrapper": os.getpid()}}))
stderr = {stderr!r}
Path("failure.log").write_text(stderr)
sys.stderr.write(stderr)
print('{{"cpu": true}}', flush=True)
raise SystemExit({returncode})
"""
    with tempfile.TemporaryDirectory(dir=case.root) as staging:
        rc, output = _diagnostic_call(case, source, diagnostics, staging, timeout_s=3)
        assert (Path(staging) / "failure.log").read_text() == stderr
    assert not Path(staging).exists()
    assert (rc, output) == (returncode, '{"cpu": true}\n')
    assert diagnostics == {
        "stderr_tail": stderr[-8192:], "stderr_truncated": len(stderr) > 8192,
        "stdout_tail": '{"cpu": true}\n', "stdout_truncated": False,
        "caller_tag": "preserved",
    }
    assert not _alive(json.loads((case.root / "pids.json").read_text())["wrapper"])
    assert not _entries(case.env["LEGION_PROC_REGISTRY_DIR"])


@pytest.mark.parametrize("reason", ["timeout", "cancel", "callback_failure", "sigkill"])
def test_stderr_diagnostics_include_cleanup_and_preserve_original_exception(cpu, reason):
    case = cpu()
    diagnostics = {}
    original = RuntimeError("original CPU callback failure")
    signalled = False
    source = """
import json, os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[1])
(root / "pids.json").write_text(json.dumps({"wrapper": os.getpid()}))
def terminate(_signum, _frame):
    print("cpu-term-cleanup", file=sys.stderr, flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)
print("cpu-before-wait", file=sys.stderr, flush=True)
(root / "stderr-ready").touch()
while True:
    time.sleep(0.02)
"""

    def check():
        nonlocal signalled
        ready = (case.root / "stderr-ready").exists()
        if ready and reason == "callback_failure":
            raise original
        if ready and reason == "sigkill" and not signalled:
            os.kill(json.loads((case.root / "pids.json").read_text())["wrapper"], signal.SIGKILL)
            signalled = True
        return ready and reason == "cancel"

    with tempfile.TemporaryDirectory(dir=case.root) as staging:
        if reason == "sigkill":
            assert _diagnostic_call(case, source, diagnostics, staging,
                                    timeout_s=3, cancel_check=check) == (-signal.SIGKILL, "")
        else:
            error_type = {"timeout": subprocess.TimeoutExpired, "cancel": InterruptedError,
                          "callback_failure": RuntimeError}[reason]
            with pytest.raises(error_type) as caught:
                _diagnostic_call(case, source, diagnostics, staging,
                                 timeout_s=0.8 if reason == "timeout" else 3, cancel_check=check)
            if reason == "callback_failure":
                assert caught.value is original
            elif reason == "timeout":
                assert caught.value.timeout == 0.8
    assert not Path(staging).exists()
    expected = "cpu-before-wait\n" + ("" if reason == "sigkill" else "cpu-term-cleanup\n")
    assert diagnostics == {
        "stderr_tail": expected, "stderr_truncated": False,
        "stdout_tail": "", "stdout_truncated": False,
    }
    assert not _alive(json.loads((case.root / "pids.json").read_text())["wrapper"])
    assert not _entries(case.env["LEGION_PROC_REGISTRY_DIR"])


@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_stdout_only_diagnostics_survive_cancellation_or_timeout(cpu, reason):
    case = cpu()
    before = ("x" * 16384 if reason == "timeout" else "") + "cpu-stdout-before-wait\n"
    cleanup = "cpu-stdout-term-cleanup\n"
    diagnostics, before_cleanup = {}, {}
    checks = 0
    source = f"""
import json, os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[1])
(root / "pids.json").write_text(json.dumps({{"wrapper": os.getpid()}}))
def terminate(_signum, _frame):
    print("cpu-stdout-term-cleanup", flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, terminate)
Path("stdout-only.txt").write_text({before!r})
sys.stdout.write({before!r})
sys.stdout.flush()
(root / "stdout-ready").touch()
while True:
    time.sleep(0.02)
"""

    def check():
        nonlocal checks
        checks += 1
        ready = (case.root / "stdout-ready").exists()
        # The first two checks are before spawn/communicate. Observe output
        # after a real polling timeout, without depending on the missing keys.
        if ready and checks >= 3 and not before_cleanup:
            before_cleanup.update(diagnostics)
        return reason == "cancel" and ready and checks >= 3

    error_type = InterruptedError if reason == "cancel" else subprocess.TimeoutExpired
    with tempfile.TemporaryDirectory(dir=case.root) as staging:
        with pytest.raises(error_type) as caught:
            _diagnostic_call(case, source, diagnostics, staging, cancel_check=check,
                             timeout_s=0.8 if reason == "timeout" else 3)
        assert (Path(staging) / "stdout-only.txt").read_text() == before
    if reason == "cancel":
        assert str(caught.value) == "media_call_cancelled"
    else:
        assert caught.value.timeout == 0.8
    assert not Path(staging).exists()
    assert not _alive(json.loads((case.root / "pids.json").read_text())["wrapper"])
    assert not _entries(case.env["LEGION_PROC_REGISTRY_DIR"])
    assert "stdout_tail" in before_cleanup, "TimeoutExpired stdout was not retained"
    assert before_cleanup["stdout_tail"] == before[-8192:]
    assert before_cleanup["stdout_truncated"] is (len(before) > 8192)
    expected = before + cleanup
    assert diagnostics == {
        "stdout_tail": expected[-8192:], "stdout_truncated": len(expected) > 8192,
        "stderr_tail": "", "stderr_truncated": False,
    }


def _fixture_main():
    def no_network(event, _args):
        if event in {"socket.connect", "socket.bind", "socket.getaddrinfo"}:
            raise AssertionError("CPU registry fixtures must not use the network")

    sys.addaudithook(no_network)
    sys.path.insert(0, str(SERVICE))
    registry = importlib.import_module("legion_proc_registry")
    role, root, option = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if role in ("_leaf", "_sentinel"):
        with (root / "resource.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if role == "_sentinel":
                assert registry.register(os.getpid(), owner=int(option),
                                         meta={"kind": "__ut_cpu_sentinel"})
            (root / "leaf-ready.json").write_text(json.dumps(
                {"pid": os.getpid(), "pgid": os.getpgrp()}))
            time.sleep(30)
        return

    command = [sys.executable, "-u", str(THIS), "_leaf", str(root), "unused"]
    if option == "exec-return":
        # Shell waits in a builtin read: register its real comm before allowing
        # exec, without a helper daemon, a fake comm or an unregistered child.
        command = ["/bin/sh", "-c", 'read -r go; exec "$@"', "__ut_exec", *command]
    leaf = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if option == "exec-return" else subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    (root / "pids.json").write_text(json.dumps({"wrapper": os.getpid(), "leaf": leaf.pid}))
    if not registry.register(leaf.pid, owner=os.getpid(), comm=sys.executable,
                             meta={"kind": "__ut_cpu_detached_leaf"}):
        _group_signal(leaf.pid, signal.SIGKILL)
        leaf.wait(timeout=3)
        raise RuntimeError("CPU fixture registration failed")
    if option == "exec-return":
        leaf.stdin.write(b"go\n")
        leaf.stdin.close()
    _wait(lambda: (root / "leaf-ready.json").exists())
    data = {"wrapper": os.getpid(), "wrapper_pgid": os.getpgrp(),
            "leaf": leaf.pid, "leaf_pgid": os.getpgid(leaf.pid)}
    tmp = root / "ready.tmp"
    tmp.write_text(json.dumps(data))
    tmp.replace(root / "ready.json")
    _wait(lambda: (root / "release").exists(), timeout=10)
    if option in ("return", "exec-return"):
        print("cpu-result", flush=True)
    elif option == "sigkill":
        os.kill(os.getpid(), signal.SIGKILL)
    else:
        time.sleep(30)


if __name__ == "__main__":
    _fixture_main()
