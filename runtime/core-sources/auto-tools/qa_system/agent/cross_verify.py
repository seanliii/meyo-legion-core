"""
v13.92 · cross_verify · finalize 前多模型交叉验证 (OP-3)

动机(2026-08-10 调研结论):
  阿里云 Ops-Agentic-Search / PrimeIntellect Prime Agent 都把「多模型交叉
  验证」当标配。我们所有答案单模型一条路走到黑,撞墙不会"换个模型/换个
  角度再验一遍"。GAIA P3 TODO。

设计(确定性优先 · fail-safe):
  1. 触发判据(无需逐题 if):
     · finalize 时如果是【高风险】(数值/多值/计算/单位/ID/日期) → 触发
     · 单一确定性判据: 答案里出现数字/逗号分隔列表/单位符号/日期格式/英文 ID
     · 出现 UNKNOWN/"不知道"/"无答案" → 不触发(没东西可验)
  2. 第二模型独立复算:
     · 主答案 + 原始 question + agent 简明 evidence 摘要
     · 让第二模型(默认 sonnet,与主答案 opus 异源)独立给一个 verdict
     · 输出 {"agree": bool, "verdict_answer": str, "why": str}
  3. 不一致 → 调第三模型(longcat-2.0,快)仲裁;仍不一致 → 用主答案,但记入
     alerts(供后续复盘)。
  4. 一切 fail-safe: 网关挂/超时/格式错 → 返回 None,调用方放行主答案。
     绝不因 cross_verify 卡死交付。

用法(主入口):
  from cross_verify import maybe_cross_verify
  decision = maybe_cross_verify(question, main_answer, evidence=session.get("evidence"))
  # decision = {"triggered": bool, "final_answer": str, "agreed": bool|None,
  #             "verdict": {...}, "reason": str}

环境开关:
  MEYO_CROSS_VERIFY=off   → 整模块禁用(默认 on)
  MEYO_CROSS_VERIFY_MODE=second_only|second_then_third (默认 second_then_third)
"""
from __future__ import annotations  # v13.93.2 · 兼容 cron 环境 /usr/bin/python3 (3.9) · 见下方 dict|None 等新式注解
import os, json, re, time

try:
    from qa_llm_gateway import call as _gw_call, text as _gw_text
except Exception:  # 沙箱/独立测试时拿不到 gateway 时不挂
    _gw_call = None
    _gw_text = None


# ═══════════════════════════════════════════════════════════════
# 高风险判据(通用,非 if 单题)
# ═══════════════════════════════════════════════════════════════

# 数字 + 单位(常见物理/化学/统计单位)→ 触发
_UNIT_RE = re.compile(
    r"(?:^|\s|[^\w])"                  # 边界
    r"-?\d+(?:[\.,]\d+)?"              # 数字主体
    r"\s*(?:kg|g|mg|ug|ng|lb|oz|m|km|cm|mm|um|nm|"
    r"h|min|s|ms|us|ns|"
    r"m?s/\w*|hz|khz|mhz|ghz|"
    r"m\^?[23]|cm\^?[23]|"
    r"°[cfk]|deg|"
    r"pa|kpa|mpa|atm|bar|psi|"
    r"mol|mmol|umol|nmol|ppm|%|"
    r"\$|￥|€|£|¥|元|块|刀|"
    r"年|月|日|时|分|秒|"
    r"倍|次|个|条|篇|道|本|张|枚|"
    r"km/h|mph|rpm|fps)"
    r"(?:\s|$|[^\w])",
    re.IGNORECASE,
)
# 多值(逗号/顿号/分号分隔 2+ 个短词)→ 触发
_MULTI_VAL_RE = re.compile(r"[,，、;；]\s*[^\s,，、;；]{1,20}")
# 日期格式 → 触发
_DATE_RE = re.compile(r"\b(?:\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}/\d{1,2}/\d{2,4})\b")
# 英文 ID 编码(允许 . 分隔如 EC 1.1.1.1,case-insensitive)
_ID_RE = re.compile(r"\b[A-Z]{2,}[-\s]?\d[A-Z0-9.\s-]*\b", re.IGNORECASE)
# v13.92.1 · 裸数字答案(GAIA 最常见答案形态: "41" "4192" "1.456" "-3.2" 无单位)
# 用真实 GAIA 100miss 复查发现: 48 道数字类错题里 46 道是纯数字裸答案(无单位),
# 之前只判"数字+单位"导致 _is_high_risk 命中率仅 4%(2/48),现补齐这条。
_BARE_NUMBER_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")
# v13.96 · 前缀货币符号 + 数字($89706.00 / ￥100 / €50)
# 事故: $89706.00 未触发 cross_verify·因为 _UNIT_RE 只匹配后缀单位(数字$)·
#   _BARE_NUMBER_RE 只匹配纯数字(^89706$)·美元前缀都没覆盖到。
_PREFIX_CUR_RE = re.compile(r"(?:\$|￥|€|£|¥)\s*\d+(?:[.,]\d+)?")


