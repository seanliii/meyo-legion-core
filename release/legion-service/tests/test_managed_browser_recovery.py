"""Explicit owned recovery; fake MCP only, no browser/network/native process."""
import importlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from test_local_managed_browser import material_scope

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
RECOVER = {"name": "browser_recover", "arguments": {}}
NAVIGATE = {"name": "browser_navigate", "arguments": {"url": "about:blank"}}
TABS = {"name": "browser_tabs", "arguments": {"action": "list"}}
NATIVE_TOOLS = [
    {"name": name, "description": "offline native tool",
     "inputSchema": {"type": "object"}}
    for name in ("browser_navigate", "browser_take_screenshot", "browser_snapshot", "browser_tabs")
]


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    for key in ("HOME", "CODEX_HOME", "TMPDIR", "MEYO_LEGION_DIR", "LEGION_HOME_DIR",
                "LEGION_PROC_REGISTRY_DIR"):
        root = tmp_path / ("native-" + key.lower())
        root.mkdir()
        monkeypatch.setenv(key, str(root))
    monkeypatch.delenv("MEYO_TASK_DEADLINE", raising=False)
    monkeypatch.delenv("LEGION_MANAGED_BROWSER_ACTION_TIMEOUT_MS", raising=False)
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.syspath_prepend(str(ORCH))

    def forbidden(*args, **kwargs):
        pytest.fail("real subprocess/network is forbidden in browser recovery tests")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def case(material_scope, monkeypatch):
    materials = material_scope
    mb = importlib.import_module("managed_browser")
    hr = importlib.import_module("harness_registry")
    bridge = importlib.import_module("meyo_harnesses.mcp_bridge")
    real_mcp = bridge._StdioMCP
    cancelled = threading.Event()
    # Real parent thread-local; the HTTP worker must capture, not reread it.
    monkeypatch.setattr(hr._INPROC_CANCEL_CHECK, "check", cancelled.is_set, raising=False)
    monkeypatch.setattr(hr, "_WALL_S", 0)
    clock = SimpleNamespace(now=10.0)
    monkeypatch.setattr(mb, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock.now))
    env = dict(materials.env, MEYO_TASK_DEADLINE="100")
    created, recipes = [], []

    class FakeServer:
        server_port = 45001

        def __init__(self, lease):
            self.lease = lease

        def serve_forever(self, **kwargs):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            pass

    class FakeMCP:
        def __init__(self, name, spec, **kwargs):
            self.recipe = recipes.pop(0) if recipes else {}
            self.spec, self.kwargs = spec, kwargs
            self.proc = None
            self.shutdown_facts = {}
            self.initialization_result = {"result": {}}
            self.starts = self.exits = self.lists = 0
            self.calls = []
            self._rpc_timeout_s = kwargs.get("rpc_timeout_s")
            created.append(self)

        def _start(self):
            self.starts += 1

        def _rpc(self, method, params=None, notify=False):
            return {}

        def __enter__(self):
            try:
                hook = self.recipe.get("before_start")
                if hook:
                    hook()
                self._start()
                hook = self.recipe.get("enter")
                if hook:
                    hook()
                if self.recipe.get("enter_error"):
                    raise RuntimeError("private fixture settings must not leak")
                self._rpc("initialize")
                return self
            except BaseException:
                self.__exit__()
                raise

        def __exit__(self, *args):
            self.exits += 1
            hook = self.recipe.get("exit")
            if hook:
                hook()
            if self.recipe.get("exit_error"):
                raise RuntimeError("private fixture cleanup detail")

        def list_tools(self):
            self.lists += 1
            hook = self.recipe.get("list")
            if hook:
                hook()
            return [dict(tool) for tool in self.recipe.get("tools", NATIVE_TOOLS)]

        def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            if self.recipe.get("tool_error"):
                raise TimeoutError("private failed URL and settings")
            return {"content": [{"type": "text", "text": "offline native receipt"}]}

    monkeypatch.setattr(mb, "_Server", FakeServer)
    monkeypatch.setattr(bridge, "_StdioMCP", FakeMCP)
    monkeypatch.setattr(mb.shutil, "which", lambda *args, **kwargs: str(materials.node))
    leases = []

    def start(recipe=None):
        if recipe:
            recipes.append(recipe)
        lease = mb.ManagedBrowserLease(materials.lease_context, env=env,
                                        config_dir=materials.roots["control"])
        leases.append(lease)
        return lease.__enter__()

    materials.lease_context = SimpleNamespace(
        user_id="fixture-owner", session_id="fixture-native-slot",
        workdir=str(materials.roots["work"]),
        task_facts={"team_id": "fixture-team", "task_id": "fixture-task"})
    result = SimpleNamespace(mb=mb, hr=hr, env=env, clock=clock, cancelled=cancelled,
                             created=created, recipes=recipes, start=start,
                             materials=materials, bridge=bridge, real_mcp=real_mcp)
    yield result
    for lease in leases:
        lease.close()
        # Unknown cleanup deliberately retains capacity in production. These
        # backends have no processes; emulate fixture owner exit, not recovery.
        if lease._lock_fd is not None:
            os.close(lease._lock_fd)
            lease._lock_fd = None


