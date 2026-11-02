"""Pi config namespaces through the real registry; no model or credential-store access."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
NEW_NAMESPACE = "pi-agent-sessions"
NATIVE_POPEN = subprocess.Popen


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed_legacy_agent(directory):
    """Only synthetic old data; never use a real user's credential directory."""
    directory.mkdir(parents=True, exist_ok=True)
    records = {
        "models.json": {"providers": {"meyo": {
            "models": [{"id": "legacy-fixture-model"}], "apiKey": "legacy-fixture-not-real",
        }}},
        "auth.json": {"meyo": {"type": "api_key", "key": "legacy-fixture-not-real"}},
        "settings.json": {"fixture_setting": "must-remain-in-place"},
    }
    for name, value in records.items():
        path = directory / name
        path.write_text(json.dumps(value))
        path.chmod(0o600)
    return {path: digest(path) for path in directory.iterdir()}


@pytest.fixture
def pi_stack(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "ANTHROPIC_", "DEEPSEEK_")):
            monkeypatch.delenv(name)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    for name, leaf in (
        ("HOME", "home"), ("TMPDIR", "tmp"), ("MEYO_LEGION_DIR", "data"),
        ("LEGION_HOME_DIR", "legion"), ("LEGION_EXTERNAL_RUNTIME_ROOT", "runtimes"),
    ):
        directory = tmp_path / leaf
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.syspath_prepend(str(SERVICE))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    def prohibited(*args, **kwargs):
        raise AssertionError("Pi config tests must not run a harness or access the network")

    monkeypatch.setattr(socket.socket, "connect", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    monkeypatch.setattr(socket, "getaddrinfo", prohibited)
    monkeypatch.setattr(subprocess, "Popen", prohibited)
    registry = importlib.import_module("external_agent_registry")
    transports = importlib.import_module("external_agent_transports")
    catalog = json.loads((SERVICE / "external_agents.json").read_text())
    item = next(entry for entry in catalog["agents"] if entry["key"] == "pi")
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(registry, "load_catalog", lambda: [copy.deepcopy(item)])
    monkeypatch.setattr(registry, "_probe_one", lambda *a, **kw: {
        "execution_ready": True, "status": "ready", "status_detail": "",
    })
    monkeypatch.setattr(registry, "source_dir", lambda entry: source)
    monkeypatch.setattr(registry, "_deployment_paths", lambda entry: None)
    monkeypatch.setattr(registry, "_node_bin_dirs", lambda entry: [])
    monkeypatch.setitem(sys.modules, "legion_sandbox", SimpleNamespace(
        prepare_peer_sources=lambda *a, **kw: ""))
    stack = SimpleNamespace(registry=registry, item=item, root=tmp_path, calls=[], before_read=None)

    def transport(spec, values, environment, timeout_s, event_cb=None):
        if stack.before_read:
            stack.before_read(values)
        agent_dir = Path(environment["PI_CODING_AGENT_DIR"])
        assert agent_dir.is_relative_to(tmp_path), "Only newly generated synthetic state may be read"
        models = json.loads((agent_dir / "models.json").read_text())
        auth = json.loads((agent_dir / "auth.json").read_text())
        record = {
            "agent_dir": agent_dir, "session_dir": Path(values["session_dir"]),
            "session_id": values["session_id"], "requested_model": values["model"],
            "observed_model": models["providers"]["meyo"]["models"][0]["id"],
            "credential_matches": auth["meyo"]["key"] == values["appid"],
            "command": transports._format(spec["command"], values),
        }
        stack.calls.append(record)
        return {"status": "ok", "final": "SYNTHETIC_TRANSPORT_ONLY"}

    monkeypatch.setattr(registry, "run_transport", transport)
    return stack


def run(stack, work, session, model, *, checkpoint=None):
    return stack.registry.run(
        "pi", "Synthetic config isolation check", workdir=str(work),
        model=model, session_id=session, timeout_s=5,
        env={"MEYO_UNIFIED_APPID": "synthetic-not-a-real-credential-" + model,
             "MEYO_UNIFIED_GATEWAY": "https://example.invalid/v1"},
        runtime_context={"user_id": "fixture-owner",
                         "task_memory": {"native_checkpoint": checkpoint} if checkpoint else {}},
    )


@pytest.mark.parametrize("shared", (True, False), ids=("shared-workdir", "separate-workdirs"))
def test_different_native_sessions_keep_own_config_during_overlap(pi_stack, shared):
    a_ready, b_ready = threading.Event(), threading.Event()

    def pause_first(values):
        if values["model"] == "fixture-model-A":
            a_ready.set()
            assert b_ready.wait(4), "B could not reach the transport boundary"
        else:
            b_ready.set()

    pi_stack.before_read = pause_first
    work_a = pi_stack.root / "work"
    work_b = work_a if shared else pi_stack.root / "other-work"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, pi_stack, work_a, "native-A", "fixture-model-A")
        assert a_ready.wait(4)
        second = pool.submit(run, pi_stack, work_b, "native-B", "fixture-model-B")
        assert second.result(timeout=5)["status"] == "ok"
        assert first.result(timeout=5)["status"] == "ok"
    records = {row["requested_model"]: row for row in pi_stack.calls}
    for row in records.values():
        assert row["observed_model"] == row["requested_model"]
        assert row["credential_matches"] is True
    assert records["fixture-model-A"]["agent_dir"] != records["fixture-model-B"]["agent_dir"]