def _is_high_risk(answer: str) -> bool:
    """通用判据: 数字+单位 / 裸数字 / 多值 / 日期 / 英文 ID 任一命中即视为高风险。
    容忍阈值: 答案里有上述任一模式且总长 < 800 字符(短答案,精确匹配关键)。
    长答案(>800)按"已展开论证"处理,不二次验证(成本不划算,模型已充分表达)。"""
    if not answer or not answer.strip():
        return False
    a = answer.strip()
    if len(a) > 800:
        return False
    low = a.lower()
    if any(bad in low for bad in ("unknown", "不知道", "答不出", "无答案", "unable to answer")):
        return False
    return bool(
        _UNIT_RE.search(a)
        or _BARE_NUMBER_RE.match(a)
        or _PREFIX_CUR_RE.search(a)
        or _MULTI_VAL_RE.search(a)
        or _DATE_RE.search(a)
        or _ID_RE.search(a)
    )


# ═══════════════════════════════════════════════════════════════
# 第二模型 prompt(故意只给 question + 答案,不给 evidence 摘要,
# 让它从 0 独立判 — 与主模型的"看过 evidence"形成正交)
# ═══════════════════════════════════════════════════════════════

_VERIFY_PROMPT = """你是一个独立的审查员。请基于【问题本身】判断下面给出的【答案】是否合理。
不要去看任何外部资料,不要被任何上下文带偏,只看【问题问的 + 答案答的】在事实/逻辑层面是否自洽。

【问题】
{question}

【待审查的答案】
{answer}

【判决要点】
- 数字/单位/日期 是否自洽?有没有明显的量级错(1000x)、单位混(kg vs g)、日期格式不一致?
- 多值答案 中每个元素是否真的能各自独立成立?有没有自相矛盾?
- 是否回避了问题(答非所问)?
- 是否含 UNKNOWN/不知道/答不出 之类的弃权?如有,直接标 agree=false(没东西可验)。
- 计算题【数据闭合判据】:若题面数据恰好凑齐某教科书标准公式的全部输入
  (如理想气体方程只需 n,P,T——质量给 n·地点给 P,T·一个不多一个不少)且只要一个数,
  则出题人就是按该标准公式出题——按标准公式算出的答案就是合理的,
  【不能】以"现实中应用真实物性/高阶修正才对"为由判不合理。
  (实证:Freon-12海沟题·理想气体算55=标答·审查员用真实密度判193'更合理'→错杀。)

【输出格式(严格 JSON,只能输出这一行)】
{{"agree": true/false, "verdict_answer": "你独立给出的最佳答案(如有)", "why": "20字内一句话原因"}}
"""


