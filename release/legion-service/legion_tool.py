#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""legion_tool · 军团各家共享的「统一工具检索 + 执行」CLI(Footprint Ladder 第2档)

背景(用户 2026-08-27 武器库更新):
  军团工具箱有 800+ 件 skill/工具(toolbox.scan),但把 800 个 schema 全塞进每家
  的 prompt = 撑爆上下文 + 破坏 prompt 缓存(hermes AGENTS.md:19「缓存神圣」)。
  正确做法(hermes Footprint Ladder:CLI+skill 是第2档·zero model-tool footprint):
    → 给各家【一条命令】按需检索,而不是常驻全清单。
    → 各家(cc/codex/hermes… 都是独立 CLI 进程)在 LEGION_PROTOCOL 里读到这条命令,
      自己判断"要不要查、查什么"(context not control),查到再调。

三个子命令(各家在命令行直接用):
  legion_tool.py search "<关键词/能力>"   → 按能力/关键词检索 top-N 可用工具 + 怎么调
  legion_tool.py run <tool_id> <json>     → 真执行一件可运行工具(媒体/opencli/媒体CLI)
  legion_tool.py list [cap]               → 列能力分布 或 某能力下的工具

设计对齐纲领:
  - 给 context 不加 control:只回"有什么、怎么调",不替模型决定用不用
  - 复用已有:检索走 toolbox.scan_toolbox,执行走 media_tools/opencli_bridge,不重造
  - 如实报告:能跑的标 runnable,只读源码的标 reference(告诉它路径自己看)
