# -*- coding: utf-8 -*-
"""Answer Vault · 反误杀答案保险箱 (2026-08-18 · anti_false_positive 落地)
====================================================================

病根(文档《最后一公里门 交付正确答案》·Q501 实证): agent 已挖到正确答案
(Tobin Sprout - Serial Killer)·但被「建模歧义/超时诚实收尾」等 Gate 误拦·
按"别硬交"指令改写成纯元评论→评分器判"未提供答案"→到手的分被 gate 扔掉。
这不是智力不足·是**治理架构缺陷**: Gate 的 False Positive 把正确答案误杀了。

六层防御落地(论文化简版·零 LLM 成本·全 fail-safe):
  L1 Answer Vault(本模块): 工具输出/AI 断言里出现【明确答案行】→立即锁定进
     session["_answer_vault"]·后续任何 Gate/兜底都能读到它;
  L4 Pre-Gate 存在性校验: finalize 被门拦截时·block 消息强制附"vault 里已有
     答案 X·重交时必须保留答案行"→防改写成元评论(append_vault_guard);
  L5 Conservative Mode: 兜底/必交卷路径把 vault 答案【前置】进 final·评分器
     子串匹配才能命中(vault_prepend/vault_rescue_final);
  L6 输出验证: final 落盘前·vault 有答案而 final 无答案行→强制恢复。

设计原则(文档): "When in doubt, preserve the answer" —— Gate 决策与已发现
答案冲突时·优先保留答案。保守偏置·不引入任何新 LLM 调用。
"""
import re

# ── 答案行检测正则(中英双语·由强到弱) ──────────────────────────────
# 强标记: 行首 "Exact Answer:/Final Answer:/Answer:/答案:/精确答案:... = X"
_VAULT_STRONG = (
    re.compile(r"(?im)^\s{0,4}(?:exact\s*answer|final\s*answer|precise\s*answer)\s*[:：]\s*([^\n]{2,200})"),
    re.compile(r"(?im)^\s{0,4}(?:精确答案|最终答案|正确答案|答案)\s*[:：]\s*([^\n]{2,200})"),
    re.compile(r"(?im)^\s{0,4}answer\s*[:：]\s*([^\n]{2,200})"),
    # "A: xxx" 独立行(题/卡/摘要里常见)——限行首且冒号后非空·避开普通英文句子
    re.compile(r"(?im)^\s{0,4}a\s*[:：]\s*([^\n]{2,200})$"),
)
# 中标记: 句中 "the answer is X" / "答案是X"
_VAULT_MID = (
    re.compile(r"(?i)\bthe\s+(?:correct\s+)?answer\s+is\s*[:：]?\s*([^\n.;]{2,160})"),
    re.compile(r"答案[是为]\s*[:：]?\s*([^\n。；]{2,160})"),
    re.compile(r"(?i)\banswer\s*[:：]\s*([^\n]{2,160})"),
)
# 工具吐出的 ENTITY=X 是待验证候选，不是可直接救援交付的答案。
_CANDIDATE_ENTITY = re.compile(
    r"(?im)^\s*(?:[•\-\*]\s*)?ENTITY\s*=\s*([^\n(]{2,160})")
