# -*- coding: utf-8 -*-
"""意图对齐验证器(IntentAlignmentVerifier · KM 2781429277 第四层 · 缺口 #8)。

文档核心: 个性化/服务型 query(推荐/适合我/偏好)的"正确"不是【唯一正确答案】·而是
【输出与用户需求的最大对齐度】。三种裁判:
  · GAIA(结果型) = 判卷(有标准答案·看对不对) → 推理轨道
  · BrowseComp(覆盖型) = 查考勤(看信息找没找全) → 记忆轨道
  · 个性化(服务型) = 读心(不是对不对·是合不合适) → 【意图对齐轨道·本模块】

验证维度(文档第四层):
  1. explicit_constraint_alignment  显性约束对齐(满足用户明确提出的约束?)
  2. implicit_preference_alignment   隐性偏好对齐(符合用户历史画像/偏好?)
  3. format_alignment                格式对齐(格式/深度/风格匹配用户期望?)
  4. ambiguity_clarification         歧义处理(意图歧义时主动澄清而非假设?)

关键红线(不能出错):
  · 个性化任务【允许部分满足】·但需明确标注置信度(非通过/不通过二元)。
  · ★bc/GAIA 是客观 benchmark(无用户画像·personalization≈0)·意图对齐【绝不介入】
    其自主答题——只在 profile.personalization 高时激活(调用方保证)。这是核心回归防护。
  · fail-safe: 无 user_profile / 无对话历史 → 跳过对应维度(不阻断·不降分冤枉)。
  · env MEYO_INTENT_ALIGN 默认开·=0 关(完全不介入)。
  · 不透答案·不针对具体题·不碰 gold。
"""
import os
import re

_PREF_HINT = re.compile(
    r"\b(recommend|suggest|for\s+me|my\s+(?:preference|taste|budget|style|goal)|"
    r"best\s+for\s+me|help\s+me\s+(?:choose|pick|decide)|prefer|budget|cheap|"
    r"affordable|beginner|advanced|vegan|vegetarian)\b"
    r"|推荐|适合我|我的(?:偏好|预算|口味|目标)|帮我(?:选|挑|决定)|预算|便宜|新手|进阶",
    re.I)