"""
import hashlib
import json
import os
import re as _re
import subprocess as _sp
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _load_toolbox():
    import toolbox
    return toolbox.scan_toolbox()


# ── 通用检索:BM25(依据 Hermes tool_search·harness_baselines/hermes-agent/tools/tool_search.py:414)──
# 用户 2026-08-28 骂醒:之前这里堆了一大坨【关键词枚举/意图词表/中文2-gram/if加分】替模型猜意图,
# 是 CLAUDE.md 早已禁止的死路(不通用·有歧义·过拟合我挑的几个query)。洗掉,改用【通用信息检索
# 算法 BM25】+ name拆词(照 Hermes),不写任何意图词表。中文query↔英文工具名的语义匹配,不在父层
# 用词表硬解——那是【模型的强项】:父层用 BM25 通用初筛给候选,把候选清单(name+desc+来源)如实
# 交给调用的军团模型,让它自己判断最对口的(context not control·让军团自己查工具库)。
import math as _math

_TOKEN_RE = _re.compile(r"[A-Za-z0-9]+")


def _tokenize(text: str) -> list:
    """英文按词、连续中文按 2/3 字符 n-gram 切分；不维护任何领域词表。

    单汉字粒度会让“桌面点击自动化”和任何包含“自动化”的文档共享“自/动/化”，在 1900 件
    catalog 上把无关项抬到前排。连续 n-gram 保留局部语义，又不替模型解释用户意图。
    """
    if not text:
        return []
    toks = [m.lower() for m in _TOKEN_RE.findall(text)]
    for chunk in _re.findall(r"[一-鿿]+", text):
        for n in (2, 3):
            toks.extend(chunk[i:i + n] for i in range(max(0, len(chunk) - n + 1)))
    return toks


def _tool_search_text(item: dict) -> str:
    """一件工具的可检索文本:name(拆词)+ 描述 + 能力标签 + 来源(照 Hermes _entry_search_text:356)。
    name 的 _ . - : 拆成空格,让 BM25 能用查询词命中 search_github 这类组合名。"""
    name = str(item.get("name") or "")
    name_words = name.replace("_", " ").replace(".", " ").replace("-", " ").replace(":", " ")
    desc = str(item.get("description") or "")
    caps = " ".join(item.get("capability_tags") or [])
    src = str(item.get("source_harness") or "")
    return f"{name_words} {desc} {caps} {src}"


def _bm25_score(q_tokens: list, doc_tokens: list, avg_dl: float,
                doc_freq: dict, n_docs: int, k1: float = 1.5, b: float = 0.75) -> float:
    """标准 BM25(照 Hermes tool_search.py:414 内联实现·不加依赖)。"""
    if not doc_tokens:
        return 0.0
    dl = len(doc_tokens)
    doc_tf = {}
    for t in doc_tokens:
        doc_tf[t] = doc_tf.get(t, 0) + 1
    score = 0.0
    for qt in q_tokens:
        df = doc_freq.get(qt, 0)
        if df == 0:
            continue
        tf = doc_tf.get(qt, 0)
        if tf == 0:
            continue
        idf = _math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * dl / max(avg_dl, 1.0)))
        score += idf * norm
    return score


def _rank_bm25(items: list, query: str) -> list:
    """按标准 BM25 排任意 catalog 项；只排字面命中，不拿零分项冒充候选。"""
    q_tokens = _tokenize(query)
    if not q_tokens or not items:
        return []
    docs = [_tokenize(_tool_search_text(it)) for it in items]
    lengths = [len(d) for d in docs]
    avg_dl = sum(lengths) / max(len(lengths), 1)
    doc_freq = {}
    for doc in docs:
        for tok in set(doc):
            doc_freq[tok] = doc_freq.get(tok, 0) + 1
    n_docs = len(items)
    scored = []
    for it, doc in zip(items, docs):
        score = _bm25_score(q_tokens, doc, avg_dl, doc_freq, n_docs)
        if score > 0 and it.get("runnable"):
            score *= 1.15
        if it.get("internal_flow"):
            score *= 0.55
        if score > 0:
            scored.append((score, it))
    scored.sort(key=lambda pair: -pair[0])
    return scored


def _rank_catalog(items: list, query: str, text_fn, top: int) -> list:
    """用同一套 BM25 检索任意工具目录；没有字面证据就诚实返回空。"""
    q_tokens = _tokenize(query)
    if not q_tokens or not items or top <= 0:
        return []
    docs = [_tokenize(text_fn(item)) for item in items]
    avg_dl = sum(map(len, docs)) / max(len(docs), 1)
    doc_freq = {}
    for doc in docs:
        for token in set(doc):
            doc_freq[token] = doc_freq.get(token, 0) + 1
    ranked = [(_bm25_score(q_tokens, doc, avg_dl, doc_freq, len(items)), item)
              for item, doc in zip(items, docs)]
    ranked = [(score, item) for score, item in ranked if score > 0]
    ranked.sort(key=lambda pair: -pair[0])
    return [item for _, item in ranked[:top]]


def search(query: str, top: int = 8, event_cb=None) -> dict:
    """按能力/关键词检索工具箱并可发射候选事实；最终选用权仍在调用方。"""
    tb = _load_toolbox()
    items = tb.get("items", [])
    partial_catalog = bool(tb.get("partial") or tb.get("warming"))
    # 去重:同名+同源的 skill 常在 .agents/.claude 两处各扫一份 → 只留一条(runnable 优先)。
    dedup = {}
    for it in items:
        k = (str(it.get("name") or "").lower(), str(it.get("source_harness") or "").lower())
        prev = dedup.get(k)
        if prev is None or (it.get("runnable") and not prev.get("runnable")):
            dedup[k] = it
    # BM25 + 通用语义向量混排。语义层优先用多语言 embedding；环境没有模型时退字符 n-gram，
    # 仍不维护领域词表/翻译表。两路都只提供候选 context，不替 harness 决定用哪个。
    catalog = list(dedup.values())
    internal_ids = {str(item.get("id") or "") for item in catalog if item.get("internal_flow")}
    scored = _rank_bm25(catalog, query)
    lexical = {it["id"]: score for score, it in scored}
    semantic_backend = "none"
    semantic = {}
    try:
        import tool_semantic_index
        sem_scores, semantic_backend = tool_semantic_index.scores(catalog, query)
        semantic = {it["id"]: score for it, score in zip(catalog, sem_scores)}
    except Exception:
        pass
    lex_max = max(lexical.values(), default=0.0) or 1.0
    ranked = []
    combined_scores = {}
    # 字符 n-gram 不是跨语言语义：中英零交集时仍会因泛中文描述产生噪声。只有真正的多语言
    # embedding 才参与混排；否则诚实保留 BM25，交给 harness 读 manifest 后自行改写重搜。
    semantic_weight = 0.55 if semantic_backend == "multilingual_embedding" else 0.0
    lexical_weight = 1.0 - semantic_weight
    for item in catalog:
        score = (lexical_weight * lexical.get(item["id"], 0.0) / lex_max
                 + semantic_weight * max(0.0, semantic.get(item["id"], 0.0)))
        # 可执行性是目录里的真实元数据，不是意图规则。用户最终要的是能跑的工具；同等相关时
        # runnable 应明显优于“只能读源码”的 reference，否则 1900 个 reference 会把 9 个真入口淹没。
        if item.get("runnable"):
            score *= 1.35
        if item.get("internal_flow"):
            score *= 0.55
        ranked.append((score, item))
    ranked.sort(key=lambda pair: -pair[0])
    # 内部保留更宽的【已有检索证据】候选池，等会话学习用真实成功关系优化后再裁 top-N。
    # 旧版先裁 top-N 再重排，伙伴即便排第 top+1 也永远救不回来，无法优化供给集合。
    supply_pool = [item for score, item in ranked[:max(top * 3, top)] if score > 0]
    hits = supply_pool[:top]
    _has_cjk = any("一" <= ch <= "鿿" for ch in (query or ""))
    rewrites = []
    rewrite_source = "not_needed"
    # 没装多语言 embedding 时，中文逐字/字符相似不等于跨语言语义。让模型做它擅长的查询改写，
    # 再用同一 BM25 查真实 catalog；这给的是候选 context，不是父层词表替它选工具。
    if (_has_cjk and semantic_backend != "multilingual_embedding" and not partial_catalog
            and len(supply_pool) < max(2, top // 2)):
        try:
            import tool_query_rewrite
            rewritten = tool_query_rewrite.rewrite(query, timeout_s=2.0)
            rewrites = rewritten.get("queries") or []
            rewrite_source = rewritten.get("source") or "model"
            # Reciprocal-rank fusion：原查询与各个模型改写是多个独立“检索视角”，按名次合并，
            # 不比较不同 query 的原始 BM25 数值，也不让原查询的泛中文命中占满全部名额。
            fused = {}
            ranked_lists = [[item for _, item in scored[:top * 3]]]
            ranked_lists.extend([[item for _, item in _rank_bm25(catalog, rewritten_query)[:top * 3]]
                                 for rewritten_query in rewrites])
            for result_list in ranked_lists:
                for rank, item in enumerate(result_list, 1):
                    rec = fused.setdefault(item["id"], {"item": item, "score": 0.0})
                    rec["score"] += 1.0 / (20 + rank)
            fused_ranked = sorted(fused.values(), key=lambda x: -x["score"])
            # 保持结果多样：多数名额给总体相关候选，同时给最相关的 runnable 和 auto 原生能力
            # 各留一个入口，避免 1600 个 reference 把可执行工具/auto 能力淹没。这里只按来源事实分槽，
            # 不按用户意图猜哪件工具正确，最终选择权仍在 harness。
            selected = fused_ranked[:max(0, top - 2)]
            selected_ids = {rec["item"]["id"] for rec in selected}
            for predicate in (
                lambda item: bool(item.get("runnable")),
                lambda item: str(item.get("id") or "").startswith("auto_tool:"),
            ):
                rec = next((x for x in fused_ranked
                            if x["item"]["id"] not in selected_ids and predicate(x["item"])), None)
                if rec:
                    selected.append(rec)
                    selected_ids.add(rec["item"]["id"])
            selected.extend(x for x in fused_ranked if x["item"]["id"] not in selected_ids)
            supply_pool = [rec["item"] for rec in selected[:max(top * 3, top)]]
            hits = supply_pool[:top]
            semantic = {rec["item"]["id"]: rec["score"] for rec in fused_ranked}
        except Exception:
            rewrite_source = "failed"
    elif _has_cjk and partial_catalog:
        # 冷启动已有 300+ 核心武器可用；不在首个用户请求里再同步等网关改写。
        # 完整索引后台落盘后，下次检索自动恢复模型改写与全量函数增强。
        rewrite_source = "deferred_index_warming"
    fallback_semantic = False
    # 模型改写不可用/超时时，通用字符向量只作为“避免交白卷”的最后候选源；仅收正分项，
    # 不随机塞零相关工具，也不覆盖已有 BM25 候选。
    if not supply_pool and semantic_backend == "character_ngram_fallback":
        semantic_ranked = sorted(
            ((score, item) for item, score in zip(catalog, sem_scores) if score > 0),
            key=lambda pair: -pair[0])
        supply_pool = [item for _score, item in semantic_ranked[:max(top * 3, top)]]
        hits = supply_pool[:top]
        fallback_semantic = bool(supply_pool)
    retrieval_scores = semantic if ((rewrites and semantic_backend != "multilingual_embedding")
                                    or fallback_semantic) else {
        item["id"]: (lexical_weight * lexical.get(item["id"], 0.0) / lex_max
                     + semantic_weight * max(0.0, semantic.get(item["id"], 0.0)))
        for item in catalog
    }
    out = []
    for it in supply_pool:
        entry = {
            "id": it["id"],
            "name": it.get("name"),
            "from": it.get("source_harness"),
            "caps": it.get("capability_tags"),
            "desc": (it.get("description") or "")[:160],
            "runnable": it.get("runnable", False),
            "execution": {
                "platforms": it.get("platforms") or [sys.platform],
                "dependencies": it.get("dependencies") or [],
                "credentials": it.get("credentials") or [],
                "input_schema": it.get("input_schema") or
                                {str(p): {"type": "string"} for p in (it.get("params") or [])},
                "installed": (None if "installed" in it and it["installed"] is None
                              else bool(it.get("installed", it.get("runnable", False)))),
                "health": it.get("health") or (
                    "ready" if it.get("runnable") else
                    "unknown" if it.get("runnable", False) is None else "reference_only"),
                "health_checked_at": it.get("health_checked_at"),
                **{key: it[key] for key in ("execution_ready", "execution_role", "reason")
                   if key in it},
            },
            "retrieval_score": round(float(retrieval_scores.get(it.get("id"), 0.0)), 6),
        }
        if it.get("runnable"):
            entry["runnable"] = True
            entry["how"] = f'legion_tool.py run {it["id"]} \'{{"...params"}}\''
            entry["params"] = it.get("params")
        elif entry["runnable"] is None:
            if it.get("kind") == "agent_tool":
                entry["how"] = (
                    "就绪未知，未验证可用；可显式尝试 "
                    f'legion_tool.py run {it["id"]} \'{{"...params"}}\''
                    "（沿用原授权和成员执行入口，不保证成功）")
                entry["params"] = it.get("params")
            else:
                entry["reference"] = it.get("path")
                entry["how"] = f'就绪未知，未确认执行入口；查看 {it.get("path")} 的使用说明'
        else:
            # 只读源码/skill:给路径,让模型自己读了照做(context not control)
            entry["reference"] = it.get("path")
            entry["how"] = f'reference only — read {it.get("path")} and apply it yourself'
        out.append(entry)
    # 不再用 _MEDIA_CAP_ALIASES 关键词枚举把媒体工具强制置顶(用户指出:有歧义、不通用;
    # OCR→gen_image正是这种 special-case 造成)。媒体工具本就在 toolbox catalog 里,和其它
    # 工具一样走 BM25;候选由军团模型读 name/desc/caps 后自主选择。
    # D类:同时搜「军团装备总库」(按需装备)供模型参考。
    # 安装目录会加载完整 registry 并探环境；冷启动已有 search_github/install 等核心入口，
    # 先返回可执行候选，完整装备库随后台索引补齐，避免首个搜索额外等待数秒。
    arsenal_hits = [] if partial_catalog else _search_arsenal(query, top=15)
    # B类落地:已装MCP服务器也可被发现(浏览器/文件/搜索/github)·带接入命令
    mcp_hits = _search_mcp(query, top=15)
    # 中文和英文工具名可能字面零交集。这里不猜意图、不加词表：如实告诉 harness 检索边界，
    # 并给完整 catalog 路径，让它凭自己的语义理解改写后重搜或直接检查清单。
    base_hint = ("以下是有字面检索证据的候选(非结论)·读描述自己判断最对口的;"
                 "runnable=true 是当前可执行候选；unknown_candidates 就绪未知，"
                 "其中已声明成员工具可按 how 显式尝试，不代表 ready；"
                 "reference 必须先读说明/按其宿主方式使用;"
                 "arsenal=可安装装备·mcp_servers=MCP。拿不准可查完整 catalog manifest 或 auto 源码。")
    if _has_cjk:
        base_hint += ("  【重要】工具名/描述多为英文,BM25 是字面初筛——若下面候选读着都不对口,"
                      "别勉强凑合选:中文没命中【不代表库里没有】,用你自己的理解把需求改写成一个/"
                      "多个英文能力短语再 search 一次(这一步最能救回对口工具),或走 next_steps。")
    # ★全仓公开函数/工具索引(用户2026-08-30:全部源码·全部公开函数被发现和使用)。
    # toolbox 是已接通/声明的工具；func_index 是当前可见源码中的候选函数。
    # 两者都交给军团，但源码符号不是原生工具名，静态命中也不是调用成功。
    harness_functions = []
    func_index_total = 0
    function_index_warming = False
    try:
        if partial_catalog:
            function_index_warming = True
            raise RuntimeError("function index deferred while toolbox warms")
        import func_index
        _func_mem = bool((getattr(func_index, "_BUILD_MEM", {}) or {}).get("data"))
        _func_path = str(getattr(func_index, "_CACHE", "") or "")
        _func_disk = bool(_func_path and os.path.isfile(_func_path)
                          and time.time() - os.path.getmtime(_func_path) < 6 * 3600)
        if not (_func_mem or _func_disk):
            function_index_warming = True
            raise RuntimeError("function index warming")
        _fi = func_index.search(query, top=12)
        func_index_total = _fi.get("total_indexed", 0)
        harness_functions = [{
            **e, "how": e["use"],
        } for e in _fi.get("results", [])]
    except Exception:
        pass
    # 武器库各目录共享同一会话学习器，但各自在自己的基础相关候选池中排序，不粗暴比较不同
    # 检索器的原始分数。主 tools 之外，MCP / 可安装装备 / 全 harness 源码函数也能受益。
    arsenal_hits = _learned_supply(arsenal_hits, query, 5)
    mcp_hits = _learned_supply(mcp_hits, query, 5, id_key="name")
    _function_rows = [{**entry,
                       "weapon_id": f"function:{entry.get('family')}:{entry.get('qual')}",
                       "retrieval_score": 1.0 / (21 + rank)}
                      for rank, entry in enumerate(harness_functions)]
    harness_functions = _learned_supply(_function_rows, query, 12, id_key="weapon_id")
    # 场景+工具伙伴优化(2026-09-03):在较宽但全部已有检索证据的候选池内，用过去查询成功史
    # 与最近成功工具的相邻伙伴关系轻微重排，再裁成最终 top-N。这样弱候选可退出供给、相关伙伴
    # 可从 top-N 边缘进入，但零相关工具绝不会凭关系硬塞。最终仍由 harness 自己选择和执行。
    ranking_context = {"active": False, "pool": len(out), "returned": min(top, len(out))}
    try:
        import legion_tool_usage as _ltu
        before_ids = [entry.get("id") for entry in out[:top]]
        signals = _ltu.ranking_context(out, query)
        out = _ltu.boost_ranking(out, query)
        final_out = out[:top]
        after_ids = [entry.get("id") for entry in final_out]
        _ltu.record_query(query, final_out)
        ranking_context = {"active": before_ids != after_ids, "pool": len(out),
                           "returned": len(final_out),
                           "changed": len(set(before_ids) ^ set(after_ids)) // 2,
                           "query_matches": signals.get("query_matches", 0),
                           "partner_matches": signals.get("partner_matches", 0),
                           "recent_tool": signals.get("recent_tool", "")}
        if ranking_context["active"]:
            ranking_context["message"] = (
                f"从 {len(out)} 件相关武器中精选 {len(final_out)} 件；"
                f"复用了 {ranking_context['query_matches']} 条同场景经验和 "
                f"{ranking_context['partner_matches']} 条成功工具链。")
        out = final_out
    except Exception:
        out = out[:top]
    # internal_flow 是运行时内部胶水，不是面向用户的首选能力。学习器可以在同类候选间换位，
    # 但不能让内部步骤占据第一入口；只把已有正分候选中的最佳用户能力提到首位，不删除任何
    # 候选、不按 query 猜意图，明确搜索内部工具名时它仍会留在紧邻位置供 harness 自主采用。
    if out and str(out[0].get("id") or "") in internal_ids:
        first_user_facing = next(
            (index for index, entry in enumerate(out[1:], 1)
             if str(entry.get("id") or "") not in internal_ids),
            None,
        )
        if first_user_facing is not None:
            out.insert(0, out.pop(first_user_facing))
    # 分类列表必须从【最终供给】投影，防结构化消费者看到两套互相冲突的顺序。
    runnable_candidates = [entry for entry in out if entry.get("runnable") is True]
    reference_candidates = [entry for entry in out if entry.get("runnable") is False]
    unknown_candidates = [entry for entry in out if entry.get("runnable") is None]
    result = {
        "query": query,
        "total_in_toolbox": tb["summary"]["total"],
        "total_harness_functions": func_index_total,
        "returned": len(out),
        "match": "candidates" if (out or arsenal_hits or mcp_hits or harness_functions) else "none",
        "retrieval": (semantic_backend if semantic_weight else
                      "model_rewrite+bm25" if rewrites else
                      "character_ngram_fallback" if fallback_semantic else "bm25"),
        "rewritten_queries": rewrites,
        "rewrite_source": rewrite_source,
        "ranking_context": ranking_context,
        "warming": partial_catalog or function_index_warming,
        "partial": partial_catalog,
        "capability_declarations_current": tb.get("capability_declarations_current"),
        "capability_declaration_error": tb.get("capability_declaration_error"),
        "capability_declarations_ref": tb.get("capability_declarations_ref"),
        "deferred_enrichments": (["full_toolbox", "arsenal_status"] if partial_catalog else [])
                                + (["function_index"] if function_index_warming else [])
                                + (["query_rewrite"] if rewrite_source == "deferred_index_warming" else []),
        "tools": out,
        # 就绪未知不等于源码参考，也不等于 ready。三栏均来自最终供给，
        # 保留 tools 兼容现有客户端；显式尝试仍由原执行入口处理。
        "runnable_candidates": runnable_candidates,
        "reference_candidates": reference_candidates,
        "unknown_candidates": unknown_candidates,
        "arsenal": arsenal_hits,
        "mcp_servers": mcp_hits,
        # 全仓公开函数/工具命中(tier=tool 一等声明工具·tier=function 二等公开函数·全部可发现)
        "harness_functions": harness_functions,
        # 完整清单是模型做跨语言语义选择所需的 context。CLI 输出不内联 1900 项，避免撑爆
        # prompt；给稳定 manifest 路径，harness 可按需读取/分片，再用自己理解重写 query。
        "catalog_manifest": getattr(__import__("toolbox"), "_CACHE", None),
        "hint": base_hint,
    }
    # context not control:候选是不是"真对口"由军团模型判断,不由父层用阈值 gate(那又回到
    # 有歧义的枚举打分)。所以【始终】附上可执行的降级出路——搜 GitHub 找现成轮子 / 派军团深度
    # 产出。候选合适就用候选,不合适就走出路;这两条对任何 query 都成立,给了不损失什么。
    # 英文关键词更利于 GitHub 搜索(照 auto search_github 的用法建议)
    en_terms = _re.findall(r"[a-z0-9]+", query.lower())
    gh_q = " ".join(en_terms[:4]) if en_terms else query
    result["no_ready_tool_next_steps"] = [
        {"option": "候选都不对口?搜 GitHub 找现成轮子/模板",
         "how": f'legion_tool.py run auto_tool:search_github query="{gh_q} ..."',
         "note": "很多任务网上有别人写好的库/模板,搜到用 install_github_repo 装下来,别从零手写。"},
        {"option": "专业知识任务(BP/合规/竞品调研):直接派军团深度产出",
         "how": "把任务交给军团 dispatch — 各家会自己搜资料/调工具/产出真交付物(PDF/Excel/网页…)",
         "note": "这类没有'一个工具'能解,靠军团调研+产出。"},
    ]
    candidates = []
    for rank, entry in enumerate([*out, *arsenal_hits, *mcp_hits, *harness_functions], 1):
        weapon_id = str(entry.get("id") or entry.get("weapon_id") or entry.get("name") or "")
        if not weapon_id:
            continue
        candidates.append({
            "id": weapon_id, "name": entry.get("name") or entry.get("qual") or weapon_id,
            "kind": ("source_function" if entry.get("qual") else
                     "mcp" if entry in mcp_hits else "arsenal" if entry in arsenal_hits else
                     "tool" if entry.get("runnable") else "skill_or_reference"),
            "source": entry.get("source_harness") or entry.get("family") or entry.get("source") or "",
            "runnable": entry.get("runnable"),
            "installed": (entry.get("execution") or {}).get("installed", entry.get("installed")),
            "rank": rank,
        })
    result["candidate_trace"] = {
        "query_hash": hashlib.sha256(str(query).encode("utf-8")).hexdigest()[:16],
        "pool": len(catalog) + len(arsenal_hits) + len(mcp_hits) + len(harness_functions),
        "returned": len(candidates), "candidates": candidates[:30],
        "ranking_context": ranking_context,
    }
    if event_cb:
        try:
            event_cb({"t": "weapon_candidates", "status": "ready",
                      **result["candidate_trace"],
                      "x": (ranking_context.get("message") or
                            f"发现 {len(candidates)} 件相关武器，交给执行成员自主选择")})
        except Exception:
            pass
    return result


def _search_mcp(query: str, top: int = 5) -> list:
    """用统一 BM25 搜 MCP 目录，不另造一套子串规则。"""
    path = os.path.join(HERE, "legion_mcp_servers.json")
    try:
        servers = json.load(open(path, encoding="utf-8")).get("servers", [])
    except Exception:
        return []
    hits = _rank_catalog(
        servers, query,
        lambda s: f"{s.get('name')} {s.get('cap')} {s.get('note')} {' '.join(s.get('command') or [])}",
        top=top,
    )
    out = []
    for s in hits:
        entry = {
            "name": s["name"], "cap": s.get("cap"),
            "installed": s.get("installed", False),
            "needs_key": s.get("needs_key", False),
            "add_cmd": s.get("add_cmd"),
        }
        if s.get("name") == "github":
            try:
                import github_access
                entry["authorization"] = github_access.status()
            except Exception:
                pass
        out.append(entry)
    return out


def _learned_supply(entries: list, query: str, top: int, id_key: str = "id") -> list:
    """对任意武器目录做同源关联供给；目录无可靠 id/分数或学习故障时保持基础顺序。"""
    if not entries:
        return []
    prepared = []
    for rank, entry in enumerate(entries):
        row = dict(entry)
        row.setdefault("retrieval_score", 1.0 / (20 + rank + 1))
        prepared.append(row)
    try:
        import legion_tool_usage as _ltu
        return _ltu.boost_ranking(prepared, query, id_key=id_key)[:top]
    except Exception:
        return prepared[:top]


def _search_arsenal(query: str, top: int = 5, probe_installed: bool = True) -> list:
    """搜军团装备总库(KM97+ecosystem)——让按需装备能被军团/用户发现。

    返回匹配项 + 安装状态 + 怎么装/纳入。这修 D类"必须能被发现"的缺口。
    """
    try:
        import install_registry as ir
    except Exception:
        return []
    # arsenal 与主工具目录共用同一通用检索，不再用空格切词+子串计分的第四套逻辑。
    items = ir.all_items()
    hits = _rank_catalog(
        items, query,
        lambda it: " ".join(str(it.get(k) or "") for k in
                            ("name", "note", "tier", "install_strategy", "source", "kind"))
                   + " " + " ".join(it.get("scenes") or []),
        top=top,
    )
    # 只对 top-N 命中项查安装状态(慢探测限定小范围)
    out = []
    for it in hits:
        installed = ir.is_installed(it) if probe_installed else None
        if installed is True:
            how = "已装·可直接用"
        elif it.get("install"):
            how = f'可一键装: install_registry.install("{it["id"]}", confirm=True)'
        else:
            how = f'需手动/纳入: {it.get("url") or it.get("note") or "见装备总库"}'
        out.append({
            "id": it["id"], "name": it.get("name"),
            "tier": it.get("tier"), "priority": it.get("priority"),
            "installed": installed, "install_status": "checking" if installed is None else "known",
            "how": how,
        })
    return out


def _run_search_github(query: str, limit: int = 5) -> dict:
    """军团级 GitHub 仓库搜索(把 auto 的 search_github 提升成任何一家都能直调的工具)。
    纯 stdlib 调 GitHub search API,无 session 依赖。返回现成仓库 + clone 地址 +
    "怎么装"指引,让军团碰到"没现成工具"时能自己搜个装上(context not control)。"""
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "search_github 需要 query(用简洁英文关键词,如 'pdf table extraction python')"}
    try:
        import urllib.request as _u, urllib.parse as _up
        limit = min(max(1, int(limit or 5)), 10)
        url = ("https://api.github.com/search/repositories?q=" + _up.quote(query)
               + f"&sort=stars&order=desc&per_page={limit}")
        import github_access
        auth = github_access.status()
        req = _u.Request(url, headers=github_access.request_headers())
        with _u.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        try:
            import github_access
            auth = github_access.status()
        except Exception:
            auth = {"authorized": False, "mode": "anonymous_public", "auth_command": "gh auth login"}
        return {"ok": False, "error": f"search_github 失败(GitHub限流/网络): {type(e).__name__}: {str(e)[:160]}",
                "github_auth": auth,
                "hint": "未授权时可让用户运行 gh auth login；也可稍后重试、改用包名直装或换搜索来源。"}
    items = data.get("items", []) or []
    repos = [{"full_name": it.get("full_name"), "stars": it.get("stargazers_count"),
              "language": it.get("language"), "clone_url": it.get("clone_url"),
              "desc": (it.get("description") or "")[:120]} for it in items]
    return {"ok": True, "query": query, "count": len(repos), "repos": repos,
            "github_auth": auth,
            "next": "选中后用 inspect_github_repo 只读查看固定 revision、README、根许可证和运行文档；"
                    "标签不代表依赖/素材已获许可，检查不等于运行验证。需要下载时再用 install_github_repo。"}


def _search_defaults() -> dict:
    """Optional deployment defaults beside the existing public model configuration."""
    path = os.path.join(HERE, "..", "config", "search.json")
    try:
        with open(path, encoding="utf-8") as stream:
            raw = stream.read(8193)
    except FileNotFoundError:
        return {}
    if len(raw) > 8192:
        raise ValueError("search configuration is too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("search configuration must be an object")
    source, alias = value.get("source", ""), value.get("friday_appid_alias", "")
    if (not isinstance(source, str) or not isinstance(alias, str)
            or any(c in source + alias for c in ("\r", "\n", "\0"))
            or (alias and not alias.startswith("appid-"))):
        raise ValueError("search defaults may contain a source and a credential alias, not a key")
    return {"source": source.strip(), "friday_appid_alias": alias.strip()}


def _run_web_search(query: str = "", queries: list | None = None, limit: int = 8,
                    mode: str = "fast", source: str = "") -> dict:
    """全军团统一实时搜索；复用 SearchFabric/Friday，不复制某家的模型工具外壳。"""
    requested = [str(q).strip() for q in (queries or [query]) if str(q).strip()]
    if not requested:
        return {"ok": False, "error": "web_search 需要 query 或非空 queries"}
    requested = requested[:4]  # 对齐 DSH web_search 的单次并发查询上限
    limit = min(max(1, int(limit or 8)), 20)
    mode = mode if mode in ("fast", "balanced", "deep") else "fast"
    if not source:
        source = os.environ.get("LEGION_WEB_SEARCH_DEFAULT_SOURCE", "").strip()
    try:
        defaults = _search_defaults() if not source or source in ("google", "google-search") else {}
    except (OSError, ValueError):
        return {"ok": False, "queries": requested, "count": 0, "results": [], "backends": [],
                "error": "搜索默认配置不可读或格式无效；配置只允许来源和计费别名，不允许密钥"}
    source = source or defaults.get("source", "")
    started = __import__("time").time()
    all_hits = []
    traces = []
    backends = []
    for current in requested:
        try:
            # Both retained providers use the same published source resolver and
            # managed child. Never import their path/state globals into a server.
            from media_tools import _bounded_media_process
            request = {"query": current, "top_k": limit, "mode": mode, "source": source}
            if source in ("google", "google-search") and defaults.get("friday_appid_alias"):
                request["friday_appid_alias"] = defaults["friday_appid_alias"]
            code, stdout = _bounded_media_process(
                [sys.executable, "-B", "-s", os.path.join(HERE, "search_fabric_worker.py")],
                _TOOL_DEADLINE_S,
                input_text=json.dumps(request, ensure_ascii=False))
            result = json.loads(stdout)
            if not isinstance(result, dict):
                raise TypeError("search_fabric worker response must be an object")
            if result.get("error_type"):
                traces.append(f"{current}:{result['error_type']}:{str(result.get('error') or '')[:160]}")
                continue
            if code:
                raise RuntimeError(f"search_fabric worker exited {code} without an error reply")
            hits, trace = result["hits"], result["trace"]
            if source in ("google", "google-search"):
                if not hits:
                    # Google 是明确指定的首选源；空结果时不伪装成功，也不把整单卡死。
                    # 用同一共享搜索底座自动补一次公开多源，并在返回中如实标出降级。
                    # GitHub repository search does not understand prose as well as web search;
                    # preserve user terms but remove year/roadmap filler without a domain lexicon.
                    repo_query = " ".join(token for token in current.split()
                                          if not token.isdigit() and token.lower() != "roadmap")
                    fallback = _run_search_github(repo_query or current, limit)
                    hits = [{"url": item.get("clone_url", "").removesuffix(".git"),
                             "title": item.get("full_name"), "content": item.get("desc"),
                             "source": "github"}
                            for item in fallback.get("repos", [])]
                    trace += ["google_empty:fallback_to_github"]
            for hit in hits or []:
                if hit.get("_error") or hit.get("_source_health"):
                    continue
                url = str(hit.get("url") or "")
                if not url or any(existing.get("url") == url for existing in all_hits):
                    continue
                backend = hit.get("source") or source or "search_fabric"
                backends.append(backend)
                all_hits.append({
                    "url": url, "title": hit.get("title") or url,
                    "snippet": hit.get("content") or hit.get("snippet") or "",
                    "source": backend, "published_at": (hit.get("meta") or {}).get("published_at"),
                    "query": current,
                })
                if len(all_hits) >= limit:
                    break
            traces.extend(trace)
        except Exception as exc:
            traces.append(f"{current}:{type(exc).__name__}:{str(exc)[:160]}")
        if len(all_hits) >= limit:
            break
    return {
        "ok": bool(all_hits), "queries": requested, "count": len(all_hits),
        "results": all_hits[:limit], "backends": list(dict.fromkeys(backends)),
        "elapsed_s": round(__import__("time").time() - started, 2),
        "trace": traces[-12:],
        "degraded_from": ("google" if "google_empty:fallback_to_github" in traces else None),
        "error": None if all_hits else "所有已选搜索源均未返回有效结果；可换 source/查询或稍后重试。",
    }


def _capability_catalog_module():
    import capability_catalog
    return capability_catalog


def _run_capability_catalog() -> dict:
    """List the default discovery entry points shared by every legion member."""
    return _capability_catalog_module().inventory()


def _run_skill_finder(query: str = "", agent_type: str = "legion",
                      timeout_s: int = 120, **params) -> dict:
    """军团共享 Skill 工厂：多源搜索、审计、隔离安装、同步和效果反馈。"""
    import skill_factory
    action = str(params.get("action") or "search")
    uid = str(params.get("uid") or os.environ.get("MEYO_USER_ID") or "default")
    if action == "list":
        return skill_factory.list_active(uid)
    if action == "install_local":
        return skill_factory.install_local(str(params.get("source") or ""), str(params.get("name") or ""),
                                           uid, str(params.get("revision") or "local"),
                                           bool(params.get("confirm")))
    if action == "feedback":
        return skill_factory.feedback(str(params.get("name") or ""), str(params.get("task") or ""),
                                      str(params.get("result") or ""), str(params.get("agent") or agent_type), uid)
    if not query.strip():
        return {"ok": False, "error": "skill_finder 需要 query"}
    return skill_factory.search(query, uid, int(params.get("limit") or 8),
                                str(params.get("scope") or "local"))


def _research_pipeline_module():
    import research_pipeline
    return research_pipeline


def _run_research_capabilities() -> dict:
    return _research_pipeline_module().capabilities()


def _run_research_stage(stage: str, params: dict) -> dict:
    pipeline = _research_pipeline_module()
    handlers = {
        "search": pipeline.search,
        "fetch": pipeline.fetch,
        "extract": pipeline.extract,
        "compress": pipeline.compress,
        "graph": pipeline.graph,
        "document": pipeline.document,
        "media": pipeline.media,
    }
    handler = handlers.get(stage)
    if not handler:
        return {"ok": False, "error": f"unknown research stage: {stage}",
                "available": sorted(handlers)}
    try:
        return handler(**params)
    except TypeError as exc:
        return {"ok": False, "stage": stage, "error": f"参数错误: {exc}"}


def _search_sources_module():
    import search_sources
    return search_sources


def _run_search_sources() -> dict:
    """Expose the complete search inventory with current dedicated-source health."""
    return _search_sources_module().inventory()


def _run_search_source_probe(source: str = "", query: str = "", limit: int = 3,
                             timeout_s: int = 120) -> dict:
    """Probe one platform through its supported dedicated or aggregate production path."""
    return _search_sources_module().probe(source, query, limit=limit, timeout=timeout_s)


def _run_web_fetch(url: str, timeout_s: int = 25) -> dict:
    """全军团共享网页抓取；返回真实后端与结构化错误。"""
    try:
        import media_tools
        result = media_tools.fetch_url(url, timeout_s=timeout_s)
        return {**result, "backend": "legion.fetch_url"}
    except Exception as exc:
        return {"ok": False, "url": url, "backend": "legion.fetch_url",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


def _mcp_bridge_module():
    root = os.path.abspath(os.path.join(HERE, "..", ".."))
    bridge_dir = os.path.join(root, "harness_baselines", "meyo-orchestrator", "meyo_harnesses")
    if not os.path.isfile(os.path.join(bridge_dir, "mcp_bridge.py")):
        bridge_dir = os.path.join(root, "legion", "orchestrator", "meyo_harnesses")
    if not os.path.isfile(os.path.join(bridge_dir, "mcp_bridge.py")):
        raise FileNotFoundError("MCP bridge missing from development/release layout")
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)
    import mcp_bridge
    return mcp_bridge


def _bound_capability_session():
    """Use the native host's same grants; never take identity/config from arguments."""
    import agent_tool_provider as provider
    from native_capability_host import CapabilitySession
    _, env, cancel = provider._runtime_binding()
    allowed = provider._json_env(env, "MEYO_NATIVE_CAPABILITY_IDS", list)
    if not allowed:
        raise ValueError("meyo_capabilities requires explicit parent grants")
    context = {
        "user_id": env.get("MEYO_USER_ID"), "session_id": env.get("MEYO_SESSION_ID"),
        "workdir": env.get("MEYO_WORKDIR"), "allowed_ids": allowed,
        "task_facts": provider._json_env(env, "MEYO_TASK_FACTS_JSON"),
        "project_context": provider._json_env(env, "MEYO_PROJECT_CONTEXT_JSON"),
        "capability_policy": provider._json_env(env, "MEYO_CAPABILITY_POLICY_JSON"),
    }
    return CapabilitySession(context, environment=env), env, cancel


