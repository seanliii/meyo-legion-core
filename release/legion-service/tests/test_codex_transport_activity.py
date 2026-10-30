"""请求活动的私有 pipe：真实环回延迟流，无模型、浏览器或外网。"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import tomllib

import pytest

from test_codex_output_budget_transport import (
    runtime, declare, context, capture_native, loopback_provider, NATIVE, MODEL, KEY,
)


def declare_stream(case, milliseconds):
    value = declare(case, cap=None)
    value["stream_idle_timeout_ms"] = milliseconds
    case.mp.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps(value))


@pytest.mark.parametrize("milliseconds", [1, 1200, 600000])
def test_stream_timeout_is_validated_operator_metadata(runtime, milliseconds):
    declare_stream(runtime, milliseconds)
    assert runtime.mr.explicit_responses_binding()["stream_idle_timeout_ms"] == milliseconds


@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.0, "600000", 10**400])
def test_invalid_stream_timeout_does_not_silently_fall_back(runtime, value):
    declare_stream(runtime, value)
    with pytest.raises(ValueError):
        runtime.mr.explicit_responses_binding()
    calls, _ = capture_native(runtime)
    result = runtime.hr._codex_adapter("__ut_bad_stream", "fixture", ctx=context(runtime))
    assert not calls
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("matches", [True, False])
def test_adapter_passes_same_timeout_and_private_fd_only_for_matching_binding(runtime, matches):
    case = runtime
    declare_stream(case, 600000)
    calls, _ = capture_native(case)
    seen = []
    read_fd, write_fd = os.pipe()

    @contextmanager
    def transport(binding, environment, checkpoint=None):
        seen.append(binding)
        yield {**binding, "client_base_url": "http://127.0.0.1:49999/v1",
               "client_api_key": "fixture-observer-token", "runtime_activity_fd": read_fd}

    case.mp.setattr(case.owned, "transparent_bridge", transport)
    try:
        result = case.hr._codex_adapter(
            "__ut_stream_binding", "fixture", ctx=context(case, appid=KEY if matches else "other-key"))
        assert result["status"] == "ok"
        command, kwargs = calls[0]
        config = tomllib.loads("\n".join(command[i + 1] for i, value in enumerate(command) if value == "-c"))
        provider = config["model_providers"]["meyo_responses"]
        if matches:
            assert seen[0]["stream_idle_timeout_ms"] == 600000
            assert provider["stream_idle_timeout_ms"] == 600000
            assert kwargs["runtime_activity_fd"] == read_fd
        else:
            assert not seen
            assert "stream_idle_timeout_ms" not in provider
            assert "runtime_activity_fd" not in kwargs
        assert "fixture-observer-token" not in " ".join(command)
        assert "fixture-observer-token" not in kwargs["input"]
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_lease_bytes_extend_finite_request_and_errors_release_it(runtime):
    clock = [0.0]
    runtime.mp.setattr(runtime.hr, "_t", SimpleNamespace(monotonic=lambda: clock[0]))
    lease = runtime.hr.RuntimeLease(0.33)
    lease.observe({"call_id": "one", "phase": "start", "budget_s": 0.6})
    clock[0] = 0.5
    lease.observe({"call_id": "one", "phase": "bytes", "budget_s": 0.6})
    clock[0] = 0.94
    assert lease.expired() is None
    lease.observe({"call_id": "one", "phase": "error", "budget_s": 0.6})
    assert not lease.pending
    clock[0] = 1.28
    assert lease.expired() == "idle_stall"


def test_display_heartbeat_never_renews_lease_and_absolute_budget_wins(runtime):
    clock = [0.0]
    runtime.mp.setattr(runtime.hr, "_t", SimpleNamespace(monotonic=lambda: clock[0]))
    lease = runtime.hr.RuntimeLease(0.33)
    bounded = runtime.hr.RuntimeLease(0.33, deadline=0.3)
    bounded.observe({"call_id": "one", "phase": "start", "budget_s": 600})
    clock[0] = 0.2
    lease.observe({"hb": True})
    lease.observe({"heartbeat": True})
    lease.observe({"meaningful": False})
    clock[0] = 0.34
    assert lease.expired() == "idle_stall"
    assert bounded.expired() == "runtime_budget"


def private_events(text):
    prefix = "\x1e[legion-ev]"
    result = []
    for line in text.split("\n"):
        if prefix in line:
            event = json.loads(line.split(prefix, 1)[1])
            if event.get("t") == "runtime_activity" and event.get("call_id"):
                result.append({key: value for key, value in event.items() if key != "t"})
    return result


@contextmanager
def delayed_provider(delay, first_chunk=True):
    payload = b'data: {"type":"fixture","text":"PRIVATE-BODY-NOT-ACTIVITY"}\n\n'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload) * 2))
                self.end_headers()
                if first_chunk:
                    self.wfile.write(payload)
                    self.wfile.flush()
                time.sleep(delay)
                self.wfile.write(payload if first_chunk else payload * 2)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


CLIENT = """
import json,os,urllib.request
print(json.dumps({"type":"thread.started","thread_id":"retained-native-checkpoint"}),flush=True)
request=urllib.request.Request(os.environ["FIXTURE_URL"]+"/responses",
 data=json.dumps({"model":"fixture-output-budget","stream":True,"input":[]}).encode(),
 headers={"Content-Type":"application/json","Authorization":"Bearer "+os.environ["FIXTURE_TOKEN"]})
with urllib.request.urlopen(request,timeout=3) as response:
 response.read()
