# -*- coding: utf-8 -*-
"""云 UI API 测试(不烧模型 token:solve 内层 mock,但 HTTP/后台线程/落状态链是真实的)。"""
import os, sys, time
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
# 测试显式启用本地开发鉴权旁路;生产默认 fail-closed。
os.environ["MEYO_AUTH_DISABLED"] = "1"
os.environ["MEYO_RUN_DB"] = f"/tmp/meyo-test-runs-{os.getpid()}.db"
os.environ["MEYO_MANAGE_OMNIGENT_RUNTIME"] = "0"
from fastapi.testclient import TestClient
from cloud_ui import api

client = TestClient(api.app)


def test_health_and_discovery():
    h = client.get("/health")
    assert h.status_code == 200 and h.json()["tools"] == 15
    assert h.json()["runtime_management"]["user_action_required"] is False
    hs = client.get("/harnesses").json()
    assert {x["key"] for x in hs} == {x["key"] for x in api.O.list_harnesses()}
    assert {x["key"] for x in hs} >= {"auto", "cc", "codex", "omnigent"}
    assert len(client.get("/templates").json()) == 10
    assert len(client.get("/skills").json()) >= 2
    snap = client.get("/health/snapshot").json()
    assert "breakers" in snap and "circuits_open" in snap


def test_index_is_real_ui():
    r = client.get("/")
    assert r.status_code == 200
    assert "觅游 · Agent 小队" in r.text
    assert "开始编排" in r.text


def test_solve_async_lifecycle():
    fake = {"final": "验证答案", "n_steps": 1,
            "steps": [{"step": 0, "tool": "list_harnesses", "args": {}}],
            "trace_path": "/tmp/fake.json"}
    with mock.patch.object(api.AL, "autonomous_solve", return_value=fake):
        r = client.post("/solve", json={"goal": "测试目标", "executor": "autoloop"})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        for _ in range(200):
            d = client.get(f"/runs/{rid}").json()
            if d["status"] == "done":
                break
            time.sleep(.01)
    assert d["status"] == "done", d
    assert d["final"] == "验证答案" and d["n_steps"] == 1
    # API schema 根本不接受/传 gold;外部只能给 goal/qhash
    assert "gold" not in d


def test_empty_goal_rejected():
    r = client.post("/solve", json={"goal": "   "})
    assert r.status_code == 400


def test_unknown_run_404():
    assert client.get("/runs/nope").status_code == 404


def test_template_instantiate_endpoint():
    r = client.post("/templates/research-brief/instantiate",
                    json={"params": {"topic": "余额宝费率"}})
    assert r.status_code == 200
    d = r.json()
    assert "余额宝费率" in d["goal"] and d["missing_params"] == []
    assert client.post("/templates/nope/instantiate", json={"params": {}}).status_code == 404


def test_auth_and_user_isolation(monkeypatch):
    """生产鉴权 fail-closed;A 用户的 run 对 B 返回404(不泄漏存在性)。"""
    monkeypatch.setenv("MEYO_AUTH_DISABLED", "0")
    monkeypatch.setenv("MEYO_API_TOKENS", "tok-a:user-a,tok-b:user-b")
    assert client.get("/harnesses").status_code == 401
    ha = {"X-Meyo-Token": "tok-a"}; hb = {"X-Meyo-Token": "tok-b"}
    fake = {"final": "A答案", "n_steps": 0, "steps": [], "trace_path": ""}
    with mock.patch.object(api.AL, "autonomous_solve", return_value=fake):
        r = client.post("/solve", headers=ha, json={"goal": "A私有目标"})
        rid = r.json()["run_id"]
        for _ in range(200):
            if client.get(f"/runs/{rid}", headers=ha).json()["status"] == "done": break
            time.sleep(.01)
    assert client.get(f"/runs/{rid}", headers=ha).status_code == 200
    assert client.get(f"/runs/{rid}", headers=hb).status_code == 404
    assert rid not in {x["run_id"] for x in client.get("/runs", headers=hb).json()}


def test_retry_creates_new_persistent_run():
    fake = {"final": "retry-ok", "n_steps": 0, "steps": [], "trace_path": ""}
    with mock.patch.object(api.AL, "autonomous_solve", return_value=fake):
        first = client.post("/solve", json={"goal": "需重试", "executor": "autoloop"}).json()["run_id"]
        second = client.post(f"/runs/{first}/retry").json()["run_id"]
        assert second != first
        for _ in range(200):
            if client.get(f"/runs/{second}").json()["status"] == "done": break
            time.sleep(.01)
    assert client.get(f"/runs/{second}").json()["final"] == "retry-ok"


def test_cancel_is_cooperative():
    entered = __import__("threading").Event()
    release = __import__("threading").Event()
    def slow(goal, **kwargs):
        entered.set()
        # 模拟 loop 下一步检查 cancel_event
        ev = kwargs["cancel_event"]
        release.wait(1)
        if ev.is_set():
            return {"status": "interrupted", "interrupt_reason": "cancelled",
                    "final": "", "steps": [], "n_steps": 0, "trace_path": ""}
        return {"final": "late", "steps": [], "n_steps": 0}
    with mock.patch.object(api.AL, "autonomous_solve", side_effect=slow):
        rid = client.post("/solve", json={"goal": "取消测试", "executor": "autoloop"}).json()["run_id"]
        assert entered.wait(3)
        assert client.post(f"/runs/{rid}/cancel").json()["status"] == "cancelling"
        release.set()
        for _ in range(200):
            d = client.get(f"/runs/{rid}").json()
            if d["status"] == "interrupted": break
            time.sleep(.01)
    assert d["status"] == "interrupted" and d["interrupt_reason"] == "cancelled"