# 占位/放弃句·出现即不算答案(防把"未找到答案"锁进保险箱)
_PLACEHOLDER = re.compile(
    r"(?i)unknown|not\s*(?:able\s+to|found|sure|determined)|cannot\s+(?:determine|find)"
    r"|无法(?:确定|找到)|不知道|未?(?:找到|能确定|知)|不确定|no\s+(?:definitive|conclusive)"
    r"|need\s+more\s+(?:info|information)|更多信息|超出|无法回答|暂无答案",
)
# v133 (2026-08-22·病灵 vault-噪音污染 修): 撞墙 hint/收尾方法论文本里含大量
# "答案是否..."/"答案是..."句式·会被 _VAULT_MID 的 `答案[是为]:` 正则误捕获。
# 实证 3ab6b1678677 gpt-5.6-sol 那次: vault 早期锁到 LCS (对)·跑到硬墙 hint 时·
# 里面一句"②答案是否直接回应题面问的那个对象"被误当答案·vault 值被污染置换·
# 最终交卷从"LCS"变成一大段方法论文本→ 评分器判 UNKNOWN。
# 判据: 命中以下 hint/元指令特征词的候选一律拒收 (匹配大小写不敏感·中英文):
_NOISE_HINT = re.compile(
    r"(?i)否直接回应|直接回应题面|题面问的那个|题面字面最贴合|"
    r"置信度与存疑点|沉默地猜|存疑点|默查|标注置信度|交卷自检|"
    r"交卷前.*秒|硬墙|硬kill|强制收尾|拼出答案|你已经查到|"
    r"AnswerVault|保险箱|以下为原始收尾|决策间隔|hang 警报|"
    r"finalize_answer|run_factual_extract|run_parallel_search|"
    r"是否直接|如果已有足够|如果还缺关键|如果在循环",
)
# 题面复述(疑问词开头的"答案"不是答案)
_QUESTIONY = re.compile(r"(?i)^(?:what|which|who|where|when|how|why|is|are|does|do|find|identify|name)\b")
# 查询/编程语言保留字和泛型占位词不是实体。通用词法门，不依赖题目或领域。
_RESERVED_CANDIDATE = re.compile(
    r"(?i)^(?:and|or|not|null|none|true|false|select|where|from|join|union|filter|"
    r"optional|bind|values|order|group|limit|offset|ask|construct|describe|"
    r"entity|candidate|item|result|data|value|object|subject|relation|unknown)$")


# 2026-08-19 · 交付净化(治开放题 final 夹带门提示噪音·面向用户自然回复)。
#   引擎内部给模型看的门提示行(方括号元指令/守卫标注/门链噪音)不该出现在交给
#   用户的答案里。此函数剥离它们·只留答案正文。非破坏性: 只作用于【交付/展示】·
#   不改 session/checkpoint 原文(评测/审计仍看原始)。env MEYO_CLEAN_FINAL=0 关。
_DELIVERY_NOISE_LINE = re.compile(
    r"(?m)^\s*(?:>\s*)?\[(?:口径约束核对|🛡️[^\]]*|ALP[^\]]*|反向卫兵|身份覆盖率?[^\]]*|"
    r"单源对账|格式验证|claim级[^\]]*|自我反思[^\]]*|心跳自省[^\]]*|目标逼近[^\]]*|"
    r"校验即搜索|AnswerVault[^\]]*|finalize重复|绕圈反思|硬门控|Reflector[^\]]*|"
    r"专家指认[^\]]*|决策间隔[^\]]*|预算[^\]]*|DEGRADED)[^\]]*\][^\n]*\n?")
_DELIVERY_NOISE_SPAN = re.compile(
    r"(?:否直接回应题面[^\n]*|以下为原始收尾内容[:：]?|说明[:：]本题为[^\n]*|"
    r"该答案断言提取自[^\n]*|已被保险箱锁定[^\n]*|"
    r"待补缺口[^\n]*|还没锁定候选[^\n]*|当前候选「[^\n]*|方向【[^\n]*)\n?")
# 撞墙强制收尾的【模板噪音行】(整行是"答案:否直接回应题面…而不是你算得最顺手的…"这种
#   收尾方法论·非真答案)——整行剥掉(2026-08-20·overload撞墙实测)
_DELIVERY_TEMPLATE_LINE = re.compile(
    r"(?m)^\s*答案[:：]\s*否直接回应题面[^\n]*\n?")
# 目标逼近/缺口地图整段泄漏(以"[目标逼近·步N]"开头的内部诊断·不该当答案)——整块剥
_DELIVERY_NOISE_BLOCK = re.compile(
    r"(?ms)^\s*\[目标逼近·步\d+\].*?(?=\n\n|\Z)")
_EMPTY_ANSWER_LINE = re.compile(r"(?m)^\s*答案[:：]\s*$\n?")


