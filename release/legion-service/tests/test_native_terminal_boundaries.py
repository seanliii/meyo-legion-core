"""原始 SDK -> 实际 adapter -> 产品 core；强停用真实但不调用模型的 worker。"""
from __future__ import annotations

import builtins
import copy
import json
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
sys.path.insert(0, str(SERVICE))


@pytest.fixture
def native_stack(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ORCH))
    for name, directory in (
        ("MEYO_LEGION_DIR", "legion"), ("LEGION_HOME_DIR", "home"),
        ("LEGION_TASK_MEMORY_DIR", "memory"), ("LEGION_SESSIONS_DIR", "sessions"),
    ):
        monkeypatch.setenv(name, str(tmp_path / directory))
    monkeypatch.setenv("AIGC_APPID", "fixture-only")
    monkeypatch.setenv("MEYO_UNIFIED_APPID", "fixture-only")
    monkeypatch.setenv("LEGION_GATEWAY_RETRIES", "0")
    monkeypatch.setitem(sys.modules, "github_access",
                        SimpleNamespace(shared_environment=lambda env: env))
    # 所有 SDK 请求均为内存桩；意外漏设的请求也不能访问远端。
    connect = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple):
            assert address[0] in {"127.0.0.1", "::1", "localhost"}, address
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    import legion_core as core
    import task_memory
    import external_agent_registry
    import harness_registry as registry
    import runtime_manifest
    assert Path(registry._hermes_adapter.__code__.co_filename).resolve() == (
        ORCH / "harness_registry.py").resolve()
    # SDK 选择前提属于 fixture，不能依赖工作机上未跟踪的 Hermes 源码。
    # 只改变布局位置；执行的 adapter 及其生成代码仍来自本次被测树。
    sdk_entry = tmp_path / "sdk-layout/meyo-orchestrator/harness_registry.py"
    sdk_entry.parent.mkdir(parents=True)
    sdk_entry.write_text("# Fixture location anchor; never imported.\n")
    sdk_source = sdk_entry.parent.parent / "hermes-agent/run_agent.py"
    sdk_source.parent.mkdir()
    sdk_source.write_text("raise AssertionError('SDK dependencies must come from test doubles')\n")
    monkeypatch.setattr(registry, "__file__", str(sdk_entry))
    supports_depth = runtime_manifest.supports_depth
    monkeypatch.setattr(runtime_manifest, "supports_depth", lambda key, depth:
                        depth in {"quick", "standard", "deep"} if key == "__ut_terminal"
                        else supports_depth(key, depth))
    monkeypatch.setattr(task_memory, "ROOT", tmp_path / "memory")
    monkeypatch.setattr(core, "_harness_keys", lambda: {"__ut_terminal"})
    monkeypatch.setattr(core, "_CORE_INPROC_KEYS", {"__ut_terminal"})
    monkeypatch.setattr(core, "_ORCH_OK", True)
    monkeypatch.setattr(core, "_dynamic_max_attempts", lambda pool: 3)
    monkeypatch.setattr(core, "_fallback_candidates", lambda *a, **kw: [])
    monkeypatch.setattr(core, "_stable_prelude", lambda *a, **kw: "")
    monkeypatch.setattr(core, "record_model_health", lambda *a, **kw: None)
    monkeypatch.setattr(core, "_record_gateway_call", lambda *a, **kw: None)
    monkeypatch.setattr(external_agent_registry, "load_catalog", lambda: [])
    monkeypatch.setattr(registry, "_STANDALONE_ADAPTERS",
                        {"__ut_terminal": registry._hermes_adapter})
    monkeypatch.setattr(registry, "_legion_prompt", lambda task, ctx: task)
    monkeypatch.setattr(registry, "_is_cloud_desktop_task", lambda task: False)
    project = tmp_path / "original-project"
    project.mkdir()
    (project / "keep.txt").write_text("original project")
    state = SimpleNamespace(core=core, registry=registry, memory=task_memory,
                            root=tmp_path, project=project, raw=[], calls=[],
                            mapped=[], selections=[], db_reads=[], events=[],
                            prior_calls=[], runtime_error=None, strip_public_error=False)
    original_execute = core._run_core_inprocess

    def execute(*args, **kwargs):
        state.prior_calls.append(copy.deepcopy(kwargs.get("prior_attempts") or []))
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(core, "_run_core_inprocess", execute)

    def select(*args, **kwargs):
        state.selections.append(kwargs)
        return {"model": "different-model", "appid": "different-fixture"}

    monkeypatch.setattr(core, "select_usable_model", select)

    def sdk_process(cmd, timeout, **kwargs):
        assert len(cmd) == 4 and cmd[1:3] == ["-P", "-c"] and kwargs.get("tag") == "hermes", (
            "SDK fixture selected an unexpected execution entry", cmd[:2], kwargs.get("tag"))
        record = {"cwd": kwargs["cwd"], "terminal_cwd": kwargs["env"]["TERMINAL_CWD"],
                  "native_home": kwargs["env"].get("HERMES_HOME")}

        class Agent:
            def __init__(self, **settings):
                self.session_id = settings.get("session_id") or "native-fixture"
                record["settings"] = settings
                self.session_input_tokens = self.session_output_tokens = 0
                self.session_total_tokens = self.session_api_calls = 0

            def run_conversation(self, task, conversation_history):
                record.update(task=task, history=copy.deepcopy(conversation_history))
                state.calls.append(record)
                self.session_input_tokens += 3
                self.session_output_tokens += 2
                self.session_total_tokens += 5
                self.session_api_calls += 1
                return copy.deepcopy(state.raw[len(state.calls) - 1])

        class DB:
            def get_messages_as_conversation(self, sid, **options):
                state.db_reads.append(sid)
                return [{"role": "user", "content": "original native history"}]

        output = []
        real_import = builtins.__import__
        modules = {
            "run_agent": SimpleNamespace(AIAgent=Agent),
            "hermes_state": SimpleNamespace(SessionDB=DB),
            "os": SimpleNamespace(environ=kwargs["env"]),
            "sys": SimpleNamespace(path=[], maxsize=sys.maxsize),
        }

        def importer(name, *args, **kw):
            return modules[name] if name in modules else real_import(name, *args, **kw)

        # 不改写生成代码，只替换 SDK、DB 和进程 I/O。
        exec(compile(cmd[-1], "<actual-hermes-sdk>", "exec"), {
            "__builtins__": {**vars(builtins), "__import__": importer,
                             "print": lambda value,**kw: output.append(value)}})
        data = json.loads(output[-1])
        state.mapped.append(copy.deepcopy(data))
        if state.strip_public_error:
            data.pop("error", None)  # 兼容只有 native_facts 的已有 adapter 输出。
        if state.runtime_error is not None:
            data["error"] = state.runtime_error
        return SimpleNamespace(returncode=0, stderr="",
                               stdout=json.dumps(data, ensure_ascii=False))

    monkeypatch.setattr(registry, "_run_live", sdk_process)
    return state