def listing(lease):
    """Actual handler/auth checks, in-memory request and reply; no listener."""
    from managed_browser import _Handler
    message = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    handler = _Handler.__new__(_Handler)
    handler.server = SimpleNamespace(lease=lease)
    handler.path = "/mcp"
    handler.headers = {
        "Host": lease._authority, "Authorization": "Bearer " + lease._token,
        "Mcp-Session-Id": lease._mcp_session,
        "Content-Type": "application/json", "Content-Length": str(len(message)),
    }
    handler.rfile = io.BytesIO(message)
    replies = []
    handler._reply = lambda status, body, **kwargs: replies.append((status, body))
    handler.do_POST()
    assert len(replies) == 1 and replies[0][0] == 200
    return replies[0][1]["result"]


def state(result):
    blocks = [block["text"] for block in result.get("content", [])
              if block.get("type") == "text"
              and block.get("text", "").startswith("MEYO_BROWSER_STATE_JSON\n")]
    assert len(blocks) == 1, "the member needs factual recovery state in the actual tool receipt"
    return json.loads(blocks[0].split("\n", 1)[1])


def fail(lease):
    result = lease.call(NAVIGATE)
    assert result.get("isError") is True
    return result


def test_directory_exposes_recovery_and_state_without_start_or_probe(case):
    lease = case.start()
    backend = case.created[0]
    before = (backend.starts, backend.lists, list(backend.calls))
    for _ in range(2):
        result = listing(lease)
        recovery = next((tool for tool in result["tools"] if tool["name"] == "browser_recover"), None)
        assert recovery is not None
        assert recovery["inputSchema"] == {
            "type": "object", "properties": {}, "additionalProperties": False}
        assert result["_meta"]["meyo_browser"]["recoverable"] is False
    assert (backend.starts, backend.lists, backend.calls) == before


def test_timeout_then_explicit_recover_preserves_binding_refs_and_does_not_replay(case):
    lease = case.start({"tool_error": True})
    original_config = lease.mcp_config
    original_identity = (lease.user_id, lease.session_id, lease.workspace)
    reference = {"path": "fixture-durable-screenshot", "sha256": "fixture-hash"}
    lease.observations.append({"artifacts": [reference]})
    assert state(fail(lease))["recoverable"] is True
    old = case.created[0]
    assert old.exits == 1
    assert listing(lease)["_meta"]["meyo_browser"]["status"] == "failed"
    result = lease.call(RECOVER)
    assert not result.get("isError"), result
    assert state(result)["status"] == "ready"
    assert "no previous pages or login state" in result["content"][0]["text"]
    assert "no actions were replayed" in result["content"][0]["text"]
    assert lease.mcp_config == original_config
    assert (lease.user_id, lease.session_id, lease.workspace) == original_identity
    assert lease.observations[0]["artifacts"] == [reference]
    new = case.created[1]
    assert new.calls == [] and old.calls == [("browser_navigate", {"url": "about:blank"})]
    assert new.kwargs["environment"]["HOME"] != old.kwargs["environment"]["HOME"]
    assert new.kwargs["environment"]["MEYO_MANAGED_BROWSER_WRITABLE_ROOTS"] == \
        old.kwargs["environment"]["MEYO_MANAGED_BROWSER_WRITABLE_ROOTS"]
    assert not lease.call(TABS).get("isError")
    assert old.exits == 1
    text = json.dumps(result)
    assert all(secret not in text for secret in (lease._token, lease.artifact_token,
                                                 "private failed URL", str(lease.control_dir)))