def test_same_workdir_sequential_calls_keep_own_config(pi_stack):
    work = pi_stack.root / "work"
    for session, model in (("native-A", "fixture-model-A"), ("native-B", "fixture-model-B")):
        assert run(pi_stack, work, session, model)["status"] == "ok"
    assert all(row["observed_model"] == row["requested_model"] and row["credential_matches"]
               for row in pi_stack.calls)


def test_same_session_checkpoint_reuses_its_agent_dir_and_original_history_arguments(pi_stack):
    work = pi_stack.root / "work"
    first = run(pi_stack, work, "Native.Keep-A", "fixture-model-A")
    initial = pi_stack.calls[-1]
    native_state = initial["agent_dir"] / "models-store.json"
    native_state.write_text('{"fixture_native_state":"keep-on-resume"}')
    state_hash = digest(native_state)
    second = run(pi_stack, work, "different-logical-turn", "fixture-model-B",
                 checkpoint=first["native_checkpoint"])
    resumed = pi_stack.calls[-1]
    assert second["native_checkpoint"] == first["native_checkpoint"]
    assert resumed["session_id"] == initial["session_id"]
    assert resumed["agent_dir"] == initial["agent_dir"]
    assert resumed["session_dir"] == initial["session_dir"] == work / ".sessions"
    assert resumed["command"][resumed["command"].index("--session-id") + 1] == initial["session_id"]
    assert resumed["command"][resumed["command"].index("--session-dir") + 1] == str(work / ".sessions")
    assert resumed["observed_model"] == "fixture-model-B" and resumed["credential_matches"]
    assert digest(native_state) == state_hash


@pytest.mark.parametrize("session", ("native-A", "models.json", "auth.json", "../../Native:Safe"))
def test_legacy_state_is_untouched_and_existing_cli_session_mapping_is_reused(pi_stack, session):
    work = pi_stack.root / "work"
    legacy = work / ".sessions/pi-agent"
    original = seed_legacy_agent(legacy)
    assert run(pi_stack, work, session, "fixture-model-A")["status"] == "ok"
    row = pi_stack.calls[-1]
    safe = pi_stack.registry._safe_key(session) or "external"
    assert row["session_id"] == safe
    assert row["agent_dir"] == work / ".sessions" / NEW_NAMESPACE / safe
    assert row["agent_dir"].resolve().is_relative_to(work.resolve())
    assert all(digest(path) == old_hash for path, old_hash in original.items())
    assert not (row["agent_dir"] / "settings.json").exists(), "No implicit legacy profile migration"
    assert row["observed_model"] == "fixture-model-A" and row["credential_matches"]
    assert stat.S_IMODE((work / ".sessions").stat().st_mode) == 0o700
    for name in ("models.json", "auth.json"):
        assert stat.S_IMODE((row["agent_dir"] / name).stat().st_mode) == 0o600


def test_ambient_agent_dir_cannot_divert_the_session_binding(pi_stack, monkeypatch):
    host = pi_stack.root / "ambient-agent"
    original = seed_legacy_agent(host)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(host))
    work = pi_stack.root / "work"
    assert run(pi_stack, work, "native-A", "fixture-model-A")["status"] == "ok"
    row = pi_stack.calls[-1]
    assert row["agent_dir"] == work / ".sessions" / NEW_NAMESPACE / row["session_id"]
    assert row["observed_model"] == "fixture-model-A" and row["credential_matches"]
    assert os.environ["PI_CODING_AGENT_DIR"] == str(host), "Do not modify the host environment"
    assert all(digest(path) == old_hash for path, old_hash in original.items())


