# -*- coding: utf-8 -*-
"""first_anchor · 第一锚点提取 + 定向锚搜(补 anchor_hop 的上游·让顺藤链能启动)

诊断依据(13 道 bc 错题真 trace):主瓶颈 = C 模式撒网锁错实体 + 描述 query 搜不到 gold。
anchor_hop.py(顺藤·治 B 类)已在 native_agent_loop:1857 接好·但它的触发条件是
【_cand_ent and _sat>=1】——需要【已经有一个确证锚点】。而 13 道错题恰恰卡在【连第一个
锚点都锚不到】: 引擎首搜用整句描述撒网(吃整个题面)·描述 query 搜不到任何可靠中间实体·
所以 _cand_ent 空或错·anchor_hop 触发不了。anchor_hop 有下游·没上游。

本模块 = 补那一步上游:
  1. 从题面提取【最独特可搜的锚点特征】(通用启发·非题目特判):
       优先级 = ① 引号内短语("no sell by date" 这种) > ② 大写专有名词序列 >
                ③ 最罕见/最具体的单一约束(带数字/年份/专名·而非 "an author" 泛词)。
     grounding: 提取的锚点【必须来自题面原文子串】(不是 LLM 凭空造)。
  2. 用锚点特征【单独定向搜】(不是整句撒网)——发射交给调用方现成搜索闭包。
  3. 从定向搜结果里【提取第一个中间实体】喂回去当 _cand_ent → 下游 anchor_hop
     的 `_cand_ent and _sat>=1` 条件就能满足·顺藤链启动。

与 staged_retrieval 的区别/协同:
  - staged_retrieval.plan_stage: 给 agent【软建议】(Stage1 该搜最特异约束)——soft prompt·
    模型可能不照做(建了没接的风险)。
  - first_anchor: 【硬执行】——引擎自己提锚点、自己发定向搜、自己把中间实体喂进候选池·
    不靠模型照做。两者内核一致(先锚最特异·别整句撒网)·一软一硬互补。
  - specificity 评分【复用】staged_retrieval.rank_constraints_by_specificity。
  - 锚点清洁校验【复用】anchor_hop._is_clean_anchor。

防题库泄漏(很重要·这 13 道是 BrowseComp 官方题·已泄漏):定向搜若命中 benchmark/题库页
(含 "Answer:" 明文 / browsecomp 泄漏特征)·【识别并跳过】——绝不从泄漏页抄答案交卷
(那是假分)。提取锚点是为了真顺约束链推理·不是搜 benchmark 泄漏页。

铁律: 通用·不针对某道题·不硬编码任何题的答案/实体。fail-safe·env 可关。
env:
  MEYO_FIRST_ANCHOR      =0 关(整体旁路)·默认开
"""
import os
import re


def _enabled():
    return os.environ.get("MEYO_FIRST_ANCHOR", "1") != "0"


# ── 题库泄漏页识别(通用特征·非题目特判) ─────────────────────────────────
#   BrowseComp/GAIA 等官方题已泄漏到网上·搜到含明文答案的题库页=作弊假分。
#   识别 → 跳过该证据块·绝不当中间实体来源。通用启发·fail-safe。
_LEAK_PAT = [
    r"(?i)\bAnswer\s*[:：]\s*\S",            # "Answer: xxx" 明文答案行
    r"(?i)\bcorrect\s+answer\b",             # "correct answer is ..."
    r"(?i)\bground[\s_-]?truth\b",           # ground truth
    r"(?i)browse\s*comp",                    # BrowseComp 题库泄漏页
    r"(?i)\bgaia\b.*\b(benchmark|dataset|leaderboard)\b",
    r"(?i)\b(benchmark|dataset)\b.*\banswer\b",
    r"(?i)问题\s*[:：].*答案\s*[:：]",        # 中文题库页"问题:...答案:..."
]


def is_leakage_page(text):
    """判一段证据文本是不是题库/benchmark 泄漏页(含明文答案)。命中→跳过·别抄。
    通用启发·fail-safe(异常→False·保守不误杀)。"""
    try:
        t = str(text or "")
        if not t:
            return False
        for pat in _LEAK_PAT:
            if re.search(pat, t):
                return True
        return False
    except Exception:
        return False


