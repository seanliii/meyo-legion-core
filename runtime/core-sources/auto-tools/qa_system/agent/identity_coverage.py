#!/usr/bin/env python3
"""identity_coverage · 2026-08-18 · 四态身份关系 + claim 级证据覆盖率

用户明确要落地的机制(原话):
  "先把 confirmed/probable/candidate/rejected 四态身份关系显式写进共享状态,
   并给每个最终答案做 claim 级证据覆盖率统计。这样才能同时提升深度、广度和准确率,
   而不是用更多请求换来更多噪声。"

直接对症 BrowseComp 多跳实体识别题的主要错因: auto 反复"检索到近似候选但锁错实体"
(凭部分匹配就交卷)或"弃权不敢答"。根因=没有对候选实体做严格的四态管理和证据
覆盖率验证。

两个通用机制(题面拆约束/证据判定都动态做·绝无针对具体题的硬编码):

  1) 四态身份关系(IdentityStates)——显式写进 session["identity_states"]:
     - candidate: 检索中出现的候选实体(还没核对约束)
     - probable : 满足【部分】题面约束的候选(有覆盖但不全)
     - confirmed: 满足题面【所有】约束、且有多源证据支撑的候选
     - rejected : 被某条约束明确排除的(记录排除原因 rejected_reason)
     状态转移由【证据 × 约束逐条核对】驱动·不是写死规则。

  2) claim 级证据覆盖率(coverage)——把题面拆成 N 个约束/claim(题面问的每个条件)·
     对候选答案统计每个 claim 有没有证据覆盖:
       coverage = 被证据支撑的 claim 数 / 总 claim 数
     低覆盖率 → 不能 confirmed → 继续搜或标注不确定度(而非弃权)。

接进 finalize 路径(native_agent_loop + qa_auto_agent), 与 Answer Vault /
constraint_mapping 门【配合不重复】: 那两个门查"约束↔实现映射表存在性"和
"锁答案不被误清"; 本机制查"最终答案这个【实体】的每条约束到底有没有证据覆盖"——
治锁错实体。

全部 fail-safe: 机制自身任何异常都不崩 run、不拦交卷(返回空/放行)。
拆约束优先用 LLM(通用·题面语义驱动)·LLM 不可用时退回规则抽取兜底。
引擎内根本没有 gold·物理上不可能泄漏答案(只对证据↔约束做覆盖判定)。
"""
import os
import re
import time

# 四态(状态机的合法态)
STATE_CANDIDATE = "candidate"
STATE_PROBABLE = "probable"
STATE_CONFIRMED = "confirmed"
STATE_REJECTED = "rejected"
_STATES = (STATE_CANDIDATE, STATE_PROBABLE, STATE_CONFIRMED, STATE_REJECTED)

# 覆盖率阈值(通用默认·非题目特判): >= 高门且多源 → 可 confirmed;
#   落在 [低门,高门) → probable; < 低门 或被约束排除 → 不能当 confirmed 交。
COVERAGE_CONFIRM = 0.80     # 全约束基本覆盖才算 confirmed 候选
COVERAGE_PROBABLE = 0.40    # 至少覆盖部分约束才算 probable
MULTI_SOURCE_MIN = 2        # confirmed 还要求 >=2 个不同来源支撑(交叉约束收敛)

# ── 2026-08-19 事故修复(Answer Vault 保守锁定) ─────────────────────────
# 病根: 低覆盖率被当成"丢弃候选换另一个"的信号——auto 自己已推理出正确候选
#   (在 candidate 池里)·却因覆盖率算 0.10→改交另一个覆盖率也不高的错候选。
# 修法(anti_false_positive 文档 L1 答案保险箱 + L5 Conservative Bias):
#   ① 第一个达到 probable+ 的候选被【软锁定】(soft-lock)进 identity_states;
#   ② 后续候选只有【严格更优】(更高覆盖率 AND 覆盖>=更多约束)才能替换软锁;
#   ③ 低覆盖 != 换实体·而是【继续补该候选证据】或【标注不确定度交该候选】。
# 全部 env 可关(通用·fail-safe)。
def _soft_lock_enabled():
    return os.environ.get("IC_SOFT_LOCK", "1").strip().lower() not in ("0", "false", "off", "no")


# ─────────────────────────────────────────────────────────────────────────
# 约束(claim)拆解: 把题面拆成可核对的原子约束。LLM 优先·规则兜底。
# ─────────────────────────────────────────────────────────────────────────
def extract_claims(question, existing_triage=None, llm_call=None):
    """把题面拆成 N 个可验证约束/claim。返回 [{"id","text","tokens"}]。

    通用: 拆约束是题面语义驱动·不写死某题约束。优先复用已有 triage.constraints
    (auto 侧 triage 已抽过·避免重复 LLM 调)·否则 LLM 现拆·再退回规则抽取。
    llm_call: 可注入的 (prompt)->str 供测试打桩;None 时用 qa_llm_gateway。
    fail-safe: 任何异常 → 规则兜底。"""
    q = str(question or "").strip()
    # ① 复用已有 triage 约束(auto 侧 triage 阶段抽过)
    try:
        tr = existing_triage or {}
        cons = [str(c).strip() for c in (tr.get("constraints") or []) if str(c).strip()]
        axes = [str(v).strip() for v in (tr.get("ambiguity_axes") or {}).values()
                if str(v).strip()]
        seed = cons + axes
        if len(seed) >= 2:
            return _pack_claims(seed[:8])
    except Exception:
        pass
    # ② LLM 现拆(通用·题面语义)
    try:
        claims = _llm_extract_claims(q, llm_call)
        if claims:
            return _pack_claims(claims[:8])
    except Exception:
        pass
    # ③ 规则兜底(引号短语/年份/数字/带定语从句的分句)
    return _pack_claims(_rule_extract_claims(q))


def _pack_claims(texts):
    out = []
    seen = set()
    for t in texts:
        t = str(t or "").strip()[:200]
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        out.append({"id": "c%d" % (len(out) + 1), "text": t,
                    "tokens": _claim_tokens(t)})
    return out


# 覆盖判定停用词(通用·非题目特判): 这些词命中不算证据支撑约束。
_STOPWORDS = frozenset((
    "the", "and", "was", "were", "who", "that", "which", "with", "for", "from",
    "born", "won", "founded", "award", "later", "his", "her", "their", "this",
    "then", "also", "into", "onto", "over", "under", "about", "press",
))


def _claim_tokens(text):
    """从一条约束抽可检验 token(年份/数字/引号词/学名式专名)——覆盖判定的锚。
    2026-08-19 校准: 除了整条专名短语·还拆出短语里的【单个显著词】(避免整条
    字面必须全命中才算覆盖·太严导致覆盖率被系统性低估)。"""
    toks = set()
    try:
        for m in re.findall(r"\d+(?:\.\d+)?", text):
            if len(m) >= 2:
                toks.add(m.lower())
        for q in (re.findall(r'"([^"]{2,40})"', text)
                  + re.findall(r"'([^']{2,40})'", text)):
            ql = q.lower()
            toks.add(ql)
            # 引号短语里的显著词也各自入锚(语义近似匹配·非死字面全命中)
            for w in re.findall(r"[a-z][a-z\-]{3,}", ql):
                if w not in _STOPWORDS:
                    toks.add(w)
        # 首字母大写专名(实体/机构/刊名) — 通用启发式
        for m in re.findall(r"\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,}){0,3})\b", text):
            if len(m) >= 4:
                ml = m.lower()
                toks.add(ml)
                # 多词专名: 拆出显著词(如 "Golden Quill" → golden/quill)·
                #   证据里出现任一显著词即视为该约束有覆盖迹象(减少字面失配)。
                if " " in ml:
                    for w in ml.split():
                        if len(w) >= 4 and w not in _STOPWORDS:
                            toks.add(w)
    except Exception:
        pass
    return sorted(toks)[:12]


def _rule_extract_claims(q):
    """无 LLM 时的约束兜底: 按连接词/标点拆分句·保留含实体或数字的分片。"""
    try:
        parts = re.split(r"[,;\n]|\band\b|\bwho\b|\bthat\b|\bwhich\b|\bwith\b", q)
        cand = []
        for p in parts:
            p = p.strip()
            if len(p) >= 6 and (re.search(r"\d", p) or re.search(r"[A-Z][a-z]", p)
                                or len(p) >= 16):
                cand.append(p)
        return cand[:6] or ([q] if q else [])
    except Exception:
        return [q] if q else []


def _llm_extract_claims(q, llm_call=None):
    """LLM 拆约束(通用): 只做拆解不解题·题面问的每个条件拆成一条可验证约束。"""
    if len(q) < 12:
        return []
    prompt = (
        "[约束拆解·只拆不解题] 把下面这道题面拆成【可独立验证的约束/条件】·"
        "题面问的实体必须同时满足的每一个条件各写一条(≤50字·英文关键词优先)。"
        "★特别标出【最具体/最个人化/最能区分相似候选】的那几条(那是锁定唯一答案的钥匙)。"
        "只输出 JSON 数组: [\"约束1\",\"约束2\",...] 3-8 条。题面:\n" + q)
    # ★灵魂驾驶员·一行注入本题范式(不同题型拆约束侧重不同:人物题拆传记细节·地理题拆
    #   空间关系·带缓存不卡)。fail-safe→原prompt。
    try:
        import perspective_engine as _pe_ec
        if _pe_ec._enabled():
            prompt = _pe_ec.inject(prompt, q, llm_call=llm_call, what="拆解本题约束")
    except Exception:
        pass
    txt = ""
    if llm_call is not None:
        txt = str(llm_call(prompt) or "")
    else:
        try:
            import qa_llm_gateway as _gw
            r = _gw.call(prompt, tier="light", timeout=30, max_tokens=500)
            txt = (r.get("out") or "") if isinstance(r, dict) else str(r or "")
        except Exception:
            return []
    import json as _json
    a, b = txt.find("["), txt.rfind("]")
    if a < 0 or b <= a:
        return []
    try:
        arr = _json.loads(txt[a:b + 1])
    except Exception:
        return []
    return [str(x).strip() for x in arr if str(x).strip()][:8]


# ─────────────────────────────────────────────────────────────────────────
# 证据收集: 从 session 汇总所有可用证据文本 + 来源(用于覆盖率与多源判定)。
# ─────────────────────────────────────────────────────────────────────────
def collect_evidence(session):
    """汇总 session 里的证据文本块(evidence / evidence_anchors / tool_log summary)。
    返回 [{"text","source"}]。fail-safe → []。"""
    out = []
    try:
        for e in (session.get("evidence") or []):
            if not isinstance(e, dict):
                continue
            t = str(e.get("content") or "").strip()
            if t:
                out.append({"text": t[:6000],
                            "source": str(e.get("url") or e.get("source") or "?")[:200]})
    except Exception:
        pass
    try:
        for a in (session.get("evidence_anchors") or []):
            if not isinstance(a, dict):
                continue
            t = str(a.get("claim") or "").strip()
            if t:
                out.append({"text": t[:2000], "source": str(a.get("source") or "anchor")[:200]})
    except Exception:
        pass
    try:
        for it in (session.get("tool_log") or [])[-24:]:
            if not isinstance(it, dict):
                continue
            t = str(it.get("summary") or "").strip()
            if t and not t[:8].startswith("[工具"):
                out.append({"text": t[:2000], "source": str(it.get("tool") or "tool")[:80]})
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────
# claim 级证据覆盖率: 对候选答案·统计每条 claim 有没有证据支撑。
# ─────────────────────────────────────────────────────────────────────────
def compute_coverage(claims, evidence, candidate="", llm_call=None):
    """对一个候选答案·逐条 claim 判是否被证据覆盖。

    返回 dict:
      {"total": N, "covered": M, "coverage": M/N,
       "per_claim": [{"id","text","covered":bool,"sources":[...],"why":str}],
       "sources": <支撑约束的不同来源数>}
    判定: 先规则(token 命中 → 覆盖·记来源)·再对未命中的 claim 用 LLM 做一次
    语义覆盖判定(通用·可打桩)。fail-safe: 异常 → 覆盖率保守判 0/放行由调用方决定。"""
    result = {"total": 0, "covered": 0, "coverage": 0.0,
              "per_claim": [], "sources": 0}
    try:
        claims = claims or []
        evidence = evidence or []
        result["total"] = len(claims)
        if not claims:
            return result
        # 组合大文本供 token 命中(附来源标记以便回溯)。兼容 collect_evidence
        #   的 {"text","source"} 与原始 evidence 的 {"content","url"} 两种形态。
        ev_blocks = [(str(e.get("source") or e.get("url") or "?"),
                      str(e.get("text") or e.get("content") or "").lower())
                     for e in evidence if isinstance(e, dict)]
        cand_low = str(candidate or "").lower()
        support_sources = set()
        uncovered = []
        for c in claims:
            toks = c.get("tokens") or []
            hit_sources = set()
            if toks:
                for src, blk in ev_blocks:
                    if any(t in blk for t in toks):
                        hit_sources.add(src)
                # 候选答案文本本身也算一处弱来源(答案里直接含约束 token)
                if cand_low and any(t in cand_low for t in toks):
                    hit_sources.add("<candidate>")
            covered = len(hit_sources) > 0
            rec = {"id": c.get("id"), "text": c.get("text"),
                   "covered": covered, "sources": sorted(hit_sources)[:5],
                   "why": "token命中" if covered else ""}
            if covered:
                result["covered"] += 1
                support_sources |= {s for s in hit_sources if s != "<candidate>"}
            else:
                uncovered.append((c, rec))
            result["per_claim"].append(rec)
        # 对规则未命中的 claim 做一次 LLM 语义覆盖判定(通用·批量一次)
        if uncovered and (llm_call is not None or _gateway_ok()):
            try:
                sem = _llm_semantic_coverage(
                    [c for c, _ in uncovered], evidence, candidate, llm_call)
                for (c, rec) in uncovered:
                    if sem.get(c.get("id")):
                        rec["covered"] = True
                        rec["why"] = "语义覆盖(LLM)"
                        result["covered"] += 1
            except Exception:
                pass
        result["sources"] = len(support_sources)
        result["coverage"] = (result["covered"] / float(result["total"])
                              if result["total"] else 0.0)
    except Exception:
        pass
    return result


