"""Actual native send boundaries, using existing SDK fixture and local CLI receiver."""
from __future__ import annotations

import builtins
import copy
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from test_native_terminal_boundaries import native_stack as existing_native_stack

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
SOURCE_URL = "https://example.invalid/owned-research-source"
QUESTION = "Continue the exact original objective; no new research request."


def prepare_native_payload_stack(root, monkeypatch):
    """Reuse native layout/isolation setup, but observe the real prompt builder."""
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.syspath_prepend(str(ORCH))
    monkeypatch.syspath_prepend(str(SERVICE))
    core = importlib.import_module("legion_core")
    registry = importlib.import_module("harness_registry")
    stable_prelude, legion_prompt = core._stable_prelude, registry._legion_prompt
    media_prelude = core._media_prelude
    legion_cli, media_cli = registry._legion_tool_path(), registry._media_cli_path()
    assert Path(legion_cli).is_file() and Path(media_cli).is_file()
    stack = existing_native_stack.__wrapped__(root, monkeypatch)
    # Existing terminal-result tests intentionally replace these with empty/identity
    # functions. Payload tests must keep the actual current product builders.
    monkeypatch.setattr(core, "_stable_prelude", stable_prelude)
    monkeypatch.setattr(registry, "_legion_prompt", legion_prompt)
    # The SDK fixture relocates registry.__file__ to select its isolated Hermes
    # layout. Preserve the real CLI locations, not that synthetic layout's
    # relative-name fallback, when observing the native prompt.
    monkeypatch.setattr(registry, "_legion_tool_path", lambda: legion_cli)
    monkeypatch.setattr(registry, "_media_cli_path", lambda: media_cli)
    monkeypatch.setattr(core, "_toolbox_hint", lambda: "BASE_TOOLBOX_SENTINEL")
    monkeypatch.setattr(core, "_delivery_context", lambda: "")
    monkeypatch.setattr(core, "_desktop_context", lambda: "")
    monkeypatch.setattr(core, "_media_prelude", lambda *a, **kw: "")
    monkeypatch.setattr(core, "_recall_experience", lambda *a, **kw: "")
    import delivery_knowledge
    import legion_sandbox
    monkeypatch.setattr(delivery_knowledge, "match_context", lambda *a, **kw: "")
    monkeypatch.setattr(legion_sandbox, "prepare_peer_sources", lambda *a, **kw: "")

    def sdk_process(cmd, timeout, **kwargs):
        # Execute the real adapter-generated Python, accepting Python options by
        # meaning rather than repairing the separate legacy len(cmd)==3 fixture.
        assert kwargs.get("tag") == "hermes" and "-c" in cmd
        code_index = cmd.index("-c")
        assert code_index + 1 == len(cmd) - 1
        record = {"transport": "hermes_sdk", "cwd": kwargs["cwd"],
                  "python_options": cmd[1:code_index],
                  "inherited_stable_context": os.environ.get("MEYO_STABLE_CONTEXT")}

        class Agent:
            def __init__(self, **settings):
                self.session_id = settings.get("session_id") or "native-payload"
                self.session_input_tokens = self.session_output_tokens = 0
                self.session_total_tokens = self.session_api_calls = 0
                record["user_id"] = settings.get("user_id")
                record["session_id"] = self.session_id

            def run_conversation(self, task, conversation_history):
                record.update(task=task, history=copy.deepcopy(conversation_history))
                capture = Path(os.environ["C_NATIVE_PAYLOAD_CAPTURE"])
                assert not capture.exists(), "One test invocation must cause only one native send"
                capture.write_text(json.dumps(record))
                return {"final_response": "NATIVE_PAYLOAD_OK", "completed": True,
                        "failed": False, "interrupted": False, "model": "fixture-model",
                        "turn_exit_reason": "text_response(1)"}

        class DB:
            def get_messages_as_conversation(self, sid, **options):
                return [{"role": "user", "content": "original native history"}]

        real_import = builtins.__import__
        modules = {
            "run_agent": SimpleNamespace(AIAgent=Agent),
            "hermes_state": SimpleNamespace(SessionDB=DB),
            "os": SimpleNamespace(environ=dict(kwargs["env"])),
            "sys": SimpleNamespace(path=[], maxsize=sys.maxsize),
        }
        output = []

        def importer(name, *args, **kw):
            return modules[name] if name in modules else real_import(name, *args, **kw)

        exec(compile(cmd[code_index + 1], "<actual-native-payload>", "exec"), {
            "__builtins__": {**vars(builtins), "__import__": importer,
                             "print": lambda value, **kw: output.append(value)},
        })
        return SimpleNamespace(returncode=0, stderr="", stdout="\n".join(map(str, output)))

    monkeypatch.setattr(registry, "_run_live", sdk_process)
    stack.real_media_prelude = media_prelude
    return stack