# 泛词(不配当独一锚点·区分度太低·搜了也是撒网)。通用·非题目特判。
_GENERIC = {
    "an author", "a book", "a person", "a man", "a woman", "a city", "a country",
    "a company", "a film", "a movie", "a game", "a song", "a player", "a team",
    "the author", "the book", "someone", "something", "somewhere", "a novel",
    "a scientist", "a politician", "a place", "a year", "a number",
    # 句首疑问/功能词(大写但非专名·区分度为 0·剔除防误报)
    "which", "what", "who", "whom", "whose", "where", "when", "why", "how",
    "the", "this", "that", "these", "those", "there", "here", "name", "identify",
    "list", "find", "give", "tell", "during", "between", "after", "before",
}


def extract_anchors(question, max_anchors=3):
    """从题面提取【最独特可搜的锚点特征】(按区分度降序)。

    优先级(通用启发·非题目特判):
      ① 引号内短语(引号是作者标记'这个词组很独特'的最强信号·如 "no sell by date")
      ② 大写专有名词序列(连续 ≥1 词首字母大写·如 Ken Walibora)
      ③ 带数字/年份的具体短语(区分度大)
    grounding: 每个返回的锚点都是题面【原文子串】(不新造)。
    过滤: 泛词(an author 这类)剔除。返回去重保序的锚点列表(上限 max_anchors)。
    fail-safe → []。"""
    out = []
    try:
        q = str(question or "")
        if not q.strip():
            return []
        # ① 引号内短语(直/弯引号都收)·最强独特信号
        for m in re.findall(r"[\"“”'‘’]([^\"“”'‘’]{3,60})[\"“”'‘’]", q):
            s = m.strip()
            if s and s.lower() not in _GENERIC:
                out.append(s)
        # ② 大写专有名词序列(连续首字母大写词·1-4 词)
        for m in re.findall(r"\b([A-Z][a-zA-Z.]+(?:\s+[A-Z][a-zA-Z.]+){0,3})\b", q):
            s = m.strip()
            # 剔句首单个普通词(如 "The"/"What")当误报: 单词且过短跳过
            if len(s.split()) == 1 and len(s) < 4:
                continue
            if s.lower() not in _GENERIC:
                out.append(s)
        # ③ 带数字/年份的具体短语(数字周边的名词短语)
        for m in re.findall(r"\b(\d{3,4})\b(?:\s+([a-zA-Z]{3,}(?:\s+[a-zA-Z]{3,}){0,2}))?", q):
            num, tail = m
            s = (num + " " + tail).strip() if tail else num
            if s and s.lower() not in _GENERIC:
                out.append(s)
    except Exception:
        pass
    # grounding 硬校验: 只保留题面原文子串(大小写不敏感)·剔非原文/纯造的
    _ql = str(question or "").lower()
    _seen, _clean = set(), []
    for a in out:
        al = a.lower().strip()
        if not al or al in _seen:
            continue
        if al not in _ql:          # grounding: 必须是题面子串
            continue
        _seen.add(al)
        _clean.append(a.strip())
    return _clean[: max(1, int(max_anchors or 3))]


