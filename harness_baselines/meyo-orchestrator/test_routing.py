# -*- coding: utf-8 -*-
"""路由与数据完整性回归 —— 锁定"路由未超单家""报告已过时"两个真实结论。"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))


def test_topic_router_does_not_beat_best_single():
    import router_eval as R
    topic, hits = R._load()
    best_name, best_hits, n = R._best_fixed(hits)
    r_correct, r_n, _ = R._route_loo(topic, hits, best_name)
    # 事实:topic LOO 路由不超过最强固定单家(诚实锁定,防夸大)
    assert r_correct <= best_hits, (r_correct, best_hits)
    assert best_name == "cc" and n == 40


def test_fair_report_numbers_are_stale_vs_current_traces():
    """FINAL_REPORT 记 codex/deepseek 22/40,但当前 traces 重算远低于此 → 报告已过时。"""
    import router_eval as R
    _, hits = R._load()
    codex = sum(hits[q].get("codex", False) for q in hits)
    deepseek = sum(hits[q].get("deepseek", False) for q in hits)
    assert codex < 22 and deepseek < 22, (codex, deepseek)
    rep = (_HERE.parent / "fair_bench_40" / "FINAL_REPORT_40q_6harness.md").read_text()
    assert "22/40" in rep  # 报告仍是旧数字,证明确实过时
