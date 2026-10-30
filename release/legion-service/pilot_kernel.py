#!/usr/bin/env python3
"""Pilot 纯续航内核。

无文件 IO、无 hook、无 print。给它回复+状态+用户配置,它只回答:
该继续还是该停、为什么、下一步提示。Claude Code hook 和军团按钮共用这一套。

设计原则:默认继续完成目标;只在真完成/真阻塞/真等待/真需用户决策时停。
不靠脆弱的"是否捕获到测试命令"卡门;context 不 control。
"""
from __future__ import annotations

import re
from typing import Any

DEFAULT_CONFIG = {
    "enabled": True,
    "intensity": "focused",  # focused=没干完就继续; gentle=只拦明显假停顿
    # 续航【范围】(用户 2026-08-28 goal-C·pilot 按 session/query/turn 自选范围动态调整):
    #   turn   =每一轮都盯(最激进·单轮说没完就继续)
    #   query  =盯到【这个 query】真完成才放行(单轮"完成"但 query 目标没达成→仍继续·防假停)
    #   session=盯到【整个 session 目标】达成才放行(跨多 query·最持久)
    # 范围越大·对"完成"越严(要看更大目标有没有真推进·不被单轮假完成骗停)。默认 query:
    # 比 turn 不那么啰嗦、又能防"单轮说完就停但用户这一单没真拿到东西"的假停。
    "scope": "query",
    "catch": {
        "ask_instead_of_act": True,
        "investigation_only": True,
        "promise_without_action": True,
        "decision_without_test": True,
    },
}

# 真正允许停的信号
_DONE = re.compile(r"(?:已完成|完成并验证|测试通过|验证通过|全部通过|done|completed|all tests pass)", re.I)
# 实质交付/满意证据(军团"迭代到满意·跑通率≠满意度"):真完成通常伴随交付物或真实验证证据。
# 光一句"完成了"没有任何落地痕迹 → 视为空泛声明,不放行(继续:拿出交付物或真验证再说完成)。
_DELIVERED = re.compile(
    r"(?:\.(?:html|py|js|md|xlsx|csv|json|pdf|mp4|png)|outputs/|交付|已生成|已保存|"
    r"代码|方案|清单|报告|链接|下载|http|字|行|满意|通过验证|真实测试|AB\s*数据|hit|命中)", re.I)
_INCOMPLETE = re.compile(r"(?:仍未|尚未|还没|没有完成|未完成|下一步(?:不应|应该|是|要)|推荐落地|接下来(?:还|要)|not yet|still not|next step)", re.I | re.S)
_BLOCKED = re.compile(r"(?:无权|权限不足|无法(?:访问|继续|进行)|真正阻塞|卡住了.*(?:无法|进不)|blocked|permission denied|需要你(?:提供|授权|给).*(?:才能|否则))", re.I)
_WAITING = re.compile(r"(?:正在等待|等待.*(?:完成|结果|响应).*(?:再|才)|挂起等|waiting on|still running)", re.I)
_DECISION = re.compile(r"(?:需要你(?:选择|决定|确认)|请你选择|which .*do you prefer|need your decision)", re.I)
_STILL_GOING = re.compile(r"(?:我先(?:做|干|去)|继续(?:做|干|推进)|不影响|同时|一边.*一边|先处理别的|already|proceed)", re.I)

# 四类假停顿
_FAKE_ASK = re.compile(r"(?:要我(?:做|继续|干|改|删|加)吗|要不要(?:我)?(?:继续|做|开始)|需要我.*吗|该.*还是.*[?？]|我该怎么|你(?:想|要)我(?:怎么|做))", re.I)
_JUST_TALK = re.compile(r"(?:你(?:说得|批评得)对|这次我(?:认真|一定)|我这就|接下来我(?:会|要|来)|让我先|我来(?:扒|查|看|扒到根))", re.I)
_INVESTIGATE_STOP = re.compile(r"(?:[:：]\s*$|先(?:看|查|扒|读|列)[一下]*(?:下)?$)", re.I)

_INVESTIGATION_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "Agent"}


