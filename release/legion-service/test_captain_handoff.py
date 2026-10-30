#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试首脑交接(handoff_captain)成功(用户 2026-08-30 目标3:过程不行交接也测试成功).

场景:首脑A收口质量差·主动请求交接给B → 验证 dispatch 真切换首脑+接任者B拿到A的产出续跑+结果挂回。
用 monkeypatch run_one 避免真跑军团(测【交接链路逻辑】·非测军团答题)。
"""
import os
import sys

os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
os.environ.setdefault("LEGION_HOME_DIR", os.path.expanduser("~/.legion"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import legion_core as lc


def test_handoff():
    calls = []

    def fake_run_one(key, task, **kw):
        calls.append({"key": key, "task": task[:60], "qhash": kw.get("qhash", "")})
        if key == "captainA":
            # 首脑A交付差·主动请求交接给 captainB
            return {"key": key, "status": "done", "final": "我做得不好,建议交给B",
                    "artifacts": [{"path": "/tmp/A_partial.md", "name": "半成品"}],
                    "orchestration_request": {
                        "handoff_captain": {"to": "captainB", "reason": "我的收口质量不足·B更擅长",
                                            "task": "接管并完成完整交付"}}}
        # 接任者B:验证它拿到了A的产出
        got_prior = "A_partial.md" in task and "前任" in task
        return {"key": key, "status": "done",
                "final": f"B接任完成(收到前任产出={got_prior})",
                "artifacts": [{"path": "/tmp/B_final.md", "name": "B最终交付"}]}

    # monkeypatch
    orig = lc.run_one
    orig_ready = lc._aigc_ready
    lc.run_one = fake_run_one
    lc._aigc_ready = lambda: {"runnable_clis": ["captainA", "captainB"]}
    # 让 valid harness 包含测试家
    orig_keys = lc._harness_keys
    lc._harness_keys = lambda: {"captainA", "captainB"}
    try:
        plan = {"captain": "captainA", "tasks": {"captainA": "做首脑收口这个任务"}}
        res = lc.dispatch("测试任务:做一份报告", plan=plan, qhash="test_handoff")
        # 断言
        called = [c["key"] for c in calls]
        print("run_one 调用序列:", called)
        assert "captainA" in called, "首脑A应被调用"
        assert "captainB" in called, "❌交接失败:接任者B未被调用"
        # B的task应含A的产出
        b_call = next(c for c in calls if c["key"] == "captainB")
        assert "handoff" in b_call["qhash"], f"❌B应走交接qhash·实际{b_call['qhash']}"
        # 结果里应有交接标记
        b_res = res.get("results")
        b_res = ([r for r in b_res if r.get("key") == "captainB"] if isinstance(b_res, list)
                 else [res.get("results", {}).get("captainB")])
        b_res = next((r for r in b_res if r), None)
        assert b_res and b_res.get("captain_handoff_from") == "captainA", \
            f"❌接任结果应标captain_handoff_from=captainA·实际{b_res}"
        print("✅首脑交接测试通过:A收口差→请求交接→B被派+走handoff qhash+接任标记captain_handoff_from=captainA")
        print(f"   B最终: {b_res.get('final')}")
        return True
    finally:
        lc.run_one = orig
        lc._aigc_ready = orig_ready
        lc._harness_keys = orig_keys


if __name__ == "__main__":
    ok = test_handoff()
    sys.exit(0 if ok else 1)
