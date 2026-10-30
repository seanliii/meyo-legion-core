# -*- coding: utf-8 -*-
import sys

for _short_name, _package_name in (
        ("perspective_engine", "qa_system.agent.perspective_engine"),):
    if __name__ == _short_name:
        sys.modules.setdefault(_package_name, sys.modules[__name__])
    elif __name__ == _package_name:
        sys.modules.setdefault(_short_name, sys.modules[__name__])
"""perspective_engine · 换位思考原语(出题人/用户视角·全局复用)

【架构级组件·2026-08-20 用户重大判断落地】
用户核心洞察: 冲击 bc 的关键 = 用 LLM 模拟【人类/出题人/用户会怎么想怎么搜怎么判断】·
而不是靠"常识联想"或"机械规则"。这是我们缺的东西——人类的模仿。覆盖所有主流路径:
native loop / auto agent / 搜索 / 召回 / 实体锁定 / finalize / 下一步规划 / 造工具。

★★铁律(用户特别强调·违反=幻觉害人)★★:
  换位思考【必须不断带入已有信息】(题面 + 已搜到的证据 + 已验证的事实)·基于这些推理·
  【绝不凭空猜/编造题面和证据里没有的实体、事实、年份】。凭空猜=幻觉·非但不帮你反而害你。
  所有产出(候选/判断/下一步)都必须能【追溯到已有信息】·带出处。

★★统一入口(2026-08-20·KM《意图-上下文驱动的统一决策函数》理想架构落地·优雅收敛)★★:
  【推荐用法】所有换位判断从单一 decide() 出口走(SoulDriver 内核):
      snap = pe.ContextSnapshot.from_session(question, session, llm_call=lc)
      pkt  = pe.decide(snap, pe.PHASE_LOCK, candidate="Matt Ox")   # 或 PHASE_RECALL/PLAN/DELIVER/BUDGET/PARADIGM
  一个 decide(五元组 snapshot, phase) 替代散落各处的直接调用——这是文档要的"把所有判断
  收敛到单一决策函数"。底层复用下面已验证的能力函数(不破坏已验证行为·向后兼容:能力函数
  仍可直接调)。

下层能力函数(decide 的内部实现·每个 = 一次带【出题人/用户视角】+【已有信息】的 LLM 调用):
  - interrogate_as_setter(PHASE_RECALL): 出题人视角——指向什么唯一实体·哪些线索是区分器·
      缺什么信息·下一步搜/算什么(带已搜证据·不重复无效方向)。
  - judge_candidate(PHASE_LOCK): 用户视角逐约束核对候选(缺证据判 unknown 不算满足·排除更有名错候选)。
  - plan_next_step(PHASE_PLAN): 规划下一步(搜什么 query / 造什么工具 / 已够则收敛 / 回退)·带已有信息。
  - check_delivery_contract(PHASE_DELIVER): 判答案是否回应题问的输出类型(治 G1 契约破)。
  - sense_topic_and_paradigm(PHASE_PARADIGM): 感知题型→动态范式(变量非写死)·带进程缓存。
  - orchestrate_budget(PHASE_BUDGET): 据题型动态定时间/步数。
  - inject: 粘合机·一行把[本题范式+进展+决策]注入任何 LLM 调用的 prompt。

通用·非题目特判·fail-safe(LLM 不可用/异常→返回中性结果·退回调用方原逻辑)。
★不卡: 题型感知进程缓存全局共享(同题多能力调用只 1 次 LLM)·热路径不接·只关键决策点接。
env MEYO_PERSPECTIVE_ENGINE=0 整体关。
"""
import os
import re
import json


def _enabled():
    # Default OFF (2026-08-25 用户: 换位思考不要了). The "换位思考/出题人视角" injected
    # hints framed the model with extra Chinese scaffolding; disabling lets the model
    # reason directly. Set MEYO_PERSPECTIVE_ENGINE=1 to restore the old behavior.
    return os.environ.get("MEYO_PERSPECTIVE_ENGINE", "0") == "1"


_CJK_RE = re.compile(r"[一-鿿]")


def _cjk_frac(text):
    t = str(text or "")
    if not t:
        return 0.0
    return len(_CJK_RE.findall(t)) / max(1, len(t))


def normalize_query_language(query, question):
    """把搜索 query 归一到题面语言(通用·不针对任何题集)。

    2026-08-24 公平基准诊断:planner 的 prompt 是中文,模型对英文事实题也会输出
    中文 query(甚至把中文交付要求当 query),英文源命中率极低。若题面基本是英文
    (CJK 占比低)而 query 含中文,剥掉中文段与全角标点,只留英文/数字/引号 anchor;
    剥空则回退原 query(fail-safe·不伤中文题:中文题面不会触发)。"""
    q = str(query or "").strip()
    if not q:
        return q
    # 题面是中文题 → 不动(中文车道本来就该用中文)。
    if _cjk_frac(question) > 0.20:
        return q
    # 英文题但 query 不含中文 → 不动。
    if not _CJK_RE.search(q):
        return q
    # 英文题 + query 混入中文:剥中文与全角标点,保留 ASCII 词/数字/引号短语。
    stripped = re.sub(r"[一-鿿　-〿＀-￯]+", " ", q)
    stripped = re.sub(r"\s+", " ", stripped).strip(" =·,.:;\"'`")
    tokens = [t for t in stripped.split() if t]
    # 中文交付要求被剥后若只剩 "20-30"/年份等数字壳,不是可用英文 query。
    # 返回空让上层换向/回退,而不是把数字壳送进搜索。
    alpha = [t for t in tokens if re.search(r"[A-Za-z]{3,}", t)]
    if not alpha:
        return ""
    return " ".join(tokens)


def _call(llm_call, prompt, max_tokens=800):
    """统一 LLM 调用(优先传入的闭包·兜底 qa_llm_gateway)。fail-safe → ''。"""
    try:
        if llm_call is not None:
            r = llm_call(prompt)
            return str(r or "")
    except Exception:
        pass
    try:
        import qa_llm_gateway as _gw
        r = _gw.call(prompt, tier="mid", timeout=40, max_tokens=max_tokens)
        return (r.get("out") or "") if isinstance(r, dict) else ""
    except Exception:
        return ""


def _evidence_digest(evidence, max_items=12, max_chars=2400):
    """把已搜证据压成【带出处】的摘要文本(供 LLM 带入已有信息·防幻觉的关键)。
    只截取标题+片段·标 [E1]/[E2]… 供 LLM 引用出处。fail-safe → ''。"""
    try:
        lines = []
        for i, e in enumerate(evidence or []):
            if i >= max_items:
                break
            if isinstance(e, dict):
                t = str(e.get("title") or e.get("url") or "")[:80]
                s = str(e.get("snippet") or e.get("text") or e.get("content") or "")[:220]
            else:
                t, s = "", str(e)[:220]
            seg = ("[E%d] %s · %s" % (i + 1, t, s)).strip()
            if seg:
                lines.append(seg)
        out = "\n".join(lines)
        return out[:max_chars]
    except Exception:
        return ""


# ── 防幻觉后处理: 校验 LLM 产出的实体/query 只用了已有信息 ──────────────────
def _grounded_terms(text):
    """把一段文本(题面+证据)拆成可引用的词集(小写·实义词)·供 grounding 校验。"""
    try:
        return set(t for t in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(t) >= 3)
    except Exception:
        return set()


def _strip_fabricated_entities(cands, grounded_text):
    """剔除【已有信息里没有的大写专名】(疑似 LLM 凭空编的实体)。
    通用防幻觉: 候选/query 里出现的多词大写专名·若其主体词都不在已有信息里→大概率是编的。
    保守: 只剔【完全不沾边】的(所有实义词都不在已有信息)·避免误杀合理改写。fail-safe→原样。"""
    try:
        gt = _grounded_terms(grounded_text)
        if not gt:
            return cands
        out = []
        for c in cands:
            cs = str(c or "").strip()
            if not cs:
                continue
            # 该候选里的实义词
            toks = [t for t in re.findall(r"[a-z0-9]+", cs.lower()) if len(t) >= 3]
            if not toks:
                out.append(cs)
                continue
            # 有任意实义词沾边已有信息→保留;完全不沾边(可能编的新实体)→剔
            if any(t in gt for t in toks):
                out.append(cs)
        return out
    except Exception:
        return cands