def merge_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """合并用户配置;未知字段忽略,缺失用默认。"""
    out = {"enabled": DEFAULT_CONFIG["enabled"], "intensity": DEFAULT_CONFIG["intensity"],
           "scope": DEFAULT_CONFIG["scope"], "catch": dict(DEFAULT_CONFIG["catch"])}
    if not isinstance(overrides, dict):
        return out
    if isinstance(overrides.get("enabled"), bool):
        out["enabled"] = overrides["enabled"]
    if overrides.get("intensity") in {"focused", "gentle"}:
        out["intensity"] = overrides["intensity"]
    if overrides.get("scope") in {"turn", "query", "session"}:
        out["scope"] = overrides["scope"]
    if isinstance(overrides.get("catch"), dict):
        for key in out["catch"]:
            if isinstance(overrides["catch"].get(key), bool):
                out["catch"][key] = overrides["catch"][key]
    return out


def classify(reply: str, state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """纯判断:返回 action=continue/stop、kind、reason、next_action。

    stop 的 kind:turn_returned/done/blocked/waiting/user_decision/disabled
    continue 的 kind:ask_instead_of_act/promise_without_action/investigation_only/incomplete/unfinished
    """
    cfg = merge_config(config)
    text = reply or ""
    if not cfg["enabled"]:
        return {"action": "stop", "kind": "disabled", "reason": "用户关闭了持续运行"}

    # 原生成员已经表达的继续意图，不是父层根据回答措辞作出的质量判决。
    # 它只请求原调用方按既有预算继续；不批准 next_plan，也不变成首脑编排。
    native = state.get("native_continuation")
    if (isinstance(native, dict)
            and native.get("source") == "orchestration_request.coordinate"
            and native.get("action") == "continue"):
        return {
            "action": "continue", "kind": "native_continue",
            "reason": native.get("reason") or "原生成员明确要求继续",
            "next_action": (
                "原生成员已明确请求继续；在原目标、现有权限和运行预算内，"
                "接着已有成果与原生上下文完成剩余工作。"),
        }

    # 明确的完成声明与本轮可返回是并列事实，不能互相替代。
    # 没有这些字段的旧 hook 仍走下面原有逻辑。
    coordination = state.get("captain_coordination")
    requests_continue = (isinstance(coordination, dict)
                         and coordination.get("action") == "continue")
    can_return = (state.get("delivered") is True
            and state.get("scope_progressed") is not False
            and not state.get("all_failed")
            and not state.get("verification_incomplete")
            and not state.get("contract_incomplete")
            and not requests_continue)
    if can_return and state.get("scope_goal_met") is True:
        return {"action": "stop", "kind": "done",
                "reason": f"调用方报告目标已完成；该声明不等于独立验收(范围={cfg['scope']})"}
    if (can_return and state.get("delivery_return_ready") is True
            and state.get("scope_goal_met") is None):
        return {"action": "stop", "kind": "turn_returned",
                "reason": "本轮已交回结果，未收到继续请求；用户目标是否达标尚未确认"}

    fake_ask = cfg["catch"]["ask_instead_of_act"] and bool(_FAKE_ASK.search(text))
    just_talk = cfg["catch"]["promise_without_action"] and bool(_JUST_TALK.search(text))
    investigate_stop = cfg["catch"]["investigation_only"] and bool(_INVESTIGATE_STOP.search(text))
    fake = fake_ask or just_talk or investigate_stop
    still_going = bool(_STILL_GOING.search(text))
    incomplete = bool(_INCOMPLETE.search(text))
    # 军团"迭代到满意·跑通率≠满意度":真完成要么伴随交付物/验证证据,要么状态里已记 mutation/交付。
    # 光一句"完成了"没有落地痕迹=空泛声明,不放行(继续:拿出交付物或真验证)。
    delivered = bool(_DELIVERED.search(text)) or bool(state.get("mutation_seen")) or bool(state.get("delivered"))
    # 结构化运行事实优先于完成措辞：dispatch 已确认整个 scope 真闭合、有交付且有推进时，
    # 模型即使只自然回复文件路径（不说“已完成”）也应停止。反之只有口头完成仍按旧契约审查。
    factual_done = (state.get("scope_goal_met") is True and delivered
                    and state.get("scope_progressed") is not False
                    and not state.get("all_failed"))
    done = (bool(_DONE.search(text)) or factual_done) and not incomplete and not fake and delivered

    # 真停止优先(但假提问不能冒充真决策)
    if done:
        # ★ scope 动态范围(goal-C·防"没真正推动的假停"):范围越大·对"完成"越严。
        #   turn=单轮说完就放行(最松);query/session=还要看【这个更大目标真推进了】才放行——
        #   否则单轮说"完成"但整单/整会话目标没动=假停·继续。信号从 state 取(父层如实给·不猜):
        #   scope_goal_met=这个范围的目标是否达成(父层判/用户判);scope_progressed=有没有实质推进。
        scope = cfg.get("scope", "query")
        if scope in {"query", "session"}:
            goal_met = state.get("scope_goal_met")
            progressed = state.get("scope_progressed")
            # 父层【明确】说这个范围目标没达成 → 是假停·继续(单轮完成≠整单/整会话完成)。
            # 只在父层给了明确 False 时拦(给 context 让它判·没给信号就按单轮 done 放行·不臆断)。
            if goal_met is False or (progressed is False and goal_met is None):
                return {"action": "continue", "kind": "scope_not_met",
                        "next_action": f"这一轮像是完成了,但【{scope}】范围的目标还没真达成——"
                                       f"别停在单轮'完成',继续推进直到这个{'整单' if scope=='query' else '整会话'}目标真拿到成果"}
        return {"action": "stop", "kind": "done", "reason": f"已明确完成(有交付物/验证证据·范围={scope})"}
    # 交付型任务的【自证满足世界对标】门槛(2026-09-08用户钉死'满足了他还会自己看世界对标是不是不够'
    # '不是固定次数不是一次·模型理解后自然停止'):世界对标交付(有锚点/参照URL上下文)声称完成时,
    # 收口前必须【对照具体对标链接/图】自证——差距已关闭或明确可接受。父层不否决·只把对标事实
    # 喂回让模型自己再看一遍:够了就停,不够它自己继续。context loop 不是 control loop。
    _ref_ctx = state.get("benchmark_refs") or state.get("reference_links") or []
    if bool(_DONE.search(text)) and delivered and not incomplete and not fake and _ref_ctx:
        return {"action": "continue", "kind": "benchmark_self_check",
                "next_action": "收口前自己再看一眼世界对标（%s）:"
                               "你现在的成品和它并排比，差距真的关上了吗？第一眼/完整度/细节有没有明显低一档？"
                               "还有明显差距就继续迭代（换资产/取工具/学它的手法）；确实逼近到尽了才说明差距和原因后收尾。"
                               % "、".join(str(r)[:80] for r in _ref_ctx[:2])}
    # 声称完成但没有任何交付/验证痕迹 → 不停,要它拿出实质结果(军团满意度导向)
    if bool(_DONE.search(text)) and not incomplete and not fake and not delivered:
        return {"action": "continue", "kind": "claim_without_delivery",
                "next_action": "别只说完成——拿出交付物(文件/代码/方案/链接)或真实验证(测试/AB/命中)再收尾,军团看满意度不是跑通率"}
    if _DECISION.search(text) and not fake:
        return {"action": "stop", "kind": "user_decision", "reason": "确需用户决策"}
    if _BLOCKED.search(text) and not still_going:
        return {"action": "stop", "kind": "blocked", "reason": "真实阻塞"}
    if _WAITING.search(text) and not still_going:
        return {"action": "stop", "kind": "waiting", "reason": "明确等待"}

    # 假停顿给具体纠正动作
    if fake_ask:
        return {"action": "continue", "kind": "ask_instead_of_act",
                "next_action": "这是你自己能定的事,别问用户——回看目标直接做最合理的那个,做完再报结果"}
    if just_talk:
        return {"action": "continue", "kind": "promise_without_action",
                "next_action": "别光表态,立刻动手做你说要做的事,并查到根(上下游全看,别看一个就下结论)"}

    last_tool = str(state.get("last_tool", ""))
    investigation_only = last_tool in _INVESTIGATION_TOOLS and not state.get("mutation_seen")
    if investigate_stop or investigation_only:
        return {"action": "continue", "kind": "investigation_only",
                "next_action": "调查别停在半路:上下游全查到根,然后实施(工具执行不是进步,推动目标才是)"}
    if incomplete:
        return {"action": "continue", "kind": "incomplete",
                "next_action": "做完你刚说的下一步,别用说明代替行动"}

    if cfg["intensity"] == "gentle":
        return {"action": "stop", "kind": "gentle_pass", "reason": "温和模式未发现明显假停顿"}
    return {"action": "continue", "kind": "unfinished",
            "next_action": state.get("next_action") or "回看目标和上一步承诺,继续最直接的下一步"}
