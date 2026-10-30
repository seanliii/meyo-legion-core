# -*- coding: utf-8 -*-
"""置信级联架构回归 —— 锁定"级联真实超过最强单家"这一有据结论。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cascade_eval as C


def test_cascade_beats_best_single_across_seeds():
    """多个 CV seed 下,CC→DeepAgents 置信级联均 > CC 17/40(稳健,非偶然)。"""
    for seed in (1, 7, 42, 123, 2024):
        r = C.run(seed=seed)
        assert r["cc_base"] == 17 and r["n"] == 40
        assert r["cascade_total"] > r["cc_base"], (seed, r["cascade_total"])
        assert r["beats_single"] is True


def test_runtime_cascade_keeps_good_primary_and_escalates_bad():
    sys.path.insert(0, str(Path(__file__).resolve().parent / "meyo_harnesses"))
    import cascade as CX
    def good(h,t,q,g):
        return {"final":"A"*300,"status":"ok","search_count":12,"elapsed":300}
    a=CX.run_cascade("t",run_fn=good)
    assert a["chosen"]=="cc" and not a["escalated"]
    def bad(h,t,q,g):
        if h=="cc": return {"final":"","status":"wall_timeout","search_count":50,"elapsed":1200}
        return {"final":"recovered","status":"ok","search_count":20,"elapsed":200}
    b=CX.run_cascade("t",run_fn=bad)
    assert b["chosen"]=="deepagents" and b["escalated"] and b["final"]=="recovered"


def test_cascade_uses_only_observable_features():
    """失败检测器只用可观测特征(无 gold/hit 泄漏)。"""
    r = C.run(seed=42)
    assert set(r["detector_features"]) == {"search_count", "elapsed", "final_len", "timeout"}
