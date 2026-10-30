# -*- coding: utf-8 -*-
"""anchor_hop · 锚点×缺口→下一跳正向串接检索(攻检索·治 B 类 gold 从没被搜到)

多专家诊断(攻检索专家 2/3): 错题大量是【B 类】——gold 实体【整个搜索过程都没被搜到】·
不是锁错了已搜到的·而是根本没顺藤摸到。机制只能"拒绝错的"救不了 B 类·必须让检索能
【顺藤】: 从【已确证的锚点实体】+【还缺的约束】生成【正向串接 query】(不是搜反例·是
正向补缺口)·发射·把新证据并回。

与 verify_as_search 的区别:
  - verify_as_search: 搜【反例】(推翻候选)——校验维度·怀疑现有候选。
  - anchor_hop:       搜【正向串接】(顺着确证锚点补缺口)——检索维度·扩展证据边界。
  两者互补: 一个防锁错·一个防漏搜。

设计铁律(专家方案):
  1. anchor 必来自【确证】(identity_states confirmed / Entity Bank top)·【不来自 LLM 脑补】。
  2. 发射复用调用方现成搜索闭包(吃现有 budget/dedup)·不新开循环。
  3. grounding 自检: 每步 query 的显著 token 必含 anchor(真串上了)·否则判 hypothesis 丢弃。
  4. 触发严格化: 只在 grounded_ratio<0.5(撒网征兆)且有确证锚点时接管·避免干扰正常顺链。
  5. 每题上限(复用调用方节流计数)防烧墙钟。纯生成 query + 调闭包·fail-safe。

env:
  MEYO_ANCHOR_HOP        =0 关(整体旁路)·默认开
  MEYO_ANCHOR_HOP_MAX    每题最多几跳(默认 2)
"""
import os
import re


def _enabled():
    return os.environ.get("MEYO_ANCHOR_HOP", "1") != "0"


def hop_max():
    try:
        return int(os.environ.get("MEYO_ANCHOR_HOP_MAX", "2") or 2)
    except Exception:
        return 2


# 缺口约束里抽关键词(去虚词)·给正向 query 用。通用启发·非题目特判。
_GAP_STOP = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "is",
    "was", "were", "are", "be", "been", "who", "what", "which", "that", "this",
    "with", "by", "from", "as", "it", "its", "his", "her", "their", "must",
    "should", "would", "有", "的", "了", "是", "在", "和", "与", "被", "个", "条",
    "约束", "满足", "题面", "关键", "证据",
}


def _gap_keywords(gap_text, max_kw=4):
    """从缺口约束文本抽关键词(专名/数字/长词)·顺藤 query 用。"""
    kws = []
    try:
        t = str(gap_text or "")
        # 引号短语整体保留
        for q in re.findall(r'"([^"]{2,50})"', t):
            kws.append(q.strip())
        # 显著数字(年份/编号)
        for m in re.findall(r"\d{3,}", t):
            kws.append(m)
        # 首字母大写专名短语
        for m in re.findall(r"\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,}){0,2})\b", t):
            if m.lower() not in _GAP_STOP:
                kws.append(m.strip())
        # 普通长词兜底
        for w in re.findall(r"[a-zA-Z一-鿿]{3,}", t):
            if w.lower() not in _GAP_STOP and w not in kws:
                kws.append(w)
    except Exception:
        pass
    # 去重保序
    _seen, _out = set(), []
    for k in kws:
        kl = k.lower()
        if kl and kl not in _seen:
            _seen.add(kl)
            _out.append(k)
    return _out[:max_kw]


def build_hop_queries(anchor_entity, gap_constraint_text, question):
    """锚点×缺口→正向串接 query(顺藤·非搜反例)。
    anchor 必须是确证实体(调用方保证)。返回 query 列表(已去重·上限内)。"""
    a = str(anchor_entity or "").strip()[:80]
    if not a or not _is_clean_anchor(a):
        return []   # 脏锚点(path_working/占位词)不生成顺藤 query
    kws = _gap_keywords(gap_constraint_text)
    kw = " ".join(kws[:3]).strip()
    qs = []
    if kw:
        # ① 锚点+缺口关键词直串(核心顺藤)
        qs.append(f"{a} {kw}")
        # ② 结构化列举意图(治"锚点已知·要从它的名单/成员/记录里定位目标")
        qs.append(f"{a} {kw} list roster record full name")
    else:
        # 缺口无关键词→用题面兜底(仍锚定 anchor)
        qtail = " ".join(_gap_keywords(question)[:3])
        if qtail:
            qs.append(f"{a} {qtail}")
    # 去重保序
    _seen, _out = set(), []
    for q in qs:
        if q.lower() not in _seen:
            _seen.add(q.lower())
            _out.append(q)
    return _out[: max(1, hop_max())]


def _query_grounded_on_anchor(query, anchor_entity):
    """grounding 自检:query 的显著 token 是否真含 anchor(串上了)。
    没串上=hypothesis·丢弃不发。通用启发·fail-safe。"""
    try:
        a = str(anchor_entity or "").lower().strip()
        ql = str(query or "").lower()
        if not a:
            return False
        # anchor 整体或其任一长词出现在 query 里即算串上
        if a in ql:
            return True
        for w in a.split():
            if len(w) >= 4 and w in ql:
                return True
        return False
    except Exception:
        return False


# 锚点污染 token(e2e 实证 path_working 类脏锚顺藤→烧空搜索)。系统内部标记/占位词·
#   绝不能当锚点顺藤。通用启发·非题目特判。
_DIRTY_ANCHOR = {
    "path_working", "path", "working", "unknown", "none", "null", "n/a", "na",
    "agentir", "entity", "vlm", "todo", "tbd", "placeholder", "example",
    "候选", "实体", "答案", "未知", "占位",
}


