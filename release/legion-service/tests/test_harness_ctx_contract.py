# -*- coding: utf-8 -*-
"""HarnessCtx 下放后的完成契约:对外 model provenance 必须信 adapter 实际值。"""
import ast
import importlib.util
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.dirname(HERE)
if SERVICE not in sys.path:
    sys.path.insert(0, SERVICE)

import legion_core as L
import error_envelope
import contextlib


@contextlib.contextmanager
def patch_exec(fake_iso):
    """同 test_harness_failure_matrix.patch_exec:同时 patch 三条执行路径(隔离/core进程内/external
    进程内)为同一 fake·让 run_one 语义(model上报/失败回流/user传递)对三条路都成立、都过测试。
    inproc 签名无 timeout_s 位置参·wrapper 适配。"""
    import inspect as _insp
    _fp = _insp.signature(fake_iso).parameters
    _has_var_kw = any(p.kind == p.VAR_KEYWORD for p in _fp.values())
    def _inproc_wrap(key, task, qhash, model_override=None, appid_override=None, **kw):
        kw.pop("timeout_s", None)  # fake_iso 用位置参 timeout_s
        if not _has_var_kw:  # fake 显式列参(无**kwargs)→只传它支持的·避免多余参 TypeError(同run_one探测式)
            kw = {k: v for k, v in kw.items() if k in _fp}
        return fake_iso(key, task, qhash, 0, model_override=model_override,
                        appid_override=appid_override, **kw)
    with mock.patch.object(L, "_run_harness_isolated", fake_iso), \
         mock.patch.object(L, "_run_core_inprocess", _inproc_wrap), \
         mock.patch.object(L, "_run_external_inprocess", _inproc_wrap):
        yield


def test_aigc_health_distinguishes_deploy_secret_from_local_fallback(tmp_path):
    """个人 models.json 可供本地开发，但不能被 health 冒充成可上线凭证。"""
    local_catalog = tmp_path / ".claude/skills/aigc-model-router/models.json"
    local_catalog.parent.mkdir(parents=True)
    local_catalog.write_text('{"programming":{"candidates":[{"appids":["local-app"]}]}}')
    real_expanduser = os.path.expanduser

    def _expanduser(path):
        if path == "~/.claude/skills/aigc-model-router/models.json":
            return str(local_catalog)
        return real_expanduser(path)

    with mock.patch.dict(os.environ, {}, clear=True), \
         mock.patch.object(L.os.path, "expanduser", side_effect=_expanduser):
        local = L._aigc_ready()
    assert local["appid_configured"] is True
    assert local["appid_source"] == "local_models_json"
    assert local["appid_deployable"] is False

    L._AIGC_READY_CACHE["data"] = None  # 90s TTL 缓存会让第二次读到第一次的 local 结果,测试先清
    with mock.patch.dict(os.environ, {"MEYO_UNIFIED_APPID": "deploy-secret"}, clear=True):
        deployed = L._aigc_ready()
    assert deployed["appid_configured"] is True
    assert deployed["appid_source"] == "env:MEYO_UNIFIED_APPID"
    assert deployed["appid_deployable"] is True
    assert "deploy-secret" not in repr(deployed)


def test_run_one_reports_adapter_actual_model():
    """ctx 下放后 harness 可自主换 model;对外必须报告 adapter 实际 model_used,
    不能继续拿父层 preferred_model 冒充(两者不同时暴露契约 bug)。"""
    def _fake_isolated(key, task, qhash, timeout_s, model_override=None,
                       appid_override=None, **kwargs):
        # **kwargs 吸收 session_id/tried_models/prior_attempts 等后续下放通道的参数,
        # 桩只关心 model_override → 保持前向兼容,不锁死 _run_harness_isolated 的签名。
        assert model_override == "parent-picked-model"
        return {"status": "ok", "final": "真交付", "model_used": "harness-actual-model"}

    with mock.patch.object(L, "_ORCH_OK", True), \
         mock.patch.object(L, "_harness_keys", return_value={"penguin"}), \
         patch_exec(_fake_isolated):
        out = L.run_one("penguin", "task", qhash="ctx-contract",
                        preferred_model="parent-picked-model", preferred_appid="app")

    assert out["status"] == "done", out
    assert out["model_used"] == "harness-actual-model", out
    assert out["model"] == "harness-actual-model", out


def _load_registry():
    """加载 harness_registry(军团 adapter 真源码)。"""
    root = os.path.dirname(os.path.dirname(SERVICE))
    reg = os.path.join(root, "harness_baselines", "meyo-orchestrator")
    if reg not in sys.path:
        sys.path.insert(0, reg)
    import harness_registry as H
    return H


# 当前内置答题 adapter 契约；auto 已按用户要求退役，保留工具但不进答题池。
# omnigent 守自治边界，但仍接 ctx 拿失败回流(prior_attempts);
# omnigent 进一步把 ctx.model 通过 backend prefer_model 下放(在网关清单里则真用它)。
_CTX_ADAPTERS = ["cc", "codex", "penguin", "jcode", "prime",
                 "hermes", "deepseek", "deepagents", "omnigent"]