def invoke(stack, raw, *, depth="standard"):
    stack.raw = copy.deepcopy(raw)
    result = stack.core.run_one(
        "__ut_terminal", "继续原工作", qhash="__native_terminal",
        preferred_model="fixture-model", preferred_appid="fixture-only",
        user_id="alice", conversation_id="native-conversation",
        task_facts={"project_context": {"repo_root": str(stack.project), "user_id": "alice"}},
        execution_depth=depth, cb=stack.events.append)
    assert stack.calls and stack.mapped, (
        "SDK fixture did not reach the native result", result.get("status"), result.get("error"))
    return result


def budget(*, completed=False, cleanup=False):
    result = {"final_response": "2", "completed": completed, "failed": False,
              "interrupted": False,
              "turn_exit_reason": "max_iterations_reached(2/6)" if completed
                                  else "max_iterations_reached(6/6)"}
    if cleanup:
        result["cleanup_errors"] = ["nonfatal cleanup warning"]
    return result


def done(**facts):
    return {"final_response": "好", "completed": True, "failed": False,
            "interrupted": False, "turn_exit_reason": "text_response(2)",
            "model": "actual-model", **facts}


@pytest.mark.parametrize("conflicting_completed,cleanup", [(True, False), (False, True)])
def test_sdk_budget_reason_resumes_same_native_project(native_stack, conflicting_completed, cleanup):
    first = budget(completed=conflicting_completed, cleanup=cleanup)
    result = invoke(native_stack, [first, done()], depth="quick")
    assert result["status"] == "done" and result["execution_calls"] == 2
    assert native_stack.mapped[0]["status"] == "incomplete"
    assert native_stack.mapped[0]["native_facts"]["completed"] is conflicting_completed
    assert result["native_attempts"][0]["facts"]["completed"] is conflicting_completed
    assert result["usage"]["total_tokens"] == 10
    assert result["model_used"] == "actual-model"
    assert native_stack.selections == []
    first_call, resumed = native_stack.calls
    assert first_call["history"] == [] and resumed["history"]
    for field in ("model", "api_key", "base_url", "session_id", "user_id"):
        assert first_call["settings"][field] == resumed["settings"][field]
    assert first_call["cwd"] == resumed["cwd"] == str(native_stack.project)
    assert resumed["terminal_cwd"] == str(native_stack.project)
    assert first_call["native_home"] == resumed["native_home"]
    assert (native_stack.project / "keep.txt").read_text() == "original project"
    assert native_stack.memory.context("__native_terminal", "__ut_terminal")["native_facts"]["completed"]