def clean_delivery_final(final_text):
    """把 final 净化成【面向用户的自然回复】: 剥门提示/守卫元指令噪音·去空答案行。
    非破坏性·净化后为空则退回原文(绝不因净化丢答案)。env MEYO_CLEAN_FINAL=0 关。"""
    try:
        import os as _os
        if _os.environ.get("MEYO_CLEAN_FINAL", "1").strip().lower() in ("0", "false", "off", "no"):
            return final_text
        s = str(final_text or "")
        if not s.strip():
            return final_text
        s2 = _DELIVERY_NOISE_BLOCK.sub("", s)    # 先剥"[目标逼近·步N]..."整块内部诊断
        s2 = _DELIVERY_TEMPLATE_LINE.sub("", s2) # 剥撞墙模板噪音行"答案:否直接回应题面…"
        s2 = _DELIVERY_NOISE_LINE.sub("", s2)
        s2 = _DELIVERY_NOISE_SPAN.sub("", s2)
        s2 = _EMPTY_ANSWER_LINE.sub("", s2)      # 剥"答案:"后面空的行(净化残留)
        s2 = re.sub(r"\n{3,}", "\n\n", s2).strip()
        return s2 if s2 else final_text
    except Exception:
        return final_text


def _clean_candidate(raw: str) -> str:
    """清洗候选答案: 去首尾空白/引号/Markdown 强调·超长截断。无效返 ''。"""
    s = (raw or "").strip().strip("`*#>~-— ").strip()
    s = s.strip('"“”').strip()
    if len(s) < 2:
        return ""
    if len(s) > 200:
        s = s[:200].rsplit(" ", 1)[0] if " " in s[:200] else s[:200]
    if _PLACEHOLDER.search(s):
        return ""
    # v166 · 模板变量占位符(题面格式指示 'Exact Answer: {your succinct...}')
    # 不是答案——agent 复读题面模板时防锁箱(v166 Q1 实证)。
    if re.search(r"\{\s*(?:your|the|answer|succinct|final|insert)[^}]{0,80}\}", s, re.I):
        return ""
    # v133 (2026-08-22·病灵 vault-噪音污染 修): hint/收尾方法论文本命中即拒收
    if _NOISE_HINT.search(s):
        return ""
    if _QUESTIONY.match(s) or _RESERVED_CANDIDATE.match(s):
        return ""
    # 单个全大写 token 多为查询运算符/字段名/缩写，不足以作为答案候选；
    # 至少要有小写、数字或多个词。显式强答案行仍可通过下游完整文本保留。
    if re.fullmatch(r"[A-Z_]{2,20}", s):
        return ""
    # 纯标点/纯数字标点混合过短
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", s):
        return ""
    return s


def detect_answer(text: str):
    """从文本里提取【明确答案断言】。返回 (answer, strength) 或 (None, 0)。

    strength: 2=强标记(行首答案标签) · 1=中标记(句中 answer is)。锁箱只认
    strength>=1·覆盖优先取 2·同强度取最后一次(越晚越接近最终结论)。
    """
    try:
        t = str(text or "")
        if len(t) < 4:
            return None, 0
        best, best_s = None, 0
        for pat in _VAULT_STRONG:
            hit = None
            for m in pat.finditer(t):
                c = _clean_candidate(m.group(1))
                if c:
                    hit = c   # 同一模式取最后一次出现
            if hit:
                best, best_s = hit, 2
        if not best:
            for pat in _VAULT_MID:
                hit = None
                for m in pat.finditer(t):
                    c = _clean_candidate(m.group(1))
                    if c:
                        hit = c
                if hit:
                    best, best_s = hit, 1
        return best, best_s
    except Exception:
        return None, 0