def _call_verifier_heterogeneous(question, answer, evidence_text="", timeout=90):
    """v13.93(2026-08-10 用户: 用异构模型接替干活+数字校验) · 用【不同家的模型】
    (gpt-5/gemini·非claude)基于【agent查到的资料】独立复算·再数值容差比对。
    这比同家换tier(opus→sonnet)强——异构模型错法不同·交叉验证才有价值。
    返回 {agree, verdict_answer, why} 或 None(不可用退回原verifier)。"""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        import model_orchestrator as _mo
        # 题型: 有数字→calc, 否则general
        kind = "calc" if re.search(r"\d", answer or "") else "general"
        r = _mo.cross_verify(question, answer, kind=kind, evidence=evidence_text)
        if r.get("agree") is None:
            return None
        # 数值容差比对: 从主答案和第二答案各抽第一个数·相对误差<1%算一致(治1.8 vs 1.80)
        agree = r["agree"]
        if not agree:
            n1 = _first_num(answer); n2 = _first_num(r.get("second_answer", ""))
            if n1 is not None and n2 is not None and n1 != 0:
                if abs(n1 - n2) / abs(n1) < 0.01:
                    agree = True
        return {"agree": agree,
                "verdict_answer": r.get("second_answer", "")[:200],
                "why": f"异构模型{r.get('second_model','')}: {r.get('verdict','')[:60]}"}
    except Exception:
        return None


def _first_num(s):
    """抽字符串里第一个数字(容差比对用)。"""
    try:
        m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
        return float(m.group(0)) if m else None
    except Exception:
        return None


def _call_verifier(question: str, answer: str, model_tier: str = "mid",
                   timeout: float = 45) -> dict | None:
    """调第二模型独立判。返回 dict or None(任何失败都返回 None → 不阻塞主流程)。"""
    if _gw_call is None:
        return None
    prompt = _VERIFY_PROMPT.format(
        question=(question or "").strip()[:1500],
        answer=(answer or "").strip()[:1500],
    )
    try:
        r = _gw_call(prompt, timeout=timeout, tier=model_tier, max_tokens=400)
    except Exception:
        return None
    if not r or not r.get("ok"):
        return None
    out = (r.get("out") or "").strip()
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:
        return None
    # 防御性归一化: 三字段兜底
    return {
        "agree": bool(d.get("agree")),
        "verdict_answer": str(d.get("verdict_answer") or "").strip(),
        "why": str(d.get("why") or "").strip()[:80],
    }


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════