def test_all_ctx_adapters_accept_ctx_and_are_backward_compatible():
    """当前全部内置 adapter 保持 ctx=None 入口，不能为旧测试重新启用 auto。"""
    import inspect
    H = _load_registry()
    assert set(H._STANDALONE_ADAPTERS) == set(_CTX_ADAPTERS)
    assert "auto" not in H._STANDALONE_ADAPTERS
    for key in _CTX_ADAPTERS:
        fn = H._STANDALONE_ADAPTERS[key]
        sig = inspect.signature(fn)
        assert "ctx" in sig.parameters, f"{key} adapter 缺 ctx 入口"
        assert sig.parameters["ctx"].default is None, f"{key} ctx 非缺省 None(破坏向后兼容)"


def _load_responses_bridge():
    root = Path(SERVICE).parents[1]
    path = root / "harness_baselines/comparison/responses_chat_bridge.py"
    spec = importlib.util.spec_from_file_location("_bridge_contract", path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    return bridge


def test_responses_bridge_only_synthesizes_codex_shell_without_client_tools():
    """Omnigent 有自己的 Read/Write/Bash 时不能再注入 Codex 专属 exec_command。"""
    bridge = _load_responses_bridge()
    own = {"tools": [{"type": "function", "name": "Write", "description": "write",
                       "parameters": {"type": "object", "properties": {}}}]}
    assert [x["function"]["name"] for x in bridge._tools(own)] == ["Write"]
    assert [x["function"]["name"] for x in bridge._tools({"tools": []})] == ["exec_command"]


def test_responses_bridge_never_sends_tool_choice_without_tools(monkeypatch):
    """线上 Codex bridge 不能复现 CCR 的悬空 tool_choice 400。"""
    bridge = _load_responses_bridge()
    captured = {}

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured.update(kwargs["json"])
            return _Response()

    monkeypatch.setattr(bridge.httpx, "AsyncClient", _Client)

    class _Request:
        headers = {"Authorization": "Bearer fixture-request-key"}
        async def json(self):
            return {"input": [{"role": "user", "content": "hello"}], "tools": []}

    import asyncio
    asyncio.run(bridge.responses(_Request()))
    assert not ("tool_choice" in captured and not captured.get("tools")), captured


def test_responses_bridge_keeps_short_request_unchanged(monkeypatch):
    bridge = _load_responses_bridge()
    payload = {"model": "m", "messages": [
        {"role": "system", "content": "contract"},
        {"role": "user", "content": "short goal"},
    ], "tools": [{"type": "function", "function": {
        "name": "read", "description": "read a file",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "examples": ["/tmp/a"]}}}}}],
        "tool_choice": "auto", "max_tokens": 2048}
    bounded, report = bridge._budget_payload(payload, {"max_output_tokens": 2048})
    assert bounded == payload
    assert report["degraded"] is False and report["reason"] == "within_budget"


def test_responses_bridge_hard_budget_preserves_goal_latest_state_and_tool_pairs(monkeypatch):
    bridge = _load_responses_bridge()
    monkeypatch.setenv("MEYO_RESPONSES_CONTEXT_WINDOW", "12000")
    monkeypatch.setenv("MEYO_RESPONSES_INPUT_BUDGET", "7000")
    messages = [
        {"role": "system", "content": "SYSTEM-CONTRACT-" + "规" * 5000},
        {"role": "user", "content": "OLD-HISTORY-" + "旧" * 10000},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_old", "type": "function",
            "function": {"name": "read", "arguments": '{"path":"old"}'}}]},
        {"role": "tool", "tool_call_id": "call_old", "content": "OLD-TOOL-" + "旧" * 10000},
        {"role": "user", "content": "USER-GOAL-START " + "目标" * 9000 + " LATEST-FACT-END"},
    ]
    payload = {"model": "m", "messages": messages, "max_tokens": 5000,
               "tools": [{"type": "function", "function": {
                   "name": "read", "description": "d" * 10000,
                   "parameters": {"type": "object", "properties": {
                       "path": {"type": "string", "examples": ["x" * 20000]}}}}}],
               "tool_choice": "auto"}
    bounded, report = bridge._budget_payload(payload, {"max_output_tokens": 5000})
    rendered = __import__('json').dumps(bounded, ensure_ascii=False)
    assert report["degraded"] is True and report["reason"] == "preflight_hard_budget"
    assert report["final_estimated_tokens"] <= report["input_budget_tokens"]
    assert "USER-GOAL-START" in rendered and "LATEST-FACT-END" in rendered
    assert "上下文预算降级" in rendered and "上下文压缩" in rendered
    call_ids = {call["id"] for row in bounded["messages"] for call in row.get("tool_calls", [])}
    output_ids = {row.get("tool_call_id") for row in bounded["messages"] if row.get("role") == "tool"}
    assert output_ids <= call_ids
    assert bounded["max_tokens"] < payload["max_tokens"]


