# -*- coding: utf-8 -*-
"""iterative_association · 迭代联想(联想→搜→读线索→再联想·烧进引擎)

用户 /goal + Kimi 方案核心洞察: 单次 LLM 联想不够(真测:水瓶题单次联想不出 Pickford)。
Kimi 能做到是靠【迭代】: 先联想/搜一批→读到线索→把线索喂回再联想→收敛。这是
"每搜一步就把结果喂回推理·下一步基于新线索"(区别于单次盲联想)。

循环(每题上限·防烧墙钟):
  round1: LLM 基于题面联想候选实体/关键词 → 搜它们 → 收集线索
  round2: LLM 基于【题面 + round1 读到的线索】再联想 → 搜 → 收集
  ... 直到 收敛(联想稳定/找到强候选) 或 到轮数上限
  每轮的线索结构化并进证据(接 evidence_graph)·候选进 Entity Bank。

★诚实边界(真数据实证): 迭代联想的前提是【每一跳能搜到有效线索】。若第一跳就搜不到
(如水瓶题:免费源无"皮克福德水瓶小抄"这条新闻)·迭代也无米下锅——那是【数据源覆盖】
问题·非本机制能补。本机制服务【能搜到线索】的题(Kimi 说的可救的那 50%)。

铁律: search_fn/llm_call 由调用方传(复用引擎闭包)·fail-safe·
env MEYO_ITERATIVE_ASSOC=0 关·不透 gold(联想 prompt 只含题面+搜到的公开线索)。
"""
import os
import re


def _enabled():
    return os.environ.get("MEYO_ITERATIVE_ASSOC", "1") != "0"


def _max_rounds():
    try:
        return int(os.environ.get("MEYO_ITER_ASSOC_ROUNDS", "3") or 3)
    except Exception:
        return 3


def _extract_clues(hits_text, max_len=1200):
    """从搜到的文本里抽【线索片段】(供喂回再联想)。取含专名/数字的句子·去噪。"""
    try:
        t = re.sub(r"<[^>]{0,200}>", " ", str(hits_text or ""))
        sents = [s.strip() for s in re.split(r"[.。;\n]+", t) if len(s.strip()) > 15]
        # 优先含专名(大写词)或数字的句子(信息量大)
        scored = []
        for s in sents[:40]:
            score = len(re.findall(r"\b[A-Z][a-z]{2,}", s)) + len(re.findall(r"\d{2,}", s))
            if score >= 2:
                scored.append((score, s))
        scored.sort(key=lambda x: -x[0])
        clues = " | ".join(s for _, s in scored[:8])
        return clues[:max_len]
    except Exception:
        return ""


def _is_entity_name(s):
    """判是否是干净的实体名(非解释句)。真e2e实证:LLM有时回"这道题信息不完整,但我
    可以基于...做第一轮联想"这类解释被当实体名发去搜。实体名判据:长度合理·不含解释
    词/句读标点·不是整句。fail-safe → False。"""
    try:
        s = str(s or "").strip()
        if not (2 <= len(s) <= 50):
            return False
        sl = s.lower()
        # 含解释/元话语词 → 不是实体名
        _bad = ("候选", "实体", "note", "第", "基于", "这道题", "信息", "联想", "不完整",
                "可以", "我", "题面", "轮", "细节", "example", "answer", "based on",
                "let me", "i can", "the question", "however", "首先", "接下来", "因此")
        if any(w in sl for w in _bad):
            return False
        # 含中文逗号/句号/分号等句读 = 整句非实体名
        if any(p in s for p in ("，", "。", "；", "、", ": ", "; ")):
            return False
        # 逗号+过长 = 描述句(实体名如"Russell David Lyons"无逗号)
        if "," in s and len(s) > 30:
            return False
        return True
    except Exception:
        return False


def _associate(question, clues, llm_call, *, round_n=1):
    """一轮联想:基于题面+已读线索·让 LLM 给候选实体。fail-safe → []。"""
    try:
        if llm_call is None:
            return []
        # 用字符串拼接·不用 % 格式化(避开裸 % 陷阱·记忆反复踩的坑)
        _clue_block = ""
        if clues:
            _clue_block = ("\n【上一轮搜索读到的线索(基于这些线索精炼你的联想·别重复"
                           "无效方向)】:\n" + str(clues))
        prompt = (
            "多跳检索题·第 " + str(round_n) + " 轮联想。识别题面【最独特的细节】·联想它"
            "标志性关联的【具体实体】(真实专有名词人名/队名/作品名)。"
            + ("结合线索后" if clues else "") + "\n\n"
            "题面: " + str(question or "")[:500] + "\n\n"
            "基于题面" + ("和上面的线索" if clues else "") + "·给出这一轮【最可能值得搜的"
            "具体实体名】(每行一个·最多6个·只要真实专有名词·不要抽象词/题面已有通用词。"
            "若线索指向了更具体的方向·顺着它给更精确的实体):"
            + _clue_block)
        resp = llm_call(prompt)
        cands = []
        for line in str(resp or "").splitlines():
            s = re.sub(r"^[\d\.\-\*、·\s]+", "", line).strip()
            s = re.sub(r"[（(].*?[）)]", "", s).strip()
            if _is_entity_name(s):
                cands.append(s)
        seen, out = set(), []
        for c in cands:
            if c.lower() not in seen:
                seen.add(c.lower())
                out.append(c)
        return out[:6]
    except Exception:
        return []


