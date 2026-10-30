# -*- coding: utf-8 -*-
"""constraint_ledger · 约束逐条对账门(P0-①·2026-08-19) · 治bc头号病根。

20道bc错题共性诊断(+KM2781841859五专家文档)锁定真ROOT(18/20):
  收敛器用【名气/频次/部分匹配】选实体·而非【逐条硬约束确证】。
  → 高置信错锁(Q16 agent自评78%硬锁Lil Pump·gold=Minh Nguyen越南小众)·
    低置信门(P0-③)拦不住78%自信——只有约束对账门能让famous实体现原形。

核心机制(唯一能治高置信错锁):
  finalize前·候选对题面【每个硬约束】三态判定(satisfied/contradicted/unknown)·
  任一 contradicted → REJECT+回溯(别锁定)。
  ★置信度由【客观硬约束通过率】驱动·不是模型自报(KM文档:verbalized confidence是
    噪音·agent写"Confidence:20%/78%"是收敛后自我合理化修辞·不可信)。
  ★反显著度偏置:候选越famous(检索频次/知名度高)·SATISFIED越要求独立命中高特异
    约束(出生地/族裔/时段)·仅靠"名字出现/部分匹配"通过的→降UNKNOWN。

复用(不重造): identity_coverage.classify_claims_tri_state(451·三态判别)、
  extract_claims(68·拆约束)、collect_evidence(199)、CLAIM_* 常量。

红线: GAIA数值/单一实体题(结果型)走原路不误伤·fail-safe(异常→放行不拦finalize)·
  不透答案(只对账候选vs约束·从不给正确答案)·env MEYO_CONSTRAINT_GATE 可关。
"""
import os
import re


