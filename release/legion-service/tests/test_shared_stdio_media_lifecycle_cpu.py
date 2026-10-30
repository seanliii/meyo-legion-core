"""Real stdio/process CPU tests; fixture capabilities never run a model or GPU."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest


SERVICE = Path(__file__).resolve().parents[1]
SERVER = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator/meyo_mcp_servers/orchestrator_server.py"
THIS = Path(__file__).resolve()


def _registry():
    if str(SERVICE) not in sys.path:
        sys.path.insert(0, str(SERVICE))
    return importlib.import_module("legion_proc_registry")


def _wait_file(path, timeout=3):
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"CPU fixture did not write {path}"
    return path


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _signal_owned_group(pid, sig):
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
    except ProcessLookupError:
        pass


class _CPUCase:
    def __init__(self, root, env):
        self.root, self.env = root, env
        self.processes, self.logs = [], []
        self.registry = _registry()
        self.media = importlib.import_module("media_tools")

    def spawn(self, mode):
        log = (self.root / f"{mode}-{len(self.logs)}.stderr").open("w+")
        self.logs.append(log)
        proc = subprocess.Popen(
            [sys.executable, "-u", str(THIS), mode, str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True,
            start_new_session=True, env=self.env,
        )
        self.processes.append(proc)
        assert self.registry.register(
            proc.pid, owner=os.getpid(), comm=sys.executable,
            meta={"kind": "__ut_cpu_stdio_launcher"},
        )
        return proc

    def command(self, mode="wrapper"):
        return [sys.executable, "-u", str(THIS), mode, str(self.root)]

    def ready(self):
        return json.loads(_wait_file(self.root / "ready.json").read_text())

    def assert_cleaned(self):
        pids = self.ready()
        assert (self.root / "wrapper-cleaned").exists(), "wrapper never received its TERM grace"
        assert (self.root / "leaf-cleaned").exists(), "independent CPU child was not cleaned"
        assert not _alive(pids["wrapper"]), "wrapper was not reaped before returning"
        assert not _alive(pids["leaf"]), "independent child was not reaped before returning"

    def cleanup(self):
        # Red tests must not leave the deliberately separated CPU process groups.
        manifest = self.root / "pids.json"
        if manifest.exists():
            pids = json.loads(manifest.read_text())
            _signal_owned_group(pids["wrapper"], signal.SIGTERM)
            deadline = time.monotonic() + 2
            while (_alive(pids["leaf"]) and time.monotonic() < deadline
                   and not (self.root / "wrapper-cleaned").exists()):
                time.sleep(0.01)
            for pid in pids.values():
                _signal_owned_group(pid, signal.SIGKILL)
                self.registry.unregister(pid)
        for proc in self.processes:
            if proc.poll() is None:
                _signal_owned_group(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    _signal_owned_group(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
            self.registry.unregister(proc.pid)
            for stream in (proc.stdin, proc.stdout):
                if stream and not stream.closed:
                    stream.close()
        for log in self.logs:
            log.close()


@pytest.fixture(autouse=True)
def cpu(tmp_path, monkeypatch):
    env = {"PATH": os.environ.get("PATH", os.defpath), "PYTHONDONTWRITEBYTECODE": "1",
           "LEGION_EXT_AUTO_UPDATE": "0", "LEGION_GATEWAY_RETRIES": "0"}
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        directory = tmp_path / name
        directory.mkdir()
        env[name] = str(directory)
        monkeypatch.setenv(name, str(directory))
    monkeypatch.syspath_prepend(str(SERVICE))
    case = _CPUCase(tmp_path, env)
    try:
        yield case
    finally:
        case.cleanup()


class _RPC:
    def __init__(self, cpu, mode="stdio-shared"):
        self.cpu, self.proc = cpu, cpu.spawn(mode)
        self.buffer, self.replies = b"", {}
        self.send({"id": "init", "method": "initialize", "params": {}})
        self.initialized = self.receive("init")

    def send(self, request):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **request}) + "\n")
        self.proc.stdin.flush()

    def call(self, request_id, name="cpu_block", arguments=None):
        self.send({"id": request_id, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments or {}}})

    def cancel(self, request_id):
        self.send({"method": "notifications/cancelled",
                   "params": {"requestId": request_id, "reason": "CPU test cancellation"}})

    def receive(self, request_id, timeout=2):
        deadline = time.monotonic() + timeout
        while request_id not in self.replies:
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                reply = json.loads(line)
                assert reply.get("id") not in self.replies, "duplicate terminal result"
                self.replies[reply["id"]] = reply
                continue
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"stdio blocked waiting for request {request_id!r}"
            readable, _, _ = select.select([self.proc.stdout], [], [], remaining)
            assert readable, f"stdio blocked waiting for request {request_id!r}"
            data = os.read(self.proc.stdout.fileno(), 65536)
            assert data, f"stdio closed before replying to {request_id!r}, rc={self.proc.poll()}"
            self.buffer += data
        return self.replies.pop(request_id)

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=5) == 0
        assert not list(Path(self.cpu.env["LEGION_PROC_REGISTRY_DIR"]).glob("*.ns-*.json"))


def test_media_existing_input_returncode_and_registration(cpu, monkeypatch):
    observed = []
    register = cpu.registry.register

    def record(pid, **kwargs):
        result = register(pid, **kwargs)
        observed.append((pid, kwargs, result))
        return result

    monkeypatch.setattr(cpu.registry, "register", record)
    result = cpu.media._bounded_media_process(
        [sys.executable, "-c", "import sys; print(sys.stdin.read()); sys.exit(7)"],
        3, "原来的同步输入", env=cpu.env,
    )
    assert result == (7, "原来的同步输入\n")
    assert len(observed) == 1 and observed[0][2]
    assert observed[0][1]["meta"]["kind"] == "media_operation"
    assert not _alive(observed[0][0])
    assert not list(Path(cpu.env["LEGION_PROC_REGISTRY_DIR"]).glob("*.ns-*.json"))


def test_media_optional_cwd(cpu):
    work = cpu.root / "workspace"
    work.mkdir()
    rc, output = cpu.media._bounded_media_process(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        3, cwd=work, env=cpu.env,
    )
    assert rc == 0 and Path(output.strip()) == work


def test_media_no_input_does_not_consume_stdio(cpu):
    proc = cpu.spawn("stdin-probe")
    output, _ = proc.communicate(input="reserved MCP request\n", timeout=5)
    assert proc.returncode == 0
    data = json.loads(output)
    assert data == {"child_stdin": "", "parent_stdin": "reserved MCP request\n"}


@pytest.mark.parametrize("reason", ["timeout", "cancel", "callback_failure"])
def test_media_original_exception_waits_for_wrapper_cleanup(cpu, reason):
    original = RuntimeError("original cancel-check failure")

    def cancel_check():
        ready = (cpu.root / "ready.json").exists()
        if ready and reason == "callback_failure":
            raise original
        return ready and reason == "cancel"

    error = {"timeout": subprocess.TimeoutExpired,
             "cancel": InterruptedError, "callback_failure": RuntimeError}[reason]
    with pytest.raises(error) as caught:
        cpu.media._bounded_media_process(
            cpu.command(), 0.8 if reason == "timeout" else 10,
            cancel_check=cancel_check, env=cpu.env,
        )
    if reason == "callback_failure":
        assert caught.value is original
    elif reason == "cancel":
        assert str(caught.value) == "media_call_cancelled"
    cpu.assert_cleaned()
    assert not list(Path(cpu.env["LEGION_PROC_REGISTRY_DIR"]).glob("*.ns-*.json"))


def test_media_term_grace_escalates_and_reaps(cpu):
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        cpu.media._bounded_media_process(
            cpu.command("stubborn"), 0.4, term_grace_s=0.15, env=cpu.env,
        )
    pid = int((cpu.root / "stubborn-pid").read_text())
    assert not _alive(pid)
    assert time.monotonic() - started < 3
    assert not list(Path(cpu.env["LEGION_PROC_REGISTRY_DIR"]).glob("*.ns-*.json"))


def test_media_cleanup_errors_do_not_replace_original_exception(cpu, monkeypatch):
    original = RuntimeError("original operation error")
    unregister = cpu.registry.unregister

    def check():
        if (cpu.root / "ready.json").exists():
            raise original
        return False

    def broken_unregister(pid):
        unregister(pid)
        raise OSError("cleanup registry error")

    with monkeypatch.context() as patch:
        patch.setattr(cpu.registry, "unregister", broken_unregister)
        with pytest.raises(RuntimeError) as caught:
            cpu.media._bounded_media_process(cpu.command(), 5, cancel_check=check, env=cpu.env)
        assert caught.value is original
    cpu.assert_cleaned()


def test_shared_stdio_sync_call_returns_only_terminal_result(cpu):
    rpc = _RPC(cpu)
    assert rpc.initialized["result"]["serverInfo"]["name"] == "meyo-capabilities"
    rpc.call("echo", "cpu_echo", {"message": "同步结果"})
    result = rpc.receive("echo")["result"]
    assert json.loads(result["content"][0]["text"]) == {
        "ok": True, "message": "同步结果", "scoped": True, "cancelled": False,
    }
    rpc.close()


def test_shared_stdio_matching_cancel_waits_for_real_cleanup_and_reuses_slot(cpu):
    rpc = _RPC(cpu)
    rpc.call("work")
    cpu.ready()
    rpc.cancel("work")
    result = rpc.receive("work", timeout=4)["result"]
    assert result["isError"] and "media_call_cancelled" in result["content"][0]["text"]
    cpu.assert_cleaned()
    rpc.call("next", "cpu_echo", {"message": "next"})
    result = json.loads(rpc.receive("next")["result"]["content"][0]["text"])
    assert result["scoped"] is True and result["cancelled"] is False
    rpc.close()


@pytest.mark.parametrize("request_id, wrong_id", [("work", "other"), (7, "7")])
def test_shared_stdio_wrong_cancel_id_list_and_busy_are_responsive(cpu, request_id, wrong_id):
    rpc = _RPC(cpu)
    rpc.call(request_id)
    cpu.ready()
    rpc.cancel(wrong_id)
    rpc.send({"id": "list", "method": "tools/list"})
    assert {tool["name"] for tool in rpc.receive("list")["result"]["tools"]} == {
        "cpu_block", "cpu_echo",
    }
    assert not (cpu.root / "wrapper-cleaned").exists(), "wrong requestId cancelled the call"
    rpc.call("overlap", "cpu_echo")
    busy = rpc.receive("overlap")["result"]
    assert busy["isError"] and "busy" in busy["content"][0]["text"].lower()
    assert not (cpu.root / "echo-called").exists(), "single-flight executed the overlap"
    rpc.cancel(request_id)
    assert rpc.receive(request_id, timeout=4)["result"]["isError"]
    cpu.assert_cleaned()
    rpc.close()


@pytest.mark.parametrize("shutdown", ["eof", "term"])
def test_shared_stdio_shutdown_waits_for_cleanup_before_unregister(cpu, shutdown):
    rpc = _RPC(cpu)
    rpc.call("work")
    cpu.ready()
    if shutdown == "eof":
        rpc.proc.stdin.close()
    else:
        os.kill(rpc.proc.pid, signal.SIGTERM)
    assert rpc.proc.wait(timeout=5) == 0
    result = rpc.receive("work")["result"]
    assert result["isError"] and "media_call_cancelled" in result["content"][0]["text"]
    cpu.assert_cleaned()
    assert json.loads((cpu.root / "host-unregistered.json").read_text()) == {
        "wrapper_cleaned": True, "leaf_cleaned": True,
    }
    assert not list(Path(cpu.env["LEGION_PROC_REGISTRY_DIR"]).glob("*.ns-*.json"))


def test_shared_stdio_tool_timeout_is_terminal_and_keeps_server_usable(cpu):
    rpc = _RPC(cpu)
    rpc.call("timeout", arguments={"timeout_s": 0.8})
    result = rpc.receive("timeout", timeout=4)["result"]
    assert result["isError"] and "TimeoutExpired" in result["content"][0]["text"]
    cpu.assert_cleaned()
    rpc.send({"id": "list", "method": "tools/list"})
    assert rpc.receive("list")["result"]["tools"]
    rpc.close()


def test_ordinary_orchestrator_keeps_original_sync_protocol(cpu):
    rpc = _RPC(cpu, "stdio-ordinary")
    assert rpc.initialized["result"]["serverInfo"]["name"] == "meyo-orchestrator"
    for request_id in (1, 2):
        rpc.call(request_id, "list_harnesses")
        result = rpc.receive(request_id)["result"]
        assert json.loads(result["content"][0]["text"]) == {"cpu_only": True}
    rpc.proc.stdin.close()
    assert rpc.proc.wait(timeout=3) == 0
    cpu.registry.unregister(rpc.proc.pid)  # The test launcher owns this non-shared process.


def _cpu_leaf(root):
    def terminate(_signum, _frame):
        time.sleep(0.12)  # Exercise a real grace period, not merely TERM delivery.
        (root / "leaf-cleaned").write_text("cleaned")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, terminate)
    (root / "leaf-ready").write_text("ready")
    while True:
        time.sleep(0.02)


def _cpu_wrapper(root):
    registry = _registry()
    child = subprocess.Popen(
        [sys.executable, "-u", str(THIS), "leaf", str(root)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, env=dict(os.environ),
    )
    try:
        assert registry.register(child.pid, owner=os.getpid(), comm=sys.executable,
                                 meta={"kind": "__ut_cpu_independent_leaf"})

        def terminate(_signum, _frame):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, terminate)
        pids = {"wrapper": os.getpid(), "leaf": child.pid}
        (root / "pids.json").write_text(json.dumps(pids))
        _wait_file(root / "leaf-ready")
        (root / "ready.json").write_text(json.dumps(pids))
        while True:
            time.sleep(0.02)
    finally:
        _signal_owned_group(child.pid, signal.SIGTERM)
        try:
            child.communicate(timeout=2)
        except BaseException:
            _signal_owned_group(child.pid, signal.SIGKILL)
            child.communicate(timeout=2)
        registry.unregister(child.pid)
        (root / "wrapper-cleaned").write_text("cleaned")


def _stdio_fixture(root, shared):
    import contextlib
    import runpy
    import threading
    import types

    if shared:
        class CPUHost:
            context = {"user_id": "__ut_cpu", "session_id": "__ut_cpu_stdio"}

            def __init__(self):
                self.local = threading.local()
                schema = {"type": "object", "properties": {}}
                self.tools = [("cpu_block", "CPU fixture", schema, self.block),
                              ("cpu_echo", "CPU fixture", schema, self.echo)]

            @contextlib.contextmanager
            def cancellation_scope(self, cancel_check):
                previous = getattr(self.local, "check", None)
                self.local.check = cancel_check
                try:
                    yield
                finally:
                    self.local.check = previous

            def _cancel_check(self):
                check = getattr(self.local, "check", None)
                return bool(check and check())

            def block(self, arguments):
                import media_tools
                rc, text = media_tools._bounded_media_process(
                    [sys.executable, "-u", str(THIS), "wrapper", str(root)],
                    arguments.get("timeout_s", 30), cancel_check=self._cancel_check,
                    env=dict(os.environ),
                )
                return {"ok": rc == 0, "text": text}

            def echo(self, arguments):
                (root / "echo-called").write_text("called")
                return {"ok": True, "message": arguments.get("message"),
                        "scoped": callable(getattr(self.local, "check", None)),
                        "cancelled": self._cancel_check()}

        host = CPUHost()
        module = types.ModuleType("native_capability_host")
        module.from_environment = lambda: host
        module.CapabilityImageResult = type("CapabilityImageResult", (dict,), {})
        sys.modules["native_capability_host"] = module
        registry = _registry()
        unregister = registry.unregister

        def record_unregister(pid):
            if pid == os.getpid():
                (root / "host-unregistered.json").write_text(json.dumps({
                    "wrapper_cleaned": (root / "wrapper-cleaned").exists(),
                    "leaf_cleaned": (root / "leaf-cleaned").exists(),
                }))
            unregister(pid)

        registry.unregister = record_unregister
    else:
        for name in ("orchestrator", "mcp_bridge", "skills", "templates", "health", "cascade"):
            sys.modules[name] = types.ModuleType(name)
        sys.modules["orchestrator"].list_harnesses = lambda: {"cpu_only": True}
    sys.argv = [str(SERVER)] + (["--shared-capabilities"] if shared else [])
    runpy.run_path(str(SERVER), run_name="__main__")


def _cpu_main():
    mode, directory = sys.argv[1:]
    root = Path(directory)
    _registry()

    def no_network(event, _args):
        if event in {"socket.connect", "socket.bind", "socket.getaddrinfo"}:
            raise AssertionError("CPU lifecycle test must not use the network")

    sys.addaudithook(no_network)
    if mode == "leaf":
        _cpu_leaf(root)
    elif mode == "wrapper":
        _cpu_wrapper(root)
    elif mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (root / "stubborn-pid").write_text(str(os.getpid()))
        while True:
            time.sleep(0.02)
    elif mode == "stdin-probe":
        import media_tools
        _, text = media_tools._bounded_media_process(
            [sys.executable, "-c", "import json,sys; print(json.dumps(sys.stdin.read()))"],
            3, env=dict(os.environ),
        )
        print(json.dumps({"child_stdin": json.loads(text), "parent_stdin": sys.stdin.read()}))
    else:
        assert mode in {"stdio-shared", "stdio-ordinary"}
        _stdio_fixture(root, shared=mode == "stdio-shared")


if __name__ == "__main__":
    _cpu_main()
