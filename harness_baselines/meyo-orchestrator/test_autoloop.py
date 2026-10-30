# -*- coding: utf-8 -*-
"""autoloop 回归 —— 验证零编排逻辑 + 落盘 + 模型自主停 (2026-08-25)

gateway 调用全 mock(离线·不烧token)。
"""
import sys, os, json, re
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autoloop as A


def test_no_orchestration_logic():
    """★铁律:autoloop 不得含任何编排/路由判断,只有 tool-loop。"""
    src = "\n".join(ln.split("#", 1)[0] for ln in open(A.__file__).read().splitlines())
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    banned = ["def recommend", "task_kind", "cost_tier", "_is_simple",
              'if.*simple', "pick_reviewer", 'return "penguin"', 'return "prime"']
    hit = [b for b in banned if b in src]
    assert not hit, f"autoloop 含编排逻辑(该无): {hit}"


def test_shared_tools_wired():
    """MCP+skill 共享层必须同时出现在实现表与 function schema,模型才能真调。"""
    impl = set(A.TOOLS_IMPL)
    schema = {t["function"]["name"] for t in A.TOOLS_SCHEMA}
    assert impl == schema, (impl - schema, schema - impl)
    assert {"list_mcp_tools", "call_mcp_tool", "list_skills", "read_skill",
            "list_templates", "instantiate_template"} <= impl


def test_model_decides_stop():
    """模型不再调工具(无 tool_calls)→ loop 停并交卷。"""
    resp_stop = {"choices": [{"message": {"content": "最终答案: Mando"},
                              "finish_reason": "stop"}]}
    with mock.patch.object(A, "_post", return_value=resp_stop), \
         mock.patch.object(A, "_save_trace", return_value="/tmp/x.json"):
        r = A.autonomous_solve("q", verbose=False)
    assert r["final"] == "最终答案: Mando"
    assert r["n_steps"] == 0, "模型首轮就停"


def test_model_drives_tools_then_stops():
    """模型自主:第1轮调工具·第2轮停。loop 忠实执行不干预选择。"""
    resp1 = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "t1", "function": {"name": "list_harnesses", "arguments": "{}"}}]},
        "finish_reason": "tool_calls"}]}
    resp2 = {"choices": [{"message": {"content": "done: prime"},
                          "finish_reason": "stop"}]}
    posts = [resp1, resp2]
    with mock.patch.object(A, "_post", side_effect=posts), \
         mock.patch.object(A, "_save_trace", return_value="/tmp/x.json"), \
         mock.patch.dict(A.TOOLS_IMPL, {"list_harnesses": lambda **k: [{"key": "prime"}]}):
        r = A.autonomous_solve("q", verbose=False)
    assert r["final"] == "done: prime"
    assert len(r["steps"]) == 1 and r["steps"][0]["tool"] == "list_harnesses"


def test_trace_saved():
    """遵 CLAUDE.md:跑完落 trace(tool_log+final+status)。"""
    import tempfile, pathlib
    resp1 = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "t1", "function": {"name": "list_harnesses", "arguments": "{}"}}]}}]}
    resp2 = {"choices": [{"message": {"content": "ans"}}]}
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(A, "TRACE_DIR", pathlib.Path(td)), \
             mock.patch.object(A, "_post", side_effect=[resp1, resp2]), \
             mock.patch.dict(A.TOOLS_IMPL, {"list_harnesses": lambda **k: ["x"]}):
            r = A.autonomous_solve("qgoal", verbose=False)
        tp = r.get("trace_path")
        assert tp and os.path.exists(tp), "应落 trace 文件"
        saved = json.load(open(tp))
        assert saved["final"] == "ans" and saved["tool_log"] and saved["status"] == "done"


def test_budget_exhaustion_forces_final_synthesis():
    tool_turn = {"choices": [{"message": {"content": None, "tool_calls": [
        {"id": "t1", "function": {"name": "list_harnesses", "arguments": "{}"}}
    ]}}]}
    synthesis = {"choices": [{"message": {"content": "基于已有结果的最终答案"}}]}
    with mock.patch.object(A, "_post", return_value=tool_turn), \
         mock.patch.object(A.urllib.request, "urlopen") as opened, \
         mock.patch.object(A, "_save_trace", return_value="/tmp/x.json"), \
         mock.patch.dict(A.TOOLS_IMPL, {"list_harnesses": lambda **k: ["x"]}):
        opened.return_value.read.return_value = json.dumps(synthesis).encode()
        r = A.autonomous_solve("q", max_steps=1, verbose=False)
    assert r["status"] == "done_after_budget"
    assert r["final"] == "基于已有结果的最终答案"


def test_gateway_error_safe():
    """网关异常 fail-safe·落 trace·不崩。"""
    with mock.patch.object(A, "_post", side_effect=Exception("boom")), \
         mock.patch.object(A, "_save_trace", return_value="/tmp/x.json"):
        r = A.autonomous_solve("q", verbose=False)
    assert r["final"] == "" and "error" in r