def test_responses_bridge_budgets_quality_audit_without_losing_goal_objection_or_final():
    bridge = _load_responses_bridge()
    import success_auditor
    prompt, _aliases = success_auditor.build_prompt(
        "QUALITY-GOAL-MUST-STAY " + "目标详情" * 20000,
        {"success_audit": {"enabled": True, "input_budget_tokens": 12000},
         "delivery_contract": {"acceptance": ["最终可用"]},
         "_prior_verification_reports": [{
             "verdict": "revise", "revision_request": "OBJECTION-MUST-BE-RECHECKED",
             "evidence": ["旧审计证据" * 20000]}]},
        [{"key": "revision", "status": "done",
          "final": "FINAL-CANDIDATE-MUST-STAY " + "最终验收现场" * 30000}])
    payload = {"model": "m", "messages": [{"role": "user", "content": prompt}],
               "max_tokens": 8192}
    bounded, report = bridge._budget_payload(
        payload, {"max_output_tokens": 8192}, budget_override=16000)
    rendered = __import__('json').dumps(bounded, ensure_ascii=False)
    assert report["final_estimated_tokens"] <= 16000
    assert "QUALITY-GOAL-MUST-STAY" in rendered
    assert "OBJECTION-MUST-BE-RECHECKED" in rendered
    assert "FINAL-CANDIDATE-MUST-STAY" in rendered
    assert "审计材料压缩" in rendered


def test_responses_bridge_retries_context_error_with_tighter_budget(monkeypatch):
    bridge = _load_responses_bridge()
    captured = []

    class _Response:
        def __init__(self, status_code, text, data=None):
            self.status_code, self.text, self._data = status_code, text, data or {}

        def json(self):
            return self._data

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured.append(kwargs["json"])
            if len(captured) == 1:
                return _Response(400, '{"error":{"code":"context_length_exceeded"}}')
            return _Response(200, "", {"choices": [{"message": {"content": "ok"}}], "usage": {}})

    monkeypatch.setattr(bridge.httpx, "AsyncClient", _Client)

    class _Request:
        headers = {"Authorization": "Bearer fixture-request-key"}
        async def json(self):
            return {"instructions": "contract", "input": [
                {"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": "GOAL " + "x" * 40000}]}],
                "max_output_tokens": 4096, "stream": False}

    import asyncio
    response = asyncio.run(bridge.responses(_Request()))
    assert len(captured) == 2
    assert bridge._token_upper_bound({"messages": captured[1]["messages"],
                                      "tools": captured[1].get("tools", [])}) \
           < bridge._token_upper_bound({"messages": captured[0]["messages"],
                                        "tools": captured[0].get("tools", [])})
    assert response["metadata"]["meyo_context_budget"]["reason"] == "context_window_retry"
    assert response["metadata"]["meyo_context_budget"]["retry_index"] == 1


def test_responses_bridge_forwards_scoped_prompt_cache_key(monkeypatch):
    bridge = _load_responses_bridge()
    captured = {}

    class _Response:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured.update(kwargs["json"])
            return _Response()

    monkeypatch.setattr(bridge.httpx, "AsyncClient", _Client)

    class _Request:
        headers = {"Authorization": "Bearer fixture-request-key"}
        async def json(self):
            return {"input": [{"role": "user", "content": "hello"}],
                    "prompt_cache_key": "client-key"}

    import asyncio
    with mock.patch.dict(os.environ, {"MEYO_PROMPT_CACHE_KEY": "scoped-key"}):
        asyncio.run(bridge.responses(_Request()))
    assert captured["prompt_cache_key"] == "scoped-key", captured


def test_shared_prompt_budget_preserves_goal_and_latest_context(monkeypatch):
    """所有内置/外部家共用的 prompt 边界应在首发前限窗，并保留目标首尾与关键现场。"""
    H = _load_registry()
    monkeypatch.setenv("LEGION_HARNESS_INPUT_BUDGET", "7000")
    ctx = H.HarnessCtx(
        stable_context="STABLE-CAPABILITY-" + "稳" * 5000,
        runtime_brief="RUNTIME-BRIEF-" + "现" * 4000,
        task_facts={"goal": "TASK-FACT-GOAL", "evidence": "证" * 5000},
        task_memory={"latest": "LATEST-TASK-STATE", "history": "旧" * 8000},
        workdir="/tmp/persistent-output",
    )
    prompt = H._legion_prompt(
        "USER-GOAL-START " + "中间材料" * 12000 + " USER-LATEST-FACT-END", ctx)
    report = ctx.context_budget_report
    assert report["degraded"] is True and report["reason"] == "preflight_hard_budget"
    assert H._prompt_upper_bound(prompt) <= 7000
    assert "USER-GOAL-START" in prompt and "USER-LATEST-FACT-END" in prompt
    assert "上下文预算降级" in prompt and "上下文压缩" in prompt
    assert report["sections"]["question"]["truncated"] is True


def test_quick_prompt_is_small_and_does_not_duplicate_project_context(monkeypatch):
    H = _load_registry()
    project = {"objective": "PROJECT-OBJECTIVE-UNIQUE", "open_gaps": [{"key": "format"}]}
    common = dict(
        stable_context="QUICK-UPGRADE-CONTRACT",
        task_facts={"goal": "只回答好", "project_context": project},
        project_context=project,
    )
    quick_ctx = H.HarnessCtx(execution_depth="quick", **common)
    full_ctx = H.HarnessCtx(execution_depth="standard", **common)
    quick_prompt = H._legion_prompt("只回答一个字：好", quick_ctx)
    full_prompt = H._legion_prompt("只回答一个字：好", full_ctx)
    assert "Full tool/capability catalog" not in quick_prompt
    assert "Full tool/capability catalog" in full_prompt
    assert "QUICK-UPGRADE-CONTRACT" in quick_prompt and "只回答一个字：好" in quick_prompt
    assert quick_prompt.count("PROJECT-OBJECTIVE-UNIQUE") == 1
    assert quick_ctx.context_budget_report["sections"]["protocol"]["original"] < 500
    assert H._prompt_upper_bound(quick_prompt) < H._prompt_upper_bound(full_prompt) * .35


