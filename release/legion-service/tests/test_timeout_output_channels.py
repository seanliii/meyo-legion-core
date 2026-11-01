"""Stop authority and received output are independent; all providers are fixtures."""
from __future__ import annotations

import builtins
import copy
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_cli_runtime_channels import cli_stack, invoke, KEY
from test_native_terminal_boundaries import native_stack, invoke as invoke_hermes, done


TEXT = '好\u0085正文\u2028仍在同一包\u2029{"status":"error"}'
USAGE = {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    for key in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_PROC_REGISTRY_DIR"):
        root = tmp_path / ("base-" + key.lower())
        root.mkdir(exist_ok=True)
        monkeypatch.setenv(key, str(root))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)

    def forbidden(*args, **kwargs):
        raise AssertionError("No native runtime, model process or network in protocol fixtures")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def stopped(command, raw, mode):
    if mode == "timeout":
        exc = subprocess.TimeoutExpired(command, 1)
        exc.legion_stdout = raw
        exc.legion_stderr = "fixture diagnostic"
        exc.runtime_reason = "runtime_budget"
        raise exc
    return SimpleNamespace(returncode=130 if mode == "cancelled" else 0,
                           cancelled=mode == "cancelled", stdout=raw, stderr="")


@pytest.fixture
def jcode_scene(cli_stack, monkeypatch, tmp_path):
    from external_adapters import jcode_native, jcode_sdk
    import external_agent_registry

    case = cli_stack
    home = tmp_path / "native-home"
    (home / "sessions").mkdir(parents=True)
    sid = "offline-received-session"
    (home / "sessions" / f"{sid}.json").write_text(json.dumps({
        "id": sid, "working_dir": str(case.project),
    }))
    ctx = case.registry.HarnessCtx(
        model="offline-model", appid="offline-fixture", user_id="offline-owner",
        session_id="offline-conversation", workdir=str(case.project))
    frame = {"type": "done", "session_id": sid, "model": ctx.model,
             "provider": "offline-provider", "text": TEXT, "usage": USAGE}
    monkeypatch.setattr(jcode_native, "executable", lambda: "/offline/jcode")
    monkeypatch.setattr(jcode_native, "prepare", lambda *a: ({}, home, ""))
    monkeypatch.setattr(external_agent_registry, "load_catalog", lambda: [])
    monkeypatch.setattr(case.registry, "_legion_prompt", lambda q, ctx: q)
    return SimpleNamespace(case=case, native=jcode_native, sdk=jcode_sdk,
                           home=home, sid=sid, ctx=ctx, frame=frame)


@pytest.mark.parametrize("mode", ["timeout", "cancelled", "normal"])
def test_jcode_cli_keeps_complete_terminal_data_without_faking_rc(jcode_scene, monkeypatch, mode):
    s = jcode_scene
    raw = (json.dumps({"type": "start", "session_id": s.sid}) + "\n"
           + json.dumps(s.frame, ensure_ascii=False) + "\n")
    monkeypatch.setattr(s.case.registry, "_run_task", lambda cmd, *a, **kw: stopped(cmd, raw, mode))
    result = s.case.registry._jcode_adapter("__ut_timeout", "fixture", ctx=s.ctx)
    assert result["final"] == TEXT and result["usage"] == USAGE
    assert result["status"] == ("ok" if mode == "normal" else mode)
    assert result["native_checkpoint"]["session_id"] == s.sid
    if mode != "normal":
        assert result["partial"] is True
    if mode == "timeout":
        assert result["error_envelope"]["category"] == "runtime_budget"
        assert result["native_facts"].get("process_returncode") is None
    if mode == "cancelled":
        assert result["cancelled"] is True


@pytest.mark.parametrize("bad", ["snapshot", "foreign_session", "raw_console", "truncated"])
def test_jcode_cli_does_not_turn_unbound_or_incomplete_channels_into_answers(jcode_scene, bad):
    s = jcode_scene
    frame = dict(s.frame)
    checkpoint = {}
    if bad == "snapshot":
        frame["type"] = "session"
    if bad == "foreign_session":
        checkpoint = {"session_id": "other-native-session"}
    raw = json.dumps(frame, ensure_ascii=False)
    if bad == "raw_console":
        raw = "PRIVATE_THINKING_SENTINEL\nconsole progress\n"
    if bad == "truncated":
        raw = raw[:-4]
    result = s.native.result_from_native(
        SimpleNamespace(returncode=None, stdout=raw, stderr=""), checkpoint, s.home, s.ctx.model)
    assert result["final"] == "" and result["usage"] is None


def sdk_frame(s):
    return {"type": "jcode_sdk_result", "status": "ok", "session_id": s.sid,
            "model": "wire-fixture", "text": TEXT,
            "usage": {"input": 3, "output": 2, "cacheReadInput": 0},
            "model_binding_matches": True, "session_binding_matches": True,
            "cleanup": {"completed": True}}