def _run_mcp_list() -> dict:
    """按需列出军团共享 MCP 工具；schema 不常驻各家 prompt。"""
    try:
        tools = list(_mcp_bridge_module().list_mcp_tools())
        import agent_tool_provider as provider
        _, env, _ = provider._runtime_binding()
        if provider._json_env(env, "MEYO_NATIVE_CAPABILITY_IDS", list):
            session, _, _ = _bound_capability_session()
            tools.extend({"server": "meyo_capabilities", "name": name,
                          "description": description, "input_schema": schema}
                         for name, description, schema, _ in session.tools)
        servers = sorted({str(item.get("server")) for item in tools})
        errors = [item for item in tools if item.get("error")]
        return {"ok": not errors, "servers": servers, "tool_count": len(tools) - len(errors),
                "errors": errors, "tools": tools}
    except Exception as exc:
        return {"ok": False, "error": f"MCP列表失败:{type(exc).__name__}:{str(exc)[:200]}"}


def _run_mcp_call(server: str, name: str, arguments: dict | None = None) -> dict:
    """调用任意已声明 MCP；具体工具由 harness 自己从 mcp_list 结果中选择。"""
    if not server or not name:
        return {"ok": False, "error": "mcp_call 需要 server 和 name；先运行 mcp_list 查看真实工具"}
    if server == "meyo_capabilities":
        try:
            session, _, cancel = _bound_capability_session()
            declared = {row[0]: row for row in session.tools}
            if name not in declared:
                raise ValueError("tool is not declared by the parent capability host")
            from native_capability_host import _schema_value
            args = {} if arguments is None else arguments
            _schema_value(args, declared[name][2])
            with session.cancellation_scope(cancel):
                result = declared[name][3](args)
            ok = result.get("ok", True) is True
            return {"ok": ok, "status": "ok" if ok else "error",
                    "server": server, "tool": name, "result": result}
        except Exception as exc:
            return {"ok": False, "status": "error", "server": server, "tool": name,
                    "error": f"{type(exc).__name__}:{str(exc)[:200]}"}
    out = _mcp_bridge_module().call_mcp_tool(server, name, arguments or {})
    return {"ok": out.get("status") == "ok", **out}