def _parse_lines(resp, max_n=8):
    """从 LLM 回复抽干净的行(剔序号/markdown/前言)。"""
    out = []
    _bad = ("note", "example", "以下", "如下", "首先", "接下来", "因此", "answer",
            "候选", "锚点", "综上", "总结", "let me", "here", "```")
    for line in str(resp or "").splitlines():
        s = re.sub(r"^[\d\.\-\*、·)）\s#>]+", "", line).strip().strip('"“”\'‘’ ')
        if not s or len(s) < 2 or len(s) > 160:
            continue
        sl = s.lower()
        if any(b in sl for b in _bad):
            continue
        out.append(s)
        if len(out) >= max_n:
            break
    return out


# ════════════════════════════════════════════════════════════════════
# 能力 1: 出题人视角审问(interrogate)——理解题目 + 找区分器 + 判缺口
# ════════════════════════════════════════════════════════════════════
def interrogate_as_setter(question, evidence=None, *, llm_call=None,
                          verified_facts=None, max_cands=5):
    """【出题人视角】审问这道题。带入已有信息(题面+已搜证据+已验证事实)·产出:
      - discriminators: 最能区分候选/锁定唯一实体的【区分器线索组合】(来自题面)。
      - search_queries: 下一步该搜的精确 query(含区分器·防泛词撒网·防重复无效方向)。
      - missing_info: 当前还缺哪些关键信息(哪条约束还没证据)。
      - candidates: 基于【已搜证据】能支持的候选实体(带出处·非凭空联想)。
    ★防幻觉: candidates 必须来自已搜证据(带 [E?] 出处)·query 只用题面/证据里的词。
    返回 dict·fail-safe → 全空(调用方退回原逻辑)。"""
    res = {"discriminators": [], "search_queries": [], "missing_info": [],
           "candidates": [], "raw": ""}
    if not _enabled():
        return res
    q = str(question or "").strip()
    if not q:
        return res
    ev_digest = _evidence_digest(evidence or [])
    vf = ""
    if verified_facts:
        vf = "\n【已验证事实】:\n" + "\n".join(
            "- " + str(x)[:200] for x in (verified_facts or [])[:8])
    prompt = (
        "你【扮演出这道多跳检索题的人】。你心里有一个【唯一确定的答案实体】·题面每个线索"
        "都是你【精心挑来指向那一个实体、排除掉所有相似(尤其更有名)候选】的区分器。\n\n"
        "题面:\n" + q[:800] + "\n\n"
        + ("【已搜到的证据(带出处·你的判断必须基于这些·不许编证据里没有的)】:\n"
           + ev_digest + "\n" if ev_digest else "【目前还没搜到证据】\n")
        + vf + "\n\n"
        "★铁律: 只依据【题面 + 上面已搜证据 + 已验证事实】推理·【绝不编造这些信息里没有的"
        "实体/事实/年份】(编=幻觉·会害了整条推理)。\n\n"
        "以出题人视角回答(每节内容各起一行·用下列标签开头):\n"
        "DISCRIMINATOR: <最能区分候选的线索组合·1条一行·来自题面·别用出生年代/职业这种宽泛线索>\n"
        "QUERY: <下一步该搜的精确 query·含区分器细节·3-10词·别整句撒网·别重复已试过的无效方向>\n"
        "MISSING: <当前还缺哪条关键信息才能锁定唯一答案·1条一行>\n"
        "CANDIDATE: <仅当已搜证据里【已出现】支持某候选时才列·必须带 [E?] 出处·没有就别写>\n"
        "★提醒: 答案很可能【不是最有名的那个】(最有名的常被某条线索排除)·别往最知名的猜。"
    )
    resp = _call(llm_call, prompt, max_tokens=900)
    res["raw"] = resp[:2000]
    grounded = q + " " + ev_digest + " " + vf
    for line in str(resp or "").splitlines():
        s = line.strip()
        if s.upper().startswith("DISCRIMINATOR:"):
            v = s.split(":", 1)[1].strip()
            if v:
                res["discriminators"].append(v[:160])
        elif s.upper().startswith("QUERY:"):
            v = s.split(":", 1)[1].strip().strip('"“”')
            if v:
                v = normalize_query_language(v, q)
                if v:
                    res["search_queries"].append(v[:120])
        elif s.upper().startswith("MISSING:"):
            v = s.split(":", 1)[1].strip()
            if v:
                res["missing_info"].append(v[:160])
        elif s.upper().startswith("CANDIDATE:"):
            v = s.split(":", 1)[1].strip()
            # 清洁: 去 [E?] 出处标记 + 中文说明括号 + 破折号后的解释·只留实体名
            #   (治"Kid Trunks [E1]（被霸凌…）"污染下游 _cand_ent)。通用·fail-safe。
            v = re.sub(r"\[E\d+\]", "", v)
            v = re.sub(r"[（(].*?[）)]", "", v)
            v = re.split(r"[·—-]{1,2}|[·:：]", v)[0].strip()
            v = v.strip(" 、,。.\"'")
            if v and len(v) >= 2:
                res["candidates"].append(v[:80])
    # ★防幻觉: 候选必须沾边已有信息(证据/题面)·剔凭空编的
    res["candidates"] = _strip_fabricated_entities(res["candidates"], grounded)
    return res


# ════════════════════════════════════════════════════════════════════
# 能力 2: 用户视角逐约束核对候选(judge)——缺证据不算满足·排除更有名的错候选
# ════════════════════════════════════════════════════════════════════
def judge_candidate(question, candidate, evidence, *, llm_call=None, claims=None):
    """【用户/出题人视角】判候选是否是题目指向的唯一答案。带入已搜证据逐约束核对。
    ★关键(方法D 实验验证有效): 【缺证据的约束判 unknown·不算满足】——这样更有名但缺
      传记细节证据的错候选会被比下去。返回:
      {verdict: "match"|"reject"|"insufficient", satisfied:n, unknown:n,
       contradicted:n, reason, per_constraint:[...]}。
    fail-safe → verdict="insufficient"(不误锁·交回调用方)。"""
    res = {"verdict": "insufficient", "satisfied": 0, "unknown": 0,
           "contradicted": 0, "reason": "", "per_constraint": []}
    if not _enabled():
        return res
    cand = str(candidate or "").strip()
    q = str(question or "").strip()
    if not cand or not q:
        return res
    ev_digest = _evidence_digest(evidence or [])
    if not ev_digest:
        res["reason"] = "无证据·无法核对"
        return res
    prompt = (
        "你在核对一道多跳检索题的候选答案是否【真的是题目指向的那个唯一实体】。\n\n"
        "题面:\n" + q[:700] + "\n\n候选答案: " + cand[:100] + "\n\n"
        "【已搜到的证据(核对只能依据这些·不许用你的常识脑补候选满足某约束)】:\n"
        + ev_digest + "\n\n"
        "逐条核对题面每个约束·对该候选判三态:\n"
        "  satisfied = 证据【明确支持】候选满足此约束(引 [E?])\n"
        "  contradicted = 证据【明确反驳】\n"
        "  unknown = 证据【既不支持也不反驳】(★没证据就判 unknown·【绝不能因为'看起来像'"
        "或'它很有名应该符合'就判 satisfied】——那是幻觉·会锁错更有名的实体)\n\n"
        "输出格式(每约束一行): CONSTRAINT: <约束> | <satisfied/contradicted/unknown> | <依据[E?]或'无证据'>\n"
        "最后一行: VERDICT: <match=全部satisfied无矛盾 / reject=有contradicted / "
        "insufficient=有unknown还需补证据>"
    )
    resp = _call(llm_call, prompt, max_tokens=800)
    res["reason"] = resp[:600]
    for line in str(resp or "").splitlines():
        s = line.strip()
        if s.upper().startswith("CONSTRAINT:"):
            body = s.split(":", 1)[1]
            st = "unknown"
            low = body.lower()
            if "satisfied" in low:
                st = "satisfied"
            elif "contradicted" in low:
                st = "contradicted"
            res["per_constraint"].append({"text": body[:160], "state": st})
            res[st] = res.get(st, 0) + 1
        elif s.upper().startswith("VERDICT:"):
            v = s.split(":", 1)[1].strip().lower()
            if "match" in v:
                res["verdict"] = "match"
            elif "reject" in v:
                res["verdict"] = "reject"
            else:
                res["verdict"] = "insufficient"
    # 一致性兜底: 有 contradicted→reject; 全 satisfied 无 unknown→match
    if res["contradicted"] > 0:
        res["verdict"] = "reject"
    elif res["unknown"] == 0 and res["satisfied"] > 0 and res["contradicted"] == 0:
        res["verdict"] = "match"
    elif res["unknown"] > 0:
        res["verdict"] = "insufficient"
    return res


