"""verify_as_search · 校验即行为(Verification-as-Search) · 2026-08-19

落地两份 KM 文档的统一内核:
  《证据驱动分层验证引擎》: 验证器不能"蒙眼当裁判"·必须基于证据·按证据覆盖度分级裁决。
  《校验即搜索》: Critic 不是评委·是【检察官】——disagree 必须【自己去搜反例·带证据】。
  用户强调: 校验即行为·不只搜索——任何工具/goal/编排都一样·校验是一等公民行为。

本模块提供【纯函数、session-light、fail-safe】的核心原语 verify_by_search:
  给定 (候选答案, 题面约束, 已有证据, 一个 search_fn) →
    ① 场景路由: Type-A(数值/逻辑自洽·不搜反例·留给原盲验) vs Type-B(事实/实体·主动搜反例)。
    ② 【主动搜反例】(Doc2 核心·_search_counter_examples): 对候选发起针对性反例检索。
    ③ 对(反例+已有)证据算证据覆盖/矛盾(复用 identity_coverage 三态·实体感知)。
    ④ 输出【四态带置信度】(Doc1 三级仲裁的判据源):
       AGREE / EVIDENCE_CONTRADICTS / INSUFFICIENT_EVIDENCE / DISAGREE_WEAK + coverage。

安全约束(Doc1 L3): 验证器只读检索·禁写/执行;反例检索次数硬上限(默认2·防烧墙钟)。
env: MEYO_VERIFY_AS_SEARCH(默认开·=0退回不搜反例)·MEYO_VERIFY_COUNTER_MAX(默认2)。
绝不透 gold(引擎内无 gold)·通用不针对具体题。
"""
from __future__ import annotations
import os
import re
import time


# ── 四态裁决(Doc1 L2/Stage4·比 agree/disagree 二元更细) ──────────────────
V_AGREE = "AGREE"                        # 证据支持候选(高覆盖·无矛盾)
V_CONTRADICTS = "EVIDENCE_CONTRADICTS"   # 有证据【明确矛盾】→ 必须响应(换候选/重搜)
V_INSUFFICIENT = "INSUFFICIENT_EVIDENCE" # 证据不足以判(既不支持也不反驳)→ 可保留但标低置信
V_DISAGREE_WEAK = "DISAGREE_WEAK"        # 覆盖偏低但无硬矛盾 → 建议补证