@pytest.fixture
def send_stack(tmp_path, monkeypatch):
    for name in ("HOME", "MEYO_LEGION_DIR", "LEGION_HOME_DIR", "LEGION_SESSIONS_DIR",
                 "LEGION_TEAM_DIR", "LEGION_TASK_MEMORY_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name))
    capture = tmp_path / "actual-native-payload.json"
    monkeypatch.setenv("C_NATIVE_PAYLOAD_CAPTURE", str(capture))
    stack = prepare_native_payload_stack(tmp_path / "native-fixture", monkeypatch)
    stack.capture = capture
    return stack


def project(stack):
    qc = {
        "schema_version": 1, "revision": 3, "original_query": QUESTION,
        "sources": [{"source_id": "S-one", "url": SOURCE_URL, "visited_at": 1789590000,
                     "content_sha256": "a" * 64, "fetch_status": "retrieved"}],
        "context_ref": {"path": str(stack.root / "canonical-run.json"), "field": "quality_context"},
    }
    (stack.root / "canonical-run.json").write_text(json.dumps({"quality_context": qc}))
    return {"objective": QUESTION, "user_id": "alice", "conversation_id": "payload-session",
            "workspace_root": str(stack.project), "quality_context": qc}


def configure_external_receiver(stack, monkeypatch):
    """Real external registry/transport launches a local CLI that records its argv."""
    import external_agent_registry as external

    receiver = stack.root / "receive-native-argv.py"
    receiver.write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        "Path(os.environ['C_NATIVE_PAYLOAD_CAPTURE']).write_text(json.dumps({"
        "'transport':'external_cli','task':sys.argv[1],"
        "'user_id':os.environ.get('MEYO_USER_ID'),"
        "'session_id':os.environ.get('MEYO_SESSION_ID'),'cwd':os.getcwd()}))\n"
        "print('NATIVE_PAYLOAD_OK')\n")
    item = {"key": "__ut_external_payload", "transports": [{
        "type": "cli", "command": [sys.executable, str(receiver), "{task}"],
        "final_nonempty_line": True,
    }]}
    monkeypatch.setattr(external, "load_catalog", lambda: [item])
    monkeypatch.setattr(external, "_probe_one", lambda *a, **kw: {"execution_ready": True})
    monkeypatch.setattr(external, "_node_bin_dirs", lambda *a: [])
    # Only installation layout is synthetic, as in the existing native fixture.
    # Task text, registry.run, template rendering and the real Popen stay intact.
    monkeypatch.setattr(external, "_context", lambda item, cwd, values: {
        "runtime_dir": str(stack.root / "external-runtime"), "workdir": cwd, **values,
    })
    return item["key"]


def configure_isolated_worker(stack, monkeypatch):
    """Actual harness_worker/env/registry/adapter; only discovery and SDK are fixtures."""
    shim = stack.root / "worker-discovery"
    shim.mkdir()
    shim.joinpath("orchestrator.py").write_text(
        "import importlib.util,sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parent)!r})\n"
        "import pytest\n"
        "from test_research_native_send_payload import prepare_native_payload_stack\n"
        "_patches=pytest.MonkeyPatch()\n"
        f"_stack=prepare_native_payload_stack(Path({str(stack.root / 'child-fixture')!r}),_patches)\n"
        f"_spec=importlib.util.spec_from_file_location('_real_payload_orchestrator',{str(ORCH / 'orchestrator.py')!r})\n"
        "_real=importlib.util.module_from_spec(_spec)\n"
        "_spec.loader.exec_module(_real)\n"
        "run_harness=_real.run_harness\n")
    monkeypatch.setenv("MEYO_ORCH_DIR", str(shim))


