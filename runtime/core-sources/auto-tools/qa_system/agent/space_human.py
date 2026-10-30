"""space_human · 2026-08-13 · 人类协作空间(能动)的 Space 适配器(挂 traversal_core)

第六个空间——最"像人"的能动空间:agent 卡住时【委托人类】(问人/让人帮忙查)。
与前五个空间(agent 自己读)的本质区别:这里 agent 不"读",而是【发出请求、等人回结果】。
  · 地址 = "谁能帮我"(human)
  · 动作 = delegate:把问题交给人,阻塞等人回文本答复,并入证据

复用现成 hitl_gate(不另造):request_approval 的登记 pending + 轮询等裁决机制,
用户在网页回复时把答复写进 decision(不只是 approved/denied,可带文本)。

这就是"像人一样带目标检索"的完整闭环:人卡住会求助,agent 也该会。
fail-safe:hitl 不可用/超时 → 明确回传"没问到人"(不静默假装拿到答案)。
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from traversal_core import Observation, Action

try:
    import hitl_gate as _hitl
except Exception:
    _hitl = None


class HumanSpace:
    name = "human"
    kind = "agentic"          # 能动空间:委托人类·不是单向读

    def __init__(self, qid: str = "human", *, timeout_s: float = 120.0):
        self.qid = qid
        self.timeout_s = timeout_s

    def entry_addrs(self) -> list[str]:
        return ["ask"]

    def observe(self, addr: str, query: str = "") -> Observation:
        # human 空间没有"读"——观测即"还没问人"。真正的交互在 act(delegate)。
        return Observation(addr="ask", content="",
                           kind="human", note="等待 delegate 向人提问")

    def act(self, action: Action) -> dict:
        """委托人类:把 query 交给人,阻塞等人回复,返回 {ok, answer}。"""
        q = action.query or "需要你帮忙提供信息"
        if _hitl is None:
            return {"ok": False, "detail": "[DEGRADED]hitl_gate 不可用·无法问人"}
        # 复用 request_approval 的等待机制;detail 带上问题,人回复写进 decision
        verdict = _hitl.request_approval(self.qid, "human_ask", q,
                                         timeout_s=self.timeout_s)
        decision = verdict.get("decision", "")
        # 人的答复:优先取 decision 里的文本;approved 但无文本则回传提示
        reply = verdict.get("reply") or verdict.get("note") or ""
        if decision == "timeout":
            return {"ok": False, "detail": f"[未问到人·超时{self.timeout_s}s] 问题: {q[:80]}"}
        if decision in ("denied", "rejected"):
            return {"ok": False, "detail": f"[人拒绝回答] {q[:80]}"}
        # approved 或带文本答复
        answer = reply or decision
        return {"ok": True, "answer": answer, "detail": f"人已回复: {str(answer)[:120]}"}


if __name__ == "__main__":
    # dry-run:验证 delegate 流程(不真等人·mock hitl)
    import types
    sp = HumanSpace("htest")
    g = globals()
    # 1) 人回复了文本
    g["_hitl"] = types.SimpleNamespace(
        request_approval=lambda *a, **k: {"decision": "approved", "reply": "答案是 bacon"})
    r = sp.act(Action(kind="delegate", target="human", query="12月8日故事提到什么肉?"))
    assert r["ok"] and "bacon" in r["answer"], f"人回复应拿到: {r}"
    print(f"delegate 拿到人类答复 ✓ · {r['detail']}")
    # 2) 超时没问到人 → fail-safe
    g["_hitl"] = types.SimpleNamespace(
        request_approval=lambda *a, **k: {"decision": "timeout"})
    r2 = sp.act(Action(kind="delegate", target="human", query="随便问"))
    assert not r2["ok"] and "超时" in r2["detail"], f"超时应fail-safe: {r2}"
    print(f"超时 fail-safe ✓ · {r2['detail'][:40]}")
    # 3) hitl 不可用 → 不静默
    g["_hitl"] = None
    r3 = sp.act(Action(kind="delegate", target="human", query="x"))
    assert not r3["ok"] and "DEGRADED" in r3["detail"], f"不可用应打标: {r3}"
    print(f"hitl不可用打标 ✓ · {r3['detail'][:40]}")
