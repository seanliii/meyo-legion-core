"""Real installed DSH + controlled local protocol; no model or native installation.

The independent probe reads only public session/turn machine events. It never
looks at assistant messages. Production adapter results must agree with those
events, not with output token counts or answer text.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_cli_runtime_channels import cli_stack, invoke, KEY
from test_adapter_final_channel import direct_stack, select_adapter


DSH = shutil.which("dsh")


@pytest.fixture
def native_dsh(cli_stack, monkeypatch, tmp_path):
    if not DSH:
        pytest.skip("DSH must already be installed; this test never installs it")
    case, registry = cli_stack, cli_stack.registry
    case.mode = "completed"
    case.answer = "controlled native text"
    case.http_calls = []
    case.process_results = []
    case.receipts = []
    case.request_seen = threading.Event()
    case.probe = tmp_path / "probe.jsonl"
    case.started = 0
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh"))
    monkeypatch.setattr(registry, "_legion_prompt", lambda question, ctx: question)
    monkeypatch.setattr(registry, "_WALL_S", 15)
    real_which = shutil.which
    monkeypatch.setattr(registry._sh, "which", lambda name:
                        DSH if name == "dsh" else None if name == "pnpm" else real_which(name))
    probe = tmp_path / "probe.mjs"
    probe.write_text("""
import { appendFileSync } from 'node:fs';
export const name = 'dsh-test-machine-probe';
export const inject = ['sessions', 'agents'];
export function apply(ctx, config) {
  appendFileSync(config.output, JSON.stringify({type: 'probe_ready'}) + '\\n');
  ctx.on('session/event', (session, event) => {
    if (event.type !== 'turn/start' && event.type !== 'turn/end') return;
    appendFileSync(config.output, JSON.stringify({
      session_id: session.id, seq: event.seq, type: event.type, cwd: session.header.cwd,
      stop_reason: event.type === 'turn/end' ? event.data.reason.kind : null,
    }) + '\\n');
    if (config.cancel && event.type === 'turn/start')
      setTimeout(() => ctx.agents.get(session.id)?.cancel({kind: 'user'}), 20);
  });
}
""")
    patch = tmp_path / "probe-patch.json"
    patch.write_text(json.dumps([{"insert": [{
        "id": "dsh-test-machine-probe", "name": str(probe),
        "config": {"workdir": str(case.project), "output": str(case.probe)},
    }]}]))
    network_guard = tmp_path / "loopback-only.mjs"
    network_guard.write_text("""