@pytest.mark.parametrize("timed_out", [False, True])
def test_jcode_sdk_parses_before_applying_parent_stop(jcode_scene, timed_out):
    s = jcode_scene
    raw = json.dumps({"type": "jcode_sdk_snapshot", "session_id": s.sid}) + "\n"
    raw += json.dumps(sdk_frame(s), ensure_ascii=False) + "\n"
    result = s.sdk._result(s.sdk._records(raw), s.ctx, s.home, {},
                           {"wire_model": "wire-fixture"}, timed_out=timed_out)
    assert result["final"] == TEXT
    assert result["usage"]["input_tokens"] == 3
    assert result["usage"]["output_tokens"] == 2
    assert result["status"] == ("timeout" if timed_out else "ok")
    if timed_out:
        assert result["partial"] is True


@pytest.mark.parametrize("bad", [
    "snapshot", "session_id", "session_binding_matches", "model_binding_matches",
    "model", "missing_file", "stored_workdir", "truncated", "raw_console",
])
@pytest.mark.parametrize("timed_out", [False, True])
def test_jcode_sdk_rejects_foreign_or_unproven_data(jcode_scene, bad, timed_out):
    s = jcode_scene
    row = sdk_frame(s)
    checkpoint = {}
    if bad == "snapshot":
        row["type"] = "jcode_sdk_snapshot"
    elif bad == "session_id":
        checkpoint = {"session_id": "another-session"}
    elif bad in ("session_binding_matches", "model_binding_matches"):
        row[bad] = False
    elif bad == "model":
        row["model"] = "foreign-wire-model"
    elif bad == "missing_file":
        (s.home / "sessions" / f"{s.sid}.json").unlink()
    elif bad == "stored_workdir":
        (s.home / "sessions" / f"{s.sid}.json").write_text(json.dumps({
            "id": s.sid, "working_dir": str(s.home / "foreign-project")}))
    raw = json.dumps(row, ensure_ascii=False)
    if bad == "truncated":
        raw = raw[:-3]
    if bad == "raw_console":
        raw = "PRIVATE_THINKING_SENTINEL\n" + json.dumps({"text": TEXT, "status": "ok"})
    result = s.sdk._result(s.sdk._records(raw), s.ctx, s.home, checkpoint,
                           {"wire_model": "wire-fixture"}, timed_out=timed_out)
    assert result["final"] == "" and result["usage"] is None
    assert result["status"] != "ok"


@pytest.mark.parametrize("mode", ["timeout", "cancelled", "normal"])
def test_hermes_sdk_actual_producer_and_core_keep_data_on_stop(native_stack, monkeypatch, mode):
    native_run = native_stack.registry._run_live

    def boundary(cmd, *args, **kwargs):
        emitted = native_run(cmd, *args, **kwargs)
        return stopped(cmd, emitted.stdout, mode)

    monkeypatch.setattr(native_stack.registry, "_run_live", boundary)
    result = invoke_hermes(native_stack, [done(final_response=TEXT)])
    assert result["final"] == TEXT
    assert result["usage"]["total_tokens"] == 5
    assert result["status"] == ("done" if mode == "normal" else mode)
    if mode == "timeout":
        assert result["error_envelope"]["category"] == "runtime_budget"
    assert len(native_stack.calls) == 1


@pytest.mark.parametrize("bad", ["binding", "session", "snapshot", "raw_console", "truncated"])
def test_hermes_only_accepts_current_bound_machine_channels(native_stack, monkeypatch, bad):
    native_run = native_stack.registry._run_live

    def boundary(cmd, *args, **kwargs):
        emitted = native_run(cmd, *args, **kwargs)
        row = json.loads(emitted.stdout)
        if bad == "binding":
            row["hermes_adapter_binding"] = {"nonce": "another-request"}
        elif bad == "session":
            row["native_checkpoint"]["session_id"] = "another-native-session"
        elif bad == "snapshot":
            row.pop("hermes_adapter_result", None)
            row["hermes_adapter_snapshot"] = True
        raw = json.dumps(row, ensure_ascii=False)
        if bad == "raw_console":
            raw = "PRIVATE_THINKING_SENTINEL\n" + json.dumps({"final": TEXT, "status": "ok"})
        if bad == "truncated":
            raw = raw[:-4]
        return stopped(cmd, raw, "timeout")

    monkeypatch.setattr(native_stack.registry, "_run_live", boundary)
    result = invoke_hermes(native_stack, [done(final_response=TEXT)])
    assert result["final"] == ""
    assert result["status"] == "timeout"
    if bad == "snapshot":
        assert result["usage"]["total_tokens"] == 5
    else:
        assert not result["usage"].get("available")