def test_execution_depth_round_trips_through_env(monkeypatch):
    H = _load_registry()
    monkeypatch.setenv("MEYO_EXECUTION_DEPTH", "quick")
    assert H.HarnessCtx.from_env().execution_depth == "quick"


def test_quick_upgrade_marker_parser_extracts_request_and_cleans_final():
    H = _load_registry()
    final, request = H._parse_orch_request(
        '准备升级\n<<LEGION_ORCH>>{"upgrade":{"depth":"standard","reason":"需要联网和文件"}}<<END>>')
    assert final == "准备升级"
    assert request == {"upgrade": {"depth": "standard", "reason": "需要联网和文件"}}


def test_shared_prompt_context_overflow_retry_tightens_same_projection(monkeypatch):
    H = _load_registry()
    monkeypatch.setenv("LEGION_HARNESS_INPUT_BUDGET", "10000")
    question = "GOAL-HEAD " + "证据" * 12000 + " LATEST-TAIL"
    first = H.HarnessCtx()
    first_prompt = H._legion_prompt(question, first)
    retry = H.HarnessCtx(prior_attempts=[{
        "attempt": 1, "fail_kind": "context_overflow", "status": "error",
        "error": "Your input exceeds the context window of this model",
    }])
    retry_prompt = H._legion_prompt(question, retry)
    assert first.context_budget_report["input_budget_tokens"] == 10000
    assert retry.context_budget_report["input_budget_tokens"] == 6200
    assert retry.context_budget_report["reason"] == "context_window_retry"
    assert H._prompt_upper_bound(retry_prompt) < H._prompt_upper_bound(first_prompt)
    assert "GOAL-HEAD" in retry_prompt and "LATEST-TAIL" in retry_prompt


def test_deepseek_native_cli_uses_actual_session_workspace():
    """安装版headless从process.cwd取工作目录，profile不应接收用户产物。"""
    H = _load_registry()
    source = Path(H.__file__).read_text(encoding="utf-8")
    assert '_launch_cwd = str(_P(_wd).resolve())' in source
    assert 'cwd=_launch_cwd' in source


def test_omnigent_runtime_failure_is_not_model_retryable():
    env = error_envelope.normalize("rc1", "local daemon exited before server became ready",
                                    "harness_runtime")
    assert env.category == "harness_runtime"
    assert env.scope == "harness"
    assert env.retryable is False


def test_run_one_stops_after_non_retryable_harness_runtime_failure():
    calls = []

    def _fake_isolated(key, task, qhash, timeout_s, model_override=None,
                       appid_override=None, **kwargs):
        calls.append(model_override)
        return {
            "status": "rc1",
            "final": "",
            "error": "local daemon exited before server became ready",
            "fail_kind": "harness_runtime",
            "error_envelope": {
                "category": "harness_runtime", "retryable": False,
                "scope": "harness", "tool": "omnigent", "code": "rc1",
                "message": "local daemon exited before server became ready",
            },
        }

    with mock.patch.object(L, "_ORCH_OK", True), \
         mock.patch.object(L, "_harness_keys", return_value={"omnigent"}), \
         patch_exec(_fake_isolated):
        out = L.run_one("omnigent", "task", qhash="runtime-stop",
                        preferred_model="gpt-5.4", preferred_appid="app")
    assert len(calls) == 1, calls
    assert out["status"] == "error" and out["fail_kind"] == "harness_runtime", out


def test_omnigent_prefer_model_pairs_appid_and_falls_back(tmp_path, monkeypatch):
    """omnigent per-call model 下放:①ctx.model 在网关清单里→真用它+配对 appid;
    ②不在清单→安全落回默认(不硬塞·不走错协议)。"""
    H = _load_registry()
    import importlib
    mh = os.path.join(os.path.dirname(H.__file__), "meyo_harnesses")
    if mh not in sys.path:
        sys.path.insert(0, mh)
    ob = importlib.import_module("backends.omnigent_backend")
    # This is a selection-contract test, not a probe of the developer's private
    # model catalog. A controlled HOME also makes combinations order-independent.
    monkeypatch.setenv("HOME", str(tmp_path))
    catalog = tmp_path / ".claude/skills/aigc-model-router/models.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"programming":{"candidates":[{"model":"gpt-5.6-sol",'
                       '"appids":["fixture-model-key"]}]}}')
    m_pref, a_pref = ob._aigc_model_appid("gpt-5.6-sol")
    assert m_pref == "gpt-5.6-sol" and a_pref, "ctx.model 在清单里应真用它且配到 appid"
    m_bad, a_bad = ob._aigc_model_appid("model-not-in-any-list-xyz")
    assert m_bad and a_bad, "不在清单的 model 应安全落回默认(而非返回空导致该家崩)"
    assert m_bad != "model-not-in-any-list-xyz", "不认识的 model 不该硬塞(会走错协议)"