def iterate(question, search_fn, llm_call, *, session=None, hops_budget=None):
    """迭代联想主循环:联想→搜→读线索→再联想·直到收敛/上限。
    search_fn(query)->list[hit dict]·llm_call(prompt)->str(都由调用方传)。
    返回 {"candidates":[...], "rounds":n, "clues_seen":str, "advice":str, "stalled":bool}。
    fail-safe → 空。绝不透 gold。"""
    res = {"candidates": [], "rounds": 0, "clues_seen": "", "advice": "", "stalled": False}
    try:
        if not _enabled() or search_fn is None or llm_call is None:
            return res
        _max = _max_rounds() if hops_budget is None else min(_max_rounds(), int(hops_budget))
        all_clues = ""
        all_cands, seen_cand = [], set()
        prev_cands = set()
        for rnd in range(1, _max + 1):
            res["rounds"] = rnd
            cands = _associate(question, all_clues, llm_call, round_n=rnd)
            # ★换位思考增强(2026-08-20 用户重大判断·治"联想被知名度主导·3轮不收敛"):
            #   除了 _associate 的常识联想·额外用【出题人视角+已收线索(过程信息)】产出候选——
            #   出题人视角会问"哪个符合独特传记细节·哪怕小众"·比纯联想更能跳出"只想最有名的"。
            #   ★带已有信息(题面+已搜线索)防幻觉。并进 cands(去重)。fail-safe·env可关。
            try:
                import perspective_engine as _pe_ia
                if _pe_ia._enabled() and rnd >= 1:
                    # 已收线索当作证据块传入(过程信息)·出题人视角审问
                    _ev_ia = [{"text": all_clues}] if all_clues.strip() else []
                    # ★优雅收敛:走统一 decide(PHASE_RECALL)(底层复用interrogate_as_setter)
                    _snap_ia = _pe_ia.ContextSnapshot(question, evidence=_ev_ia, llm_call=llm_call)
                    _ig = _pe_ia.decide(_snap_ia, _pe_ia.PHASE_RECALL)
                    _pc = (_ig.get("candidates") or [])
                    # interrogate 的 candidates 已过防幻觉(须沾边已有信息)·并进
                    for _c in _pc:
                        if _c and _c.lower() not in set(x.lower() for x in cands):
                            cands.append(_c)
            except Exception:
                pass
            if not cands:
                break
            # 收敛判定: 本轮联想与上轮完全相同 → 稳定·停(别空转)
            cur = set(c.lower() for c in cands)
            if cur and cur == prev_cands:
                break
            prev_cands = cur
            # 搜每个新候选·收线索
            new_clue = ""
            for c in cands[:3]:
                if c.lower() in seen_cand:
                    continue
                seen_cand.add(c.lower())
                all_cands.append(c)
                try:
                    hits = search_fn("%s %s" % (c, str(question or "")[:60]))
                    txt = " ".join(str(h.get("content", "")) + " " + str(h.get("title", ""))
                                   for h in (hits or []) if isinstance(h, dict))
                    new_clue += " " + _extract_clues(txt, max_len=600)
                    # 结构化并进证据图(接 evidence_graph·落账即结构化)
                    if session is not None and txt:
                        try:
                            import evidence_graph as _eg
                            _lc = (session or {}).get("_llm_call")
                            _eg.observe_triplets(session, txt[:2500], source="iter_assoc",
                                                 step=rnd, llm_call=_lc)
                        except Exception:
                            pass
                except Exception:
                    pass
            all_clues = (all_clues + " " + new_clue).strip()[:1500]
            # 无新线索(搜不到)→撞墙·停(数据源覆盖问题·非机制能补)
            if not new_clue.strip():
                res["stalled"] = True
                break
        res["candidates"] = all_cands[:8]
        res["clues_seen"] = all_clues[:800]
        if res["stalled"] and not all_clues.strip():
            res["advice"] = ("[迭代联想·撞墙] 联想的候选实体搜不到任何有效线索——这题的关键"
                             "事实很可能【不在当前可用检索源】(数据源覆盖边界·非推理问题)。"
                             "诚实标存疑·别硬猜。")
        elif all_cands:
            res["advice"] = ("[迭代联想] 经 %d 轮'联想→搜→读线索→再联想'·收敛到候选: %s。"
                             "基于读到的线索这些最值得精确验证·别再盲搜整句描述。"
                             % (res["rounds"], "、".join(all_cands[:5])))
        return res
    except Exception:
        return res
