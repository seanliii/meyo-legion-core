"""Offline native cancellation: real parent, worker, adapter, pipes and child process."""
from __future__ import annotations

import json
import importlib
import ast
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from test_worker_json_framing import worker_stack as existing_worker_stack

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
PRIVATE = "PRIVATE_REASONING_MUST_NOT_LEAVE_NATIVE"
ERROR = "stream disconnected before completion: error sending request"


@pytest.fixture
def stack(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "CODEX_", "ANTHROPIC_")):
            monkeypatch.delenv(name)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.syspath_prepend(str(ORCH))
    real_run_live = importlib.import_module("harness_registry")._run_live
    case = existing_worker_stack.__wrapped__(tmp_path, monkeypatch)
    case.real_run_live = real_run_live
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("LEGION_PROC_REGISTRY_DIR", str(tmp_path / "proc"))
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "bridge")
    return case


def invoke_native(stack, monkeypatch, *, stop=None, outcome="error", stubborn=False):
    """Replace only the CLI/model endpoint; exercise the actual adapter and worker."""
    root = stack.root
    calls = root / "native-calls"
    pidfile = root / "native-pid"
    stopped = root / "native-stopped"
    native = root / "fake-codex"
    # Build a fixture executable without interpolating shell commands or touching a real profile.
    script = f"""#!{sys.executable}
import json,os,signal,sys,time
from pathlib import Path
def emit(value): print(json.dumps(value), flush=True)
Path({str(pidfile)!r}).write_text(str(os.getpid()))
with open({str(calls)!r}, 'a') as f: f.write('call\\n')
def finish(*_):
    Path({str(stopped)!r}).write_text('SIGTERM drained')
    if {outcome != 'quiet'!r}:
        emit({{"type": "turn.failed", "error": {{"message": {ERROR!r}}}}})
    sys.exit(1)
signal.signal(signal.SIGTERM, finish)
sys.stdin.read()
args=sys.argv
Path(args[args.index('-o')+1]).write_text('已保存作品')
emit({{"type":"thread.started","thread_id":"fixture-native-checkpoint"}})
emit({{"type":"turn.started"}})
emit({{"type":"item.completed","item":{{"type":"reasoning","text":{PRIVATE!r}}}}})
"""
    if stop:
        if outcome != "quiet":
            script += 'emit({"type":"error","message":"Reconnecting 2/5: stream disconnected before completion"})\n'
        script += """
while True: time.sleep(.02)
"""
    else:
        message = ("unexpected status 403 Forbidden" if outcome == "permission" else ERROR)
        script += f"""emit({{"type":"error","message":{message!r}}})
emit({{"type":"turn.failed","error":{{"message":{message!r}}}}})
sys.exit(1)
"""
    native.write_text(script, encoding="utf-8")
    native.chmod(0o700)
    shim = root / "native-discovery"
    shim.mkdir()
    shim.joinpath("orchestrator.py").write_text(f"""
import sys,socket,os,json,time,signal
from contextlib import nullcontext
from pathlib import Path
sys.path.insert(0, {str(ORCH)!r})
import harness_registry as hr
def deny(*a, **kw): raise AssertionError("network forbidden by offline fixture")
socket.socket.connect=deny
socket.create_connection=deny
hr._sh.which=lambda _: {str(native)!r}
hr._codex_bridge_ctx=lambda *a, **kw: nullcontext(True)
hr._legion_prompt=lambda question, ctx: question
def run_harness(key, task, **kw):
    ctx=hr.HarnessCtx(model="fixture-model",appid="fixture-only",
        gateway="http://fixture.invalid",workdir=os.environ["MEYO_WORKDIR"],
        task_memory={{"native_checkpoint":{{"kind":"codex_thread",
                     "session_id":"fixture-native-checkpoint"}}}})
    if {stubborn!r}:
        # A worker which cannot finalize still leaves already observed diagnostics.
        if {stubborn == 'ignore_term'!r}: signal.signal(signal.SIGTERM, signal.SIG_IGN)
        hr._emit_runtime_status("codex", {{"source":"native_error","phase":"retry",
                                         "retry_number":2,"retry_total":5}})
        if {stubborn != 'ignore_term'!r}:
            for _ in range(100):
                hr._emit_runtime_status("codex", {{"source":"native_output","phase":"activity"}})
        if {stubborn == 'ignore_term'!r}:
            while True: time.sleep(.05)
        os._exit(7)
    return hr._codex_adapter("__native_recovery_fixture", task, ctx=ctx)
""", encoding="utf-8")
    monkeypatch.setenv("MEYO_ORCH_DIR", str(shim))
    events = []
    ready = threading.Event()
    budget_timing = {}

    def progress(event):
        events.append(event)
        if event.get("runtime_observation", {}).get("phase") == "retry":
            ready.set()
        if outcome == "quiet" and event.get("runtime_observation", {}).get("source") == "native_output":
            ready.set()

    if stop == "timeout":
        # Arm a real parent deadline only after the native retry reached its pump.
        # Worker startup retains its ordinary 10s allowance; no mocked clock or
        # synthetic expiry may replace RuntimeLease's actual deadline decision.
        original_lease = stack.registry.RuntimeLease

        class ReadyBudgetLease(original_lease):
            def expired(self):
                if ready.is_set() and "armed_at" not in budget_timing:
                    budget_timing["armed_at"] = time.monotonic()
                    self.deadline = min(self.deadline, budget_timing["armed_at"] + .2)
                    budget_timing["deadline"] = self.deadline
                reason = super().expired()
                if reason == "runtime_budget":
                    budget_timing["expired_at"] = time.monotonic()
                return reason

        monkeypatch.setattr(stack.registry, "RuntimeLease", ReadyBudgetLease)
    result = stack.core._run_harness_isolated(
        "__ut_terminal", "offline cancellation fixture", "__native_recovery",
        10,
        model_override="fixture-model", appid_override="fixture-only",
        session_id="fixture-session", user_id="alice",
        project_context={"repo_root": str(stack.project), "user_id": "alice"},
        cancel_check=ready.is_set if stop == "cancelled" else None,
        progress_cb=progress,
    )
    if pidfile.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
        assert calls.read_text().splitlines() == ["call"]
    return SimpleNamespace(result=result, events=events, stopped=stopped, ready=ready,
                           budget_timing=budget_timing)