def _enabled():
    return os.environ.get("MEYO_INTENT_ALIGN", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def _text(x):
    try:
        return x if isinstance(x, str) else str(x or "")
    except Exception:
        return ""


def _extract_explicit_constraints(user_query):
    """从用户 query 提取显性约束(通用启发·非题目特判)。约束 = 引号短语 + 偏好连接词
    引导的从句 + 数量/预算限定。返回 list[str]。fail-safe → []。"""
    q = _text(user_query)
    out = []
    try:
        out += re.findall(r'"[^"]{2,60}"', q)
        out += re.findall(r"'[^']{2,60}'", q)
        # 偏好/限定短语(under $X, at least, no more than, without, 排除, 至少)
        out += re.findall(
            r"(?:under|below|at\s+least|no\s+more\s+than|without|except|"
            r"至少|不超过|排除|除了)\s+[\w$%\.\s]{2,30}", q, re.I)
    except Exception:
        pass
    return [c.strip() for c in out if c and c.strip()]


def _check_explicit(agent_output, user_query):
    """显性约束对齐: 用户明确约束是否在输出中被回应/满足。轻量词面覆盖(不判语义对错·
    只判'是否触及')。无约束 → 1.0(不冤枉)。返回 0-1。"""
    cons = _extract_explicit_constraints(user_query)
    if not cons:
        return 1.0
    out = _text(agent_output).lower()
    hit = 0
    for c in cons:
        toks = [t for t in re.split(r"[\s\"']+", c.lower()) if len(t) >= 3]
        if not toks:
            hit += 1
            continue
        if sum(1 for t in toks if t in out) >= max(1, len(toks) // 2):
            hit += 1
    return round(hit / float(len(cons)), 3)


def _check_implicit(agent_output, user_profile):
    """隐性偏好对齐: 输出是否符合用户历史画像(角色/常问/偏好)。无画像 → None(跳过·
    不阻断)。有画像 → 画像关键词在输出中的命中率。返回 0-1 或 None。"""
    if not user_profile:
        return None
    try:
        prof = user_profile if isinstance(user_profile, dict) else {}
        kws = []
        for k in ("role", "preferences", "interests", "常问", "偏好", "痛点"):
            v = prof.get(k)
            if isinstance(v, str):
                kws.append(v)
            elif isinstance(v, (list, tuple)):
                kws += [str(x) for x in v]
        kws = [w.lower() for w in kws if w and len(str(w)) >= 3]
        if not kws:
            return None
        out = _text(agent_output).lower()
        hit = sum(1 for w in kws if w in out)
        return round(min(1.0, hit / float(max(1, len(kws)))), 3)
    except Exception:
        return None


def _check_format(agent_output, user_query):
    """格式对齐: 用户是否要求了特定格式(list/步骤/表格/字数)·输出是否匹配。
    无格式要求 → 1.0。返回 0-1。"""
    q = _text(user_query).lower()
    out = _text(agent_output)
    try:
        wants_list = bool(re.search(r"\b(list|steps|bullet|列出|步骤|清单)\b", q))
        if wants_list:
            has_list = bool(re.search(r"(^|\n)\s*(?:[-*\d]+[\.\)]|•)", out))
            return 1.0 if has_list else 0.5
        return 1.0
    except Exception:
        return 1.0


def _check_ambiguity(user_query):
    """歧义处理: query 是否高度歧义(需澄清)。个性化 query 缺关键限定(如"推荐个东西"
    但无预算/用途/偏好)→ 歧义高。返回 0-1(1=不歧义·清晰)。"""
    q = _text(user_query)
    try:
        has_pref = bool(_PREF_HINT.search(q))
        cons = _extract_explicit_constraints(q)
        # 有偏好意图但零显性约束 = 歧义(不知按什么偏好推荐)
        if has_pref and not cons and len(q.split()) < 12:
            return 0.4
        return 1.0
    except Exception:
        return 1.0


def verify_intent_alignment(agent_output, user_query,
                            user_profile=None, conversation_history=None):
    """意图对齐验证(编排入口)。返回 dict:
      { score: 加权综合对齐度 0-1, confidence: 同 score(个性化允许部分满足),
        checks: {各维度分}, action: pass|clarify|low_confidence, details }
    ★调用方须保证仅在 personalization 高时调用(bc/GAIA 客观题不该到这)。
    fail-safe: 任何异常 → pass(不阻断答题)。"""
    if not _enabled():
        return {"score": 1.0, "confidence": 1.0, "action": "pass",
                "checks": {}, "disabled": True}
    try:
        checks = {
            "explicit": _check_explicit(agent_output, user_query),
            "implicit": _check_implicit(agent_output, user_profile),
            "format": _check_format(agent_output, user_query),
            "ambiguity": _check_ambiguity(user_query),
        }
        # 加权综合(跳过 None 维度·不冤枉缺画像的题)
        weights = {"explicit": 0.4, "implicit": 0.3, "format": 0.1,
                   "ambiguity": 0.2}
        num = den = 0.0
        for k, w in weights.items():
            v = checks.get(k)
            if v is None:
                continue
            num += float(v) * w
            den += w
        score = round(num / den, 3) if den else 1.0

        # 三级裁决(个性化不追求唯一答案·允许部分满足·只在明显不对齐时干预):
        #   歧义太高(意图不清)→ clarify(主动澄清而非假设); 对齐低 → 标低置信度(不否决·
        #   不重跑·个性化本就无标准答案); 否则 pass。
        if checks["ambiguity"] < 0.5:
            action = "clarify"
        elif score < 0.5:
            action = "low_confidence"
        else:
            action = "pass"
        return {"score": score, "confidence": score, "action": action,
                "checks": checks}
    except Exception:
        return {"score": 1.0, "confidence": 1.0, "action": "pass",
                "checks": {}, "error": True}
