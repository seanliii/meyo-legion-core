# -*- coding: utf-8 -*-
"""budget_policy · auto 工具预算、资源保护与策略提醒。

2026-08-27 从 qa_auto_agent god-file 抽出。默认 context not control:策略信号只提醒；
仅硬墙钟、极端重复、资源安全等真正运行保障可阻断。共享依赖由 auto 显式 configure
注入，本模块不反向 import god-file。
"""
import os
import time


def configure(**dependencies):
    globals().update(dependencies)


def _check_budget(session, tool_name, content_key=None):
    """在每次工具真正执行前调用，返回 (allow, guard_message)。

    content_key: 深度考据类工具(shell_exec/evolution_execute)传入本次调用的
        命令内容指纹——这类工具"每次命令都不同"是正常的多方考据,只有**完全
        相同的命令**连续重复才算空转,所以按内容判重而非按工具名(见
        _CONTENT_DEDUP_TOOLS 上方注释)。其余工具不传,沿用按工具名判重。

    guard_message 非空时，无论 allow 是 True/False，都应该把这段话作为
    这次工具调用的（部分）返回内容附带给模型——对应 CCB 的"nudge 消息"，
    让模型在下一轮决策时就已经知道"该收尾了"，而不是死等到硬顶才发现。
    """
    budget = session.get("_budget")
    if not budget:
        return True, ""

    budget["n_tool_calls"] += 1
    # (2026-08-15·#51 Steer双队列) 步间检查点消费用户中途插话:每次工具调用前 drain steer 队列·
    #   有则把用户的纠偏/补充【注入下一步】(活着纠偏·非死后收尸)·三态回写供 UI 显示。
    #   只注入提示不改核心态(不越权);单题最多消费 _STEER_MAX 条(防刷爆预算);全 fail-open。
    _steer_inject = ""
    try:
        _qid = session.get("qid") or ""
        if _qid:
            _steer_used = int(budget.get("_steer_consumed", 0) or 0)
            _STEER_MAX = int(os.environ.get("MEYO_STEER_MAX", "5"))
            if _steer_used < _STEER_MAX:
                import steer_queue as _sq
                _msgs = _sq.drain(_qid, max_items=min(3, _STEER_MAX - _steer_used))
                for _m in _msgs:
                    budget["_steer_consumed"] = _steer_used = _steer_used + 1
                    _txt = str(_m.get("text", ""))[:500]
                    _kd = _m.get("kind")
                    if _kd == "rethink":
                        # (E·回溯性重思) 用户对【之前某句话/判断】有疑虑·要求带【现在积累的全部上下文】
                        #   回头重审那个锚点·只修订该判断·不推翻后续别的、不打断当前进度(往回重审·非往后纠偏)。
                        _steer_inject += (
                            f"\n[🔄用户回溯重思·带最新上下文重审前面的判断] 用户对你之前的一个判断有疑虑:"
                            f"「{_txt}」\n请【带上你现在已经掌握的全部上下文/新证据】·回头重新审视这个具体判断"
                            f"(而不是当时的信息):它在现在看来还成立吗?若不成立·【只修订这一个判断】并说明"
                            f"'原判断→新判断·因为现在知道了X'·【不要推翻你后续别的正确工作·也不必从头重来】·"
                            f"改完继续当前进度。\n")
                        _emit_gate_event(_qid, "rethink", "applied", {"anchor": _txt[:60]})
                    else:
                        _steer_inject += (f"\n[⚡用户中途插话·请立即纳入] {_txt}\n")
                        _emit_gate_event(_qid, "steer", "applied", {"kind": _kd})
                    try:
                        _sq.mark(_qid, _m.get("steer_id", ""), "applied")
                    except Exception:
                        pass
                # 超限的:标 rejected(告诉 UI 没生效·别以为发了就等于生效)
                if _steer_used >= _STEER_MAX:
                    for _m in _sq.drain(_qid, max_items=99):
                        _sq.mark(_qid, _m.get("steer_id", ""), "rejected")
                        _emit_gate_event(_qid, "steer", "rejected", {"reason": "over_max"})
    except Exception:
        _steer_inject = ""  # steer 是增强·任何异常绝不阻断主流程
    # 记工具序列(反思脑要看agent已走的步骤·像真人回顾)·只留最近30个
    _seq = budget.setdefault("_tool_seq", [])
    _seq.append(tool_name)
    if len(_seq) > 30:
        del _seq[:len(_seq) - 30]
    # v72 · 数"真检索/考据类工具"的调用(判难题放弃前够不够努力)。放在放行
    # 之前累加没关系:这里是"打算调用"计数,循环卫兵拦下的极少数情况不影响
    # "是否真去外部查过"的整体判断(拦下的那次本就是重复空转)。
    if tool_name in _INVESTIGATIVE_TOOLS:
        budget["n_investigative_calls"] = budget.get("n_investigative_calls", 0) + 1

    # NativeEngine 是唯一生产引擎:工具选择、顺序、重试与收敛由模型+Native loop负责。
    # 旧路径下面23道启发式 blocker 原本在 _thin_mode 时就全部 bypass；现删除死分支，
    # 只保留计数和用户中途插话 context。资源保护由 NativeEngine 墙钟/调用上限和各工具自身 guard 承担。
    return True, _steer_inject