def test_omnigent_runtime_env_reaches_child_without_global_leak(monkeypatch, tmp_path):
    H = _load_registry()
    import importlib
    mh = os.path.join(os.path.dirname(H.__file__), "meyo_harnesses")
    if mh not in sys.path:
        sys.path.insert(0, mh)
    ob = importlib.import_module("backends.omnigent_backend")
    captured = {}

    class FakeBridge:
        def __enter__(self):
            return True

        def __exit__(self, *_args):
            return False

    native_cli = tmp_path / "omnigent"
    native_cli.write_text(f"#!{sys.executable}\n# native console fixture\n")
    native_cli.chmod(0o755)
    monkeypatch.setattr(ob, "_omnigent_bin", lambda: str(native_cli))
    monkeypatch.setattr(ob, "_aigc_model_appid", lambda _model: ("model", "appid"))
    monkeypatch.setattr(H, "_codex_bridge_ctx", lambda _port: FakeBridge())

    def fake_run(*_args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(_args[0], 0, stdout="done", stderr="")

    monkeypatch.setattr(H, "_run_task", fake_run)
    runtime = {"MEYO_SESSION_ID": "omnigent:conversation-1",
               "MEYO_USER_ID": "alice", "MEYO_WORKDIR": "/tmp/omnigent-scene"}
    before = {key: os.environ.get(key) for key in runtime}
    out = ob.run("q", "task", prefer_model="model", runtime_env=runtime)
    assert out["status"] == "ok"
    assert {key: captured[key] for key in runtime} == runtime
    assert {key: os.environ.get(key) for key in runtime} == before


def test_inproc_runtime_env_is_thread_local_and_reaches_child(monkeypatch):
    H = _load_registry()
    monkeypatch.setattr(H, "_gateway_no_proxy", lambda env: env)
    barrier = threading.Barrier(2)
    seen = {}

    class FakeProcess:
        def __init__(self, cmd, **kwargs):
            seen[threading.current_thread().name] = {
                key: kwargs["env"].get(key)
                for key in ("MEYO_SESSION_ID", "MEYO_USER_ID", "MEYO_WORKDIR")}
            self.pid = os.getpid()
            self.stdout = self.stderr = type("Stream", (), {
                "readline": lambda self: "", "close": lambda self: None})()
            self.returncode = 0
            self.stdin = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(H._sp, "Popen", FakeProcess)
    monkeypatch.setattr(H, "_load_proc_registry", lambda: None)

    def worker(session, user, workdir):
        H.set_inproc_runtime_env({"MEYO_SESSION_ID": session, "MEYO_USER_ID": user,
                                  "MEYO_WORKDIR": workdir})
        barrier.wait()
        H._run_live(["fake"], 1)
        H.set_inproc_runtime_env(None)

    first = threading.Thread(target=worker, name="first", args=("codex:conv", "alice", "/tmp/a"))
    second = threading.Thread(target=worker, name="second", args=("hermes:conv", "bob", "/tmp/b"))
    first.start(); second.start(); first.join(); second.join()
    assert seen == {
        "first": {"MEYO_SESSION_ID": "codex:conv", "MEYO_USER_ID": "alice", "MEYO_WORKDIR": "/tmp/a"},
        "second": {"MEYO_SESSION_ID": "hermes:conv", "MEYO_USER_ID": "bob", "MEYO_WORKDIR": "/tmp/b"},
    }
    assert getattr(H._INPROC_RUNTIME_ENV, "values", {}) == {}


def test_inproc_run_live_cancel_terminates_native_process():
    H = _load_registry()
    cancelled = threading.Event()
    H.set_inproc_cancel_check(cancelled.is_set)
    timer = threading.Timer(0.15, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        result = H._run_live([sys.executable, "-c", "import time; time.sleep(30)"], 60)
    finally:
        timer.cancel()
    assert result.cancelled is True and result.returncode == 130
    assert time.monotonic() - started < 5
    assert H.consume_inproc_cancelled() is True
    H.set_inproc_cancel_check(None)


def test_run_live_preserves_stdout_on_timeout_for_checkpoint_recovery(monkeypatch):
    H = _load_registry()
    monkeypatch.setattr(H, "_load_proc_registry", lambda: None)
    try:
        H._run_live([sys.executable, "-c", "print('SESSION=durable',flush=True);import time;time.sleep(30)"], 1.0)
        raise AssertionError("expected timeout")
    except subprocess.TimeoutExpired as exc:
        assert "SESSION=durable" in str(getattr(exc, "legion_stdout", ""))


def test_auto_adapter_reuses_checkpoint_id_and_returns_native_contract(monkeypatch, tmp_path):
    H = _load_registry()
    captured = {}
    checkpoint = {"kind": "auto_checkpoint", "session_id": "stable-auto-qid"}
    # 精简发布包按设计不含 auto 源码。本例测子进程调用契约，必须自行提供源码发现
    # 夹具，不能依赖开发者机器恰有完整仓库；和下方 Hermes 源码分支测试一致。
    registry_path = tmp_path / "harness_baselines" / "meyo-orchestrator" / "harness_registry.py"
    source = tmp_path / "seed-users-system" / "qa_system" / "agent" / "run_auto_thin.py"
    source.parent.mkdir(parents=True)
    source.write_text("# source-discovery fixture; never executed\n")
    monkeypatch.setattr(H, "__file__", str(registry_path))

    def fake_run(cmd, timeout, **kwargs):
        assert "-c" in cmd, cmd
        captured["code"] = cmd[cmd.index("-c") + 1]
        payload = {"final": "完成", "status": "ok", "model_used": "auto-model",
                   "usage_records": [{"input_tokens": 3, "output_tokens": 1}],
                   "native_checkpoint": checkpoint, "resumed_native": True}
        return type("Result", (), {"returncode": 0,
                                   "stdout": __import__('json').dumps(payload), "stderr": ""})()

    monkeypatch.setattr(H, "_run_live", fake_run)
    result = H._auto_adapter("fresh-qhash", "继续", ctx=H.HarnessCtx(
        user_id="alice", task_memory={"native_checkpoint": checkpoint}))
    assert result["status"] == "ok", result
    assert "stable-auto-qid" in captured["code"] and "resume=True" in captured["code"]
    assert "user_id=\"alice\"" in captured["code"]
    assert result["native_checkpoint"] == checkpoint and result["resumed_native"] is True


def test_auto_adapter_without_source_remains_unavailable(monkeypatch, tmp_path):
    """发现夹具不能掩盖发布包缺源码的事实，也不能重新启用已退役的 auto。"""
    H = _load_registry()
    registry_path = tmp_path / "harness_baselines" / "meyo-orchestrator" / "harness_registry.py"
    monkeypatch.setattr(H, "__file__", str(registry_path))
    launch = mock.Mock(side_effect=AssertionError("没有 auto 源码不得启动原生进程"))
    monkeypatch.setattr(H, "_run_live", launch)
    result = H._auto_adapter("q", "继续", ctx=H.HarnessCtx(user_id="alice"))
    assert result["status"] == "unavailable", result
    assert "run_auto_thin" in result["error"]
    launch.assert_not_called()
    assert "auto" not in H._STANDALONE_ADAPTERS


def test_omnigent_timeout_preserves_native_checkpoint(monkeypatch, tmp_path):
    H = _load_registry()
    import importlib
    mh = os.path.join(os.path.dirname(H.__file__), "meyo_harnesses")
    if mh not in sys.path:
        sys.path.insert(0, mh)
    ob = importlib.import_module("backends.omnigent_backend")

    class FakeBridge:
        def __enter__(self):
            return True

        def __exit__(self, *_args):
            return False

    native_cli = tmp_path / "omnigent"
    native_cli.write_text(f"#!{sys.executable}\n# native console fixture\n")
    native_cli.chmod(0o755)
    monkeypatch.setattr(ob, "_omnigent_bin", lambda: str(native_cli))
    monkeypatch.setattr(ob, "_aigc_model_appid", lambda _model: ("model", "appid"))
    monkeypatch.setattr(H, "_codex_bridge_ctx", lambda _port: FakeBridge())
    monkeypatch.setattr(H, "_run_task", lambda *args, **kwargs: (_ for _ in ()).throw(
        subprocess.TimeoutExpired(args[0], 1, output="OMNIGENT_CONVERSATION_ID=conv-timeout")))
    result = ob.run("q", "task")
    assert result["status"] == "timeout"
    assert result["native_checkpoint"] == {
        "kind": "omnigent_conversation", "session_id": "conv-timeout"}


def test_hermes_adapter_loads_native_session_history_and_preserves_timeout_checkpoint(monkeypatch, tmp_path):
    H = _load_registry()
    captured = {}
    # This test targets the source/SDK branch, not whichever user CLI happens
    # to be installed. Only source discovery is a fixture; execution is the
    # captured native command below.
    registry_path = tmp_path / "harness_baselines" / "meyo-orchestrator" / "harness_registry.py"
    source = registry_path.parent.parent / "hermes-agent"
    source.mkdir(parents=True)
    (source / "run_agent.py").write_text("# source-discovery fixture; never executed\n")
    monkeypatch.setattr(H, "__file__", str(registry_path))

    def fake_run(cmd, timeout, **kwargs):
        # Native launch may append a usage-output path after the Python source.
        # Inspect the real -c payload, not whichever argument happens to be last.
        assert "-c" in cmd
        captured["code"] = cmd[cmd.index("-c") + 1]
        return type("Result", (), {"returncode": 0,
                                   "stdout": __import__('json').dumps({
                                       "final": "完成", "status": "ok", "model_used": "m",
                                       "type": "result", "hermes_adapter_result": True,
                                       "native_checkpoint": {"kind": "hermes_session", "session_id": "sid"},
                                       "resumed_native": True}), "stderr": ""})()

    monkeypatch.setattr(H, "_run_live", fake_run)
    result = H._hermes_adapter("q", "继续", ctx=H.HarnessCtx(
        model="m", appid="a", memory_dir=str(tmp_path / "hermes-memory"),
        session_id="fresh", task_memory={"native_checkpoint": {
            "kind": "hermes_session", "session_id": "sid"}}))
    assert "get_messages_as_conversation" in captured["code"]
    assert "conversation_history=history" in captured["code"]
    assert result["native_checkpoint"]["session_id"] == "sid"
    assert result["resumed_native"] is True


def test_builtin_manifest_matches_real_replay_and_compaction_semantics():
    import runtime_manifest
    specs = runtime_manifest.autonomy_specs()
    assert specs["auto"]["resume"] == "checkpoint"
    assert specs["deepagents"]["resume"] == "cold_replay"
    assert specs["deepagents"]["compaction"] == "none"
    assert specs["deepagents"]["usage"] == "native_if_reported"
    assert specs["deepseek"]["compaction"] == "native_events"
    assert specs["hermes"]["integration"] == "sdk-subprocess"
    assert specs["deepagents"]["integration"] == "sdk-subprocess"


def test_prompt_cache_key_is_private_and_session_scoped():
    H = _load_registry()
    key = H.prompt_cache_key("alice@example.com", "session-42", "codex")
    assert key == H.prompt_cache_key("alice@example.com", "session-42", "codex")
    assert key != H.prompt_cache_key("bob@example.com", "session-42", "codex")
    assert key != H.prompt_cache_key("alice@example.com", "session-43", "codex")
    assert key != H.prompt_cache_key("alice@example.com", "session-42", "hermes")
    assert "alice" not in key and "session-42" not in key
    assert len(key) <= 64
    assert H.prompt_cache_key("", "session-42", "codex") == ""


def test_stable_prompt_prefix_survives_dynamic_turn_changes():
    H = _load_registry()
    stable = "STABLE-CAPABILITIES-" * 80
    first = H._legion_prompt("第一轮问题", H.HarnessCtx(
        stable_context=stable, runtime_brief="动态简报A", task_memory={"round": 1}))
    second = H._legion_prompt("第二轮问题", H.HarnessCtx(
        stable_context=stable, runtime_brief="动态简报B", task_memory={"round": 2}))
    marker = "\n\n【运行时现场简报"
    assert first.split(marker, 1)[0] == second.split(marker, 1)[0]
    assert stable in first and stable in second
    assert first.index(stable) < first.index("动态简报A") < first.index("第一轮问题")


def test_ctx_model_replaces_global_and_prior_attempts_flow():
    """ctx.model 真取回(不撞 _UNIFIED_MODEL 全局单值)+ 失败回流真拼进 prompt。"""
    H = _load_registry()
    env = {
        "MEYO_UNIFIED_MODEL": "ctx-picked-model",
        "MEYO_UNIFIED_APPID": "app-x",
        "MEYO_SESSION_ID": "penguin:qX",
        "MEYO_TRIED_MODELS": '["bad-model"]',
        "MEYO_PRIOR_ATTEMPTS": '[{"attempt":1,"model":"bad-model","status":"rc1","error":"boom"}]',
    }
    with mock.patch.dict(os.environ, env):
        ctx = H.HarnessCtx.from_env()
        assert ctx.model == "ctx-picked-model"          # per-call 值,非全局单值
        assert ctx.session_id == "penguin:qX"
        assert ctx.tried_models == ["bad-model"]
        assert len(ctx.prior_attempts) == 1
        prelude = H._prior_attempts_prelude(ctx.prior_attempts)
        assert "bad-model" in prelude and "rc1" in prelude  # 失败真回流进 prompt 前缀
        assert H._prior_attempts_prelude([]) == ""          # 空回流零回归


def test_timeout_failure_context_keeps_partial_and_error():
    ctx = L._build_failure_context("", 1, "prime", "m", "timeout",
                                   "半成品路径 /tmp/part.csv", False,
                                   error="HTTP 429 full detail")
    assert "status=timeout" in ctx
    assert "半成品路径 /tmp/part.csv" in ctx
    assert "HTTP 429 full detail" in ctx


def _prompt_call_has_ctx(call):
    """检查 ctx 的实际参数槽，不让换行或其它参数里的 ctx 干扰判断。"""
    value = next((keyword.value for keyword in call.keywords if keyword.arg == "ctx"),
                 call.args[1] if len(call.args) > 1 else None)
    return isinstance(value, ast.Name) and value.id == "ctx"


def test_all_prompt_adapters_pass_ctx_to_unified_builder():
    """所有走统一协议的 adapter 必须把 ctx 真传入 `_legion_prompt`，不能静默退成 env 空值。"""
    H = _load_registry()
    source = Path(H.__file__).read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source, filename=H.__file__))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "_legion_prompt"]
    assert calls, "未找到 adapter prompt 调用"
    missing = [f"{call.lineno}: {ast.get_source_segment(source, call)}"
               for call in calls if not _prompt_call_has_ctx(call)]
    assert not missing, "仍有 adapter 漏传 ctx:\n" + "\n".join(missing)


