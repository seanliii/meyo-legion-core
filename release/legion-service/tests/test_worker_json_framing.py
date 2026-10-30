"""真实 worker 的 UTF-8 JSON 行不能被正文中的 Unicode 分隔符拆碎。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading

import pytest

from test_native_terminal_boundaries import native_stack as existing_native_stack


@pytest.fixture
def worker_stack(tmp_path, monkeypatch):
    for name in ("HOME", "LEGION_TEAM_DIR", "MEYO_SESSION_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    stack = existing_native_stack.__wrapped__(tmp_path / "native", monkeypatch)
    assert Path(stack.core._WORKER).resolve() == (
        Path(__file__).resolve().parents[1] / "harness_worker.py")
    return stack


def invoke_worker(stack, monkeypatch, text, *, parent_stop=None):
    """只替换模型入口；协议输出、进程组、stdout pump 和父解析均运行真代码。"""
    expected = {
        "status": "ok", "ok": True, "final": "好",
        "native_checkpoint": {"kind": "codex_session", "session_id": "fixture-session"},
        "native_facts": {
            "completed": True,
            "tool_results": [{"type": "command_execution", "aggregated_output": text}],
        },
        "native_events": ["fixture-final"], "resumed_native": True,
        "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        "model_used": "fixture-model",
    }
    if parent_stop:
        expected["error_envelope"] = {
            "category": "rate_limit", "retryable": True, "scope": "provider",
            "message": "child envelope must not override the parent stop",
        }
    payload = stack.root / "fixture-result.json"
    payload.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
    marker = stack.root / "worker-result-flushed"
    received = threading.Event()
    shim = stack.root / "worker-discovery"
    shim.mkdir()
    shim.joinpath("orchestrator.py").write_text(
        "import atexit,json,sys,time\nfrom pathlib import Path\n"
        # atexit runs after the real worker prints its final JSON. Waiting there
        # exercises a parent stop with a fully flushed, genuine worker envelope.
        "def hold_after_final():\n"
        "    sys.stdout.flush()\n"
        f"    Path({str(marker)!r}).write_text('flushed')\n"
        "    print('\\x1e[legion-ev]' + json.dumps({'t': 'fixture_result_flushed'}),\n"
        "          file=sys.__stderr__, flush=True)\n"
        "    while True: time.sleep(.05)\n"
        "def run_harness(key, task, **kwargs):\n"
        f"    assert key == '__ut_terminal', key\n"
        f"    if {bool(parent_stop)!r}: atexit.register(hold_after_final)\n"
        f"    return json.loads(Path({str(payload)!r}).read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MEYO_ORCH_DIR", str(shim))

    def on_progress(event):
        if event.get("t") == "fixture_result_flushed":
            received.set()

    if parent_stop:
        def pump_ready_cpu_sample(group):
            # 此采样点在真实父端 stdout/stderr 两泵启动之后，不能在 Popen 内等。
            # 只同步“已收包”前提；不改时钟/.3s 预算，也不替代真实停止/回收。
            received.wait(5)
            return 0

        monkeypatch.setattr(stack.core, "_pgroup_cpu_seconds", pump_ready_cpu_sample)
    result = stack.core._run_harness_isolated(
        "__ut_terminal", "fixture; no model request", "__worker_json_framing",
        .3 if parent_stop == "timeout" else 10,
        model_override="fixture-model", appid_override="fixture-only",
        session_id="fixture-session", user_id="alice",
        project_context={"repo_root": str(stack.project), "user_id": "alice"},
        cancel_check=received.is_set if parent_stop == "cancelled" else None,
        progress_cb=on_progress if parent_stop else None,
    )
    if parent_stop:
        # 就绪失败也先让真实父端完成回收，断言不能把仍活着的 worker 遗留出去。
        assert marker.exists(), "real worker did not flush its result"
        assert received.is_set(), "parent pump did not receive the flushed worker result"
    return result, expected


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029", "\n", "\r\n", "\t", ""])
def test_real_worker_keeps_unicode_inside_json_strings(worker_stack, monkeypatch, separator):
    text = f"中文工具输出{separator}仍是同一条 JSON，{{不是新信封}}"
    result, expected = invoke_worker(worker_stack, monkeypatch, text)
    assert result["status"] == "ok", result
    for field, value in expected.items():
        assert result[field] == value
    assert Path(result["workdir"]).resolve() == worker_stack.project.resolve()


@pytest.mark.parametrize("parent_stop", ["cancelled", "timeout"])
@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_real_worker_stop_keeps_unicode_and_parent_authority(
        worker_stack, monkeypatch, parent_stop, separator):
    result, expected = invoke_worker(
        worker_stack, monkeypatch, f"已输出{separator}工具现场", parent_stop=parent_stop)
    assert result["status"] == parent_stop
    assert result["ok"] is False
    assert result["cancelled"] is (parent_stop == "cancelled")
    assert result["final"] == expected["final"]
    assert result["partial"] is True
    for field in ("native_checkpoint", "native_events", "resumed_native", "usage", "model_used"):
        assert result[field] == expected[field]
    assert result["native_facts"] == {
        **expected["native_facts"], "worker_error_envelope": expected["error_envelope"]}
    assert result["error_envelope"]["category"] == (
        "user_cancel" if parent_stop == "cancelled" else "runtime_budget")
    assert result["error_envelope"]["retryable"] is False
    assert Path(result["workdir"]).resolve() == worker_stack.project.resolve()


def test_fixture_start_failure_reaps_only_its_own_worker(worker_stack, monkeypatch):
    marker = worker_stack.root / "worker-result-flushed"
    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda path: False if path == marker else real_exists(path))
    real_popen = subprocess.Popen
    created = []

    def capture_worker(args, **kwargs):
        proc = real_popen(args, **kwargs)
        if len(args) > 1 and args[1] == worker_stack.core._WORKER:
            created.append(proc)
        return proc

    monkeypatch.setattr(worker_stack.core.subprocess, "Popen", capture_worker)
    with pytest.raises(AssertionError, match="real worker did not flush"):
        invoke_worker(worker_stack, monkeypatch, "fixture", parent_stop="cancelled")
    assert len(created) == 1
    assert created[0].poll() is not None
    assert created[0].stdout.closed
    assert created[0].stderr.closed