# ── 结构化答案抽取(2026-08-25 · 借鉴 claude-code-oss code-review 范式)────────
# 病根同源(与本轮 prime/penguin adapter 踩的坑一致): 从含工具日志/自检 hint/
# 收尾方法论的【混合文本流】里用正则捞答案·必然被噪音污染(v133 vault-噪音污染)。
# cc-oss 的做法: 最终答案【只从结构化字段取】(assistant message 的 text part /
# 显式 answer 键)·不从混合流正则猜。这里落地为: 交卷 payload 若带结构化答案字段·
# 直接信任它·完全绕过 _VAULT_MID/_NOISE_HINT 那套正则补丁。
#
# 通用性(遵 memory 避免任务专用规则): 只认通用结构化键名·不含任何 benchmark/
# domain 线索;命中即用·未命中 fail-safe 退回 detect_answer(文本正则)·零回归。
#
# ⚠️ 关键(2026-08-25 真 ckpt 实证): 绝不收 `final`/`narrative`/`text` 这类
#   【最终文本流】字段——它们常含 [DEGRADED]/自检方法论/论证噪音(实测 s["final"]
#   = "[DEGRADED] 答案:否直接回应题面…")·盲信会把噪音锁成高信任答案·放大污染。
#   只认【专门的结构化答案字段】(driver 圈选/converge 聚合独立产出的纯答案)。
_STRUCT_ANSWER_KEYS = (
    "final_answer", "selected_entity", "chosen_answer",
    "predicted_answer", "prediction", "converged_answer",
)


def extract_structured_answer(payload):
    """从【结构化交卷 payload】优先取答案·绕过文本正则噪音。

    payload 可为 dict / 带属性的对象 / 结构化候选列表。返回 (answer, source) ·
    answer 为 '' 表示无结构化答案(调用方应退回 detect_answer)。source ∈
    {'struct_key','struct_candidate',''} · 供台账区分抽取来源。

    fail-safe: 任何异常返回 ('', '') · 绝不抛。
    """
    try:
        if payload is None:
            return "", ""

        def _get(obj, key):
            if isinstance(obj, dict):
                return obj.get(key)
            return getattr(obj, key, None)

        # 1) 显式结构化答案键(最高信任·仅纯字符串·不收文本流)
        for k in _STRUCT_ANSWER_KEYS:
            v = _get(payload, k)
            if isinstance(v, str):
                c = _clean_candidate(v)
                if c:
                    return c, "struct_key"

        # 1b) 聚合结果对象(如 converge_answer_from_paths 产出
        #     {"answer":...,"confidence":...})。这类是【结构化聚合】·其 answer 子键
        #     是纯答案(非文本流)·安全。仅认 dict 形态·且必须带 answer 子键。
        for ak in ("answer", "converge_result", "aggregate", "consensus"):
            v = _get(payload, ak)
            if isinstance(v, dict):
                inner = v.get("answer")
                if isinstance(inner, str):
                    c = _clean_candidate(inner)
                    if c:
                        return c, "struct_key"

        # 2) 结构化候选列表(如 path_results 聚合的 ranked · 取最高 confidence)
        for lk in ("ranked", "candidates", "top2", "path_results"):
            lst = _get(payload, lk)
            if isinstance(lst, list) and lst:
                best, best_conf = "", -1.0
                for item in lst:
                    a = _get(item, "answer") if not isinstance(item, str) else item
                    conf = _get(item, "confidence") if isinstance(item, dict) else None
                    if isinstance(a, str):
                        c = _clean_candidate(a)
                        if c and (float(conf or 0.0) >= best_conf):
                            best, best_conf = c, float(conf or 0.0)
                if best:
                    return best, "struct_candidate"

        return "", ""
    except Exception:
        return "", ""