def _safe_repo_name(full_name: str) -> str:
    return _re.sub(r"[^A-Za-z0-9_.-]+", "-", full_name.strip()).strip("-.")[:120]


def _github_clone_plan(clone_url: str, full_name: str = "") -> dict:
    """纯计划：不访问网络/凭证，不创建目录，不执行仓库脚本。"""
    import urllib.parse as _up
    raw = str(clone_url or "")
    try:
        parsed = _up.urlsplit(raw.strip())
        allowed = (parsed.scheme == "https" and parsed.hostname in ("github.com", "www.github.com")
                   and parsed.port in (None, 443) and parsed.username is None
                   and parsed.password is None and not parsed.query and not parsed.fragment
                   and not any(ord(c) < 32 for c in raw))
    except ValueError:
        allowed = False
    if not allowed:
        return {"ok": False, "status": "failed",
                "error": "只接受无凭证、无查询参数的 https://github.com/owner/repo 地址"}
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if len(parts) != 2 or any(p in (".", "..") or not _re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts):
        return {"ok": False, "status": "failed", "error": "GitHub 地址必须是 owner/repo"}
    repo_name = _safe_repo_name(full_name or "/".join(parts))
    if not repo_name:
        return {"ok": False, "status": "failed", "error": "无法生成安全仓库目录名"}
    import legion_paths as _paths
    destination = os.path.join(_paths.repos_dir(), repo_name)
    return {
        "ok": True,
        "status": "planned",
        "source": f"https://github.com/{parts[0]}/{parts[1]}.git",
        "destination": destination,
        "command": ["git", "clone", "--depth", "1",
                    f"https://github.com/{parts[0]}/{parts[1]}.git", destination],
        "requires_confirmation": True,
        "next": "确认后调用 install_github_repo；clone 后还要读 README/依赖并做 health check，不能把下载成功冒充工具可用。",
    }