# ════════════════════════════════════════════════════════════════════
# 能力 3: 规划下一步(plan_next)——搜什么/造什么工具/收敛·带已有信息
# ════════════════════════════════════════════════════════════════════
def plan_next_step(question, evidence, *, llm_call=None, tried_queries=None,
                   current_candidate=None):
    """【解题人视角】规划下一步·带入已搜证据+已试过的方向(不重复无效)。产出:
      {action: "search"|"build_tool"|"converge"|"backtrack",
       query: <若 search·精确 query>, tool_need: <若 build_tool·要什么工具>,
       reason}。fail-safe → action="search" 空(退回调用方)。"""
    res = {"action": "search", "query": "", "tool_need": "", "reason": ""}
    if not _enabled():
        return res
    q = str(question or "").strip()
    if not q:
        return res
    ev_digest = _evidence_digest(evidence or [])
    tried = ""
    if tried_queries:
        tried = "\n【已试过的搜索方向(别重复·换角度)】:\n" + "\n".join(
            "- " + str(x)[:80] for x in (tried_queries or [])[:10])
    cc = ("\n【当前候选】: " + str(current_candidate)[:80]) if current_candidate else ""
    prompt = (
        "你在解一道多跳检索题·像一个熟练的人那样规划【下一步最该做什么】。\n\n"
        "题面:\n" + q[:600] + "\n\n"
        + ("【已搜到的证据】:\n" + ev_digest + "\n" if ev_digest else "【还没搜到有用证据】\n")
        + cc + tried + "\n\n"
        "★只依据以上已有信息判断·别编证据里没有的东西。选一个动作:\n"
        "  search = 还缺关键信息·给一条【精确 query】(含题面里最独特的区分器·不是泛词·"
        "不重复上面试过的无效方向)\n"
        "  build_tool = 缺的是【需要计算/解析/调专门数据源才能得到的信息】(如算距离/解析结构化"
        "数据/查专门数据库)·说明要什么工具\n"
        "  converge = 已有证据已足够锁定唯一答案·该收敛交付\n"
        "  backtrack = 当前方向被证据否掉·该回退换实体\n\n"
        "输出:\nACTION: <search/build_tool/converge/backtrack>\n"
        "QUERY: <若 search·精确 query·3-10词>\nTOOL: <若 build_tool·要什么工具>\n"
        "REASON: <一句·基于哪条已有信息做此判断>"
    )
    resp = _call(llm_call, prompt, max_tokens=500)
    for line in str(resp or "").splitlines():
        s = line.strip()
        u = s.upper()
        if u.startswith("ACTION:"):
            v = s.split(":", 1)[1].strip().lower()
            for a in ("search", "build_tool", "converge", "backtrack"):
                if a in v:
                    res["action"] = a
                    break
        elif u.startswith("QUERY:"):
            _rawq = s.split(":", 1)[1].strip().strip('"“”')
            res["query"] = normalize_query_language(_rawq, question)[:120]
        elif u.startswith("TOOL:"):
            res["tool_need"] = s.split(":", 1)[1].strip()[:200]
        elif u.startswith("REASON:"):
            res["reason"] = s.split(":", 1)[1].strip()[:200]
    return res


# ════════════════════════════════════════════════════════════════════
# 能力 4: 交付契约检查(check_delivery)——用户视角判答案是否回应题目所问的类型
# ════════════════════════════════════════════════════════════════════
def check_delivery_contract(question, answer, *, llm_call=None):
    """【用户视角】判交付的答案是否【回应了题目问的那个东西/输出类型】。
    治 G1 契约破: 题问'real name(真名)'却交 stage name(艺名)、题问'哪一年'却交事件名、
    题问'城市'却交国家——即使指向同一实体/相关·也没回应题问。带入题面(问什么)+答案(给什么)。
    ★防幻觉: 只判【形式/类型是否匹配题问】·不判答案对错·不引入新信息。
    返回 {ok: bool, expected: <题目要的输出类型>, issue: <若不匹配·问题描述>}。
    fail-safe → ok=True(不误拦·交回调用方)。"""
    res = {"ok": True, "expected": "", "issue": ""}
    if not _enabled():
        return res
    q = str(question or "").strip()
    a = str(answer or "").strip()
    if not q or not a:
        return res
    # ★2026-08-20(真实trace回测发现·治漏看尾部答案): final 常是"大段约束↔证据映射说明 +
    #   尾部 Exact Answer: X"·只看前段会漏掉真答案误判 ok=False。优先抽【Exact Answer/
    #   答案行】·连同前段一起给(答案行放最前·确保被看到)。fail-safe。
    _a_for_judge = a
    try:
        _m = re.search(r"(?:exact\s*answer|答案|final\s*answer)\s*[:：]\s*([^\n]{1,120})",
                       a, re.I)
        if _m:
            _a_for_judge = "【最终答案】" + _m.group(1).strip() + "\n\n【完整回答】" + a
    except Exception:
        _a_for_judge = a
    prompt = (
        "从【提问者视角】判断:这个答案【回应了题目问的那个东西吗】?只看【类型/形式是否对上"
        "题目所问】·不判答案内容对错·不引入题面/答案之外的信息。\n\n"
        "题目:\n" + q[:600] + "\n\n答案:\n" + _a_for_judge[:800] + "\n\n"
        "例:题问'真名(real name)'但答案给的是艺名/昵称→没回应(类型不符);题问'哪一年'"
        "但答案给的是事件名而非年份→没回应;题问'城市名'但给了国家→没回应。\n"
        "输出:\nEXPECTED: <题目要求的输出类型·如 人的真名/年份/城市名/数量…>\n"
        "OK: <yes=答案形式回应了题问 / no=类型不符没回应>\n"
        "ISSUE: <若 no·一句说明答案给的是什么、题目其实要什么>"
    )
    resp = _call(llm_call, prompt, max_tokens=300)
    for line in str(resp or "").splitlines():
        s = line.strip()
        u = s.upper()
        if u.startswith("EXPECTED:"):
            res["expected"] = s.split(":", 1)[1].strip()[:120]
        elif u.startswith("OK:"):
            v = s.split(":", 1)[1].strip().lower()
            if v.startswith("no") or "不符" in v or "没回应" in v:
                res["ok"] = False
        elif u.startswith("ISSUE:"):
            res["issue"] = s.split(":", 1)[1].strip()[:200]
    return res


# ════════════════════════════════════════════════════════════════════
# ★★灵魂驾驶员(pilot)· 2026-08-20 用户愿景 · 上下文+意图=系统的粘合机
# ════════════════════════════════════════════════════════════════════
# 用户: 他是系统的灵魂驾驶员——本质=【上下文 + 人的意图】·在所有判断点被调用·
#   动态演进出【适合当前题目/上下文的判断、约束、范式】(变量而非写死常量)。
#   能感知题型→切对应范式(人物/文学/科技/日常/调研…·不写死人物题范式)·统一编排
#   (时间/步数/搜索/造工具/拆解)·审计并动态更新前置 prompt。
# ★优雅+不卡的关键: 【进程级缓存】——同一题(问题指纹)的题型/范式只算一次·后续 O(1)。
#   分级: 便宜规则先跑·只在题型感知这一次调 LLM(每题一次·可缓存)·不是每步都调。

_PILOT_CACHE = {}   # qfingerprint -> {"topic":..., "paradigm":..., "output_type":...}