def observe(session, tool_name: str, summary) -> str:
    """工具结果落账处的 L1 钩子: 从 summary 检测答案断言→锁进 vault。

    静默(不给 agent 注入任何提示·零打扰);返回 ''(接口对称·便于未来扩展)。
    fail-safe: 任何异常吞掉·绝不影响工具主流程。
    """
    try:
        if not isinstance(summary, str) or len(summary) < 8:
            return ""
        # ★统一接线(治"建了没接牢"): 工具结果落账的统一钩子里·顺带把实体采集进
        #   Entity Memory Bank(结构化证据保持·KM2781609136)。一处接·所有调 observe
        #   的工具落账点(native 6处+auto 4处)全自动采集·不再零散漏接。fail-safe。
        try:
            import entity_memory_bank as _emb
            _emb.observe_entities(session, summary, source=str(tool_name or ""))
        except Exception:
            pass
        # ★升级(用户/goal·Graph-Based Memory): 落账时【结构化提取三元组】入证据图·
        #   不事后正则(治A类gold-in-thoughts丢失)。每搜一步就结构化存图·收敛时图遍历。
        #   与 Entity Bank 同一钩子·两侧(native+auto)全覆盖。fail-safe·env 可关。
        try:
            import evidence_graph as _eg
            _step = len((session or {}).get("tool_log") or []) if isinstance(session, dict) else 0
            # session._llm_call: 引擎(native/auto)启动时存的 LLM 闭包·供旁路机制复用做
            #   语义抽取(真搜噪声文本正则不可靠·LLM 抽准)。无则退回严格启发式。
            _lc = (session or {}).get("_llm_call") if isinstance(session, dict) else None
            _eg.observe_triplets(session, summary, source=str(tool_name or ""),
                                 step=_step, llm_call=_lc)
        except Exception:
            pass
        # 拦截/报错类消息不含真答案(防把门消息里的示例当答案)
        head = summary[:60]
        if (head.startswith("[") and ("拦截" in head or "阻断" in head or "熔断" in head
                                      or "blocked" in head.lower())):
            return ""
        # 候选层：ENTITY=X 只进入候选保险箱，供 progressive/关系验证使用；
        # 它没有“已验证答案”语义，绝不能在最终 UNKNOWN 时被前置交付。
        for _match in _CANDIDATE_ENTITY.finditer(summary):
            _candidate = _clean_candidate(_match.group(1))
            if not _candidate:
                continue
            _items = session.setdefault("_candidate_vault", [])
            _known = {str(item.get("answer") or "").lower() for item in _items
                      if isinstance(item, dict)}
            if _candidate.lower() not in _known:
                _items.append({"answer": _candidate, "strength": 1,
                               "tool": str(tool_name or "")[:40],
                               "ts": __import__("time").time(), "verified": False})
                del _items[:-20]
        ans, strength = detect_answer(summary)
        if not ans:
            return ""
        # 明确答案也需要通过“是否已有关系进度/是否来自最终交付工具”的通用门。
        # run_factual_extract 这类抽取器会产出候选断言，但若当前关系覆盖仍为 0，
        # 它还不是可救援交付的答案，只能先进入候选层，避免把未验证候选前置到 UNKNOWN。
        progress = (session or {}).get("_progress_state") or {}
        relation_ok = float(progress.get("relation_coverage") or 0.0) > 0
        _tool_s = str(tool_name or "")
        final_source = _tool_s in ("finalize", "finalize_answer", "check_answer_contract")
        sourced_extract = (_tool_s == "run_factual_extract"
                           and re.search(r"来源|source|独立|多源|evidence", str(summary), re.I))
        if (strength < 2 and not relation_ok) or (not relation_ok and not final_source and not sourced_extract):
            _items = session.setdefault("_candidate_vault", [])
            _known = {str(item.get("answer") or "").lower() for item in _items
                      if isinstance(item, dict)}
            if ans.lower() not in _known:
                _items.append({"answer": ans, "strength": strength,
                               "tool": str(tool_name or "")[:40],
                               "ts": __import__("time").time(),
                               "verified": False, "promote_blocked": "no_relation_coverage"})
                del _items[:-20]
            return ""
        v = session.get("_answer_vault") or {}
        old_s = int(v.get("strength") or 0)
        # 强度更高的覆盖;同强度取新(后出断言更接近结论);弱不覆盖强
        if strength >= old_s:
            session["_answer_vault"] = {
                "answer": ans, "strength": strength,
                "tool": str(tool_name or "")[:40],
                "ts": __import__("time").time(),
            }
        return ""
    except Exception:
        return ""


