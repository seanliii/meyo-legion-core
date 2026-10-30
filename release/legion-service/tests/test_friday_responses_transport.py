"""Friday 的协议选择、每请求授权和原生续会话；不请求真实网关。"""
import json
import os
from pathlib import Path
import sys
import tomllib
from types import SimpleNamespace

import pytest

SERVICE = Path(__file__).resolve().parents[1]
ORCH = SERVICE.parents[1] / "harness_baselines/meyo-orchestrator"
sys.path[:0] = [str(SERVICE), str(ORCH)]

import harness_registry as HR
import model_registry as MR


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for key, leaf in (("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
                      ("LEGION_HOME_DIR", "legion")):
        root = tmp_path / leaf
        root.mkdir()
        monkeypatch.setenv(key, str(root))
    monkeypatch.delenv("LEGION_CODEX_TRANSPORT", raising=False)
    monkeypatch.delenv("MEYO_PROMPT_CACHE_KEY", raising=False)
    monkeypatch.setenv("MEYO_UNIFIED_GATEWAY", "https://aigc.sankuai.com/v1/openai/native")


@pytest.mark.parametrize("model,path,provider", [
    ("GLM-5.3", "/agentic/v1", None),
    ("deepseek-v4-pro", "/agentic/v1", None),
    ("gpt-5.5", "/v1", "Azure"),
])
def test_friday_protocol_follows_documented_model_provider(model, path, provider):
    route = MR.responses_endpoint(model, "https://aigc.sankuai.com/v1/openai/native")
    assert route["base_url"] == "https://aigc.sankuai.com" + path
    assert route["http_headers"].get("x-context-provider") == provider


def test_explicit_gateway_not_replaced_by_default(monkeypatch):
    monkeypatch.setenv("MEYO_UNIFIED_GATEWAY", "https://aigc.sankuai.com/v1/openai/native")
    assert MR.responses_endpoint("GLM-5.3", "https://other.example/v1") is None
    route = MR.responses_endpoint("gpt-5.5", "https://aigc.sankuai.com/agentic/v1")
    assert route["base_url"].endswith("/agentic/v1")
    assert "x-context-provider" not in route["http_headers"]
    assert MR.responses_endpoint("GLM-5.3", "https://aigc.sankuai.com.evil.example/v1") is None


def capture_native(monkeypatch, calls):
    monkeypatch.setattr(HR._sh, "which", lambda _: "/test/native-codex")

    def no_bridge(*args, **kwargs):
        raise AssertionError("Friday 原生请求不得启动 Responses→Chat 转换桥")

    monkeypatch.setattr(HR, "_codex_bridge_ctx", no_bridge)

    def run(cmd, timeout, **kwargs):
        calls.append((cmd, kwargs))
        Path(cmd[cmd.index("-o") + 1]).write_text("原生结果")
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "type": "thread.started", "thread_id": "native-thread-1"}) + "\n")

    monkeypatch.setattr(HR, "_run_live", run)