"""


@pytest.mark.parametrize(("delay", "milliseconds", "success"), [(0.65, 1200, True), (0.65, 120, False)])
def test_real_stream_activity_protects_lease_but_real_read_timeout_exits(
    runtime, capsys, delay, milliseconds, success,
):
    case = runtime
    case.mp.setattr(case.hr, "_TASK_IDLE_S", 0.2)
    case.mp.setattr(case.hr, "_pgroup_cpu_seconds", lambda *_: -1)
    stopped = []
    original_stop = case.owned._stop_owned

    def stop(proc, registry):
        original_stop(proc, registry)
        stopped.append(proc.poll())
        assert not Path(registry._entry_path(proc.pid)).exists()

    case.mp.setattr(case.owned, "_stop_owned", stop)
    with delayed_provider(delay) as base:
        binding = {"model": MODEL, "api_key": KEY, "base_url": base, "http_headers": {},
                   "stamp": {"sha256": "fixture-stream-binding"}, "stream_idle_timeout_ms": milliseconds}
        with case.owned.transparent_bridge(binding, dict(os.environ)) as active:
            assert "runtime_activity_fd" in active, "显式观测必须接入真实 inherited pipe"
            activity_fd = active["runtime_activity_fd"]
            env = dict(os.environ, FIXTURE_URL=active["client_base_url"], FIXTURE_TOKEN=active["client_api_key"])
            result = case.hr._run_live([sys.executable, "-u", "-c", CLIENT], 3,
                                      env=env, task_run=True, runtime_activity_fd=activity_fd)
            assert (result.returncode == 0) is success
            assert "retained-native-checkpoint" in result.stdout
        with pytest.raises(OSError):
            os.fstat(activity_fd)
    assert len(stopped) == 1 and stopped[0] is not None
    events = private_events(capsys.readouterr().err)
    assert events and events[0]["phase"] == "start"
    assert any(event["phase"] == ("end" if success else "error") for event in events)
    assert all(set(event) == {"call_id", "phase", "budget_s"} for event in events)
    assert all(event["budget_s"] == milliseconds / 1000 for event in events)
    assert "PRIVATE-BODY-NOT-ACTIVITY" not in json.dumps(events)
    assert KEY not in json.dumps(events)
    # 同一事件可被现有外层 RuntimeLease 消费，无须改 core 或引入监控器。
    clock = [0.0]
    case.mp.setattr(case.hr, "_t", SimpleNamespace(monotonic=lambda: clock[0]))
    outer = case.hr.RuntimeLease(0.2)
    outer.observe(events[0])
    clock[0] = 0.3
    if success:
        assert outer.expired() is None


@pytest.mark.parametrize("stop", ["cancel", "absolute_budget"])
def test_pending_provider_never_defeats_cancel_or_budget_and_keeps_checkpoint(runtime, capsys, stop):
    case = runtime
    case.mp.setattr(case.hr, "_TASK_IDLE_S", 0.2)
    case.mp.setattr(case.hr, "_pgroup_cpu_seconds", lambda *_: -1)
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.write(write_fd, json.dumps({"call_id": "pending", "phase": "start", "budget_s": 5}).encode() + b"\n")
    cancelled = threading.Event()
    timer = None
    if stop == "cancel":
        case.hr.set_inproc_cancel_check(cancelled.is_set)
        timer = threading.Timer(0.2, cancelled.set)
        timer.start()
    command = [sys.executable, "-u", "-c",
               'import time;print(\'{"type":"thread.started","thread_id":"keep-this"}\',flush=True);time.sleep(10)']
    try:
        if stop == "absolute_budget":
            with pytest.raises(subprocess.TimeoutExpired) as caught:
                case.hr._run_live(command, 0.4, task_run=True, runtime_activity_fd=read_fd)
            assert caught.value.runtime_reason == "runtime_budget"
            assert "keep-this" in caught.value.legion_stdout
        else:
            result = case.hr._run_live(command, 3, task_run=True, runtime_activity_fd=read_fd)
            assert result.cancelled is True and result.returncode == 130
            assert "keep-this" in result.stdout
    finally:
        if timer:
            timer.cancel()
        case.hr.set_inproc_cancel_check(None)
        case.hr.consume_inproc_cancelled()
        os.close(read_fd)
        os.close(write_fd)


def test_actual_native_loopback_keeps_checkpoint_and_emits_only_private_metadata(runtime, capsys):
    """同版本真实 Codex CLI；上游仅本机夹具，不是模型调用。"""
    case = runtime
    assert NATIVE.is_file()
    with loopback_provider() as (base, observed):
        value = declare(case, base=base, cap=4096)
        value["stream_idle_timeout_ms"] = 600000
        case.mp.setenv("LEGION_CODEX_RESPONSES_BINDING", json.dumps(value))
        result = case.hr._codex_adapter(
            "__ut_native_stream_activity", "Return a fixture acknowledgment; no tools.",
            ctx=context(case, base=base))
    assert result["status"] == "ok", result
    assert result["native_checkpoint"]["session_id"]
    assert result["native_checkpoint"]["provider_binding"] == case.mr.ccr_binding_key(MODEL, KEY, base)
    assert len(observed) == 1
    assert observed[0]["body"]["max_output_tokens"] == 4096
    assert "stream_idle_timeout_ms" not in observed[0]["body"], "读超时属于 transport，不是模型请求正文"
    events = private_events(capsys.readouterr().err)
    assert {event["phase"] for event in events} >= {"start", "bytes", "end"}
    assert all(set(event) == {"call_id", "phase", "budget_s"} for event in events)
    assert all(event["budget_s"] == 600 for event in events)
    assert KEY not in json.dumps(events)
    assert "runtime_activity" not in json.dumps(result)
