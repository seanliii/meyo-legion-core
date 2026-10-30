# -*- coding: utf-8 -*-
"""best_effort_answer · Kimi式"最可能答案"输出策略(烧进引擎·治撞墙就弃权/乱猜)

用户 /goal(Kimi 水瓶题输出策略): 没找到直接证据·但基于线索有合理推断时·别弃权也别
乱猜·而是像 Kimi 那样:
  ① 明确给出【最可能的答案】(Jordan Pickford)
  ② 说明【关联的推断依据】(2019欧国联点球战·皮克福德水瓶小抄习惯)
  ③ 诚实【披露不确定性】(没找到2019水瓶小抄直接照片·基于习惯+比赛性质推断)
  ④ 提供【相关背景】(2018世界杯/2024欧洲杯有更明确证据·防用户记错年份)

区别于纯"标存疑弃权":弃权=什么都不给(用户没收获);best-effort=给最可能答案+透明标注·
用户能判断能用。这治"撞墙就弃权/乱猜"——BrowseComp 很多题就是这种"有合理推断但无直接
证据"·Kimi 靠此策略拿分。

铁律: 只在【有合理候选】(联想链/证据图/多假设给出过候选)时用·纯无候选仍标 UNKNOWN。
纯生成输出模板·不自己判对错·fail-safe·env MEYO_BEST_EFFORT=0 关·不透 gold。
"""
import os


def _enabled():
    return os.environ.get("MEYO_BEST_EFFORT", "1") != "0"


def build_best_effort(question, candidate, *, reasoning="", alternatives=None,
                      confidence_note=""):
    """生成 Kimi 式 best-effort 答案模板(给 agent 收尾用·非直接当最终答案)。
    candidate=最可能答案·reasoning=推断依据·alternatives=易混/备选·
    confidence_note=不确定性说明。返回注入串。fail-safe → ''。"""
    try:
        if not _enabled() or not candidate:
            return ""
        parts = [
            "[best-effort 输出策略·搜不到直接证据但有合理推断时这样交·别弃权也别乱猜]",
            "按 Kimi 式最可能答案组织你的 finalize(不是弃权·是透明地给最有用的答案):",
            "  ① 明确给出【最可能的答案】: %s" % str(candidate)[:80],
        ]
        if reasoning:
            parts.append("  ② 说明【推断依据】: %s" % str(reasoning)[:200])
        else:
            parts.append("  ② 说明【推断依据】: 基于你已搜到的线索链条(哪几步指向它)")
        parts.append("  ③ 诚实【披露不确定性】: %s"
                     % (str(confidence_note)[:150] if confidence_note
                        else "说明你【没找到哪部分直接证据】·这答案是基于什么【推断】而非确证"))
        if alternatives:
            _alts = alternatives if isinstance(alternatives, list) else [alternatives]
            parts.append("  ④ 提供【相关背景/备选】: 若有易混候选(%s)或用户可能记错的相近事实·"
                         "一并列出防误解" % "、".join(str(a)[:40] for a in _alts[:3]))
        else:
            parts.append("  ④ 提供【相关背景】: 若有相近/易混的事实(如不同年份的同类事件)·"
                         "一并说明防用户记错")
        parts.append("这比①沉默弃权(用户没收获) ②硬猜一个装作确定(误导用户) 都好——"
                     "透明地给最可能答案+标清不确定=BrowseComp 这类题的最优交法。")
        return "\n".join(parts)
    except Exception:
        return ""


def should_best_effort(session, candidate, *, stall=0, phi=0.0):
    """判是否该走 best-effort(有合理候选 + 撞墙但非全空)。
    有候选 + 进度停滞但证据图非空(说明搜到过东西·只是没确证)→ best-effort。
    纯无候选/无任何证据 → 仍标 UNKNOWN。返回 bool。fail-safe → False。"""
    try:
        if not _enabled() or not candidate:
            return False
        # 证据图/证据池非空 = 搜到过线索(有推断基础)·非凭空
        _has_evidence = False
        try:
            import evidence_graph as _eg
            _has_evidence_g = _eg.graph_stats(session).get("edges", 0) >= 1
            _ev = (session or {}).get("evidence") or []
            _has_evidence = _has_evidence_g or len(_ev) >= 2
        except Exception:
            _has_evidence = bool((session or {}).get("evidence"))
        return bool(_has_evidence)
    except Exception:
        return False