def get(session):
    """读 vault。返回 dict(answer/strength/tool/ts) 或 None。fail-safe。"""
    try:
        v = (session or {}).get("_answer_vault") or {}
        a = str(v.get("answer") or "").strip()
        if a and len(a) >= 2:
            return v
        return None
    except Exception:
        return None


def answer_text(session) -> str:
    """便捷: 取 vault 答案文本(无则 '')。"""
    v = get(session)
    return str(v.get("answer") or "") if v else ""


def append_vault_guard(session, block_msg: str) -> str:
    """L4 Pre-Gate 校验: finalize 被门拦截时·若 vault 已有答案→在拦截消息后
    强制附加"保留答案行"守卫·防 agent 按门的"别硬交"指令把答案改写成元评论。

    这就是 Conservative Mode 的提示侧: 门可以要求补质量·但不许丢答案。
    fail-safe: 任何异常原样返回 block_msg。
    """
    try:
        v = get(session)
        if not v or not isinstance(block_msg, str):
            return block_msg
        a = str(v.get("answer") or "")[:160]
        src = str(v.get("tool") or "?")
        return block_msg + (
            f"\n\n[🛡️AnswerVault·反误杀守卫] 你此前({src} 步)已挖到明确答案断言:"
            f"『{a}』——它已被保险箱锁定。按门的要求补齐质量项时【必须保留明确"
            f"答案行】(如 `A: {a}` 或 `答案: {a}` 放在答案开头)·禁止把答案改写成"
            f"纯元评论/纯方法论讨论——评分器需要看到具体答案值·丢掉它=0分。"
        )
    except Exception:
        return block_msg


def note_locked_entity(session, entity, *, source: str = "", strength: int = 1) -> str:
    """v122(2026-08-20 · 第3层 bc 重跑丢失已锁实体的根因修) · 显式锁定一个
    【高置信中间结论实体】进 vault(不依赖它出现在"答案:"行里)。

    病根(bc_1786949292 实证): 引擎多源确认了作者=Ken Walibora·但这个中间
    结论从未以"答案: X"的形式落进 vault(vault 只认答案行)·于是第一次
    finalize 被打断后·第二次 predict_then_search/finalize 从原始泛化题面重新
    规划·丢了 Walibora·漂回"South African/1970-1974"。

    修法(a·放宽写入): 多源提及/高置信的候选实体(哪怕只是 probable·不是
    终答)也软锁进 vault——这样重跑/二次收尾能读到它当作已锁定的中间结论。
    只在【更高或同强度】时覆盖(与 observe 同口径·弱不覆盖强)。fail-safe。
    返回锁定的实体文本(未锁定返 '')。"""
    try:
        e = _clean_candidate(str(entity or ""))
        if not e:
            return ""
        strength = int(strength) if strength else 1
        strength = max(1, min(2, strength))
        v = session.get("_answer_vault") or {}
        old_s = int(v.get("strength") or 0)
        if strength >= old_s:
            session["_answer_vault"] = {
                "answer": e, "strength": strength,
                "tool": (str(source or "locked_entity"))[:40],
                "ts": __import__("time").time(),
            }
        return e
    except Exception:
        return ""


def relock_carryover(session) -> str:
    """v122(2026-08-20 · 第3层) · 重跑/二次 finalize/二次 predict_then_search 前的
    【已锁定实体上下文注入】(修法 b)。

    把 vault 里已锁定的候选实体作为显式上下文·让主脑重跑时【持有中间结论】·
    而不是从原始泛化题面从零重来。返回一段可拼进 advice/prompt 的自然语言;
    vault 为空则返回 ''(不注入噪音)。fail-safe。"""
    try:
        v = get(session)
        if not v:
            return ""
        a = str(v.get("answer") or "")[:160]
        src = str(v.get("tool") or "?")
        if not a:
            return ""
        return (
            f"\n\n[🔒已锁定候选实体·重跑必带] 你此前({src} 步)已经把候选实体锁定为"
            f"『{a}』(多源/高置信确认的中间结论·非泛化猜测)。**除非有新证据明确"
            f"推翻它·请继续基于『{a}』深挖、补证、收窄**——不要丢掉它从原始题面"
            f"重新泛化猜测(那正是把已找到的对答案漂走的典型失败)。若要换实体·必须"
            f"先说明是哪条新证据推翻了『{a}』。"
        )
    except Exception:
        return ""