@pytest.mark.parametrize("arguments", [
    {"url": "https://outside.invalid"}, {"root": "/"}, {"network": True},
    {"env": {"HOME": "/"}}, {"_meta": {"cwd": "/"}}, [], None,
])
def test_recovery_parameters_cannot_change_scope(case, arguments):
    lease = case.start({"tool_error": True})
    fail(lease)
    result = lease.call({"name": "browser_recover", "arguments": arguments})
    assert result.get("isError") is True
    assert len(case.created) == 1
    # A rejected request must not consume the member's later valid recovery.
    assert not lease.call(RECOVER).get("isError")


@pytest.mark.parametrize("terminal", ["healthy", "closed", "cancelled", "runtime_budget"])
def test_healthy_or_terminal_scope_does_not_restart(case, terminal):
    lease = case.start({"tool_error": terminal != "healthy"})
    if terminal != "healthy":
        fail(lease)
    if terminal == "closed":
        lease.close()
    elif terminal == "cancelled":
        case.cancelled.set()
    elif terminal == "runtime_budget":
        case.clock.now = 101
    result = lease.call(RECOVER)
    assert result.get("isError") is True
    assert state(result)["recoverable"] is False
    assert len(case.created) == 1
    if terminal == "cancelled":
        case.cancelled.clear()
        assert lease.call(RECOVER).get("isError") is True
        assert len(case.created) == 1


def test_parent_cancel_binding_is_captured_before_http_thread(case):
    lease = case.start({"tool_error": True})
    fail(lease)
    case.cancelled.set()
    results = []
    thread = threading.Thread(target=lambda: results.append(lease.call(RECOVER)))
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert state(results[0])["reason"] == "cancelled"
    assert len(case.created) == 1


@pytest.mark.parametrize("phase", ["before_start", "enter", "list"])
def test_close_wins_recovery_race_and_backend_exit_is_once(case, phase):
    lease = case.start({"tool_error": True})
    fail(lease)
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(2)

    case.recipes.append({phase: hold, "exit": release.set})
    results = []
    recovering = threading.Thread(target=lambda: results.append(lease.call(RECOVER)))
    recovering.start()
    try:
        assert entered.wait(1), "recovery did not reach the fake startup boundary"
        closing = threading.Thread(target=lease.close)
        closing.start()
        closing.join(2)
        assert not closing.is_alive(), "close must interrupt initialization, not await its RPC timeout"
    finally:
        release.set()
        recovering.join(2)
    assert not recovering.is_alive()
    assert results[0].get("isError") is True
    assert lease.closed
    assert all(backend.exits == 1 for backend in case.created)
    if phase == "before_start":
        assert case.created[-1].starts == 0, "no spawn after close won"


@pytest.mark.parametrize("phase", ["before_start", "enter", "list"])
def test_deadline_expiry_during_recovery_never_returns_a_ready_lease(case, phase):
    lease = case.start({"tool_error": True})
    fail(lease)
    case.recipes.append({phase: lambda: setattr(case.clock, "now", 101)})
    result = lease.call(RECOVER)
    assert result.get("isError") is True
    assert state(result)["reason"] == "runtime_budget"
    assert state(result)["recoverable"] is False
    assert all(backend.exits == 1 for backend in case.created)
    if phase == "before_start":
        assert case.created[-1].starts == 0


def test_failed_recovery_can_be_explicitly_attempted_again_no_loop(case):
    lease = case.start({"tool_error": True})
    fail(lease)
    case.recipes.append({"enter_error": True})
    failed = lease.call(RECOVER)
    assert failed.get("isError") is True and state(failed)["recoverable"] is True
    assert len(case.created) == 2
    assert all(backend.exits == 1 for backend in case.created)
    assert not lease.call(RECOVER).get("isError")
    assert len(case.created) == 3
    case.created[-1].recipe["tool_error"] = True
    fail(lease)
    assert not lease.call(RECOVER).get("isError")
    assert len(case.created) == 4
    assert all(backend.exits == 1 for backend in case.created[:-1])
    assert all(not backend.calls for backend in (case.created[1], case.created[-1]))