import net from 'node:net';
const connect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const options = Array.isArray(args[0]) ? args[0][0] : args[0];
  const host = typeof options === 'object' ? options.host : args[1];
  if (typeof host === 'string' && !['127.0.0.1', 'localhost', '::1'].includes(host))
    throw new Error('fixture forbids non-loopback network');
  return connect.apply(this, args);
};
""")

    class Protocol(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            # Synthetic request metadata only; never retain prompts/messages.
            case.http_calls.append({
                "message_count": len(body.get("messages") or []),
                "mode": case.mode,
            })
            case.request_seen.set()
            if case.mode == "cancelled":
                time.sleep(1)
                return
            if case.mode == "error":
                data = json.dumps({"error": {"code": "FIXTURE_REJECTED",
                                            "message": "controlled protocol rejection"}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            reason = "length" if case.mode == "max-tokens" else "stop"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for frame in [
                {"choices": [{"index": 0, "delta": {
                    "role": "assistant", "content": case.answer,
                    "reasoning_content": "PRIVATE_REASONING_SENTINEL"}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                 # Deliberately NOT a 4096/4096 signal. Native finish_reason wins.
                 "usage": {"prompt_tokens": 13, "completion_tokens": 7, "total_tokens": 20}},
            ]:
                self.wfile.write(("data: " + json.dumps(frame) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Protocol)
    httpd.daemon_threads = False
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    case.gateway = f"http://127.0.0.1:{httpd.server_port}/v1"
    native_run = registry._run_task

    def run(command, *args, **kwargs):
        command = [command[0], "--patch", str(patch), *command[1:]]
        env = kwargs.get("env") or {}
        # No inherited provider credentials, user config or installation command.
        kwargs["env"] = {
            "PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"),
            "DSH_HOME": str(tmp_path / "dsh"), "TMPDIR": str(tmp_path),
            "DSH_TELEMETRY_DISABLED": "1", "NO_PROXY": "127.0.0.1,localhost",
            "NODE_OPTIONS": "--import=" + str(network_guard),
            "DEEPSEEK_API_KEY": "controlled-fixture-key",
            "DEEPSEEK_BASE_URL": case.gateway,
        }
        for key, value in env.items():
            if key.startswith("LEGION_DSH_"):
                kwargs["env"][key] = value
        result = native_run(command, *args, **kwargs)
        for i, value in enumerate(command[:-1]):
            if value != "--patch":
                continue
            for entry in json.loads(Path(command[i + 1]).read_text()):
                for inserted in entry.get("insert") or []:
                    if inserted.get("id") != "legion-dsh-terminal":
                        continue
                    output = Path(inserted["config"]["output"])
                    if output.exists():
                        receipt = json.loads(output.read_text())
                        case.receipts.append(receipt)
                        # Replay evidence contains only the machine side channel.
                        (tmp_path / f"receipt-{len(case.receipts)}.json").write_text(json.dumps(receipt))
        case.process_results.append({
            "rc": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
        })
        return result

    monkeypatch.setattr(registry, "_run_task", run)
    case.ctx = registry.HarnessCtx(
        model="fixture-model", appid="controlled-fixture-key", gateway=case.gateway,
        max_tokens=64, workdir=str(case.project), memory_dir=str(tmp_path / "memory"),
        user_id="owner-a", session_id="conversation-a",
    )

    def call(*, checkpoint=None, user=None, conversation=None):
        case.request_seen.clear()
        case.started = time.monotonic()
        probe_config = json.loads(patch.read_text())
        probe_config[0]["insert"][0]["config"]["cancel"] = case.mode == "cancelled"
        patch.write_text(json.dumps(probe_config))
        registry.set_inproc_cancel_check(lambda: time.monotonic() - case.started > 12)
        ctx = copy.deepcopy(case.ctx)
        if checkpoint:
            ctx.task_memory = {"native_checkpoint": copy.deepcopy(checkpoint)}
        if user is not None:
            ctx.user_id = user
        if conversation is not None:
            ctx.session_id = conversation
        try:
            return registry._deepseek_adapter("__ut_dsh_request", "Reply once.", ctx=ctx)
        finally:
            registry.set_inproc_cancel_check(None)

    case.call = call
    try:
        yield case
    finally:
        registry.set_inproc_cancel_check(None)
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("mode,native_stop,rc", [
    ("completed", "completed", 0),
    ("max-tokens", "max-tokens", 1),
    ("error", "error", 1),
    ("cancelled", "aborted", 1),
])
def test_real_headless_terminal_is_projected_without_guessing_or_private_messages(
        native_dsh, mode, native_stop, rc):
    case = native_dsh
    case.mode = mode
    result = case.call()
    assert case.probe.exists(), case.process_results
    probe = [json.loads(line) for line in case.probe.read_text().splitlines()]
    ended = [row for row in probe if row["type"] == "turn/end"]
    assert ended and ended[-1]["stop_reason"] == native_stop, (probe, case.process_results)
    assert case.process_results[-1]["rc"] == rc
    facts = result.get("native_facts") or {}
    assert facts.get("dsh_stop_reason") == native_stop, result
    assert facts["process_returncode"] == rc
    assert facts["session_id"] == ended[-1]["session_id"]
    assert facts["completed"] is (mode == "completed")
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(facts)
    assert "controlled native text" not in json.dumps(facts)
    assert case.receipts
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(case.receipts)
    assert "controlled native text" not in json.dumps(case.receipts)
    if mode == "max-tokens":
        assert result["status"] == "rc1", "preserve the actual native process exit"
        assert result["error_envelope"]["category"] == "output_limit"
    if mode == "completed":
        assert result["status"] == "ok"
        assert result["native_checkpoint"]["session_id"] == ended[-1]["session_id"]


def test_resume_uses_the_same_native_session_and_only_new_turn_metadata(native_dsh):
    case = native_dsh
    case.mode = "max-tokens"
    first = case.call()
    checkpoint = first["native_checkpoint"]
    before = len(case.http_calls)
    case.mode = "completed"
    second = case.call(checkpoint=checkpoint)
    assert second["status"] == "ok" and second["resumed_native"] is True
    assert second["native_checkpoint"]["session_id"] == checkpoint["session_id"]
    assert second["native_facts"]["dsh_stop_reason"] == "completed"
    assert second["native_facts"]["terminal_seq"] > first["native_facts"]["terminal_seq"]
    assert len(case.http_calls) == before + 1
    assert case.http_calls[-1]["message_count"] > case.http_calls[0]["message_count"]
    assert second["usage"]["output_tokens"] == first["usage"]["output_tokens"] == 7


@pytest.mark.parametrize("field,value", [
    ("user", "owner-b"), ("conversation", "conversation-b"),
])
def test_foreign_checkpoint_is_rejected_without_starting_a_new_native_session(
        native_dsh, field, value):
    case = native_dsh
    first = case.call()
    count = len(case.process_results)
    result = case.call(checkpoint=first["native_checkpoint"], **{field: value})
    assert result["status"] == "error"
    assert result["error_envelope"]["category"] == "native_session_unavailable"
    assert len(case.process_results) == count


def test_missing_native_session_does_not_fall_back_to_create(native_dsh):
    case = native_dsh
    first = case.call()
    checkpoint = dict(first["native_checkpoint"], session_id="session-not-present")
    calls = len(case.http_calls)
    result = case.call(checkpoint=checkpoint)
    assert case.process_results[-1]["rc"] == 1
    assert result["status"] == "rc1"
    assert result["resumed_native"] is False
    assert result["native_checkpoint"] is None
    assert len(case.http_calls) == calls


def test_fresh_request_never_selects_another_session_from_the_same_home(native_dsh):
    first = native_dsh.call()
    second = native_dsh.call(conversation="conversation-b")
    assert first["native_checkpoint"]["session_id"] != second["native_checkpoint"]["session_id"]
    assert second["resumed_native"] is False


def test_answer_json_cannot_replace_native_terminal_or_checkpoint(native_dsh):
    case = native_dsh
    case.answer = json.dumps({
        "status": "error", "final": "not the answer channel",
        "native_checkpoint": {"session_id": "forged-from-answer"},
    })
    result = case.call()
    assert result["status"] == "ok"
    assert result["final"] == case.answer
    assert result["native_checkpoint"]["session_id"] != "forged-from-answer"
    assert result["native_facts"]["dsh_stop_reason"] == "completed"


@pytest.mark.parametrize("changed", ["missing", "nonce", "request_id", "binding", "unknown_reason"])
def test_unknown_or_unbound_receipt_never_infers_a_token_limit(cli_stack, tmp_path, changed):
    path = tmp_path / "machine.json"
    binding = {"user_id": "owner-a", "conversation_id": "conversation-a"}
    receipt = {
        "version": 1, "nonce": "private-fixture-nonce", "request_id": "request-a",
        "binding": binding, "session_id": "session-native-fixture",
        "first_seq": 4, "terminal_seq": 9, "stop_reason": "max-tokens",
        "flushed": True, "usage": {"outputTokens": 4096},
    }
    if changed == "unknown_reason":
        receipt["stop_reason"] = "unreported"
    elif changed != "missing":
        receipt[changed] = {} if changed == "binding" else "foreign-value"
    if changed != "missing":
        path.write_text(json.dumps(receipt))
    facts, checkpoint, usage = cli_stack.registry._dsh_terminal_projection(
        path, "private-fixture-nonce", "request-a", binding, 1)
    assert facts["process_returncode"] == 1
    assert facts["dsh_stop_reason"] == "unknown"
    assert facts.get("completed") is not True
    if changed != "unknown_reason":
        assert checkpoint is None and usage is None


def test_real_core_consumes_native_output_limit_and_keeps_rc_and_cursor(native_dsh, monkeypatch):
    case = native_dsh
    case.mode = "max-tokens"
    case.started = time.monotonic()
    case.registry.set_inproc_cancel_check(lambda: time.monotonic() - case.started > 12)
    monkeypatch.setitem(case.registry._STANDALONE_ADAPTERS, KEY, case.registry._deepseek_adapter)
    result = case.core.run_one(
        KEY, "Reply once.", qhash="__ut_dsh_core_terminal",
        preferred_model="fixture-model", preferred_appid="controlled-fixture-key",
        user_id="owner-a", conversation_id="conversation-a", execution_depth="quick",
        task_facts={"project_context": {"repo_root": str(case.project), "user_id": "owner-a"}},
    )
    assert result["status"] == "error"
    assert result["error_envelope"]["category"] == "output_limit"
    assert result["native_facts"]["process_returncode"] == 1
    assert result["native_facts"]["dsh_stop_reason"] == "max-tokens"
    assert result["native_checkpoint"]["session_id"] == result["native_facts"]["session_id"]


def test_timeout_without_a_native_receipt_does_not_invent_a_session(cli_stack, monkeypatch):
    registry = cli_stack.registry
    monkeypatch.setattr(registry._sh, "which",
                        lambda name: "/offline/dsh" if name == "dsh" else None)
    monkeypatch.setattr(registry, "_legion_prompt", lambda question, ctx: question)

    def timeout(command, *args, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(registry, "_run_task", timeout)
    result = registry._deepseek_adapter(
        "request-is-not-a-native-uuid", "Reply once.", ctx=registry.HarnessCtx(
            workdir=str(cli_stack.project), session_id="logical-conversation-only"))
    assert result["status"] == "timeout"
    assert not result.get("native_checkpoint")


@pytest.mark.parametrize("observation", ["missing", "invalid", "unknown_reason", "blocked"])
def test_unknown_observation_does_not_demote_native_success_in_real_core(
        direct_stack, monkeypatch, tmp_path, observation):
    case, registry = direct_stack, direct_stack.registry
    # Existing CompletedProcess fixture only: neither this file nor Node runs.
    binary = tmp_path / "offline-dsh"
    binary.write_text("offline native fixture; never executed\n")
    monkeypatch.setattr(registry._sh, "which",
                        lambda name: str(binary) if name == "dsh" else None)
    native = registry._run_task

    def receipt(command, *args, **kwargs):
        result = native(command, *args, **kwargs)
        if observation != "missing":
            patch = json.loads(Path(command[command.index("--patch") + 1]).read_text())
            config = next(entry["config"] for row in patch for entry in row.get("insert", [])
                          if entry.get("id") == "legion-dsh-terminal")
            Path(config["output"]).write_text(json.dumps({
                "version": 1, "nonce": "foreign-nonce" if observation == "invalid" else config["nonce"],
                "request_id": config["requestId"], "binding": config["binding"],
                "session_id": "session-native-fixture", "first_seq": 4, "terminal_seq": 9,
                "stop_reason": "blocked" if observation == "blocked" else "future-native-reason",
                "flushed": True, "usage": {"outputTokens": 7},
            }))
        return result

    monkeypatch.setattr(registry, "_run_task", receipt)
    select_adapter(case, monkeypatch, "deepseek")
    result = invoke(case)  # Actual run_one -> actual DSH adapter -> existing native stub.
    packet = case.packets[0]
    assert case.native_calls == ["deepseek"]
    assert packet["final"] == result["final"] == case.answer
    assert packet["native_facts"]["process_returncode"] == 0
    if observation == "blocked":
        assert packet["native_facts"]["dsh_stop_reason"] == "blocked"
        assert packet["status"] == "incomplete"
        assert result["status"] != "done", "an explicit noncompletion fact still wins"
    else:
        assert result["status"] == "done", {
            "core_status": result["status"], "adapter_status": packet["status"],
            "native_facts": packet["native_facts"],
        }
        assert packet["status"] == "ok"
        assert packet["native_facts"]["dsh_stop_reason"] == "unknown"
        assert result["native_facts"]["dsh_stop_reason"] == "unknown"
        assert result["native_facts"].get("completed") is not True


def test_real_collector_timeout_stdout_reaches_dsh_final(cli_stack, monkeypatch, tmp_path):
    registry = cli_stack.registry
    command = [sys.executable, "-u", "-c",
               "import sys,time; print('SAFE_DSH_FINAL'); "
               "print('SAFE_DSH_DIAGNOSTIC', file=sys.stderr); time.sleep(10)"]
    with pytest.raises(subprocess.TimeoutExpired) as captured:
        registry._run_live(command, .3, tag="__ut_dsh_timeout_capture")
    error = captured.value
    assert error.legion_stdout == "SAFE_DSH_FINAL\n"
    assert error.legion_stderr == "SAFE_DSH_DIAGNOSTIC\n"
    assert error.output is None
    monkeypatch.setattr(registry._sh, "which",
                        lambda name: "/offline/dsh" if name == "dsh" else None)
    monkeypatch.setattr(registry, "_legion_prompt", lambda question, ctx: question)

    def timeout(*args, **kwargs):
        raise error

    monkeypatch.setattr(registry, "_run_task", timeout)
    result = registry._deepseek_adapter("collector-replay", "Reply once.",
                                       ctx=registry.HarnessCtx(workdir=str(cli_stack.project)))
    assert result["status"] == "timeout"
    assert result["final"] == "SAFE_DSH_FINAL"
    assert result["stderr"] == "SAFE_DSH_DIAGNOSTIC\n"
    assert not result.get("native_checkpoint")
    (tmp_path / "collector-replay.json").write_text(json.dumps({
        "legion_stdout": error.legion_stdout, "legion_stderr": error.legion_stderr,
        "standard_output": error.output, "result": result,
    }))


@pytest.mark.parametrize("protocol", ["plain", "legacy_json", "legacy_partial"])
@pytest.mark.parametrize("runtime_reason", [None, "runtime_budget"])
def test_timeout_preserves_declared_final_usage_and_runtime_failure(
        cli_stack, monkeypatch, tmp_path, protocol, runtime_reason):
    registry = cli_stack.registry
    monkeypatch.setattr(registry, "_legion_prompt", lambda question, ctx: question)
    binary = tmp_path / "offline-dsh"
    binary.write_text("not executed\n")
    monkeypatch.setattr(registry._sh, "which", lambda name:
                        str(binary) if name == "dsh" else
                        "/offline/pnpm" if name == "pnpm" and protocol.startswith("legacy") else None)
    expected_checkpoint = {"kind": "deepseek_session", "session_id": "session-legacy-fixture"}
    if protocol.startswith("legacy"):
        # Select the existing source-bridge branch, but never execute this stub.
        registry_path = tmp_path / "legacy/meyo-orchestrator/harness_registry.py"
        bridge = tmp_path / "legacy/deepseek-harness/examples/headless-agent/legion-resume.ts"
        bridge.parent.mkdir(parents=True)
        bridge.write_text("// not executed\n")
        registry_path.parent.mkdir(parents=True)
        monkeypatch.setattr(registry, "__file__", str(registry_path))

    def timeout(command, *args, **kwargs):
        if protocol == "plain":
            text = 'SAFE_TEXT {"native_checkpoint":"literal answer field"}'
            patches = json.loads(Path(command[command.index("--patch") + 1]).read_text())
            config = next(entry["config"] for row in patches for entry in row.get("insert", [])
                          if entry.get("id") == "legion-dsh-terminal")
            Path(config["output"]).write_text(json.dumps({
                "version": 1, "nonce": config["nonce"], "request_id": config["requestId"],
                "binding": config["binding"], "session_id": "session-native-fixture",
                "first_seq": 1, "terminal_seq": 3, "stop_reason": "completed", "flushed": True,
                "usage": {"inputTokens": 13, "outputTokens": 7},
            }))
        else:
            text = "PRIVATE_LOG_NOT_A_FINAL\n" + json.dumps({
                "status": "ok", "final": "SAFE_BRIDGE_FINAL",
                "usage": {"input_tokens": 13, "output_tokens": 7, "total_tokens": 20},
                "native_checkpoint": expected_checkpoint,
                "resumed_native": True, "model_used": "fixture-model",
            })
            if protocol == "legacy_partial":
                text = text[:-1]  # An incomplete JSON envelope is not a final channel.
        error = subprocess.TimeoutExpired(command, 1, output=b"stale standard output")
        error.legion_stdout = text + "\n"
        error.legion_stderr = "SAFE_DSH_DIAGNOSTIC\n"
        if runtime_reason:
            error.runtime_reason = runtime_reason
        raise error

    monkeypatch.setattr(registry, "_run_task", timeout)
    result = registry._deepseek_adapter(
        "timeout-replay", "Reply once.", ctx=registry.HarnessCtx(
            model="fixture-model", workdir=str(cli_stack.project),
            user_id="owner-a", session_id="conversation-a"))
    assert result["status"] == "timeout"
    assert result["final"] == (
        'SAFE_TEXT {"native_checkpoint":"literal answer field"}'
        if protocol == "plain" else "SAFE_BRIDGE_FINAL" if protocol == "legacy_json" else "")
    assert "PRIVATE_LOG_NOT_A_FINAL" not in result["final"]
    assert result["stderr"] == "SAFE_DSH_DIAGNOSTIC\n"
    if protocol == "legacy_partial":
        assert not result.get("usage") and not result.get("native_checkpoint")
    else:
        assert result["usage"]["total_tokens"] == 20
        assert result["native_checkpoint"]["session_id"] == (
            "session-native-fixture" if protocol == "plain" else expected_checkpoint["session_id"])
    if runtime_reason:
        assert result["fail_kind"] == runtime_reason
        assert result["error_envelope"]["category"] == runtime_reason
        assert result["error_envelope"]["retryable"] is False
    else:
        assert not result.get("error_envelope")
    (tmp_path / "timeout-replay.json").write_text(json.dumps({
        "protocol": protocol, "runtime_reason": runtime_reason, "result": result,
    }))


def test_bridge_publishes_known_usage_before_terminal_without_claiming_flush(cli_stack, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("offline JavaScript callback replay needs an existing Node, never installs it")
    bridge = Path(cli_stack.registry.__file__).with_name("dsh_terminal_bridge.mjs")
    script = tmp_path / "replay-machine-events.mjs"
    script.write_text(r"""