def _enabled():
    return os.environ.get("MEYO_VERIFY_AS_SEARCH", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def _counter_max():
    try:
        return max(0, int(os.environ.get("MEYO_VERIFY_COUNTER_MAX", "2")))
    except Exception:
        return 2


# ── 场景路由(Doc1 L1 Type-A/B) ────────────────────────────────────────────
#   Type-A: 纯数值/逻辑/量纲自洽——搜反例无意义(留给正交盲验·不搜)。
#   Type-B: 事实/实体/专名/地名/物种——必须靠外部证据·走主动搜反例。
#   规则+语义·fail-safe 默认 Type-B(宁可多搜一次·不误判成盲验)。
_NUMERIC_ANS = re.compile(r"^[\s$€£¥]*[-+]?\d[\d,\.]*\s*[%a-zA-Z°]{0,6}\s*$")


def scenario_of(question, answer):
    """返回 'A'(结构性自洽·不搜反例) 或 'B'(事实实体·搜反例)。fail-safe → 'B'。"""
    try:
        a = str(answer or "").strip()
        q = str(question or "")
        # 答案是纯数值/百分比/单位量 + 题面像计算/统计 → Type-A
        if _NUMERIC_ANS.match(a):
            if re.search(r"(?i)(calcul|compute|sum|total|average|多少|总和|平均|"
                         r"百分比|percent|how many|数值|概率|probability)", q):
                return "A"
        return "B"
    except Exception:
        return "B"


# ── Doc2 核心: 反例检索 query 生成(针对候选·通用不写死某题) ────────────────
def _counter_queries(candidate, constraints):
    """生成反例检索 query(校验即搜索): 找【推翻/替代/争议】候选的信息。"""
    c = str(candidate or "").strip()[:80]
    qs = []
    if not c:
        return qs
    # ① 同义词/替代实体(治"锁错相邻实体"——Solanum hermannii vs capsicoides)
    qs.append(f"{c} synonym alternative correct name")
    # ② 争议/纠错信号
    qs.append(f"{c} controversy incorrect dispute mistaken")
    # ③ 约束否证(候选是否【不】满足某条约束)
    for con in (constraints or [])[:2]:
        con_s = str(con).strip()[:60]
        if con_s:
            qs.append(f"{c} {con_s}")
    return qs[: max(1, _counter_max() + 1)]


def _is_counter_hit(evidence_items, candidate):
    """粗判新证据里是否含【针对候选的反例信号】(通用启发式·非题目特判)。"""
    try:
        cl = str(candidate or "").lower()
        neg = ("not ", "incorrect", "mistaken", "actually ", "instead", "rather than",
               "synonym of", "reclassified", "错误", "并非", "实为", "更正", "而非",
               "被归为", "实际上是")
        for e in evidence_items or []:
            blk = str((e.get("content") or e.get("text") or "")).lower() \
                if isinstance(e, dict) else str(e).lower()
            if any(n in blk for n in neg) and (cl[:20] in blk or not cl):
                return True
        return False
    except Exception:
        return False


def search_counter_examples(candidate, constraints, search_fn, scope=None):
    """Doc2: Critic 主动搜反例。search_fn(query, scope)->list[evidence]。
    返回 (新证据 list, 是否命中反例信号 bool)。次数受 MEYO_VERIFY_COUNTER_MAX 限。
    只读·fail-safe(search_fn 抛错→跳过该 query·绝不崩)。"""
    found = []
    if not _enabled() or search_fn is None:
        return found, False
    n_max = _counter_max()
    if n_max <= 0:
        return found, False
    for i, q in enumerate(_counter_queries(candidate, constraints)):
        if i >= n_max:
            break
        try:
            res = search_fn(q, scope) if scope is not None else search_fn(q)
            if res:
                found.extend(res if isinstance(res, list) else [res])
        except Exception:
            continue
    return found, _is_counter_hit(found, candidate)


# ── 主原语: verify_by_search(校验即行为·四态带置信度) ─────────────────────
def verify_by_search(candidate, question, evidence, *, constraints=None,
                     search_fn=None, scope=None, llm_call=None):
    """校验即行为的核心。返回 dict:
      {"verdict": V_*, "coverage": 0..1, "contradicted": int, "satisfied": int,
       "total": int, "scenario": "A"|"B", "counter_hit": bool,
       "new_evidence": [...], "reason": str}
    流程: 场景路由 → (Type-B)主动搜反例 → 对(反例+已有)证据算三态覆盖/矛盾 → 四态裁决。
    纯函数·不改 session·不透 gold·fail-safe(异常→INSUFFICIENT 放行·不误杀)。"""
    out = {"verdict": V_INSUFFICIENT, "coverage": 0.0, "contradicted": 0,
           "satisfied": 0, "total": 0, "scenario": "B", "counter_hit": False,
           "new_evidence": [], "reason": ""}
    try:
        cand = str(candidate or "").strip()
        if not cand:
            out["reason"] = "empty_candidate"
            return out
        scen = scenario_of(question, cand)
        out["scenario"] = scen
        if scen == "A":
            # Type-A: 结构性自洽·搜反例无意义·交给原正交盲验·此处不介入。
            out["verdict"] = V_INSUFFICIENT
            out["reason"] = "type_A_structural(留给正交盲验·不搜反例)"
            return out

        import identity_coverage as _ic
        claims = constraints
        if not claims:
            claims = _ic.extract_claims(question, None, llm_call)
        # ① 主动搜反例(Doc2 核心)
        counter_ev, counter_hit = search_counter_examples(
            cand, [c.get("text") if isinstance(c, dict) else c for c in (claims or [])],
            search_fn, scope=scope)
        out["counter_hit"] = counter_hit
        out["new_evidence"] = counter_ev
        # ② 对(已有 + 反例)证据算三态·实体感知(治幻觉候选白蹭别人证据)
        all_ev = list(evidence or []) + list(counter_ev or [])
        ev_ment = _ic._evidence_mentioning(all_ev, cand)
        tri = _ic.classify_claims_tri_state(claims or [], ev_ment, cand, llm_call)
        total = int(tri.get("total") or 0)
        sat = int(tri.get("satisfied") or 0)
        con = int(tri.get("contradicted") or 0)
        cov = (sat / float(total)) if total else 0.0
        out.update({"coverage": round(cov, 3), "contradicted": con,
                    "satisfied": sat, "total": total})
        # ③ 四态裁决(Doc1 L2 判据源)
        if con > 0:
            out["verdict"] = V_CONTRADICTS
            out["reason"] = ("发现%d条约束被证据【矛盾】(含%s反例)·候选应被淘汰/换"
                             % (con, "搜到" if counter_hit else "已有"))
        elif total and cov >= 0.8:
            out["verdict"] = V_AGREE
            out["reason"] = "证据高覆盖(%.0f%%·%d/%d约束)·支持候选" % (100 * cov, sat, total)
        elif total and cov < 0.3:
            out["verdict"] = V_INSUFFICIENT
            out["reason"] = "证据覆盖过低(%.0f%%)·不足以判·标低置信" % (100 * cov)
        else:
            out["verdict"] = V_DISAGREE_WEAK
            out["reason"] = "中等覆盖(%.0f%%)无硬矛盾·建议补证再判" % (100 * cov)
        return out
    except Exception as _e:
        out["verdict"] = V_INSUFFICIENT
        out["reason"] = "verify_by_search异常(降级放行): %s" % (str(_e)[:80])
        return out


# ── Doc1 Stage5: 三级证据仲裁(供 cross_verify 调用·把四态映射成动作) ──────
ACT_RETRY = "RETRY_WITH_INVESTIGATION"        # 高覆盖+矛盾/反例 → 回锦标赛/继续搜
ACT_KEEP_FLAG = "KEEP_MAIN_BUT_FLAG_LOW_CONF" # 低覆盖+无硬矛盾 → 保主但标低置信
ACT_RETRIEVE = "TRIGGER_EVIDENCE_RETRIEVAL"   # 中覆盖 → 触发补充检索再判
ACT_KEEP = "KEEP_MAIN"                        # 高覆盖支持 → 维持主答案


def arbitrate(verify_result, main_confirmed=None):
    """三级证据仲裁(Doc1 Stage5·替代二元 MEYO_CV_KEEP_MAIN_ON_NOEVIDENCE):
      EVIDENCE_CONTRADICTS → RETRY(有反证·必须响应·不保守保留)
      AGREE               → KEEP(证据支持)
      INSUFFICIENT + 主答案已confirmed → KEEP_FLAG(裁判缺信息·保内核·标低置信)
      INSUFFICIENT + 主答案未confirmed → RETRIEVE(该补证·不能凭空保留)
      DISAGREE_WEAK       → RETRIEVE(补证再判)
    fail-safe → KEEP(保守放行)。"""
    try:
        v = (verify_result or {}).get("verdict")
        if v == V_CONTRADICTS:
            return ACT_RETRY
        if v == V_AGREE:
            return ACT_KEEP
        if v == V_INSUFFICIENT:
            return ACT_KEEP_FLAG if main_confirmed else ACT_RETRIEVE
        if v == V_DISAGREE_WEAK:
            return ACT_RETRIEVE
        return ACT_KEEP
    except Exception:
        return ACT_KEEP


if __name__ == "__main__":
    # 自测桩: 反例命中→CONTRADICTS→RETRY
    def _fake_search(q, scope=None):
        return [{"content": "Solanum hermannii is actually a synonym; the correct "
                            "accepted name is Solanum capsicoides.", "url": "wcvp.org"}]
    r = verify_by_search("Solanum hermannii",
                         "Identify the species native to region B with spiny fruit.",
                         [], constraints=[{"id": "c1", "text": "native to region B",
                                           "tokens": ["region", "b"]}],
                         search_fn=_fake_search)
    print("verdict:", r["verdict"], "| counter_hit:", r["counter_hit"],
          "| action:", arbitrate(r, main_confirmed=False))