@pytest.mark.parametrize(("source", "expected"), [
    ("_legion_prompt(question, ctx)", True),
    ("_legion_prompt(\n    question,\n    ctx,\n)", True),
    ("_legion_prompt(question, ctx=ctx)", True),
    ('f"native({_legion_prompt(question, ctx)!r})"', True),
    ("_legion_prompt(question)", False),
    ("_legion_prompt(_prior_attempts_prelude(ctx.prior_attempts) + question)", False),
    ("run_harness(ctx, _legion_prompt(\n    question\n))", False),
    ("_legion_prompt(question, None)", False),
    ("_legion_prompt(question, adapter_capabilities=ctx)", False),
    ("_legion_prompt(question)  # ctx 尚未传入", False),
])
def test_prompt_ctx_check_tracks_argument_not_formatting(source, expected):
    """多行、关键字及 f-string 不误报；外层或问题字符串里的 ctx 不冒充传参。"""
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "_legion_prompt"]
    assert len(calls) == 1
    assert _prompt_call_has_ctx(calls[0]) is expected


def test_runtime_prompt_contains_real_values_and_failure_history():
    """最终给 adapter 的 prompt 必须含本轮实际简报、workdir、源码入口和完整失败事实。"""
    H = _load_registry()
    ctx = H.HarnessCtx(
        runtime_brief="分工=prime负责核验；机器=Darwin arm64；画像=数据分析师；验收=生成xlsx",
        workdir="/tmp/legion-visible-workdir",
        peer_source_context="Peer source access:\n- /tmp/legion-visible-workdir/peer_sources/codex",
        prior_attempts=[{"attempt": 1, "model": "bad-model", "status": "rc1",
                         "fail_kind": "gateway", "error": "HTTP 429 full detail",
                         "actions": [{"tool": "fetch_url", "ok": False}],
                         "workdir": "/tmp/failed-workdir"}],
    )
    prompt = H._legion_prompt(H._prior_attempts_prelude(ctx.prior_attempts) + "本轮任务", ctx)
    for expected in ("prime负责核验", "Darwin arm64", "数据分析师", "生成xlsx",
                     "/tmp/legion-visible-workdir", "peer_sources/codex",
                     "HTTP 429 full detail", "fetch_url", "/tmp/failed-workdir"):
        assert expected in prompt, (expected, prompt[-3000:])