def _inspect_github_repo(clone_url: str, full_name: str = "", revision: str = "",
                         timeout_s: float = 20, plan_only: bool = False) -> dict:
    """只读 GitHub metadata/固定commit/README/根许可证/运行文档；不克隆、不执行、不判质量。"""
    plan = _github_clone_plan(clone_url, full_name)
    if not plan.get("ok") or plan_only:
        return plan
    import github_repo_inspection
    repository = plan["source"].removeprefix("https://github.com/").removesuffix(".git")
    facts = github_repo_inspection.inspect(repository, revision, timeout_s)
    return {**plan, "runtime_verified": False, "cloned": False, "commands_executed": False,
            "sources": [], **facts, "requested_url": str(clone_url).strip(),
            "clone_plan": plan, "requires_confirmation": False}


def _install_github_repo(clone_url: str, full_name: str = "", confirm: bool | None = None,
                         timeout_s: int = 180, capability_policy: dict | None = None) -> dict:
    """浅克隆到军团共享 repos；产品军团默认允许，显式 false 仍只预览。"""
    plan = _github_clone_plan(clone_url, full_name)
    if confirm is None:
        try:
            from legion_permissions import capability_policy as _default_policy
            confirm = bool((capability_policy or _default_policy()).get("install"))
        except Exception:
            confirm = False
    if not plan.get("ok") or not confirm:
        return plan
    destination = plan["destination"]
    if os.path.isdir(destination):
        return {**plan, "status": "already_installed", "installed": True}
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        import github_access
        proc = _sp.run(plan["command"], capture_output=True, text=True, timeout=timeout_s,
                       env=github_access.git_environment())
    except Exception as exc:
        return {**plan, "ok": False, "status": "failed",
                "error": f"{type(exc).__name__}:{exc}"}
    exists = os.path.isdir(destination)
    return {**plan, "ok": proc.returncode == 0 and exists,
            "status": "installed" if proc.returncode == 0 and exists else "failed",
            "installed": exists, "exit_code": proc.returncode,
            "stdout": (proc.stdout or "")[-1200:], "stderr": (proc.stderr or "")[-1200:]}


# 工具级 deadline(军团自己设计的持续化项②·context not control):父层自己发起的工具调用
# (media/opencli/search_github)加一层兜底 timeout + 把超时/失败归一成【结构化 TOOL_TIMEOUT】,
# 让调用方(军团/harness)能识别"这是工具超时·该换招"而不是把英文报错当答案。
# 依据 deepseek timeout-policy(README.zh.md:20-32):工具超时/失败返回结构化
#   {isError:true, content:"Error: tool call timed out after <ms>ms"} 原样喂回模型,模型自己换招。
# 只管【父层无状态工具】的 deadline;各家 harness 进程内部工具的 deadline 是它们自己的事
# (替它管=control)——那部分只通过 env/prelude 下放建议值(见 tool_timeout_hint)。
_TOOL_DEADLINE_S = float(os.environ.get("LEGION_TOOL_TIMEOUT", "180"))
# 结构化事件回流协议(与 harness_worker._EV_PREFIX / harness_registry 一致):CLI run 模式把
# tool_started/end/timeout 写 stderr 带此前缀,worker 透传→legion_core解析→SSE→前端。
_EV_PREFIX = "\x1e[legion-ev]"


def tool_timeout_hint() -> int:
    """给各家 harness 的工具级 deadline 建议值(秒·context)。供 prelude/env 下放·harness 自主用。"""
    return int(_TOOL_DEADLINE_S)


def _brief(v, n: int = 80) -> str:
    """参数/结果摘要(过程事件用·不吐全量·防撑爆)。"""
    try:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    except Exception:
        s = str(v)
    s = " ".join(str(s).split())
    return s[:n] + ("…" if len(s) > n else "")


def _tool_source_ref(tool_id: str) -> dict:
    """Resolve a selected weapon back to its declared Skill, repository, or native source."""
    try:
        import toolbox
        snapshot = toolbox.cached_toolbox() or {}
        item = next((row for row in snapshot.get("items") or [] if row.get("id") == tool_id), {})
    except Exception:
        item = {}
    kind = "tool"
    if str(tool_id).startswith("auto_tool:search_github"):
        kind = "github_search"
    elif str(tool_id).startswith("auto_tool:install_github_repo"):
        kind = "github_install"
    elif str(tool_id).startswith("function:") or str(tool_id) in {"func_call", "runnable:func_call"}:
        kind = "source_function"
    elif item.get("kind") == "skill":
        kind = "skill"
    return {key: value for key, value in {
        "id": tool_id, "name": item.get("name") or tool_id, "kind": kind,
        "path": item.get("path"), "repository": item.get("url") or item.get("repository"),
        "source_harness": item.get("source_harness"),
    }.items() if value not in (None, "")}


def _record_tool_usage(tool_id: str, ok: bool) -> dict:
    """场景学习的故障隔离边界；返回可审计归因但绝不改变工具执行结果。"""
    try:
        import legion_tool_usage as _ltu
        return _ltu.record_run(tool_id, ok)
    except Exception:
        return {"recorded": False, "candidate_matched": False, "query_age_s": None}