def _gateway_ok():
    try:
        import qa_llm_gateway  # noqa: F401
        return True
    except Exception:
        return False


def _llm_semantic_coverage(claims, evidence, candidate, llm_call=None):
    """对规则未命中的 claim·让 LLM 判"证据是否语义支撑该约束成立于候选答案"。
    返回 {claim_id: bool}。只判覆盖·不给答案。fail-safe → {}。"""
    if not claims:
        return {}
    ev_txt = "\n".join("- [%s] %s" % (str(e.get("source") or e.get("url"))[:40],
                                      str(e.get("text") or e.get("content"))[:400])
                       for e in (evidence or [])[:12])[:6000]
    cl_txt = "\n".join("%s: %s" % (c.get("id"), c.get("text")) for c in claims)
    prompt = (
        "[证据覆盖判定·只判是否支撑·不解题不给答案] 候选答案(实体): %s\n\n"
        "下列每条约束·请只依据【证据】判断证据是否支撑'该候选满足此约束'。"
        "没有证据支撑就判 false(不许臆测)。\n约束:\n%s\n\n证据:\n%s\n\n"
        '只输出 JSON: {"c1":true/false,...}' % (str(candidate)[:200], cl_txt, ev_txt))
    txt = ""
    if llm_call is not None:
        txt = str(llm_call(prompt) or "")
    else:
        try:
            import qa_llm_gateway as _gw
            r = _gw.call(prompt, tier="light", timeout=30, max_tokens=400)
            txt = (r.get("out") or "") if isinstance(r, dict) else str(r or "")
        except Exception:
            return {}
    import json as _json
    a, b = txt.find("{"), txt.rfind("}")
    if a < 0 or b <= a:
        return {}
    try:
        d = _json.loads(txt[a:b + 1])
        return {str(k): bool(v) for k, v in d.items()}
    except Exception:
        return {}


# ═════════════════════════════════════════════════════════════════════════
# 2026-08-18 · 通用候选判别器(Candidate Discriminator) · 治"锁错实体"主瓶颈
# ─────────────────────────────────────────────────────────────────────────
# 数据实证(BrowseComp): 13 道错题里 12 道是"多个候选里选错了"·不是搜不到、
#   不是交不出。答对题均 26 工具、错题 38 工具(错题搜更多却更差=撒网发散没收敛)。
# 病根: classify_state 只按【单个候选自己的覆盖率】分四态·没有候选间横向对比·
#   选哪个靠软锁"先到先锁+严格更优才换"。多个候选都 probable(覆盖率都不高)时·
#   缺【结构化判别】——不判"哪个被更多硬约束支撑、哪个被某约束矛盾排除"。第一个
#   锁到的若是错的·软锁反而锁错(轮间波动=有时先锁对有时先锁错)。
#
# 通用判别器(灵活·非写死·不针对具体题·不透 gold):
#   ① 逐约束【三态判定】: 对每个候选·把每条约束判成
#      {满足 satisfied / 矛盾 contradicted / 未知 unknown}——不只"有没有证据覆盖"
#      (原二元)·而是【证据是否与约束一致】。矛盾比"未覆盖"强得多的淘汰信号。
#   ② 硬约束【矛盾→直接 rejected】: 任一候选有约束被证据矛盾(不是未知)·直接
#      rejected(比覆盖率低更强的淘汰信号)。砍掉"覆盖率碰巧不低但其实矛盾"的错候选。
#   ③ 候选间【约束满足排序】: 剩下的按 (满足硬约束数, -矛盾数, 覆盖率, 来源数)
#      排序·选被约束支撑最强的。替换判据从"严格更优覆盖率"升级为"约束满足更优":
#      新候选满足更多硬约束(或矛盾更少)→ 可替换软锁·哪怕覆盖率数字没更高。
#      这让"先锁到错的"能被"约束满足更强的对的"纠正。
# env 可关: MEYO_IC_DISCRIMINATOR=1 默认开·=0 退回纯覆盖率软锁(fail-safe)。
# ═════════════════════════════════════════════════════════════════════════
# 约束三态(每条约束相对某候选的判定)
CLAIM_SATISFIED = "satisfied"
CLAIM_CONTRADICTED = "contradicted"
CLAIM_UNKNOWN = "unknown"


def _discriminator_enabled():
    return os.environ.get("MEYO_IC_DISCRIMINATOR", "1").strip().lower() \
        not in ("0", "false", "off", "no")