def _is_clean_anchor(anchor_entity):
    """锚点质量门:必须是【干净专名】才配当顺藤锚点(治 e2e 实证 path_working 脏锚)。
    拒: 系统标记词/占位词/纯符号/过短/无有效专名 token。通用·fail-safe。"""
    try:
        a = str(anchor_entity or "").strip()
        if len(a) < 3 or len(a) > 80:
            return False
        al = a.lower()
        # 命中脏锚词表(整体或任一 token)→拒
        if al in _DIRTY_ANCHOR:
            return False
        for w in re.split(r"[\s_]+", al):
            if w in _DIRTY_ANCHOR:
                return False
        # 含内部下划线标记(path_working 类)→拒
        if "_" in a:
            return False
        # 必须含至少一个像样的专名 token(≥3 字母的词·或大写开头·或中日韩字)
        if re.search(r"[A-Z][a-zA-Z]{2,}", a) or re.search(r"[a-zA-Z]{4,}", a) \
                or re.search(r"[一-鿿]{2,}", a):
            return True
        return False
    except Exception:
        return False


def extract_first_anchor_queries(question, max_q=3):
    """v13.97(2026-08-20·13道bc错题trace实证) · 【第一锚点提取】治首搜撒网锚不到实体。

    真根因(全部13错题走native·backend=native·anchor_hop组件齐全但触发不了):
      下游 anchor_hop_search 触发需 `_cand_ent`(确证锚点)·但引擎首搜用【整句题面】
      撒网(predict_then_search 吃整个 question)→描述型 query 搜不到任何中间实体→
      _cand 空/幻觉→_cand_ent 锚不到→anchor_hop 永不触发→撒网锁错(8/13 C 模式)。

    对症(人工实跑验证:Walibora 锚"no sell by date"一步命中·Emily Aston 锚
    "debut novel 3-episode miniseries author wrote"一步命中候选作家集):
      不搜整句·提取题面【最独特的约束组合】做首搜 query·先锚第一个中间实体。
      独特度优先级: ①引号短语(天然独特) ②专名序列 ③"最罕见约束的组合"(带数字/
      年份/具体名词的多约束叠加·非'an author'这种泛词)。

    通用·不针对任何题·不含答案。返回 query 列表(首搜用·锚到实体后交 anchor_hop 顺藤)。
    fail-safe: 异常返 []。env MEYO_FIRST_ANCHOR=0 时调用方可跳过。
    """
    try:
        q = str(question or "").strip()
        if len(q) < 10:
            return []
        out = []
        # ① 引号短语(最独特·天然可锚)——整体保留·单独成 query
        quoted = re.findall(r'"([^"]{3,60})"', q) + re.findall(r"[“”]([^“”]{3,60})[”“]", q)
        for ph in quoted[:2]:
            ph = ph.strip()
            if ph:
                out.append(ph)
        # ② 专名序列(连续大写词·如人名/机构/作品名)
        proper = re.findall(r"\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,}){1,3})\b", q)
        for pn in proper[:2]:
            if pn.lower() not in _GAP_STOP and pn not in out:
                out.append(pn.strip())
        # ③ 最罕见约束组合: 抽含【数字/年份/具体名词】的短语·组合成锚点 query
        #    (治无引号无专名的题·如"debut novel 3-episode miniseries"·独特在组合)
        #    用 _gap_keywords 抽题面关键词·取含数字或最长的几个拼一条组合 query
        kws = _gap_keywords(q, max_kw=8)
        # 优先含数字的关键词(年份/集数/数量=高特异)
        numeric = [k for k in kws if re.search(r"\d", k)]
        specific = [k for k in kws if k not in numeric and len(k) >= 4]
        combo = (numeric[:2] + specific[:3])
        if len(combo) >= 2:
            out.append(" ".join(combo[:4]))
        # 去重保序·去掉太泛的单词 query(<2 token 且非引号/专名的不单独发)
        _seen, _final = set(), []
        for x in out:
            xl = x.lower().strip()
            if xl and xl not in _seen and len(xl) >= 3:
                _seen.add(xl)
                _final.append(x.strip())
        return _final[:max_q]
    except Exception:
        return []


def anchor_hop_search(anchor_entity, gaps, question, search_fn, *,
                      hops_used=0):
    """顺藤检索: 对确证锚点 × 每个缺口约束·生成正向 query 并发射·并回新证据。
    search_fn(query)->list[evidence](调用方现成闭包·吃 budget/dedup)。
    返回 (新证据 list, 实际发射跳数)。fail-safe(search_fn 抛错→跳过该 query)。
    ★锚点质量门: 脏锚点(path_working/占位词)不顺藤(e2e 实证防烧空搜索)。"""
    found = []
    n_hop = 0
    if not _enabled() or search_fn is None:
        return found, 0
    if not _is_clean_anchor(anchor_entity):
        return found, 0   # 脏锚点·不顺藤(否则 path_working 类烧空搜索)
    a = str(anchor_entity or "").strip()
    if not a:
        return found, 0
    _cap = max(0, hop_max() - int(hops_used or 0))
    if _cap <= 0:
        return found, 0
    # 对每个缺口生成顺藤 query·grounding 自检后发射
    for gap in (gaps or [])[:_cap]:
        if n_hop >= _cap:
            break
        for q in build_hop_queries(a, gap, question):
            if n_hop >= _cap:
                break
            # grounding 硬判据:query 没真串上 anchor→丢弃(防脑补脱锚)
            if not _query_grounded_on_anchor(q, a):
                continue
            try:
                _ev = search_fn(q)
                if _ev:
                    found.extend(_ev if isinstance(_ev, list) else [_ev])
                n_hop += 1
            except Exception:
                continue
    return found, n_hop