def _enabled():
    return os.environ.get("MEYO_CONSTRAINT_GATE", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def _anti_salience_enabled():
    return os.environ.get("MEYO_ANTI_SALIENCE", "1").strip().lower() \
        not in ("0", "false", "off", "no")


def compute_hard_confidence(verdicts):
    """★硬约束通过率驱动的置信度(替代verbalized confidence·KM文档核心)。
    verdicts: list[str] 每条约束的三态(satisfied/contradicted/unknown)。
    返回 (confidence[0..1], reject_bool, reason)。
      · 任一 contradicted → 置信=0.0·reject=True(封顶·强制拒绝·这是治高置信错锁的关键:
        Q16自评78%但族裔约束contradicted→置信强制0→reject·自评没用)。
      · 无contradicted: 置信 = satisfied/total(有unknown不算全过·置信打折)。
    fail-safe: 空→(1.0, False)(不拦)。"""
    try:
        vs = [str(v or "").lower() for v in (verdicts or [])]
        total = len(vs)
        if total == 0:
            return 1.0, False, "无约束(不拦)"
        contradicted = sum(1 for v in vs if v in ("contradicted", "violated"))
        satisfied = sum(1 for v in vs if v == "satisfied")
        unknown = total - contradicted - satisfied
        if contradicted > 0:
            return 0.0, True, ("硬约束被矛盾 %d/%d → 置信封顶0·强制reject"
                               "(不信模型自评)" % (contradicted, total))
        conf = round(satisfied / float(total), 3)
        # ★KM2781519228第二点: UNKNOWN硬约束不能放行(没确证不能锁·治Rennes类:母队没
        #   确证却锁了)。严格模式(默认开)下·unknown比例过高(≥半数且有未确证)→也reject
        #   (置信不足以锁定)。可 env MEYO_CONSTRAINT_STRICT=0 关(退回只拦contradicted)。
        _strict = os.environ.get("MEYO_CONSTRAINT_STRICT", "1").strip().lower() \
            not in ("0", "false", "off", "no")
        if _strict and total >= 2 and unknown >= (total + 1) // 2 and satisfied < total:
            return conf, True, ("硬约束未确证过多 unknown %d/%d(未全确证不能锁定)·"
                                "置信=%.2f → 需补证据或换候选" % (unknown, total, conf))
        # 有unknown约束 → 未全确证·置信不满(封顶提醒调用方"缺确证")
        return conf, False, ("约束通过 %d/%d(unknown %d)·置信=%.2f"
                             % (satisfied, total, unknown, conf))
    except Exception:
        return 1.0, False, "对账异常·fail-safe不拦"


def answer_grounding(candidate, evidence, min_hits=1):
    """★收尾grounding检查(治"凭LLM知识脑补答案·证据里0次"·足球题真病根:Rennes/
    Celtic在检索证据里0次却被交)。候选答案的关键实体token是否在【检索证据】里出现·
    完全没出现=脱离证据脑补→grounded=False。返回 {grounded, hits, reason}。
    ★核心:答案必须来自检索证据·不是LLM内部知识。fail-safe → grounded=True(不拦)。
    env MEYO_ANSWER_GROUNDING 默认开。"""
    out = {"grounded": True, "hits": 0, "reason": ""}
    try:
        if os.environ.get("MEYO_ANSWER_GROUNDING", "1").strip().lower() \
                in ("0", "false", "off", "no"):
            return out
        cand = str(candidate or "").strip()
        if not cand or len(cand) < 3:
            return out   # 空/极短→不判(交给别的门)
        # 候选的显著token(≥3字符·剥常见词)
        toks = [t for t in re.split(r"[\s,./、·\-\"'()]+", cand)
                if len(t) >= 3 and t.lower() not in
                ("the", "and", "for", "was", "were", "answer", "explanation")]
        if not toks:
            return out
        pool = ""
        for e in (evidence or []):
            if isinstance(e, dict):
                pool += " " + str(e.get("text") or e.get("content")
                                  or e.get("summary") or "").lower()
            else:
                pool += " " + str(e or "").lower()
        if not pool.strip():
            return out   # 没证据池→不判(可能证据没传·fail-safe不误拦)
        hits = sum(1 for t in toks if t.lower() in pool)
        out["hits"] = hits
        # 关键实体token一个都没在证据里出现 = 脱离证据脑补
        if hits < min_hits:
            out["grounded"] = False
            out["reason"] = ("答案关键实体在检索证据里出现 %d 次(<%d)——"
                             "这个答案很可能是凭已知知识脑补·不是从检索证据得出。"
                             % (hits, min_hits))
        return out
    except Exception:
        return out


def _fame_score(candidate, evidence):
    """候选的显著度/名气分(启发·检索频次≈证据里出现次数)。返回 0..1。
    越高越famous·反显著度用它决定是否提升验证严格度。fail-safe → 0。"""
    try:
        cand = str(candidate or "").strip().lower()
        if not cand:
            return 0.0
        # 显著token(候选里≥3字符的词)在证据池出现总次数
        toks = [t for t in re.split(r"[\s,./、·\-]+", cand) if len(t) >= 3]
        if not toks:
            return 0.0
        pool = ""
        for e in (evidence or []):
            if isinstance(e, dict):
                pool += " " + str(e.get("text") or e.get("content")
                                   or e.get("summary") or "").lower()
            else:
                pool += " " + str(e or "").lower()
        hits = sum(pool.count(t) for t in toks)
        # 归一: 出现≥6次算高显著(famous候选反复被检索到)
        return round(min(1.0, hits / 6.0), 3)
    except Exception:
        return 0.0


def _apply_anti_salience(candidate, claims, tri, evidence):
    """★反显著度偏置(治famous盖过小众gold): 候选越famous·SATISFIED越要求独立
    命中【高特异约束】(引号专名/族裔/出生地/精确时段/数字)。famous候选若其
    satisfied只来自泛泛部分匹配(无高特异约束支撑)→把这些satisfied降级unknown。
    返回调整后的 verdicts(list)。fail-safe → 原样。"""
    try:
        if not _anti_salience_enabled():
            return [t.get("state") for t in tri]
        fame = _fame_score(candidate, evidence)
        FAME_TH = float(os.environ.get("MEYO_FAME_THRESH", "0.6") or 0.6)
        out = []
        for i, t in enumerate(tri):
            st = t.get("state")
            claim_txt = str((claims[i] if i < len(claims) else {}).get("text")
                            or "")
            # 高特异约束: 含引号专名/族裔/年份区间/数字/地名等强区分信息
            high_spec = bool(re.search(
                r'"[^"]{2,}"|\b(born|生于|出生|ethnic|nationality|籍|族|'
                r'19\d\d|20\d\d|\d{2,})\b', claim_txt, re.I))
            # famous候选 + satisfied + 该约束不是高特异 → 可能靠名气蒙混 → 降unknown
            if (fame >= FAME_TH and st == "satisfied" and not high_spec):
                out.append("unknown")
            else:
                out.append(st)
        return out
    except Exception:
        return [t.get("state") for t in tri]


def constraint_reconcile(candidate, question, evidence, *,
                         claims=None, triage=None, llm_call=None):
    """约束逐条对账门(编排入口)。返回 dict:
      { enabled, reject(bool), confidence(0..1), reason,
        verdicts:[三态], contradicted_claims:[被矛盾的约束文本],
        satisfied:n, contradicted:n, unknown:n, fame:float }
    ★用法: finalize前调·reject=True→别锁定该候选·回溯/换候选/继续搜。
    结果型任务由调用方在外层判(该门对'实体识别/多约束'题最有价值)。
    fail-safe: 关闭/异常/拆不出约束 → reject=False(放行·不拦finalize)。"""
    res = {"enabled": False, "reject": False, "confidence": 1.0,
           "reason": "", "verdicts": [], "contradicted_claims": [],
           "satisfied": 0, "contradicted": 0, "unknown": 0, "fame": 0.0}
    try:
        if not _enabled():
            return res
        cand = str(candidate or "").strip()
        if not cand:
            return res   # 空候选→交给done-final空修复·不拦
        res["enabled"] = True
        import identity_coverage as _ic
        # ★收尾grounding优先检查(治凭空脑补·足球题真病根:Rennes证据0次却交)。
        #   答案关键实体完全不在检索证据里→脱离证据脑补→直接reject。
        _grd = answer_grounding(cand, evidence)
        if not _grd.get("grounded"):
            res["reject"] = True
            res["confidence"] = 0.0
            res["reason"] = "[grounding] " + _grd.get("reason", "")
            res["grounding_hits"] = _grd.get("hits", 0)
            return res
        # 复用: 拆约束
        if claims is None:
            try:
                claims = _ic.extract_claims(question, existing_triage=triage,
                                            llm_call=llm_call)
            except Exception:
                claims = []
        if not claims:
            return res   # 拆不出硬约束(纯开放题)→不介入·放行
        if evidence is None:
            evidence = []
        # 复用: 三态判别(satisfied/contradicted/unknown)
        tri = _ic.classify_claims_tri_state(claims, evidence, candidate=cand,
                                            llm_call=llm_call)
        tri_list = tri if isinstance(tri, list) else (tri or {}).get("per_claim") or []
        # 归一每条的 state。★关键: identity_coverage.classify_claims_tri_state
        #   返回的字段名是 "status"(satisfied/contradicted/unknown)·不是"state"/
        #   "verdict"——之前漏读 status 导致全判 unknown(Wave3 conf=0 却不reject 的真
        #   根因·又是"接了没接对"字段名)。三个 key 都兼容。
        norm = []
        for t in tri_list:
            if isinstance(t, dict):
                norm.append({"state": t.get("status") or t.get("state")
                             or t.get("verdict") or "unknown",
                             "text": t.get("text")})
            else:
                norm.append({"state": "unknown", "text": ""})
        # ★反显著度: famous候选的泛泛satisfied降unknown
        verdicts = _apply_anti_salience(cand, claims, norm, evidence)
        res["verdicts"] = verdicts
        res["fame"] = _fame_score(cand, evidence)
        # ★置信由硬约束通过率算(非verbalized)
        conf, reject, reason = compute_hard_confidence(verdicts)
        res["confidence"] = conf
        res["reject"] = reject
        res["reason"] = reason
        res["satisfied"] = sum(1 for v in verdicts if str(v).lower() == "satisfied")
        res["contradicted"] = sum(1 for v in verdicts
                                  if str(v).lower() in ("contradicted", "violated"))
        res["unknown"] = len(verdicts) - res["satisfied"] - res["contradicted"]
        # 被矛盾的约束文本(给回溯用·指明哪条约束把候选否了)
        for i, v in enumerate(verdicts):
            if str(v).lower() in ("contradicted", "violated"):
                ct = str((claims[i] if i < len(claims) else {}).get("text") or "")
                if ct:
                    res["contradicted_claims"].append(ct[:120])
        return res
    except Exception:
        return res


def _extract_candidate_entities(evidence, max_cand=12):
    """从证据池提取【候选实体】(专名/大写词组/引号短语)。返回 list[str]·去重。
    治gold-in-thoughts丢失(KM2781609136):正确实体在证据里出现过·收敛时要能重新拿到
    候选池·而非只靠LLM在自然语言里记(交接会丢)。fail-safe → []。"""
    import re as _re
    cands = []
    seen = set()
    # 停用词/句首常见词/★系统噪声词(工具名/元信息·治从tool_log提出AgentIR/ENTITY/VLM噪声)
    _STOP = {"the", "this", "that", "these", "those", "he", "she", "it", "they",
             "we", "i", "a", "an", "explanation", "answer", "question", "the answer",
             "europa", "league", "europa league", "world", "worlds", "step",
             "first", "second", "within", "based", "given", "however", "but",
             "and", "or", "in", "on", "at", "for", "with", "reasoning", "analysis",
             "confidence", "note", "final", "exact", "clue", "clues",
             # ★系统/工具噪声(tool_log元信息·非实体答案)
             "agentir", "entity", "vlm", "readtimeout", "sar", "person", "phd",
             "ams", "api", "github", "pubchem", "wikidata", "wayback", "cdx",
             "run_parallel_search", "fetch_url_smart", "ask_expert_model",
             "query_structured_db", "scan_environment", "shell_exec",
             "self_calibrate", "predict_then_search", "run_factual_extract",
             "key constraints", "reasoning through", "constraints", "clue chain",
             "candidate", "verdict", "coverage", "source", "sources", "budget"}
    # 系统噪声段标记(整段跳过·心跳自省/元信息不是检索结果)
    _NOISE_SEG = ("心跳自省", "主动审视", "[已附证据文本]", "决策间隔", "query多样性硬顶",
                  "反思脑", "Reasoning Through", "Key constraints", "request_more_budget",
                  "[DEGRADED]", "wall_block", "Reflector", "硬阻断")
    try:
        for e in (evidence or []):
            txt = (str(e.get("text") or e.get("content") or e.get("summary") or "")
                   if isinstance(e, dict) else str(e or ""))
            if not txt:
                continue
            # ★去系统噪声: 只删噪声【句】·不删整段(治之前整段跳过把真实实体如
            #   Trolltunga一起丢了)。按句切·丢掉含噪声标记的句·保留真检索内容句。
            if any(m in txt for m in _NOISE_SEG):
                _keep = []
                for _sent in re.split(r'(?<=[.。!?！？\n])', txt):
                    if not any(m in _sent for m in _NOISE_SEG):
                        _keep.append(_sent)
                txt = " ".join(_keep)
                if not txt.strip():
                    continue   # 整段全是噪声·才跳
            # 专名: 大写开头词组(可含 of/the/de/van/di 连接) + 引号短语
            for m in _re.findall(
                    r'[A-Z][a-zA-Z]+(?:\s+(?:[A-Z][a-zA-Z]+|of|the|de|van|di|von))'
                    r'{0,3}|"[^"]{2,50}"', txt):
                c = m.strip().strip('"').strip()
                # ★去噪: 切在句号处(治"FC Krasnodar. Arsenal"→取"FC Krasnodar")·
                #   去尾部标点·去两端连接词
                c = _re.split(r'[.。;;:!?\n]', c)[0].strip()
                c = _re.sub(r'^(?:the|a|an)\s+', '', c, flags=_re.I).strip()
                c = _re.sub(r'\s+(?:of|the|in|and)$', '', c, flags=_re.I).strip()
                if len(c) < 3:
                    continue
                cl_low = c.lower()
                if cl_low in seen or cl_low in _STOP:
                    continue
                try:
                    import candidate_quality as _cq
                    if _cq.is_technical_noise(c):
                        continue
                    c = _cq.normalize_candidate(c)
                    cl_low = c.lower()
                except Exception:
                    pass
                # 单个句首大写词(无空格·且是常见词)大概率非实体·跳
                if " " not in c and cl_low in _STOP:
                    continue
                seen.add(cl_low)
                cands.append(c)
                if len(cands) >= max_cand:
                    return cands
    except Exception:
        pass
    return cands


def suggest_best_candidate(question, evidence, *, claims=None, triage=None,
                           exclude=None, llm_call=None):
    """★"找到对的"(非只拒错·KM2781609136 gold-in-thoughts丢失): 从证据池提取候选
    实体·每个算【约束覆盖率】·推荐覆盖最高的(治Krasnodar约束覆盖高却被Rennes叙事
    流畅盖过)。返回 dict:
      { enabled, best:最优候选, coverage:它的约束覆盖率, ranked:[(cand,cov)...],
        reason }
    收敛/撞墙时用:别交叙事流畅的·交约束覆盖最高的。fail-safe → best=None(不干预)。"""
    res = {"enabled": False, "best": None, "coverage": 0.0, "ranked": [],
           "reason": ""}
    try:
        if not _enabled():
            return res
        res["enabled"] = True
        import identity_coverage as _ic
        if claims is None:
            try:
                claims = _ic.extract_claims(question, existing_triage=triage,
                                            llm_call=llm_call)
            except Exception:
                claims = []
        if not claims:
            return res
        cands = _extract_candidate_entities(evidence)
        ex = set((exclude or []))
        ex = {str(x).strip().lower() for x in ex}
        scored = []
        for c in cands:
            if c.strip().lower() in ex:
                continue   # 排除已知错候选(如agent交错的Rennes)
            try:
                cov = _ic.compute_coverage(claims, evidence, candidate=c,
                                           llm_call=llm_call)
                cvr = float(cov.get("coverage") or 0.0) if isinstance(cov, dict) else 0.0
            except Exception:
                cvr = 0.0
            scored.append((c, round(cvr, 3)))
        # 按覆盖率降序·覆盖率相同不看名气(反显著度:别偏famous)
        scored.sort(key=lambda x: -x[1])
        res["ranked"] = scored[:6]
        if scored and scored[0][1] > 0:
            res["best"] = scored[0][0]
            res["coverage"] = scored[0][1]
            res["reason"] = ("证据池中约束覆盖最高的候选=%s(覆盖%.0f%%)·"
                             "优先于叙事流畅但覆盖低的候选" % (scored[0][0],
                                                     scored[0][1] * 100))
        return res
    except Exception:
        return res


def gate_advice(recon, best_suggestion=None):
    """对账门reject时·给agent的换向建议(通用·不透答案)。
    best_suggestion: suggest_best_candidate的返回·有则附"证据里约束覆盖最高的候选"提示
    (帮agent找对的·非只拒错)。"""
    try:
        if not recon or not recon.get("reject"):
            return ""
        cc = recon.get("contradicted_claims") or []
        lines = ["[约束对账门] 当前候选与题面硬约束【矛盾】·不能锁定为答案·"
                 "置信=%.2f(由硬约束通过率算·非自评)。" % (recon.get("confidence") or 0)]
        if cc:
            lines.append("被矛盾的约束(候选不满足这些·必须换候选或补证据):")
            for i, c in enumerate(cc[:4], 1):
                lines.append("  %d. %s" % (i, c))
        lines.append("正确做法: 别硬锁这个候选(哪怕它更有名/出现更多次)·"
                     "回到证据里找【同时满足全部硬约束】的实体·或对未确证约束定向补检索。"
                     "这不是判你答案错·是约束对账说这个候选被反证了。")
        # ★"找到对的": 附证据池里约束覆盖最高的候选(治gold-in-thoughts丢失·帮agent
        #   收敛到对的·非只拒错)。只给方向不断言正确(仍要agent自己核证)。
        if best_suggestion and best_suggestion.get("best"):
            lines.append("提示(非定论·你自己核证): 你已收集的证据里·约束覆盖最高的"
                         "候选是【%s】(覆盖%.0f%%)——它比当前候选更满足题面约束·"
                         "优先核证它·别让'更有名/叙事更顺'的候选盖过约束覆盖更高的。"
                         % (best_suggestion["best"],
                            (best_suggestion.get("coverage") or 0) * 100))
        return "\n".join(lines)
    except Exception:
        return ""