def test_budget_resume_stops_at_existing_parent_attempt_limit(native_stack):
    result = invoke(native_stack, [budget(completed=True)] * 3)
    assert result["status"] == "error"
    assert result["execution_calls"] == result["attempts"] == 3
    assert len(native_stack.calls) == 3 and native_stack.selections == []
    assert result["fail_kind"] == "native_iteration_budget"
    assert result["native_facts"]["completed"] is True  # 不改写原始矛盾事实。


@pytest.mark.parametrize("field,message,category", [
    ("error", "authorization required", "authorization_required"),
    ("failure_reason", "credential required", "credential_required"),
])
@pytest.mark.parametrize("upgrade", [False, True])
def test_raw_sdk_permission_after_budget_never_selects_a_third_model(
        native_stack, field, message, category, upgrade):
    # 复用 /tmp/meyo-product-resume-after-review.md 中的失败序列。
    partial = "我会去做"
    if upgrade:
        partial += '\n<<LEGION_ORCH>>{"upgrade":{"depth":"standard"}}<<END>>'
    denied = {"final_response": partial, "completed": False, "failed": True,
              "interrupted": False, "turn_exit_reason": "provider_failure", field: message}
    result = invoke(native_stack, [budget(), denied, done()],
                    depth="quick" if upgrade else "standard")
    assert len(native_stack.calls) == result["execution_calls"] == 2
    assert result["status"] == "error"
    assert result["error_envelope"]["category"] == category
    assert native_stack.selections == []
    assert not any(event.get("t") == "execution_depth_upgraded" for event in native_stack.events)
    assert result["native_facts"][field] == message
    assert native_stack.mapped[1]["error"] == message
    if upgrade:
        assert result["execution_depth"] == "quick"
        assert any(event.get("execution_depth") == "quick" for event in native_stack.events)


def test_raw_sdk_cancel_prevents_upgrade_and_retry(native_stack):
    cancelled = {"final_response": '<<LEGION_ORCH>>{"upgrade":{"depth":"standard"}}<<END>>',
                 "completed": True, "failed": False, "interrupted": True,
                 "turn_exit_reason": "interrupted_by_user"}
    result = invoke(native_stack, [budget(), cancelled, done()], depth="quick")
    assert result["status"] == "cancelled" and result["execution_calls"] == 2
    assert result["native_facts"]["interrupted"] is True
    assert native_stack.selections == []
    assert not any(event.get("t") == "execution_depth_upgraded" for event in native_stack.events)


def test_legitimate_quick_upgrade_keeps_native_session_and_physical_call_count(native_stack):
    first = budget()
    first["final_response"] = '<<LEGION_ORCH>>{"upgrade":{"depth":"standard"}}<<END>>'
    result = invoke(native_stack, [first, done()], depth="quick")
    assert result["status"] == "done"
    assert result["execution_calls"] == 2 and result["attempts"] == 1
    assert result["execution_depth_history"] == ["quick", "standard"]
    assert result["resumed_native"] is True
    assert native_stack.calls[1]["history"] and native_stack.selections == []


