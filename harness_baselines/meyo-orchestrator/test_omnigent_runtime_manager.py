import json
import os
from unittest import mock

import omnigent_runtime_manager as R


class FakeProc:
    returncode = None
    def poll(self): return self.returncode
    def terminate(self): self.returncode = 0
    def wait(self, timeout=None): return self.returncode
    def kill(self): self.returncode = -9


def _disable_registry_sync(monkeypatch, manager):
    monkeypatch.setattr(manager, "_sync_agent_runtime", lambda: "test-fingerprint")


def test_reuses_online_host(monkeypatch, tmp_path):
    manager = R.OmnigentRuntimeManager(
        omnigent_root=tmp_path, orchestrator_root=tmp_path,
        server_url="http://local", popen=mock.Mock(),
    )
    _disable_registry_sync(monkeypatch, manager)
    monkeypatch.setattr(manager, "_host_online", lambda: True)
    status = manager.ensure_ready()
    assert status.ready and status.host_online
    manager.popen.assert_not_called()


def test_starts_host_and_waits_until_online(monkeypatch, tmp_path):
    spawned = mock.Mock(return_value=FakeProc())
    manager = R.OmnigentRuntimeManager(
        omnigent_root=tmp_path, orchestrator_root=tmp_path,
        server_url="http://local", popen=spawned,
    )
    checks = iter([False, False, True])
    _disable_registry_sync(monkeypatch, manager)
    monkeypatch.setattr(manager, "_host_online", lambda: next(checks))
    monkeypatch.setattr(R.time, "sleep", lambda value: None)
    status = manager.ensure_ready(timeout_s=2)
    assert status.ready and spawned.call_count == 1
    assert spawned.call_args.args[0][-2:] == ["host", "http://local"]


def test_sync_agent_runtime_exports_paths_and_fingerprint(monkeypatch, tmp_path):
    manager = R.OmnigentRuntimeManager(
        omnigent_root=tmp_path, orchestrator_root=tmp_path,
        server_url="http://local", popen=mock.Mock(),
    )
    fake_bridge = mock.Mock()
    fake_bridge.builtin_agent_env.return_value = "/tmp/captain:/tmp/meyo-cc"
    fake_bridge.bundle_fingerprint.return_value = "abc123"
    with mock.patch.dict("sys.modules", {"omnigent_registry_bridge": fake_bridge}):
        assert manager._sync_agent_runtime() == "abc123"
    fake_bridge.sync.assert_called_once_with()
    assert os.environ["OMNIGENT_BUILTIN_AGENT_DIRS"] == "/tmp/captain:/tmp/meyo-cc"
    assert os.environ["MEYO_OMNIGENT_AGENT_BUNDLE_FINGERPRINT"] == "abc123"


def test_host_exit_is_reported_not_exposed_as_manual_command(monkeypatch, tmp_path):
    proc = FakeProc(); proc.returncode = 2
    manager = R.OmnigentRuntimeManager(
        omnigent_root=tmp_path, orchestrator_root=tmp_path,
        server_url="http://local", popen=lambda *a, **k: proc,
    )
    _disable_registry_sync(monkeypatch, manager)
    monkeypatch.setattr(manager, "_host_online", lambda: False)
    status = manager.ensure_ready(timeout_s=1)
    assert not status.ready and status.error == "host_exited:2"
    assert "omnigent host" not in status.error