@pytest.mark.parametrize("stop", ["cancelled", "timeout"])
def test_real_worker_drains_native_and_keeps_parent_stop(stack, monkeypatch, stop):
    run = invoke_native(stack, monkeypatch, stop=stop)
    result = run.result
    assert run.ready.is_set(), result
    if stop == "timeout":
        assert run.budget_timing["armed_at"] < run.budget_timing["deadline"]
        assert run.budget_timing["expired_at"] >= run.budget_timing["deadline"]
    assert run.stopped.read_text() == "SIGTERM drained"
    assert result["status"] == stop and result.get("ok") is not True
    assert bool(result.get("cancelled")) is (stop == "cancelled")
    assert result["error_envelope"]["category"] == (
        "user_cancel" if stop == "cancelled" else "runtime_budget")
    assert result["error_envelope"]["retryable"] is False
    assert result["native_checkpoint"]["session_id"] == "fixture-native-checkpoint"
    if stop == "cancelled":
        assert result["resumed_native"] is True
    assert result["final"] == "已保存作品"
    facts = result["native_facts"]
    if stop == "cancelled":
        assert facts["interrupted"] is True and facts["process_returncode"] == 130
    else:
        # Either the parent stop or the native deadline may win this race.
        assert facts["process_returncode"] in (None, 130)
    assert facts["turn_status"] == "failed"
    assert facts["turn_result"]["error"]["message"] == ERROR
    assert any(item.get("retry_number") == 2 for item in facts["runtime_observations"])
    assert PRIVATE not in json.dumps(result)
    assert PRIVATE not in json.dumps(run.events)
    assert (stack.project / "keep.txt").read_text() == "original project"


@pytest.mark.parametrize("outcome", ["error", "permission"])
def test_real_native_error_is_not_rewritten_as_cancellation(stack, monkeypatch, outcome):
    run = invoke_native(stack, monkeypatch, outcome=outcome)
    assert run.result["status"] == "rc1", run.result
    assert not run.result.get("cancelled")
    facts = run.result["native_facts"]
    assert facts["process_returncode"] == 1
    assert facts["turn_status"] == "failed"
    observations = facts["runtime_observations"]
    if outcome == "permission":
        assert any(item.get("http_status") == 403 for item in observations)
        assert "403" in run.result["error"]
    else:
        assert any(item.get("stream_disconnected") for item in observations)
    assert PRIVATE not in json.dumps(run.result)
    assert PRIVATE not in json.dumps(run.events)


def test_unfinalized_worker_retains_only_observed_facts(stack, monkeypatch):
    run = invoke_native(stack, monkeypatch, stubborn=True)
    assert run.result["status"] == "err:worker_no_json:rc7"
    facts = run.result["native_facts"]
    assert list(facts) == ["runtime_observations"]
    assert facts["runtime_observations"][0]["retry_number"] == 2
    assert len(facts["runtime_observations"]) == 2
    assert "native_checkpoint" not in run.result  # No fabricated native execution.