def llm_extract_anchors(question, llm_call, *, max_anchors=3):
    """★LLM 提锚点(语义判断·治正则提不出【独特约束组合】)。

    真实病根(已实测): 对 "debut novel adapted 3-episode miniseries, author wrote
    adaptation" 这类题·正则只会抓年份/引号/大写词(提出 '2016 and' 这种垃圾锚点·锚不到
    实体)。而人工最快路径是搜【独特约束组合】"author debut novel 3-episode miniseries
    wrote adaptation"→一步命中作者。这个"哪几个约束组合起来最独特、最可能一步搜到实体"
    是【语义判断】·正则做不好(铁律: 语义判断用 LLM 不用规则表)。

    prompt 让 LLM 输出 2-3 个【锚点 query 候选】(不是整句·是最独特的特征/约束组合)。
    grounding: 只保留【题面里真实出现的词】的组合(LLM 不许凭空造新信息)——校验每个候选
    query 的每个实义词都在题面里出现过·否则丢弃(防 LLM 幻觉引入题面没有的实体/属性)。
    不透 gold: prompt 只给题面·让它提"该搜什么"·不要它给答案。
    fail-safe → [](LLM 不可用/异常→空·调用方回退正则)。"""
    try:
        if llm_call is None:
            return []
        q = str(question or "").strip()
        if not q:
            return []
        n = max(1, int(max_anchors or 3))
        prompt = (
            "你现在【扮演出这道多跳检索题的人】。你心里有一个【唯一确定的答案实体】·题面"
            "每个线索都是你【精心挑的区分器】——目的是让它只指向那一个实体·【排除掉所有"
            "相似的、更有名的实体】。你的任务:想清楚【该去搜什么】才能一步锚到那个实体。\n\n"
            "以【出题人视角】分析:\n"
            "  ① 先假设有【好几个相似的、更有名的候选】都大致符合题面里那些【宽泛线索】"
            "(出生年代、'某年出道'、职业类别——一大批人都符合)。光靠宽泛线索会收敛到"
            "【最有名的那几个】·但那往往是错的(否则题目没区分度)。\n"
            "  ② ★真正能区分它们的·恰恰是那些【具体的、个人化的传记细节 / 罕见属性 /"
            "精确的时间×事件组合】——例如具体的人生经历、家庭/身世事实、与某个具体作品或"
            "团体/人物的关联。这些【未必每个知名候选都有公开记录】·正因如此才能把它们筛开。"
            "【不要因为'这类细节常见'就跳过它】——常见不等于每个候选都有可查证据·它恰恰是"
            "把范围缩到唯一的钥匙。\n"
            "  ③ 因此:把【宽泛线索 + 一到两个最具体的传记细节/关联】拼成 query 去搜·目标是"
            "搜到那个【符合全部细节的、可能你没听过的】实体·而不是搜到最有名的。\n"
            "  ④ ★关键警觉:答案很可能【不是最有名的那个】——别往'最知名的XX'方向猜。\n"
            "规则(必须遵守):\n"
            "  - query 主要用题面里【真实出现过的特征/约束词】;可以补【等价的自然搜索用词】"
            "(如把题面'gathered public attention'表达为'breakout'/'viral'·把'song'说成'track')——"
            "这些是同义改写·不是引入题面没有的【新实体/新事实/新年份】(那些绝不许编)。\n"
            "  - 不要回答最终答案·只给【该去搜什么】。\n"
            "  - 输出 %d 个候选 query·每行一个·【最能锁定唯一实体(含最具体传记细节)的排第一】。"
            "每行是一句搜索 query(3-10 个词)。\n\n"
            "题面: %s\n\n区分器搜索 query(每行一个·最能锁唯一实体的优先):"
            % (n, q[:800]))
        resp = llm_call(prompt)
        # 题面词集(实义词·用于 grounding 校验: 候选 query 的词必须来自题面)
        _qwords = set(re.findall(r"[a-z0-9]+", q.lower()))
        _bad = ("最终答案", "answer", "候选", "锚点", "query", "题面", "首先", "接下来",
                "note:", "example", "这道题", "以下", "如下", "let me", "i think",
                "the question", "here are", "here's")
        cands = []
        for line in str(resp or "").splitlines():
            s = re.sub(r"^[\d\.\-\*、·)）\s]+", "", line).strip()
            s = s.strip('"“”\'‘’ ')
            sl = s.lower()
            if not s or len(s) < 4 or len(s) > 120:
                continue
            # 剔【解释/前言/中文说明句】(真跑实证 LLM 会带 "最独特的组合是:**...**" 这种
            #   前言·含 CJK/markdown/冒号叙述)——锚点 query 必须是【干净英文关键词短语】。
            if re.search(r"[一-鿿]", s):
                continue          # 含中文字符→是说明句不是搜索 query
            if any(ch in s for ch in ("*", "：", ":", "**", "—", "。", "，", "；")):
                continue          # markdown/叙述标点→前言句·非 query
            if any(b in sl for b in _bad):
                continue
            # grounding(2026-08-20 重构·治"同义改写词被全滤→锚点返空"):
            #   防幻觉的核心是【别编新实体(人名/地名/作品名)/新年份】·不是【禁止同义改写
            #   的通用小写词】。原来要求每个实义词都在题面→把 sibling(s)/breakout/teenage/
            #   brother 这种通用词/词形变化全滤掉→锚点全空(实证 Minh 题 LLM 已生成完美区分器
            #   query 却被滤空)。改成【只拦看起来像编造实体的 token】:
            #     ①原始 query 里【题面没有的大写专名词】(首字母大写·非句首)→疑似编的新实体·拦;
            #     ②题面没有的【数字】(编新年份/编号)→拦;
            #   其余题面外的【通用小写词】(同义改写/词形变化)一律放行。
            _orig_toks = re.findall(r"[A-Za-z0-9']+", s)   # 保留大小写判专名
            _bad_oov = False
            for _i, _tk in enumerate(_orig_toks):
                _tkl = _tk.lower()
                if _tkl in _qwords or len(_tkl) < 3:
                    continue
                # 题面没有的数字(如编造年份)→拦
                if _tk.isdigit():
                    _bad_oov = True
                    break
                # 题面没有的大写专名词(非句首·首字母大写)→疑似编造的新实体名→拦
                if _i > 0 and _tk[:1].isupper() and _tk[1:].islower():
                    _bad_oov = True
                    break
            if _bad_oov:
                continue
            toks = [t for t in re.findall(r"[a-z0-9]+", sl) if len(t) >= 3]
            if not toks:
                continue
            # query 短语判据: 关键词拼接·不是整句(词数 3-12·太长像整句撒网)
            if len(sl.split()) > 12:
                continue
            cands.append(s)
        # 去重保序
        seen, out = set(), []
        for c in cands:
            cl = c.lower()
            if cl not in seen:
                seen.add(cl)
                out.append(c)
        return out[:n]
    except Exception:
        return []