def test_cleanup_warning_is_not_a_native_failure(native_stack):
    result = invoke(native_stack, [done(cleanup_errors=["authorization required in optional cleanup"])])
    assert result["status"] == "done" and result["execution_calls"] == 1
    assert result["native_facts"]["cleanup_errors"]
    assert native_stack.selections == []


@pytest.mark.parametrize("runtime_error", [None, "worker runtime diagnostic"])
def test_core_consumes_nested_error_without_overwriting_runtime_error(native_stack, runtime_error):
    native_stack.strip_public_error = True
    native_stack.runtime_error = runtime_error
    result = invoke(native_stack, [{
        "final_response": "我会去做", "completed": False, "failed": True,
        "turn_exit_reason": "provider_failure", "error": "credential required"}, done()])
    assert result["execution_calls"] == 1 and result["status"] == "error"
    assert result["error_envelope"]["category"] == "credential_required"
    assert "credential required" in result["error"]
    if runtime_error:
        assert runtime_error in result["error"]
    assert native_stack.selections == []


def test_empty_native_error_does_not_clear_runtime_authorization_failure(native_stack):
    native_stack.runtime_error = "authorization required by runtime"
    result = invoke(native_stack, [{
        "final_response": "partial", "completed": False, "failed": False,
        "turn_exit_reason": "unrecognized_stop", "error": "", "failure_reason": ""}])
    assert result["execution_calls"] == 1
    assert result["error_envelope"]["category"] == "authorization_required"
    assert "authorization required by runtime" in result["error"]
    assert result["native_facts"]["error"] == ""
    assert native_stack.selections == []


def test_existing_error_envelope_remains_authoritative(native_stack):
    envelope = {"category": "native_session_unavailable", "retryable": False,
                "scope": "harness", "message": "explicit native envelope"}
    result = invoke(native_stack, [{
        "final_response": "partial", "completed": False, "failed": True,
        "error": "authorization required", "error_envelope": envelope}])
    assert result["error_envelope"]["category"] == envelope["category"]
    assert result["error_envelope"]["message"] == envelope["message"]
    assert result["execution_calls"] == 1


def test_nonretryable_native_failure_is_not_retried_due_to_final_wording(native_stack):
    result = invoke(native_stack, [{
        "final_response": "我会去做", "completed": False, "failed": True,
        "error": "native terminal failure", "turn_exit_reason": "provider_failure"}, done()])
    assert result["status"] == "error" and result["execution_calls"] == 1
    assert result["error_envelope"]["retryable"] is False
    assert native_stack.selections == []


def test_retryable_provider_rate_limit_does_not_reissue_or_rotate(native_stack):
    result = invoke(native_stack, [{
        "final_response": "我会去做", "completed": False, "failed": True,
        "error": "HTTP 429 rate limit", "turn_exit_reason": "provider_failure"}, done()])
    # Retryable means this can be retried after provider backoff; it is not
    # permission to immediately change the model/credential in this request.
    assert result["status"] == "error" and result["execution_calls"] == 1
    assert native_stack.selections == []
    assert len(native_stack.prior_calls) == 1
    assert result["error_envelope"]["category"] == "rate_limit"
    assert result["error_envelope"]["scope"] == "provider"
    assert result["error_envelope"]["retryable"] is True


def test_unrecognized_incomplete_is_not_budget_resume(native_stack):
    result = invoke(native_stack, [{
        "final_response": "partial", "completed": False, "failed": False,
        "turn_exit_reason": "unrecognized_stop"}])
    assert result["status"] == "error" and result["execution_calls"] == 1
    assert not any(event.get("t") == "native_resume" for event in native_stack.events)


