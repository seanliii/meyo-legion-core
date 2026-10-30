"""原单曾翻源码才找到模型 CLI；公共发现与执行应复用同一入口。"""
import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import pytest

SERVICE = Path(__file__).resolve().parents[1]
MODEL_IDS = {"runnable:model_catalog", "runnable:model_chat", "runnable:model_decisions"}


@pytest.fixture(autouse=True)
def case(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("MEYO_", "LEGION_", "AIGC_", "OPENAI_", "ANTHROPIC_", "CODEX_")):
            monkeypatch.delenv(key)
    for key, leaf in (("HOME", "home"), ("MEYO_LEGION_DIR", "data"),
                      ("LEGION_HOME_DIR", "legion"), ("LEGION_PROC_REGISTRY_DIR", "registry")):
        path = tmp_path / leaf
        path.mkdir()
        monkeypatch.setenv(key, str(path))
    monkeypatch.syspath_prepend(str(SERVICE))
    cli = importlib.import_module("media_model_cli")
    tools = importlib.import_module("legion_tool")
    catalog = importlib.import_module("capability_catalog")
    spec = importlib.util.spec_from_file_location("model_discovery_toolbox", SERVICE / "toolbox.py")
    toolbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(toolbox)
    model_source = tmp_path / "models.json"
    model_source.write_text(json.dumps({
        "_meta": {"last_verified": "2026-09-02"},
        "programming": {"candidates": [
            {"model": "fixture-model", "rank": 1, "status": "PASS",
             "appids": ["fixture-secret"], "note": "private fixture-secret note"},
            {"model": "fixture-second", "rank": 2, "status": "PASS",
             "appids": ["fixture-second-secret"]},
        ]},
    }))
    monkeypatch.setenv("MEYO_MODEL_CATALOG_PATH", str(model_source))
    monkeypatch.setenv("MEYO_MEDIA_GATEWAY", "http://127.0.0.1:1/no-network")
    import external_agent_registry as registry
    empty = tmp_path / "external.json"
    empty.write_text('{"agents":[]}')
    monkeypatch.setattr(registry, "CATALOG", empty)
    monkeypatch.setattr(toolbox, "_read_cached_toolbox", lambda: None)
    monkeypatch.setattr(toolbox, "_schedule_refresh", lambda: None)
    monkeypatch.setattr(toolbox, "skill_source_identity", lambda: {"fixture": True})
    monkeypatch.setattr(tools, "_load_toolbox", toolbox.scan_toolbox)
    monkeypatch.setattr(tools, "_record_tool_usage", lambda *a, **kw: {})
    monkeypatch.setattr(tools, "_search_mcp", lambda *a, **kw: [])
    monkeypatch.setattr(cli, "_request", lambda *a, **kw: pytest.fail("unexpected model request"))
    media = importlib.import_module("media_tools")
    native_bounded = media._bounded_media_process
    def isolated_cli(command, timeout_s, input_text=None, **kwargs):
        result = cli.run_model_tool(command[-1], json.loads(input_text))
        return (0 if result.get("ok") else 1), json.dumps(result, ensure_ascii=False)
    monkeypatch.setattr(media, "_bounded_media_process", isolated_cli)
    return SimpleNamespace(cli=cli, tools=tools, catalog=catalog, toolbox=toolbox,
                           patch=monkeypatch, source=model_source, media=media,
                           native_bounded=native_bounded)


def model_rows(snapshot):
    return {row["id"]: row for row in snapshot["items"] if row["id"] in MODEL_IDS}


def test_capability_entry_exposes_invocable_tools_not_live_model_claims(case):
    rows = {row["id"]: row for row in case.catalog.inventory()["discovery"]}
    assert {"model_catalog", "model_chat", "model_decisions"} <= rows.keys()
    for name in ("model_catalog", "model_chat", "model_decisions"):
        assert f"runnable:{name}" in rows[name]["invoke"]
        assert rows[name]["live_verified"] is False
    assert '"questions"' in rows["model_decisions"]["invoke"]
    assert '"profile_id"' in rows["model_decisions"]["invoke"]
    assert '"prompt"' not in rows["model_decisions"]["invoke"]
    assert "fixture-secret" not in json.dumps(rows)


