"""Cursor agent members: binding rules and the real launcher against a stand-in SDK.

No network and no real key: the launcher script runs under the local Node with a
fixture `@cursor/sdk` that only records what it was given.
"""
import copy
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
CURSOR = "https://api.cursor.com"
KEY = "fake-member-key-cursor"

STAND_IN_SDK = r"""
const fs = require("fs");
const path = require("path");
const note = (row) => fs.appendFileSync(process.env.FIXTURE_CURSOR_LOG, JSON.stringify(row) + "\n");
class JsonlLocalAgentStore { constructor(dir) { this.dir = dir; } }
function agent(id, options, resumed) {
  return {
    agentId: id,
    async send(text) {
      note({ call: "send", text, resumed, model: options.model.id, cwd: options.local.cwd,
             sandbox: options.local.sandboxOptions.enabled,
             store_inside_state: options.local.store.dir.includes(path.join(".sessions", "cursor-agent")),
             environment_names_holding_key: Object.keys(process.env).filter((n) => process.env[n] === options.apiKey),
             key_on_command_line: process.argv.some((part) => part.includes(options.apiKey)) });
      const failing = process.env.FIXTURE_CURSOR_MODE === "run_error";
      return {
        id: "run-fixture", supports: () => true, async cancel() {},
        async *stream() {
          yield { type: "tool_call", call_id: "c1", name: "write", status: "running", args: { path: "hello.txt" } };
          fs.writeFileSync(path.join(options.local.cwd, "hello.txt"), "hi");
          yield { type: "tool_call", call_id: "c1", name: "write", status: "completed", result: { ok: true } };
          for (let i = 0; i < 200; i += 1) {
            yield { type: "assistant", message: { role: "assistant", content: [{ type: "text", text: "." }] } };
          }
        },
        async wait() {
          return failing
            ? { id: "run-fixture", status: "error", error: { message: "provider rejected " + options.apiKey, code: "run_failed" } }
            : { id: "run-fixture", status: "finished", result: "done: hello.txt",
                usage: { inputTokens: 10, outputTokens: 5, cacheReadTokens: 2, cacheWriteTokens: 0, totalTokens: 17 } };
        },
      };
    },
    async [Symbol.asyncDispose]() { note({ call: "dispose" }); },
  };
}
module.exports = {
  JsonlLocalAgentStore,
  Cursor: { models: { list: async () => [{ id: "fixture-model", displayName: "Fixture" }] } },
  Agent: {
    async create(options) { note({ call: "create" }); return agent("agent-fixture", options, false); },
    async resume(id, options) { note({ call: "resume", id }); return agent(id, options, true); },
  },
};
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SERVICE))
    for name in ("HOME", "CODEX_HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR"):
        folder = tmp_path / name.lower()
        folder.mkdir(mode=0o700)
        monkeypatch.setenv(name, str(folder))
    for name in list(os.environ):
        if (any(mark in name.upper() for mark in ("KEY", "TOKEN", "SECRET", "PASSWORD", "APPID"))
                or name in ("LEGION_MEMBER_MODEL_BINDINGS", "LEGION_CODEX_TRANSPORT",
                            "LEGION_CODEX_RESPONSES_BINDING", "LEGION_CURSOR_SANDBOX")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def runtime():
    return importlib.import_module("personal_runtime")


@pytest.fixture
def root(runtime, tmp_path):
    path = tmp_path / "instance"
    runtime.initialize(path, "cursor-test", 18081)
    return path


def candidate(runtime, root, tmp_path, name, model, base_url):
    key = tmp_path / f"{name}.key"
    key.write_text(f"fake-member-key-{name}\n")
    key.chmod(0o600)
    return runtime.save_model_profile(root, name, model, base_url, key)


def test_cursor_members_are_in_the_catalog_and_take_only_a_cursor_key():
    agents = {row["key"]: row for row in
              json.loads((SERVICE / "external_agents.json").read_text())["agents"]}
    for key in ("cursor-claude", "cursor-gpt", "cursor-gemini"):
        row = agents[key]
        command = row["transports"][0]["command"]
        assert row["credentials"]["any_of"] == ["CURSOR_API_KEY"]
        assert row["env"] == {"CURSOR_API_KEY": "{appid}"}
        # The key travels in the environment only; the task is the last argument.
        assert "{appid}" not in json.dumps(command) and command[-2:] == ["--", "{task}"]
        assert command[1] == "{adapter_dir}/cursor_member.mjs"
    assert (SERVICE / "external_adapters/cursor_member.mjs").is_file()


def test_cursor_agent_binds_only_to_a_cursor_candidate_and_reaches_the_environment(
        runtime, root, tmp_path):
    codex_key = tmp_path / "codex.key"
    codex_key.write_text("fake-member-key-codex\n")
    codex_key.chmod(0o600)
    runtime.configure_model(root, "codex-fixture", "https://codex.example.invalid/v1", codex_key)
    candidate(runtime, root, tmp_path, "cursor", "claude-fixture", CURSOR + "/")
    candidate(runtime, root, tmp_path, "other", "other-fixture", "https://other.example.invalid/v1")
    # A Cursor key is not a model endpoint, and another provider's key is not a Cursor key.
    for member, profile, protocol in (("cursor-claude", "other", "cursor-agent"),
                                      ("pi", "cursor", "responses"),
                                      ("cc", "cursor", "anthropic-messages")):
        with pytest.raises(ValueError):
            runtime.bind_member_model(root, member, profile, protocol)
    assert not (root / "secrets/member-models.json").exists()

    bound = runtime.bind_member_model(root, "cursor-claude", "cursor", "cursor-agent")
    assert bound["bound"] and bound["protocol"] == "cursor-agent" and KEY not in json.dumps(bound)
    env = runtime.environment(root, inherited={})[0]
    declared = json.loads(env[runtime.MEMBER_BINDINGS_ENV])
    assert declared["cursor-claude"]["protocol"] == "cursor-agent"
    assert KEY not in env[runtime.MEMBER_BINDINGS_ENV]
    registry = importlib.import_module("model_registry")
    resolved = registry.explicit_member_bindings(env)["cursor-claude"]
    assert resolved == {"model": "claude-fixture", "base_url": CURSOR, "api_key": KEY,
                        "protocol": "cursor-agent"}
    # A hand-edited declaration cannot point a Cursor member's key somewhere else.
    declared["cursor-claude"]["base_url"] = "https://other.example.invalid/v1"
    with pytest.raises(ValueError):
        registry.explicit_member_bindings({**env, runtime.MEMBER_BINDINGS_ENV: json.dumps(declared)})


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    if not shutil.which("node"):
        pytest.skip("Node is not installed; the Cursor launcher cannot be exercised")
    external = importlib.import_module("external_agent_registry")
    catalog = json.loads((SERVICE / "external_agents.json").read_text())["agents"]
    item = next(entry for entry in catalog if entry["key"] == "cursor-claude")
    runtimes = tmp_path / "runtimes"
    package = runtimes / "cursor-claude/node_modules/@cursor/sdk"
    package.mkdir(parents=True)
    (runtimes / "cursor-claude/package.json").write_text("{}")
    (package / "package.json").write_text(json.dumps({"name": "@cursor/sdk", "main": "index.js"}))
    (package / "index.js").write_text(STAND_IN_SDK)
    source = tmp_path / "source"
    source.mkdir()
    log = tmp_path / "sdk-calls.jsonl"
    monkeypatch.setenv("LEGION_EXTERNAL_RUNTIME_ROOT", str(runtimes))
    monkeypatch.setenv("FIXTURE_CURSOR_LOG", str(log))
    monkeypatch.setattr(external, "load_catalog", lambda: [copy.deepcopy(item)])
    monkeypatch.setattr(external, "_probe_one", lambda *a, **kw: {
        "execution_ready": True, "status": "ready", "status_detail": ""})
    monkeypatch.setattr(external, "source_dir", lambda entry: source)
    monkeypatch.setattr(external, "_deployment_paths", lambda entry: None)
    monkeypatch.setitem(sys.modules, "legion_sandbox", SimpleNamespace(
        prepare_peer_sources=lambda *a, **kw: ""))
    work = tmp_path / "work"
    work.mkdir()
    events = []

    def dispatch(task="Write hello.txt"):
        return external.run(
            "cursor-claude", task, workdir=str(work), model="claude-fixture",
            session_id="cursor-session", timeout_s=60, event_cb=events.append,
            env={"MEYO_UNIFIED_APPID": KEY, "AIGC_APPID": KEY,
                 "MEYO_UNIFIED_GATEWAY": CURSOR, "MEYO_MODEL_PROTOCOL": "cursor-agent"},
            runtime_context={"user_id": "fixture-owner"})

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()]

    return SimpleNamespace(dispatch=dispatch, calls=calls, events=events, work=work)


def test_cursor_member_does_the_task_and_never_exposes_its_key(launcher):
    result = launcher.dispatch()
    assert result["status"] == "ok", {name: result.get(name) for name in ("status", "error", "stderr")}
    assert result["final"] == "done: hello.txt"
    assert (launcher.work / "hello.txt").read_text() == "hi"
    usage = result["usage_records"][-1]
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (10, 5, 17)
    sent = next(row for row in launcher.calls() if row["call"] == "send")
    assert sent["model"] == "claude-fixture" and sent["text"] == "Write hello.txt"
    assert Path(sent["cwd"]).resolve() == launcher.work.resolve()
    assert sent["store_inside_state"] and sent["sandbox"] is False
    # The agent's own shell inherits the launcher's environment: the key is gone from it.
    assert sent["environment_names_holding_key"] == [] and sent["key_on_command_line"] is False
    assert KEY not in json.dumps(result, ensure_ascii=False, default=str)
    assert KEY not in json.dumps(launcher.events, ensure_ascii=False, default=str)
    assert [row["call"] for row in launcher.calls()] == ["create", "send", "dispose"]
    # Process display: the tool call is a step; 200 text chunks are not 200 steps.
    kinds = [(event.get("t"), event.get("tool") or event.get("kind")) for event in launcher.events]
    assert ("tool_started", "write") in kinds and ("tool_end", "write") in kinds
    assert sum(1 for kind in kinds if kind == ("harness_event", "activity")) <= 2


def test_same_session_continues_the_previous_cursor_conversation(launcher):
    assert launcher.dispatch()["status"] == "ok"
    assert launcher.dispatch("继续")["status"] == "ok"
    calls = [row for row in launcher.calls() if row["call"] in ("create", "resume", "send")]
    assert [row["call"] for row in calls] == ["create", "send", "resume", "send"]
    assert calls[2]["id"] == "agent-fixture" and calls[3]["resumed"] is True and calls[3]["text"] == "继续"


def test_a_failed_cursor_run_is_reported_as_failed_with_the_key_removed(launcher, monkeypatch):
    monkeypatch.setenv("FIXTURE_CURSOR_MODE", "run_error")
    result = launcher.dispatch()
    assert result["status"] != "ok" and not result["final"]
    assert "provider rejected [redacted]" in result["error"]
    assert KEY not in json.dumps(result, ensure_ascii=False, default=str)


def test_cursor_member_without_a_key_stops_before_loading_the_sdk(tmp_path):
    if not shutil.which("node"):
        pytest.skip("Node is not installed; the Cursor launcher cannot be exercised")
    import subprocess
    done = subprocess.run(
        [shutil.which("node"), str(SERVICE / "external_adapters/cursor_member.mjs"),
         "--runtime-dir", str(tmp_path / "absent"), "--workdir", str(tmp_path),
         "--state-dir", str(tmp_path / "state"), "--model", "fixture", "--", "task"],
        capture_output=True, text=True, timeout=30,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    assert done.returncode == 1
    assert json.loads(done.stdout)["error"]["name"] == "credentials_missing"
    assert not (tmp_path / "state").exists()