import { readFileSync } from 'node:fs';
const source = readFileSync(process.argv[2], 'utf8');
const config = JSON.parse(process.argv[3]);
const initStart = source.indexOf('const record = ') + 'const record = '.length;
const initEnd = source.indexOf('\n  };', initStart) + '\n  }'.length;
const record = new Function('config', 'return ' + source.slice(initStart, initEnd))(config);
const begin = source.indexOf('agentCtx.on("session/event", ') + 'agentCtx.on("session/event", '.length;
const end = source.indexOf('\n        });', begin) + '\n        }'.length;
const published = [];
const publish = () => published.push(structuredClone(record));
const callback = new Function('record', 'publish', 'expected', 'config',
  'return ' + source.slice(begin, end))(record, publish, 'session-machine-fixture', config);
const session = {id: 'session-machine-fixture', header: {cwd: config.binding.workdir}};
callback(session, {type: 'turn/start', seq: 1, data: {turn: 1}});
const data = {turn: 1, usage: {inputTokens: 13, outputTokens: 7}};
Object.defineProperty(data, 'message', {get() {throw new Error('private message accessed');}});
callback(session, {type: 'assistant/message', seq: 2, data});
const beforeTerminal = structuredClone(published.at(-1));
callback({id: 'another-session', header: session.header},
  {type: 'assistant/message', seq: 2, data: {turn: 1, usage: {outputTokens: 1000}}});