def test_cold_and_complete_scan_share_same_model_tool_declarations(case):
    cold = model_rows(case.toolbox.scan_toolbox())
    assert set(cold) == MODEL_IDS
    for row in cold.values():
        assert row["runnable"] is True and row["health"] == "unknown"
    complete = model_rows({"items": case.toolbox._scan_shared_web_tools()})
    assert complete == cold
    assert "max_tokens" in cold["runnable:model_chat"]["params"]


def test_existing_cached_snapshot_cannot_hide_new_builtin_tools(case):
    cached = {"items": [{"id": "legacy:reference", "name": "reference", "runnable": False}],
              "summary": {"total": 1, "runnable_tools": 0}, "scanned_at": 1}
    before = copy.deepcopy(cached)
    case.patch.setattr(case.toolbox, "_read_cached_toolbox", lambda: cached)
    current = case.toolbox.scan_toolbox()
    assert set(model_rows(current)) == MODEL_IDS
    assert current["summary"]["total"] == 1 + len(MODEL_IDS)
    assert current["summary"]["runnable_tools"] == len(MODEL_IDS)
    assert cached == before


@pytest.mark.parametrize("name", ["model_chat", "model_decisions"])
def test_real_search_exposes_model_call_and_schema_without_a_probe(case, name):
    import tool_semantic_index
    case.patch.setattr(tool_semantic_index, "scores", lambda items, query: ([0.0] * len(items), "none"))
    found = case.tools.search(name, top=8)
    row = next((r for r in found["tools"] if r["id"] == "runnable:" + name), None)
    assert row is not None
    expected = {"prompt", "model"} if name == "model_chat" else {"profile_id", "state", "questions"}
    assert expected <= set(row["params"])
    assert row["execution"]["health"] == "unknown"


def test_catalog_tool_reads_declared_source_without_credential_or_network(case):
    result = case.tools.run_tool("runnable:model_catalog", {"domain": "programming"})
    assert result["ok"] is True
    assert result["live_verified"] is False
    assert result["source_last_verified"] == "2026-09-02"
    assert [r["model"] for r in result["candidates"]] == ["fixture-model", "fixture-second"]
    text = json.dumps(result)
    assert "fixture-secret" not in text and "private" not in text and "appids" not in text


def test_chat_tool_preserves_selected_model_budget_and_scoped_usage(case):
    requests = []
    def request(direction, endpoint, payload, timeout_s, *, model=None):
        requests.append((direction, endpoint, payload, timeout_s, model))
        return {"model": "fixture-second", "usage": {
            "prompt_tokens": 12, "completion_tokens": 9, "total_tokens": 21,
            "completion_tokens_details": {"reasoning_tokens": 5},
            "private": "fixture-secret",
        }, "choices": [{"finish_reason": "stop", "message": {
            "content": "真实答案", "reasoning_content": "不可输出的内部内容"}}]}
    case.patch.setattr(case.cli, "_request", request)
    events = []
    result = case.tools.run_tool("runnable:model_chat", {
        "prompt": "公开测试问题", "domain": "programming", "model": "fixture-second",
        "max_tokens": 2048, "timeout_s": 30,
    }, event_cb=events.append)
    assert result["ok"] is True and result["answer"] == "真实答案"
    assert result["configured_model"] == result["reported_model"] == "fixture-second"
    assert requests[0][2]["max_tokens"] == 2048 and requests[0][4] == "fixture-second"
    assert result["usage"]["completion_tokens"] == 9
    assert result["usage_scope"] == "single_model_tool_request"
    text = json.dumps(result, ensure_ascii=False)
    assert "fixture-secret" not in text and "不可输出" not in text
    assert [e["t"] for e in events] == ["tool_started", "tool_end"]


