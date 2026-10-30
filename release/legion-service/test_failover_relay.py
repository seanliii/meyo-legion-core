#!/usr/bin/env python3.12
"""补位接力回归锁(用户2026-08-28核心:一家坏了没交付·别家补队接力修复)。

验证 dispatch 的补位接力链路(monkeypatch run_one·只验编排调度·不碰 agent 内部):
- 一家失败→别家带【完整失败context】接力·这一单不白卷(用户中心命门)
- 多轮接力(A挂→B挂→C成)
- 无人可接时如实失败·不假成功
- 首家就交付时零浪费·不触发接力

跑法:python3.12 release/legion-service/test_failover_relay.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import legion_core as lc

_orig_run_one = lc.run_one
_orig_ready = lc._aigc_ready


def _restore():
    lc.run_one = _orig_run_one
    lc._aigc_ready = _orig_ready


def test_relay_success_with_failctx():
    """codex 失败 → cc 接力成功;接力家必须看得见上一家的失败 context(不失忆重跑)。"""
    calls = []
    lc._aigc_ready = lambda: {"runnable_clis": ["codex", "cc", "hermes"]}

    def fake(key, task, qhash="adhoc", cb=None, preferred_model=None,
             preferred_appid=None, attempt_timeout=None, direction="programming", **kwargs):
        calls.append({"key": key, "has_failctx": "没能交付" in task})
        if key == "codex":
            return {"key": "codex", "status": "error", "final": "", "error": "网关429·attempt用尽", "attempts": 3}
        return {"key": key, "status": "done",
                "final": f"[{key}接力]补上的真实交付内容超过二十字确保算真交付。", "attempts": 1}
    lc.run_one = fake

    events = []
    def cb(ev):
        if ev.get("agent") == "commander" and ev.get("t") in ("reason", "result"):
            events.append(ev.get("x", ""))

    r = lc.dispatch("复杂调研交付", {"tasks": {"codex": "负责调研这块"}}, cb=cb, mode="pk")["results"][0]
    assert calls == [{"key": "codex", "has_failctx": False}, {"key": "cc", "has_failctx": True}], calls
    assert r.get("status") == "done" and r.get("relayed_from") == "codex" and r.get("relay_by") == "cc", r
    assert lc._is_delivered(r)
    assert any("接力成功" in e for e in events)


def test_relay_keeps_failed_members_existing_artifact_in_final_delivery():
    """prime 在 rc1 前已产 HTML；接力家复验成功后，原成品和接力 lineage 必须留在最终结果。"""
    lc._aigc_ready = lambda: {"runnable_clis": ["prime", "cc"]}
    received = []
    artifact = {"path": "/tmp/already-built.html", "name": "already-built.html",
                "url": "/home-outputs/prime/already-built.html", "kind": "web",
                "provenance": {"verified": True, "sha256": "a" * 64}}

    def fake(key, task, **kwargs):
        if key == "prime":
            return {"key": key, "status": "error", "final": "", "error": "rc1",
                    "artifacts": [artifact], "partial": True}
        received.append(task)
        return {"key": key, "status": "done", "final": "已真实打开已有 HTML 并完成交付。",
                "artifacts": []}

    lc.run_one = fake
    out = lc.dispatch("制作离线网页", {"tasks": {"prime": "制作 HTML"}}, mode="pk")
    result = out["results"][0]
    assert received and "/tmp/already-built.html" in received[0]
    assert [item["name"] for item in result["artifacts"]] == ["already-built.html"]
    assert result["artifacts"][0]["lineage"]["handoffs"][-1] == {
        "from": "prime", "to": "cc", "reason": "failure_relay"}
    assert result["via_relay"] is True and out["delivered_count"] == 1


def test_relay_multi_round():
    """codex 挂 → cc 也挂 → hermes 才成(多轮接力·_MAX_FAILOVER 默认 2)。"""
    lc._aigc_ready = lambda: {"runnable_clis": ["codex", "cc", "hermes"]}
    seq = []

    def fake(key, task, **kw):
        seq.append(key)
        if key in ("codex", "cc"):
            return {"key": key, "status": "error", "final": "", "error": f"{key}挂了", "attempts": 3}
        return {"key": key, "status": "done", "final": f"[{key}]真交付超过二十字的实质内容确保通过。", "attempts": 1}
    lc.run_one = fake

    r = lc.dispatch("q", {"tasks": {"codex": "t"}}, mode="pk")["results"][0]
    assert seq == ["codex", "cc", "hermes"], seq
    assert r.get("relay_by") == "hermes" and lc._is_delivered(r), r


def test_no_relay_available():
    """只有 codex 一个可跑家·失败后无人可接·如实失败·绝不假成功。"""
    lc._aigc_ready = lambda: {"runnable_clis": ["codex"]}
    lc.run_one = lambda key, task, **kw: {"key": key, "status": "error", "final": "", "error": "codex挂了", "attempts": 3}
    r = lc.dispatch("q", {"tasks": {"codex": "t"}}, mode="pk")["results"][0]
    assert r.get("status") != "done" and not lc._is_delivered(r) and not r.get("relay_by"), r


def test_first_delivers_no_waste():
    """首家就交付·不触发任何接力(零浪费)。"""
    lc._aigc_ready = lambda: {"runnable_clis": ["codex", "cc"]}
    n = [0]

    def fake(key, task, **kw):
        n[0] += 1
        return {"key": key, "status": "done", "final": "一次就交付的真实内容超过二十字确保算真交付。", "attempts": 1}
    lc.run_one = fake
    r = lc.dispatch("q", {"tasks": {"codex": "t"}}, mode="pk")["results"][0]
    assert n[0] == 1 and not r.get("relay_by") and lc._is_delivered(r), (n[0], r)


def test_short_valid_answer_not_relayed():
    """context-not-control 回归:合法短答案(如"2")是真交付·父层不得以长度门误杀触发接力。

    根因复盘:_is_delivered 曾用"至少20字"规则替用户判断质量·会把正确短答案判成没交付去烧第二家
    (control·违反给context不加control + 死路黑名单"加门")。修后只信 run_one status=done+final非空。
    """
    lc._aigc_ready = lambda: {"runnable_clis": ["codex", "cc"]}
    n = [0]

    def fake(key, task, **kw):
        n[0] += 1
        return {"key": key, "status": "done", "final": "2", "attempts": 1}  # 短·但正确·真交付
    lc.run_one = fake
    r = lc.dispatch("1+1=?", {"tasks": {"codex": "算这道题"}}, mode="pk")["results"][0]
    assert n[0] == 1, f"短答案不该触发接力·实际调用 {n[0]} 次"
    assert not r.get("relay_by") and lc._is_delivered(r), r


if __name__ == "__main__":
    tests = [test_relay_success_with_failctx,
             test_relay_keeps_failed_members_existing_artifact_in_final_delivery,
             test_relay_multi_round, test_no_relay_available, test_first_delivers_no_waste,
             test_short_valid_answer_not_relayed]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"✅ {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"❌ {t.__name__}: {e}")
        finally:
            _restore()
    print(f"\n{'全通过' if not failed else f'{failed} 个失败'}({len(tests) - failed}/{len(tests)})")
    sys.exit(1 if failed else 0)