def parse_config(cmd):
    return tomllib.loads("\n".join(cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-c"))


def test_adapter_preserves_native_context_credential_and_resume(monkeypatch, tmp_path):
    calls = []
    capture_native(monkeypatch, calls)
    monkeypatch.setenv("MEYO_APPID_ALIASES", "appid-selected=request-secret")
    monkeypatch.setenv("AIGC_APPID", "wrong-global-secret")
    ctx = HR.HarnessCtx(model="GLM-5.3", appid="appid-selected",
                        gateway="https://aigc.sankuai.com/v1/openai/native",
                        user_id="user-a", session_id="conversation-a",
                        prompt_cache_key="legion:v1:scoped",
                        workdir=str(tmp_path / "work"),
                        task_memory={"native_checkpoint": {"session_id": "native-thread-1"}})
    result = HR._codex_adapter("friday", "延续原任务", ctx=ctx)
    assert result["status"] == "ok"
    cmd, kw = calls[0]
    config = parse_config(cmd)
    provider = config["model_providers"][config["model_provider"]]
    assert provider["base_url"] == "https://aigc.sankuai.com/agentic/v1"
    assert provider["wire_api"] == "responses"
    assert provider["env_key"] == "AIGC_APPID"
    assert kw["env"]["AIGC_APPID"] == "request-secret"
    assert "request-secret" not in repr(cmd) + repr(result)
    assert provider["env_http_headers"]["Mt-Session-Id"] == "MEYO_FRIDAY_SESSION_ID"
    assert kw["env"]["MEYO_FRIDAY_SESSION_ID"] == "legion:v1:scoped"
    assert config["disable_response_storage"] is True
    assert cmd[cmd.index("-m") + 1] == ctx.model
    assert cmd[cmd.index("resume") + 1] == "native-thread-1"
    assert kw["cwd"] == str(tmp_path / "work")
    assert result["resumed_native"] is True


def test_unresolved_explicit_alias_does_not_fall_back_to_other_secret(monkeypatch, tmp_path):
    calls = []
    capture_native(monkeypatch, calls)
    monkeypatch.setenv("MEYO_APPID_ALIASES", "")
    monkeypatch.setenv("AIGC_APPID", "wrong-global-secret")
    result = HR._codex_adapter("missing", "task", ctx=HR.HarnessCtx(
        model="GLM-5.3", appid="appid-unresolved", workdir=str(tmp_path / "work")))
    assert calls == []
    assert result["status"] == "unavailable"
    assert "appid-unresolved" not in repr(result)
    assert "wrong-global-secret" not in repr(result)


def test_explicit_bridge_rollback_keeps_selected_gateway(monkeypatch, tmp_path):
    calls, bridge_calls = [], []
    capture_native(monkeypatch, calls)
    monkeypatch.setenv("LEGION_CODEX_TRANSPORT", "bridge")

    class Bridge:
        def __enter__(self):
            return True

        def __exit__(self, *args):
            return False

    def bridge(port, env=None):
        bridge_calls.append(env)
        return Bridge()

    monkeypatch.setattr(HR, "_codex_bridge_ctx", bridge)
    ctx = HR.HarnessCtx(model="GLM-5.3", appid="selected",
                        gateway="https://approved.example/v1",
                        workdir=str(tmp_path / "work"))
    result = HR._codex_adapter("bridge", "task", ctx=ctx)
    assert result["status"] == "ok"
    assert len(bridge_calls) == 1
    assert bridge_calls[0]["MEYO_UNIFIED_GATEWAY"] == ctx.gateway
    config = parse_config(calls[0][0])
    assert config["model_providers"][config["model_provider"]]["base_url"].startswith("http://127.0.0.1:")


def test_bridge_rollback_uses_a_chat_endpoint_not_responses_plus_chat():
    import importlib.util
    path = SERVICE.parents[1] / "harness_baselines/comparison/responses_chat_bridge.py"
    spec = importlib.util.spec_from_file_location("friday_fallback_bridge_test", path)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    for path in ("/v1", "/v1/responses", "/agentic/v1", "/agentic/v1/responses"):
        assert bridge._chat_upstream("https://aigc.sankuai.com" + path) == \
            "https://aigc.sankuai.com/v1/openai/native"
    assert bridge._chat_upstream("https://approved.example/v1") == "https://approved.example/v1"
    assert bridge._chat_upstream("https://approved.example/v1/chat/completions") == \
        "https://approved.example/v1"


def test_product_keeps_selected_response_transport(monkeypatch):
    import legion_core as legion
    from test_harness_ctx_contract import patch_exec

    def fake(*args, **kwargs):
        return {"status": "ok", "final": "好", "model_used": "GLM-5.3",
                "responses_transport": "friday_responses"}

    monkeypatch.setattr(legion, "_ORCH_OK", True)
    monkeypatch.setattr(legion, "_harness_keys", lambda: {"codex"})
    with patch_exec(fake):
        result = legion.run_one("codex", "回一个字：好", qhash="friday-transport",
                                preferred_model="GLM-5.3", preferred_appid="test-key")
    assert result["responses_transport"] == "friday_responses"
    assert result["final"] == "好"
