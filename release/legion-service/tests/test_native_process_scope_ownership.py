"""Independent, bounded review probes. Product sources remain read-only.

Run with Python directly; only standard-library unittest is required.
Every OS process is a private, short sleeping fixture, cleaned in tearDown.
The registry source is captured once at import, with its hash printed in the log.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

REGISTRY = Path(__file__).resolve().parents[1] / "legion_proc_registry.py"
SOURCE = REGISTRY.read_text()
SOURCE_SHA256 = hashlib.sha256(SOURCE.encode()).hexdigest()


def identity(pid):
    result = subprocess.run(
        ["ps", "-o", "pid=,ppid=,pgid=,lstart=,stat=", "-p", str(pid)],
        capture_output=True, text=True, timeout=2,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"},
    )
    parts = result.stdout.split()
    if len(parts) != 9 or parts[-1].startswith("Z"):
        return None
    return {
        "pid": int(parts[0]), "ppid": int(parts[1]), "pgid": int(parts[2]),
        "birth": " ".join(parts[3:8]),
    }


def announce(path, extra=""):
    """Fixture publishes its own identity before waiting or spawning children."""
    return (
        "import json,os,sys,time,subprocess;from pathlib import Path;"
        "r=subprocess.run(['ps','-o','lstart=','-p',str(os.getpid())],"
        "capture_output=True,text=True,timeout=2,"
        "env={**os.environ,'LC_ALL':'C','TZ':'UTC0'});"
        f"Path({str(path)!r}).write_text(json.dumps({{'pid':os.getpid(),"
        "'pgid':os.getpgrp(),'birth':' '.join(r.stdout.split())}));"
        + extra
    )


@unittest.skipUnless(os.name == "posix", "POSIX process identity")
class IndependentScopeReview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scope-independent-review-")
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, {
            "HOME": str(self.directory / "home"),
            "MEYO_LEGION_DIR": str(self.directory / "meyo"),
            "LEGION_HOME_DIR": str(self.directory / "legion"),
            "LEGION_PROC_REGISTRY_DIR": str(self.directory / "registry"),
        })
        self.env.start()
        self.reg = types.ModuleType("review_registry_" + self._testMethodName)
        self.reg.__file__ = str(REGISTRY)
        exec(compile(SOURCE, str(REGISTRY), "exec"), self.reg.__dict__)
        self.processes = []
        self.known = {}
        self.identity_files = []

    def spawn(self, code):
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", code], start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)
        current = identity(process.pid)
        self.assertIsNotNone(current)
        self.known[process.pid] = current
        return process

    def handoff_file(self, name):
        path = self.directory / name
        self.identity_files.append(path)
        return path

    def read_identity(self, path):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                value = json.loads(path.read_text())
                self.known[value["pid"]] = value
                return value
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(.01)
        self.fail("fixture did not publish identity")

    def current_owned(self, pid):
        current = identity(pid)
        expected = self.known.get(pid)
        # A fixture is explicitly allowed to setsid; birth must remain the same.
        return current if current and expected and current["birth"] == expected["birth"] else None

    def tearDown(self):
        # Recover only birth-stamped identities published by this test itself.
        for path in self.identity_files:
            try:
                value = json.loads(path.read_text())
                self.known.setdefault(value["pid"], value)
            except (OSError, ValueError, KeyError):
                pass
        try:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                for pid in self.known:
                    if self.current_owned(pid):
                        try:
                            os.kill(pid, sig)
                        except ProcessLookupError:
                            pass
                for process in self.processes:
                    try:
                        process.wait(timeout=.3)
                    except subprocess.TimeoutExpired:
                        pass
                time.sleep(.03)
            remaining = [pid for pid in self.known if self.current_owned(pid)]
            print("CLEANUP", self._testMethodName, json.dumps(remaining), flush=True)
            self.assertEqual(remaining, [], "review fixture leaked a process")
        finally:
            self.env.stop()
            self.temp.cleanup()

    def basic_tree(self):
        child_file = self.handoff_file("child.json")
        child = announce(child_file, "time.sleep(7)")
        parent = self.spawn(
            "import subprocess,sys,time;"
            f"subprocess.Popen([sys.executable,'-B','-c',{child!r}],"
            "start_new_session=True,stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(7)"
        )
        child_pid = self.read_identity(child_file)["pid"]
        return parent, child_pid

    def test_late_setsid_still_reaped_after_being_observed(self):
        child_file = self.handoff_file("late-child.json")
        moved_file = self.directory / "moved.json"
        trigger = self.directory / "detach"
        child = announce(child_file) + "\n"
        child += (
            "deadline=time.monotonic()+3\n"
            f"while not Path({str(trigger)!r}).exists() and time.monotonic()<deadline:\n"
            " time.sleep(.01)\n"
            "os.setsid()\n"
            f"Path({str(moved_file)!r}).write_text(json.dumps({{'pgid':os.getpgrp()}}))\n"
            "time.sleep(6)\n"
        )
        parent = self.spawn(
            "import subprocess,sys,time;"
            f"subprocess.Popen([sys.executable,'-B','-c',{child!r}],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL);time.sleep(7)"
        )
        child_pid = self.read_identity(child_file)["pid"]
        scope = self.reg.NativeProcessScope(parent.pid, owner=os.getpid())
        scope.observe(force=True)
        before = self.reg._read_entry(self.reg._entry_path(child_pid))
        self.assertEqual(before["pgid"], parent.pid)
        trigger.touch()
        deadline = time.monotonic() + 2
        while not moved_file.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(moved_file.exists())
        scope.observe(force=True)
        after = self.reg._read_entry(self.reg._entry_path(child_pid))
        reaper = threading.Thread(target=parent.wait, daemon=True)
        reaper.start()
        scope.cleanup()
        reaper.join(timeout=1)
        survivor = self.current_owned(child_pid)
        print("LATE_SETSID", json.dumps({
            "before_pgid": before["pgid"], "stored_pgid": after["pgid"],
            "actual_pgid": child_pid, "survived_cleanup": bool(survivor),
        }), flush=True)
        self.assertIsNone(survivor, "observed child changed PGID but ledger never refreshed it")

    def test_native_record_created_during_observe_is_not_overwritten(self):
        parent, child_pid = self.basic_tree()
        scope = self.reg.NativeProcessScope(parent.pid, owner=os.getpid())
        real_register = self.reg.register
        injected = []

        def concurrent_native_registration(pid, *args, **kwargs):
            if pid == child_pid and kwargs.get("expected_start_time") and not injected:
                # Deterministic interleaving: native registration happens after
                # observe's by_pid snapshot, before its locked register/write.
                self.assertTrue(real_register(
                    pid, owner=os.getpid(),
                    meta={"scope_id": "native-owner",
                          "native_cleanup": {"protocol": "__review_protected"}},
                ))
                injected.append(pid)
            return real_register(pid, *args, **kwargs)

        with patch.object(self.reg, "register", side_effect=concurrent_native_registration):
            scope.observe(force=True)
        record = self.reg._read_entry(self.reg._entry_path(child_pid))
        print("NATIVE_RACE", json.dumps({
            "injected": injected, "remaining_meta": record["meta"],
        }), flush=True)
        self.assertIn("native_cleanup", record["meta"],
                      "observer overwrote a concurrent native protection record")

    def test_other_scope_is_a_subtree_boundary(self):
        middle_file = self.handoff_file("middle.json")
        leaf_file = self.handoff_file("leaf.json")
        leaf_code = announce(leaf_file, "time.sleep(7)")
        middle_code = announce(middle_file) + (
            f"subprocess.Popen([sys.executable,'-B','-c',{leaf_code!r}],"
            "start_new_session=True,stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(7)"
        )
        parent = self.spawn(
            "import subprocess,sys,time;"
            f"subprocess.Popen([sys.executable,'-B','-c',{middle_code!r}],"
            "start_new_session=True,stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(7)"
        )
        middle_pid = self.read_identity(middle_file)["pid"]
        leaf_pid = self.read_identity(leaf_file)["pid"]
        first = self.reg.NativeProcessScope(parent.pid, owner=os.getpid())
        second = self.reg.NativeProcessScope(middle_pid, owner=os.getpid())
        first.observe(force=True)
        leaf = self.reg._read_entry(self.reg._entry_path(leaf_pid))
        print("FOREIGN_SCOPE", json.dumps({
            "outer_scope": first.scope_id, "inner_scope": second.scope_id,
            "leaf_scope": (leaf or {}).get("meta", {}).get("scope_id"),
        }), flush=True)
        reaper = threading.Thread(target=parent.wait, daemon=True)
        reaper.start()
        first.cleanup()
        reaper.join(timeout=1)
        self.assertIsNotNone(self.current_owned(leaf_pid),
                             "outer scope crossed an existing scope and killed its child")

    def test_sibling_scopes_sharing_owner_remain_independent(self):
        first_process = self.spawn("import time;time.sleep(7)")
        second_process = self.spawn("import time;time.sleep(7)")
        first = self.reg.NativeProcessScope(first_process.pid, owner=os.getpid())
        second = self.reg.NativeProcessScope(second_process.pid, owner=os.getpid())
        reaper = threading.Thread(target=first_process.wait, daemon=True)
        reaper.start()
        first.cleanup()
        reaper.join(timeout=1)
        self.assertIsNotNone(first_process.poll())
        self.assertIsNone(second_process.poll())
        second_record = self.reg._read_entry(self.reg._entry_path(second_process.pid))
        self.assertEqual(second_record["meta"]["scope_id"], second.scope_id)

    def test_native_refusal_outside_scope_filter_is_protected(self):
        parent, child_pid = self.basic_tree()
        scope = self.reg.NativeProcessScope(parent.pid, owner=os.getpid())
        scope.observe(force=True)
        self.assertTrue(self.reg.register(
            child_pid, owner=os.getpid(),
            meta={"scope_id": "native-owner",
                  "native_cleanup": {"protocol": "__review_unavailable"}},
        ))
        # The protected group exists outside the scope filter.
        reaper = threading.Thread(target=parent.wait, daemon=True)
        reaper.start()
        scope.cleanup()
        reaper.join(timeout=1)
        self.assertIsNotNone(self.current_owned(child_pid))
        data = self.reg._read_entry(self.reg._entry_path(child_pid))
        self.assertIn("native_cleanup", data["meta"])

    def test_observation_cost_is_reported_not_load_tested(self):
        parent, child_pid = self.basic_tree()
        scope = self.reg.NativeProcessScope(parent.pid, owner=os.getpid())
        original = self.reg.subprocess.run
        calls = []

        def measured_run(*args, **kwargs):
            calls.append(args[0][:4])
            return original(*args, **kwargs)

        started = time.monotonic()
        with patch.object(self.reg.subprocess, "run", side_effect=measured_run):
            scope.observe(force=True)
        elapsed = time.monotonic() - started
        print("OBSERVE_COST", json.dumps({
            "new_descendants": 1, "subprocess_calls": calls,
            "elapsed_seconds": round(elapsed, 6),
        }), flush=True)
        self.assertIsNotNone(self.reg._read_entry(self.reg._entry_path(child_pid)))


if __name__ == "__main__":
    print("SOURCE", REGISTRY, SOURCE_SHA256, flush=True)
    unittest.main(verbosity=2)