@pytest.mark.parametrize("native_exit,through_core,parent_status,child_envelope", [
    pytest.param(native_exit, through_core, parent_status, None,
                 id=f"{native_exit}-{through_core}-{parent_status}")
    for native_exit in ("interrupted", "budget")
    for through_core in (False, True)
    for parent_status in ("cancelled", "timeout")
] + [
    pytest.param("interrupted", True, "cancelled",
                 {"category": "rate_limit", "retryable": True, "scope": "provider",
                  "message": "child rate limit"},
                 id="parent_cancel_over_child_rate_limit"),
    pytest.param("interrupted", True, "timeout",
                 {"category": "user_cancel", "retryable": False, "scope": "user",
                  "message": "child cancellation"},
                 id="parent_timeout_over_child_cancel"),
])
def test_real_worker_stop_preserves_native_facts_without_overriding_parent(
        native_stack, monkeypatch, parent_status, through_core, native_exit, child_envelope):
    core = native_stack.core
    marker = native_stack.root / "worker-ready"
    received = threading.Event()
    worker = native_stack.root / "native-worker.py"
    native_facts = {
        "completed": True, "interrupted": native_exit == "interrupted",
        "turn_exit_reason": ("interrupted_by_user" if native_exit == "interrupted"
                             else "max_iterations_reached(2/6)"),
    }
    worker.write_text(
        "import json,os,signal,sys,time\n"
        "from pathlib import Path\n"
        "os.chdir(os.environ['MEYO_WORKDIR'])\n"
        "signal.signal(signal.SIGTERM,lambda *_:exit(0))\n"
        f"print(json.dumps({{'status':'ok','ok':True,'cancelled':{parent_status != 'cancelled'!r},'final':'partial',"
        "'native_checkpoint':{'kind':'hermes_session','session_id':'stopped-native'},"
        f"'native_facts':{native_facts!r},"
        "'resumed_native':True,'native_events':['worker-final'],"
        "'workdir':os.getcwd(),'cwd':os.getcwd(),"
        "'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5},"
        f"'model_used':'worker-model',**{({'error_envelope': child_envelope} if child_envelope else {})!r}}}),flush=True)\n"
        f"Path({str(marker)!r}).write_text('ready')\n"
        "print('\\x1e[legion-ev]' + json.dumps({'t':'fixture_result_flushed'}),file=sys.stderr,flush=True)\n"
        "while True:time.sleep(.05)\n")
    # 本例验证“已落盘/已输出”的原生现场，不能用 .3s 冷启动竞速冒充前提。
    # Popen 立即返回，先启动真实父端两泵，再同步实际收包；不改预算或停止逻辑。
    def on_progress(event):
        native_stack.events.append(event)
        if event.get("t") == "fixture_result_flushed":
            received.set()

    def pump_ready_cpu_sample(group):
        received.wait(3)
        return 0

    monkeypatch.setattr(core, "_WORKER", str(worker))
    monkeypatch.setattr(core, "_CORE_INPROC_KEYS", set())
    monkeypatch.setattr(core, "_dynamic_max_attempts", lambda pool: 1)
    monkeypatch.setattr(core, "_IDLE_TIMEOUT", 10)
    monkeypatch.setattr(core, "_pgroup_cpu_seconds", pump_ready_cpu_sample)
    cancel = received.is_set if parent_status == "cancelled" else (lambda: False)
    context = {"repo_root": str(native_stack.project), "user_id": "alice"}
    if through_core:
        result = core.run_one(
            "__ut_terminal", "fixture", qhash="__stopped_native",
            preferred_model="fixture-model", preferred_appid="fixture-only",
            user_id="alice", conversation_id="stop-conversation", cancel_check=cancel,
            task_facts={"project_context": context},
            cb=on_progress,
            attempt_timeout=5 if parent_status == "cancelled" else .3)
    else:
        result = core._run_harness_isolated(
            "__ut_terminal", "fixture", "__stopped_native",
            5 if parent_status == "cancelled" else .3,
            user_id="alice", project_context=context, cancel_check=cancel,
            progress_cb=on_progress)
    assert marker.exists(), "fixture worker never produced its native checkpoint"
    assert received.is_set(), "parent pump did not receive the native checkpoint readiness"
    assert result["status"] == parent_status
    assert result["native_checkpoint"]["session_id"] == "stopped-native"
    assert {name: result["native_facts"][name] for name in native_facts} == native_facts
    assert result["resumed_native"] is True
    assert result["native_events"] == ["worker-final"]
    assert result["workdir"] == str(native_stack.project)
    assert result["usage"]["total_tokens"] == 5
    assert native_stack.selections == []
    if through_core:
        assert result["execution_calls"] == 1
    else:
        assert result["cwd"] == str(native_stack.project)
        assert result["ok"] is False
        assert result["cancelled"] is (parent_status == "cancelled")
    if child_envelope:
        category = "user_cancel" if parent_status == "cancelled" else "runtime_budget"
        assert result["error_envelope"]["category"] == category
        assert result["error_envelope"]["retryable"] is False
        assert result["native_facts"]["worker_error_envelope"] == child_envelope
        if parent_status == "timeout":
            assert result["fail_kind"] == "runtime_budget"