def test_uncooperative_worker_deadline_keeps_cancel_and_observed_retry(stack, monkeypatch):
    run = invoke_native(stack, monkeypatch, stop="cancelled", stubborn="ignore_term")
    assert run.result["status"] == "cancelled" and run.result["ok"] is False
    assert run.result["error_envelope"]["category"] == "user_cancel"
    assert run.result["native_facts"]["runtime_observations"][0]["retry_number"] == 2
    assert "native_checkpoint" not in run.result


def test_cancel_without_native_error_does_not_copy_raw_reasoning(stack, monkeypatch):
    run = invoke_native(stack, monkeypatch, stop="cancelled", outcome="quiet")
    assert run.result["status"] == "cancelled", run.result
    assert run.stopped.exists()
    assert "原生调用已停止" in run.result["error"]
    assert PRIVATE not in json.dumps(run.result)
    assert PRIVATE not in json.dumps(run.events)
    assert run.result["native_facts"]["turn_status"] == "started"


def test_owned_transport_facts_follow_existing_pipe_without_payload(stack):
    hr = stack.registry
    events = []
    read_fd, write_fd = os.pipe()
    def writer():
        for phase in ("start", "bytes", "end"):
            os.write(write_fd, (json.dumps({"call_id": PRIVATE, "phase": phase,
                "budget_s": 2}) + "\n").encode())
            time.sleep(.12)
        os.close(write_fd)
    thread = threading.Thread(target=writer)
    hr.set_inproc_event_sink(events.append)
    try:
        thread.start()
        result = stack.real_run_live([sys.executable, "-c", "import time;time.sleep(.6)"],
            3, tag="codex", task_run=True, runtime_activity_fd=read_fd)
        assert result.returncode == 0
    finally:
        hr.set_inproc_event_sink(None)
        thread.join(timeout=2)
        os.close(read_fd)
    public = [event for event in events if event.get("t") == "task_status"]
    observed = [event["runtime_observation"] for event in public]
    assert [item["phase"] for item in observed if item["source"] == "responses_transport"] == [
        "start", "bytes", "end"]
    assert PRIVATE not in json.dumps(public)


def test_already_cancelled_does_not_spawn_another_native_process(stack, monkeypatch):
    hr = stack.registry
    def forbidden(*a, **kw):
        raise AssertionError("cancelled invocation must not start a native process")
    monkeypatch.setattr(hr._sp, "Popen", forbidden)
    hr.set_inproc_cancel_check(lambda: True)
    try:
        result = stack.real_run_live(["fixture-never-started"], 1, task_run=True)
        assert result.cancelled and result.returncode == 130
    finally:
        hr.set_inproc_cancel_check(None)


def test_native_deadline_drains_terminal_before_raising_timeout(stack):
    pidfile = stack.root / "deadline-native-pid"
    command = [sys.executable, "-c", f"""
import os,signal,sys,time,json
from pathlib import Path
def finish(*_):
    print(json.dumps({{"type":"turn.failed","error":{{"message":"deadline drained"}}}}), flush=True)
    sys.exit(1)
signal.signal(signal.SIGTERM, finish)
Path({str(pidfile)!r}).write_text(str(os.getpid()))
print(json.dumps({{"type":"thread.started","thread_id":"deadline-checkpoint"}}), flush=True)
while True: time.sleep(.02)
"""]
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        stack.real_run_live(command, .5, tag="codex", task_run=True)
    assert raised.value.runtime_reason == "runtime_budget"
    records = [json.loads(line) for line in raised.value.legion_stdout.splitlines()]
    assert records[0]["thread_id"] == "deadline-checkpoint"
    assert records[-1]["error"]["message"] == "deadline drained"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.parametrize("missing", ["state", "setter", "noncallable"])