def lock_structured(session, payload) -> str:
    """交卷路径显式调用: 从【结构化 payload】抽答案·以最高信任(source=struct_*·
    strength=3)锁进 vault。这是 cc-oss #2 结构化抽取的接入点——交卷前若有 driver
    圈选结果/converge 聚合结果等结构化对象·优先用它·绕开混合文本正则噪音。

    返回锁定的答案(''表示 payload 无结构化答案·调用方无需处理)。fail-safe。
    """
    try:
        if not isinstance(session, dict):
            return ""
        ans, src = extract_structured_answer(payload)
        if not ans:
            return ""
        v = session.get("_answer_vault") or {}
        # 结构化 strength=3 高于文本强标记(2);同为结构化时取新。
        if int(v.get("strength") or 0) <= 3:
            session["_answer_vault"] = {
                "answer": ans, "strength": 3, "source": src,
                "tool": "lock_structured",
                "ts": __import__("time").time(),
            }
        return ans
    except Exception:
        return ""


def rescue_final(final_text: str, session) -> str:
    """L5+L6 输出验证/恢复: final 文本若【无答案行】而 vault 有→把 vault 答案
    前置成明确答案行·原文本保留为论证。已有答案行则原样返回(不重复前置)。

    治两类实证: ① 兜底路径以 meta 文本打头·评分器子串匹配不到; ② gate 逼
    改写后答案被埋进段落深处。fail-safe: 异常原样返回。
    """
    try:
        # v2026-08-25 (cc-oss #2 结构化抽取接入): 先看 vault 是否记录了【结构化来源】
        # 的答案(struct_key/struct_candidate)——这类答案直取自交卷 payload 的结构化
        # 字段·未经混合文本正则·最可信。有则优先·绕开可能被噪音污染的文本锁答案。
        a = ""
        try:
            v = get(session) or {}
            if v.get("source") in ("struct_key", "struct_candidate"):
                a = str(v.get("answer") or "").strip()
        except Exception:
            a = ""
        if not a:
            a = answer_text(session)
        if not a or not isinstance(final_text, str):
            return final_text
        body = final_text or ""
        # 已含该答案(宽松: 答案前40字符已出现在文本里)→不重复前置
        if a[:40] in body:
            return body
        # 已有别的明确答案行→尊重现有(可能是 agent 更新过的结论)·不强插
        if re.search(r"(?im)^\s{0,4}(?:exact\s*answer|final\s*answer|answer|答案|精确答案|最终答案|a)\s*[:：]\s*\S", body):
            return body
        return (f"答案: {a}\n\n"
                f"> [🛡️AnswerVault 前置] 该答案断言提取自你此前的工具输出"
                f"(已被保险箱锁定·防被收尾改写丢失)。以下为原始收尾内容:\n\n"
                + body)
    except Exception:
        return final_text


