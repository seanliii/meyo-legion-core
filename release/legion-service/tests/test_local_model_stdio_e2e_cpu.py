"""Real shared MCP entry -> trusted host -> CPU-only wrapper, never a model.

Reuse the protocol/process helpers, not their substitute CapabilitySession.
The server below imports its real from_environment in an isolated interpreter.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
from types import SimpleNamespace

import pytest

from test_local_model_capability_host import (
    FAKE_WRAPPER, IDS, ORCH, PREFIX, SERVICE,
    _pin_isolation_env, coding_params, local,
)
from test_shared_stdio_media_lifecycle_cpu import _CPUCase, _RPC, _alive


class _RealSharedCase(_CPUCase):
    def __init__(self, binding):
        environment = {
            **os.environ, "LEGION_EXT_AUTO_UPDATE": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.spec = binding.module.native_stdio_spec(
            SimpleNamespace(**binding.context, execution_depth="standard"),
            environment, ORCH / "meyo_mcp_servers/orchestrator_server.py",
        )
        env = {
            **self.spec["env"],
            # Synthetic secrets deliberately test the real host's sanitization.
            "OPENAI_API_KEY": "CPU_SECRET_MUST_NOT_REACH_WRAPPER",
            "PYTHONPATH": "/CPU_SECRET_MUST_NOT_REACH_WRAPPER",
        }
        super().__init__(binding.script.parent, env)

    def spawn(self, mode):
        assert mode == "stdio-shared"
        log = (self.root / "real-host.stderr").open("w+")
        self.logs.append(log)
        proc = subprocess.Popen(
            [self.spec["command"], *self.spec["args"]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True,
            start_new_session=True, env=self.env,
        )
        self.processes.append(proc)
        assert self.registry.register(
            proc.pid, owner=os.getpid(), comm=self.spec["command"],
            meta={"kind": "__ut_real_shared_stdio_launcher"},
        )
        return proc


@pytest.fixture
def real_host(local):
    cases = []

    def start():
        case = _RealSharedCase(local)
        cases.append(case)
        return _RPC(case)

    try:
        yield start
    finally:
        for case in cases:
            case.cleanup()


def _call(rpc, request_id, action, params):
    rpc.call(request_id, "call_capability", {
        "tool_id": PREFIX + action, "params_json": json.dumps(params),
    })


def _payload(response):
    result = response["result"]
    assert len(result["content"]) == 1
    return result, json.loads(result["content"][0]["text"])


def _assert_only_host_registered(rpc):
    entries = [
        json.loads(path.read_text())
        for path in Path(rpc.cpu.env["LEGION_PROC_REGISTRY_DIR"]).glob("*.ns-*.json")
    ]
    assert len(entries) == 1, entries
    assert entries[0]["pid"] == rpc.proc.pid
    assert entries[0]["meta"]["kind"] == "native-shared-capabilities"


def _lifecycle_wrapper(local):
    # Reuse the existing real CPU wrapper/independent child cleanup fixture.
    # Only the wrapper is a fixture: the MCP server and host stay unmodified.
    helper = SERVICE / "tests/test_shared_stdio_media_lifecycle_cpu.py"
    local.script.write_text(
        "import runpy, sys\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "Path(args[args.index('--output') + 1]).write_text('partial CPU fixture')\n"
        "try:\n"
        f"    runpy.run_path({str(helper)!r}, run_name='__cpu_fixture__')"
        "['_cpu_wrapper'](Path(__file__).parent)\n"
        "finally:\n"
        "    print('CPU_ONLY_WRAPPER_CLEANED', file=sys.stderr, flush=True)\n"
    )


def test_real_shared_entry_lists_only_granted_tools_and_cpu_doctors(local, real_host):
    rpc = real_host()
    assert rpc.initialized["result"]["serverInfo"]["name"] == "meyo-capabilities"
    rpc.send({"id": "tools", "method": "tools/list"})
    tools = rpc.receive("tools")["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"list_capabilities", "call_capability"}
    rpc.call("actions", "list_capabilities")
    _, actions = _payload(rpc.receive("actions"))
    assert {action["id"] for action in actions["actions"]} == set(IDS)
    assert actions["readiness"] == "not_probed"
    assert actions["workdir"] == str(local.work)
    for kind in ("coding", "shape"):
        _call(rpc, kind, "preflight", {"kind": kind})
        result, payload = _payload(rpc.receive(kind))
        assert not result.get("isError")
        assert payload["ok"] is True and payload["generation_tested"] is False
        assert payload["observation"]["cpu_fixture"] is True
        assert payload["observation"]["gpu_tested"] is False
    assert not list(local.work.iterdir())
    _assert_only_host_registered(rpc)
    rpc.close()


@pytest.mark.parametrize("action", ["coding_text", "shape_3d"])
def test_real_shared_entry_publishes_readback_and_bound_receipt(local, real_host, action):
    local.script.write_text(FAKE_WRAPPER.replace(
        "args = sys.argv[1:]",
        "args = sys.argv[1:]\nPath(__file__).with_suffix('.pid').write_text(str(os.getpid()))",
    ))
    if action == "coding_text":
        params = coding_params(local)
        source = Path(params["prompt_file"])
        source.write_text("CPU prompt file, not argv or game code.\n" * 6000)
    else:
        source = local.work / "input.png"
        source.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
        params = {"image_file": str(source),
                  "output_file": str(local.work / "shape.glb"), "seed": 42}
    rpc = real_host()
    _call(rpc, "generate", action, params)
    result, payload = _payload(rpc.receive("generate", timeout=5))
    assert payload["ok"] is True and not result.get("isError"), result
    assert not _alive(int(local.script.with_suffix(".pid").read_text()))
    raw = Path(payload["path"]).read_bytes()
    receipt = json.loads(Path(payload["receipt_path"]).read_text())
    assert hashlib.sha256(raw).hexdigest() == payload["sha256"] == receipt["output"]["sha256"]
    assert receipt["input"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert receipt["user_id"] == "alice"
    assert receipt["session_id"] == payload["session_id"] == "same-original-session"
    wrapper = receipt["wrapper_receipt"]
    assert wrapper["cpu_fixture"] is True and wrapper["has_secret"] is False
    assert wrapper["seen_env"]["HOME"] == local.config["home"]
    assert wrapper["seen_env"]["MEYO_WORKDIR"] == str(local.work)
    assert wrapper["seen_env"]["LOCAL_MEDIA_LEGION_SERVICE_DIR"] == str(SERVICE)
    assert payload["quality_review"] == receipt["quality_review"] == "not_performed"
    if action == "coding_text":
        assert raw == source.read_bytes()
    else:
        assert raw[:4] == b"glTF"
    _assert_only_host_registered(rpc)
    rpc.close()


def test_real_shared_entry_returns_failure_evidence_not_empty_error(local, real_host):
    local.script.write_text(
        "import sys\n"
        "sys.stderr.write('x' * 10000 + '\\nCPU_ONLY_MISSING_RUNTIME\\n')\n"
        "raise SystemExit(7)\n"
    )
    params = coding_params(local)
    rpc = real_host()
    _call(rpc, "fail", "coding_text", params)
    result, payload = _payload(rpc.receive("fail", timeout=5))
    assert result["isError"] is True
    assert payload["ok"] is False and payload["returncode"] == 7
    context = payload["failure_context"]
    assert context["stderr_tail"].endswith("CPU_ONLY_MISSING_RUNTIME\n")
    assert len(context["stderr_tail"]) <= 8192 and context["stderr_truncated"] is True
    assert Path(context["staging_dir"]).is_dir()
    assert "CPU_SECRET_MUST_NOT_REACH_WRAPPER" not in json.dumps(result)
    assert not Path(params["output_file"]).exists()
    _assert_only_host_registered(rpc)
    rpc.close()


@pytest.mark.parametrize("stop", ["cancel", "timeout", "eof", "term"])
def test_real_shared_entry_reaps_before_terminal_result(local, real_host, stop):
    _lifecycle_wrapper(local)
    if stop == "timeout":
        local.monkeypatch.setenv("MEYO_LOCAL_MODEL_CONFIG", json.dumps({
            **local.config, "timeout_s": 2,
        }))
    params = coding_params(local)
    rpc = real_host()
    _call(rpc, "owned-call", "coding_text", params)
    pids = rpc.cpu.ready()
    # Cancellation/list traffic must be consumed while the real host is busy.
    rpc.cancel("different-call")
    rpc.send({"id": "during", "method": "tools/list"})
    assert rpc.receive("during")["result"]["tools"]
    assert _alive(pids["wrapper"]) and _alive(pids["leaf"])
    assert not (rpc.cpu.root / "wrapper-cleaned").exists()
    shutdown_deadline = time.monotonic() + 5 if stop in {"eof", "term"} else None
    if stop == "cancel":
        rpc.cancel("owned-call")
    elif stop == "eof":
        rpc.proc.stdin.close()
    elif stop == "term":
        os.kill(rpc.proc.pid, signal.SIGTERM)
    # Drain the terminal reply before waiting: a full stdout pipe would otherwise
    # block the worker's flush and the host's join even after its children are reaped.
    # Reading and process exit share the original five-second shutdown budget.
    reply_timeout = (max(0, shutdown_deadline - time.monotonic())
                     if shutdown_deadline is not None else 5)
    response = rpc.receive("owned-call", timeout=reply_timeout)
    if shutdown_deadline is not None:
        assert rpc.proc.wait(timeout=max(0, shutdown_deadline - time.monotonic())) == 0
    result, payload = _payload(response)
    assert result["isError"] is True and payload["ok"] is False
    assert payload["status"] == ("timeout" if stop == "timeout" else "cancelled")
    assert "CPU_ONLY_WRAPPER_CLEANED" in payload["failure_context"]["stderr_tail"]
    assert Path(payload["failure_context"]["staging_dir"]).is_dir()
    assert not Path(params["output_file"]).exists()
    assert not Path(params["output_file"] + ".receipt.json").exists()
    rpc.cpu.assert_cleaned()
    if stop in {"eof", "term"}:
        assert not list(Path(local.config["registry_dir"]).glob("*.ns-*.json"))
    else:
        _assert_only_host_registered(rpc)
        rpc.close()