def rank_anchors(anchors):
    """锚点按区分度降序(最独特的先搜)。

    extract_anchors 已按【优先级顺序】产出(引号短语 > 专名序列 > 数字短语)·这里做
    【稳定排序】把它保住·同时把兜底信号(staged_retrieval 特异度)当次级键·不打乱主优先级。
    关键: 单个纯年份(如 "2016")区分度低(很多题都有年份)·压到最后·别当第一锚点。
    fail-safe → 原序返回(extract_anchors 的优先级顺序已足够)。"""
    try:
        anchors = list(anchors or [])
        order = {a: i for i, a in enumerate(anchors)}   # 保 extract 的原优先级

        def _key(a):
            al = str(a or "")
            # 纯数字(年份/编号)单独出现=低区分度·压到最后(penalty 大)
            bare_num = bool(re.fullmatch(r"\d{2,4}", al.strip()))
            penalty = 1000 if bare_num else 0
            return (penalty, order.get(a, 999))
        return sorted(anchors, key=_key)
    except Exception:
        return list(anchors or [])


def _mid_entity_from_evidence(evidence_blocks, anchor):
    """从定向搜结果里提取【第一个中间实体】当候选锚点(喂给 _cand_ent)。

    - 跳过题库泄漏页(含 Answer: 明文)——不从泄漏页抄。
    - 提取每块里的大写专名短语·取【与 anchor 不同】且【清洁】(过 anchor_hop._is_clean_anchor)
      的第一个当中间实体(anchor 本身是特征短语·我们要的是它指向的那个'实体名')。
    - fail-safe → ''(没提到就返回空·不硬编)。"""
    try:
        import anchor_hop as _ah
    except Exception:
        _ah = None
    try:
        al = str(anchor or "").lower().strip()
        for blk in (evidence_blocks or []):
            txt = blk.get("text") if isinstance(blk, dict) else blk
            txt = str(txt or "")
            if not txt:
                continue
            if is_leakage_page(txt):
                continue   # 题库泄漏页·跳过不抄
            # 抽大写专名短语(候选中间实体)
            #   ★2026-08-20 修脏提取(实证提出 "Lil Pump\n  SNIPPET"): ①\s+ 会跨换行·
            #   把下一行的结构标签连进专名→改 [ ]+ 只在同一行连词;②剔除证据块结构标签词
            #   (SNIPPET/QUOTE/ENTITY/SRC/DESC/AKA…种子证据的字段名·非实体)。
            for m in re.findall(r"\b([A-Z][a-zA-Z.]+(?:[ ]+[A-Z][a-zA-Z.]+){0,3})\b", txt):
                cand = m.strip()
                # 剔除结构标签残留(种子证据格式 ENTITY=/SRC=/QUOTE=/SNIPPET 等)——
                #   若专名末尾/开头连了全大写标签词·去掉它(通用·非题目特判)
                cand = re.sub(r"\b(SNIPPET|QUOTE|ENTITY|SRC|DESC|AKA|URL|SUMMARY|TITLE)\b",
                              "", cand).strip()
                cl = cand.lower()
                if not cl or cl == al:
                    continue
                # 锚点特征短语本身/其子串不算新实体
                if cl in al or al in cl:
                    continue
                # 清洁门(复用 anchor_hop)
                if _ah is not None and not _ah._is_clean_anchor(cand):
                    continue
                if len(cand) < 3 or len(cand) > 80:
                    continue
                return cand
    except Exception:
        pass
    return ""