# ═════════════════════════════════════════════════════════════════════════
# 2026-08-18 增量①: 黑名单单调剪枝(治【轮间波动】——同题一次对一次错)
# ─────────────────────────────────────────────────────────────────────────
# 病根: 判别器把被证据【矛盾】的候选判 rejected·但那只是【本次评估】的一次性判定——
#   下一轮/下一步该候选覆盖率若碰巧回升(证据窗口漂移/token 命中抖动)·又被当成
#   合法候选重新参与排序、甚至被软锁·于是"同题一次对一次错"(轮间波动)。
# 修法(单调·永不复活): 只要一个候选被判 contradicted(硬约束被证据【矛盾】·不是
#   unknown/低覆盖)·就把它的归一实体名写进 session["identity_blacklist"]·此后:
#   ①rank_candidates 过滤掉它  ②maybe_soft_lock 不软锁它、软锁替换时不换成它
#   ③soft_lock_final_body 收尾兜底不交它。黑名单单调只增不减(永不复活)。
# 关键(灵活非学死): 只有【矛盾 contradicted】进黑名单(硬否决·有反证)·unknown/
#   低覆盖【绝不进】(留纠错空间·证据没补齐≠错)。fail-safe·env 可关(=0 退回)。
# ═════════════════════════════════════════════════════════════════════════
def _blacklist_enabled():
    return os.environ.get("MEYO_IC_BLACKLIST", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def get_blacklist(session):
    """读 session 里的候选黑名单(归一实体名 set)。fail-safe → set()。"""
    try:
        bl = (session or {}).get("identity_blacklist")
        if isinstance(bl, set):
            return bl
        if isinstance(bl, (list, tuple)):
            return set(str(x) for x in bl)
        return set()
    except Exception:
        return set()


def is_blacklisted(session, entity):
    """该候选是否已被硬否决(在黑名单里·永不复活)。fail-safe → False。"""
    try:
        if not _blacklist_enabled():
            return False
        return _norm_entity(entity) in get_blacklist(session)
    except Exception:
        return False


def add_to_blacklist(session, entity, reason=""):
    """把被【矛盾】否决的候选加入黑名单(单调·只增不减)。fail-safe → None。
    只在矛盾(contradicted)时调·unknown/低覆盖不调(那不是硬否决)。"""
    try:
        if not _blacklist_enabled() or not isinstance(session, dict):
            return None
        ent = str(entity or "").strip()
        if not ent:
            return None
        key = _norm_entity(ent)
        bl = session.get("identity_blacklist")
        if not isinstance(bl, set):
            bl = set(bl) if isinstance(bl, (list, tuple)) else set()
        bl.add(key)
        # 有界(单题串行·被否决候选不会太多)
        if len(bl) > 60:
            bl = set(list(bl)[-60:])
        session["identity_blacklist"] = bl
        # 附带记原因(可观测·不参与判定)
        try:
            book = session.setdefault("identity_blacklist_reason", {})
            book[key] = str(reason or "证据矛盾")[:160]
            if len(book) > 60:
                for k in list(book.keys())[:-60]:
                    del book[k]
        except Exception:
            pass
        return key
    except Exception:
        return None


def classify_claims_tri_state(claims, evidence, candidate="", llm_call=None):
    """对一个候选·把每条约束判成三态 {satisfied/contradicted/unknown}。

    这是判别器的核心升级: 从二元"覆盖/未覆盖"→三态"一致/矛盾/未知"。
      - satisfied  : 证据支持该候选满足此约束(≈原 covered)。
      - contradicted: 证据【明确反驳】该候选满足此约束(如约束"1911生"·证据说
                      该候选"1920生")——比 unknown 强得多的淘汰信号。
      - unknown    : 既无支持也无反驳(证据还没补齐·留纠错空间·绝不淘汰)。

    判定管线(通用·题面语义驱动·不写死某题):
      1) 先复用 compute_coverage 的规则/语义覆盖 → 覆盖=satisfied·未覆盖先记 unknown;
      2) 对 unknown 的约束做一次 LLM 语义【矛盾判定】(证据是否反驳)·可打桩;
      3) LLM 不可用时·规则兜底: 数值/年份约束若证据里出现【与约束值不同的同类数值】
         且候选名同现·判 contradicted(通用启发式·非题目特判)。
    返回 {"per_claim":[{"id","text","status","why","sources"}],
          "satisfied":n,"contradicted":n,"unknown":n,"total":n,
          "coverage":<辅助覆盖率>,"sources":n}。fail-safe → 全 unknown。
    """
    out = {"per_claim": [], "satisfied": 0, "contradicted": 0, "unknown": 0,
           "total": 0, "coverage": 0.0, "sources": 0}
    try:
        claims = claims or []
        evidence = evidence or []
        out["total"] = len(claims)
        if not claims:
            return out
        # ① 复用覆盖率判定(规则 token 命中 + LLM 语义覆盖)得到 satisfied 基线
        cov = compute_coverage(claims, evidence, candidate, llm_call)
        out["coverage"] = float(cov.get("coverage") or 0.0)
        out["sources"] = int(cov.get("sources") or 0)
        per_cov = {c.get("id"): c for c in (cov.get("per_claim") or [])}
        unknown_claims = []
        for c in claims:
            cid = c.get("id")
            rec = per_cov.get(cid) or {}
            if rec.get("covered"):
                out["per_claim"].append(
                    {"id": cid, "text": c.get("text"), "status": CLAIM_SATISFIED,
                     "why": rec.get("why") or "证据支持", "sources": rec.get("sources") or []})
                out["satisfied"] += 1
            else:
                unknown_claims.append(c)
        # ② 对未满足的约束判【是否被证据矛盾】(LLM 优先·规则兜底)
        contra = {}
        if unknown_claims:
            if llm_call is not None or _gateway_ok():
                try:
                    contra = _llm_semantic_contradiction(
                        unknown_claims, evidence, candidate, llm_call)
                except Exception:
                    contra = {}
            # 规则兜底: 同类数值矛盾(通用·不针对具体题)
            try:
                rule_contra = _rule_numeric_contradiction(
                    unknown_claims, evidence, candidate)
                for k, v in rule_contra.items():
                    if v and not contra.get(k):
                        contra[k] = v
            except Exception:
                pass
        for c in unknown_claims:
            cid = c.get("id")
            if contra.get(cid):
                out["per_claim"].append(
                    {"id": cid, "text": c.get("text"), "status": CLAIM_CONTRADICTED,
                     "why": contra.get(cid) if isinstance(contra.get(cid), str)
                            else "证据矛盾", "sources": []})
                out["contradicted"] += 1
            else:
                out["per_claim"].append(
                    {"id": cid, "text": c.get("text"), "status": CLAIM_UNKNOWN,
                     "why": "无证据支持也无反驳", "sources": []})
                out["unknown"] += 1
    except Exception:
        pass
    return out


def _rule_numeric_contradiction(claims, evidence, candidate):
    """规则兜底矛盾判定(通用启发式·零题目特判):
    约束里有显著数值/年份·而证据里【候选名同现 + 出现同位数不同的同类数值】·
    判该约束被矛盾。例: 约束'born in 1911'·证据'Finn Elliot was born in 1920'
    (候选 Finn Elliot 同现·年份 1920≠1911)→ contradicted。
    保守: 只在候选名在该证据块出现时才判矛盾(避免张冠李戴误伤)。返回 {cid: why}。"""
    res = {}
    try:
        cand_low = _norm_entity(candidate)
        cand_words = [w for w in cand_low.split() if len(w) >= 3]
        for c in claims:
            claim_text = str(c.get("text") or "")
            # 约束里的年份(4位)和一般数值
            claim_years = set(re.findall(r"\b(1[6-9]\d{2}|20\d{2})\b", claim_text))
            claim_nums = set(m for m in re.findall(r"\b\d{1,4}(?:\.\d+)?\b", claim_text)
                             if m not in claim_years and len(m) >= 2)
            if not claim_years and not claim_nums:
                continue
            for e in (evidence or []):
                if not isinstance(e, dict):
                    continue
                blk = str(e.get("text") or e.get("content") or "").lower()
                if not blk:
                    continue
                # 候选名须在该证据块出现(保守·避免误伤别的实体)
                if cand_words and not all(w in blk for w in cand_words):
                    continue
                # 年份矛盾: 约束要 1911·证据块里(候选同现)出现别的年份且没有 1911
                if claim_years:
                    ev_years = set(re.findall(r"\b(1[6-9]\d{2}|20\d{2})\b", blk))
                    if ev_years and not (claim_years & ev_years):
                        res[c.get("id")] = "证据年份%s与约束%s矛盾" % (
                            sorted(ev_years)[0], sorted(claim_years)[0])
                        break
    except Exception:
        pass
    return res


def _llm_semantic_contradiction(claims, evidence, candidate, llm_call=None):
    """LLM 判"证据是否【明确反驳】候选满足该约束"(只判矛盾·不给答案)。
    返回 {claim_id: true/false 或 why 串}。保守: 没有明确反证判 false(unknown 优先·
    留纠错空间)。fail-safe → {}。绝不透 gold(只对证据↔约束判一致性)。"""
    if not claims:
        return {}
    ev_txt = "\n".join("- [%s] %s" % (str(e.get("source") or e.get("url"))[:40],
                                      str(e.get("text") or e.get("content"))[:400])
                       for e in (evidence or [])[:12])[:6000]
    cl_txt = "\n".join("%s: %s" % (c.get("id"), c.get("text")) for c in claims)
    prompt = (
        "[证据矛盾判定·只判是否【反驳】·不解题不给答案] 候选实体: %s\n\n"
        "下列每条约束·请只依据【证据】判断证据是否【明确反驳】'该候选满足此约束'"
        "(即证据给出了与约束【冲突】的事实·如约束说 1911 年出生·证据说该候选 1920 "
        "年出生)。注意区分: 没找到证据(unknown) != 找到反证(contradicted)——只有"
        "找到【明确冲突的事实】才判 true·仅仅没有支持证据一律判 false。\n"
        "约束:\n%s\n\n证据:\n%s\n\n"
        '只输出 JSON: {"c1":true/false,...}(true=证据明确反驳)'
        % (str(candidate)[:200], cl_txt, ev_txt))
    txt = ""
    if llm_call is not None:
        txt = str(llm_call(prompt) or "")
    else:
        try:
            import qa_llm_gateway as _gw
            r = _gw.call(prompt, tier="light", timeout=30, max_tokens=400)
            txt = (r.get("out") or "") if isinstance(r, dict) else str(r or "")
        except Exception:
            return {}
    import json as _json
    a, b = txt.find("{"), txt.rfind("}")
    if a < 0 or b <= a:
        return {}
    try:
        d = _json.loads(txt[a:b + 1])
        return {str(k): bool(v) for k, v in d.items()}
    except Exception:
        return {}


def discriminator_key(tri):
    """从三态结果抽【约束满足比较键】·用于候选间排序/替换判据(约束满足更优):
      (has_contradiction:bool 反向, satisfied_count, -contradicted_count,
       coverage_ratio, sources)。
    比较时: 无矛盾 > 有矛盾(矛盾候选沉底)·然后满足硬约束多的优先·矛盾少的优先·
    最后才看覆盖率/来源。让"约束满足更强"压过"覆盖率数字更高"。fail-safe → 最差键。"""
    try:
        sat = int(tri.get("satisfied") or 0)
        con = int(tri.get("contradicted") or 0)
        cov = float(tri.get("coverage") or 0.0)
        srcs = int(tri.get("sources") or 0)
        # 无矛盾排前(True 排前 → 用 con == 0)
        return (con == 0, sat, -con, cov, srcs)
    except Exception:
        return (False, 0, 0, 0.0, 0)


def is_constraint_better(new_tri, locked_tri):
    """新候选是否【约束满足更优】于软锁候选(替换判据·升级自 _is_strictly_better):
      - 新候选有矛盾而旧的没矛盾 → 绝不替换(False);
      - 旧候选有矛盾而新的没矛盾 → 替换(True·纠错: 把矛盾的错候选换成干净的);
      - 都无矛盾: 满足硬约束数更多·或(数相等且)覆盖率键更优 → 替换。
    关键: 哪怕覆盖率数字没更高·只要满足更多硬约束/矛盾更少·就能替换软锁·
    让"先锁到错的"被"约束满足更强的对的"纠正。fail-safe → False(保守不换)。"""
    try:
        nk = discriminator_key(new_tri)
        lk = discriminator_key(locked_tri)
        # nk/lk = (no_contra, sat, -con, cov, srcs)
        n_ok, n_sat = nk[0], nk[1]
        l_ok, l_sat = lk[0], lk[1]
        if not n_ok and l_ok:
            return False           # 新的有矛盾·旧的干净 → 不换
        if n_ok and not l_ok:
            return True            # 旧的有矛盾·新的干净 → 换(纠错)
        # 同为干净或同为有矛盾: 满足硬约束更多 → 换;相等则整体键更优才换
        if n_sat > l_sat:
            return True
        if n_sat < l_sat:
            return False
        return nk > lk
    except Exception:
        return False


def rank_candidates(candidates_tri, session=None):
    """候选间排序: 输入 [{"entity","tri":<三态结果>}]·按 discriminator_key 降序。
    矛盾候选(has_contradiction)自动沉底。返回排序后的同结构 list(附 state/rejected)。

    增量①(黑名单单调剪枝·治轮间波动): 传入 session 时·【黑名单里的候选被物理过滤】
    (不出现在返回列表·永不复活)——即使它某轮覆盖率回升也不再被当候选。fail-safe →
    原序(黑名单读失败按不过滤处理·绝不因剪枝崩)。"""
    try:
        items = list(candidates_tri or [])
        # 增量①: 黑名单过滤(被矛盾否决的候选永不复活)——出现即剔除
        if session is not None and _blacklist_enabled():
            try:
                bl = get_blacklist(session)
                if bl:
                    items = [it for it in items
                             if _norm_entity(it.get("entity")) not in bl]
            except Exception:
                pass
        for it in items:
            tri = it.get("tri") or {}
            it["_key"] = discriminator_key(tri)
            # 任一硬约束被矛盾 → rejected(比覆盖率低更强的淘汰信号)
            it["rejected"] = int(tri.get("contradicted") or 0) > 0
        items.sort(key=lambda x: x.get("_key"), reverse=True)
        return items
    except Exception:
        return list(candidates_tri or [])


# ─────────────────────────────────────────────────────────────────────────
# 四态身份关系: 显式写进 session["identity_states"]·状态转移由覆盖率驱动。
# ─────────────────────────────────────────────────────────────────────────
def classify_state(coverage_result, multi_source_min=MULTI_SOURCE_MIN,
                   confirm=COVERAGE_CONFIRM, probable=COVERAGE_PROBABLE,
                   rejected_reason="", tri=None):
    """由覆盖率结果推候选实体的四态。通用规则(证据驱动·不写死某题):
      - tri 里有约束被【矛盾】(contradicted) → rejected(判别器最强淘汰信号·
        比覆盖率低更强·砍掉"覆盖率碰巧不低但其实矛盾"的错候选)
      - 有 rejected_reason(某约束明确排除) → rejected
      - coverage >= confirm 且 支撑来源数 >= multi_source_min → confirmed
      - coverage >= confirm 但单源 → probable(需第二通道交叉)
      - coverage >= probable → probable
      - 否则 → candidate
    返回 (state, reason)。fail-safe → (candidate, '判定异常')。"""
    try:
        # 判别器: 任一硬约束被证据矛盾 → 直接 rejected(通用·env 可关)
        if tri and _discriminator_enabled():
            try:
                ncon = int(tri.get("contradicted") or 0)
                if ncon > 0:
                    why = next((p.get("why") for p in (tri.get("per_claim") or [])
                                if p.get("status") == CLAIM_CONTRADICTED), "证据矛盾")
                    return STATE_REJECTED, "约束被矛盾(%d条)·%s" % (ncon, str(why)[:120])
            except Exception:
                pass
        if rejected_reason:
            return STATE_REJECTED, str(rejected_reason)[:200]
        cov = float(coverage_result.get("coverage") or 0.0)
        srcs = int(coverage_result.get("sources") or 0)
        cvd = int(coverage_result.get("covered") or 0)
        tot = int(coverage_result.get("total") or 0)
        if cov >= confirm and srcs >= multi_source_min:
            return STATE_CONFIRMED, "%d/%d约束覆盖·%d源交叉" % (cvd, tot, srcs)
        if cov >= confirm and srcs < multi_source_min:
            return STATE_PROBABLE, "约束覆盖足但只单源(%d源)·需第二通道交叉" % srcs
        if cov >= probable:
            return STATE_PROBABLE, "%d/%d约束覆盖·尚缺关键约束证据" % (cvd, tot)
        return STATE_CANDIDATE, "仅%d/%d约束覆盖·证据不足以锁定" % (cvd, tot)
    except Exception:
        return STATE_CANDIDATE, "判定异常"


def upsert_identity(session, entity, state, reason="", coverage=None):
    """把一个候选实体的四态记录写进 session["identity_states"](显式共享状态)。
    幂等 upsert(按实体名归一)·记录状态/原因/覆盖率/时间。fail-safe。"""
    try:
        if not isinstance(session, dict) or not str(entity or "").strip():
            return None
        if state not in _STATES:
            state = STATE_CANDIDATE
        book = session.setdefault("identity_states", {})
        key = _norm_entity(entity)
        rec = book.get(key) or {"entity": str(entity)[:200], "history": []}
        rec["state"] = state
        rec["reason"] = str(reason)[:200]
        if coverage is not None:
            rec["coverage"] = round(float(coverage.get("coverage") or 0.0), 3)
            rec["covered"] = int(coverage.get("covered") or 0)
            rec["total"] = int(coverage.get("total") or 0)
            rec["sources"] = int(coverage.get("sources") or 0)
        rec["at_time"] = round(time.time(), 1)
        hist = rec.setdefault("history", [])
        hist.append({"state": state, "t": rec["at_time"]})
        del hist[:-8]
        book[key] = rec
        # 有界(单题串行·候选不会太多)
        if len(book) > 40:
            for k in list(book.keys())[:-40]:
                del book[k]
        return rec
    except Exception:
        return None


def _norm_entity(e):
    try:
        return re.sub(r"\s+", " ", str(e).strip().lower())[:120]
    except Exception:
        return str(e)[:120]


# ─────────────────────────────────────────────────────────────────────────
# Answer Vault 软锁定(Conservative Bias) · 2026-08-19 事故修复
#   第一个达到 probable+ 的候选被软锁进 session["_ic_soft_lock"]。后续候选
#   只有【严格更优】(更高覆盖率 AND 覆盖约束数 >= 已锁)才能替换——防止
#   "候选间抖动换答案"把对的换成错的(低覆盖候选 X 是对的·别丢 X 交 Y)。
# ─────────────────────────────────────────────────────────────────────────
def _cov_key(cov):
    """从覆盖率结果抽比较键 (coverage_ratio, covered_count, sources)。"""
    try:
        return (float(cov.get("coverage") or 0.0),
                int(cov.get("covered") or 0),
                int(cov.get("sources") or 0))
    except Exception:
        return (0.0, 0, 0)


def _is_strictly_better(new_cov, locked_cov):
    """新候选是否【严格更优】于软锁候选: 更高覆盖率 AND 覆盖约束数 >= 已锁。
    抖动(覆盖率相近甚至更低)→ False·不许替换软锁。"""
    try:
        n_ratio, n_cvd, _ = _cov_key(new_cov)
        l_ratio, l_cvd, _ = _cov_key(locked_cov)
        # 严格更优: 覆盖率明显更高(留 0.01 抖动带) 且 覆盖约束数不减少
        return (n_ratio > l_ratio + 1e-6) and (n_cvd >= l_cvd)
    except Exception:
        return False


def get_soft_lock(session):
    """读软锁候选。返回 {"entity","coverage","state"} 或 None。fail-safe。"""
    try:
        v = (session or {}).get("_ic_soft_lock") or {}
        if str(v.get("entity") or "").strip():
            return v
        return None
    except Exception:
        return None


# ── 2026-08-19 事故修复② · 软锁候选进 done-final 兜底链 ──────────────────
# 病根(bc 实测): 撞墙钟/预算耗尽时·软锁定的 probable 候选(auto 自己找到的最佳
#   候选)【没被作为最终答案交出】·反而在补证据中耗尽预算·最后交了"DEGRADED 元评论
#   无答案"(❌)。修法(与 done-final 空修复精神一致): 收尾兜底时·只要 Answer Vault
#   有软锁候选·就把它作为答案交出(标注"证据未完全覆盖·置信度中等")·绝不因覆盖率
#   没到 confirmed 就交元评论/空。软锁候选比元评论有价值得多。
# 优先级顺序(finalize 兜底链): confirmed 候选 → 软锁 probable 候选(标注置信度)
#   → 已有 final → narrative/best_effort → DEGRADED UNKNOWN。
# 全 env 可关(IC_SOFT_LOCK_FINAL=0 关本兜底·仍保留软锁定本身)·fail-safe。
def _soft_lock_final_enabled():
    return (_soft_lock_enabled() and
            os.environ.get("IC_SOFT_LOCK_FINAL", "1").strip().lower()
            not in ("0", "false", "off", "no"))


def soft_lock_final_body(session):
    """收尾兜底: 若 session 有软锁候选·把它渲染成【带不确定度标注的答案正文】。

    返回 (body_str, meta_dict) 或 (None, None)。body_str 以"答案: X"开头(评分器
    子串命中)·附证据覆盖率/置信度标注。fail-safe: 读软锁失败/关闭 → (None, None),
    调用方退回原兜底链(绝不崩·绝不破坏 done-final 空修复)。

    通用: 不针对具体题·软锁候选是 auto 自己按四态覆盖率找到的最佳候选。
    绝不透 gold(引擎内无 gold)。"""
    try:
        if not _soft_lock_final_enabled():
            return None, None
        lock = get_soft_lock(session)
        if not lock:
            return None, None
        ent = str(lock.get("entity") or "").strip()
        if not ent:
            return None, None
        # 增量①: 黑名单候选绝不作为最终答案交出(被矛盾否决=永不复活·治轮间波动)
        if is_blacklisted(session, ent):
            return None, None
        cov = lock.get("coverage") or {}
        state = str(lock.get("state") or STATE_PROBABLE)
        covered = int(cov.get("covered") or 0)
        total = int(cov.get("total") or 0)
        srcs = int(cov.get("sources") or 0)
        ratio = float(cov.get("coverage") or 0.0)
        # 置信度语义标注(confirmed→较高·probable→中等)·非题目特判。
        conf_label = "较高(已确认)" if state == STATE_CONFIRMED else "中等(证据未完全覆盖)"
        note = ("> [证据覆盖率标注·置信度%s] 该答案是本轮按题面约束逐条核对后【覆盖率最高的候选】"
                "(%d/%d 条约束有证据支撑·%.0f%%·%d 个独立来源)·在预算耗尽/撞墙前作为最佳候选"
                "交出。证据尚未完全覆盖全部约束·请按中等置信度对待/复核。"
                % (conf_label, covered, total, 100 * ratio, srcs))
        body = "答案: %s\n\n%s" % (ent, note)
        meta = {"entity": ent, "state": state, "coverage": round(ratio, 3),
                "covered": covered, "total": total, "sources": srcs}
        return body, meta
    except Exception:
        return None, None


def _enforce_softlock_enabled():
    # 默认开·治【推理锁定不确定性(FLAKY)】——同题同代码不同轮锁不同实体。
    #   =0 退回"接受模型末端 final"(fail-safe·完全等价旧行为)。
    return os.environ.get("MEYO_IC_ENFORCE_SOFTLOCK", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def enforce_soft_lock_on_final(session, question, final_text,
                               triage=None, llm_call=None):
    """收尾【硬门】· 治 BrowseComp 头号病根【推理锁定不确定性(FLAKY)】。

    病根(两份 KM 审计定性): maybe_soft_lock 已把"约束满足最强的候选"软锁进
      session·但 native 快乐路径 done 只接受【模型末端 final 里的实体】——模型
      最后一步幻觉出一个覆盖率也不低、但被【更强约束候选】比下去的错实体(如
      Kenneth Alexander vs 软锁的 Russell Lyons)·就直接交错卷。soft_lock 只在
      DEGRADED 兜底被读·快乐路径从不比对 → 同题轮间波动 5%~55%。

    修法(通用·不透 gold·不针对具体题): final 落盘前·把 final 的核心实体和
      软锁候选做【约束满足横向对比】(复用 is_constraint_better 三态判据):
        ① final 实体被【黑名单】(此前被证据矛盾否决) → 强制换软锁候选;
        ② 软锁候选【约束满足严格更优】于 final 实体(满足更多硬约束/矛盾更少) →
           换软锁候选;
        ③ 否则(final 实体自己就是软锁、或不劣于软锁) → 保留 final(绝不抖动)。
      换时用 soft_lock_final_body 的带不确定度标注正文·评分器子串可命中。

    返回 (new_final_body 或 None, meta)。None = 保留原 final(不介入)。
    fail-safe: 任何异常 → (None, {}) 保留原 final·绝不破坏 done-final。
    保守偏置: 只有软锁【严格更优】或 final 被黑名单才换·平手/存疑不换(防把对的换错)。
    """
    meta = {"acted": False, "reason": ""}
    try:
        if not _enforce_softlock_enabled() or not isinstance(session, dict):
            return None, meta
        fin = str(final_text or "").strip()
        if not fin:
            return None, meta          # 空 final 由 done-final 兜底链处理·不在此门
        lock = get_soft_lock(session)
        if not lock:
            return None, meta          # 无软锁候选·无可比对·放行
        lock_ent = str(lock.get("entity") or "").strip()
        if not lock_ent or is_blacklisted(session, lock_ent):
            return None, meta          # 软锁本身被否决 → 不拿它当答案
        # final 的核心实体: 优先答案行·退回全文(_norm_entity 已做归一)
        fin_ent = _extract_answer_entity(fin) or fin
        # ① final 实体自己就是软锁实体 → 一致·保留(不重复渲染)
        if _norm_entity(fin_ent) == _norm_entity(lock_ent):
            meta["reason"] = "final==softlock"
            return None, meta
        # ② final 实体被黑名单(此前被证据【矛盾】否决)→ 强制换软锁
        forced = is_blacklisted(session, fin_ent)
        better = False
        if not forced:
            # ③ 横向约束满足对比: 给 final 实体临时算三态·与软锁 tri 比。
            #   关键(治 compute_coverage 实体无关缺陷): final 实体的证据必须【与该实体
            #   共现】才算它满足约束——否则一个幻觉实体会"继承"关于真实体的证据(证据
            #   里含全部约束 token 但说的是别人)·被判满分而躲过对账。用实体过滤后的
            #   证据给 final 算三态·软锁则用它软锁时存下的(已按自身实体覆盖过的)tri。
            try:
                claims = extract_claims(question, triage, llm_call)
                if claims:
                    evidence = collect_evidence(session)
                    fin_evidence = _evidence_mentioning(evidence, fin_ent)
                    fin_tri = classify_claims_tri_state(
                        claims, fin_evidence, fin_ent, llm_call)
                    lock_tri = lock.get("tri") or {}
                    # 软锁【严格更优】于 final(把 new=软锁 / locked=final 传入):
                    #   satisfied 更多 / 矛盾更少 → True·平手 → False(保守不换)。
                    better = is_constraint_better(lock_tri, fin_tri)
                    meta["fin_tri"] = _tri_slim(fin_tri)
                    meta["lock_tri"] = _tri_slim(lock_tri)
            except Exception:
                better = False
        if not (forced or better):
            meta["reason"] = "final_kept(not worse)"
            return None, meta          # 保守: final 不劣于软锁 → 保留
        body, _lm = soft_lock_final_body(session)
        if not body:
            return None, meta          # 软锁渲染不出(如黑名单)→ 保留原 final
        meta.update({"acted": True,
                     "reason": "blacklist_forced" if forced else "constraint_better",
                     "from_entity": fin_ent[:80], "to_entity": lock_ent[:80]})
        return body, meta
    except Exception:
        return None, meta


_ANSWER_ENTITY_RE = re.compile(
    r"(?im)^\s{0,4}(?:exact\s*answer|final\s*answer|答案|answer|a)\s*[:：]\s*"
    r"(\S[^\n]{0,200})")


def _evidence_mentioning(evidence, entity):
    """只保留【提到该实体】的证据块(治 compute_coverage 实体无关缺陷)。

    幻觉实体不会与关于真实体的证据共现·过滤后它的约束覆盖率自然塌到接近 0·
    对账门就能识别"这个 final 实体其实没有证据支撑"。实体名按空白拆词·任一
    显著词(≥3 字符·防 the/of 之类)命中证据块即算共现(宽松·防漏真支撑)。
    fail-safe: 异常/实体空 → 原样返回(不过滤·保守不误杀)。"""
    try:
        ent = str(entity or "").strip().lower()
        if not ent or not evidence:
            return evidence
        words = [w for w in re.split(r"[\s,.;:()\[\]]+", ent)
                 if len(w) >= 3 and w not in ("the", "and", "ltd", "inc", "of")]
        if not words:
            return evidence
        out = []
        for e in evidence:
            if not isinstance(e, dict):
                continue
            blk = str(e.get("text") or e.get("content") or "").lower()
            if any(w in blk for w in words):
                out.append(e)
        return out
    except Exception:
        return evidence


def _extract_answer_entity(text):
    """从 final 文本抽核心实体(答案行优先·无则首非空行)。fail-safe → ''。"""
    try:
        m = _ANSWER_ENTITY_RE.search(str(text or ""))
        if m:
            return m.group(1).strip()
        for ln in str(text or "").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith(("[", ">", "#")):
                return ln[:200]
        return ""
    except Exception:
        return ""


def maybe_soft_lock(session, entity, state, cov, tri=None):
    """把达到 probable+ 的候选软锁定进 session["_ic_soft_lock"](幂等·保守):
      - 无软锁 且 新候选达到 probable+ → 锁定新候选;
      - 已有软锁 → 替换判据【二选一】:
          · 判别器开(MEYO_IC_DISCRIMINATOR=1·默认): 用 is_constraint_better
            (约束满足更优)——满足更多硬约束/矛盾更少即可替换·哪怕覆盖率数字没更高·
            让"先锁到错的"被"约束满足更强的对的"纠正;旧候选被矛盾→新干净候选也能顶上。
          · 判别器关: 退回旧的 _is_strictly_better(纯覆盖率严格更优)。
    tri: 该候选的三态结果(可选)·用于约束满足替换判据 + 落 session。
    返回当前软锁 dict。fail-safe → None。"""
    try:
        if not _soft_lock_enabled() or not isinstance(session, dict):
            return None
        ent = str(entity or "").strip()
        if not ent:
            return get_soft_lock(session)
        # 增量①: 黑名单候选永不软锁(即使覆盖率回升)——治轮间波动
        if is_blacklisted(session, ent):
            return get_soft_lock(session)
        promotable = state in (STATE_PROBABLE, STATE_CONFIRMED)
        cur = get_soft_lock(session)
        if cur is None:
            if promotable:
                lock = {"entity": ent[:200], "coverage": cov or {},
                        "state": state, "at_time": round(time.time(), 1)}
                if tri is not None:
                    lock["tri"] = _tri_slim(tri)
                session["_ic_soft_lock"] = lock
                return lock
            return None
        # 已有软锁: 同实体 → 刷新覆盖率/三态(证据在补·可提升);异实体 → 需更优
        if _norm_entity(cur.get("entity")) == _norm_entity(ent):
            cur["coverage"] = cov or cur.get("coverage") or {}
            cur["state"] = state
            if tri is not None:
                cur["tri"] = _tri_slim(tri)
            cur["at_time"] = round(time.time(), 1)
            session["_ic_soft_lock"] = cur
            return cur
        # 替换判据: 判别器(约束满足更优) 或 退回覆盖率严格更优
        if _discriminator_enabled() and tri is not None:
            better = is_constraint_better(tri, cur.get("tri") or {})
        else:
            better = promotable and _is_strictly_better(
                cov or {}, cur.get("coverage") or {})
        if better:
            lock = {"entity": ent[:200], "coverage": cov or {},
                    "state": state, "at_time": round(time.time(), 1),
                    "replaced": cur.get("entity")}
            if tri is not None:
                lock["tri"] = _tri_slim(tri)
            session["_ic_soft_lock"] = lock
            return lock
        return cur   # 保持原锁(新候选不够更优·不抖动换)
    except Exception:
        return get_soft_lock(session)


def _tri_slim(tri):
    """三态结果瘦身存进软锁(只留排序/展示要用的计数·不存整块证据)。"""
    try:
        return {"satisfied": int(tri.get("satisfied") or 0),
                "contradicted": int(tri.get("contradicted") or 0),
                "unknown": int(tri.get("unknown") or 0),
                "total": int(tri.get("total") or 0),
                "coverage": round(float(tri.get("coverage") or 0.0), 3),
                "sources": int(tri.get("sources") or 0)}
    except Exception:
        return {}


def identity_summary(session):
    """给模型看的四态摘要(按状态分组·一行紧凑)。fail-safe → ''。"""
    try:
        book = (session.get("identity_states") or {})
        if not book:
            return ""
        groups = {s: [] for s in _STATES}
        for rec in book.values():
            groups.get(rec.get("state") or STATE_CANDIDATE,
                       groups[STATE_CANDIDATE]).append(rec)
        parts = []
        _label = {STATE_CONFIRMED: "已确认", STATE_PROBABLE: "很可能",
                  STATE_CANDIDATE: "候选", STATE_REJECTED: "已排除"}
        for st in (STATE_CONFIRMED, STATE_PROBABLE, STATE_CANDIDATE, STATE_REJECTED):
            recs = groups[st]
            if not recs:
                continue
            items = []
            for r in recs[:4]:
                cov = r.get("coverage")
                tag = ("%s(%d/%d约束·%d源)" % (
                    str(r.get("entity"))[:40], int(r.get("covered") or 0),
                    int(r.get("total") or 0), int(r.get("sources") or 0))
                    if cov is not None else str(r.get("entity"))[:40])
                if st == STATE_REJECTED and r.get("reason"):
                    tag += "[排除:%s]" % str(r.get("reason"))[:40]
                items.append(tag)
            parts.append("%s: %s" % (_label[st], " | ".join(items)))
        return "[四态身份关系] " + " · ".join(parts)
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────
# finalize 编排入口: 评估候选答案·产出四态 + 覆盖率·给出门决策。
# ─────────────────────────────────────────────────────────────────────────
def evaluate_answer(session, question, candidate_answer, *,
                    triage=None, llm_call=None):
    """finalize 前对候选答案做四态 + claim 级覆盖率评估。

    返回 dict:
      {"state","reason","coverage": <compute_coverage 结果>,
       "claims":[...], "uncovered":[claim.text,...], "should_block":bool,
       "advice": <低覆盖时给模型的补证/标注建议文本>,
       "keep_candidate":bool,  # True=绝不丢弃本候选(继续补证/标注不确定度交)
       "soft_lock": <当前软锁候选 dict 或 None>}
    should_block: 覆盖率过低(< COVERAGE_PROBABLE)时建议拦一次让其【继续补该候选
      的证据】·而不是凭部分匹配锁错实体交卷。**低覆盖 != 丢弃候选换另一个**——
      2026-08-19 事故根因就是"低覆盖→换实体→把对的换成错的"。只给信号·真正拦不
      拦由调用方(带只拦一次守卫)决定。
    绝不弃权·绝不抖动换答案: 覆盖够 probable 但没到 confirmed → 不拦·标注不确定度
      交(绝不弃权)。第一个 probable+ 候选被软锁定·后续候选除非严格更优否则不替换。
      fail-safe 全放行。"""
    res = {"state": STATE_CANDIDATE, "reason": "", "coverage": {},
           "claims": [], "uncovered": [], "should_block": False, "advice": "",
           "keep_candidate": False, "soft_lock": None,
           "should_stop_searching": False}
    try:
        cand = str(candidate_answer or "").strip()
        if not cand:
            return res
        claims = extract_claims(question, triage, llm_call)
        res["claims"] = claims
        if not claims:
            return res    # 拆不出约束(如纯开放题) → 不介入·放行
        evidence = collect_evidence(session)
        cov = compute_coverage(claims, evidence, cand, llm_call)
        res["coverage"] = cov
        res["uncovered"] = [c.get("text") for c in cov.get("per_claim") or []
                            if not c.get("covered")]
        # ── 通用候选判别器: 逐约束三态(满足/矛盾/未知)·矛盾比未覆盖强得多 ──
        tri = None
        if _discriminator_enabled():
            try:
                tri = classify_claims_tri_state(claims, evidence, cand, llm_call)
                res["tri"] = tri
                res["contradicted"] = [p.get("text") for p in (tri.get("per_claim") or [])
                                       if p.get("status") == CLAIM_CONTRADICTED]
            except Exception:
                tri = None
        state, reason = classify_state(cov, tri=tri)
        res["state"] = state
        res["reason"] = reason
        # 写进显式共享状态(把候选实体的四态显式落 session)
        upsert_identity(session, cand[:120], state, reason, cov)
        # 增量①(黑名单单调剪枝·治轮间波动): 候选被【矛盾】否决 → 进黑名单·永不复活。
        #   只 contradicted 才进(硬否决·有反证)·rejected_reason/低覆盖不进(留纠错空间)。
        if state == STATE_REJECTED and tri and int(tri.get("contradicted") or 0) > 0:
            try:
                add_to_blacklist(session, cand[:120], reason)
            except Exception:
                pass
        # 增量②(early convergence·治撒网烧墙钟): 有 confirmed 候选且无更强竞争者 →
        #   建议停止继续搜索(是【建议】非强制·qa_auto/看门狗收尾读它提前收敛)。
        res["should_stop_searching"] = _should_stop_searching(session, state, tri)
        # Answer Vault 软锁定 + 判别器替换判据: 第一个达到 probable+ 的候选被软锁·
        #   后续候选除非【约束满足更优】(或退回严格更优覆盖率)否则不替换。
        #   rejected(约束被矛盾)候选绝不软锁——那是被淘汰的错候选。
        lock = None
        if state != STATE_REJECTED:
            lock = maybe_soft_lock(session, cand[:120], state, cov, tri=tri)
        else:
            lock = get_soft_lock(session)
        res["soft_lock"] = lock
        # 判别器门决策①: 本候选被约束【矛盾】(rejected) → 拦(这是"找到反证"·
        #   与"没找到证据"根本不同)。这时【应当】换候选(证据明确排除了它)·且
        #   提示优先回到软锁的、未被矛盾的候选。这砍掉"覆盖率碰巧不低但其实矛盾"的错答案。
        if state == STATE_REJECTED and tri and int(tri.get("contradicted") or 0) > 0:
            res["should_block"] = True
            res["keep_candidate"] = False   # 矛盾候选=找到反证·可弃换(不同于低覆盖)
            res["advice"] = _contradiction_advice(cand, tri, lock)
            return res
        # 门决策②: 覆盖率 < probable 门 → 建议拦一次逼补【本候选】证据(治锁错实体)。
        #   关键修复(2026-08-19): 低覆盖【绝不丢弃候选换另一个】——软锁一旦存在·
        #   若本候选就是软锁候选则保留它继续补证;若本候选不是软锁候选且不严格更优·
        #   提示回到软锁候选(别把已锁定的更优候选换成这个覆盖更低的)。
        cov_ratio = float(cov.get("coverage") or 0.0)
        if cov_ratio < COVERAGE_PROBABLE and cov.get("total"):
            res["should_block"] = True
            res["keep_candidate"] = True   # 不丢弃·继续补证或标注不确定度交
            res["advice"] = _block_advice(cand, cov, res["uncovered"], lock)
        return res
    except Exception:
        return res


def _block_advice(candidate, cov, uncovered, soft_lock=None):
    """低覆盖率拦截建议文本(治"凭部分匹配就锁错实体")。通用·不透答案。
    关键(2026-08-19 事故修复): 绝不建议"换一个候选交"——低覆盖时【继续补该候选
    证据】或【标注不确定度交该候选】。若已有软锁的更优候选·提示优先回到它。"""
    lines = [
        "[claim级证据覆盖率过低·不能当 confirmed 交·但【不要丢弃它换别的】] 候选实体 "
        "「%s」 目前只有 %d/%d 条题面约束有证据支撑(覆盖率 %.0f%%·%d 个独立来源)。"
        % (str(candidate)[:80], int(cov.get("covered") or 0),
           int(cov.get("total") or 0), 100 * float(cov.get("coverage") or 0),
           int(cov.get("sources") or 0)),
        "覆盖率低【只说明证据还没补齐】·不代表这个候选错——绝不能因为覆盖率低就"
        "改交另一个覆盖率也不高的候选(那样极易把已经找对的答案丢掉换成错的)。",
    ]
    # 软锁提示: 已锁定的更优候选优先(Conservative Bias·防抖动换答案)
    try:
        if soft_lock and _norm_entity(soft_lock.get("entity")) != _norm_entity(candidate):
            lk = soft_lock.get("coverage") or {}
            lines.append(
                "注意: 你此前已把候选 「%s」 软锁定(覆盖率 %.0f%%·%d/%d 约束)——"
                "它更优。除非本候选【严格更优】(更高覆盖率且满足更多约束)·否则请"
                "回到软锁候选·别抖动换答案。"
                % (str(soft_lock.get("entity"))[:60],
                   100 * float(lk.get("coverage") or 0),
                   int(lk.get("covered") or 0), int(lk.get("total") or 0)))
    except Exception:
        pass
    if uncovered:
        lines.append("以下约束【还没有证据覆盖】·请针对性补证(每条找到独立来源再交):")
        for i, u in enumerate(uncovered[:5], 1):
            lines.append("  %d. %s" % (i, str(u)[:100]))
    lines.append(
        "正确做法(优先级从高到低): ①【继续补本候选证据】——对未覆盖约束逐条精确检索"
        "(引号包实体+该约束关键词)·换第二通道交叉核对(避免单源系统性偏差); "
        "②补不到位就【保留本候选交·并明确标注不确定度/置信度】(绝不弃权·绝不空交); "
        "③只有当某条约束用可靠证据【明确排除】了本候选(不是没找到证据·而是找到了"
        "反证)·才考虑其它候选·且新候选也要过同样的覆盖率检验(严格更优才替换)。"
        "关键红线: 不许仅因覆盖率低就把本候选换成另一个覆盖率也不高的候选。")
    return "\n".join(lines)


def _contradiction_advice(candidate, tri, soft_lock=None):
    """候选被约束【矛盾】(找到反证)时的拦截建议。通用·不透答案。
    与低覆盖 advice 根本不同: 这里证据【明确反驳】了本候选·应当【弃换】·而不是
    继续补证。列出被矛盾的约束·并优先回到未被矛盾的软锁候选/其它候选。"""
    contra = [(p.get("text"), p.get("why")) for p in (tri.get("per_claim") or [])
              if p.get("status") == CLAIM_CONTRADICTED]
    lines = [
        "[候选被约束【矛盾】排除·不是没找到证据·而是找到了反证——应当换候选] 候选实体 "
        "「%s」 有 %d 条题面约束被证据【明确反驳】(此为比'覆盖率低'强得多的淘汰信号)。"
        % (str(candidate)[:80], len(contra)),
    ]
    if contra:
        lines.append("被矛盾的约束(证据与之冲突):")
        for i, (t, why) in enumerate(contra[:5], 1):
            lines.append("  %d. %s  ← %s" % (i, str(t)[:100], str(why)[:80]))
    # 优先回到未被矛盾的软锁候选(若其本身未被矛盾)
    try:
        if soft_lock and _norm_entity(soft_lock.get("entity")) != _norm_entity(candidate):
            slk_tri = soft_lock.get("tri") or {}
            if int(slk_tri.get("contradicted") or 0) == 0:
                lines.append(
                    "此前软锁的候选 「%s」 未被任何约束矛盾·约束支撑更强——优先回到它复核/交付。"
                    % str(soft_lock.get("entity"))[:60])
    except Exception:
        pass
    lines.append(
        "正确做法: ①这个候选被反证排除·换回其它【未被矛盾】的候选(优先满足硬约束更多、"
        "无矛盾的那个); ②对新候选同样逐条核对约束的满足/矛盾; ③绝不把被矛盾的候选当"
        "答案交(那正是'锁错实体')。")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# 2026-08-18 增量②: early convergence(治【撒网】——错题38工具 vs 答对26工具)
# ─────────────────────────────────────────────────────────────────────────
# 病根实证: 答对题均 26 工具、错题 38 工具——错题【搜得更多却更差】(撒网发散没收敛·
#   烧墙钟)。当已经有一个 confirmed 候选(约束基本全满足 + 多源交叉)且没有别的
#   同样强的竞争者时·【继续搜索只会引入噪声】·该发信号建议收敛停搜。
# 修法(建议非强制·fail-safe·env 可关): evaluate_answer 返回里加
#   should_stop_searching=True——当①本候选 confirmed·或②session 里已有 confirmed
#   候选·且无更强/同强的未决竞争者。qa_auto 收尾/看门狗读它提前收敛(不硬停·防误伤)。
# ═════════════════════════════════════════════════════════════════════════
def _early_conv_enabled():
    return os.environ.get("MEYO_IC_EARLYCONV", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def _should_stop_searching(session, state, tri, *, question="", candidate="",
                           evidence=None, llm_call=None):
    """判是否建议停止继续搜索(early convergence)。建议非强制·fail-safe → False。
    触发条件(通用·不写死某题·不透 gold):
      - 有一个 confirmed 候选(本候选 confirmed·或 session identity_states 里有 confirmed);
      - 且没有【同样强或更强】的未决竞争者(避免两个都很强时过早停·那要深挖区分)。
    只是【建议】——调用方(收尾/看门狗)结合自身预算/进展决定是否真收敛。

    ★2026-08-20(用户重大判断·循环时间控制换位思考): 规则判"可停"时·若传了 question/
    candidate/evidence/llm_call·再用【出题人视角】复核一次"这个候选真的回应了题目所问、
    没漏关键约束/没漏最后一跳吗"(如题问 real name·别锚到艺名就停)。复核判"还没到"→
    不建议停(返回 False·让它继续)。★带已有信息·防幻觉。向后兼容: 不传→纯规则。"""
    try:
        if not _early_conv_enabled():
            return False
        # 本候选 confirmed·且无矛盾 → 最强信号
        cur_confirmed = (state == STATE_CONFIRMED
                         and (not tri or int(tri.get("contradicted") or 0) == 0))
        # 扫 session 四态: 是否已有 confirmed 候选
        book = (session or {}).get("identity_states") or {}
        confirmed = [r for r in book.values()
                     if (r or {}).get("state") == STATE_CONFIRMED]
        if not cur_confirmed and not confirmed:
            return False
        # 检查是否有【同样强的未决竞争者】(另一个也很可能对的候选)——有则别过早停
        #   (交给增量③深挖区分)。这里用 probable+ 且非黑名单的其它实体计数。
        strong_others = 0
        for k, r in book.items():
            r = r or {}
            if r.get("state") in (STATE_PROBABLE, STATE_CONFIRMED):
                if is_blacklisted(session, r.get("entity") or k):
                    continue
                strong_others += 1
        # 只有一个强候选(就是 confirmed 的那个)→ 可收敛;多个强候选并存 → 先别停
        _rule_stop = (cur_confirmed or bool(confirmed)) and strong_others <= 1
        if not _rule_stop:
            return False
        # ★换位复核(2026-08-20): 规则说可停·出题人视角再确认没漏关键约束/最后一跳。
        if question and candidate and llm_call is not None:
            try:
                import perspective_engine as _pe_ss
                if _pe_ss._enabled():
                    # ★优雅收敛:走统一 decide(PHASE_LOCK)(底层复用judge_candidate)
                    _snap_ss = _pe_ss.ContextSnapshot(question, evidence=evidence or [],
                                                      llm_call=llm_call)
                    _j = _pe_ss.decide(_snap_ss, _pe_ss.PHASE_LOCK, candidate=candidate)
                    # 换位判据说【还不够(insufficient·有 unknown 约束没证据)或矛盾】→ 别停·继续查
                    if _j.get("verdict") in ("insufficient", "reject"):
                        return False
            except Exception:
                pass
        return _rule_stop
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════════════════
# 2026-08-18 增量③: 同分深挖 + 全否回溯(治【多候选选错】的平局与死路)
# ─────────────────────────────────────────────────────────────────────────
# 病根: rank_candidates 只给排序·没给【下一步动作建议】。两个候选硬约束满足数接近
#   (平局)时·瞎搜等于赌;所有候选都被矛盾否决(死路)时·还在死候选上耗预算。
# 修法(给提示让 agent 自己做·不代劳): decide_next_action(ranked) 读排序结果·返回
#   {action, advice, top}:
#   - all_rejected  → backtrack: 全被矛盾·回溯重找候选(别在死候选耗);
#   - tie (top1/top2 硬约束满足数差 <=1) → deepdive: 给【区分性提示】(该补哪条约束
#     的证据来区分这两个候选)让 agent 针对性深挖·而非瞎搜;
#   - top1 显著领先(差 >1) → lock: 直接锁定(配合 early convergence 收敛)。
# 通用·fail-safe·不透 gold(只依据约束满足计数)。
# ═════════════════════════════════════════════════════════════════════════
def decide_next_action(ranked, *, question="", evidence=None, llm_call=None):
    """由排序后的候选列表决定下一步动作建议。输入 rank_candidates 的输出。
    返回 {"action": "lock"|"deepdive"|"backtrack"|"noop",
          "advice": <给 agent 的提示文本>, "top": <top1 entity 或 None>,
          "tie_needs_deepdive": bool, "all_rejected_backtrack": bool}。
    fail-safe → {"action":"noop",...}(绝不崩·绝不代劳·只给提示)。

    ★2026-08-20(用户重大判断·换位思考): 平局(两候选难分)时·若传了 question/evidence/
    llm_call·用 perspective_engine 从【出题人视角+已搜证据】判"出题人会用哪条线索区分这两个"·
    给具体的区分性搜索建议(比纯规则的 _tie_deepdive_advice 更贴题意)。带已有信息·防幻觉。
    向后兼容: 不传这三个参数→退回原纯规则行为。"""
    out = {"action": "noop", "advice": "", "top": None,
           "tie_needs_deepdive": False, "all_rejected_backtrack": False}
    try:
        items = list(ranked or [])
        if not items:
            return out
        # 全否回溯: 所有候选都被矛盾否决(rejected)→ 死路·回溯重找
        alive = [it for it in items if not it.get("rejected")]
        if not alive:
            out["action"] = "backtrack"
            out["all_rejected_backtrack"] = True
            out["advice"] = _backtrack_advice(items)
            return out
        top = alive[0]
        out["top"] = str(top.get("entity") or "") or None
        # 只有一个存活候选 → 直接锁定
        if len(alive) < 2:
            out["action"] = "lock"
            out["advice"] = ("[候选唯一存活·可锁定] 「%s」 是唯一未被矛盾否决的候选·"
                             "约束满足最强——锁定它·别再撒网烧墙钟(可标注不确定度交)。"
                             % str(top.get("entity"))[:60])
            return out
        second = alive[1]
        t1 = int((top.get("tri") or {}).get("satisfied") or 0)
        t2 = int((second.get("tri") or {}).get("satisfied") or 0)
        gap = t1 - t2
        if gap > 1:
            # top1 显著领先 → 直接锁定
            out["action"] = "lock"
            out["advice"] = ("[top1 显著领先·可锁定] 「%s」 满足 %d 条硬约束·领先次优 "
                             "「%s」(%d 条)超过 1 条——锁定 top1·别再对平手候选瞎搜。"
                             % (str(top.get("entity"))[:60], t1,
                                str(second.get("entity"))[:60], t2))
            return out
        # 平局(差 <=1) → 深挖: 给区分性提示(该补哪条约束的证据来区分这两个)
        out["action"] = "deepdive"
        out["tie_needs_deepdive"] = True
        out["advice"] = _tie_deepdive_advice(top, second)
        # ★换位思考增强(2026-08-20): 出题人视角判"该搜什么来区分这两个平局候选"·带已搜证据。
        if question and llm_call is not None:
            try:
                import perspective_engine as _pe
                if _pe._enabled():
                    _e1 = str(top.get("entity") or "")
                    _e2 = str(second.get("entity") or "")
                    _itq = (str(question)[:500] +
                            "\n\n[当前两个难分的候选]: 「%s」 vs 「%s」·两者约束满足数接近。"
                            "从出题人视角: 题面哪条线索最能区分这两个?该搜什么精确 query 去确认?"
                            % (_e1[:50], _e2[:50]))
                    # ★优雅收敛:走统一 decide(PHASE_RECALL)(底层复用interrogate_as_setter)
                    _snap_ig = _pe.ContextSnapshot(_itq, evidence=evidence or [],
                                                   llm_call=llm_call)
                    _ig = _pe.decide(_snap_ig, _pe.PHASE_RECALL)
                    _qs = _ig.get("search_queries") or []
                    if _qs:
                        out["advice"] = (out["advice"] +
                            " ｜【换位思考·区分这两个候选该搜】: " + " / ".join(_qs[:2]))
                        out["perspective_queries"] = _qs[:2]
            except Exception:
                pass
        return out
    except Exception:
        return out


def _distinguishing_claims(top, second):
    """找出区分 top1/top2 的约束(在一个候选满足、另一个未知/矛盾的约束上补证)。
    返回 [claim_text,...]。fail-safe → []。"""
    diff = []
    try:
        p1 = {p.get("id"): p for p in ((top.get("tri") or {}).get("per_claim") or [])}
        p2 = {p.get("id"): p for p in ((second.get("tri") or {}).get("per_claim") or [])}
        for cid in set(p1) | set(p2):
            s1 = (p1.get(cid) or {}).get("status")
            s2 = (p2.get(cid) or {}).get("status")
            if s1 != s2:   # 两候选在该约束上状态不同 → 有区分力
                txt = (p1.get(cid) or p2.get(cid) or {}).get("text")
                if txt:
                    diff.append(str(txt)[:100])
    except Exception:
        pass
    return diff[:5]


def _tie_deepdive_advice(top, second):
    """平局深挖提示(治两候选难分·让 agent 针对性补区分性证据·不瞎搜)。通用·不透答案。"""
    e1 = str(top.get("entity"))[:60]
    e2 = str(second.get("entity"))[:60]
    t1 = int((top.get("tri") or {}).get("satisfied") or 0)
    t2 = int((second.get("tri") or {}).get("satisfied") or 0)
    lines = [
        "[候选平局·需针对性深挖区分·别瞎撒网] 候选 「%s」(满足 %d 硬约束)与 "
        "「%s」(%d)约束满足数接近·难分伯仲。撒网搜更多只会引入噪声(错题平均 38 "
        "工具 vs 答对 26)——请【针对能区分这两个候选的约束】精确补证:"
        % (e1, t1, e2, t2),
    ]
    diff = _distinguishing_claims(top, second)
    if diff:
        lines.append("以下约束在两候选间状态不同(补它的独立证据即可分出胜负):")
        for i, d in enumerate(diff, 1):
            lines.append("  %d. %s" % (i, d))
    else:
        lines.append("请找出【只有其中一个候选满足】的关键约束(如具体年份/机构/作品)·"
                     "对该约束分别检索两个候选名·看谁被证据支持、谁被反驳。")
    lines.append("做法: 对上述区分性约束·分别用『\"实体名\" + 约束关键词』精确检索·换第二"
                 "通道交叉·让证据【明确支持一个、反驳另一个】——分出后即锁定·不再泛搜。")
    return "\n".join(lines)


def _backtrack_advice(items):
    """全否回溯提示(所有候选被矛盾否决=死路·别在死候选上耗预算)。通用·不透答案。"""
    names = [str(it.get("entity"))[:40] for it in (items or [])[:5]
             if it.get("entity")]
    lines = [
        "[全部候选被矛盾否决·死路——回溯重找候选·别在死候选上耗预算] 当前所有候选 "
        "(%s) 都有硬约束被证据【明确反驳】·继续给它们补证是浪费墙钟。"
        % ("、".join(names) if names else "?"),
        "做法: ①回到题面重读约束·换【不同的检索入口/关键词组合】找【全新的候选实体】"
        "(别再变着法搜已被否决的这些); ②被矛盾的约束往往是最强的定位锚——用它反向"
        "检索(如『约束值 + 题面其它条件』)直接找满足该约束的实体; ③新候选同样逐条"
        "过约束满足/矛盾判定·别重蹈覆辙。",
    ]
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# 2026-08-18 · 机制1: ALP(Answer Lock Protocol)——交卷前证据绑定验证
# ─────────────────────────────────────────────────────────────────────────
# 病根(Kimi 确认·LongCat MoE token 级动态路由 gating): temperature=0 仍漂移·
#   "推理链末端实体锁定失效"——采样随机把【对的实体】在推理链末端漂成【证据里
#   根本没出现过的错实体】(第2题 BATM 幻觉出证据外实体·锁错实体)。
# 现有四态/覆盖率门查的是"答案实体的每条约束有没有证据覆盖";ALP 补的是【更前置
#   的一层】: 先问"答案里的核心实体·到底在不在【已收集证据集】里"——治"末端
#   幻觉出一个证据里压根没搜到过的实体"(比约束覆盖更基础的绑定)。
#
# 机制(通用·fail-safe·不透 gold·不针对具体题):
#   ① 从候选答案抽【核心实体/token】(复用 _vote_key 式归一 + 专名/数值锚·同
#      _claim_tokens 风格);
#   ② 在【已收集证据集】(collect_evidence)里做实体级归一/token 匹配·算 support
#      score(命中证据块的实体 token 比例·近似 identity_coverage 归一);
#   ③ 分级: score>0.9 supported / 0.6-0.9 partial / <0.6 unsupported;
#   ④ strict_mode(MEYO_ALP_STRICT 默认开): unsupported → 拒绝交卷·触发约束重试
#      (提示"你交的 X 在证据里没有支撑·要么从证据里已出现的候选选·要么补证据");
#      partial → 警告但放行(标注); supported → 放行。
#   fail-safe: 抽取失败/无证据/异常一律不拦(返回 supported·不卡交卷)。
# ═════════════════════════════════════════════════════════════════════════
ALP_SUPPORTED = "supported"
ALP_PARTIAL = "partial"
ALP_UNSUPPORTED = "unsupported"
ALP_SUPPORTED_MIN = 0.9    # >0.9 supported
ALP_PARTIAL_MIN = 0.6      # 0.6-0.9 partial · <0.6 unsupported


def _alp_enabled():
    return os.environ.get("MEYO_ALP", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def _alp_strict():
    """strict_mode 默认开: unsupported 拒绝·partial 警告放行。"""
    return os.environ.get("MEYO_ALP_STRICT", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def _answer_entity_tokens(answer):
    """从候选答案抽【核心实体锚 token】(通用·非题目特判)。
    抽: 显著数值/年份 + 引号短语 + 首字母大写专名(及其显著单词)。复用与
    _claim_tokens 同款启发式(实体别名/token 匹配的归一)·避免死字面。
    先剥掉"答案:"/"Answer:"前缀与元评论·只锚真正的答案实体。fail-safe → []。"""
    toks = set()
    try:
        raw = str(answer or "")
        # 剥常见答案前缀 + 只取答案行(避免把整段叙事/标注当实体)
        raw = re.sub(r"(?im)^\s*(答案|answer|final answer|结论)\s*[:：]\s*", "", raw)
        # 取第一非空行(答案通常在首行)+ 去掉 markdown 标注块
        first = ""
        for ln in raw.splitlines():
            ln = ln.strip().lstrip(">#*- ").strip()
            if ln and not ln.startswith("[") and len(ln) >= 1:
                first = ln
                break
        text = first or raw
        for m in re.findall(r"\d+(?:\.\d+)?", text):
            if len(m) >= 2:
                toks.add(m.lower())
        for q in (re.findall(r'"([^"]{2,60})"', text)
                  + re.findall(r"'([^']{2,60})'", text)):
            ql = q.lower().strip()
            if ql:
                toks.add(ql)
                for w in re.findall(r"[a-z][a-z\-]{3,}", ql):
                    if w not in _STOPWORDS:
                        toks.add(w)
        for m in re.findall(r"\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,}){0,4})\b", text):
            if len(m) >= 3:
                ml = m.lower()
                toks.add(ml)
                if " " in ml:
                    for w in ml.split():
                        if len(w) >= 3 and w not in _STOPWORDS:
                            toks.add(w)
        # 若没抽到专名/数值(如答案是纯小写短语)·退回归一词集(单词级锚)
        if not toks:
            for w in re.findall(r"[a-z0-9][a-z0-9\-]{2,}", text.lower()):
                if w not in _STOPWORDS:
                    toks.add(w)
    except Exception:
        pass
    return sorted(toks)[:16]


# 开放/解释/推理/写作类 query 的信号(通用启发·非题目特判·不透 gold)。
#   命中=这题不是"在证据里找一个特定实体/事实值"·ALP 不该介入。
_OPEN_Q_PAT = re.compile(
    r"(?i)(解释|说明|介绍|概述|总结|比较|对比|区别|如何|怎么|怎样|为什么|为何|"
    r"分析|评价|看法|建议|写一?[段篇]|帮我写|生成|翻译|润色|改写|举例|列举|"
    r"什么是|是什么|作用|原理|概念|意思|含义|区别是|一句话|简述|详述|"
    r"谈谈|聊聊|讲讲|讲一下|说一下|优缺点|利弊|"
    r"explain|describe|summar|compare|difference|how to|how do|why|"
    r"what is|what are|what does|write|generate|translate|rewrite|brainstorm|"
    r"suggest|pros and cons|overview|introduce|in one sentence|tell me about)")
# 反信号: 明确要"找出/识别一个特定实体/名字/年份"的题(这类才需要 ALP)。
_ENTITY_LOOKUP_PAT = re.compile(
    r"(?i)(找出|identify|name the|full name|哪一?[个位家部]|谁是|是谁|哪年|哪一年|"
    r"which\s+\w+\s+(is|was|has)|who\s+(is|was)|出生年|出生于|成立于)")


def _is_open_ended_question(question):
    """判 question 是否开放/解释/推理类(ALP 不介入)。反信号(实体查找)优先→不算开放。
    fail-safe → False(拿不准当非开放·保持 ALP 原行为·不误放)。"""
    try:
        q = str(question or "").strip()
        if not q:
            return False
        if _ENTITY_LOOKUP_PAT.search(q):
            return False    # 明确要找特定实体/名字/年份 → 需要 ALP·非开放
        return bool(_OPEN_Q_PAT.search(q))
    except Exception:
        return False


def alp_verify(session, answer, *, evidence=None):
    """机制1核心: 验证候选答案的【核心实体】是否在【已收集证据集】里有支撑。

    返回 dict:
      {"level": supported|partial|unsupported, "score": float,
       "tokens": [...], "hit_tokens": [...], "sources": <命中的不同来源数>,
       "should_block": bool, "advice": <unsupported 时的重试提示·否则 "">}
    分级: score>0.9 supported / 0.6-0.9 partial / <0.6 unsupported。
    strict_mode(默认): unsupported → should_block=True + 重试提示;partial → 不拦
      (可由调用方标注);supported → 放行。
    fail-safe: env 关/抽不到实体 token/无证据/异常 → supported 放行(绝不卡交卷)。"""
    res = {"level": ALP_SUPPORTED, "score": 1.0, "tokens": [], "hit_tokens": [],
           "sources": 0, "should_block": False, "advice": ""}
    try:
        if not _alp_enabled():
            return res
        # 2026-08-19 治【开放题被 ALP 误伤】: ALP 是给【实体识别/事实查找题】做"答案实体
        #   在不在证据里"的验证。开放/解释/推理/写作类 query 根本没有"要在证据里找的实体"
        #   (如"三句话解释向量数据库")·却因答案里的名词被当实体、又没搜证据→score 低→
        #   unsupported→拦交卷→慢+噪音。修:非实体/事实题直接 fail-safe 放行(不做 ALP)。
        #   判据从 session.question 读·通用启发·env 可关(MEYO_ALP_OPENQ_SKIP=0 退回旧行为)。
        if os.environ.get("MEYO_ALP_OPENQ_SKIP", "1").strip().lower() \
                not in ("0", "false", "off", "no"):
            _q = str((session or {}).get("question") or "")
            if _q and _is_open_ended_question(_q):
                return res    # 开放/解释/推理题 → ALP 不介入·放行(治开放题慢+误拦)
        toks = _answer_entity_tokens(answer)
        res["tokens"] = toks
        if not toks:
            return res    # 抽不到实体锚(纯开放/数值缺失) → fail-safe 放行
        ev = evidence if evidence is not None else collect_evidence(session or {})
        if not ev:
            return res    # 无证据集 → 不拦(可能证据还没落 session·fail-safe)
        ev_blocks = [(str(e.get("source") or e.get("url") or "?"),
                      str(e.get("text") or e.get("content") or "").lower())
                     for e in ev if isinstance(e, dict)]
        if not ev_blocks:
            return res
        hit = set()
        hit_sources = set()
        for t in toks:
            for src, blk in ev_blocks:
                if t and t in blk:
                    hit.add(t)
                    hit_sources.add(src)
                    break
        score = (len(hit) / float(len(toks))) if toks else 1.0
        res["score"] = round(score, 3)
        res["hit_tokens"] = sorted(hit)[:16]
        res["sources"] = len(hit_sources)
        if score > ALP_SUPPORTED_MIN:
            res["level"] = ALP_SUPPORTED
        elif score >= ALP_PARTIAL_MIN:
            res["level"] = ALP_PARTIAL
        else:
            res["level"] = ALP_UNSUPPORTED
        # strict_mode: unsupported 拒绝交卷·partial 警告放行·supported 放行
        if res["level"] == ALP_UNSUPPORTED and _alp_strict():
            res["should_block"] = True
            res["advice"] = _alp_advice(answer, res, ev_blocks)
    except Exception:
        return {"level": ALP_SUPPORTED, "score": 1.0, "tokens": [],
                "hit_tokens": [], "sources": 0, "should_block": False, "advice": ""}
    return res


def _alp_advice(answer, res, ev_blocks):
    """ALP unsupported 拒绝交卷的重试提示(通用·不透 gold·不代劳)。
    提示: 你交的实体在证据里没有支撑·要么从【证据里已出现的候选】里选·要么补证据。
    列出证据里出现的显著专名(供 agent 参考·不是给 gold·是它自己搜到的证据)。"""
    miss = [t for t in (res.get("tokens") or [])
            if t not in set(res.get("hit_tokens") or [])]
    # 从证据块里挖出现的显著专名(agent 自己搜到的·非 gold)供参考
    seen_ents = []
    try:
        seen = set()
        for _src, blk in (ev_blocks or [])[:12]:
            for m in re.findall(r"\b[a-z][a-z]{2,}(?:\s+[a-z][a-z]{2,}){0,3}\b", blk):
                if m in _STOPWORDS or m in seen:
                    continue
                seen.add(m)
                if len(seen_ents) < 8:
                    seen_ents.append(m)
    except Exception:
        pass
    lines = [
        "[ALP·交卷前证据绑定验证不通过·答案实体在证据里没有支撑——拒绝交卷] "
        "你要交的答案里的【核心实体】(锚: %s)只有 %.0f%% 在你【已收集的证据】里出现过"
        "(证据支撑分 %.2f < %.2f)。这极可能是【推理链末端把对的实体漂成了证据里"
        "根本没搜到过的实体】(末端幻觉/锁错实体)。"
        % ("、".join(str(t) for t in (res.get("tokens") or [])[:6]) or "?",
           100 * float(res.get("score") or 0), float(res.get("score") or 0),
           ALP_PARTIAL_MIN),
    ]
    if miss:
        lines.append("这些实体锚【在证据里找不到】: %s"
                     % "、".join(str(m) for m in miss[:8]))
    lines.append(
        "正确做法(二选一·别硬交这个证据里没有的实体):\n"
        "  ① 【从证据里已经出现过的候选里选答案】——你已搜到的证据里出现的实体才是"
        "有支撑的候选(别凭记忆/推理臆造一个证据里没有的名字);\n"
        "  ② 如果你确信这个实体对·就【补证据】: 用『\"该实体名\" + 题面关键约束』"
        "精确检索·把支撑它的证据搜回来再交(让答案实体真的出现在证据集里)。")
    if seen_ents:
        lines.append("(参考·你的证据里出现过的实体/词: %s ——若答案是其中之一请核对)"
                     % "、".join(seen_ents))
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════
# 2026-08-18 · 机制2: Self-Consistency Vote——震荡题多次采样投票
# ─────────────────────────────────────────────────────────────────────────
# 病根(Kimi 确认): LongCat MoE 架构层非确定性——同题一次对一次错(轮间波动)。
#   temperature=0 仍因 token 级路由 gating 漂移·最终答案生成不稳定。
# 机制(只对【震荡/低置信】题启用·避免所有题都跑 N 次烧 N 倍耗时):
#   触发条件(should_self_consistency)= 判别器判 tie_needs_deepdive · 或 覆盖率中等
#     无 confirmed · 或 ALP 判 partial。
#   触发时对该题的【最终答案生成】跑 N=3(MEYO_SC_VOTE_N)次独立采样·实体级投票
#     + 证据过滤(只保留在证据里出现的实体)·consensus>=0.6(多数)才锁定;不足则
#     标低置信交最高票。复用 _vote_key 做实体投票聚合。
#   fail-safe: 投票失败/采样器异常 → 退回单次答案。env 可关(MEYO_SC_VOTE_N=0/1)。
# 注意: N 次是【最终答案生成】(轻量 finalize 重采)·不是整题重跑(省耗时)。
# ═════════════════════════════════════════════════════════════════════════
SC_CONSENSUS_MIN = 0.6     # 多数派占比 >= 0.6 才锁定·否则标低置信


def _sc_vote_n():
    """采样次数 N。MEYO_SC_VOTE_N 默认 3;<=1 视为关闭(不重采)。fail-safe → 3。"""
    try:
        n = int(os.environ.get("MEYO_SC_VOTE_N", "3").strip())
        return n if n >= 1 else 0
    except Exception:
        return 3


def _sc_enabled():
    return _sc_vote_n() >= 2


def should_self_consistency(*, ic_result=None, next_action=None, alp_result=None):
    """判该题是否需要触发 Self-Consistency 多次采样投票(只对震荡/低置信题)。
    触发(任一命中·通用·不针对具体题):
      - next_action.tie_needs_deepdive(判别器判候选平局·难分伯仲);
      - ic_result 覆盖率中等(probable 段·> probable 门但 < confirm 门)且无 confirmed;
      - alp_result.level == partial(答案实体只部分被证据支撑)。
    非震荡(confirmed/唯一强候选/ALP supported)→ 不触发(不跑 N 次·省耗时)。
    fail-safe → False(读失败按不触发·绝不误触发烧耗时)。"""
    try:
        if not _sc_enabled():
            return False
        # ① 判别器平局
        if next_action and next_action.get("tie_needs_deepdive"):
            return True
        # ② ALP partial
        if alp_result and alp_result.get("level") == ALP_PARTIAL:
            return True
        # ③ 覆盖率中等·无 confirmed(probable 段)
        if ic_result:
            st = ic_result.get("state")
            cov = float((ic_result.get("coverage") or {}).get("coverage") or 0.0)
            if st == STATE_PROBABLE and COVERAGE_PROBABLE <= cov < COVERAGE_CONFIRM:
                return True
    except Exception:
        return False
    return False


def self_consistency_vote(session, question, sampler, *, base_answer="",
                          vote_key=None, n=None):
    """对震荡题的【最终答案生成】跑 N 次独立采样·实体级投票 + 证据过滤·选共识答案。

    参数:
      sampler: 无参 callable → 返回一次采样的答案文本(调用方注入·如轻量 finalize 重采)。
      base_answer: 已有的单次答案(计入投票·并作 fail-safe 退回值)。
      vote_key: 归一化投票 key 函数 (str)->str;None 时用内建(复用 qa_auto._vote_key 风格)。
      n: 采样次数;None 时读 MEYO_SC_VOTE_N。
    行为:
      ① 采 N 次(base_answer 也计一票)·每个答案过【证据过滤】(答案核心实体须在
         证据集里出现·否则该票作废——治采样漂出证据外的幻觉实体);
      ② 用 vote_key 做实体级归一聚合投票;
      ③ consensus = 最高票占比 >= 0.6(SC_CONSENSUS_MIN) → 锁定该答案(低置信=False);
         不足 → 标低置信(low_confidence=True)交最高票(仍给答案·不弃权)。
    返回 dict:
      {"answer": <选定答案原文>, "consensus": float, "low_confidence": bool,
       "n_samples": int, "n_valid": int, "votes": {key: count}, "triggered": True}。
    fail-safe: 采样器异常/无有效票 → {"answer": base_answer, "triggered": False, ...}。"""
    out = {"answer": str(base_answer or ""), "consensus": 0.0,
           "low_confidence": True, "n_samples": 0, "n_valid": 0,
           "votes": {}, "triggered": False}
    try:
        if not _sc_enabled() or sampler is None:
            return out
        vk = vote_key or _sc_vote_key
        n = n if (n and n >= 2) else _sc_vote_n()
        ev = collect_evidence(session or {})
        samples = []
        if str(base_answer or "").strip():
            samples.append(str(base_answer).strip())
        for _ in range(max(0, n - len(samples))):
            try:
                a = sampler()
            except Exception:
                a = ""
            a = str(a or "").strip()
            if a:
                samples.append(a)
        out["n_samples"] = len(samples)
        if not samples:
            return out
        # 证据过滤: 只保留【核心实体在证据里出现】的票(治采样漂出证据外幻觉)
        valid = []
        for a in samples:
            keep = True
            try:
                if ev:
                    av = alp_verify(session or {}, a, evidence=ev)
                    # 证据里完全找不到实体锚(unsupported·且抽得到锚)→ 该票作废
                    if av.get("level") == ALP_UNSUPPORTED and av.get("tokens"):
                        keep = False
            except Exception:
                keep = True
            if keep:
                valid.append(a)
        out["n_valid"] = len(valid)
        pool = valid or samples   # 全被过滤则退回全部(fail-safe·不至于无票)
        # 实体级投票聚合
        buckets = {}
        for a in pool:
            try:
                k = vk(a)
            except Exception:
                k = a[:120]
            buckets.setdefault(k, []).append(a)
        out["votes"] = {k: len(v) for k, v in buckets.items()}
        win_key = max(buckets, key=lambda k: len(buckets[k]))
        win = buckets[win_key]
        consensus = len(win) / float(len(pool)) if pool else 0.0
        out["consensus"] = round(consensus, 3)
        out["answer"] = win[0]
        out["low_confidence"] = consensus < SC_CONSENSUS_MIN
        out["triggered"] = True
    except Exception:
        return {"answer": str(base_answer or ""), "consensus": 0.0,
                "low_confidence": True, "n_samples": 0, "n_valid": 0,
                "votes": {}, "triggered": False}
    return out


def _sc_vote_key(ans):
    """归一化投票 key(复用 qa_auto_agent._vote_key 风格·抗表述差异):
    优先取数值/编号 token 排序·否则取小写去标点词集。fail-safe → 原文截断。"""
    try:
        nums = re.findall(r"\d+(?:\.\d+)+|\b\d{3,}\b", ans or "")
        if nums:
            return "|".join(sorted(set(nums)))
        words = sorted(set(re.findall(r"[a-z0-9]+", (ans or "").lower())))
        return " ".join(words)[:120]
    except Exception:
        return (ans or "").strip()[:120]


def sc_annotate(answer, vote):
    """把 Self-Consistency 投票结果标注进答案正文(低置信时打标·不改答案本身)。
    consensus 达标 → 原样返回;低置信 → 追加不确定度标注(通用·不透 gold)。fail-safe。"""
    try:
        if not vote or not vote.get("triggered"):
            return answer
        if vote.get("low_confidence"):
            note = ("\n\n> [Self-Consistency·低置信标注] 本题检测到轮间波动·已做 %d 次"
                    "独立采样投票(有效 %d 票)·最高票共识仅 %.0f%%(< %.0f%%)——答案取"
                    "最高票但置信度偏低·请复核。"
                    % (int(vote.get("n_samples") or 0), int(vote.get("n_valid") or 0),
                       100 * float(vote.get("consensus") or 0), 100 * SC_CONSENSUS_MIN))
            return str(answer or "") + note
        return answer
    except Exception:
        return answer


# ═════════════════════════════════════════════════════════════════════════
# 2026-08-19 · 候选锦标赛(CandidateTournament) · 融进目标逼近循环
# ─────────────────────────────────────────────────────────────────────────
# 用户洞察(Solanum hermannii 题实证): "cross_verify_now 触发·第二模型不同意
#   hermannii·但仲裁'保留主答案'没采纳质疑——交叉验证发现问题但决策没正确响应。"
#   核心: "每次并行搜索和收敛都应该在循环里就引入校验·优化搜索质量和是否停止·
#   这才叫小步逼近。"
#
# 病根: 现有 soft_lock 是【锁一个主答案·后续严格更优才换】——本质二选一(信主
#   或信第二模型)。多跳实体题里模型会 laciniatum→hermannii 迭代却过早锁定·而
#   cross_verify 收尾一次性仲裁也是二选一·两边都可能错(hermannii 不是正解)。
#
# 修法(治本·非二选一): 把候选管理从"锁一个"改成【锦标赛】——多候选(laciniatum/
#   hermannii/capsicoides...)并存·每个候选维护 (约束满足三态 + 覆盖率 + 来源多样
#   性) 的综合分·复用 classify_claims_tri_state / discriminator_key。循环中持续:
#     ① 每轮新证据 → 各候选重算三态覆盖(observe);
#     ② 定期淘汰弱候选(被约束【矛盾】=硬淘汰·或综合分显著垫底);
#     ③ 收敛判据: 只在 top 候选覆盖率 >= 0.9 且【显著领先】top2 时才选胜者·
#        否则 status=continue(继续搜/诚实标不确定)——治"过早锁 hermannii"。
#   **不提前锁定单一主答案**。淘汰只淘汰被【矛盾】否决的(硬反证)与综合分远低于
#   领先者的·unknown/低覆盖【绝不淘汰】(留纠错空间·同判别器语义)。
#
# 与已有机制配合(不重复): 复用四态/三态覆盖率(compute_coverage/
#   classify_claims_tri_state)做每候选评分·复用 discriminator_key 排序;
#   tournament 是【循环中多候选竞争的容器 + 收敛裁决】·soft_lock 是【单候选保守
#   锁定】——tournament 开时优先用它裁决收敛·关时退回 soft_lock(fail-safe)。
# env MEYO_NATIVE_TOURNAMENT 默认【关】(=1 开·只对 entity 多跳题开)·fail-safe
#   退回现有 soft_lock/cross_verify 路径。绝不透 gold(引擎内无 gold·只对证据↔约束
#   判一致性)·不针对具体题。
# ═════════════════════════════════════════════════════════════════════════

# 锦标赛参数(通用默认·非题目特判)
TOURNAMENT_MAX_CANDIDATES = 6       # 并存候选上限(有界·单题串行)
TOURNAMENT_ELIM_RATIO = 0.3         # 综合分 < 领先者 * 此比例 → 淘汰(且总数 > 2)
TOURNAMENT_WIN_COVERAGE = 0.90      # 胜者覆盖率下限(>=0.9 才可能选胜者)
TOURNAMENT_LEAD_MARGIN = 1          # 胜者需领先 top2 的【满足硬约束数】差(> 此值)
TOURNAMENT_ELIM_EVERY = 3           # 每 N 次 observe 触发一次弱候选淘汰


def tournament_enabled():
    """锦标赛总开关。默认【关】(只对 entity 多跳题按需开)·fail-safe 退回 soft_lock。
    MEYO_NATIVE_TOURNAMENT=1 开。"""
    try:
        return os.environ.get("MEYO_NATIVE_TOURNAMENT", "0").strip().lower() \
            in ("1", "true", "on", "yes")
    except Exception:
        return False


def _tournament_score(tri):
    """候选综合分(0-1·越高越强): 约束满足率为主·覆盖率/来源多样性为辅·
    有矛盾则重罚(沉底)。复用 discriminator_key 的优先级语义但压成标量·用于
    淘汰阈值比较(排序仍用 discriminator_key 保精确)。fail-safe → 0.0。"""
    try:
        sat = int(tri.get("satisfied") or 0)
        con = int(tri.get("contradicted") or 0)
        tot = int(tri.get("total") or 0) or 1
        cov = float(tri.get("coverage") or 0.0)
        srcs = int(tri.get("sources") or 0)
        if con > 0:
            return 0.0                      # 被矛盾 → 综合分归零(硬淘汰信号)
        sat_ratio = sat / float(tot)
        # 满足率 0.6 + 覆盖率 0.3 + 来源多样性(封顶 0.1)
        return round(sat_ratio * 0.6 + cov * 0.3 + min(srcs * 0.05, 0.1), 4)
    except Exception:
        return 0.0


class CandidateTournament:
    """候选锦标赛: 循环中多候选竞争到最后一刻·不提前锁单一主答案。

    用法(融进循环·每轮末调):
      t = CandidateTournament(claims)          # claims=extract_claims 结果
      t.observe("laciniatum", evidence)        # 每有新证据/新候选就 observe
      t.observe("hermannii",  evidence)
      status, winner, info = t.decide()        # 收敛裁决(continue/converged/no_candidates)

    - observe: 用 classify_claims_tri_state 给候选算三态·更新综合分。定期淘汰弱候选。
    - decide : 只在 top 覆盖率>=0.9 且领先 top2 硬约束数 > margin 时 status=converged;
               否则 continue(继续搜/诚实标不确定)。全被矛盾 → status=all_rejected。
    fail-safe: 任何异常不崩·decide 退回 continue(不逼交卷·不误锁)。绝不透 gold。"""

    def __init__(self, claims, *, max_candidates=TOURNAMENT_MAX_CANDIDATES,
                 elim_ratio=TOURNAMENT_ELIM_RATIO,
                 win_coverage=TOURNAMENT_WIN_COVERAGE,
                 lead_margin=TOURNAMENT_LEAD_MARGIN, llm_call=None):
        self.claims = claims or []
        self.max_candidates = int(max_candidates)
        self.elim_ratio = float(elim_ratio)
        self.win_coverage = float(win_coverage)
        self.lead_margin = int(lead_margin)
        self.llm_call = llm_call
        self.candidates = {}             # norm_entity -> {entity, tri, score, seen}
        self.eliminated = {}             # norm_entity -> reason(可观测)
        self.observe_n = 0

    def observe(self, entity, evidence, *, tri=None):
        """观测一个候选: 给它按【提到它的证据】算三态覆盖·更新综合分。
        tri 可直接注入(测试/复用已算结果);否则内部 classify。fail-safe → None。
        被矛盾否决(contradicted>0)的候选综合分归零·后续淘汰会剔除。"""
        try:
            ent = str(entity or "").strip()
            if not ent or not self.claims:
                return None
            key = _norm_entity(ent)
            if key in self.eliminated:
                return None              # 已淘汰·不复活(单调·同黑名单语义)
            if tri is None:
                # 只用【提到该候选的证据】算覆盖(治 compute_coverage 实体无关缺陷)
                ev_grounded = _evidence_mentioning(evidence or [], ent)
                tri = classify_claims_tri_state(
                    self.claims, ev_grounded, ent, self.llm_call)
            rec = self.candidates.get(key) or {"entity": ent[:200], "seen": 0}
            rec["entity"] = ent[:200]
            rec["tri"] = tri
            rec["score"] = _tournament_score(tri)
            rec["seen"] = int(rec.get("seen") or 0) + 1
            self.candidates[key] = rec
            self.observe_n += 1
            # 定期淘汰弱候选(每 N 次 observe)
            if self.observe_n % TOURNAMENT_ELIM_EVERY == 0:
                self.eliminate_weak()
            # 候选数超上限 → 立即淘汰(保留强者)
            if len(self.candidates) > self.max_candidates:
                self.eliminate_weak(force=True)
            return rec
        except Exception:
            return None

    def eliminate_weak(self, *, force=False):
        """淘汰弱候选(单调·不复活):
          ① 被约束【矛盾】(contradicted>0) → 硬淘汰(找到反证·不同于低覆盖);
          ② 综合分 < 领先者 * elim_ratio 且总数 > 2 → 淘汰(显著垫底);
          ③ force(超上限)时·把综合分最低者淘汰到 max_candidates。
        unknown/低覆盖【绝不淘汰】(留纠错空间)。fail-safe → 不淘汰。"""
        try:
            if not self.candidates:
                return
            # ① 硬淘汰: 被矛盾的候选
            for key in list(self.candidates.keys()):
                tri = self.candidates[key].get("tri") or {}
                if int(tri.get("contradicted") or 0) > 0:
                    self._eliminate(key, "约束被证据矛盾(找到反证)")
            if len(self.candidates) <= 2 and not force:
                return
            scores = [c.get("score") or 0.0 for c in self.candidates.values()]
            if not scores:
                return
            top_score = max(scores)
            # ② 综合分显著垫底(相对领先者)
            if top_score > 0 and len(self.candidates) > 2:
                for key in list(self.candidates.keys()):
                    sc = self.candidates[key].get("score") or 0.0
                    if sc < top_score * self.elim_ratio:
                        self._eliminate(key, "综合分显著垫底(%.3f < %.3f*%.2f)"
                                        % (sc, top_score, self.elim_ratio))
            # ③ 超上限强制裁到 max
            if force and len(self.candidates) > self.max_candidates:
                ordered = sorted(self.candidates.items(),
                                 key=lambda kv: kv[1].get("score") or 0.0)
                for key, _ in ordered[:len(self.candidates) - self.max_candidates]:
                    self._eliminate(key, "超候选上限·综合分最低裁汰")
        except Exception:
            pass

    def _eliminate(self, key, reason):
        try:
            if key in self.candidates:
                self.eliminated[key] = str(reason)[:160]
                del self.candidates[key]
        except Exception:
            pass

    def ranked(self):
        """当前存活候选按 discriminator_key 降序(矛盾者已淘汰·此处基本无矛盾)。
        返回 rank_candidates 结构 list。fail-safe → []。"""
        try:
            items = [{"entity": c.get("entity"), "tri": c.get("tri") or {}}
                     for c in self.candidates.values()]
            return rank_candidates(items)
        except Exception:
            return []

    def decide(self):
        """收敛裁决(核心·治过早锁定): 只在【真逼近】时选胜者·否则继续。

        返回 (status, winner_entity_or_None, info):
          - "no_candidates" : 没有存活候选(全被矛盾淘汰或还没 observe);
          - "converged"     : top 覆盖率>=win_coverage 且领先 top2 硬约束数 > margin
                              (或只剩唯一存活候选且其覆盖率达标) → winner=该候选;
          - "continue"      : 尚未收敛(覆盖率不够 / 与 top2 太接近 / 需深挖区分)——
                              继续搜/诚实标不确定·【绝不过早锁单一主答案】。
        info 含 top/second 的 (entity, satisfied, coverage) 便于观测/组装提示。
        fail-safe: 任何异常 → ("continue", None, {})(不逼交卷·不误锁)。"""
        info = {"top": None, "second": None, "n_alive": 0, "n_eliminated": 0}
        try:
            info["n_eliminated"] = len(self.eliminated)
            ranked = self.ranked()
            alive = [it for it in ranked if not it.get("rejected")]
            info["n_alive"] = len(alive)
            if not alive:
                return "no_candidates", None, info
            top = alive[0]
            top_tri = top.get("tri") or {}
            top_cov = float(top_tri.get("coverage") or 0.0)
            top_sat = int(top_tri.get("satisfied") or 0)
            info["top"] = {"entity": top.get("entity"),
                           "satisfied": top_sat, "coverage": round(top_cov, 3)}
            # 唯一存活候选 + 覆盖率达标 → 收敛(无竞争者)
            if len(alive) < 2:
                if top_cov >= self.win_coverage:
                    return "converged", top.get("entity"), info
                return "continue", None, info
            second = alive[1]
            sec_tri = second.get("tri") or {}
            sec_sat = int(sec_tri.get("satisfied") or 0)
            info["second"] = {"entity": second.get("entity"),
                              "satisfied": sec_sat,
                              "coverage": round(float(sec_tri.get("coverage") or 0.0), 3)}
            # 收敛判据: top 覆盖率达标 且【显著领先】top2(满足硬约束数差 > margin)
            if top_cov >= self.win_coverage and (top_sat - sec_sat) > self.lead_margin:
                return "converged", top.get("entity"), info
            # 否则继续(覆盖率不够 或 与 top2 太接近=需深挖区分)——绝不过早锁定
            return "continue", None, info
        except Exception:
            return "continue", None, info

    def summary(self):
        """给模型看的锦标赛状态摘要(候选并存+综合分+收敛判据)。fail-safe → ''。"""
        try:
            ranked = self.ranked()
            if not ranked:
                return ""
            status, winner, info = self.decide()
            parts = []
            for it in ranked[:5]:
                tri = it.get("tri") or {}
                parts.append("「%s」满足%d/%d约束·覆盖%.0f%%%s"
                             % (str(it.get("entity"))[:40],
                                int(tri.get("satisfied") or 0),
                                int(tri.get("total") or 0),
                                100 * float(tri.get("coverage") or 0.0),
                                "·被矛盾" if int(tri.get("contradicted") or 0) > 0 else ""))
            head = ("[候选锦标赛·%d 候选并存竞争] " % len(ranked))
            if status == "converged" and winner:
                tail = ("→ 收敛: 「%s」覆盖率达标且显著领先·可锁定交卷。" % str(winner)[:40])
            elif status == "no_candidates":
                tail = "→ 无存活候选(全被矛盾否决)·回溯重找。"
            else:
                tail = ("→ 未收敛: top 候选覆盖率未达 %.0f%% 或与次优太接近·"
                        "【别过早锁定】·继续对区分性约束补证(诚实标不确定度也行)。"
                        % (100 * self.win_coverage))
            return head + " | ".join(parts) + " " + tail
        except Exception:
            return ""