def run_tool(tool_id: str, params: dict, timeout_s: float | None = None,
             event_cb=None) -> dict:
    """真执行一件可运行工具·带工具级 deadline(项②)+ 结构化生命周期事件(项③)。

    ②在 _run_tool_inner 外包一层兜底 timeout:超时返回结构化 TOOL_TIMEOUT(仿 deepseek·可被调用方
    识别为"该换招的工具失败"·不是答案)。工具内部若已有自己的 timeout(media gen_image 90s)会先
    命中·此处是【线程级兜底】+【结构化归一】。

    ③event_cb(可选):父层自己发起的工具调用有【明确的生命周期】(不是猜·父层就是发起方),
    发结构化三件套 tool_started / tool_end / tool_timeout(带 call_id 关联)。军团反思共识:
    tool_start 未 tool_end 是"最强在推进信号"(比 stdout/CPU 更可靠判在干活 vs 卡死),前端也能
    据此画工具时间线。每个事件同时带人话 x 字段·让现有前端 step 渲染器不改也能显示(绕开前端归属)。
    只发【父层无状态工具】的生命周期·不猜各家 harness 进程内部工具(那是它们自己的事·context not control)。
    """
    import concurrent.futures as _cf
    import time as _t
    from contextlib import nullcontext
    params = dict(params or {})
    _model_tool = tool_id.removeprefix("runnable:")
    if _model_tool in ("model_catalog", "model_chat", "model_decisions"):
        # 在事件、权限账本或进程参数之前拒绝凭证/网关覆盖，避免误传值被日志回显。
        import media_model_cli
        try:
            media_model_cli.validate_model_tool(
                _model_tool, {k: v for k, v in params.items()
                              if k not in ("_capability_policy", "_idempotency_key")})
        except media_model_cli.MediaError as exc:
            return {"ok": False, "isError": True, "error": str(exc)}
    _agent_call = None
    # 授权前只保存原参数，不加载可选 orchestrator；保留尚未 pop 的权限字段。
    _agent_params = dict(params) if tool_id.startswith("agent_tool:") else None
    if tool_id.removeprefix("auto_tool:").removeprefix("runnable:") in {
            "inspect_github_repo", "install_github_repo"}:
        # Reject URL credentials before lifecycle events or install ledgers can echo them.
        _plan = _github_clone_plan(params.get("clone_url") or params.get("url") or "",
                                  params.get("full_name") or "")
        if not _plan.get("ok"):
            return _plan
    try:
        import legion_permissions as _permissions
        _policy = params.pop("_capability_policy", None)
        _runtime_snapshot = None
        _registry = sys.modules.get("harness_registry")
        _runtime_local = getattr(_registry, "_INPROC_RUNTIME_ENV", None)
        _bound_context = getattr(_runtime_local, "values", None)
        _bound_cancel = getattr(getattr(_registry, "_INPROC_CANCEL_CHECK", None), "check", None)
        if (_agent_params is not None or _bound_context is not None or callable(_bound_cancel)
                or hasattr(_runtime_local, "inherit_process_context") or _policy is not None
                or "MEYO_CAPABILITY_POLICY_JSON" in os.environ):
            import agent_tool_provider as _provider
            _registry, _env, _cancel = _provider._runtime_binding()
            _runtime_snapshot = (
                dict(_env), _cancel,
                getattr(_registry._INPROC_RUNTIME_ENV, "inherit_process_context", True))
            # 先收窄可信父策略，再授权。空且不继承的 host scope 不借进程权限；
            # 损坏的父策略走结构化错误，不能悄悄退回默认允许。
            if _policy is not None or "MEYO_CAPABILITY_POLICY_JSON" in _env:
                _policy = _provider._narrow_policy(
                    _provider._json_env(_env, "MEYO_CAPABILITY_POLICY_JSON"),
                    _policy if _policy is not None else {})
        _authorization = _permissions.authorize_tool(tool_id, params, _policy)
    except Exception as exc:
        return {"ok": False, "isError": True, "error": f"tool policy unavailable:{type(exc).__name__}"}
    if not _authorization.get("allowed"):
        return {"ok": False, "isError": True,
                "authorization": _authorization,
                "requires_confirmation": bool(_authorization.get("requires_confirmation")),
                "error": (f"APPROVAL_REQUIRED:{_authorization.get('action')}" if _authorization.get("requires_confirmation")
                          else f"CAPABILITY_DENIED:{_authorization.get('action')}")}
    _local_binding = None
    _dl = float(timeout_s if timeout_s is not None else _TOOL_DEADLINE_S)
    if (tool_id.removeprefix("runnable:") == "mcp_call"
            and params.get("server") == "meyo_capabilities"
            and params.get("name") == "call_capability"
            and isinstance(params.get("arguments"), dict)
            and str(params["arguments"].get("tool_id", "")).startswith("local_mcp:meyo-local-model:")):
        # The host already owns a cancellable process boundary. Do not put its
        # cleanup behind a generic Future whose timeout cannot stop a worker.
        try:
            import math
            import agent_tool_provider as _provider
            _session, _env, _cancel = _bound_capability_session()
            _dl = float(_session.local_config["timeout_s"])
            if timeout_s is not None:
                requested = float(timeout_s)
                if not math.isfinite(requested) or requested <= 0:
                    raise ValueError("local tool timeout must be finite and positive")
                _dl = min(_dl, requested)
            inherited = float(_env.get("MEYO_TASK_DEADLINE") or 0)
            if not math.isfinite(inherited) or inherited < 0:
                raise ValueError("invalid parent task deadline")
            limit = _t.monotonic() + _dl
            _env["MEYO_TASK_DEADLINE"] = str(min(limit, inherited) if inherited else limit)
            if _policy is not None:
                _env["MEYO_CAPABILITY_POLICY_JSON"] = json.dumps(_provider._narrow_policy(
                    _provider._json_env(_env, "MEYO_CAPABILITY_POLICY_JSON"), _policy))
            _local_binding = (_env, _cancel)
        except Exception as exc:
            return {"ok": False, "isError": True,
                    "error": f"local capability binding:{type(exc).__name__}:{str(exc)[:200]}"}
    if tool_id.removeprefix("runnable:") in ("model_chat", "model_decisions"):
        # 将此工具调用的实际剩余预算交给已有可回收进程边界；不借主模型输出预算。
        requested = params.get("timeout_s", 90)
        if type(requested) in (int, float) and 0 < requested < float("inf"):
            params["timeout_s"] = min(requested, _dl)
    _idempotency_key = str(params.pop("_idempotency_key", "") or
                           os.environ.get("MEYO_TASK_IDEMPOTENCY_KEY") or "")
    _effect = {"decision": "untracked"}
    if _idempotency_key and _authorization.get("action") in {
            "dependency_install", "billable", "public_deploy", "git_push", "pull_request", "release"}:
        try:
            import side_effect_ledger
            _effect = side_effect_ledger.begin(
                tool_id, params, _idempotency_key, lease_s=max(60, int(_dl) + 30),
                safe_retry=_authorization.get("action") == "dependency_install")
        except Exception as exc:
            return {"ok": False, "isError": True,
                    "error": f"SIDE_EFFECT_LEDGER_UNAVAILABLE:{type(exc).__name__}:{exc}"}
        if _effect.get("decision") == "reuse":
            return {**dict(_effect.get("result") or {}), "reused": True,
                    "effect_id": _effect.get("effect_id"), "authorization": _authorization}
        if _effect.get("decision") == "in_progress":
            return {"ok": False, "isError": True, "effect_in_progress": True,
                    "effect_id": _effect.get("effect_id"),
                    "error": "SIDE_EFFECT_IN_PROGRESS:相同副作用仍在执行，拒绝重复启动"}
        if _effect.get("decision") == "requires_reconciliation":
            return {"ok": False, "isError": True, "requires_reconciliation": True,
                    "effect_id": _effect.get("effect_id"),
                    "error": "SIDE_EFFECT_UNKNOWN:上次进程在副作用完成落账前崩溃，必须先查询外部系统状态，禁止自动重放"}
    call_id = f"{tool_id}-{int(_t.time()*1000) % 1000000}"
    source_ref = _tool_source_ref(tool_id)
    selection = {"source": "caller_selected", "candidate_verified": "pending_search_association"}

    def _ev(d):
        if event_cb:
            try:
                event_cb(d)
            except Exception:
                pass

    _ev({"t": "tool_started", "tool": tool_id, "call_id": call_id,
         "args_brief": _brief(params), "ts": _t.time(), "selection": selection,
         "source_ref": source_ref,
         "x": f"开始使用 {source_ref.get('name') or tool_id}（{_brief(params, 40)}）"})
    _t0 = _t.time()
    try:
        if _runtime_snapshot is not None:
            # 执行复用授权前的同一份快照，不在回调后重新取身份/权限。
            # 普通无绑定、无策略的 CLI 仍不额外加载调度中枢。
            _agent_call = _provider._bind_runtime_call(
                _run_tool_inner, tool_id, _agent_params if _agent_params is not None else params,
                capability_policy=_policy, _binding=_runtime_snapshot)
        with (nullcontext() if _local_binding is not None
              else _cf.ThreadPoolExecutor(max_workers=1)) as _ex:
            try:
                if _local_binding is not None:
                    with _provider._runtime_scope(*_local_binding, inherit_process_context=False):
                        out = _run_tool_inner(tool_id, params)
                    if out.get("result", {}).get("status") == "timeout":
                        out["tool_timeout"] = True
                        out["isError"] = True
                        _ev({"t": "tool_timeout", "tool": tool_id, "call_id": call_id,
                             "status": "timeout", "limit_s": _dl, "source_ref": source_ref,
                             "x": f"{tool_id} 已超时"})
                else:
                    fut = (_ex.submit(_agent_call) if _agent_call is not None
                           else _ex.submit(_run_tool_inner, tool_id, params))
                    out = fut.result(timeout=_dl)
                _ms = int((_t.time() - _t0) * 1000)
                _ok = bool(out.get("ok"))
                # 场景学习只记录事实；候选归因/时间窗/scope 校验全部由独立学习器负责。
                attribution = _record_tool_usage(tool_id, _ok) or {}
                final_selection = {**selection,
                                   "candidate_verified": "matched" if attribution.get("candidate_matched") else "not_matched",
                                   "query_age_s": attribution.get("query_age_s")}
                _ev({"t": "tool_end", "tool": tool_id, "call_id": call_id, "ok": _ok,
                     "status": "succeeded" if _ok else "failed", "ms": _ms,
                     "selection": final_selection, "source_ref": source_ref,
                     "result_brief": _brief(out.get("url") or out.get("answer") or out.get("text")
                                            or out.get("error") or out),
                     "x": (f"{source_ref.get('name') or tool_id} 已完成（{_ms}ms）" if _ok
                           else f"{source_ref.get('name') or tool_id} 未成功（{_ms}ms）")})
                if isinstance(out, dict):
                    out.setdefault("authorization", _authorization)
                    if _effect.get("effect_id"):
                        out["effect_id"] = _effect["effect_id"]
                        try:
                            import side_effect_ledger
                            side_effect_ledger.finish(_effect["effect_id"], out)
                        except Exception:
                            out["effect_ledger_warning"] = "finish_failed"
                return out
            except _cf.TimeoutError:
                # 结构化 TOOL_TIMEOUT(deepseek 式)·isError 让调用方一眼识别是工具超时该换招。
                attribution = _record_tool_usage(tool_id, False) or {}
                final_selection = {**selection,
                                   "candidate_verified": "matched" if attribution.get("candidate_matched") else "not_matched",
                                   "query_age_s": attribution.get("query_age_s")}
                _ev({"t": "tool_timeout", "tool": tool_id, "call_id": call_id,
                     "status": "timeout", "limit_s": int(_dl), "selection": final_selection,
                     "source_ref": source_ref,
                     "x": f"{source_ref.get('name') or tool_id} 超过 {int(_dl)}s 未返回·可换其他来源"})
                timed_out = {"ok": False, "isError": True, "tool_timeout": True,
                             "content": f"TOOL_TIMEOUT: {tool_id} 超过 {int(_dl)}s 未返回",
                             "error": f"TOOL_TIMEOUT: {tool_id} 超过 {int(_dl)}s 未返回",
                             "hint": "工具超时(网关慢/工具卡)·换个工具或换个方式重试·别干等。"}
                if _effect.get("effect_id"):
                    try:
                        import side_effect_ledger
                        side_effect_ledger.finish(_effect["effect_id"], timed_out)
                    except Exception:
                        pass
                return timed_out
    except Exception as e:
        # 线程池自身异常兜底(极端·不该发生)·仍走结构化出口不抛。
        attribution = _record_tool_usage(tool_id, False) or {}
        final_selection = {**selection,
                           "candidate_verified": "matched" if attribution.get("candidate_matched") else "not_matched",
                           "query_age_s": attribution.get("query_age_s")}
        _ev({"t": "tool_end", "tool": tool_id, "call_id": call_id, "ok": False,
             "status": "failed", "selection": final_selection, "source_ref": source_ref,
             "x": f"{source_ref.get('name') or tool_id} 调度失败·可换其他来源"})
        return {"ok": False, "isError": True, "error": f"run_tool 调度失败: {type(e).__name__}: {e}"}