def format_guard(session, final_markdown: str, wants_number: bool = False) -> str:
    """L4b(Q3·Trolltunga 实证) · 提交前格式验证层: 答案对但形式错(引号包裹/
    题面要数值而答案行无数字)会被评分器判错——内容对·形式错=沉默失败。

    规则(零 LLM·只拦明确失配·每题最多拦一次):
      ① wants_number(题面要数值答案·由调用方用 _question_wants_number 判)而
         答案头部(前220字符·答案行通常在此)一个数字都没有 → 拦·要求以
         `答案: <数值>` 开头重排(论证保留在后)。
      ② 答案行被引号包裹(``答案: "X"``)→ 软提示(不拦·随消息附)。
    vault 有更合规的答案时在消息里直接给建议行。fail-safe: 异常返回 ''。
    """
    try:
        fm = str(final_markdown or "")
        if not fm.strip() or not isinstance(session, dict):
            return ""
        b = session.setdefault("_budget", {})
        head = fm[:220]
        msgs = []
        if wants_number:
            import re as _re_fg
            ans_line = ""
            m = _re_fg.search(r"(?im)^\s{0,4}(?:exact\s*answer|final\s*answer|answer|答案|"
                              r"精确答案|最终答案|a)\s*[:：]\s*([^\n]{1,200})", fm)
            if m:
                ans_line = m.group(1)
            probe = ans_line if ans_line else head
            if not _re_fg.search(r"\d", probe):
                if not b.get("_fmt_num_blocked_once"):
                    b["_fmt_num_blocked_once"] = True
                    _v = answer_text(session)
                    _suggest = ""
                    if _v and _re_fg.search(r"\d", _v):
                        _suggest = (f"\n(你的工具输出里曾断言过含数字的答案『{_v[:80]}』——"
                                    f"若它才是题面所问的数值·直接以它重排。)")
                    return (f"[格式验证·Q3] 题面要求的是【数值型答案】·但你答案行里没有"
                            f"任何数字——评分器按数值匹配会判错(内容对·形式错=沉默丢分)。"
                            f"请重排交付: 第一行写 `答案: <具体数值>`(可含单位/约数说明·"
                            f"但必须以数值开头)·完整论证放后面。{_suggest}")
        # 软提示: 答案行引号包裹
        import re as _re_fg2
        m2 = _re_fg2.search(r"(?im)^\s{0,4}(?:answer|答案|final answer)\s*[:：]\s*[\"'“”‘’]([^\n]{1,150})[\"'“”‘’]", fm)
        if m2 and not b.get("_fmt_quote_hinted"):
            b["_fmt_quote_hinted"] = True
            msgs.append(f"\n\n[格式提示] 答案值被引号包裹(`{m2.group(1)[:60]}`)——评分器做"
                        f"子串匹配时引号可能造成失配·建议去掉包裹引号(如 `答案: {m2.group(1)[:60]}`)。")
        return "".join(msgs)
    except Exception:
        return ""


if __name__ == "__main__":  # 冒烟自测
    # 1) 强标记检测
    a, s = detect_answer(" blah\nExact Answer: Tobin Sprout - Serial Killer\n ok")
    assert a and "Tobin Sprout" in a and s == 2, (a, s)
    # 2) 中文
    a, s = detect_answer("分析了很多……\n答案: 34689")
    assert a == "34689" and s == 2, (a, s)
    # 3) 句中弱标记
    a, s = detect_answer("After checking, the answer is Paris, France.")
    assert a and "Paris" in a and s == 1, (a, s)
    # 4) 占位句不锁
    a, s = detect_answer("Exact Answer: unknown — cannot determine")
    assert a is None, a
    # 5) 题面复述不锁
    a, s = detect_answer("Answer: what is the capital?")
    assert a is None, a
    # 6) vault 生命周期
    sess = {}
    observe(sess, "run_factual_extract", "…\n精确答案: Rio de Janeiro\n…")
    assert answer_text(sess) == "Rio de Janeiro"
    # 弱不覆盖强
    observe(sess, "search", "the answer is maybe Lisbon")
    assert answer_text(sess) == "Rio de Janeiro"
    # 强覆盖强(新结论)
    observe(sess, "finalize", "答案: Lisbon")
    assert answer_text(sess) == "Lisbon"
    # 7) gate 守卫
    g = append_vault_guard(sess, "[门拦截] 需补建模歧义说明")
    assert "AnswerVault" in g and "Lisbon" in g
    # 8) rescue: 无答案行的 final 被前置
    r = rescue_final("综合来看该城市位于葡萄牙·人口约50万。", sess)
    assert r.startswith("答案: Lisbon"), r[:40]
    # 9) rescue: 已含答案不重复
    r2 = rescue_final("答案: Lisbon 是首都。", sess)
    assert r2 == "答案: Lisbon 是首都。"
    print("ANSWER VAULT SMOKE PASS")