def _pick_anchors(question, llm_call, *, max_anchors=3):
    """选锚点: 【LLM 优先】(语义判断独特约束组合)·LLM 不可用/提不出→【正则兜底】。
    返回 (anchors:list, via:str)。via ∈ {"llm","regex",""}。fail-safe → ([],"")。"""
    # ① LLM 优先(治正则提不出 'debut novel 3-episode miniseries' 这种独特约束组合)
    try:
        llm_anchors = llm_extract_anchors(question, llm_call, max_anchors=max_anchors)
    except Exception:
        llm_anchors = []
    if llm_anchors:
        return llm_anchors, "llm"
    # ② 正则兜底(LLM 不可用/异常/提不出时·保留原有正则逻辑·别删)
    try:
        reg = rank_anchors(extract_anchors(question, max_anchors=max_anchors))
    except Exception:
        reg = []
    return (reg, "regex") if reg else ([], "")


def first_anchor_seed(question, search_fn, *, max_anchors=3, llm_call=None):
    """第一锚点提取 + 定向锚搜 + 提中间实体。让 anchor_hop 有确证锚点可顺藤。

    锚点提取【LLM 优先·正则兜底】(2026-08-20·真实测正则提出 '2016 and' 垃圾锚点·
    锚不到实体; 独特约束组合如 'debut novel 3-episode miniseries wrote adaptation'
    是语义判断·正则做不好——铁律: 语义判断用 LLM 不用规则表)。

    参数:
      question: 题面(原文·grounding 源)。
      search_fn(query) -> list[evidence blocks]: 调用方现成搜索闭包(吃 budget/dedup·
        走 run_parallel_search / AnySearch 二手源)。
      llm_call(prompt)->str: LLM 纯文本闭包(从 session._llm_call 取·和 feature_to_entity
        一样)。传了就 LLM 提锚点·没传/异常自动回退正则。
    返回 {
      "anchor_feature": 用了哪个锚点特征做定向搜(独特约束组合·grounding),
      "mid_entity":     从定向搜结果提到的第一个中间实体(喂 _cand_ent·可空),
      "searched":       实际发了几个定向 query,
      "skipped_leak":   是否遇到并跳过了题库泄漏页,
      "anchor_via":     锚点来源(llm/regex·可观测·诊断用),
    }。fail-safe → 全空(退回原行为)。"""
    out = {"anchor_feature": "", "mid_entity": "", "searched": 0,
           "skipped_leak": False, "anchor_via": ""}
    if not _enabled() or search_fn is None:
        return out
    try:
        anchors, via = _pick_anchors(question, llm_call, max_anchors=max_anchors)
        out["anchor_via"] = via
        if not anchors:
            return out          # 提不出独特锚点→不定向搜(退回原撒网/其余机制)
        # 取最独特的锚点(1 个·别一次撒多个 query·那又变撒网)做定向搜
        top = anchors[0]
        out["anchor_feature"] = top
        try:
            ev = search_fn(top)
        except Exception:
            ev = None
        out["searched"] = 1
        if not ev:
            return out
        ev_list = ev if isinstance(ev, list) else [ev]
        # 泄漏页检测(报给调用方·也用于提实体时跳过)
        try:
            out["skipped_leak"] = any(
                is_leakage_page(b.get("text") if isinstance(b, dict) else b)
                for b in ev_list)
        except Exception:
            pass
        out["mid_entity"] = _mid_entity_from_evidence(ev_list, top)
    except Exception:
        pass
    return out
