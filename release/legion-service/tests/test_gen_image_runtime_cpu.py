"""Real file/ledger boundaries with fake image transport; never call a model."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
import errno
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SERVICE = Path(__file__).resolve().parents[1]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF7sAAAAASUVORK5CYII=")


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "ANTHROPIC_", "CODEX_")):
            monkeypatch.delenv(key)
    root = tmp_path.resolve()
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "TMPDIR"):
        path = root / key
        path.mkdir(mode=0o700)
        monkeypatch.setenv(key, str(path))
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.syspath_prepend(str(SERVICE))
    local = threading.local()
    registry = SimpleNamespace(_INPROC_RUNTIME_ENV=local)
    monkeypatch.setitem(sys.modules, "harness_registry", registry)
    media = importlib.import_module("media_tools")
    monkeypatch.setattr(media, "_MEDIA_DIR", str(root / "host-media"))

    def forbidden(*args, **kwargs):
        raise AssertionError("offline test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    work = root / "work"
    work.mkdir(mode=0o700)
    state = root / "state"
    state.mkdir(mode=0o700)
    env = {
        "MEYO_WORKDIR": str(work), "MEYO_USER_ID": "fixture-user",
        "MEYO_SESSION_ID": "codex:fixture-order", "MEYO_LEGION_DIR": str(state),
        "MEYO_CAPABILITY_POLICY_JSON": json.dumps(
            {"billable": True, "network": True, "write": True, "read": True}),
    }
    local.values = dict(env)
    local.inherit_process_context = False
    value = SimpleNamespace(root=root, media=media, work=work, env=env, local=local,
                            calls=[], patch=monkeypatch, real_run=subprocess.run)

    def child(command, **kwargs):
        value.calls.append((command, kwargs))
        path = Path(command[command.index("--out") + 1])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        result = {"ok": True, "path": str(path), "bytes": len(PNG),
                  "sha256": hashlib.sha256(PNG).hexdigest(), "request_started": True}
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")

    monkeypatch.setattr(media.subprocess, "run", child)
    return value


def test_bound_image_lands_in_its_workdir_with_verified_facts_and_no_fake_url(case):
    result = case.media.run_media_tool("gen_image", {"prompt": "fixture", "timeout_s": "5"})
    assert result["ok"], result
    assert Path(result["path"]).is_relative_to(case.work)
    assert result["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert result["bytes"] == len(PNG)
    assert not result.get("url"), "a workspace file is not served under /media by magic"
    command, options = case.calls[0]
    assert len(case.calls) == 1 and "--json" in command
    assert Path(options["cwd"]) == case.work
    assert options["env"]["MEYO_SESSION_ID"] == case.env["MEYO_SESSION_ID"]
    assert options["env"]["MEYO_LEGION_DIR"] == case.env["MEYO_LEGION_DIR"]
    assert options["env"]["MEYO_MODEL_WORKSPACE_ID"] == json.dumps(
        [case.work.stat().st_dev, case.work.stat().st_ino])
    assert not (case.root / "host-media").exists()


def test_same_request_reuses_existing_ledger_and_rechecks_artifact(case):
    args = {"prompt": "fixture"}
    first = case.media.run_media_tool("gen_image", args)
    second = case.media.run_media_tool("gen_image", args)
    assert first["ok"] and second["ok"], (first, second)
    assert len(case.calls) == 1
    assert first["path"] == second["path"] and second["idempotent_hit"]
    assert second["sha256"] == first["sha256"]
    Path(first["path"]).unlink()
    third = case.media.run_media_tool("gen_image", args)
    assert not third["ok"] and third["requires_reconciliation"], third
    assert len(case.calls) == 1, "lost artifact is not permission to pay again"


def test_cached_artifact_hash_mismatch_never_silently_reuses_or_regenerates(case):
    first = case.media.run_media_tool("gen_image", {"prompt": "fixture"})
    assert first["ok"], first
    Path(first["path"]).write_bytes(b"replaced content")
    result = case.media.run_media_tool("gen_image", {"prompt": "fixture"})
    assert not result["ok"] and result["requires_reconciliation"]
    assert len(case.calls) == 1


def test_cached_result_cannot_bypass_current_read_permission(case):
    first = case.media.run_media_tool("gen_image", {"prompt": "fixture"})
    assert first["ok"], first
    case.local.values["MEYO_CAPABILITY_POLICY_JSON"] = json.dumps(
        {"billable": True, "network": True, "write": True, "read": False})
    result = case.media.run_media_tool("gen_image", {"prompt": "fixture"})
    assert not result["ok"]
    assert len(case.calls) == 1


def test_missing_bound_workdir_does_not_borrow_host_state(case):
    case.local.values.pop("MEYO_WORKDIR")
    case.patch.setenv("MEYO_WORKDIR", str(case.work))
    result = case.media.run_media_tool("gen_image", {"prompt": "fixture"})
    assert result["ok"] is False and result["error"] == "media_image_workspace_invalid"
    assert not case.calls


def test_threads_with_same_order_do_not_borrow_each_others_workdir_or_ledger(case):
    second = case.root / "second-work"
    second.mkdir(mode=0o700)
    barrier = threading.Barrier(2)

    def generate(work):
        case.local.values = {**case.env, "MEYO_WORKDIR": str(work)}
        case.local.inherit_process_context = False
        barrier.wait()
        return case.media.run_media_tool("gen_image", {"prompt": "fixture"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(generate, work) for work in (case.work, second)]
        results = [future.result(timeout=10) for future in futures]
    assert all(row["ok"] for row in results), results
    assert len(case.calls) == 2
    assert Path(results[0]["path"]).is_relative_to(case.work)
    assert Path(results[1]["path"]).is_relative_to(second)


def test_timeout_is_unknown_and_does_not_repeat_the_billable_call(case):
    def timeout(command, **kwargs):
        case.calls.append(command)
        raise subprocess.TimeoutExpired(command, 20)

    case.patch.setattr(case.media.subprocess, "run", timeout)
    first = case.media.run_media_tool("gen_image", {"prompt": "fixture"})
    second = case.media.run_media_tool("gen_image", {"prompt": "fixture"})
    assert not first["ok"] and first["requires_reconciliation"], first
    assert not second["ok"] and second["requires_reconciliation"], second
    assert len(case.calls) == 1


@pytest.mark.parametrize("started", [False, True])
def test_failed_child_preserves_request_start_boundary(case, started):
    def failure(command, **kwargs):
        case.calls.append(command)
        return subprocess.CompletedProcess(command, 1, json.dumps({
            "ok": False, "error": "media_image_unavailable", "request_started": started}),
            "")

    case.patch.setattr(case.media.subprocess, "run", failure)
    first = case.media.run_media_tool("gen_image", {"prompt": "fixture"})
    second = case.media.run_media_tool("gen_image", {"prompt": "fixture"})
    assert not first["ok"] and not second["ok"]
    assert first["error"] == "media_image_unavailable"
    assert first["request_started"] is started
    assert len(case.calls) == (1 if started else 2)


@pytest.mark.parametrize("started", [False, True])
def test_real_image_json_error_is_preserved_with_empty_stderr(case, started):
    """Exercise the actual CLI serializer, not an invented stderr contract."""
    cli = importlib.import_module("media_model_cli")
    wire, requests = [], []

    def fake_request(*args, **kwargs):
        requests.append(True)
        raise cli.MediaError("media_image_missing")

    case.patch.setattr(cli, "_request", fake_request)

    def inprocess_child(command, **kwargs):
        case.calls.append(command)
        target = Path(command[command.index("--out") + 1])
        if not started:
            target.write_bytes(PNG)  # Real O_EXCL failure before _request.
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, kwargs["env"], clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = cli.main(command[2:])
        wire.append((json.loads(stdout.getvalue()), stderr.getvalue()))
        return subprocess.CompletedProcess(command, rc, stdout.getvalue(), stderr.getvalue())

    case.patch.setattr(case.media.subprocess, "run", inprocess_child)
    args = {"prompt": "offline serializer fixture"}
    first = case.media.run_media_tool("gen_image", args)
    second = case.media.run_media_tool("gen_image", args)
    assert wire[0][1] == ""
    assert wire[0][0]["request_started"] is started
    assert first["request_started"] is started
    assert first["error"] == wire[0][0]["error"]
    assert len(case.calls) == (1 if started else 2)
    assert len(requests) == (1 if started else 0)
    if started:
        assert second["error"] == "SIDE_EFFECT_UNKNOWN"


@pytest.mark.parametrize("error", [
    {"secret": "do-not-echo"}, ["do-not-echo"], None, "untrusted:do-not-echo",
])
def test_json_failure_never_echoes_unrecognized_or_non_string_errors(case, error):
    def failure(command, **kwargs):
        case.calls.append(command)
        return subprocess.CompletedProcess(command, 1, json.dumps({
            "ok": False, "error": error, "request_started": True}), "")

    case.patch.setattr(case.media.subprocess, "run", failure)
    result = case.media.run_media_tool("gen_image", {"prompt": "fixture"})
    assert result["error"] == "media_tool_invalid_response"
    assert result["requires_reconciliation"]
    assert "do-not-echo" not in json.dumps(result)


def test_post_popen_pipe_oserror_is_unknown_not_safe_to_repeat(case):
    launches, communicates = [], []

    class PostStartPipeFailure:
        """Successful construction, then pipe failure; no real process starts."""

        def __init__(self, command, **kwargs):
            self.args = command
            launches.append(True)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, *args, **kwargs):
            communicates.append(True)
            raise OSError(errno.EIO, "fixture pipe failure after successful Popen")

        def kill(self):
            pass

    case.patch.setattr(subprocess, "Popen", PostStartPipeFailure)
    case.patch.setattr(case.media.subprocess, "run", case.real_run)
    args = {"prompt": "offline pipe fixture"}
    first = case.media.run_media_tool("gen_image", args)
    second = case.media.run_media_tool("gen_image", args)
    ledger_path = case.work / ".meyo-media" / "side_effect_ledger.json"
    rows = list(json.loads(ledger_path.read_text())["effects"].values())
    assert len(rows) == 1 and len(communicates) == 1
    assert (len(launches), first.get("request_started"), second.get("error"),
            rows[0]["status"], rows[0]["attempts"]) == (
                1, None, "SIDE_EFFECT_UNKNOWN", "unknown", 1)


def test_operator_legacy_cache_is_rejected_before_reading_outside_media_root(case):
    cli = importlib.import_module("media_model_cli")
    idem = importlib.import_module("legion_idempotency")
    case.local.values = {}
    case.local.inherit_process_context = True
    for key, value in case.env.items():
        if key != "MEYO_WORKDIR":
            case.patch.setenv(key, value)
    case.patch.delenv("MEYO_WORKDIR", raising=False)
    case.patch.delenv("MEYO_MODEL_WORKSPACE_ID", raising=False)
    Path(case.media._MEDIA_DIR).mkdir(mode=0o700)
    unrelated = case.root / "unrelated"
    unrelated.mkdir(mode=0o700)
    image = unrelated / "outside.png"
    image.write_bytes(PNG)
    image.chmod(0o600)
    params = {"prompt": "offline cache fixture"}
    old_cache = Path(case.env["MEYO_LEGION_DIR"]) / "idempotency" / "fixture-order.json"
    old_cache.parent.mkdir(mode=0o700)
    old_cache.write_text(json.dumps({idem._key("gen_image", params): {"result": {
        "ok": True, "path": str(image), "bytes": len(PNG),
        "sha256": hashlib.sha256(PNG).hexdigest(), "url": "/media/outside.png",
    }}}))
    old_cache.chmod(0o600)
    before = old_cache.read_bytes()

    def forbidden_read(*args, **kwargs):
        raise AssertionError("out-of-root operator cache was read")

    case.patch.setattr(cli, "image_file_facts", forbidden_read)
    result = case.media.run_media_tool("gen_image", params)
    assert not case.calls and old_cache.read_bytes() == before
    assert result["ok"] is False and result["error"] == "media_image_path_invalid"
    assert result["requires_reconciliation"]


def test_legacy_cache_is_read_only_and_validated_before_reuse(case):
    idem = importlib.import_module("legion_idempotency")
    image = case.work / "legacy.png"
    image.write_bytes(PNG)
    path = Path(case.env["MEYO_LEGION_DIR"]) / "idempotency" / "fixture-order.json"
    path.parent.mkdir(mode=0o700)
    params = {"prompt": "fixture"}
    path.write_text(json.dumps({idem._key("gen_image", params): {
        "result": {"ok": True, "path": str(image), "url": "/media/legacy.png"}}}))
    path.chmod(0o600)
    before = path.read_bytes()
    result = case.media.run_media_tool("gen_image", params)
    assert result["ok"] and result["idempotent_hit"], result
    assert result["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert not result.get("url") and not case.calls
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path], "legacy lookup must not acquire/create a lock"


@pytest.mark.parametrize("arguments", [["gen_image", "--help"], ["--json", "gen_image", "--help"]])
def test_media_help_never_invokes_model_or_ledger(case, arguments):
    script = """
import runpy, socket, subprocess, sys
def forbidden(*args, **kwargs):
    raise AssertionError('help attempted a side effect')
subprocess.run = forbidden
socket.socket.connect = forbidden
sys.path.insert(0, sys.argv[1])
sys.argv = [sys.argv[1] + '/media_tools.py', *sys.argv[2:]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    result = case.real_run([sys.executable, "-c", script, str(SERVICE), *arguments],
                          text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "prompt" in result.stdout and "ERROR" not in result.stdout