def test_output_limit_returns_real_failure_and_usage_not_an_empty_success(case):
    case.patch.setattr(case.cli, "_request", lambda *a, **kw: {
        "model": "fixture-model", "usage": {"completion_tokens": 100},
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
    })
    result = case.tools.run_tool("runnable:model_chat", {
        "prompt": "测试", "domain": "programming", "max_tokens": 100})
    assert result["ok"] is False and result["isError"] is True
    assert result["error"] == "media_output_limit"
    assert result["finish_reason"] == "length"
    assert result["usage"]["completion_tokens"] == 100


def test_gateway_failure_does_not_echo_body_or_request_credentials(case):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("https://secret.invalid/key", 429,
                                     "fixture-secret", {}, None)
    case.patch.setattr(case.cli, "_request", fail)
    result = case.tools.run_tool("runnable:model_chat", {"prompt": "测试", "domain": "programming"})
    assert result["ok"] is False and result["error"] == "media_http_error:429"
    assert "fixture-secret" not in json.dumps(result) and "secret.invalid" not in json.dumps(result)


@pytest.mark.parametrize("extra", [
    {"max_tokens": 0}, {"max_tokens": True}, {"max_tokens": 3.5},
    {"timeout_s": float("nan")}, {"gateway": "https://unapproved.invalid"},
    {"appid": "unapproved-key"},
])
def test_invalid_call_or_credential_overrides_are_rejected_before_network(case, extra):
    result = case.tools.run_tool("runnable:model_chat", {
        "prompt": "测试", "domain": "programming", **extra})
    assert result["ok"] is False
    assert result["error"] in {"media_budget_invalid", "media_timeout_invalid", "media_tool_parameters_invalid"}


def test_model_call_obeys_existing_billable_permission(case):
    result = case.tools.run_tool("runnable:model_chat", {
        "prompt": "测试", "domain": "programming", "_capability_policy": {"billable": False}})
    assert result["ok"] is False
    assert result["authorization"]["action"] == "billable"
    assert result["error"] == "CAPABILITY_DENIED:billable"


def test_invalid_credential_input_is_rejected_before_lifecycle_logging(case):
    events = []
    result = case.tools.run_tool("runnable:model_chat", {
        "prompt": "测试", "appid": "must-not-be-logged"}, event_cb=events.append)
    assert result["ok"] is False
    assert events == []


