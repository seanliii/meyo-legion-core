"""Real process regression: cancellation must include owned setsid children.

No models or provider calls. Every test has an independent last-resort cleanup
so reproducing the old leak cannot leave another background process behind.
"""
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

SERVICE = Path(__file__).resolve().parents[1]
HARNESS = SERVICE.parents[1] / "harness_baselines" / "meyo-orchestrator"


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    for name, leaf in (
        ("HOME", "home"), ("MEYO_LEGION_DIR", "meyo"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_PROC_REGISTRY_DIR", "processes"),
    ):
        monkeypatch.setenv(name, str(tmp_path / leaf))
    monkeypatch.delenv("MEYO_TASK_DEADLINE", raising=False)
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.syspath_prepend(str(HARNESS))
    registry = importlib.import_module("legion_proc_registry")
    harness = importlib.import_module("harness_registry")
    monkeypatch.setattr(harness, "_PROC_REGISTRY", registry)
    monkeypatch.setattr(harness._INPROC_CANCEL_CHECK, "check", None, raising=False)
    monkeypatch.setattr(harness._INPROC_CANCEL_CHECK, "cancelled", False, raising=False)
    monkeypatch.setattr(harness._INPROC_RUNTIME_ENV, "values", {}, raising=False)
    return harness, registry


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def cleanup_group(pid):
    if not pid or pid <= 1:
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def wait_json(path):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.02)
    raise AssertionError("fixture did not publish its process identity")


@pytest.mark.skipif(os.name != "posix", reason="POSIX setsid regression")
@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_interrupted_native_cli_reaps_detached_child_not_other_request(
        runtime, tmp_path, monkeypatch, reason):
    harness, registry = runtime
    child_file = tmp_path / "owned-child.json"
    # The unrelated task has the SAME supervisor PID. A kill-all-by-owner fix
    # would make this test fail and would be unsafe in an in-process server.
    peer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(45)"],
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    registry.register(peer.pid, owner=os.getpid(), meta={"kind": "other-request"})
    command = (
        "import json,subprocess,sys,time; from pathlib import Path;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(45)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL);"
        f"Path({str(child_file)!r}).write_text(json.dumps({{'pid':child.pid}}));"
        "print('owned detached worker started',flush=True);time.sleep(45)"
    )
    child_pid = None
    try:
        if reason == "cancel":
            harness._INPROC_CANCEL_CHECK.check = child_file.exists
            result = harness._run_live(
                [sys.executable, "-c", command], 10, cwd=str(tmp_path),
                tag="__ut_detached_scope", task_run=True,
            )
            assert result.cancelled is True
        else:
            original_expired = harness.RuntimeLease.expired
            # Exercise the real timeout cleanup after the real subprocess has
            # started. This is a lease-event fixture, not a subsecond startup
            # performance assertion on a developer's loaded machine.
            monkeypatch.setattr(
                harness.RuntimeLease, "expired",
                lambda lease: "runtime_budget" if child_file.exists() else original_expired(lease))
            with pytest.raises(subprocess.TimeoutExpired):
                harness._run_live(
                    [sys.executable, "-c", command], 10, cwd=str(tmp_path),
                    tag="__ut_detached_scope", task_run=True,
                )
        assert child_file.exists(), "native command did not reach the fixture"
        child_pid = json.loads(child_file.read_text())["pid"]
        deadline = time.monotonic() + 2
        while alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not alive(child_pid), "cancelled/timed-out native task left a detached child alive"
        assert peer.poll() is None, "cleanup killed a concurrent unrelated request"
    finally:
        harness._INPROC_CANCEL_CHECK.check = None
        if child_pid is None and child_file.exists():
            child_pid = json.loads(child_file.read_text())["pid"]
        cleanup_group(child_pid)
        cleanup_group(peer.pid)
        peer.wait(timeout=3)
        registry.unregister(peer.pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_crash_sweep_keeps_live_member_identity_after_group_leader_exits(runtime, tmp_path):
    _, registry = runtime
    child_file = tmp_path / "same-group-child.json"
    code = (
        "import json,subprocess,sys,time; from pathlib import Path;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(45)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        f"Path({str(child_file)!r}).write_text(json.dumps({{'pid':child.pid}}));"
        "time.sleep(45)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", code], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    child = None
    try:
        # owner=0 is the existing ledger's no-live-owner/crash-recovery state.
        scope = registry.NativeProcessScope(parent.pid, owner=0, meta={"kind": "__ut_scope"})
        child = wait_json(child_file)["pid"]
        scope.observe(force=True)
        entries = [d for _, d in registry._load_entries()]
        assert any(d["pid"] == child and d["pgid"] == parent.pid for d in entries)
        parent.kill()  # Kill the leader only; its child remains in the original group.
        parent.wait(timeout=3)
        assert alive(child)
        registry.sweep()
        deadline = time.monotonic() + 2
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not alive(child), "dead group leader caused a live member to lose its cleanup record"
    finally:
        try:
            os.killpg(parent.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        parent.wait(timeout=3)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process identity")
def test_scope_does_not_override_native_cleanup_refusal(runtime):
    _, registry = runtime
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(45)"], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        scope = registry.NativeProcessScope(process.pid, owner=os.getpid())
        registry.register(process.pid, owner=os.getpid(), meta={
            "native_cleanup": {"protocol": "__ut_unavailable_protocol"},
            "scope_id": scope.scope_id,
        })
        assert scope.observe(force=True) == []
        assert scope.cleanup() == []
        assert process.poll() is None
        assert registry._read_entry(registry._entry_path(process.pid)) is not None
    finally:
        cleanup_group(process.pid)
        process.wait(timeout=3)
        registry.unregister(process.pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process identity")
def test_observed_identity_must_still_match_when_registered(runtime):
    _, registry = runtime
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(45)"], start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert registry.register(
            process.pid, owner=os.getpid(), expected_start_time="not-this-process") is False
        assert registry._read_entry(registry._entry_path(process.pid)) is None
        assert process.poll() is None
    finally:
        cleanup_group(process.pid)
        process.wait(timeout=3)


@pytest.mark.skipif(os.name != "posix", reason="POSIX background lifetime")
@pytest.mark.parametrize("exit_code", [0, 3])
def test_cli_exit_keeps_successful_background_but_cleans_failed_scope(runtime, tmp_path, exit_code):
    harness, registry = runtime
    child_file = tmp_path / "intentional-background.json"
    code = (
        "import json,subprocess,sys,time; from pathlib import Path;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(45)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL);"
        f"Path({str(child_file)!r}).write_text(json.dumps({{'pid':child.pid}}));"
        f"print('background started',flush=True);time.sleep(.65);sys.exit({exit_code})"
    )
    child = None
    try:
        result = harness._run_live(
            [sys.executable, "-c", code], 10, cwd=str(tmp_path),
            tag="__ut_background_lifetime", task_run=True,
        )
        assert result.returncode == exit_code
        child = wait_json(child_file)["pid"]
        if exit_code == 0:
            assert alive(child), "normal return is not authorization to stop a handed-off service"
            assert any(d["pid"] == child for _, d in registry._load_entries())
        else:
            assert not alive(child), "failed CLI left its already observed detached worker alive"
    finally:
        if child is None and child_file.exists():
            child = wait_json(child_file)["pid"]
        cleanup_group(child)
