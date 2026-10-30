#!/usr/bin/env python3
"""deliverable_router · 2026-08-12 · 通用化 Phase2 · 交付四路由 + M-93动作门

用户方向:真实用户的交付物不止一个短答案(GAIA)·还有报告/文件/动作/持续监控。
deliverable 字段(TaskSpec·Phase0已建)路由到四种收尾器:
  fact     一个短答案(GAIA)——复用内核 finalize·行为不变
  artifact 报告/表格/文件——留接口(未来接 nocode_delivery)
  action   发消息/预订/改外部状态(最危险)——过 M-93 动作门
  monitor  持续监控("盯着票价")——登记(挂 cron 是 Phase3)

★M-93 动作门(核心·最危险要做对):副作用动作必须过 {可逆性, 影响半径, 用户确认} 三件套。
铁律:**不可逆动作永远要人按确认·agent 无权自作主张**。这是基础设施(CC建·用户配阈值)·
不是 agent 自律——和"agent不给自己造手铐"不冲突:门是环境规则·由人配阈值。

全在 run_task 层调用·内核 run_auto_agent 一字不改。fact/GAIA 走透传=165等价严格不破。
"""

# 交付物类型(与 task_spec 同源)
FACT, ARTIFACT, ACTION, MONITOR = "fact", "artifact", "action", "monitor"


def action_gate(action_desc, reversibility="unknown", impact_radius="unknown"):
    """M-93 动作门:判副作用动作要不要人确认。返回 {allow, needs_confirm, reason, audit}。

    reversibility: reversible(可撤销·如草稿) | irreversible(不可撤销·如删除/付款/发出) | unknown
    impact_radius: self(只影响自己) | small(几个人/一个外部系统) | large(广播/多系统) | unknown
    铁律:不可逆 或 拿不准 → 永远要人确认(agent无权自作主张)。
    """
    rev = str(reversibility or "unknown").lower()
    imp = str(impact_radius or "unknown").lower()
    reasons = []
    needs_confirm = False
    # ① 不可逆 → 永远要确认(铁律)
    if rev == "irreversible":
        needs_confirm = True
        reasons.append("不可逆动作(agent无权自作主张·必须人确认)")
    # ② 拿不准可逆性 → 保守当不可逆(拿不准=要确认)
    elif rev == "unknown":
        needs_confirm = True
        reasons.append("可逆性未知·保守要人确认")
    # ③ 影响半径大 → 要确认(即便可逆·广播/多系统影响大)
    if imp == "large":
        needs_confirm = True
        reasons.append("影响半径大(广播/多系统)")
    elif imp == "unknown":
        needs_confirm = True
        reasons.append("影响半径未知·保守要人确认")
    # ④ 可逆+自己+小 → 可放行(仍记审计)
    if not needs_confirm:
        reasons.append("可逆+影响小·放行(已记审计)")
    audit = {"action": str(action_desc)[:200], "reversibility": rev,
             "impact_radius": imp, "needs_confirm": needs_confirm}
    return {"allow": not needs_confirm, "needs_confirm": needs_confirm,
            "reason": "·".join(reasons), "audit": audit}


def route_delivery(spec, final, trace):
    """按 spec.deliverable 路由收尾。返回 (final, trace)·可能改写 trace 加路由标记。
    fact/GAIA 走透传(行为不变·165等价)·其余标记待办(现在系统无外部集成·立骨架)。
    fail-safe:任何异常都退回透传(不阻断交付)。"""
    try:
        deliverable = getattr(spec, "deliverable", FACT)
    except Exception:
        deliverable = FACT

    if not isinstance(trace, dict):
        return final, trace  # trace 非 dict(异常态)·直接透传

    if deliverable == FACT:
        # GAIA + 默认:内核 finalize 已产出短答案·透传·零变化
        return final, trace

    if deliverable == ARTIFACT:
        # 报告/文件:留接口(未来接 nocode_delivery.build_one)·现在标 pending
        trace["delivery_route"] = "artifact"
        trace["artifact_pending"] = True  # 提示上层:该走文件生成+生成后自检
        return final, trace

    if deliverable == ACTION:
        # 副作用动作:过 M-93 门。默认保守(unknown/unknown→要确认)·
        # 除非 spec.context 显式给了 reversibility/impact_radius。
        ctx = getattr(spec, "context", {}) or {}
        gate = action_gate(
            action_desc=getattr(spec, "raw_input", "")[:200],
            reversibility=ctx.get("reversibility", "unknown"),
            impact_radius=ctx.get("impact_radius", "unknown"),
        )
        trace["delivery_route"] = "action"
        trace["action_gate"] = gate
        if gate["needs_confirm"]:
            # 不执行·返回确认请求(agent无权自作主张·把决定权交回用户)
            trace["action_pending"] = True
            trace["confirm_prompt"] = f"这是一个副作用动作·需要你确认后才执行:{gate['reason']}"
        return final, trace

    if deliverable == MONITOR:
        # 持续监控:登记(挂 cron 是 Phase3)·现在标 registered
        trace["delivery_route"] = "monitor"
        trace["monitor_registered"] = True
        trace["monitor_target"] = getattr(spec, "raw_input", "")[:200]
        return final, trace

    return final, trace  # 未知 deliverable·透传


if __name__ == "__main__":
    # 自测:M-93门(不可逆永远确认)+四路由
    # ① 不可逆→要确认
    g1 = action_gate("删除生产数据库", "irreversible", "large")
    assert g1["needs_confirm"] and not g1["allow"], "不可逆必须要确认"
    print("✅ M-93 不可逆动作:", g1["reason"])
    # ② 拿不准→保守要确认
    g2 = action_gate("发条消息", "unknown", "unknown")
    assert g2["needs_confirm"], "拿不准保守要确认"
    print("✅ M-93 未知可逆性:", g2["reason"])
    # ③ 可逆+自己+小→放行
    g3 = action_gate("保存草稿到本地", "reversible", "self")
    assert g3["allow"] and not g3["needs_confirm"], "可逆+自己+小可放行"
    print("✅ M-93 可逆小动作:", g3["reason"])

    # 路由(mock spec)
    class _S:
        def __init__(s, d, ctx=None):
            s.deliverable = d; s.raw_input = "x"; s.context = ctx or {}
    # fact透传(GAIA等价·trace不加路由标)
    _, t_fact = route_delivery(_S(FACT), "ans", {})
    assert "delivery_route" not in t_fact, "fact透传·不改trace(GAIA等价)"
    print("✅ fact路由:透传·GAIA等价不破")
    # action过门(默认unknown→要确认)
    _, t_act = route_delivery(_S(ACTION), "ans", {})
    assert t_act.get("action_pending") and t_act["action_gate"]["needs_confirm"], "action默认要确认"
    print("✅ action路由:过M-93门·默认要确认(不执行)")
    # action可逆小→放行
    _, t_act2 = route_delivery(_S(ACTION, {"reversibility": "reversible", "impact_radius": "self"}), "ans", {})
    assert not t_act2.get("action_pending"), "可逆小动作放行"
    print("✅ action路由:可逆+自己→放行")
    # monitor登记
    _, t_mon = route_delivery(_S(MONITOR), "ans", {})
    assert t_mon.get("monitor_registered"), "monitor登记"
    print("✅ monitor路由:登记")
    print("\n✅✅ deliverable_router 全通(M-93不可逆永远确认·fact透传保GAIA等价)")