def test_unconfirmed_cleanup_does_not_create_a_new_backend(case):
    lease = case.start({"tool_error": True, "exit_error": True})
    fail(lease)
    for _ in range(2):
        result = lease.call(RECOVER)
        assert result.get("isError") is True
        assert state(result)["reason"] == "cleanup_unconfirmed"
    assert len(case.created) == 1 and case.created[0].exits == 1


@pytest.mark.parametrize("new_tools", [
    [tool for tool in NATIVE_TOOLS if tool["name"] != "browser_take_screenshot"],
    [tool for tool in NATIVE_TOOLS if tool["name"] != "browser_tabs"],
    [{**tool, "inputSchema": {"type": "object", "required": ["new_argument"]}}
     for tool in NATIVE_TOOLS],
])
def test_recovery_rejects_missing_or_changed_native_tool_contract(case, new_tools):
    lease = case.start({"tool_error": True})
    original_tools = listing(lease)["tools"]
    fail(lease)
    case.recipes.append({"tools": new_tools})
    result = lease.call(RECOVER)
    assert result.get("isError") is True
    assert state(result)["recoverable"] is True
    assert lease.tools == original_tools
    assert case.created[-1].exits == 1
    assert not lease.call(RECOVER).get("isError")


def test_recovery_revalidates_original_materials_before_start(case):
    lease = case.start({"tool_error": True})
    fail(lease)
    entry = case.materials.package / "index.js"
    original = entry.read_bytes()
    entry.unlink()
    target = case.materials.roots["work"] / "untrusted.js"
    target.write_text("not an authorized runtime")
    entry.symlink_to(target)
    result = lease.call(RECOVER)
    assert result.get("isError") is True
    assert state(result)["recoverable"] is True
    assert len(case.created) == 1, "unsafe materials must be rejected before backend creation"
    entry.unlink()
    entry.write_bytes(original)
    assert not lease.call(RECOVER).get("isError")


def test_recovery_cannot_follow_replaced_workspace_root(case):
    lease = case.start({"tool_error": True})
    fail(lease)
    work = case.materials.roots["work"]
    work.rename(work.with_name("original-work"))
    other = work.with_name("other-owner")
    other.mkdir()
    work.symlink_to(other, target_is_directory=True)
    result = lease.call(RECOVER)
    assert result.get("isError") is True
    assert len(case.created) == 1


def test_recovery_uses_frozen_parent_env_not_later_caller_mutation(case):
    lease = case.start({"tool_error": True})
    fail(lease)
    original = case.created[0].kwargs["environment"]
    case.env.update(MEYO_RUNTIME_ROOT="/caller-replacement", MEYO_TASK_DEADLINE="100000")
    case.materials.lease_context.workdir = "/caller-replacement"
    assert not lease.call(RECOVER).get("isError")
    assert case.created[1].kwargs["environment"]["MEYO_RUNTIME_ROOT"] == original["MEYO_RUNTIME_ROOT"]
    case.clock.now = 101
    assert lease.call(RECOVER).get("isError") is True


def test_cleanup_completion_can_be_rechecked_without_stopping_again(case, monkeypatch):
    lease = case.start({"tool_error": True})
    old = case.created[0]
    process = SimpleNamespace(pid=987654321, code=None)
    process.poll = lambda: process.code
    old.proc = process
    killed = []
    registry = importlib.import_module("legion_proc_registry")
    monkeypatch.setattr(registry, "kill_owned_by", lambda pid: killed.append(pid))
    monkeypatch.setattr(lease, "_remember_descendants", lambda root: True)
    fail(lease)
    assert state(lease.call(RECOVER))["reason"] == "cleanup_unconfirmed"
    assert len(case.created) == 1 and old.exits == 1
    process.code = 0  # The old process has now actually exited.
    assert not lease.call(RECOVER).get("isError")
    assert old.exits == 1 and killed == [process.pid]


def test_worker_exit_does_not_hide_a_live_known_descendant(case, monkeypatch):
    lease = case.start({"tool_error": True})
    identity = (987654321, 123.0, "running")
    lease.browser_processes[987654322] = identity
    monkeypatch.setattr(case.mb, "_process_identity", lambda pid: identity)
    fail(lease)
    assert state(lease.call(RECOVER))["reason"] == "cleanup_unconfirmed"
    assert len(case.created) == 1


