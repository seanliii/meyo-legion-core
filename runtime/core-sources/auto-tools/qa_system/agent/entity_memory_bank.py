# -*- coding: utf-8 -*-
"""entity_memory_bank · 结构化证据保持(KM2781609136·2026-08-19) · 治"搜到了交接时丢了"。

核心发现(KM2781609136 + 查真实tool_log): agent不是不会搜·是搜到的实体用自然语言
传递·每次交接(工具结果→prompt→收敛)丢失隐藏状态。足球题 Arsenal 在证据里 48 次·
但最终推理里 0 次 = 收敛时把检索到的实体全丢了·凭 LLM 知识脑补。

方案(替代"事后从混杂 tool_log 正则提实体"·那不可靠): 在【工具结果落账的当下】就把
实体+出现次数+来源采集进【结构化库】(session["_entity_bank"])·收敛时从这个结构化库
选(而非从自然语言文本正则提)。当下采集比事后提准得多——因为工具结果是相对干净的
检索正文·还没被心跳自省/元信息稀释。

用法:
  · observe_entities(session, tool_result_text): 每次工具结果落账时调·采集实体。
  · rank_by_coverage(session, question, claims): 收敛时调·返回按约束覆盖排序的实体候选。

fail-safe·env MEYO_ENTITY_BANK 可关。不透答案(只采集/排序·不断言正确)。
"""
import os
import re


def _enabled():
    return os.environ.get("MEYO_ENTITY_BANK", "1").strip().lower() \
        not in ("0", "false", "off", "no")


# 系统/工具噪声词(不是实体·别采集)
_NOISE = {"agentir", "entity", "vlm", "readtimeout", "sar", "api", "github",
          "pubchem", "wikidata", "wayback", "cdx", "person", "phd", "ams",
          "the", "this", "that", "answer", "explanation", "question", "clue",
          "reasoning", "analysis", "constraints", "candidate", "source", "http",
          "https", "httpsconnectionpool", "read", "note", "final", "step",
          "europa", "league", "world", "let", "identifying", "solving", "analyzing"}
# 噪声段标记(整句是系统输出·不采集)
_NOISE_MARK = ("心跳自省", "主动审视", "决策间隔", "query多样性硬顶", "反思脑",
               "request_more_budget", "[DEGRADED]", "wall_block", "Reflector",
               "硬阻断", "Reasoning Through", "Key constraints")


def _extract_entities_from_text(txt, max_n=20):
    """从【单条工具结果】(相对干净的检索正文)抽实体。比事后从混杂日志提准。
    专名: 大写开头词/词组(含 of/de/von 连接) + 引号短语。滤噪声词。返回 list。"""
    out = []
    seen = set()
    try:
        s = str(txt or "")
        # ★去HTML/网页源码噪声(工具结果常含原始HTML·治DOCTYPE/http-equiv/div当实体)
        if "<" in s and (">" in s):
            s = re.sub(r'<[^>]{0,200}>', ' ', s)          # 去标签
            s = re.sub(r'(?i)(DOCTYPE|http-equiv|href|div|span|style|meta|'
                       r'script|Please click|Google Search)\S*', ' ', s)
        if not s or any(m in s for m in _NOISE_MARK):
            # 噪声段: 按句剔除噪声句·保留真检索句
            kept = [p for p in re.split(r'(?<=[.。!?！？\n])', s)
                    if not any(m in p for m in _NOISE_MARK)]
            s = " ".join(kept)
        if not s.strip():
            return out
        for m in re.findall(
                r'[A-Z][a-zA-Z]+(?:\s+(?:[A-Z][a-zA-Z]+|of|the|de|van|di|von|dei))'
                r'{0,4}|"[^"]{2,60}"', s):
            c = m.strip().strip('"').strip()
            c = re.split(r'[.。;;:!?\n]', c)[0].strip()
            c = re.sub(r'^(?:the|a|an)\s+', '', c, flags=re.I).strip()
            if len(c) < 3:
                continue
            cl = c.lower()
            # 整体或首词是噪声→跳
            if cl in seen or cl in _NOISE or cl.split()[0] in _NOISE:
                continue
            seen.add(cl)
            out.append(c)
            if len(out) >= max_n:
                break
    except Exception:
        pass
    return out