@pytest.mark.parametrize("mode", ["timeout", "cancelled"])
def test_hermes_parent_stop_owns_error_channel_but_keeps_native_cause(
        native_stack, monkeypatch, mode):
    child_error = {"category": "authorization_required", "scope": "provider",
                   "retryable": False, "message": "native diagnostic"}
    native_stack.raw = [done(final_response=TEXT, error="native diagnostic",
                             error_envelope=child_error)]
    native_run = native_stack.registry._run_live

    def boundary(cmd, *args, **kwargs):
        emitted = native_run(cmd, *args, **kwargs)
        if mode == "timeout":
            exc = subprocess.TimeoutExpired(cmd, 1)
            exc.legion_stdout = emitted.stdout
            raise exc  # Ordinary timeout, not an invented owner-budget limit.
        return stopped(cmd, emitted.stdout, mode)

    monkeypatch.setattr(native_stack.registry, "_run_live", boundary)
    ctx = native_stack.registry.HarnessCtx(
        model="fixture-model", appid="fixture-only", workdir=str(native_stack.project),
        session_id="native-conversation", user_id="alice")
    result = native_stack.registry._hermes_adapter("__ut_stop", "fixture", ctx=ctx)
    assert result["status"] == mode and result["final"] == TEXT
    assert result["usage"]["total_tokens"] == 5
    assert result["native_facts"]["worker_error_envelope"] == child_error
    assert (result.get("error_envelope") or {}).get("category") != "authorization_required"
    if mode == "cancelled":
        assert result["error_envelope"]["category"] == "user_cancel"


@pytest.mark.parametrize("sidecar", ["valid", "missing", "invalid", "symlink", "oversized"])
def test_hermes_old_cli_timeout_reads_only_its_designated_usage_file(
        cli_stack, monkeypatch, tmp_path, sidecar):
    H = cli_stack.registry
    monkeypatch.setattr(H, "__file__", str(tmp_path / "old-layout/meyo-orchestrator/harness_registry.py"))
    monkeypatch.setattr(H._sh, "which", lambda name: "/offline/hermes" if name == "hermes" else None)
    monkeypatch.setattr(H, "_legion_prompt", lambda text, ctx: text)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="ai-gateway", stderr=""))
    seen = []
    foreign = tmp_path / "foreign-usage.json"
    foreign.write_text(json.dumps({"total_tokens": 999, "api_key": "PRIVATE_SENTINEL"}))

    def boundary(cmd, *a, **kw):
        path = Path(cmd[cmd.index("--usage-file") + 1])
        assert not path.exists(), "a previous request's usage must not be reused"
        if sidecar == "valid":
            path.write_text(json.dumps({**USAGE, "completed": True,
                                        "model": "fixture-model", "api_key": "PRIVATE_SENTINEL"}))
        elif sidecar == "invalid":
            path.write_text("{")
        elif sidecar == "symlink":
            path.symlink_to(foreign)
        elif sidecar == "oversized":
            path.write_text(json.dumps({**USAGE, "padding": "x" * 65536}))
        seen.append(path)
        return stopped(cmd, "PRIVATE_THINKING_SENTINEL\nunverified CLI output", "timeout")

    monkeypatch.setattr(H, "_run_task", boundary)
    ctx = H.HarnessCtx(model="fixture-model", appid="offline-fixture",
                       workdir=str(cli_stack.project))
    result = H._hermes_adapter("__ut_legacy", "fixture", ctx=ctx)
    assert result["final"] == "" and result["status"] == "timeout"
    if sidecar == "valid":
        assert result["usage"]["total_tokens"] == 5
    else:
        assert result["usage"] is None
    assert "PRIVATE_SENTINEL" not in json.dumps(result)
    assert "PRIVATE_THINKING_SENTINEL" not in json.dumps(result)
    assert result["error_envelope"]["category"] == "runtime_budget"
    assert seen and not seen[0].exists()
    assert json.loads(foreign.read_text())["total_tokens"] == 999


