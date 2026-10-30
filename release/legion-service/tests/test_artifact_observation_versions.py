"""Artifact observations keep scanned facts across worker and native/auto callbacks.

No model or service: existing run_one/worker fixtures replace only execution.
The nested watcher is also run directly for deterministic scan/write races.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace

import pytest

from test_explicit_responses_provider import isolated, explicit_run_one
from test_worker_json_framing import worker_stack


SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def pin_isolation(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_")):
            monkeypatch.delenv(name)
    for name in (
        "HOME", "CODEX_HOME", "TMPDIR", "MEYO_LEGION_DIR", "LEGION_HOME_DIR",
        "LEGION_PROJECT_CONTEXT_DIR", "LEGION_TASK_MEMORY_DIR", "LEGION_TEAM_DIR",
        "LEGION_ARTIFACT_STORE_DIR", "LEGION_SESSIONS_DIR", "LEGION_USER_CONFIG_DIR",
        "LEGION_PROC_REGISTRY_DIR",
    ):
        directory = tmp_path / ("pin-" + name)
        directory.mkdir()
        monkeypatch.setenv(name, str(directory))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def forbidden(*args, **kwargs):
        raise AssertionError("artifact observation tests never use the network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def fact_case(explicit_run_one):
    case = explicit_run_one
    case.ad = importlib.import_module("artifact_delivery")
    case.monkeypatch.setattr(case.ad, "OUTPUTS", str(case.root / "service-outputs"))
    case.monkeypatch.setattr(case.ad, "_git_changed_files", lambda *_: None)
    case.monkeypatch.setattr(
        importlib.import_module("artifact_store"), "ROOT", case.root / "artifact-store")
    case.work = Path(os.environ["MEYO_LEGION_DIR"]) / "outputs/__ut_artifact_facts"
    case.work.mkdir(parents=True)
    return case


def observe(case, key, observations, *, cancelled=False):
    """Real run_one and _prog; only the native execution endpoints are doubles."""
    events, calls = [], []

    def execute(channel, member, progress_cb, cancel_check, user_id, session_id):
        calls.append((channel, member))
        assert user_id == "owner-a" and session_id == f"{member}:original-session"
        assert cancel_check is not None and not cancel_check()
        for event in observations:
            progress_cb(deepcopy(event))
        return {"status": "ok", "final": "好", "workdir": str(case.work)}

    def native(member, task, qhash, model_override=None, appid_override=None, **kwargs):
        return execute("native", member, kwargs["progress_cb"], kwargs["cancel_check"],
                       kwargs["user_id"], kwargs["session_id"])

    def worker(member, task, qhash, timeout_s, model_override=None, appid_override=None,
               *, progress_cb=None, cancel_check=None, user_id="", session_id="",
               project_context=None, task_memory=None, execution_depth="standard",
               provider_binding=None, **kwargs):
        return execute("worker", member, progress_cb, cancel_check, user_id, session_id)

    mp = case.monkeypatch
    mp.setattr(case.core, "_harness_keys", lambda: {"codex", "auto"})
    mp.setattr(case.core, "_run_core_inprocess", native)
    mp.setattr(case.core, "_run_harness_isolated", worker)
    result = case.core.run_one(
        key, "offline artifact observation fixture", qhash="__ut_artifact_facts",
        preferred_model="fixture-model",
        preferred_appid="private-fixture-key" if key == "codex" else "fixture-only",
        user_id="owner-a", conversation_id="original-session",
        cancel_check=lambda: cancelled, cb=events.append)
    if cancelled:
        assert result["status"] == "cancelled" and not calls
    else:
        assert result["status"] == "done", result
        assert calls == [("native" if key == "codex" else "worker", key)]
    return [event for event in events if event["t"] == "artifact_ready"]


def fact(body=b"first real bytes\n"):
    return {
        "t": "artifact_ready", "url": "/home-outputs/__ut_fixture/index.html",
        "name": "index.html", "ext": "html", "bytes": len(body),
        "provenance": {"source": "workdir-discovery",
                       "sha256": hashlib.sha256(body).hexdigest(), "verified": True},
    }


@pytest.mark.parametrize("key", ["codex", "auto"])
def test_identical_fact_is_not_repeated_for_transport_noise(fact_case, key):
    first = fact()
    replay = {**first, "phase": "再次观察", "ts": 123, "elapsed": 10}
    replay["provenance"] = dict(reversed(list(first["provenance"].items())))
    events = observe(fact_case, key, [first, deepcopy(first), replay])
    assert len(events) == 1


@pytest.mark.parametrize("key", ["codex", "auto"])
def test_missing_hash_can_be_upgraded_once(fact_case, key):
    complete = fact()
    early = {k: v for k, v in complete.items() if k != "provenance"}
    events = observe(fact_case, key, [early, complete, deepcopy(complete), early])
    assert len(events) == 2
    assert "provenance" not in events[0]
    assert events[1]["provenance"] == complete["provenance"]


@pytest.mark.parametrize("key", ["codex", "auto"])
def test_same_url_changed_content_is_a_new_fact(fact_case, key):
    first, second = fact(b"version A\n"), fact(b"version B\n")
    events = observe(fact_case, key, [first, second, deepcopy(second), first])
    assert len(events) == 2
    assert [event["provenance"]["sha256"] for event in events] == [
        first["provenance"]["sha256"], second["provenance"]["sha256"]]


@pytest.mark.parametrize("key", ["codex", "auto"])
@pytest.mark.parametrize("metadata", [
    {"version": 2}, {"export_snapshot_id": "fixture-snapshot"},
    {"lineage": {"sources": [{"kind": "project_file", "path": "index.html"}]}},
    {"description": "新增导出说明"}, {"next_action": "打开查看"}, {"in_progress": False},
])
def test_same_url_changed_metadata_is_not_swallowed(fact_case, key, metadata):
    first = fact()
    changed = {**first, **metadata}
    events = observe(fact_case, key, [first, changed, deepcopy(changed)])
    assert len(events) == 2
    assert all(events[1][name] == value for name, value in metadata.items())


@pytest.mark.parametrize("key", ["codex", "auto"])
@pytest.mark.parametrize("identity", ["url", "path", "name-only"])
def test_same_name_does_not_merge_different_or_unidentified_files(fact_case, key, identity):
    first, second = fact(), fact()
    first.pop("url")
    second.pop("url")
    if identity != "name-only":
        first[identity] = "/home-outputs/one/index.html"
        second[identity] = "/home-outputs/two/index.html"
    assert len(observe(fact_case, key, [first, second])) == 2


@pytest.mark.parametrize("key", ["codex", "auto"])
def test_preexisting_cancel_does_not_emit_or_execute(fact_case, key):
    assert observe(fact_case, key, [fact()], cancelled=True) == []


@pytest.fixture
def watcher(fact_case):
    """Execute the exact nested product functions, without spawning its threads.

    This permits a deterministic mutation *after* scan_new_files returned,
    rather than a timing-dependent sleep or a reimplementation of the watcher.
    """
    case = fact_case
    module = ast.parse((SERVICE / "harness_worker.py").read_text(encoding="utf-8"))
    main = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                and node.name == "main")
    names = {"_emit_new_artifacts", "_artifact_meta", "_art_url"}
    functions = [node for node in ast.walk(main) if isinstance(node, ast.FunctionDef)
                 and node.name in names]
    events = []
    scope = {
        "Path": Path, "json": json, "time": time, "key": "__ut_artifact",
        "_attempt_workdir": str(case.work), "_artifact_started_at": 0,
        "_artifact_state": {}, "_artifact_seen": set(), "_artifact_safety_seen": set(),
        "_artifact_delivery": case.ad, "_emit_event": lambda event: events.append(deepcopy(event)),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]),
                 str(SERVICE / "harness_worker.py"), "exec"), scope)
    path = case.work / "deliverable.txt"
    path.write_bytes(b"first real bytes\n")
    path.with_name(path.name + ".progress.json").write_text(
        json.dumps({"description": "原始说明"}), encoding="utf-8")
    return SimpleNamespace(case=case, path=path, events=events, emit=scope["_emit_new_artifacts"])


def test_worker_keeps_real_scan_provenance_and_suppresses_duplicate(watcher):
    scan = watcher.case.ad.scan_new_files(0, workdir=str(watcher.case.work))[0]
    watcher.emit()
    watcher.emit()
    assert len(watcher.events) == 1
    event = watcher.events[0]
    assert event["provenance"] == scan["provenance"]
    assert event["bytes"] == scan["bytes"] == len(watcher.path.read_bytes())
    assert event["provenance"]["sha256"] == hashlib.sha256(watcher.path.read_bytes()).hexdigest()


def test_worker_unchanged_file_does_not_repeat_sidecar_grace_wait(watcher, monkeypatch):
    watcher.path.with_name(watcher.path.name + ".progress.json").unlink()
    waits = []
    monkeypatch.setattr(time, "sleep", waits.append)
    watcher.emit()
    watcher.emit()
    watcher.emit()
    assert waits == [0.15]


def test_worker_scan_then_rewrite_never_pairs_old_hash_with_new_stat(watcher, monkeypatch):
    first = watcher.path.read_bytes()
    second = b"second and longer real bytes\n"
    scan = watcher.case.ad.scan_new_files
    scans = []

    def scan_then_rewrite(*args, **kwargs):
        items = scan(*args, **kwargs)
        scans.append(deepcopy(items))
        if len(scans) == 1:
            watcher.path.write_bytes(second)
        return items

    monkeypatch.setattr(watcher.case.ad, "scan_new_files", scan_then_rewrite)
    watcher.emit()
    watcher.emit()
    watcher.emit()
    # Either retain a coherent older observation or defer it to the next scan.
    # Never combine versions, swallow the new scan, or amplify its repeats.
    bodies = {hashlib.sha256(body).hexdigest(): body for body in (first, second)}
    assert 1 <= len(watcher.events) <= 2
    for event in watcher.events:
        assert event["bytes"] == len(bodies[event["provenance"]["sha256"]])
    assert watcher.events[-1]["provenance"] == scans[1][0]["provenance"]
    assert sum(event["provenance"]["sha256"] == hashlib.sha256(second).hexdigest()
               for event in watcher.events) == 1


def test_worker_defers_internally_inconsistent_scan_until_next_observation(watcher, monkeypatch):
    """Real scanner gets size, preview, then provenance in separate reads."""
    second = b"longer bytes written between size and provenance\n"
    preview = watcher.case.ad._preview
    changed = False

    def rewrite_during_preview(path):
        nonlocal changed
        if not changed:
            changed = True
            watcher.path.write_bytes(second)
        return preview(path)

    monkeypatch.setattr(watcher.case.ad, "_preview", rewrite_during_preview)
    watcher.emit()
    watcher.emit()
    watcher.emit()
    assert len(watcher.events) == 1
    event = watcher.events[0]
    assert event["bytes"] == len(second)
    assert event["provenance"]["sha256"] == hashlib.sha256(second).hexdigest()


def test_worker_does_not_invent_missing_hash_and_allows_later_scan_upgrade(watcher, monkeypatch):
    scan = watcher.case.ad.scan_new_files
    first = True

    def legacy_then_complete(*args, **kwargs):
        nonlocal first
        items = scan(*args, **kwargs)
        if first:
            first = False
            for item in items:
                item.pop("provenance", None)
        return items

    monkeypatch.setattr(watcher.case.ad, "scan_new_files", legacy_then_complete)
    watcher.emit()
    watcher.emit()
    watcher.emit()
    assert len(watcher.events) == 2
    assert not watcher.events[0].get("provenance")
    assert not watcher.events[0].get("sha256")
    assert watcher.events[1]["provenance"]["sha256"] == hashlib.sha256(
        watcher.path.read_bytes()).hexdigest()


def test_worker_sidecar_metadata_upgrade_does_not_require_file_rewrite(watcher):
    watcher.emit()
    watcher.path.with_name(watcher.path.name + ".progress.json").write_text(
        json.dumps({"description": "补齐说明", "next_action": "打开文件"}), encoding="utf-8")
    watcher.emit()
    watcher.emit()
    assert len(watcher.events) == 2
    assert watcher.events[1]["description"] == "补齐说明"
    assert watcher.events[1]["next_action"] == "打开文件"


@pytest.mark.parametrize("sensitive_location", ["content", "sidecar"])
def test_worker_still_withholds_sensitive_content_and_metadata(watcher, sensitive_location):
    synthetic_secret = "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----"
    if sensitive_location == "content":
        watcher.path.write_text(synthetic_secret)
    else:
        watcher.path.with_name(watcher.path.name + ".progress.json").write_text(
            json.dumps({"description": synthetic_secret}))
    watcher.emit()
    assert not [event for event in watcher.events if event["t"] == "artifact_ready"]
    assert watcher.path.exists()


@pytest.mark.parametrize("key", ["__ut_terminal", "auto"])
@pytest.mark.parametrize("stop_after_artifact", [False, True])
def test_real_worker_scan_reaches_parent_with_exact_facts(
        worker_stack, monkeypatch, key, stop_after_artifact):
    """Real worker process, scanning and stderr pump; only orchestrator is a stub."""
    stack = worker_stack
    work = Path(os.environ["MEYO_LEGION_DIR"]) / "outputs/__ut_worker_artifact"
    work.mkdir(parents=True)
    shim = stack.root / "artifact-orchestrator"
    shim.mkdir()
    body = "<p>真实扫描字节，不调用模型</p>\r\n".encode()
    shim.joinpath("orchestrator.py").write_text(
        "import os,time\nfrom pathlib import Path\n"
        "def run_harness(key, task, **kwargs):\n"
        f"    assert key == {key!r}, key\n"
        "    path = Path(os.environ['MEYO_WORKDIR']) / 'index.html'\n"
        f"    path.write_bytes({body!r})\n"
        f"    while {stop_after_artifact!r}: time.sleep(.05)\n"
        "    return {'status': 'ok', 'final': '好'}\n",
        encoding="utf-8")
    monkeypatch.setenv("MEYO_ORCH_DIR", str(shim))
    events = []
    result = stack.core._run_harness_isolated(
        key, "offline fixture; no model", "__ut_worker_artifact", 10,
        model_override="fixture-model", appid_override="fixture-only",
        session_id="fixture-session", user_id="alice", progress_cb=events.append,
        cancel_check=(lambda: any(event["t"] == "artifact_ready" for event in events))
        if stop_after_artifact else None,
        project_context={"repo_root": str(work), "user_id": "alice"})
    assert result["status"] == ("cancelled" if stop_after_artifact else "ok"), result
    if stop_after_artifact:
        assert result["cancelled"] is True and result["ok"] is False
    artifacts = [event for event in events if event["t"] == "artifact_ready"]
    assert len(artifacts) == 1, events
    event = artifacts[0]
    assert Path(event["path"]).read_bytes() == body
    assert event["bytes"] == len(body)
    assert event["provenance"]["sha256"] == hashlib.sha256(body).hexdigest()
    assert event["provenance"]["source"] == "workdir-discovery"
    assert event["provenance"]["verified"] is True