def observe_entities(session, tool_result_text, source=""):
    """★工具结果落账时采集实体进结构化库(治交接丢失·当下采集比事后提准)。
    session["_entity_bank"] = {entity_lower: {"name","hits","sources"}}。
    fail-safe·静默(不打扰主流程)。"""
    try:
        if not _enabled() or session is None:
            return
        ents = _extract_entities_from_text(tool_result_text)
        if not ents:
            return
        bank = session.setdefault("_entity_bank", {})
        for e in ents:
            k = e.lower()
            rec = bank.get(k)
            if rec:
                rec["hits"] = rec.get("hits", 1) + 1
                if source and source not in rec.get("sources", []):
                    rec.setdefault("sources", []).append(source[:60])
            else:
                bank[k] = {"name": e, "hits": 1,
                           "sources": [source[:60]] if source else []}
        # 有界(防膨胀·保留高频前200)
        if len(bank) > 200:
            top = sorted(bank.items(), key=lambda x: -x[1].get("hits", 0))[:200]
            session["_entity_bank"] = dict(top)
    except Exception:
        pass


def get_bank_entities(session):
    """取结构化库里的实体(按出现频次降序)。返回 [(name, hits, sources)]。"""
    try:
        bank = (session or {}).get("_entity_bank") or {}
        items = sorted(bank.values(), key=lambda r: -r.get("hits", 0))
        return [(r["name"], r.get("hits", 1), r.get("sources", [])) for r in items]
    except Exception:
        return []


def rank_by_coverage(session, question, *, claims=None, evidence=None,
                     exclude=None, llm_call=None):
    """★收敛时从【结构化库】选约束覆盖最高的实体(治gold-in-thoughts丢失·从结构化库
    选比从自然语言文本正则提可靠)。返回 {best, coverage, ranked, from_bank}。
    fail-safe → best=None。"""
    res = {"best": None, "coverage": 0.0, "ranked": [], "from_bank": True}
    try:
        if not _enabled():
            return res
        ents = get_bank_entities(session)
        if not ents:
            res["from_bank"] = False
            return res
        import identity_coverage as _ic
        if claims is None:
            try:
                claims = _ic.extract_claims(question, llm_call=llm_call)
            except Exception:
                claims = []
        if not claims:
            return res
        if evidence is None:
            try:
                evidence = _ic.collect_evidence(session)
            except Exception:
                evidence = (session or {}).get("evidence") or []
        ex = {str(x).strip().lower() for x in (exclude or [])}
        scored = []
        for name, hits, _src in ents[:30]:   # 只评估高频前30(省时)
            if name.lower() in ex:
                continue
            try:
                cov = _ic.compute_coverage(claims, evidence, candidate=name,
                                           llm_call=llm_call)
                cvr = float(cov.get("coverage") or 0.0) if isinstance(cov, dict) else 0.0
            except Exception:
                cvr = 0.0
            # 覆盖率为主·频次为次(治famous偏置:别只看频次)
            scored.append((name, round(cvr, 3), hits))
        # ★关联度圈选(2026-08-20·用户核心洞察:实体拾取不准真病根=没算'含候选实体的
        #   段落↔题目约束'的相似度/关联度·只靠布尔覆盖+名气/频次拍脑袋)。补:算每个候选
        #   所在段落与题目约束的关联度(BM25粗筛+LLM精排)·作【首要】排序权重·治
        #   "部分覆盖但更有名/高频"的错实体被选上。关联度>覆盖率>频次。fail-safe:算不出
        #   退回原(覆盖率,频次)排序·零回归。env MEYO_ENTITY_RELEVANCE=0 关。
        _rel_map = {}
        try:
            import entity_relevance as _er
            if _er._enabled() and scored:
                _rr = _er.rank_by_relevance(
                    question, [(n, h) for n, _c, h in scored], evidence,
                    claims=claims, llm_call=llm_call, session=session)
                for _n, _rel, _bm, _ls in (_rr.get("ranked") or []):
                    _rel_map[_n] = _rel
        except Exception:
            _rel_map = {}
        # 综合排序:关联度(首要)→覆盖率→频次。关联度缺时靠覆盖率+频次兜底(零回归)。
        scored2 = [(n, c, h, _rel_map.get(n, 0.0)) for n, c, h in scored]
        scored2.sort(key=lambda x: (-x[3], -x[1], -x[2]))
        res["ranked"] = [(n, c) for n, c, _h, _r in scored2[:6]]
        res["ranked_detail"] = [(n, c, r) for n, c, _h, r in scored2[:6]]  # 可观测:带关联度
        if scored2 and (scored2[0][3] > 0 or scored2[0][1] > 0):
            res["best"] = scored2[0][0]
            res["coverage"] = scored2[0][1]
            res["relevance"] = scored2[0][3]
        return res
    except Exception:
        return res