def maybe_cross_verify(question: str, main_answer: str,
                       *, evidence: list | None = None,
                       force: bool = False,
                       main_confirmed: bool | None = None) -> dict:
    """finalize 前的多模型交叉验证。

    Returns
    -------
    dict {
        "triggered":   bool,        # 本题是否真触发了验证
        "final_answer": str,         # 最终采纳的答案(默认原答案;不一致时第三模型仲裁)
        "agreed":      bool|None,    # 第二模型是否 agree
        "verdict":     dict|None,    # 第二模型的原始判决
        "reason":      str,          # 一句话原因(给调用方打 log / 记 alert)
        "needs_more_verification": bool,  # True=分歧未解决·主答案未 confirmed·应回锦标赛/继续搜
        "elapsed_s":   float,
    }

    main_confirmed : bool | None
        主答案是否【已充分验证 confirmed】(由调用方从 identity_coverage 四态传入)。
        2026-08-19 治【仲裁保守 bug】(Solanum hermannii 题): 之前"无证据型 disagree
        一律保留主答案"反噬——第二模型正确质疑一个【还没 confirmed 的主答案】·却被
        当无效 disagree 保留了错答案。修法:
          · main_confirmed=True  → 保留 Russell Lyons 内核(缺证据的第二模型别乱翻
            【已充分验证】的主答案);
          · main_confirmed=False → 主答案自己都没到 confirmed·disagree 必须响应——
            不保守保留·而是回锦标赛/继续搜(needs_more_verification=True);
          · main_confirmed=None(调用方不知道) → 保守退回旧行为(保留主答案·fail-safe)。
    """
    t0 = time.time()
    # 环境开关
    if os.environ.get("MEYO_CROSS_VERIFY", "on").lower() in ("off", "0", "false", "no"):
        return {
            "triggered": False, "final_answer": main_answer, "agreed": None,
            "verdict": None, "reason": "cross_verify disabled by env",
            "elapsed_s": 0.0,
        }
    # 触发判据
    if not force and not _is_high_risk(main_answer):
        return {
            "triggered": False, "final_answer": main_answer, "agreed": None,
            "verdict": None, "reason": "low_risk (无数字/多值/日期/ID)",
            "elapsed_s": 0.0,
        }

    # ── 校验即行为 · 证据驱动分层验证(两份 KM 文档内核·2026-08-19)─────────────
    #   《校验即搜索》: Critic 不是评委·是检察官——先【主动搜反例·基于证据】判·而非
    #   直接找第二模型凭记忆裸判。《证据驱动分层验证引擎》Stage4/5: 按证据覆盖/矛盾
    #   四态裁决(AGREE/CONTRADICTS/INSUFFICIENT/WEAK)→三级仲裁动作。只在有 evidence
    #   时前置介入(纯数值/逻辑 Type-A 会被 verify_as_search 判成不介入·退回原盲验)。
    #   命中【EVIDENCE_CONTRADICTS】(有反证)→ 直接 needs_more_verification·不再走
    #   "无证据保主"老路(治 Solanum/仲裁保守 bug 的根)。其余情况【继续】走下面既有
    #   异构验证器链(不改老路)。env MEYO_VERIFY_AS_SEARCH 可关·fail-safe。
    _vas_reason = ""
    if evidence:
        try:
            import verify_as_search as _vas
            if _vas._enabled():
                _sfn = None
                try:
                    import qa_step_library as _M
                    _scope = _M.step0_scope_judge(question)

                    def _sfn(q, scope=_scope):   # 只读检索(Doc1 L3 安全约束)
                        return _M.step2_parallel_search(q, scope)
                except Exception:
                    _sfn = None
                _vr = _vas.verify_by_search(
                    main_answer, question, evidence, search_fn=_sfn)
                _act = _vas.arbitrate(_vr, main_confirmed=main_confirmed)
                _vas_reason = "verify_as_search:%s/%s cov=%.2f" % (
                    _vr.get("verdict"), _act, float(_vr.get("coverage") or 0))
                # 有反证据【矛盾】→ 必须响应(不保守保留错答案·治头号病根)
                if _vr.get("verdict") == _vas.V_CONTRADICTS \
                        or _act == _vas.ACT_RETRY:
                    return {
                        "triggered": True, "final_answer": main_answer,
                        "agreed": False, "verdict": {"verdict_answer": "",
                            "why": _vr.get("reason", "")},
                        "needs_more_verification": True,
                        "reason": ("evidence_contradicts(检察官主动搜反例·发现证据矛盾"
                                   "·必须回锦标赛/继续搜·不保守保留) · %s"
                                   % _vr.get("reason", "")),
                        "elapsed_s": round(time.time() - t0, 3),
                    }
        except Exception:
            _vas_reason = ""

    # v13.93 · 优先用【异构模型(gpt-5/gemini)基于资料复算】(用户架构:接替干活+数字校验)·
    # 异构模型错法不同·交叉验证价值远高于同家换tier。失败退回原claude verifier。
    # v127(2026-08-19 · 用户实证 Solanum hermannii 案例修复) · 病根: 原来只取
    # evidence[:8](池里最早加入的 8 条)×400 字——对"锁错相邻实体"这类题，
    # 恰恰是【后期反查阶段】才找到的关键辨析证据(比如某学名的同义词表、某球队
    # 队史的关键年份)最有判断力，早期粗筛的前 8 条反而是导致锁错候选的那批。
    # 只给"最早、最短"的证据 = 让裁判看的资料本身就有偏置，裁判天然更容易
    # 附和主模型的最初误判。改为:取【最近 16 条】(优先新证据，含反查产物)+
    # 单条截断放宽到 900 字(taxon 同义词表/球队队史这类需要更长上下文才能
    # 判断的关键句常常超过 400 字被腰斩)。
    _ev_text = ""
    if evidence:
        try:
            _ev_list = evidence if isinstance(evidence, list) else [evidence]
            _ev_recent = _ev_list[-16:] if len(_ev_list) > 16 else _ev_list
            _ev_text = "\n".join(str(e)[:900] for e in _ev_recent)
        except Exception:
            _ev_text = ""
    verdict = _call_verifier_heterogeneous(question, main_answer, evidence_text=_ev_text)
    # v127 · 标记这次实际拿到裁决的 verifier 有没有真的看到证据——异构路径看到了
    # (_ev_text 已传入 prompt)；回退到同家 _call_verifier 时【完全不传证据】
    # (见 _VERIFY_PROMPT/_call_verifier 签名)，即使 _ev_text 本身非空，这条回退
    # 路径的 verifier 依然是"裸判"，后面"无证据型 disagree 不算数"的豁免要按
    # 实际发生的路径判，不能只看 _ev_text 是否非空。
    _verdict_saw_evidence = bool(verdict is not None and len(_ev_text or "") >= 30)
    if verdict is None:
        # 退回原同家 verifier(sonnet)·此路径不给证据·裸判
        verdict = _call_verifier(question, main_answer, model_tier="mid")
        _verdict_saw_evidence = False
    if verdict is None:
        return {
            "triggered": True, "final_answer": main_answer, "agreed": None,
            "verdict": None, "reason": "verifier_unavailable · 放行主答案",
            "elapsed_s": round(time.time() - t0, 3),
        }

    # 一致 → 直接采纳主答案
    if verdict["agree"]:
        return {
            "triggered": True, "final_answer": main_answer, "agreed": True,
            "verdict": verdict, "reason": f"agree · {verdict['why']}",
            "elapsed_s": round(time.time() - t0, 3),
        }

    # v13.96 · 过滤无效 disagree(第二模型自己没看懂/拒答 ≠ 答案有争议)
    # 事故: 表格题 $89706.00 被拦 30s·因为 Sonnet 说 disagree 但理由是"拒答/没收到文件"
    # 这不是实质性分歧——只有第二模型给出了具体替代答案或具体理由时才硬拦。
    _invalid_disagree_patterns = (
        "拒绝", "refuse", "无法", "unable", "没收到", "未收到",
        "没有文件", "no file", "cannot access", "不能访问",
        "i don't have", "没有足够", "insufficient",
        # 2026-08-19(KM实证·Russell Lyons题)· 治【第二模型因缺证据/题面被截断而
        #   说"无法验证多约束→主答案是拼凑猜测"·把已充分验证的正确答案硬翻成错】。
        #   本 verifier 按设计【故意不给 evidence】(cross_verify.py:104)·所以它
        #   "验证不了多约束"是【它自己的信息缺失】·不是主答案有实质争议——正是
        #   v13.96 要过滤的那类无效 disagree。只在【没给出具体替代实体】时判无效。
        "被截断", "截断", "无法验证", "无法核实", "验证不了", "拼凑", "猜测",
        "guess", "cannot verify", "can't verify", "unable to verify",
        "not enough", "lack of", "缺少", "缺乏", "truncat",
    )
    _why_lower = (verdict.get("why") or "").lower()
    _alt = (verdict.get("verdict_answer") or "").strip()
    _is_invalid_disagree = (
        any(p in _why_lower for p in _invalid_disagree_patterns)
        and not _alt  # 没给替代答案 + 拒答/无法验证理由 → 无效(不硬翻主答案)
    )
    if _is_invalid_disagree:
        return {
            "triggered": True, "final_answer": main_answer, "agreed": None,
            "verdict": verdict,
            "reason": f"disagree_filtered(第二模型拒答·非实质性分歧) · {verdict['why']}",
            "elapsed_s": round(time.time() - t0, 3),
        }

    # 2026-08-19(KM实证·Russell Lyons题·v127 修订)
    #   【原始动机】: 若 verifier 确实没拿到任何资料(_ev_text 为空)·它对多跳约束题
    #   说"无法验证"是【真话】(它自身信息不足)·不该让这种"验证不了"翻掉一个已用
    #   大量证据支撑的主答案。
    #   【v127 前的 bug(用户实证 Solanum hermannii 案例指出)】: 这条判据只看
    #   disagree 的【措辞】里有没有"证据不足/无法验证"这类字眼·完全不检查
    #   ①这次调用【到底有没有真的传 evidence 给第二模型】 ②第二模型是否给出了
    #   具体的替代候选(_alt)。结果是: 就算这次已经把 evidence 真传给了第二模型
    #   (它是【看过资料后】依然不认同、并给出了具体的候选异名/候选队名)·只要它
    #   谦虚地说了一句"现有资料还不能100%确证"·就被这条规则整体判"不算数"·
    #   直接保留主答案——等于系统性地让"仲裁/反查"机制对专业实体题失效(裁判其实
    #   给了有依据的意见，却被当成没依据的噪声滤掉)。
    #   【修复】只有在【本次真的没给 verifier 传 evidence】(_ev_text 为空/极短)时·
    #   才适用"它自身信息不足→不算数"的豁免；只要 evidence 非空(它是基于资料判的)·
    #   哪怕措辞里带"不能完全确证"也【不再无条件保留主答案】——按下面正常的
    #   "无替代答案才算无效"(_is_invalid_disagree)判据处理，或进入第三模型仲裁。
    #   给了具体 alt 时额外要求 agent 必须去核实，而不是静默压制。
    if (os.environ.get("MEYO_CV_KEEP_MAIN_ON_NOEVIDENCE", "1").strip().lower()
            not in ("0", "false", "off", "no")
            and not _verdict_saw_evidence):
        _noverify_patterns = (
            "被截断", "截断", "无法验证", "无法核实", "验证不了", "拼凑", "猜测",
            "缺证据", "没有证据", "证据不足", "无evidence", "没给evidence",
            "guess", "cannot verify", "can't verify", "unable to verify",
            "truncat", "no evidence", "lack of evidence", "insufficient evidence",
        )
        if any(p in _why_lower for p in _noverify_patterns):
            # 2026-08-19 治【仲裁保守 bug】(Solanum hermannii 题·交叉验证发现问题
            #   但决策没响应): Russell Lyons 内核只在【主答案已充分验证 confirmed】时
            #   才成立——那时第二模型的"无法验证"确属它自身信息不足·保留主答案对。
            #   但【主答案自己都没到 confirmed】时(如模型迭代到 hermannii 但约束覆盖
            #   还不高)·第二模型的 disagree 是【应当响应的真信号】·不能保守保留错主
            #   答案了事——应回锦标赛/继续搜(needs_more_verification=True)。
            #   main_confirmed=True/None 时保留旧行为(内核不破·fail-safe)。
            if main_confirmed is False:
                return {
                    "triggered": True, "final_answer": main_answer, "agreed": False,
                    "verdict": verdict, "needs_more_verification": True,
                    "reason": ("disagree_main_unconfirmed(主答案未充分验证 confirmed·"
                               "第二模型 disagree 应响应·回锦标赛/继续搜·不保守保留) · %s"
                               % verdict.get("why", "")),
                    "elapsed_s": round(time.time() - t0, 3),
                }
            return {
                "triggered": True, "final_answer": main_answer, "agreed": None,
                "verdict": verdict, "needs_more_verification": False,
                "reason": ("disagree_noevidence(主答案已 confirmed·第二模型因自身缺证据/"
                           "题面截断说'无法验证'·非指出具体事实错误·不翻主答案) · %s"
                           % verdict.get("why", "")),
                "elapsed_s": round(time.time() - t0, 3),
            }

    # 不一致 → 决定要不要第三模型仲裁
    mode = os.environ.get("MEYO_CROSS_VERIFY_MODE", "second_then_third")
    if mode != "second_then_third":
        return {
            "triggered": True, "final_answer": main_answer, "agreed": False,
            "verdict": verdict, "reason": f"disagree · {verdict['why']} (无第三模型)",
            "elapsed_s": round(time.time() - t0, 3),
        }

    # 第三模型仲裁:longcat-2.0(快且便宜),让两个异源答案 + 自己思考,挑一个
    arbiter_prompt = _ARBITER_PROMPT.format(
        question=(question or "").strip()[:1500],
        main_answer=(main_answer or "").strip()[:1000],
        alt_answer=verdict.get("verdict_answer") or "(未给出)",
        main_why=verdict.get("why") or "",
    )
    try:
        r = _gw_call(arbiter_prompt, timeout=30, tier="light", max_tokens=300)
        if r and r.get("ok"):
            out = (r.get("out") or "").strip()
            m = re.search(r"\{.*\}", out, re.DOTALL)
            if m:
                try:
                    ad = json.loads(m.group(0))
                    pick = str(ad.get("pick") or "").lower()
                    if pick == "main":
                        return {
                            "triggered": True, "final_answer": main_answer,
                            "agreed": False, "verdict": verdict,
                            "reason": f"disagree · 仲裁保留主答案 · {ad.get('why','')}",
                            "elapsed_s": round(time.time() - t0, 3),
                        }
                    if pick == "alt" and verdict.get("verdict_answer"):
                        return {
                            "triggered": True,
                            "final_answer": verdict["verdict_answer"],
                            "agreed": False, "verdict": verdict,
                            "reason": f"disagree · 仲裁改用 alt · {ad.get('why','')}",
                            "elapsed_s": round(time.time() - t0, 3),
                        }
                except Exception:
                    pass
    except Exception:
        pass
    # 仲裁失败/格式错 → 默认保留主答案 + 记入 alert
    _alert_if_possible("cross_verify_disagree_no_arbiter", {
        "question": (question or "")[:300],
        "main_answer": (main_answer or "")[:500],
        "alt_answer": (verdict.get("verdict_answer") or "")[:500],
        "why": verdict.get("why", ""),
    })
    return {
        "triggered": True, "final_answer": main_answer, "agreed": False,
        "verdict": verdict, "reason": f"disagree · 仲裁失败·保留主答案 · {verdict['why']}",
        "elapsed_s": round(time.time() - t0, 3),
    }