const afterForeign = structuredClone(published.at(-1));
callback(session, {type: 'turn/end', seq: 3, data: {turn: 1, reason: {kind: 'completed'}}});
process.stdout.write(JSON.stringify({beforeTerminal, afterForeign, afterTerminal: published.at(-1)}));
""")
    config = {
        "nonce": "machine-fixture-nonce", "requestId": "request-machine-fixture",
        "binding": {"user_id": "owner-a", "conversation_id": "conversation-a",
                    "workdir": str(cli_stack.project)},
    }
    # One bounded pure-JS callback replay; not a DSH runtime, HTTP server or model.
    result = cli_stack.registry._run_live(
        [node, str(script), str(bridge), json.dumps(config)], 3,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        tag="__ut_dsh_usage_callback")
    assert result.returncode == 0, result.stderr
    replay = json.loads(result.stdout)
    before = replay["beforeTerminal"]
    assert before["usage"] == {"inputTokens": 13, "outputTokens": 7}
    assert before["flushed"] is False and before["terminal_seq"] is None
    assert before["stop_reason"] == "unknown"
    assert replay["afterForeign"] == before
    assert replay["afterTerminal"]["flushed"] is False, "turn/end is not a persistence barrier"
    path = tmp_path / "interrupted-machine-receipt.json"
    path.write_text(json.dumps(before))
    facts, checkpoint, usage = cli_stack.registry._dsh_terminal_projection(
        path, config["nonce"], config["requestId"], config["binding"], None)
    assert facts["dsh_stop_reason"] == "unknown" and checkpoint is None
    assert usage["input_tokens"] == 13 and usage["output_tokens"] == 7
    assert usage["total_tokens"] == 20
    from token_telemetry import normalize
    counted = normalize(usage)
    assert counted["complete"] is False and counted["coverage"] == "partial"
    (tmp_path / "usage-publication-replay.json").write_text(json.dumps({
        **replay, "projected_usage": usage, "normalized_usage": counted,
        "native_facts": facts, "checkpoint": checkpoint,
    }))


@pytest.mark.parametrize("cancel_after_native", [False, True])
def test_core_retains_timeout_content_without_promoting_budget_or_cancel(
        direct_stack, monkeypatch, cancel_after_native):
    case, registry = direct_stack, direct_stack.registry
    cancelled = False

    def timeout(command, *args, **kwargs):
        nonlocal cancelled
        cancelled = cancel_after_native
        error = subprocess.TimeoutExpired(command, 1)
        error.legion_stdout = "SAFE_DSH_PARTIAL\n"
        error.legion_stderr = "SAFE_DSH_DIAGNOSTIC\n"
        error.runtime_reason = "runtime_budget"
        raise error

    monkeypatch.setattr(registry, "_run_task", timeout)
    select_adapter(case, monkeypatch, "deepseek")
    result = case.core.run_one(
        KEY, "Reply once.", qhash="__ut_dsh_timeout_core",
        preferred_model="offline-model", preferred_appid=case.appid,
        user_id="offline-owner", conversation_id="offline-conversation",
        execution_depth="quick", cancel_check=lambda: cancelled,
        task_facts={"project_context": {"repo_root": str(case.project), "user_id": "offline-owner"}},
    )
    assert case.packets[0]["status"] == "timeout"
    assert result["status"] != "done"
    assert result["final"] == "SAFE_DSH_PARTIAL"
    assert result["error_envelope"]["category"] == "runtime_budget"
    if cancel_after_native:
        assert result["status"] == "cancelled"