@pytest.fixture
def deepagents_scene(cli_stack, monkeypatch):
    case = cli_stack
    case.mode = "normal"
    case.bad = None
    case.frames = []
    H = case.registry
    monkeypatch.setattr(H, "_legion_prompt", lambda text, ctx: text)
    monkeypatch.setattr(H, "_find_search_cli", lambda: None)
    case.ctx = H.HarnessCtx(
        model="offline-model", appid="offline-fixture", user_id="offline-owner",
        session_id="offline-conversation", workdir=str(case.project))
    real_import = builtins.__import__

    def run(cmd, *args, **kwargs):
        request = json.loads(kwargs["input"])
        stdout = io.StringIO()
        result = {"status": "ok", "final": TEXT, "usage_records": [USAGE],
                  "workdir": request["workdir"], "model_used": request["model"],
                  "native_facts": {"engine": "deepagents", "completed": True}}

        def native(req):
            assert req == request
            # Arbitrary stdout must not be mistaken for the returned native result.
            stdout.write("PRIVATE_THINKING_SENTINEL\n"
                         + json.dumps({"status": "ok", "final": "not a terminal"}) + "\n")
            return copy.deepcopy(result)

        fake_sys = SimpleNamespace(path=[], stdin=io.StringIO(kwargs["input"]), stdout=stdout)
        modules = {"sys": fake_sys, "deepagents_checkpoint": SimpleNamespace(run_native=native)}

        def importer(name, *a, **kw):
            return modules[name] if name in modules else real_import(name, *a, **kw)

        def print_line(value, **kwargs):
            stdout.write(str(value) + "\n")

        exec(compile(cmd[cmd.index("-c") + 1], "<actual-deepagents-producer>", "exec"), {
            "__builtins__": {**vars(builtins), "__import__": importer, "print": print_line}})
        raw = stdout.getvalue()
        row = json.loads(raw.rstrip("\n").split("\n")[-1])
        case.frames.append(copy.deepcopy(row))
        if case.bad == "binding":
            row["binding"] = {"nonce": "foreign-request"}
        elif case.bad == "session":
            row.setdefault("result", result)["native_checkpoint"] = {
                "kind": "deepagents_langgraph_sqlite", "session_id": "foreign-session",
                "workdir": request["workdir"], "qhash": request["qhash"],
                "path": str(case.project / ".sessions/foreign.sqlite3")}
        elif case.bad == "workdir":
            row.setdefault("result", result)["workdir"] = str(case.project / "another")
        if case.bad in ("binding", "session", "workdir"):
            raw = json.dumps(row, ensure_ascii=False) + "\n"
        elif case.bad == "raw_console":
            raw = "PRIVATE_THINKING_SENTINEL\n" + json.dumps({"status": "ok", "final": TEXT}) + "\n"
        elif case.bad == "activity":
            raw = json.dumps({"type": "runtime_activity", "phase": "start", "call_id": "fixture"}) + "\n"
        elif case.bad == "truncated":
            raw = json.dumps(row, ensure_ascii=False)[:-5]
        return stopped(cmd, raw, case.mode)

    monkeypatch.setattr(H, "_run_task", run)
    return case


@pytest.mark.parametrize("mode", ["normal", "timeout", "cancelled"])
def test_deepagents_same_package_producer_and_consumer_preserve_stop_data(deepagents_scene, mode):
    s = deepagents_scene
    s.mode = mode
    result = s.registry._deepagents_adapter("__ut_framing", "fixture", ctx=s.ctx)
    assert s.frames[0].get("type") == "deepagents_adapter_result"
    assert result["final"] == TEXT and result["usage_records"] == [USAGE]
    assert result["status"] == ("ok" if mode == "normal" else mode)
    assert "PRIVATE_THINKING_SENTINEL" not in json.dumps(result)
    assert "binding" not in result
    if mode != "normal":
        assert result["partial"] is True
    if mode == "timeout":
        assert result["error_envelope"]["category"] == "runtime_budget"
        assert result["native_facts"]["completed"] is True


@pytest.mark.parametrize("bad", ["binding", "session", "workdir", "raw_console", "activity", "truncated"])
def test_deepagents_never_uses_arbitrary_status_json_or_foreign_frames(deepagents_scene, bad):
    s = deepagents_scene
    s.bad = bad
    result = s.registry._deepagents_adapter("__ut_framing", "fixture", ctx=s.ctx)
    assert result["status"] != "ok"
    assert result["final"] == "" and not result.get("usage_records")


@pytest.mark.parametrize("family", ["jcode", "deepagents"])
def test_real_core_consumes_complete_data_without_overriding_timeout(
        jcode_scene, deepagents_scene, monkeypatch, family):
    if family == "jcode":
        s = jcode_scene
        raw = json.dumps(s.frame, ensure_ascii=False) + "\n"
        monkeypatch.setattr(s.case.registry, "_run_task",
                            lambda cmd, *a, **kw: stopped(cmd, raw, "timeout"))
        case, adapter = s.case, s.case.registry._jcode_adapter
    else:
        case = deepagents_scene
        case.mode = "timeout"
        adapter = case.registry._deepagents_adapter
    monkeypatch.setitem(case.registry._STANDALONE_ADAPTERS, KEY, adapter)
    result = invoke(case)
    assert result["status"] == "timeout", result
    assert result["final"] == TEXT
    assert result["usage"]["total_tokens"] == 5
    assert result["error_envelope"]["category"] == "runtime_budget"