def test_cancel_between_budget_attempts_retains_last_native_workdir(native_stack):
    native_stack.raw = [budget(), done()]
    stopped = {"value": False}

    def event(row):
        native_stack.events.append(row)
        if row.get("t") == "native_resume":
            stopped["value"] = True

    result = native_stack.core.run_one(
        "__ut_terminal", "continue", qhash="__cancel_after_budget",
        preferred_model="fixture-model", preferred_appid="fixture-only",
        user_id="alice", conversation_id="native-conversation",
        task_facts={"project_context": {"repo_root": str(native_stack.project), "user_id": "alice"}},
        cb=event, cancel_check=lambda: stopped["value"])
    assert result["status"] == "cancelled" and result["execution_calls"] == 1
    assert result["workdir"] == str(native_stack.project)
    assert result["native_checkpoint"]["kind"] == "hermes_session"
    assert result["usage"]["total_tokens"] == 5
    assert len(native_stack.calls) == 1 and native_stack.selections == []


@pytest.mark.parametrize("root_field", ["repo_root", "workspace_root", None])
def test_existing_external_entry_receives_project_intent_and_native_context(
        native_stack, monkeypatch, root_field):
    import external_agent_registry as registry
    import legion_sandbox
    item = {"key": "__ut_external", "transports": [{"type": "sdk", "worker": ["fixture"]}]}
    monkeypatch.setattr(registry, "load_catalog", lambda: [item])
    monkeypatch.setattr(registry, "_probe_one", lambda *a, **kw: {
        "execution_ready": True, "status": "ready", "status_detail": ""})
    monkeypatch.setattr(registry, "_strip_dead_proxy", lambda env: env)
    original_prepare = legion_sandbox.prepare_peer_sources
    observed = {}
    peer = native_stack.project / "peer_sources"
    peer.mkdir()
    (peer / "keep.txt").write_text("existing source")

    def prepare(key, destination, *, preserve_project=False):
        observed.update(key=key, destination=destination, preserve_project=preserve_project)
        return original_prepare(key, destination, preserve_project=preserve_project)

    def transport(spec, context, env, timeout_s, event_cb=None):
        observed.update(context=copy.deepcopy(context), env_workdir=env["MEYO_WORKDIR"],
                        peer_context=env["MEYO_PEER_SOURCE_CONTEXT"])
        return {"status": "ok", "final": "fixture"}

    monkeypatch.setattr(legion_sandbox, "prepare_peer_sources", prepare)
    monkeypatch.setattr(registry, "run_transport", transport)
    project = {root_field: str(native_stack.project), "user_id": "alice"} if root_field else {}
    result = native_stack.core._run_external_inprocess(
        "__ut_external", "continue", "__external_project",
        model_override="fixture-model", appid_override="fixture-only", user_id="alice",
        session_id="external-logical", project_context=project, timeout_s=5,
        task_memory={"native_checkpoint": {"session_id": "external-native"}})
    assert result["status"] == "ok"
    assert observed["preserve_project"] is bool(root_field)
    assert observed["destination"] == observed["env_workdir"] == result["workdir"]
    assert observed["context"]["session_id"] == "external-native"
    assert observed["context"]["model"] == "fixture-model"
    assert observed["peer_context"]
    if root_field:
        assert result["workdir"] == str(native_stack.project)
    else:
        assert result["workdir"] != str(native_stack.project)
    assert (peer / "keep.txt").read_text() == "existing source"