def test_quick_depth_keeps_harness_contract_without_heavy_context():
    quick = L._stable_prelude("quick")
    standard = L._stable_prelude("standard")
    assert "快速交付契约" in quick
    assert "upgrade" in quick and "standard" in quick
    assert "用户云真机现场" not in quick
    assert "军团共享工具箱" not in quick
    assert len(quick) < len(standard) / 4


def test_user_unit_and_memory_dir_downstream():
    """user 单元 + 记忆目录下放:ctx 真取回 user_id/memory_dir;缺省退回 default/空(零回归)。"""
    H = _load_registry()
    env = {
        "MEYO_UNIFIED_MODEL": "m",
        "MEYO_SESSION_ID": "hermes:q",
        "MEYO_USER_ID": "alice",
        "MEYO_MEMORY_DIR": "/tmp/legion_mem/alice",
    }
    with mock.patch.dict(os.environ, env):
        ctx = H.HarnessCtx.from_env()
        assert ctx.user_id == "alice"
        assert ctx.memory_dir == "/tmp/legion_mem/alice"
    # 缺省(未设 user/memory env)→ default/空:未接记忆的家/老调用零回归
    clean = {k: v for k, v in os.environ.items()
             if k not in ("MEYO_USER_ID", "MEYO_MEMORY_DIR")}
    with mock.patch.dict(os.environ, clean, clear=True):
        ctx2 = H.HarnessCtx.from_env()
        assert ctx2.user_id == "default"
        assert ctx2.memory_dir == ""