def _qfp(question, session=None):
    """题目指纹(用于缓存·同题不重复调 LLM)。
    ★2026-08-20 用户洞察: 多轮对话/新session也是更新时机——同题面但用户追加了新意图/新
    上下文时·缓存不能复用旧意图向量。指纹纳入 session 的【意图版本】(qid+对话轮次)·
    新意图→新指纹→重新感知。fail-safe: 无session退回纯题面指纹(bc单题场景不变)。"""
    try:
        import hashlib
        _base = str(question or "")[:400]
        # session 维度: qid + 用户消息轮次(多轮追加新意图时轮次变→指纹变→重感知)
        if isinstance(session, dict):
            _qid = str(session.get("qid") or "")
            # 对话轮次: intent_ledger 的更新次数 / tool_log 的用户消息数(近似意图版本)
            _turn = str(session.get("_intent_version")
                        or len(session.get("intent_ledger") or [])
                        or "")
            _base = _qid + "|" + _turn + "|" + _base
        return hashlib.md5(_base.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return str(question or "")[:64]


def sense_topic_and_paradigm(question, *, llm_call=None, session=None):
    """★感知【意图向量】+动态产出对应范式(变量非写死)。带进程级缓存(同题一次)。
    ★2026-08-20 用户洞察: 不该只按【题型】(太窄)——意图/目标/主题/约束更灵活。升级成
    意图向量: primary_intent(事实查询/推理计算/调研报告/动作执行) + topic(领域·辅助) +
    goal(真正要达成的目标) + key_constraints(最能区分的约束) + output_type + paradigm。
    这样【任何意图/主题/约束组合】都能被感知·非硬套7种题型。向后兼容(保留topic等字段)。
    返回 {primary_intent, topic, goal, key_constraints, output_type, paradigm,
          discriminator_hint}。fail-safe→通用。"""
    _default = {
        "primary_intent": "fact_lookup", "topic": "general", "goal": "",
        "key_constraints": [],
        "output_type": "",
        "paradigm": ("找题面里最具体、最独特的约束组合去锁定唯一答案·别只对宽泛线索"
                     "(那会收敛到最有名的错候选)。"),
        "discriminator_hint": "最具体、最独特的约束组合",
        "dynamic_tags": {},   # ★LLM 自写的动态标签(自增自改·非写死字段)
    }
    if not _enabled():
        return _default
    q = str(question or "").strip()
    if not q:
        return _default
    fp = _qfp(q, session)
    if fp in _PILOT_CACHE:
        return _PILOT_CACHE[fp]
    # ★① 本轮题面 + session 前轮上下文(治多轮对话短问依赖前文)。只带【已提炼的意图/结论
    #   摘要】·不塞全 session 原文(太长/旧内容干扰)。fail-safe→只本轮。
    _ctx_prefix = ""
    try:
        if isinstance(session, dict):
            _prev = []
            # 前轮意图账本(已提炼·非原文)
            for _it in (session.get("intent_ledger") or [])[-3:]:
                _t = str((_it.get("summary") if isinstance(_it, dict) else _it) or "")[:120]
                if _t:
                    _prev.append(_t)
            # 前轮结论(narrative/final 摘要)
            _pf = str(session.get("final") or session.get("narrative") or "")[:200]
            if _pf:
                _prev.append("前轮结论: " + _pf)
            if _prev:
                _ctx_prefix = "【前轮对话上下文(理解本轮意图用·本轮可能依赖前文)】:\n" + \
                    "\n".join("- " + x for x in _prev) + "\n\n"
    except Exception:
        _ctx_prefix = ""
    prompt = (
        "你是解题系统的【灵魂驾驶员】。判断这道题的【意图向量】(别只归题型·抓意图+目标+约束"
        "这些更本质的)。\n\n" + _ctx_prefix + "本轮题面:\n" + q[:700] + "\n\n"
        "输出(每行一个标签·简短):\n"
        "INTENT: <主意图·fact_lookup事实查询/reasoning推理计算/research调研/action动作执行>\n"
        "TOPIC: <领域·辅助·如 person/geography/literature/tech/numeric/其它>\n"
        "GOAL: <一句·这题真正要达成什么(比题型具体·若依赖前轮上下文要结合)>\n"
        "CONSTRAINTS: <最能区分唯一答案的2-3个关键约束·分号隔开·来自题面别猜答案>\n"
        "OUTPUT_TYPE: <最终要什么类型的答案·如 人的真名/年份/城市名/数量>\n"
        "PARADIGM: <一句·这题该怎么搜怎么判断最高效(用什么源/工具/思路)>\n"
        "处理任何实体识别题时，把目标写成变量关系图：未知目标、已知约束、候选实体、"
        "已证关系、缺失关系。每次只选择最能区分候选的一条缺失关系去补证，禁止预设"
        "某个领域的答案类别或照题目线索写固定搜索秘诀。\n"
        "TAGS: <★你【自己决定】这道题还有哪些值得标注的关键维度(自由命名·如 时效性=高/"
        "有歧义轴=读法/需计算=是/多跳=3跳/易撞名人陷阱=是…只标这道题真正相关的·k=v 用分号隔开)>"
    )
    r = _call(llm_call, prompt, max_tokens=360)
    out = dict(_default)
    out["dynamic_tags"] = {}
    for line in str(r or "").splitlines():
        s = line.strip()
        u = s.upper()
        if u.startswith("INTENT:"):
            out["primary_intent"] = s.split(":", 1)[1].strip()[:30] or out["primary_intent"]
        elif u.startswith("GOAL:"):
            out["goal"] = s.split(":", 1)[1].strip()[:160]
        elif u.startswith("CONSTRAINTS:"):
            _cs = s.split(":", 1)[1].strip()
            out["key_constraints"] = [c.strip() for c in re.split(r"[;；]", _cs) if c.strip()][:4]
        elif u.startswith("TAGS:"):
            # ★LLM 自写动态标签: 解析 k=v;k=v(自由命名·自增自改)
            _tg = s.split(":", 1)[1].strip()
            for _kv in re.split(r"[;；]", _tg):
                if "=" in _kv:
                    _k, _v = _kv.split("=", 1)
                    _k, _v = _k.strip()[:24], _v.strip()[:40]
                    if _k and _v:
                        out["dynamic_tags"][_k] = _v
        elif u.startswith("TOPIC:"):
            out["topic"] = s.split(":", 1)[1].strip()[:40] or out["topic"]
        elif u.startswith("OUTPUT_TYPE:"):
            out["output_type"] = s.split(":", 1)[1].strip()[:60]
        elif u.startswith("DISCRIMINATOR:"):
            out["discriminator_hint"] = s.split(":", 1)[1].strip()[:160] or out["discriminator_hint"]
        elif u.startswith("PARADIGM:"):
            _p = s.split(":", 1)[1].strip()
            if _p:
                out["paradigm"] = _p[:300]
    if r:   # 只在真拿到 LLM 结果时缓存(空结果不缓存·下次重试)
        _PILOT_CACHE[fp] = out
    return out


def pilot_stance(question, *, llm_call=None, session=None):
    """★动态范式指引(替代写死的固定 stance)·据意图向量定制。给 system_prompt/关键节点注入。
    用户: 他能被调用时知道这题的意图·切对应范式。带缓存·不卡。fail-safe→通用。
    ★传 session 则带多轮上下文感知意图·并把 LLM 自写的动态标签一并注入(自由维度)。"""
    if not _enabled():
        return ""
    try:
        tp = sense_topic_and_paradigm(question, llm_call=llm_call, session=session)
        _kc = tp.get("key_constraints") or []
        _kc_txt = "; ".join(_kc[:3]) if _kc else (tp.get("discriminator_hint") or "")
        # ★LLM 自写的动态标签(自由维度·如 多跳/易撞名陷阱/时效性…)一并注入给主脑
        _tags = tp.get("dynamic_tags") or {}
        _tags_txt = ("· 本题关键提示(驾驶员自动标注): "
                     + " ｜ ".join("%s=%s" % (k, v) for k, v in list(_tags.items())[:6]) + "\n") \
            if _tags else ""
        return (
            "【灵魂驾驶员·本题意图向量(动态定制)】\n"
            "· 意图=%s ｜ 领域=%s\n"
            "· 本题目标: %s\n"
            "· 最能锁唯一答案的关键约束: %s\n"
            "· 突破口范式: %s\n"
            "· 要的答案类型: %s\n"
            "%s"
            "★铁律: 所有判断基于【已查到的信息(题面+已搜证据)】·别猜最有名的(常被约束排除)·"
            "缺证据的约束判'未知'不算满足·绝不编造证据里没有的实体/事实(=幻觉害你)。" % (
                tp.get("primary_intent", "fact_lookup"), tp.get("topic", "general"),
                tp.get("goal", "") or "见题面", _kc_txt, tp.get("paradigm", ""),
                tp.get("output_type", "") or "见题面", _tags_txt))
    except Exception:
        return ""


def orchestrate_budget(question, *, llm_call=None, base_wall_s=900, base_max_iter=30):
    """★统一编排建议:据题型/难度动态给【时间/步数预算】(变量非写死常量)。
    用户: 时间和步数也该他统一控制。简单题少给(省时不卡)·多跳难题多给。带缓存·fail-safe→base。
    返回 {wall_s, max_iter, reason}。只是【建议】·调用方结合硬上限用(绝不超过硬顶)。"""
    _out = {"wall_s": base_wall_s, "max_iter": base_max_iter, "reason": "default"}
    if not _enabled():
        return _out
    try:
        tp = sense_topic_and_paradigm(question, llm_call=llm_call)
        # 规则档(便宜·不额外调 LLM·复用已缓存的题型):numeric_compute/daily 通常步数少;
        #   多约束 person/tech 多跳题步数多。用题型 + 题面约束数粗估。
        q = str(question or "")
        _n_constraints = q.count("- ") + q.count("；") + q.count(". ")  # 粗估约束条数
        topic = str(tp.get("topic") or "").lower()
        if any(k in topic for k in ("numeric", "compute", "daily")):
            _out.update(wall_s=int(base_wall_s * 0.7), max_iter=max(12, int(base_max_iter * 0.6)),
                        reason="计算/日常题·步数少即可(省时)")
        elif _n_constraints >= 5 or any(k in topic for k in ("person", "tech", "research")):
            _out.update(wall_s=base_wall_s, max_iter=base_max_iter,
                        reason="多约束多跳题·给足预算逼近")
        else:
            _out.update(wall_s=int(base_wall_s * 0.85), max_iter=base_max_iter,
                        reason="标准题")
    except Exception:
        pass
    return _out


# ════════════════════════════════════════════════════════════════════
# ★粘合机·一行注入(用户: 所有有LLM的地方都可动态插入最新上下文·最优雅的方式)
# ════════════════════════════════════════════════════════════════════
def inject(base_prompt, question, *, session=None, llm_call=None, what=""):
    """★任何调 LLM 的地方·一行把【本题范式(题型定制)+当前上下文】注入进它的 prompt。
    这是"灵魂驾驶员=所有地方的粘合机"的最优雅形态: 每个 LLM 调用走过去时·都带上
    本题的意图+范式+进展·而不是各自裸调。

    用法: prompt = pe.inject(my_prompt, question, session=s, what="选择搜索源")
    ★不卡: 题型范式带进程缓存(同题一次)·上下文摘要是轻量字符串拼接(无额外LLM)。
    ★防幻觉: 注入的是【题面范式+已有信息摘要】·不注入任何编造内容。
    fail-safe → 原样返回 base_prompt(注入失败绝不拖累调用方)。"""
    try:
        if not _enabled() or not base_prompt:
            return base_prompt
        _parts = []
        # ① 本题范式(题型定制·缓存·不卡)
        try:
            tp = sense_topic_and_paradigm(question, llm_call=llm_call)
            _parts.append("[本题范式·题型=%s] 突破口: %s ｜ 区分器: %s ｜ 要的答案类型: %s"
                          % (tp.get("topic", "?"), str(tp.get("paradigm", ""))[:140],
                             str(tp.get("discriminator_hint", ""))[:80],
                             tp.get("output_type", "") or "见题面"))
        except Exception:
            pass
        # ② 当前上下文摘要(已锁候选/已搜证据条数·轻量·无 LLM)
        if isinstance(session, dict):
            try:
                _cand = str(session.get("_ic_kept_candidate") or "")[:60]
                _nev = len(session.get("evidence") or [])
                _ctx = []
                if _cand:
                    _ctx.append("当前候选=%s" % _cand)
                if _nev:
                    _ctx.append("已搜证据%d条" % _nev)
                if _ctx:
                    _parts.append("[当前进展] " + " · ".join(_ctx))
            except Exception:
                pass
        if what:
            _parts.append("[本次决策] " + str(what)[:120])
        if not _parts:
            return base_prompt
        _pilot_block = ("【灵魂驾驶员·带入本题意图+上下文(基于已有信息·别编造)】\n"
                        + "\n".join(_parts) + "\n\n")
        return _pilot_block + str(base_prompt)
    except Exception:
        return base_prompt


# ════════════════════════════════════════════════════════════════════
# ★★★SoulDriver 统一决策内核(2026-08-20·KM文档理想架构落地·优雅收敛)
# ════════════════════════════════════════════════════════════════════
# KM《意图-上下文驱动的统一决策函数》: 把分散的判断收敛到【单一 decide() 决策函数 +
#   五元组 ContextSnapshot】。设计原则(优雅+不破坏已验证能力):
#   - ContextSnapshot: 从 session 一次性构造的统一上下文包(意图/状态/证据/资源/经验)。
#   - decide(snapshot, phase): 单一决策入口·按 phase 分派到【已验证的能力函数】·返回
#     统一 DecisionPacket。底层能力函数(interrogate/judge/plan/…)保持不动=不破坏已验证行为。
#   - 各接入点【可选】走 decide()(统一出口·优雅)或继续直接调能力函数(向后兼容)。
#   这是"表面收敛成单一内核·底层复用已验证能力"的优雅重构·非推倒重来。


class ContextSnapshot:
    """五元组上下文包(KM文档): 意图 + 系统状态 + 证据 + 资源 + 经验。
    从 session 一次性构造·各能力函数从它取所需·避免每处重复组装。轻量·无LLM。"""
    __slots__ = ("question", "session", "evidence", "candidate", "answer",
                 "tried_queries", "claims", "llm_call", "system_state")

    def __init__(self, question, *, session=None, evidence=None, candidate=None,
                 answer=None, tried_queries=None, claims=None, llm_call=None):
        self.question = str(question or "")
        self.session = session if isinstance(session, dict) else {}
        # 证据: 显式传入 > session.evidence
        self.evidence = evidence if evidence is not None else (self.session.get("evidence") or [])
        self.candidate = candidate or self.session.get("_ic_kept_candidate") or ""
        self.answer = answer or self.session.get("final") or ""
        self.tried_queries = tried_queries or []
        self.claims = claims
        self.llm_call = llm_call or self.session.get("_llm_call")
        # 系统状态(轻量·无LLM): 步数/证据量/是否有候选
        self.system_state = {
            "n_evidence": len(self.evidence) if hasattr(self.evidence, "__len__") else 0,
            "has_candidate": bool(self.candidate),
            "n_tried": len(self.tried_queries),
        }

    @classmethod
    def from_session(cls, question, session, **kw):
        return cls(question, session=session, **kw)


# 决策阶段(phase)——对应 KM 文档的判断点类型
PHASE_RECALL = "recall"       # 召回/理解题目(找区分器+下一步搜什么+基于证据的候选)
PHASE_PLAN = "plan"           # 规划下一步(搜/造工具/收敛/回退)
PHASE_LOCK = "lock"           # 实体锁定(逐约束核对候选·缺证据不满足)
PHASE_DELIVER = "deliver"     # 交付契约(答案是否回应题问类型)
PHASE_BUDGET = "budget"       # 时间/步数预算编排
PHASE_PARADIGM = "paradigm"   # 题型感知→范式
PHASE_DIAGNOSE = "diagnose"   # 卡点诊断(根因+出路·治绕圈)
PHASE_VERIFY = "verify"       # 交付前终检(三源会师)
PHASE_FLIGHT = "flight"       # ★开局全局预判(复杂度→机制清单+预期步数·一次换N次)
PHASE_REVIEW = "review"       # ★运行时复盘(证据/关系图→保留、扩展或回溯假设)


def decide(snapshot, phase, *, candidate=None, answer=None):
    """★★统一决策函数(KM文档理想架构的单一出口)。按 phase 分派到已验证能力函数·
    返回统一 DecisionPacket(dict)。所有换位判断都可从这一个入口出。
    ★优雅: 一个 decide() 替代散落各处的直接调用;★不破坏: 底层复用已验证能力函数。
    fail-safe → {"phase":phase, "ok":True}(中性·调用方退回原逻辑)。

    用法(接入点可选统一走这里):
      snap = pe.ContextSnapshot.from_session(question, session, llm_call=lc)
      pkt = pe.decide(snap, pe.PHASE_LOCK, candidate="Matt Ox")
      if pkt["verdict"] == "reject": ...
    """
    pkt = {"phase": phase, "ok": True}
    if not _enabled() or not isinstance(snapshot, ContextSnapshot):
        return pkt
    try:
        q, ev, lc = snapshot.question, snapshot.evidence, snapshot.llm_call
        cand = candidate or snapshot.candidate
        ans = answer or snapshot.answer
        if phase == PHASE_PARADIGM:
            pkt.update(sense_topic_and_paradigm(q, llm_call=lc, session=snapshot.session))
        elif phase == PHASE_RECALL:
            pkt.update(interrogate_as_setter(q, ev, llm_call=lc))
        elif phase == PHASE_PLAN:
            pkt.update(plan_next_step(q, ev, llm_call=lc,
                                      tried_queries=snapshot.tried_queries,
                                      current_candidate=cand or None))
        elif phase == PHASE_LOCK:
            if cand:
                pkt.update(judge_candidate(q, cand, ev, llm_call=lc, claims=snapshot.claims))
        elif phase == PHASE_DELIVER:
            if ans:
                pkt.update(check_delivery_contract(q, ans, llm_call=lc))
        elif phase == PHASE_BUDGET:
            pkt.update(orchestrate_budget(q, llm_call=lc))
        elif phase == PHASE_DIAGNOSE:
            _ss = snapshot.system_state or {}
            pkt.update(diagnose_stuck(q, ev, llm_call=lc,
                                      tried_queries=snapshot.tried_queries,
                                      candidate=cand,
                                      stall_rounds=int(_ss.get("stall_rounds") or 0)))
        elif phase == PHASE_VERIFY:
            if ans:
                pkt.update(verify_answer(q, ans, ev, llm_call=lc, candidate=cand))
        elif phase == PHASE_FLIGHT:
            pkt.update(plan_flight(q, llm_call=lc, session=snapshot.session))
        elif phase == PHASE_REVIEW:
            pkt.update(review_search_path(snapshot))
    except Exception:
        pass
    return pkt


# ════════════════════════════════════════════════════════════════════
# ★★驾驶员延伸动作(2026-08-20 用户愿景第2批)·audit/diagnose/verify/refresh
# ════════════════════════════════════════════════════════════════════


def review_actionable(review, executed_queries=None):
    """通用受控动作门：结构化路径完整、工具受限、动作指纹非重复才允许执行。"""
    try:
        review = review or {}
        action = str(review.get("action") or "").lower()
        tool = str(review.get("action_tool") or "search").lower()
        args = review.get("action_args") or {}
        query = str(review.get("next_query") or next(iter(args.values()), "")).strip()
        if not args and query:
            key = {"search": "question", "structured_db": "query",
                   "fetch": "url", "search_github": "query", "github": "query"}.get(tool, "question")
            args = {key: query}
        path = str(review.get("relation_path") or "").strip()
        fingerprint = "%s:%s" % (tool, json.dumps(args, ensure_ascii=False, sort_keys=True))
        seen = {str(item).strip().lower() for item in (executed_queries or [])}
        return (action in ("expand", "backtrack")
                and tool in ("search", "structured_db", "fetch", "search_github", "github")
                and isinstance(args, dict) and bool(args) and len(query) >= 6
                and "->" in path and fingerprint.lower() not in seen
                and query.lower() not in seen)
    except Exception:
        return False


def review_search_path(snapshot):
    """运行中根据真实证据和实体关系图复盘路径，返回可执行的下一条关系边。

    与开局 paradigm 不同：它必须判断旧假设是否获得约束增量；没有增量就扩展实体族，
    并把下一步写成 subject → relation → object 的精确查询，而不是“换方向”空话。
    """
    res = {"action": "keep", "reason": "", "new_hypotheses": [],
           "next_query": "", "relation_path": "", "candidate": "",
           "action_tool": "search", "action_args": {}}
    if not _enabled() or not isinstance(snapshot, ContextSnapshot):
        return res
    try:
        sess = snapshot.session or {}
        triage = sess.get("triage") or {}
        readings = (sess.get("intent_ledger") or {}).get("readings") or []
        graph = sess.get("_evidence_graph") or {}
        nodes = sorted((graph.get("nodes") or {}).values(),
                       key=lambda rec: -int(rec.get("degree") or 0))[:12]
        edges = sorted(graph.get("edges") or [],
                       key=lambda edge: -float(edge.get("conf") or 0))[:18]
        node_text = ", ".join(str(rec.get("name") or "") for rec in nodes)
        edge_text = "\n".join("- %s --%s--> %s" % (
            edge.get("s"), edge.get("r"), edge.get("o")) for edge in edges)
        progress = sess.get("_progress_state") or {}
        missing = sess.get("_missing_constraints") or []
        failed_paths = sess.get("_pilot_failed_paths") or []
        prompt = (
            "你是解题系统的灵魂驾驶员，正在运行中复盘搜索路径。只依据现有信息，不猜答案。\n"
            "目标不是再做同义改写，而是找【缺失的实体关系边】并小步逼近。\n"
            "若现有假设没有新增约束证据，必须 EXPAND 或 BACKTRACK；EXPAND 的类别必须"
            "由题面与当前证据动态归纳，禁止套用预置领域枚举。\n"
            "下一步必须是一个可执行查询，并表达 entity -> relation -> entity。\n\n"
            "题面: %s\n约束: %s\n当前缺失约束: %s\n进度状态: %s\n"
            "零增量失败路径(不得换词重试同一路径): %s\n当前 readings: %s\n"
            "已试查询: %s\n当前证据摘录:\n%s\n图节点: %s\n图边:\n%s\n\n"
            "只输出：\nACTION: KEEP/EXPAND/BACKTRACK\n"
            "REASON: <基于哪条缺口>\nHYPOTHESES: <由题面与证据动态归纳的新假设，分号隔开>\n"
            "CANDIDATE: <仅当已有关系证据支持且类型符合题问时填实体，否则留空>\n"
            "RELATION_PATH: <subject -> relation -> object>\n"
            "ACTION_TOOL: <search|structured_db|fetch|search_github>\n"
            "ACTION_ARGS: <严格JSON对象；search用question，structured_db用query，fetch用url，"
            "search_github用query。若需要计算/代码，先用search_github寻找数据源或工具；"
            "不要输出可执行shell命令。只给执行该缺失边所需的最小参数>\n"
            "NEXT_QUERY: <兼容字段·与ACTION_ARGS中的核心查询一致>"
            % (snapshot.question[:700], str(triage.get("constraints") or [])[:500],
               str(missing)[:500], str(progress)[:400], str(failed_paths)[-800:],
               str(readings)[:1000], str(snapshot.tried_queries[-10:])[:800],
               "\n".join(str(item)[:600] for item in snapshot.evidence[-12:])[:5000],
               node_text[:500], edge_text[:1500]))
        raw = _call(snapshot.llm_call, prompt, max_tokens=500)
        for line in str(raw or "").splitlines():
            s = line.strip(); u = s.upper()
            if u.startswith("ACTION:"):
                value = s.split(":", 1)[1].strip().lower()
                if value in ("keep", "expand", "backtrack"):
                    res["action"] = value
            elif u.startswith("REASON:"):
                res["reason"] = s.split(":", 1)[1].strip()[:240]
            elif u.startswith("HYPOTHESES:"):
                value = s.split(":", 1)[1].strip()
                res["new_hypotheses"] = [x.strip() for x in re.split(r"[;；]", value)
                                         if x.strip()][:6]
            elif u.startswith("CANDIDATE:"):
                res["candidate"] = s.split(":", 1)[1].strip()[:160]
            elif u.startswith("RELATION_PATH:"):
                res["relation_path"] = s.split(":", 1)[1].strip()[:240]
            elif u.startswith("ACTION_TOOL:"):
                tool = s.split(":", 1)[1].strip().lower()
                if tool in ("search", "structured_db", "fetch", "search_github", "github"):
                    res["action_tool"] = "search_github" if tool == "github" else tool
            elif u.startswith("ACTION_ARGS:"):
                try:
                    import json as _json
                    value = s.split(":", 1)[1].strip()
                    parsed = _json.loads(value)
                    if isinstance(parsed, dict):
                        res["action_args"] = parsed
                except Exception:
                    pass
            elif u.startswith("NEXT_QUERY:"):
                res["next_query"] = s.split(":", 1)[1].strip()[:300]
        if not res["action_args"] and res["next_query"]:
            key = {"search": "question", "structured_db": "query",
                   "fetch": "url", "search_github": "query", "github": "query"}.get(res["action_tool"], "question")
            res["action_args"] = {key: res["next_query"]}
        return res
    except Exception:
        return res

def audit_prompt(base_prompt, question, *, llm_call=None, call_site=""):
    """★动作:审计前置prompt哪里不够好+动态补强(KM文档明确提的DPAI)。
    与inject区别: inject是【加注入】·audit_prompt是【先诊断现有prompt缺陷再针对性补】。
    诊断维度: 有没有说清要什么输出类型/有没有防幻觉约束/有没有带区分器思路/有没有自相矛盾。
    返回 {issues:[缺陷], patched_prompt:补强后的prompt}。★带缓存的题型感知·防幻觉。
    fail-safe → {issues:[], patched_prompt:base_prompt}(原样)。"""
    res = {"issues": [], "patched_prompt": base_prompt}
    if not _enabled() or not base_prompt:
        return res
    try:
        tp = sense_topic_and_paradigm(question, llm_call=llm_call)
        prompt = (
            "你是prompt审计员。看下面这个即将发给LLM的prompt·【只诊断它缺什么】(别改写全文):\n"
            "检查: ①有没有说清要什么输出类型 ②有没有防幻觉约束(别编证据没有的) ③有没有带"
            "'找区分器别猜最有名的'思路 ④有没有自相矛盾/冗余。\n\n"
            "任务题型: %s ｜ 要的答案类型: %s ｜ 调用点: %s\n\n"
            "待审prompt:\n%s\n\n输出(每行一个·没缺陷输出 NONE):\nISSUE: <缺陷>"
            % (tp.get("topic", "?"), tp.get("output_type", ""), call_site or "?",
               str(base_prompt)[:1500]))
        resp = _call(llm_call, prompt, max_tokens=400)
        for line in str(resp or "").splitlines():
            s = line.strip()
            if s.upper().startswith("ISSUE:"):
                v = s.split(":", 1)[1].strip()
                if v and "none" not in v.lower():
                    res["issues"].append(v[:160])
        # 有缺陷→用 inject 针对性补强(复用注入子系统)
        if res["issues"]:
            _patch = "【prompt审计·补强】发现缺陷已补: " + "; ".join(res["issues"][:3])
            res["patched_prompt"] = inject(base_prompt, question, llm_call=llm_call,
                                           what=_patch)
    except Exception:
        pass
    return res


def diagnose_stuck(question, evidence, *, llm_call=None, tried_queries=None,
                   candidate="", stall_rounds=0):
    """★动作:卡住时结构化诊断根因+出路(比 _reflect 更结构化·治反复绕圈痛点)。
    诊断根因分类(带已有信息判): searched_wrong(搜错方向)/locked_wrong(锁错实体)/
    circling(同义改写绕圈)/source_gap(源覆盖不到需换渠道或造工具)/need_compute(缺计算)。
    返回 {root_cause, evidence_based_reason, concrete_exit}。★带已有信息·防幻觉。
    fail-safe → {root_cause:"unknown"}。"""
    res = {"root_cause": "unknown", "evidence_based_reason": "", "concrete_exit": ""}
    if not _enabled():
        return res
    try:
        ev_digest = _evidence_digest(evidence or [])
        tried = "\n已试查询:\n" + "\n".join("- " + str(x)[:70] for x in (tried_queries or [])[:8]) \
            if tried_queries else ""
        prompt = (
            "解题卡住了(连续 %d 轮无新进展)。【只依据已有信息】诊断根因并给具体出路·别脑补。\n\n"
            "题面:\n%s\n\n当前候选: %s\n%s%s\n\n"
            "诊断(选一个最主要根因):\n"
            "  searched_wrong=搜的方向不对(query没扣题面区分器)\n"
            "  locked_wrong=锁错实体(证据支持的是别的)\n"
            "  circling=同义改写绕圈(反复搜相似query零新证据)\n"
            "  source_gap=通用搜索覆盖不到(需换渠道/垂类源/存档)\n"
            "  need_compute=缺的信息要算/解析(该造工具而非搜)\n\n"
            "输出:\nROOT: <上面5个之一>\nREASON: <一句·基于哪条已有信息判的>\n"
            "EXIT: <一个具体可执行的下一步·不是'继续搜'这种废话>"
            % (int(stall_rounds), str(question)[:500], str(candidate)[:60] or "无",
               ("\n已搜证据:\n" + ev_digest) if ev_digest else "\n(还没有有效证据)", tried))
        resp = _call(llm_call, prompt, max_tokens=400)
        for line in str(resp or "").splitlines():
            s = line.strip(); u = s.upper()
            if u.startswith("ROOT:"):
                res["root_cause"] = s.split(":", 1)[1].strip()[:40]
            elif u.startswith("REASON:"):
                res["evidence_based_reason"] = s.split(":", 1)[1].strip()[:200]
            elif u.startswith("EXIT:"):
                res["concrete_exit"] = s.split(":", 1)[1].strip()[:200]
    except Exception:
        pass
    return res


def verify_answer(question, answer, evidence, *, llm_call=None, candidate=""):
    """★动作:交付前终检(三源会师·比单独check_delivery更全)。检查:
      ①答案是否真锁定了具体实体(非泛泛而谈) ②是否回应题问的输出类型 ③证据是否支撑
      (每个关键断言有出处·非脑补)。返回 {pass, reasons, missing}。★缺证据判不通过·防幻觉。
    fail-safe → {pass:True}(不误拦交卷)。"""
    res = {"pass": True, "reasons": [], "missing": ""}
    if not _enabled():
        return res
    a = str(answer or "").strip()
    if not a:
        return res
    try:
        # 优先抽 Exact Answer 行(治漏看尾部)
        _m = re.search(r"(?:exact\s*answer|答案|final\s*answer)\s*[:：]\s*([^\n]{1,120})", a, re.I)
        _ans_line = _m.group(1).strip() if _m else a[:120]
        ev_digest = _evidence_digest(evidence or [])
        prompt = (
            "交付前终检(三源会师)。【只依据已有证据】判这个答案能不能交·别脑补。\n\n"
            "题面:\n%s\n\n答案要点: %s\n\n已搜证据:\n%s\n\n"
            "逐项检查:\n①答案是否【锁定了具体实体/值】(不是'可能是X'泛泛而谈)?\n"
            "②是否【回应题问的输出类型】(题问真名就给真名·问年份就给年份)?\n"
            "③关键断言是否【有证据支撑】(能引到证据·非脑补)?\n\n"
            "输出:\nPASS: <yes=三项都过可交 / no=有项不过>\n"
            "MISSING: <若no·还缺什么才能交>"
            % (str(question)[:500], _ans_line, ev_digest or "(无证据)"))
        resp = _call(llm_call, prompt, max_tokens=300)
        for line in str(resp or "").splitlines():
            s = line.strip(); u = s.upper()
            if u.startswith("PASS:"):
                v = s.split(":", 1)[1].strip().lower()
                if v.startswith("no") or "不过" in v:
                    res["pass"] = False
            elif u.startswith("MISSING:"):
                res["missing"] = s.split(":", 1)[1].strip()[:200]
    except Exception:
        pass
    return res


def refresh_context(snapshot, *, new_evidence=None, new_tool_output=""):
    """★动作(用户核心洞察):处理新上下文·及时更新动态变化。缓存不能一次性拿死——
    题型范式可长缓存(题不变)·但【证据/进展/候选是动态的】·搜索/分析/工具执行后带来大量
    新上下文·关键步骤要刷新snapshot的动态部分。返回更新后的snapshot(同一对象·就地更新)。
    ★关键: 只刷【动态部分(evidence/candidate/system_state)】·题型范式缓存不动(题没变)。
    若新上下文提示题目理解要变(极罕见·如发现题面歧义)·可清题型缓存重感知。fail-safe→原snapshot。"""
    if not isinstance(snapshot, ContextSnapshot):
        return snapshot
    try:
        # 合并新证据(搜索/工具带来的)
        if new_evidence:
            _ne = new_evidence if isinstance(new_evidence, list) else [new_evidence]
            _cur = list(snapshot.evidence) if hasattr(snapshot.evidence, "__iter__") else []
            snapshot.evidence = _cur + _ne
        # 从 session 刷新动态部分(候选/证据可能被别处更新了)
        if isinstance(snapshot.session, dict):
            _sc = snapshot.session.get("_ic_kept_candidate")
            if _sc:
                snapshot.candidate = _sc
            _sf = snapshot.session.get("final")
            if _sf:
                snapshot.answer = _sf
        # 刷新系统状态快照(动态)
        snapshot.system_state = {
            "n_evidence": len(snapshot.evidence) if hasattr(snapshot.evidence, "__len__") else 0,
            "has_candidate": bool(snapshot.candidate),
            "n_tried": len(snapshot.tried_queries),
            "last_tool_output": str(new_tool_output or "")[:200],  # 最新工具产出摘要
        }
    except Exception:
        pass
    return snapshot


# ════════════════════════════════════════════════════════════════════
# ★★★flight plan · 开局全局预判(用户第7轮愿景·更高一层引擎优化)
# ════════════════════════════════════════════════════════════════════
# 用户洞察: 驾驶员不该只在局部判断点被调·应在【开局做一次全局预判】——用户心理预期时间/
#   这题该走哪些步骤/哪些机制必选哪些可跳。且预判本身是变量·驾驶员一路观察一路纠正
#   (简单题轻装·发现难了再加载)。所有机制【动态调整是否启用】·非静态env开关。
# ★关键突破(解"判断简单又是一次判断"): 这次预判【极便宜·一次轻量调用】·产出整个flight
#   plan·后续所有机制照它执行——【一次预判换掉N次分散的机制判断/调用】·净省(法国首都
#   这种·预判'trivial'→跳过pilot/first_anchor/多专家·几秒出答案)。

# 复杂度档 → 该启用的机制集(变量·驾驶员可动态改)。这是"哪些必选哪些可跳"的落地。
_FLIGHT_PROFILES = {
    "trivial":  {"expected_steps": 1, "skip": ["pilot_stance", "first_anchor",
                 "iterative_association", "multi_expert", "multipath", "mea_audit",
                 "goal_approach", "diagnose"], "budget_ratio": 0.15},
    "simple":   {"expected_steps": 3, "skip": ["multi_expert", "multipath",
                 "iterative_association"], "budget_ratio": 0.4},
    "standard": {"expected_steps": 8, "skip": ["multi_expert"], "budget_ratio": 0.7},
    "hard":     {"expected_steps": 20, "skip": [], "budget_ratio": 1.0},
}


def plan_flight(question, *, llm_call=None, session=None):
    """★开局全局预判(flight plan)。一次轻量调用产出:这题复杂度/预期步数/哪些机制启用或跳过/
    预算比例。后续所有机制读它动态决定是否启用——【一次预判换掉N次分散判断】·省时。
    driver一路可纠正(见 revise_flight)。返回 {complexity, expected_steps, enabled(set),
    skip(set), budget_ratio, reason}。fail-safe→standard(不激进跳·保能力)。"""
    _default_profile = "standard"
    if not _enabled():
        # 关时返回全开(旧行为·不跳任何机制)
        return {"complexity": "hard", "expected_steps": 20, "enabled": set(),
                "skip": set(), "budget_ratio": 1.0, "reason": "engine_off_全开"}
    q = str(question or "").strip()
    if not q:
        return _mk_flight(_default_profile, "empty_q")
    # 便宜规则预筛(先不调LLM·极短无约束→trivial;明显多约束→hard·省LLM)
    _wc = len(q.split())
    _n_constraints = q.count("- ") + q.count("；") + q.count(". ") + q.count(", and ")
    # 规则能定的直接定(省LLM): 极短问句=trivial;超多约束=hard
    if _wc <= 12 and _n_constraints == 0 and "?" in q:
        return _mk_flight("trivial", "rule:短问句无约束")
    if _n_constraints >= 5:
        return _mk_flight("hard", "rule:多约束(%d)" % _n_constraints)
    # 中间地带才调LLM判(一次·可缓存)
    try:
        fp = "flight:" + _qfp(q, session)
        if fp in _PILOT_CACHE:
            return _PILOT_CACHE[fp]
        prompt = (
            "你是解题系统的调度员。快速预判这道题的【复杂度】(决定投多少资源·别过度也别不足):\n\n"
            "题面:\n" + q[:600] + "\n\n"
            "trivial=常识直答无需检索(如'法国首都') | simple=1-2次检索即可 | "
            "standard=多步检索推理 | hard=多跳+多约束+易撞错的难题。\n"
            "输出一行:\nCOMPLEXITY: <trivial/simple/standard/hard>\nWHY: <一句为什么>")
        r = _call(llm_call, prompt, max_tokens=80)
        _comp = _default_profile
        _why = "llm"
        for line in str(r or "").splitlines():
            u = line.strip().upper()
            if u.startswith("COMPLEXITY:"):
                _v = line.split(":", 1)[1].strip().lower()
                for c in ("trivial", "simple", "standard", "hard"):
                    if c in _v:
                        _comp = c
                        break
            elif u.startswith("WHY:"):
                _why = line.split(":", 1)[1].strip()[:80]
        _fl = _mk_flight(_comp, _why)
        if r:
            _PILOT_CACHE[fp] = _fl
        return _fl
    except Exception:
        return _mk_flight(_default_profile, "fail-safe")


def _mk_flight(complexity, reason=""):
    """据复杂度档产出 flight plan(机制启用/跳过清单)。"""
    prof = _FLIGHT_PROFILES.get(complexity) or _FLIGHT_PROFILES["standard"]
    return {
        "complexity": complexity,
        "expected_steps": prof["expected_steps"],
        "skip": set(prof["skip"]),          # 该跳过的机制名
        "enabled": set(),                   # 显式启用(空=除skip外都启用)
        "budget_ratio": prof["budget_ratio"],
        "reason": reason,
    }


def flight_allows(flight, mechanism):
    """★机制启用查询: 各机制开头调这个·flight plan 说跳过就跳(动态·非静态env)。
    fail-safe→True(拿不准就启用·保能力)。"""
    try:
        if not isinstance(flight, dict):
            return True
        return mechanism not in (flight.get("skip") or set())
    except Exception:
        return True


def revise_flight(flight, *, observed_harder=False, observed_easier=False, reason=""):
    """★动态纠正(driver一路观察一路改·预判是变量): 实际比预判难→升级(加载更多机制)·
    比预判易→降级(省资源)。返回更新后的flight。fail-safe→原flight。"""
    try:
        if not isinstance(flight, dict):
            return flight
        _order = ["trivial", "simple", "standard", "hard"]
        _cur = flight.get("complexity", "standard")
        _i = _order.index(_cur) if _cur in _order else 2
        if observed_harder and _i < 3:
            _new = _order[_i + 1]
            _fl = _mk_flight(_new, "revised_up:" + reason)
            _fl["revised_from"] = _cur
            return _fl
        if observed_easier and _i > 0:
            _new = _order[_i - 1]
            _fl = _mk_flight(_new, "revised_down:" + reason)
            _fl["revised_from"] = _cur
            return _fl
    except Exception:
        pass
    return flight
