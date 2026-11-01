"""Unknown/partial CPU samples cannot manufacture progress; no real processes."""
import ast
from contextlib import nullcontext
import ctypes
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    monkeypatch.syspath_prepend(str(SERVICE))
    return importlib.import_module("legion_proc_registry")


@pytest.mark.parametrize("scale", [1 / 1e9, (125 / 3) / 1e9])
def test_cpu_uses_only_totals_and_rejects_any_partial_read(registry, monkeypatch, scale):
    class Task(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("user", "system", "threads_user", "threads_system")]

    class API:
        missing = set()

        def proc_pidinfo(self, pid, flavor, arg, pointer, size):
            if pid in self.missing:
                return 0
            item = pointer._obj
            item.user = 10_000 if pid == 101 else 1_000
            item.system = 0
            item.threads_user = item.threads_system = 9_000_000
            return size

    api = API()
    monkeypatch.setattr(registry, "_darwin_proc_api", lambda: (ctypes, api, None, Task, scale))
    assert registry._darwin_cpu_seconds([101, 102]) == pytest.approx(11_000 * scale)
    api.missing = {102}
    assert registry._darwin_cpu_seconds([101, 102]) == -1
    api.missing = set()
    assert registry._darwin_cpu_seconds([101, 102]) == pytest.approx(11_000 * scale)


def test_scope_member_visibility_and_membership_changes_reset_baseline(registry, monkeypatch):
    monkeypatch.setattr(registry.sys, "platform", "darwin")
    monkeypatch.setattr(registry, "_pid_namespace", lambda: "fixture-host")
    monkeypatch.setattr(registry, "registry_directory_scope", lambda _: nullcontext())
    records = [
        {"pid": pid, "pgid": pid, "start_time": "fixture-birth", "owner": 999,
         "pid_namespace": "fixture-host", "meta": {"scope_id": "scope"}}
        for pid in (101, 102)
    ]
    rows = {r["pid"]: dict(r) for r in records}
    alive = {101, 102}
    monkeypatch.setattr(registry, "_load_entries", lambda: [(None, r) for r in records])
    monkeypatch.setattr(registry, "_darwin_process_snapshot", lambda _pids: dict(rows))
    monkeypatch.setattr(registry, "is_alive", lambda pid: pid in alive)
    monkeypatch.setattr(registry, "_darwin_cpu_seconds",
                        lambda pids: sum({101: 10., 102: 1.}[pid] for pid in pids) if pids else -1)
    scope = registry.NativeProcessScope.__new__(registry.NativeProcessScope)
    scope.owner, scope.scope_id = 999, "scope"
    scope._namespace, scope._directory, scope._cpu_cohort = "fixture-host", "/fixture", None
    assert scope.cpu_seconds() == 11
    hidden = rows.pop(102)
    assert scope.cpu_seconds() == -1, "a still-live invisible member is not zero CPU"
    rows[102] = hidden
    assert scope.cpu_seconds() == 11
    rows.pop(102)
    alive.remove(102)
    assert scope.cpu_seconds() == -1, "a changed member set needs a new difference baseline"
    assert scope.cpu_seconds() == 10


@pytest.mark.parametrize("platform,expected", [("darwin", -1), ("linux", 7)])
def test_unknown_scope_does_not_switch_cpu_counter_sets(platform, expected):
    path = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator/harness_registry.py"
    tree = ast.parse(path.read_text())
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "_owned_cpu_seconds")
    namespace = {
        "_sys": SimpleNamespace(platform=platform),
        "_process_scope": SimpleNamespace(cpu_seconds=lambda: -1),
        "_pgroup_cpu_seconds": lambda _: 7,
        "proc": SimpleNamespace(pid=101),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    assert namespace["_owned_cpu_seconds"]() == expected