def test_new_config_paths_remain_private_export_paths(pi_stack):
    safety = importlib.import_module("artifact_content_safety")
    work = pi_stack.root / "work"
    assert run(pi_stack, work, "native-A", "fixture-model-A")["status"] == "ok"
    for name in ("models.json", "auth.json"):
        relative = (pi_stack.calls[-1]["agent_dir"] / name).relative_to(work)
        assert safety._export_internal_path(relative) is True


def test_original_pi_session_manager_reopens_history_at_unchanged_path(pi_stack):
    """Optional native source check; never launch Pi's CLI/model runtime."""
    configured = os.environ.get("PI_NATIVE_TEST_SOURCE")
    if not configured:
        pytest.skip("set PI_NATIVE_TEST_SOURCE to an existing matching Pi source checkout")
    native = Path(configured).resolve()
    session_module = native / "packages/coding-agent/src/core/session-manager.ts"
    tsx = native / "node_modules/tsx/dist/esm/index.mjs"
    node = shutil.which("node")
    if not session_module.is_file() or not tsx.is_file() or not node or sys.platform != "darwin":
        pytest.skip("existing Pi/tsx/Node and macOS sandbox are required; no installation")
    work = pi_stack.root / "work"
    history_dir = work / ".sessions"
    history_dir.mkdir(parents=True)
    sid = pi_stack.registry._safe_key("Native.Keep-A")
    stamp = "2026-11-02T00:00:00.000Z"
    history = history_dir / f"2026-11-02T00-00-00-000Z_{sid}.jsonl"
    records = [
        {"type": "session", "version": 3, "id": sid, "timestamp": stamp, "cwd": str(work)},
        {"type": "message", "id": "user0001", "parentId": None, "timestamp": stamp,
         "message": {"role": "user", "content": "LEGACY_HISTORY_SENTINEL", "timestamp": 0}},
    ]
    history.write_text("".join(json.dumps(row) + "\n" for row in records))
    before = digest(history)
    checkpoint = {"session_id": "Native.Keep-A"}
    assert run(pi_stack, work, "new-logical-turn", "fixture-model-A", checkpoint=checkpoint)["status"] == "ok"
    call = pi_stack.calls[-1]
    program = pi_stack.root / "native-history.mjs"
    program.write_text("""
import { pathToFileURL } from 'node:url';
const [modulePath, work, directory, id, expectedPath] = process.argv.slice(2);
const { SessionManager } = await import(pathToFileURL(modulePath).href);
const found = (await SessionManager.list(work, directory)).find(row => row.id === id);
if (!found || found.path !== expectedPath) throw new Error('original history lookup changed');
const manager = SessionManager.open(found.path, directory);
if (manager.getSessionId() !== id || !JSON.stringify(manager.getEntries()).includes('LEGACY_HISTORY_SENTINEL'))
  throw new Error('original history was not restored');
console.log(JSON.stringify({historyRestored: true, sessionId: manager.getSessionId()}));
""")
    # Native module may only write this synthetic fixture and cannot use network.
    profile = "\n".join([
        "(version 1)", "(allow default)", "(deny network*)",
        '(deny file-write* (require-all (require-not (subpath ' + json.dumps(str(pi_stack.root)) +
        ')) (require-not (literal "/dev/null"))))',
    ])
    command = [
        "/usr/bin/sandbox-exec", "-p", profile, node, "--max-old-space-size=128",
        "--import", str(tsx), str(program), str(session_module), str(work),
        str(call["session_dir"]), call["session_id"], str(history),
    ]
    environment = {
        "PATH": os.environ["PATH"], "HOME": str(pi_stack.root / "home"),
        "TMPDIR": str(pi_stack.root / "tmp"), "TSX_DISABLE_CACHE": "1",
        "PI_CODING_AGENT_DIR": str(call["agent_dir"]), "PI_SKIP_VERSION_CHECK": "1",
    }
    # Explicit exception to the fixture's subprocess ban: source-only history API, not CLI.
    process = NATIVE_POPEN(command, cwd=pi_stack.root, env=environment, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                           start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=3)
        pytest.fail("bounded native history check timed out; no installation or retry")
    assert process.returncode == 0, stderr
    assert json.loads(stdout)["historyRestored"] is True
    assert digest(history) == before
