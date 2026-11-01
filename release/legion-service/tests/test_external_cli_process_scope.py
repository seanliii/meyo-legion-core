"""Real external-transport lifecycle using the shared process-scope fixture."""
import importlib
import json
import os
import subprocess
import sys
import time

import pytest

from test_native_detached_process_scope import runtime, alive, cleanup_group, wait_json


@pytest.mark.skipif(os.name != "posix", reason="POSIX detached-child scope")
@pytest.mark.parametrize("ending", ["timeout", "normal"])
def test_external_cli_owns_observed_children_without_stopping_another_request(runtime, tmp_path, ending):
    _, registry = runtime
    transport = importlib.import_module("external_agent_transports")
    child_file = tmp_path / "external-child.json"
    peer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(45)"],
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    registry.register(peer.pid, owner=os.getpid(), meta={"kind": "other-request"})
    code = (
        "import json,subprocess,sys,time; from pathlib import Path;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(45)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL);"
        f"Path({str(child_file)!r}).write_text(json.dumps({{'pid':child.pid}}));"
        "print('offline child started',flush=True);"
        + ("time.sleep(45)" if ending == "timeout" else "time.sleep(.8)")
    )
    spec = {"command": [sys.executable, "-c", code], "streaming_protocol": "text"}
    child = None
    try:
        if ending == "timeout":
            with pytest.raises(subprocess.TimeoutExpired):
                transport.run_cli(spec, {"workdir": str(tmp_path)}, dict(os.environ), .8)
        else:
            result = transport.run_cli(spec, {"workdir": str(tmp_path)}, dict(os.environ), 3)
            assert result["returncode"] == 0
        child = wait_json(child_file)["pid"]
        if ending == "timeout":
            deadline = time.monotonic() + 2
            while alive(child) and time.monotonic() < deadline:
                time.sleep(.02)
            assert not alive(child), "external timeout leaked its observed detached child"
        else:
            assert alive(child), "normal completion is not permission to stop a handed-off service"
            assert any(row["pid"] == child for _, row in registry._load_entries())
        assert peer.poll() is None, "same supervisor PID must not imply same execution scope"
    finally:
        if child is None and child_file.exists():
            child = json.loads(child_file.read_text())["pid"]
        cleanup_group(child)
        cleanup_group(peer.pid)
        peer.wait(timeout=3)
        registry.unregister(peer.pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX native wrapper ownership")
@pytest.mark.parametrize("allow_native_cleanup", [True, False])
def test_wrapper_native_cleanup_is_distinct_from_scope_cleanup(
        runtime, tmp_path, monkeypatch, allow_native_cleanup):
    _, registry = runtime
    transport = importlib.import_module("external_agent_transports")
    identities = tmp_path / "native-wrapper.json"
    native_ready = tmp_path / "native-registered"
    calls, order = [], []
    real_scope, real_kill_owned = registry.NativeProcessScope, registry.kill_owned_by
    peer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(45)"],
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    registry.register(peer.pid, owner=os.getpid(), meta={"kind": "other-request"})

    class ReadyAbort(RuntimeError):
        pass

    class Scope(real_scope):
        def observe(self, *, force=False):
            rows = super().observe(force=force)
            if not force and native_ready.exists():
                raise ReadyAbort("explicit teardown after native registration")
            return rows

        def cleanup(self, **kwargs):
            order.append("scope")
            return super().cleanup(**kwargs)

    def record_kill(owner, **kwargs):
        calls.append((owner, kwargs))
        return real_kill_owned(owner, **kwargs)

    def native_cleanup(record):
        identity = wait_json(identities)
        assert record["owner"] == identity["wrapper_pid"]
        assert not alive(identity["wrapper_pid"]), "native cleanup preceded wrapper exit"
        order.append("native")
        if allow_native_cleanup:
            cleanup_group(record["pid"])
        return {"completed": allow_native_cleanup}

    def event(_event):
        if native_ready.exists():
            return
        identity = wait_json(identities)
        assert registry.register(
            identity["child_pid"], owner=identity["wrapper_pid"],
            meta={"native_cleanup": {"protocol": "__ut_native_protocol"}})
        native_ready.write_text("ready")

    monkeypatch.setattr(registry, "NativeProcessScope", Scope)
    monkeypatch.setattr(registry, "kill_owned_by", record_kill)
    monkeypatch.setattr(registry, "_cleanup_native", native_cleanup)
    command = (
        "import json,os,subprocess,sys,time; from pathlib import Path;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(45)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL);"
        f"Path({str(identities)!r}).write_text(json.dumps("
        "{'wrapper_pid':os.getpid(),'child_pid':child.pid}));"
        "print('native wrapper ready',flush=True);time.sleep(45)"
    )
    child = None
    try:
        with pytest.raises(ReadyAbort):
            transport.run_cli(
                {"command": [sys.executable, "-c", command], "streaming_protocol": "text"},
                {"workdir": str(tmp_path)}, dict(os.environ), 10, event_cb=event)
        identity = wait_json(identities)
        child = identity["child_pid"]
        assert order[:2] == ["native", "scope"], "wrapper protocol cleanup was replaced by ancestry cleanup"
        assert calls[0] == (identity["wrapper_pid"], {})
        assert all(kwargs.get("scope_id") for owner, kwargs in calls if owner == os.getpid())
        assert peer.poll() is None
        if allow_native_cleanup:
            deadline = time.monotonic() + 2
            while alive(child) and time.monotonic() < deadline:
                time.sleep(.02)
            assert not alive(child)
        else:
            assert alive(child), "scope cleanup overrode the native protocol refusal"
            assert registry._read_entry(registry._entry_path(child)) is not None
    finally:
        if child is None and identities.exists():
            child = json.loads(identities.read_text())["child_pid"]
        cleanup_group(child)
        cleanup_group(peer.pid)
        peer.wait(timeout=3)
        registry.unregister(peer.pid)