_ARBITER_PROMPT = """两个独立 AI 给同一题出了不同答案,你是仲裁。请独立判断哪个更合理。

【问题】
{question}

【主答案(opus 出)】
{main_answer}

【备选答案(sonnet 出)】
{alt_answer}

【主答案被反对的理由】
{main_why}

【仲裁规则(按序适用)】
1. 计算题先查【数据闭合】:若题面数据恰好凑齐某教科书标准公式的全部输入
   (如理想气体方程只需 n,P,T·一个不多一个不少)且只要一个数·则出题人就是按该
   标准公式出题——两个候选中来自标准公式/闭式计算的优先。
   【警惕】若备选答案和反对理由都基于同一套高阶假设(真实物性/非理想修正/EOS选型)·
   那是共享偏见不是共识——高阶模型之间本身就能差±10%·出题人不会拿它出
   精确到个位的题。(实证:Freon-12海沟题 gold=55=理想气体·仲裁信'真实密度
   更合理'选193→错。)
2. 都不适用时才凭事实/量级/单位自洽性独立判断。

只输出 JSON: {{"pick": "main"|"alt", "why": "20字内"}}
"""


def _alert_if_possible(kind: str, payload: dict) -> None:
    """轻量 alert 落盘:失败/不一致留痕,供后续人工/进化复盘。不阻塞主流程。"""
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.join(_o.path.dirname(_o.path.abspath(__file__)), "..", "..", "v11", "evolution"))
        from alert_bus import notify as _n
        _n("cross_verify", kind, json.dumps(payload, ensure_ascii=False)[:600])
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # 1) 触发判据自测
    tests = [
        ("41", False, "纯数字无单位 → 保守不触发(短答案+无单位)"),
        ("3.14 kg", True, "数字+单位"),
        ("orange, white, blue", True, "多值"),
        ("2024-01-15", True, "日期"),
        ("EC-12345", True, "英文 ID"),
        ("Hello world, this is a long essay about... ", False, "短文(<800)无数字/逗号/ID → 不触发"),
        ("UNKNOWN", False, "弃权不触发"),
        ("不知道", False, "弃权中文不触发"),
        ("Picnic is in Polybius Plaza.", False, "短英文短语无数字/逗号/ID → 不触发"),
        ("x" * 1000, False, "长文(>800) → 不触发(成本不划算)"),
        ("temperature is 25.5 °C at 101 kPa", True, "数字+单位组合"),
        ("EC 1.1.1.1", True, "英文 ID with dots"),
    ]
    print("== 触发判据 ==")
    for ans, expected, note in tests:
        got = _is_high_risk(ans)
        flag = "✅" if got == expected else "⚠️"
        print(f"  {flag} [{note}] got={got} expected={expected} · {ans!r}")