def _run_tool_inner(tool_id: str, params: dict) -> dict:
    """真执行一件可运行工具。当前可跑:媒体工具(gen_image/see_image/…)+ opencli + search_github。"""
    if tool_id.startswith("agent_tool:"):
        import agent_tool_provider
        _, provider, native_tool = tool_id.split(":", 2)
        try:
            return agent_tool_provider.invoke(
                provider, native_tool, params.get("arguments", params),
                **agent_tool_provider._call_kwargs(params))
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    if tool_id.startswith("external_capability:"):
        import external_agent_registry as registry
        try:
            _, key, action = tool_id.split(":", 2)
        except ValueError:
            return {"ok": False, "error": "external_capability id 必须是 external_capability:<key>:<action>"}
        import agent_tool_provider
        active = sys.modules.get("harness_registry")
        has_context = (any(name in os.environ for name in agent_tool_provider._ENV_FIELDS)
                       or getattr(getattr(active, "_INPROC_RUNTIME_ENV", None), "values", {}))
        bound = agent_tool_provider._runtime_binding()[1] if has_context else {}
        host_only = os.environ.get("MEYO_NATIVE_HOST_ACTIVE") == "1"
        if host_only:
            from native_capability_host import _ENV_KEYS
            bound = {**{key: value for key, value in os.environ.items() if key in _ENV_KEYS},
                     **bound}
        return registry.run_capability(
            key, action, params, timeout_s=int(params.get("timeout_s") or 0) or None,
            env=bound or None, workdir=str(bound.get("MEYO_WORKDIR") or ""),
            isolated_environment=host_only)
    # dsh-vision 脚本工具无状态直调(源码在本仓库·PIL纯本地·不派家)。军团 run vision_tool:<name>。
    if tool_id.startswith("vision_tool:"):
        import vision_toolkit_bridge as _vt
        _vid = tool_id.split(":", 1)[-1]
        if _vt.available() and _vid in _vt.list_bridged_tools():
            return _vt.call(_vid, params, timeout_s=int(params.get("timeout_s") or 120))
        return {"ok": False, "error": f"dsh-vision无工具 {_vid}",
                "hint": f"可直调: {_vt.list_bridged_tools()}"}
    # 1) 媒体工具(runnable:xxx 或裸 media id)
    mid = tool_id.split(":", 1)[-1] if tool_id.startswith("runnable:") else tool_id
    if mid in ("model_catalog", "model_chat", "model_decisions"):
        import media_tools
        return media_tools.run_shared_model_tool(mid, params)
    try:
        import media_tools
        if mid in media_tools.MEDIA_TOOLS:
            return media_tools.run_media_tool(mid, params)
    except Exception as e:
        return {"ok": False, "error": f"media_tools 执行失败: {e}"}
    # 1.5) 全军团共享的 WebSearch/WebFetch 等价入口。各家自己的原生搜索继续保留；
    # 共享入口解决的是“某家没有/当前 provider 不可用时仍能搜索抓取”，不是覆盖其原生能力。
    if mid == "web_search":
        return _run_web_search(params.get("query") or "", params.get("queries"),
                               int(params.get("limit") or params.get("top_k") or 8),
                               params.get("mode") or "fast", params.get("source") or "")
    if mid == "openmontage_demos":
        import subprocess as _sp, pathlib as _pl
        script = _pl.Path(__file__).resolve().parents[2] / "harness_baselines/ecosystem/openmontage/render_demo.py"
        command = [sys.executable, str(script)]
        if params.get("list", False): command.append("--list")
        elif params.get("demo"): command.append(str(params["demo"]))
        result = _sp.run(command, capture_output=True, text=True, timeout=int(params.get("timeout_s") or 600))
        return {"ok": result.returncode == 0, "stdout": result.stdout[-8000:],
                "error": result.stderr[-4000:], "command": command}
    if mid in ("web_fetch", "fetch_url"):
        return _run_web_fetch(params.get("url") or "", int(params.get("timeout_s") or 25))
    if mid == "repo_search":
        try:
            import func_index
            return {"ok": True, **func_index.search(
                str(params.get("query") or params.get("q") or ""),
                top=int(params.get("top") or 15), tier=params.get("tier"))}
        except Exception as e:
            return {"ok": False, "error": f"repo_search 失败: {type(e).__name__}: {e}"}
    if mid in ("file_read", "spreadsheet_inspect", "media_inspect"):
        try:
            import file_hub
            path = str(params.get("path") or params.get("file") or params.get("file_path") or "")
            if not path:
                return {"ok": False, "error": f"{mid} 需要 path"}
            if mid == "spreadsheet_inspect" and file_hub.kind_of(path) != "spreadsheet":
                return {"ok": False, "error": f"不是可解析表格: {path}"}
            result = file_hub.describe(path, str(params.get("url") or ""))
            return {"tool": mid, **result}
        except Exception as e:
            return {"ok": False, "error": f"{mid} 失败: {type(e).__name__}: {e}"}
    if mid == "desktop_control":
        try:
            import desktop_fs
            args = dict(params)
            bound = (os.environ.get("MEYO_LEGION_UID") or os.environ.get("MEYO_USER_ID") or "").strip()
            return {"ok": True, **desktop_fs.local_mcp_call(
                "desktop_control", args, bound_uid=bound or None)}
        except Exception as e:
            return {"ok": False, "error": f"desktop_control 失败: {type(e).__name__}: {e}"}
    if mid == "my_workspace":
        # 用户配置和查看自己的:memory(md)/沙盒文件/pilot/云电脑是否在。
        try:
            import my_workspace
            uid = str(params.get("uid") or os.environ.get("MEYO_USER_ID") or "default")
            return my_workspace.workspace(uid)
        except Exception as e:
            return {"ok": False, "error": f"my_workspace 失败: {e}"}
    if mid == "my_view":
        # 因人而异:某用户的个性化视图(已装skill/常用agent/偏好首脑/历史)。
        try:
            import user_view
            uid = str(params.get("uid") or os.environ.get("MEYO_USER_ID") or "default")
            return user_view.my_view(uid)
        except Exception as e:
            return {"ok": False, "error": f"my_view 失败: {e}"}
    if mid == "skill_improve":
        # Warp式自进化improver:定时观察者·聚合skill反馈→产出可审编辑提议(不自动改·人审后生效)。
        try:
            import skill_improver
            uid = str(params.get("uid") or os.environ.get("MEYO_USER_ID") or "default")
            if params.get("skill"):
                return skill_improver.propose_improvement(str(params["skill"]), uid=uid,
                                                          since_days=int(params.get("since_days") or 30))
            return skill_improver.improve_all(uid=uid, since_days=int(params.get("since_days") or 30))
        except Exception as e:
            return {"ok": False, "error": f"skill_improve 失败: {e}"}
    if mid == "source_audit":
        # 任意成员都可发起的源码审核→互学→自优化入口。长方法放在按需Skill里，
        # 此处只提供事实、证据核验和持久账本，不把审核协议塞进普通任务prompt。
        try:
            import source_audit
            action = str(params.get("action") or "prepare")
            uid = str(params.get("uid") or os.environ.get("MEYO_USER_ID") or "default")
            if action == "record":
                return source_audit.record(params.get("entry") or {}, uid)
            if action == "list":
                return source_audit.list_entries(uid, str(params.get("member") or ""),
                                                 str(params.get("status") or ""),
                                                 int(params.get("limit") or 50))
            if action == "show":
                return source_audit.show(str(params.get("audit_id") or ""), uid)
            if action == "prior_art":
                return source_audit.discover_prior_art(
                    params.get("queries") or params.get("query") or params.get("goal") or "",
                    int(params.get("limit") or 40))
            if action == "verify":
                return source_audit.verify_reference(str(params.get("source_root") or ""),
                                                     str(params.get("file") or ""),
                                                     int(params.get("line") or 0),
                                                     str(params.get("quote") or ""),
                                                     str(params.get("revision") or ""))
            return source_audit.prepare(str(params.get("goal") or params.get("query") or ""),
                                        params.get("scope"), params.get("dimensions"),
                                        str(params.get("mode") or "full-loop"), uid)
        except Exception as e:
            return {"ok": False, "error": f"source_audit 失败: {type(e).__name__}: {e}"}
    if mid == "agent_adapters":
        # 全agent适配状态总览(ready能派/installable装了能派/capability_lib走函数调用)。
        try:
            import agent_adapters
            return agent_adapters.overview()
        except Exception as e:
            return {"ok": False, "error": f"agent_adapters 失败: {e}"}
    if mid == "func_call":
        # 使用一个被发现的公开函数:纯本地py真直调·否则诚实给派家出路(func_invoke分层)。
        try:
            import func_invoke
            return func_invoke.invoke(
                str(params.get("path") or ""), str(params.get("qual") or ""),
                str(params.get("family") or params.get("harness") or ""),
                params.get("args") or params.get("arguments") or {},
                lang=str(params.get("lang") or "python"),
                # 默认真派(不能直调就自动启动对应家去调);传 dispatch:false 只预览出路
                dispatch=params.get("dispatch", True))
        except Exception as e:
            return {"ok": False, "error": f"func_call 失败: {e}"}
    if mid == "func_search":
        # 全仓公开函数/工具检索(全部29k+·各家源码里每个def)。军团用它查"哪家有现成函数"。
        try:
            import func_index
            q = str(params.get("q") or params.get("query") or "")
            _tier = params.get("tier")  # 'tool' 只搜声明工具·None 搜全部
            return {"ok": True, **func_index.search(q, top=int(params.get("top") or 15), tier=_tier)}
        except Exception as e:
            return {"ok": False, "error": f"func_search 失败: {e}"}
    if mid == "capability_catalog":
        return _run_capability_catalog()
    if mid == "research_capabilities":
        return _run_research_capabilities()
    if mid.startswith("research_"):
        return _run_research_stage(mid.removeprefix("research_"), params)
    if mid == "skill_finder":
        factory_params = {key: value for key, value in params.items()
                          if key not in ("query", "agent_type", "timeout_s")}
        return _run_skill_finder(params.get("query") or "", params.get("agent_type") or "legion",
                                 int(params.get("timeout_s") or 120), **factory_params)
    if mid == "skill_factory_list":
        import skill_factory
        return skill_factory.list_active(str(params.get("uid") or os.environ.get("MEYO_USER_ID") or "default"))
    if mid == "deploy_site":
        # mode=local 仅做静态文件地址映射；未托管应用进程，HTTP/业务可用性尚未测试。
        # URL 访问仍受既有服务授权约束；mode=public 对外发布仍需 confirm 授权。
        import site_deploy
        return site_deploy.deploy(str(params.get("path") or ""), str(params.get("mode") or "local"),
                                  str(params.get("provider") or "vercel"), bool(params.get("confirm")))
    if mid == "search_sources":
        return _run_search_sources()
    if mid == "search_source_probe":
        return _run_search_source_probe(params.get("source") or "", params.get("query") or "",
                                        int(params.get("limit") or 3),
                                        int(params.get("timeout_s") or 120))
    if mid == "mcp_list":
        return _run_mcp_list()
    if mid == "mcp_call":
        arguments = params.get("arguments") or {}
        # 身份绑定(context not control):harness 子进程经 MEYO_USER_ID 携带真实用户身份,
        # cua-local-desktop 的桌面/文件工具用【绑定身份】执行,参数里的 uid 只能一致不能改写。
        bound = (os.environ.get("MEYO_LEGION_UID") or os.environ.get("MEYO_USER_ID") or "").strip()
        if bound and str(params.get("server") or "") == "cua-local-desktop":
            arguments = {**arguments, "uid": bound}
        return _run_mcp_call(params.get("server") or "", params.get("name") or "", arguments)
    # 2) opencli(网页→CLI)
    if mid == "opencli":
        try:
            import opencli_bridge
            if not opencli_bridge.available():
                return {"ok": False, "error": "opencli 未安装(npm i -g @jackwener/opencli)"}
            return opencli_bridge.run(params.get("args") or [])
        except Exception as e:
            return {"ok": False, "error": f"opencli 执行失败: {e}"}
    # 2.5) auto 无状态工具直执行:auto 的大部分工具依赖 qid/session 装配(得派 auto 家跑),
    #      但 search_github 是纯 GitHub API 调用、无 session 依赖 —— 军团任何一家发现它后
    #      应能【直接 run 真装新工具】,不该只给"读源码"。这才是"注册让大家发现"的闭环:
    #      发现→检索→真执行。(用户 2026-08-28:让 auto 全部工具都能被发现+使用。)
    _auto_id = tool_id.split(":", 1)[-1] if tool_id.startswith("auto_tool:") else tool_id
    if _auto_id == "search_github":
        return _run_search_github(params.get("query") or params.get("q") or "",
                                  int(params.get("limit") or 5))
    if _auto_id == "inspect_github_repo":
        return _inspect_github_repo(params.get("clone_url") or params.get("url") or "",
                                    params.get("full_name") or "", params.get("revision") or "",
                                    params.get("timeout_s", 20), params.get("plan_only") is True)
    if _auto_id == "install_github_repo":
        return _install_github_repo(params.get("clone_url") or params.get("url") or "",
                                    params.get("full_name") or "",
                                    (bool(params["confirm"]) if "confirm" in params else None),
                                    int(params.get("timeout_s") or 180),
                                    params.get("capability_policy"))
    # 2.6) ★auto无状态工具直执行(用户2026-08-30:auto拆成大家立即可用的工具):
    #      auto的媒体/搜索/抓取/研究类工具本质无session依赖·映射到 media_tools + 已有 _run_* 真实现,
    #      让军团任何家 run auto_tool:<name> 直接跑·不再"派auto家"。session依赖类(spawn_subagent等)仍走下方native.
    # ★auto拆解(用户2026-08-30):auto里真正无状态的工具映射到军团独立实现,让大家 run 直接跑。
    #   诚实说明:auto的OCR/PDF/图像工具(dots_ocr/mineru_pdf/image_preprocess…)依赖auto的模型
    #   pipeline+session装配·不能凭空当runnable(否则是假装·违CLAUDE.md);真无状态的是"抓取"类。
    #   军团独立媒体能力(生图/识图/读PDF)本就有独立工具入口(见 media_tools·非auto_tool命名空间)。
    try:
        _AUTO_TO_RUN = {
            # auto抓取类 → legion_tool 无状态抓取入口(真无session依赖)
            "fetch_url_smart": lambda p: _run_web_fetch(p.get("url") or "", int(p.get("timeout_s") or 25)),
            "fetch_outlinks": lambda p: _run_web_fetch(p.get("url") or "", int(p.get("timeout_s") or 25)),
        }
        if _auto_id in _AUTO_TO_RUN:
            return _AUTO_TO_RUN[_auto_id](params)
    except Exception as e:
        return {"ok": False, "error": f"auto无状态工具执行失败({_auto_id}): {e}"}
    # 2.7) ★capability_stack工厂工具无状态桥(用户2026-08-30:所有工厂工具让全军团直接用·
    #      绕开auto恶心的完整pipeline)。OCR/PDF/图像/duckdb/sympy/wayback等20个t_工具核心无状态·
    #      session仅记录用(给空dict即可)→军团 run auto_tool:<name> 直接走桥·不派auto家。
    try:
        import capstack_bridge as _cb
        if _cb.available() and _auto_id in _cb.list_bridged_tools():
            _path = params.get("path") or params.get("image_path") or params.get("pdf_path") or \
                    params.get("video_path") or params.get("file") or ""
            return _cb.call(_auto_id, path=_path, question=params.get("question") or params.get("q") or "",
                            timeout_s=int(params.get("timeout_s") or 120))
    except Exception as e:
        return {"ok": False, "error": f"capstack桥调失败({_auto_id}): {e}"}
    # 3) 其它:目前只读(skill/源码类),给出路径让模型自己读
    try:
        import toolbox
        it = toolbox.get_tool(tool_id)
        if it:
            note = (f"{tool_id} 的源码已保留，但它仍依赖旧引擎的会话装配，尚未接成独立工具。"
                    f"auto 引擎已停用，不可派单；可读 {it.get('path')} 复用实现，"
                    f"或选择共享工具箱中已有的可运行能力。"
                    if it.get("auto_native") else
                    f"{tool_id} 是 {it.get('kind')} 类(skill/源码),不直接执行。"
                    f"读 {it.get('path')} 按它说明操作。")
            return {"ok": False, "runnable": False, "reference": it.get("path"),
                    "auto_native": bool(it.get("auto_native")), "note": note}
    except Exception:
        pass
    return {"ok": False, "error": f"未知或不可执行的工具: {tool_id}",
            "hint": "先用 `legion_tool.py search <关键词>` 找可 run 的工具。"}


