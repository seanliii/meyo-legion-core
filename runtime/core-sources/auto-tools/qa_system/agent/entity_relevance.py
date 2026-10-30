# -*- coding: utf-8 -*-
"""entity_relevance · 2026-08-20 · 用户核心洞察落地

信息搜索 = 严格关系推理 + 信息计算。实体拾取一直不准(撒网锁错实体)的**真病根**:
从来没算过「包含候选实体的段落/句子 ↔ 题目约束」的**相似度/关联度**——现在靠
布尔覆盖(compute_coverage)+ 名气/频次(hits)拍脑袋选,于是「部分匹配但更有名/更高频」
的错实体被选上。

本模块补这一层:对每个候选实体,计算它**所在段落**与**题目每条约束**的关联度(连续分·
非布尔),圈选出真正在语义上同时贴合题目所有约束的实体去推动下一步。

混合方案(用户拍板·快+准平衡·不依赖重包):
  ① 粗筛(BM25·numpy手写·零依赖): 算候选段落对题目约束 token 的 BM25 相关度·粗排。
  ② 精排(LLM·仅 top-K): 对粗排靠前的候选·LLM 打「段落与题目每条约束的匹配度 0-1 +
     因果关系判断」·加权成总关联度。
  ③ 圈选: 关联度最高的候选(关联度 > 覆盖率 > 频次)。

全 fail-safe: 任何一层失败退回下一层(LLM 挂→纯 BM25→空)。env MEYO_ENTITY_RELEVANCE=0 可关。
不引入新依赖(只用 numpy·已装)·不装 sentence-transformers(embedding 当前不可得)。
"""
import os
import re
import math

_STOP = frozenset((
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "and", "or", "is", "was",
    "were", "are", "be", "by", "with", "that", "this", "who", "which", "their", "they",
    "one", "had", "has", "have", "from", "as", "it", "its", "他", "她", "的", "了", "是",
    "在", "和", "与", "或", "who", "whose", "whom", "what", "when", "where",
))


def _enabled():
    return os.environ.get("MEYO_ENTITY_RELEVANCE", "1") != "0"


def _tok(text):
    """分词(通用·英中混合): 抽字母数字词 + 中文单字·去停用词/纯短词。"""
    try:
        s = str(text or "").lower()
        # 英文词 + 数字 + 中文字符块
        toks = re.findall(r"[a-z0-9][a-z0-9\-']+|[0-9]{2,}|[一-鿿]+", s)
        out = []
        for t in toks:
            if t in _STOP or len(t) < 2:
                continue
            # 中文块拆单字(粗粒度·够用)
            if re.match(r"[一-鿿]+", t):
                out.extend([c for c in t if c not in _STOP])
            else:
                out.append(t)
        return out
    except Exception:
        return []


def _bm25_scores(query_toks, docs_toks, k1=1.5, b=0.75):
    """BM25 打分(numpy·零依赖): query 对每个 doc 的相关度。返回 list[float]。
    docs_toks: list[list[str]]。fail-safe → 全 0。"""
    try:
        import numpy as np
        N = len(docs_toks)
        if N == 0:
            return []
        # 文档频率 df
        df = {}
        for dt in docs_toks:
            for w in set(dt):
                df[w] = df.get(w, 0) + 1
        avgdl = sum(len(dt) for dt in docs_toks) / max(1, N)
        scores = []
        q_set = [w for w in query_toks if w in df]  # 只算出现过的 query 词
        for dt in docs_toks:
            dl = len(dt)
            tf = {}
            for w in dt:
                tf[w] = tf.get(w, 0) + 1
            s = 0.0
            for w in q_set:
                n_qi = df.get(w, 0)
                idf = math.log(1 + (N - n_qi + 0.5) / (n_qi + 0.5))
                f = tf.get(w, 0)
                s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
            scores.append(float(s))
        return scores
    except Exception:
        return [0.0] * len(docs_toks)