def test_recovery_cleans_backend_even_when_interrupted_by_baseexception(case):
    lease = case.start({"tool_error": True})
    fail(lease)

    def interrupted():
        raise KeyboardInterrupt("synthetic offline interruption")

    case.recipes.append({"list": interrupted})
    with pytest.raises(KeyboardInterrupt):
        lease.call(RECOVER)
    assert case.created[-1].exits == 1
    assert lease._recovering is False
    assert not lease.call(RECOVER).get("isError")


def test_parent_deadline_bounds_actual_stdio_wait_without_a_process(case, monkeypatch):
    lease = case.start()
    monkeypatch.setattr(case.bridge, "_StdioMCP", case.real_mcp)
    # The real transport writes an anonymous pipe and waits on its empty reply
    # queue. No child, browser or socket exists and no actual PID is assigned.
    backend = lease._create_backend()
    read_fd, write_fd = os.pipe()
    backend.proc = SimpleNamespace(stdin=os.fdopen(write_fd, "w"), stdout=io.StringIO())
    case.clock.now = 99.98
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="mcp_rpc_timeout:tools/call"):
            backend._rpc("tools/call", {"name": "browser_tabs", "arguments": {"action": "list"}})
        assert backend._rpc_timeout_s <= .021
        assert time.monotonic() - started < 1
        request = json.loads(os.read(read_fd, 4096))
        assert request["params"] == {"name": "browser_tabs", "arguments": {"action": "list"}}
    finally:
        backend.proc.stdin.close()
        backend.proc.stdout.close()
        backend.proc = None
        os.close(read_fd)


def test_initial_backend_missing_core_tool_is_not_advertised(case):
    with pytest.raises(case.mb.BrowserUnavailable, match="required core tools"):
        case.start({"tools": NATIVE_TOOLS[:1]})
    assert case.created[0].exits == 1


def test_cancellation_during_recovery_does_not_publish_ready_state(case):
    lease = case.start({"tool_error": True})
    fail(lease)
    case.recipes.append({"enter": case.cancelled.set})
    result = lease.call(RECOVER)
    assert result.get("isError") is True
    assert state(result)["reason"] == "cancelled"
    assert case.created[-1].exits == 1


@pytest.mark.parametrize("value", ["private-fixture-value", "NaN", "Infinity", "-1"])
def test_invalid_parent_deadline_is_rejected_without_echoing_configuration(case, value):
    case.env["MEYO_TASK_DEADLINE"] = value
    with pytest.raises(case.mb.BrowserUnavailable) as caught:
        case.start()
    assert str(caught.value) == "invalid parent task deadline"
    assert case.created == []


def test_confirmed_failed_cleanup_frees_slot_and_recovery_must_compete_again(case):
    first = case.start({"tool_error": True})
    fail(first)
    assert first._lock_fd is None, "a confirmed-dead backend must not hold capacity until recovery"
    case.materials.lease_context.session_id = "fixture-other-member"
    second = case.start()
    assert first.store == second.store
    assert not second.call(TABS).get("isError")
    assert second._lock_fd is not None
    assert not first.call(RECOVER).get("isError")
    replacement = case.created[-1]
    assert first._lock_fd is None, "recovery initializes MCP, not a browser or a private capacity lane"
    blocked = first.call(TABS)
    assert blocked.get("isError") is True
    assert "slot is in use" in blocked["content"][0]["text"]
    assert replacement.calls == []
    second.close()
    assert not first.call(TABS).get("isError")
    assert replacement.calls == [("browser_tabs", {"action": "list"})]


def test_unknown_cleanup_keeps_capacity_even_when_scope_closes(case):
    first = case.start({"tool_error": True, "exit_error": True})
    fail(first)
    case.materials.lease_context.session_id = "fixture-other-member"
    second = case.start()
    try:
        assert first._lock_fd is not None
        assert "slot is in use" in second.call(TABS)["content"][0]["text"]
        first.close()
        assert first.closed
        assert first._lock_fd is not None, "close cannot assert that unknown browser cleanup succeeded"
        assert "slot is in use" in second.call(TABS)["content"][0]["text"]
        assert case.created[1].calls == []
        assert first.call(RECOVER).get("isError") is True
    finally:
        # There are no actual child processes. Release only this fixture's own
        # descriptor, emulating owner process exit, so tests never leak capacity.
        if first._lock_fd is not None:
            os.close(first._lock_fd)
            first._lock_fd = None
