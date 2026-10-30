# -*- coding: utf-8 -*-
"""captain 工具层回归 —— 验证工具客观、无内置编排规则 (2026-08-25 重写)

铁律:orchestrator 只提供 list/read_history/read_trace/run 工具 + 无建议的契约。
不得含 recommend / _is_simple / 固定 reviewer / 拆解规则 / 固定 captain。
"""
import sys, os
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orchestrator as O


def test_list_is_objective():
    """list_harnesses 给客观身份+可用态·无能力评分/排序/推荐。"""
    lst = O.list_harnesses()
    # 9 原生 + ecosystem 后端 adapter(cua/supervision/herdr/orca/omnigent…)。
    # 军团纲领=能纳入就纳入,家数只增不减,不写死具体数字(否则每加一家就假红)。
    assert len(lst) >= 13, [r["key"] for r in lst]
    for row in lst:
        assert {"key", "display_name", "cli_hint", "available"} <= set(row), row
        assert isinstance(row["available"], bool), row


def test_no_hardcoded_routing():
    """★铁律:orchestrator 不得含路由/判断规则。"""
    import re as _re
    src = "\n".join(ln.split("#", 1)[0] for ln in open(O.__file__).read().splitlines())
    src = _re.sub(r'""".*?"""', "", src, flags=_re.S)   # 剥 docstring/注释
    banned = ["def recommend", "def _is_simple", "def pick_reviewer",
              "cost_tier", "strong_on", "task_kind in", 'return "penguin"',
              'return "prime"', "def plan("]
    hit = [b for b in banned if b in src]
    assert not hit, f"orchestrator 代码仍含硬编码编排(该删): {hit}"


def test_contract_gives_autonomy_not_recipe():
    """契约只给目标+工具+'你自主决定',不给固定流程/角色/数量/停止规则。"""
    c = O.autonomous_captain_contract("查那首viral歌")
    assert c["goal"] == "查那首viral歌"
    assert set(c["tools"]) >= {"list_harnesses", "read_harness_history", "run_harness"}
    instr = c["instruction"]
    assert "自主决定" in instr and "不要依赖预设" in instr
    # 契约不得规定"派penguin/先拆N步/必须复核"这类
    for banned in ("penguin", "先拆", "必须复核", "reviewer=", "3步"):
        assert banned not in instr, f"契约不该规定固定编排: {banned}"


def test_read_history_objective():
    """read_harness_history 原样给客观字段·不总结谁强谁弱。"""
    # auto 有历史 trace(全错题里有)
    rows = O.read_harness_history("auto", limit=5)
    if rows:
        for r in rows:
            assert set(r) >= {"qid", "hit", "status", "trace_path"}, r
            # 不含任何"评分/推荐"派生字段
            assert "score" not in r and "recommend" not in r


def test_run_harness_calls_pool():
    """run_harness 真调 HARNESSES·不自动加reviewer/重试(那由模型决定)。"""
    fake = {"prime": lambda qh, q, g: {"final": "Mando", "status": "ok"}}
    with mock.patch.object(O, "_load_harnesses", return_value=fake):
        out = O.run_harness("prime", "task?", "qh1", "Mando")
    assert out["harness"] == "prime" and out["final"] == "Mando"
    # 只调一次·无隐式 reviewer 步骤


def test_run_unknown_harness_safe():
    fake = {"prime": lambda *a: {"final": "x"}}
    with mock.patch.object(O, "_load_harnesses", return_value=fake):
        out = O.run_harness("nonexist", "t")
    assert out["status"] == "unknown_harness" and "available" in out


if __name__ == "__main__":
    tests = [test_list_is_objective, test_no_hardcoded_routing,
             test_contract_gives_autonomy_not_recipe, test_read_history_objective,
             test_run_harness_calls_pool, test_run_unknown_harness_safe]
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