def _candidate_passages(candidate, evidence, window=280, session=None):
    """抽出【提到候选实体的信息片】·整理成该候选的'证据画像'。
    ★2026-08-20(用户洞察:先切片·再整理片间关系·才能算准关联度):
      升级点=从'固定字符窗口'改成【语义句子切片】——把 evidence 切成句子·只保留
      真正提到候选实体的句子片(语义完整·不切断/不混无关句)·再按出现顺序整理。
      并(若有 evidence_graph)附上该实体的三元组关系边(片间关系:同指/因果/属性)·
      让下游关联度算在'整理过关系的证据簇'上·而非孤立字符窗口。
    fail-safe: 切句失败→退回原字符窗口。"""
    cand = str(candidate or "").strip()
    cand_low = cand.lower()
    if not cand_low:
        return ""
    _last = cand.split()[-1].lower() if cand.split() else cand_low  # 姓氏/主词(多词宽松匹配)
    # ── ① 语义句子切片(复用 evidence_graph._sentences·句子级不切断语义)──
    sents = []
    try:
        import evidence_graph as _eg
        for e in (evidence or []):
            if not isinstance(e, dict):
                continue
            txt = str(e.get("text") or e.get("content") or "")
            for s in _eg._sentences(txt):
                sl = s.lower()
                # 只保留真正提到候选实体(全名或姓氏)的句子片
                if cand_low in sl or (len(_last) >= 4 and _last in sl):
                    sents.append(s.strip())
            if len(sents) >= 12:
                break
    except Exception:
        sents = []
    # 去重保序
    _seen, _uniq = set(), []
    for s in sents:
        _k = s[:80].lower()
        if _k not in _seen:
            _seen.add(_k)
            _uniq.append(s)
    passage = " … ".join(_uniq[:8])
    # ── ② 整理片间关系(evidence_graph 三元组·该实体的关系边附进画像)──
    if session is not None:
        try:
            import evidence_graph as _eg2
            _paths = _eg2.find_paths(session, cand, max_hops=1)
            if _paths:
                _rel_txt = "; ".join(str(p)[:80] for p in (_paths or [])[:5])
                if _rel_txt:
                    passage += "  [关系:" + _rel_txt + "]"
        except Exception:
            pass
    # ── fail-safe: 句子切片没抽到→退回原固定字符窗口(零回归)──
    if not passage.strip():
        parts = []
        for e in (evidence or []):
            if not isinstance(e, dict):
                continue
            txt = str(e.get("text") or e.get("content") or "")
            low = txt.lower()
            idx = low.find(cand_low)
            if idx < 0 and len(_last) >= 4:
                idx = low.find(_last)
            if idx >= 0:
                a = max(0, idx - window // 2)
                b = min(len(txt), idx + len(cand_low) + window // 2)
                parts.append(txt[a:b])
            if len(parts) >= 4:
                break
        passage = " … ".join(parts)
    return passage[:2000]


def rank_by_relevance(question, candidates, evidence, *, claims=None,
                      llm_call=None, top_k_llm=4, session=None):
    """★核心: 算每个候选实体【所在段落 ↔ 题目/约束】的关联度·圈选最匹配的。
    - question: 题面(约束来源)
    - candidates: [(name, hits), ...] 或 [name, ...]
    - evidence: [{text/content, source/url}, ...]
    - claims: 题面拆出的约束(可选·有则精排更细)
    返回 {best, relevance, ranked:[(name, rel, bm25, llm)...], method}。fail-safe→best=None。
    """
    res = {"best": None, "relevance": 0.0, "ranked": [], "method": "none"}
    try:
        if not _enabled():
            return res
        # 归一化 candidates
        cands = []
        for c in (candidates or []):
            if isinstance(c, (tuple, list)):
                cands.append((str(c[0]), int(c[1]) if len(c) > 1 else 0))
            else:
                cands.append((str(c), 0))
        cands = [(n, h) for n, h in cands if n.strip()]
        if not cands:
            return res
        # 题目 query tokens(约束优先·退回题面)。claims 兼容 str 列表 / dict 列表
        #   ({id,text,tokens}·identity_coverage.extract_claims 的格式)/ 混合。
        _claim_strs = []
        for _c in (claims or []):
            if isinstance(_c, dict):
                _claim_strs.append(str(_c.get("text") or _c.get("claim") or ""))
            else:
                _claim_strs.append(str(_c or ""))
        q_text = " ".join([s for s in _claim_strs if s]) if _claim_strs else str(question or "")
        q_toks = _tok(q_text)
        # 每个候选的证据画像(所在段落)
        profiles = [(n, h, _candidate_passages(n, evidence, session=session)) for n, h in cands]
        docs_toks = [_tok(p) for _, _, p in profiles]
        # ① 粗筛 BM25
        bm25 = _bm25_scores(q_toks, docs_toks)
        _mx = max(bm25) if bm25 else 0.0
        bm25n = [(s / _mx if _mx > 0 else 0.0) for s in bm25]  # 归一 0-1
        ranked_bm = sorted(
            [(profiles[i][0], profiles[i][1], bm25n[i], profiles[i][2])
             for i in range(len(profiles))],
            key=lambda x: -x[2])
        res["method"] = "bm25"
        # ② 精排 LLM(仅 top-K·省调用)——打「段落↔约束匹配度+因果」
        llm_scores = {}
        if llm_call is not None and ranked_bm:
            topk = ranked_bm[:max(1, int(top_k_llm))]
            try:
                llm_scores = _llm_relevance(question, claims, topk, llm_call)
                if llm_scores:
                    res["method"] = "bm25+llm"
            except Exception:
                llm_scores = {}
        # ③ 综合分: 有 LLM 分用 LLM 为主(0.7)+BM25(0.3)·无则纯 BM25
        final = []
        for name, hits, bmn, _prof in ranked_bm:
            ls = llm_scores.get(name)
            if ls is not None:
                rel = 0.7 * float(ls) + 0.3 * bmn
            else:
                rel = bmn
            final.append((name, round(rel, 4), round(bmn, 4),
                          round(float(ls), 4) if ls is not None else None))
        final.sort(key=lambda x: -x[1])
        res["ranked"] = final[:6]
        if final and final[0][1] > 0:
            res["best"] = final[0][0]
            res["relevance"] = final[0][1]
        return res
    except Exception:
        return res


def _llm_relevance(question, claims, topk, llm_call):
    """LLM 精排: 对 top-K 候选·打「其证据段落与题目每条约束的匹配度(含因果) 0-1」。
    返回 {name: score}。fail-safe→{}。"""
    try:
        _cl = ""
        if claims:
            _cs = []
            for c in list(claims)[:8]:
                if isinstance(c, dict):
                    _cs.append(str(c.get("text") or c.get("claim") or ""))
                else:
                    _cs.append(str(c or ""))
            _cl = "\n题目约束:\n" + "\n".join(f"  - {s}" for s in _cs if s)
        blocks = []
        for i, (name, _h, _bmn, prof) in enumerate(topk):
            blocks.append(f"[候选{i}] {name}\n  其证据段落: {prof[:500] or '(无段落)'}")
        prompt = (
            "你在给候选实体打【关联度】分·【站在出题人视角】想:题目每个约束都是出题人挑来"
            "指向唯一实体、排除相似(尤其更有名)候选的区分器。对每个候选·看它的证据段落是否"
            "在语义上**贴合题目要求**·并判断段落里的信息与约束是否有**真实因果/对应关系**"
            "(不是碰巧出现同样的词)·尤其看段落有没有覆盖那些【最具体的区分器细节】(而非只对上"
            "宽泛线索)。打分 0-1: 1=段落强支撑该候选满足题目全部关键约束(含独特区分器); "
            "0=段落与题目要求无关/矛盾。★别被名气/出现频次影响(最有名的常被某条区分器排除)·"
            "也别因'看起来像'就给高分——只认段落里【真实写到的、与约束对应的信息】·别脑补。\n\n"
            f"题目: {str(question)[:800]}{_cl}\n\n"
            + "\n\n".join(blocks) +
            '\n\n严格输出 JSON: {"scores":[{"i":0,"score":0.0,"why":"简短"}...]}')
        r = llm_call(prompt) if callable(llm_call) else None
        out = ""
        if isinstance(r, dict):
            out = r.get("out") or r.get("text") or ""
        else:
            out = str(r or "")
        m = re.search(r"\{[\s\S]+\}", out)
        if not m:
            return {}
        import json
        data = json.loads(m.group(0))
        scores = {}
        for it in data.get("scores", []):
            i = int(it.get("i", -1))
            if 0 <= i < len(topk):
                scores[topk[i][0]] = max(0.0, min(1.0, float(it.get("score", 0))))
        return scores
    except Exception:
        return {}