def test_cascade_executor_routes_and_records_escalation():
    fake={"final":"级联答案","status":"ok","chosen":"deepagents","escalated":True,
          "route":[{"harness":"cc"},{"harness":"deepagents"}]}
    with mock.patch.object(api.CX,"run_cascade",return_value=fake) as m:
        rid=client.post("/solve",json={"goal":"极限正确题","executor":"cascade"}).json()["run_id"]
        for _ in range(200):
            d=client.get(f"/runs/{rid}").json()
            if d["status"] in ("done","error"):break
            time.sleep(.01)
    assert m.called and d["executor"]=="cascade" and d["escalated"] and d["chosen"]=="deepagents"


def test_context_facts_include_history_memory_toolbox_permissions(monkeypatch):
    service_modules = {
        "legion_memory": type("M", (), {"search": staticmethod(
            lambda goal, top, user_id: {"results": [{"text": "memory"}]})}),
        "task_memory": type("T", (), {"context": staticmethod(
            lambda qhash, key: {"phase": "复检"})}),
        "toolbox": type("B", (), {"scan_toolbox": staticmethod(
            lambda: {"summary": {"total": 541}})}),
        "legion_permissions": type("P", (), {"capability_policy": staticmethod(
            lambda: {"write": True})}),
    }
    with mock.patch.dict(sys.modules, service_modules):
        facts = api._captain_context_facts("new", "local", "goal", "q", {
            "steps": [{"type": "event"}], "artifacts": [{"path": "x"}]
        })
    assert facts["long_term_memory"] == [{"text": "memory"}]
    assert facts["task_memory"]["phase"] == "复检"
    assert facts["toolbox"]["total"] == 541 and facts["permissions"]["write"]
    assert facts["artifacts"] == [{"path": "x"}]


def test_captain_executor_routes_to_mcp_captain():
    """executor=captain → 走 run_via_captain_mcp(真 Claude+我方 MCP 的可用路径)。"""
    fake = {"final": "场景模板 10 个", "status": "ok", "executor": "captain"}
    with mock.patch.object(api.OR, "run_via_captain_mcp", return_value=fake) as m:
        rid = client.post("/solve", json={"goal": "模板数量", "executor": "captain"}).json()["run_id"]
        for _ in range(200):
            d = client.get(f"/runs/{rid}").json()
            if d["status"] in ("done", "error"): break
            time.sleep(.01)
    assert m.called
    assert "首脑作战上下文" in m.call_args.kwargs["context_block"]
    assert d["status"] == "done" and d["executor"] == "captain" and "10" in d["final"]


def test_omnigent_incomplete_delivery_falls_back_to_autoloop():
    with mock.patch.object(api.OR, "run_via_omnigent", return_value={
        "final": "仍在运行", "status": "ok", "executor": "omnigent"
    }), mock.patch.object(api.AL, "autonomous_solve", return_value={
        "final": "降级后的完整交付答案", "status": "done", "steps": [], "n_steps": 1
    }) as fallback:
        rid = client.post("/solve", json={"goal": "需要完整结果", "executor": "omnigent"}).json()["run_id"]
        for _ in range(200):
            result = client.get(f"/runs/{rid}").json()
            if result["status"] in ("done", "error"):
                break
            time.sleep(.01)
    assert fallback.called and result["status"] == "done"
    assert result["fallback_from"] == "omnigent" and result["fallback_to"] == "autoloop"


def test_runtime_failure_is_internal_fallback_not_user_command(monkeypatch):
    monkeypatch.setattr(api, "_RUNTIME_ENABLED", True)
    monkeypatch.setattr(api._RUNTIME, "ensure_ready", lambda timeout_s: type("S", (), {
        "ready": False, "error": "host_start_timeout"
    })())
    with mock.patch.object(api.AL, "autonomous_solve", return_value={
        "final": "自动降级仍完成用户交付", "status": "done", "steps": [], "n_steps": 1
    }):
        rid = client.post("/solve", json={"goal": "不允许提示用户运行命令", "executor": "omnigent"}).json()["run_id"]
        for _ in range(200):
            result = client.get(f"/runs/{rid}").json()
            if result["status"] in ("done", "error"):
                break
            time.sleep(.01)
    assert result["status"] == "done"
    assert result["runtime_recovery"] == "host_start_timeout"
    assert "omnigent host" not in result["final"]


def test_invalid_executor_rejected():
    assert client.post("/solve", json={"goal": "x", "executor": "bogus"}).status_code == 400


def test_executor_status_and_omnigent_is_default():
    h = client.get("/health").json()
    ex = h["executors"]
    assert ex["autoloop"]["available"] and ex["captain"]["available"]
    assert ex["omnigent"]["available"] is True
    assert h["executor_default"] == "omnigent"
    with mock.patch.object(api.OR, "run_via_omnigent", return_value={
        "final": "原生编排完成", "status": "ok", "executor": "omnigent"
    }) as run:
        rid = client.post("/solve", json={"goal": "x"}).json()["run_id"]
        for _ in range(200):
            result = client.get(f"/runs/{rid}").json()
            if result["status"] in ("done", "error"):
                break
            time.sleep(.01)
    assert run.called and result["executor"] == "omnigent"
    assert "首脑作战上下文" in run.call_args.kwargs["context_block"]


def test_store_recovery_marks_running_interrupted(tmp_path):
    from cloud_ui.store import RunStore
    s = RunStore(tmp_path / "r.db")
    s.put({"run_id": "r1", "user_id": "u", "goal": "g", "status": "running"})
    s.recover_interrupted()
    r = s.get("r1", "u")
    assert r["status"] == "interrupted" and r["interrupt_reason"] == "server_restarted"