@pytest.mark.parametrize("stop", [False, True])
def test_legacy_registry_without_cancel_api_keeps_legacy_worker_stop(stack, monkeypatch, missing, stop):
    shim = stack.root / "legacy-discovery"
    shim.mkdir()
    registry = "from types import SimpleNamespace\n"
    if missing != "state":
        registry += "_INPROC_CANCEL_CHECK=SimpleNamespace(check=None)\n"
    if missing == "state":
        registry += "def set_inproc_cancel_check(check): pass\n"
    elif missing == "noncallable":
        registry += "set_inproc_cancel_check=None\n"
    shim.joinpath("harness_registry.py").write_text(registry)
    shim.joinpath("orchestrator.py").write_text(f"""
import signal,sys,json,time
def run_harness(key, task, **kwargs):
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    if {stop!r}:
        print('\\x1e[legion-ev]'+json.dumps({{'t':'fixture_legacy_started'}}), file=sys.stderr,flush=True)
        while True: time.sleep(.02)
    return {{'status':'ok','final':'legacy started'}}
""")
    monkeypatch.setenv("MEYO_ORCH_DIR", str(shim))
    started = threading.Event()
    def progress(event):
        if event.get("t") == "fixture_legacy_started":
            started.set()
    result = stack.core._run_harness_isolated(
        "__ut_terminal", "legacy fixture", "__legacy_stop", 5,
        model_override="fixture-model", appid_override="fixture-only",
        session_id="fixture-session", user_id="alice",
        project_context={"repo_root": str(stack.project), "user_id": "alice"},
        progress_cb=progress, cancel_check=started.is_set if stop else None)
    if stop:
        assert started.is_set() and result["status"] == "cancelled", result
        assert result["error_envelope"]["category"] == "user_cancel"
        assert not result.get("native_facts")  # No claim of a graceful native drain.
    else:
        assert result["status"] == "ok" and result["final"] == "legacy started", result


def test_native_stdout_cannot_claim_owned_responses_transport(stack):
    events = []
    hr = stack.registry
    hr.set_inproc_event_sink(events.append)
    try:
        record = json.dumps({"type":"runtime_activity", "call_id":"native_claim",
                             "phase":"bytes", "budget_s":2})
        result = stack.real_run_live([sys.executable, "-c", f"print({record!r})"],
            2, tag="codex", task_run=True)
        assert result.returncode == 0
    finally:
        hr.set_inproc_event_sink(None)
    observations = [event["runtime_observation"] for event in events
                    if "runtime_observation" in event]
    assert observations and all(item["source"] != "responses_transport" for item in observations)


def test_retry_status_is_content_free_coalesced_and_not_attempt_progress(stack, monkeypatch):
    hr = stack.registry
    events = []
    context = hr._RunEventContext(events.append)
    for n in (1, 1, 2):
        hr._emit_harness_event("codex", "stdout", json.dumps({
            "type": "error", "message": f"Reconnecting {n}/5: {ERROR} {PRIVATE}"}), context)
    assert [event["runtime_observation"]["retry_number"] for event in events] == [1, 2]
    import performance_contract
    for event in events:
        assert event["t"] == "task_status" and event["meaningful"] is False
        assert event["heartbeat"] is False
        assert not performance_contract.classify_event("step", event)["heartbeat"]
        assert not stack.core._event_advances_attempt(event)
    assert PRIVATE not in json.dumps(events)


def test_recovery_progress_persists_observation_but_not_private_activity(stack):
    # Execute the current recovery callback without importing/starting the HTTP server.
    tree = ast.parse((SERVICE / "server.py").read_text())
    resume = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_resume_saved_team")
    callback = next(node for node in ast.walk(resume) if isinstance(node, ast.FunctionDef)
                    and node.name == "progress")
    saved, renewed, emitted = [], [], []
    import artifact_delivery
    namespace = {"TEAMS": SimpleNamespace(
        renew_resume=lambda *args: renewed.append(args),
        append_event=lambda *args: saved.append(args)),
        "team_id": "fixture-team", "owner": "fixture-owner", "LEGION": stack.core,
        "ARTIFACTS": artifact_delivery}
    exec(compile(ast.Module(body=[callback], type_ignores=[]), str(SERVICE / "server.py"), "exec"), namespace)
    stack.registry._emit_runtime_status("codex", {"source": "native_error", "phase": "retry",
        "retry_number": 1, "retry_total": 5}, stack.registry._RunEventContext(emitted.append))
    for event in [emitted[0], {"t": "runtime_activity", "call_id": PRIVATE},
                  {"t": "reason", "hb": True, "x": "思考中"}]:
        namespace["progress"](event)
    assert saved == [("fixture-team", emitted[0], "step")]
    assert len(renewed) == 3


@pytest.mark.parametrize("value", [
    None, {"source": "unknown", "phase": "start"},
    {"source": "native_error", "phase": "private"},
])
def test_invalid_observation_is_rejected(stack, value):
    assert stack.registry.runtime_observation_facts(value) is None


def test_runtime_observation_whitelist(stack):
    facts = stack.registry.runtime_observation_facts({
        "source": "responses_transport", "phase": "bytes", "budget_s": 600,
        "observed_at": 12, "http_status": 200, "retry_number": True,
        "retry_total": -1, "prompt": PRIVATE, "headers": {"Authorization": PRIVATE},
        "body": PRIVATE, "call_id": PRIVATE, "env": PRIVATE,
    })
    assert facts == {"source": "responses_transport", "phase": "bytes",
                     "budget_s": 600, "observed_at": 12}