def test_run_one_passes_user_and_memory_to_isolated():
    """run_one 把 user_id 归一成 memory_dir 并下放给 _run_harness_isolated(探测式·前向兼容)。"""
    captured = {}

    # 显式列出下放参数名(含 user_id/memory_dir):run_one 的前向兼容探测按具名参数判定被调方
    # 是否支持下放通道·**kwargs 桩会被判"老替身"而不传(那是保护老实现·非本测试意图)。
    def _fake_isolated(key, task, qhash, timeout_s, model_override=None,
                       appid_override=None, session_id="", user_id="default",
                       memory_dir="", tried_models=None, prior_attempts=None,
                       runtime_brief=""):
        captured.update({"user_id": user_id, "memory_dir": memory_dir,
                         "session_id": session_id, "runtime_brief": runtime_brief})
        return {"status": "ok", "final": "ok", "model_used": "m"}

    with mock.patch.object(L, "_ORCH_OK", True), \
         mock.patch.object(L, "_harness_keys", return_value={"hermes"}), \
         patch_exec(_fake_isolated):
        L.run_one("hermes", "task", qhash="request-99", user_id="alice",
                  conversation_id="conversation-7", runtime_brief="本轮真实简报值")

    assert captured.get("user_id") == "alice", captured
    assert captured.get("runtime_brief") == "本轮真实简报值", captured
    assert captured.get("session_id") == "hermes:conversation-7", captured
    assert "memory" in (captured.get("memory_dir") or ""), captured
    assert "alice" in (captured.get("memory_dir") or ""), captured


if __name__ == "__main__":
    test_run_one_reports_adapter_actual_model()
    test_prompt_cache_key_is_private_and_session_scoped()
    test_stable_prompt_prefix_survives_dynamic_turn_changes()
    test_all_ctx_adapters_accept_ctx_and_are_backward_compatible()
    test_responses_bridge_only_synthesizes_codex_shell_without_client_tools()
    test_omnigent_runtime_failure_is_not_model_retryable()
    test_run_one_stops_after_non_retryable_harness_runtime_failure()
    test_ctx_model_replaces_global_and_prior_attempts_flow()
    test_timeout_failure_context_keeps_partial_and_error()
    test_all_prompt_adapters_pass_ctx_to_unified_builder()
    test_runtime_prompt_contains_real_values_and_failure_history()
    test_user_unit_and_memory_dir_downstream()
    test_run_one_passes_user_and_memory_to_isolated()
    print("8/8 PASS · 全10家ctx契约 + 运行时简报/失败回流/可达源码/workdir + user记忆")
