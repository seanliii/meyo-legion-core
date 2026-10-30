# -*- coding: utf-8 -*-
"""staged_retrieval · 分阶段证据收集(烧进引擎·治B类"泛化搜索→分阶段")

用户 /goal(Kimi 方案·治 B 类 17 道): 检索策略从"泛化搜索"到"分阶段证据收集"。
问题在于把多跳检索当单次搜索。应做 staged evidence gathering:
  · Stage 1(锚定·~30% 预算): 用【最高特异度约束】做 query 快速锁定候选枢轴实体
    (不是整句描述撒网——高区分度约束如引号专名/精确数字/族裔缩小候选空间);
  · Stage 2-3(顺藤·~70% 预算): 用 Stage1 拿到的实体锚点·顺着还缺的约束正向补搜
    (接 anchor_hop)·精确验证·结构化入证据图(接 evidence_graph)。

核心 = 不同层用不同约束: 先搜最特异的(信息量大、噪声小)拿锚点·再顺藤补其余。
这直接对应指导思想: 别同层撒网(Stage1 一次锁锚)·顺着证据推进(Stage2-3 grounded)。

铁律: 纯规划(排约束+给下一步 query 建议)·不自己发搜索(发射交给调用方现成闭包)·
fail-safe·env MEYO_STAGED_RETRIEVAL=0 关。绝不透 gold。
"""
import os
import re

# 高特异信号(复用 constraint_ledger 同族判据·区分度高=信息量大噪声小→Stage1 先搜)
_HI_SPEC_PAT = [
    r'"[^"]{2,40}"',            # 引号专名
    r"\b\d{4}\b",               # 精确年份
    r"\b\d{1,3}(?:\.\d+)?%",    # 百分比(如 gold 26.9%)
    r"\b\d{2,}\b",              # 显著数字(比分/编号/CS 数)
    r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b",  # 多词专名
    r"(?:裔|nationality|ethnic|born in|native of)",  # 族裔/出生地
]


def _enabled():
    return os.environ.get("MEYO_STAGED_RETRIEVAL", "1") != "0"


def specificity_score(claim_text):
    """约束特异度评分(越高=区分度越大·Stage1 越该先搜)。启发·通用。
    引号专名/精确数字/族裔 = 高特异; 泛泛描述(born 2000-2010 类宽约束)= 低。"""
    try:
        t = str(claim_text or "")
        score = 0
        for i, pat in enumerate(_HI_SPEC_PAT):
            if re.search(pat, t):
                # 引号/百分比/精确数字权重更高(信息量最大)
                score += (3 if i <= 3 else 2)
        # 长描述但无任何高特异信号 = 宽约束(低分)
        if score == 0 and len(t) > 20:
            score = 1
        return score
    except Exception:
        return 0


def rank_constraints_by_specificity(claims):
    """按特异度降序排约束(Stage1 取最特异的先搜)。返回 [(claim, score) ...]。"""
    try:
        scored = [(c, specificity_score(c.get("text") if isinstance(c, dict) else c))
                  for c in (claims or [])]
        return sorted(scored, key=lambda x: -x[1])
    except Exception:
        return [(c, 0) for c in (claims or [])]


def plan_stage(session, claims, *, has_anchor=False, budget_used_ratio=0.0):
    """规划当前该处于哪个阶段·给下一步该搜什么的建议(不自己发搜索)。
    返回 {
      "stage": 1|2|3,
      "focus_constraint": str,   # 本阶段该聚焦的约束文本
      "advice": str,             # 给 agent 的下一步检索建议(不透 gold)
    }。fail-safe → stage=1 无建议(不干扰)。"""
    out = {"stage": 1, "focus_constraint": "", "advice": ""}
    try:
        if not _enabled():
            return out
        ranked = rank_constraints_by_specificity(claims)
        if not ranked:
            return out
        # 阶段判定: 没锚点=Stage1(先用最特异约束锁锚); 有锚点=Stage2/3(顺藤补缺)
        if not has_anchor:
            out["stage"] = 1
            top_c, top_s = ranked[0]
            out["focus_constraint"] = str(
                top_c.get("text") if isinstance(top_c, dict) else top_c)[:120]
            out["advice"] = (
                "[分阶段·Stage1 锚定] 别整句描述撒网。先用【最高特异度约束】单独做 query "
                "锁定第一个候选枢轴实体: 『%s』(引号专名/精确数字/族裔这类区分度最大的信息·"
                "噪声小最易命中)。拿到一个实体锚点后再进入 Stage2 顺藤。" % out["focus_constraint"])
        else:
            # 有锚点·据预算判 Stage2(补缺)还是 Stage3(收敛)
            out["stage"] = 3 if budget_used_ratio >= 0.7 else 2
            # 顺藤该补的约束 = 还没被高特异锁定的其余约束(取次特异的)
            rest = [c for c, s in ranked[1:]] if len(ranked) > 1 else []
            if rest:
                out["focus_constraint"] = str(
                    rest[0].get("text") if isinstance(rest[0], dict) else rest[0])[:120]
            if out["stage"] == 2:
                out["advice"] = (
                    "[分阶段·Stage2 顺藤] 已有实体锚点。用锚点×还缺的约束正向补搜(顺藤·"
                    "非重新撒网)·把新证据结构化入图。聚焦缺口: 『%s』。" % out["focus_constraint"])
            else:
                out["advice"] = (
                    "[分阶段·Stage3 收敛] 预算已用大半·别再开新搜索方向。用已积累的证据图做"
                    "路径遍历: 从锚点出发找路径完整(每边有来源+高置信)连到答案的枢轴实体·"
                    "锁定它;无完整路径就诚实标不确定·别硬凑。")
        return out
    except Exception:
        return out