def test_model_tool_uses_real_registered_child_and_offline_http(case, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "body": body,
                         "correct_key": self.headers["Authorization"] == "Bearer fixture-second-secret"})
            output = json.dumps({"model": "fixture-second", "usage": {"total_tokens": 7},
                                 "choices": [{"finish_reason": "stop",
                                              "message": {"content": "offline proof"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    case.patch.setenv("MEYO_MEDIA_GATEWAY", f"http://127.0.0.1:{server.server_port}/v1")
    case.patch.setattr(case.media, "_bounded_media_process", case.native_bounded)
    try:
        result = case.tools.run_tool("runnable:model_chat", {
            "prompt": "测试", "domain": "programming", "model": "fixture-second", "max_tokens": 2048})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert result["ok"] is True and result["answer"] == "offline proof"
    assert result["configured_model"] == "fixture-second" and result["usage"]["total_tokens"] == 7
    assert len(seen) == 1 and seen[0]["correct_key"]
    assert seen[0]["path"] == "/v1/chat/completions"
    assert seen[0]["body"]["model"] == "fixture-second"
    assert seen[0]["body"]["max_tokens"] == 2048


@pytest.mark.parametrize("cancel", [False, True])
def test_existing_process_boundary_stops_a_waiting_child(case, tmp_path, cancel):
    import sys
    import time
    import subprocess
    started = time.monotonic()
    check = (lambda: time.monotonic() - started > 0.2) if cancel else None
    expected = InterruptedError if cancel else subprocess.TimeoutExpired
    with pytest.raises(expected):
        case.native_bounded(
            [sys.executable, "-c", "import time; time.sleep(10)"], 0.4,
            cancel_check=check)
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("declared", [False, True])
def test_borrowed_catalog_uses_bound_source_not_host_authorization(case, tmp_path, declared):
    import sys
    bound = tmp_path / "bound.json"
    bound.write_text(json.dumps({"programming": {"candidates": [
        {"model": "bound-model", "status": "PASS", "appids": ["bound-private"]}]}}))
    fields = {"MEYO_MODEL_CATALOG_PATH": str(bound)} if declared else {}
    runtime = SimpleNamespace(
        _INPROC_RUNTIME_ENV=SimpleNamespace(values=fields, inherit_process_context=False),
        _INPROC_CANCEL_CHECK=SimpleNamespace(check=None))
    case.patch.setitem(sys.modules, "harness_registry", runtime)
    case.patch.setattr(case.media, "_bounded_media_process", case.native_bounded)
    result = case.media.run_shared_model_tool("model_catalog", {"domain": "programming"})
    if declared:
        assert result["ok"] is True
        assert [row["model"] for row in result["candidates"]] == ["bound-model"]
    else:
        assert result["ok"] is False
        assert result["error"] == "media_model_catalog_unavailable"
    assert "fixture-model" not in json.dumps(result) and "bound-private" not in json.dumps(result)


@pytest.mark.parametrize("inherit", [False, True])
def test_real_tool_thread_preserves_its_explicit_model_binding(case, tmp_path, inherit):
    import legion_core  # noqa: F401 -- resolves the existing runtime module
    import harness_registry as registry
    bound = tmp_path / "thread-bound.json"
    bound.write_text(json.dumps({"programming": {"candidates": [
        {"model": "thread-bound-model", "status": "PASS", "appids": ["thread-private"]}]}}))
    case.patch.setattr(registry._INPROC_RUNTIME_ENV, "values",
                       {"MEYO_MODEL_CATALOG_PATH": str(bound)}, raising=False)
    case.patch.setattr(registry._INPROC_RUNTIME_ENV, "inherit_process_context", inherit, raising=False)
    case.patch.setattr(case.media, "_bounded_media_process", case.native_bounded)
    result = case.tools.run_tool("runnable:model_catalog", {"domain": "programming"})
    assert result["ok"] is True
    assert [row["model"] for row in result["candidates"]] == ["thread-bound-model"]


def test_untrusted_finish_metadata_cannot_escape_in_result(case):
    case.patch.setattr(case.cli, "_request", lambda *a, **kw: {
        "model": "fixture-model", "choices": [{
            "finish_reason": {"debug": "private-fixture-secret"},
            "message": {"content": "answer"}}]})
    result = case.tools.run_tool("runnable:model_chat", {"prompt": "测试", "domain": "programming"})
    assert result["ok"] is False and result["error"] == "media_finish_reason_invalid"
    assert "private-fixture-secret" not in json.dumps(result)


def test_invalid_stdin_encoding_is_rejected_before_spawn(case, monkeypatch):
    monkeypatch.setattr(case.media.subprocess, "Popen",
                        lambda *a, **kw: pytest.fail("invalid input must not spawn"))
    with pytest.raises(UnicodeEncodeError):
        case.native_bounded(["unused"], 2, input_text="\ud800")


def test_cancel_callback_error_still_reaps_its_child(case, monkeypatch):
    import os
    import signal
    import sys
    children = []
    original = case.media.subprocess.Popen
    def spawn(*args, **kwargs):
        proc = original(*args, **kwargs)
        children.append(proc)
        return proc
    monkeypatch.setattr(case.media.subprocess, "Popen", spawn)
    checks = []
    def cancelled():
        checks.append(True)
        if len(checks) > 1:
            raise ValueError("callback failure")
        return False
    try:
        with pytest.raises(ValueError):
            case.native_bounded([sys.executable, "-c", "import time;time.sleep(10)"], 1,
                                cancel_check=cancelled)
        assert children and children[0].poll() is not None
    finally:
        for proc in children:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate(timeout=2)