def test_model_cannot_pass_qhash_gold():
    """★防串题+防作弊:模型调 run_harness 传的 qhash/gold 被忽略,用外层固定的。"""
    captured = {}
    def fake_run(harness, task, qhash="adhoc", gold=""):
        captured["qhash"] = qhash; captured["gold"] = gold
        return {"final": "x", "status": "ok"}
    A._SOLVE_CTX["qhash"] = "REAL_QID"; A._SOLVE_CTX["gold"] = "SECRET_GOLD"
    with mock.patch.object(A.O, "run_harness", side_effect=fake_run):
        # 模型恶意传别题 qhash + 泄漏 gold
        A._run_harness_tool("prime", "task", qhash="WRONG_QID", gold="CHEAT")
    assert captured["qhash"] == "REAL_QID", "必须用外层qhash·忽略模型传的"
    assert captured["gold"] == "SECRET_GOLD", "gold来自外层·非模型传的CHEAT"
    # schema 里不该暴露 qhash/gold 给模型
    rh = next(t for t in A.TOOLS_SCHEMA if t["function"]["name"] == "run_harness")
    props = rh["function"]["parameters"]["properties"]
    assert "gold" not in props and "qhash" not in props, "schema不该暴露gold/qhash"


def test_subagent_log_written():
    """子agent每次调用落独立完整日志(stdout/stderr/final/status·可追踪)。"""
    import tempfile, pathlib, time
    A._SOLVE_CTX["qhash"] = "QTEST"
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(A, "_SUB_LOG_DIR", pathlib.Path(td)), \
             mock.patch.object(A.O, "run_harness",
                 return_value={"harness": "prime", "final": "Mando",
                               "status": "ok", "stdout": "searched...", "stderr": ""}):
            out = A._run_harness_tool("prime", "find song")
        assert out["final"] == "Mando"
        logs = list(pathlib.Path(td).glob("QTEST_prime_*.json"))
        assert logs, "应落子agent日志"
        d = json.load(open(logs[0]))
        assert d["harness"] == "prime" and d["stdout"] == "searched..." and d["status"] == "ok"


def test_subagent_hard_timeout():
    """子agent卡死→硬超时熔断(治codex卡23分钟)·不永久阻塞。"""
    import tempfile, pathlib, time
    A._SOLVE_CTX["qhash"] = "QTO"
    def _hang(*a, **k):
        time.sleep(30)  # 模拟卡死
        return {"final": "late"}
    with tempfile.TemporaryDirectory() as td:
        # 隔离熔断状态,避免本会话其它跑把 codex 提前熔断而短路了本用例
        with mock.patch.object(A, "_SUB_LOG_DIR", pathlib.Path(td)), \
             mock.patch.object(A.H, "_BREAKER_FILE", pathlib.Path(td) / "b.json"), \
             mock.patch.object(A.H, "_STATE_DIR", pathlib.Path(td)), \
             mock.patch.object(A, "SUBAGENT_HARD_TIMEOUT", 1), \
             mock.patch.object(A.O, "run_harness", side_effect=_hang):
            t0 = time.time()
            out = A._run_harness_tool("codex", "task")
            el = time.time() - t0
        assert "HARD_TIMEOUT" in out["status"], f"应熔断: {out}"
        assert el < 40, f"熔断后应快速返回·不等满30s卡死: {el}"


def test_harness_stats_objective():
    """harness_stats 给客观聚合(命中率/中位耗时)·无'该选谁'推荐·不按优劣排序。"""
    s = A.O.harness_stats()
    assert isinstance(s, list) and s
    for r in s:
        assert set(r) >= {"harness", "n_samples", "hit_rate", "median_elapsed_s"}
        assert "recommend" not in r and "rank" not in r and "best" not in r
    # 按 harness 名排序(非按优劣)·避免暗示推荐
    names = [r["harness"] for r in s]
    assert names == sorted(names), "应按名排序·不按优劣"


def test_self_evolve_dispatch():
    """mode=self_evolve → 走 run_self_evolve(penguin/prime原生goal)·由模型自选。"""
    import tempfile, pathlib
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(A, "_SUB_LOG_DIR", pathlib.Path(td)), \
             mock.patch.object(A.O, "run_self_evolve",
                 return_value={"harness": "prime", "final": "evolved", "status": "ok",
                               "mode": "self_evolve"}) as se, \
             mock.patch.object(A.O, "run_harness",
                 return_value={"final": "single", "status": "ok"}) as rh:
            out = A._run_harness_tool("prime", "高层目标", mode="self_evolve")
        assert se.called and not rh.called, "self_evolve应走run_self_evolve"
        assert out["final"] == "evolved"


def test_self_evolve_unsupported_harness():
    """非penguin/prime用self_evolve → unsupported(模型改用single)。"""
    r = A.O.run_self_evolve("codex", "obj")
    assert r["status"] == "self_evolve_unsupported"


if __name__ == "__main__":
    tests = [test_no_orchestration_logic, test_shared_tools_wired,
             test_model_decides_stop,
             test_model_drives_tools_then_stops, test_trace_saved,
             test_gateway_error_safe, test_model_cannot_pass_qhash_gold,
             test_subagent_log_written, test_subagent_hard_timeout,
             test_harness_stats_objective, test_self_evolve_dispatch,
             test_self_evolve_unsupported_harness]
    passed = 0
    for t in tests:
        try:
            print(f"  ✅ {t()}"); passed += 1
        except AssertionError as e:
            print(f"  ❌ {t.__name__}: {e}")
        except Exception as e:
            print(f"  💥 {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} PASS")
    raise SystemExit(0 if passed == len(tests) else 1)