def list_caps(cap: str = "") -> dict:
    tb = _load_toolbox()
    if not cap:
        return {"by_capability": tb["summary"]["by_capability"],
                "total": tb["summary"]["total"],
                "hint": "用 `legion_tool.py list <cap>` 看某类;<cap> 见上面的键。"}
    items = [it for it in tb.get("items", []) if cap in (it.get("capability_tags") or [])]
    # 去重(同名同源 skill 常被扫两份)·runnable 优先展示
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: (0 if x.get("runnable") else 1)):
        k = (str(it.get("name") or "").lower(), str(it.get("source_harness") or "").lower())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    return {"capability": cap, "count": len(uniq),
            "tools": [{"id": it["id"], "name": it.get("name"),
                       "runnable": bool(it.get("runnable")),
                       "desc": (it.get("description") or "")[:120]} for it in uniq[:40]]}


def _main(argv):
    if not argv:
        print(json.dumps({"usage": "legion_tool.py search|run|list ..."}, ensure_ascii=False))
        return 1
    cmd = argv[0]
    if cmd == "search":
        q = " ".join(argv[1:])
        def _cli_search_event(event):
            try:
                sys.stderr.write(_EV_PREFIX + json.dumps(event, ensure_ascii=False) + "\n")
                sys.stderr.flush()
            except Exception:
                pass
        result = search(q, event_cb=_cli_search_event)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    if cmd == "run":
        if len(argv) < 2:
            print("ERROR: run 需 <tool_id> [json_params]")
            return 1
        tool_id = argv[1]
        params = {}
        if len(argv) >= 3:
            raw = argv[2]
            try:
                params = json.loads(raw)
            except Exception:
                # 允许简写:run see_image "path" "question" 位置参数
                params = {"_positional": argv[2:]}
        # 项③:CLI run 也发结构化生命周期事件到 stderr(带 _EV_PREFIX)·worker 透传→SSE→前端。
        # harness 用 shell 调 legion_tool.py run 时,过程就像 CC 一样被看到(tool_started/end/timeout)。
        def _cli_ev(d):
            try:
                sys.stderr.write(_EV_PREFIX + json.dumps(d, ensure_ascii=False) + "\n")
                sys.stderr.flush()
            except Exception:
                pass
        out = run_tool(tool_id, params, event_cb=_cli_ev)
        # 结构化工具一律输出完整 JSON。旧版优先只打印 url，导致 web_fetch 虽抓到了标题/正文，
        # harness 却只看见原 URL，还得绕回底层再调用一次；这正是结果回流断链。
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok") else 1
    if cmd == "list":
        print(json.dumps(list_caps(argv[1] if len(argv) > 1 else ""),
                         ensure_ascii=False, indent=1))
        return 0
    if cmd == "adopted":
        # 跨家经验学习飞轮:只有 harness 自己知道它是否真采纳了召回经验·父层绝不猜。
        # 给它一个真回报入口(context not control):用了就主动调,没用就不调。
        if len(argv) != 2:
            print(json.dumps({"ok": False, "error": "adopted 需 <本次召回的entry_id>；不接受问题/来源/身份参数"},
                             ensure_ascii=False))
            return 1
        try:
            import legion_memory
            bound = (os.environ.get("MEYO_LEGION_UID") or os.environ.get("MEYO_USER_ID") or "").strip()
            result = legion_memory.mark_adopted(argv[1], user_id=bound)
        except Exception as e:
            result = {"ok": False, "error": f"经验回报失败:{type(e).__name__}:{str(e)[:160]}"}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    if cmd == "audit-source":
        if len(argv) < 2:
            print(json.dumps({"ok": False, "error": "audit-source 需 JSON 参数"}, ensure_ascii=False))
            return 1
        try:
            params = json.loads(argv[1])
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"audit-source JSON 无效: {e}"}, ensure_ascii=False))
            return 1
        out = run_tool("runnable:source_audit", params)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out.get("ok") else 1
    print(json.dumps({"error": f"未知命令 {cmd}", "cmds": ["search", "run", "list", "adopted", "audit-source"]},
                     ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