@pytest.mark.parametrize("path", ["core_inproc", "external_inproc", "isolate"])
@pytest.mark.parametrize("depth", ["quick", "standard", "deep"])
def test_research_and_existing_qc_at_actual_native_send(send_stack, monkeypatch, path, depth):
    stack = send_stack
    packet = project(stack)
    plan = {"quality_context": packet["quality_context"], "project_context": packet,
            "tasks": {"member": QUESTION}}
    plan_before = copy.deepcopy(plan)
    full_task = QUESTION + stack.core._build_prelude(QUESTION, plan, user_id="alice")
    prefix = stack.core._stable_prelude(depth)
    options = {
        "model_override": "fixture-model", "appid_override": "fixture-only",
        "session_id": "payload-session", "user_id": "alice", "project_context": packet,
        "task_facts": {"goal": QUESTION}, "execution_depth": depth,
    }
    if path == "core_inproc":
        result = stack.core._run_core_inprocess("__ut_terminal", full_task, "capture-core",
                                                timeout_s=20, **options)
    elif path == "external_inproc":
        key = configure_external_receiver(stack, monkeypatch)
        # An explicit positive timeout exercises inproc, not its isolate fallback.
        result = stack.core._run_external_inprocess(key, full_task, "capture-external",
                                                    timeout_s=20, **options)
    else:
        configure_isolated_worker(stack, monkeypatch)
        result = stack.core._run_harness_isolated("__ut_terminal", full_task, "capture-isolate",
                                                  timeout_s=20, **options)
    assert stack.capture.exists(), {
        "path": path, "depth": depth, "status": result.get("status"), "error": result.get("error"),
    }
    actual = json.loads(stack.capture.read_text())
    sent = actual["task"]
    assert result["final"] == "NATIVE_PAYLOAD_OK", {
        key: result.get(key) for key in ("status", "error", "final")}
    assert actual["transport"] == ("external_cli" if path == "external_inproc" else "hermes_sdk")
    assert actual["user_id"] == "alice" and actual["session_id"] == "payload-session"
    assert Path(actual["cwd"]).resolve() == stack.project.resolve()
    assert prefix in sent  # Not merely present in ctx or serialized runtime metadata.
    assert sent.count("\n\nPROJECT_CONTEXT_JSON\n") == 1
    assert sent.count(SOURCE_URL) == 1
    native_project = json.loads(sent.split("\n\nPROJECT_CONTEXT_JSON\n", 1)[1].split("\n\n", 1)[0])
    assert native_project["quality_context"] == packet["quality_context"]
    assert "本单研究来源·已保存事实的短索引" not in sent
    assert plan == plan_before and packet["quality_context"] == plan_before["quality_context"]
    research = getattr(stack.core, "_RESEARCH_CAPABILITIES", "")
    if depth == "quick":
        assert stack.core._QUICK_DELIVERY_CONTRACT in sent
        assert "BASE_TOOLBOX_SENTINEL" not in sent
        if research:
            assert research not in sent
        # Existing QC is legitimate input; quick need not erase it.
        assert SOURCE_URL in sent
    else:
        assert sent.count("BASE_TOOLBOX_SENTINEL") == 1
        if research:
            assert sent.count(research) == 1


@pytest.mark.parametrize("attached", [False, True])
def test_visual_tool_examples_preserve_receipts_at_actual_native_send(send_stack, monkeypatch, attached):
    stack = send_stack
    monkeypatch.setattr(stack.core, "_media_prelude", stack.real_media_prelude)
    packet = project(stack)
    plan = {"project_context": packet, "tasks": {"member": QUESTION}}
    if attached:
        image = stack.project / "attached.png"
        image.write_bytes(b"fixture; prompt construction does not inspect image content")
        plan["attachments"] = [{"path": str(image)}]
    full_task = QUESTION + stack.core._build_prelude(QUESTION, plan, user_id="alice")
    result = stack.core._run_core_inprocess(
        "__ut_terminal", full_task, "capture-image-example", timeout_s=20,
        model_override="fixture-model", appid_override="fixture-only",
        session_id="payload-session", user_id="alice", project_context=packet,
        task_facts={"goal": QUESTION}, execution_depth="standard")
    assert result["final"] == "NATIVE_PAYLOAD_OK", result
    actual = json.loads(stack.capture.read_text())
    sent = actual["task"]
    media_cli = str(SERVICE / "media_tools.py")
    assert f'see_image:  python3.12 {media_cli!r} --json see_image "PATH" "QUESTION"' in sent
    if attached:
        assert str(image) in sent
        assert f"图片: python3.12 {media_cli!r} --json see_image 'PATH' 'QUESTION'" in sent
        assert "可用工具(已配好key,直接调·不要让用户再配置)" not in sent
        assert "工具入口不是授权或可用性证明；配置与可用性以本次工具结果为准" in sent
