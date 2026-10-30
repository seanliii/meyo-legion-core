"""v13.87.28 · search_planner · LLM 看 query 挑 source 组合

用户 2026-07-23 goal:
「不是硬来·而是整体协作·根据不同任务·你自己每次搜索反思·
 然后看看如何组合在不同场景下更好」

── 病 ──
现役 deep mode = 硬跑 forger + fanout + last30days + 3 引擎 · 45s
不管 query 是啥都上全套 · 浪费 · 不适应

── 解 ──
planner 看 query · 判 intent · 挑 source subset:

query intent 5 类:
    fact_lookup      · 事实查证 (RAG 是啥) → web + scholar 少量 · 快
    trend_recent     · 最近趋势 (2026 副业趋势) → last30days + friday · 中
    deep_research    · 深度研究 (RAG 评测方法) → 3 引擎 + scholar + 论文 · 慢
    social_signal    · 真人经验 (副业月入真实) → xhs + reddit + hn · 中
    code_technical   · 技术代码 (rust framework) → hn + github + friday_bing · 中

planner 输出 SearchPlan (dataclass):
    intent · sources_ordered · use_deep · use_last30days · use_3engines
    · reason · confidence

学习: 每次搜完 planner 落 .search_plans.jsonl · reflector 反思后进化
"""
from __future__ import annotations  # v13.93.2 · 兼容 cron 环境 /usr/bin/python3 (3.9) · 见下方 str|None 等新式注解
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


# v13.88.78 · 静默监控接入
def _alert(severity: str, msg: str):
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from alert_bus import notify
        notify("search_planner", severity, msg)
    except Exception:
        pass

V11 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PLANS_LOG = os.path.join(V11, ".search_plans.jsonl")

# Model 选 haiku · 每次 query 都调 · 快
_MODEL = os.environ.get("MEYO_PLANNER_MODEL", "haiku")

# v13.93 · EvoSkill 接入 · planner system prompt 外部化
# 用户 2026-08-11「skill forge 自进化不能改变搜索吗·而且是通用的方式」
# 之前 _PLANNER_SYSTEM 硬编码在这个 .py 里·每次优化只能人工改代码+发版本号
# (v56i/v56n/...) · 现在把它挪到外部文本文件·让 v11/evolution/evo_skill_loop.py
# 能像管理其它 Skill SOP 一样对它跑「信号采集→回归检测→归因改写→验证门控→
# 回归护栏」的完整闭环。_PLANNER_SYSTEM_FALLBACK 保留在代码里作为最后一道
# fail-safe 兜底(外部文件读取失败/损坏时用它·不因为 EvoSkill 出问题就搜索瘫痪)。
_SOP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "skills_sop", "search_planner_sop.md")


def _strip_front_matter(text: str) -> str:
    """去掉 SOP 文件开头的 YAML front-matter(--- ... ---)·只留正文。"""
    if text.startswith("---"):
        m = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.DOTALL)
        if m:
            return text[m.end():].lstrip("\n")
    return text


def _load_planner_sop() -> str:
    """优先读外部 SOP 文件(可被 EvoSkill 自动改写) · 读不到就用内置兜底常量。
    每次调用都实时读盘(不缓存)·跟博客里"Optimizer 必须看到与 Target 完全
    一致的 SOP 版本"同一原则:planner 执行时也必须用最新生效版本,不能因为
    进程内缓存了旧版本导致行为和磁盘上的 SOP 文本不一致。"""
    try:
        if os.path.exists(_SOP_PATH):
            raw = open(_SOP_PATH, encoding="utf-8").read()
            body = _strip_front_matter(raw).strip()
            if body:
                return body
    except Exception:
        pass
    return _PLANNER_SYSTEM_FALLBACK


def _load_learned_techniques() -> str:
    """v13.93 · 打通断头闭环:之前 search_meta_learning.get_techniques_for_planner()
    学到的搜索技巧只有旧链路 v11/agent_search.py 消费·新链路 search_planner.py
    从未读取过(总结完全没被用上)。这里补上真正的消费点。失败静默返回空串,
    不影响 planner 主流程。"""
    try:
        from search_meta_learning import get_techniques_for_planner
        return get_techniques_for_planner()
    except Exception:
        return ""


@dataclass
class SearchPlan:
    """一次搜索的执行计划 · v13.87.29 加 depth_score 动态深度"""
    intent: str  # fact_lookup / trend_recent / deep_research / social_signal / code_technical
    sources_ordered: list = field(default_factory=list)  # 按优先级排 · fanout 走这些
    # v13.87.29 · 不再 fast/balanced/deep 硬套 · LLM 判 0-100 深度分
    depth_score: int = 50  # 0-30 fast, 31-70 balanced, 71-100 deep
    use_last30days: bool = False
    use_3engines: bool = False
    use_scholar: bool = False
    use_supervise_loop: bool = False
    max_iter: int = 1
    reason: str = ""
    confidence: int = 50  # planner 自己置信度
    fallback: str = "fanout_default"  # 挂了走啥
    # v13.87.31 · 动态 site: dorks · query 里有实体时生成
    site_dorks: list = field(default_factory=list)


# 意图 → 默认组合 (LLM 挂时兜底)
# v13.87.29 · Google 全球兜底 · 必进 4 类 intent (social_signal 除外·中文差)
# depth_score 决定循环/追搜/3engines 强度
# v56i(2026-08-05) · skill 编排升级(用户「为啥用sources不用skill编排」)。
#   按日志实测质量重排每类intent的搜索skill组合(旧模板首选friday_google Q0.38-0.46差·
#   含hackernews Q0.349最差)。新组合: friday_bing(Q0.89主力)+anysearch(绕反爬高质量·
#   实测秒返带全文)替代google系·firecrawl_search(反爬极强·难题补)作深研补充源。
#   每类query配最优skill组合·这就是「skill编排」的实质(source=可执行搜索skill)。
_INTENT_TEMPLATES = {
    "fact_lookup": SearchPlan(
        intent="fact_lookup",
        # v56n · 加 tavily(answer字段直接抽答·事实题最需精确抽取)
        sources_ordered=["friday_bing", "anysearch", "tavily"],
        depth_score=25,  # 快而少
        use_last30days=False, use_3engines=False,
        use_supervise_loop=False, max_iter=1,
        reason="事实查证·friday_bing+anysearch+tavily(answer直接抽答)·少而准",
        confidence=70),
    "trend_recent": SearchPlan(
        intent="trend_recent",
        sources_ordered=["friday_bing", "anysearch", "friday_zhipu"],
        depth_score=60,  # 中度 · 需追搜时效
        use_last30days=True, use_3engines=True,
        use_supervise_loop=True, max_iter=2,
        reason="最近趋势·bing+anysearch+zhipu·last30days",
        confidence=75),
    "deep_research": SearchPlan(
        intent="deep_research",
        # v56k · 深研上四源: bing(快主力)+anysearch(绕反爬)+wigolo(18引擎融合带评分)+
        #   zhihu(中文社媒)·firecrawl(反爬难题)由补搜按需追加。深研要广度·多源互补值得。
        sources_ordered=["friday_bing", "anysearch", "wigolo",
                          "zhihu_global"],
        depth_score=90,  # 深 · 全套追搜
        use_last30days=True, use_3engines=True,
        use_scholar=True, use_supervise_loop=True, max_iter=3,
        reason="深度研究·bing+anysearch+wigolo(18引擎)+zhihu·多源互补·3轮追搜",
        confidence=80),
    "social_signal": SearchPlan(
        intent="social_signal",
        # 中文社媒场景 · 优先社媒 · anysearch 兜底(替代 friday_google)
        sources_ordered=["xiaohongshu", "zhihu", "sogou_weixin",
                          "anysearch"],
        depth_score=55,
        use_last30days=True, use_3engines=False,
        use_supervise_loop=True, max_iter=2,
        reason="真人经验·中文社媒优先·anysearch 兜底(reddit 等海外)",
        confidence=75),
    "code_technical": SearchPlan(
        intent="code_technical",
        sources_ordered=["github_issues", "friday_bing", "anysearch"],
        depth_score=50,
        use_last30days=True, use_3engines=False,
        use_supervise_loop=False, max_iter=1,
        reason="技术代码·github(Q0.68)+bing+anysearch(code域路由)",
        confidence=75),
}


# v13.93 · 改名 _PLANNER_SYSTEM_FALLBACK · 真正生效的内容优先来自
# skills_sop/search_planner_sop.md(见 _load_planner_sop)·这份常量只在外部
# 文件缺失/损坏时兜底,内容与抽出时的最后版本(v13.87.31)保持一致。
_PLANNER_SYSTEM_FALLBACK = """You are a search planner. Given a user query, decide:
1. Which of 5 intents fits best
2. Which sources to try (ordered by priority) — GOOGLE MUST BE INCLUDED (see below)
3. depth_score 0-100 (决定 use_l30 / use_3eng / max_iter)
4. Whether to use last30days / 3 engines / scholar / supervise loop
5. Max iterations if loop

Return JSON only:
{
  "intent": "fact_lookup | trend_recent | deep_research | social_signal | code_technical",
  "sources_ordered": ["source1", "source2", ...],
  "depth_score": 0-100,
  "use_last30days": bool,
  "use_3engines": bool,
  "use_scholar": bool,
  "use_supervise_loop": bool,
  "max_iter": 1-3,
  "reason": "one line why",
  "confidence": 0-100
}

## 🔴 GOOGLE 硬规 (v13.87.29 · 用户抓「google 说了一千遍」)
- `friday_google` 是全球最权威兜底 · **必进 sources_ordered · 至少前 3**
- 例外 · social_signal 场景 (纯中文社媒真人) · Google 可后置或省
- 例外 · fact_lookup 纯代码题 (rust/python API) · hn/github 优先 · Google 3位

## 🔴 sources_ordered 多样性硬规 (v13.87.30 · 用户抓「只 3 源」)
- **不能全是 Friday 类** (friday_google/bing/zhipu 是"广度层" · 由 use_3engines=true 自动并行)
- **sources_ordered 至少 5 类不同角色**:
  1. Google 兜底 (friday_google)
  2. 至少 1 个内容源: zhihu / hackernews / xiaohongshu (按 intent)
  3. 至少 1 个技术源: github_issues / github_repos / sspai (按 intent)
  4. 至少 1 个社媒: reddit / v2ex / sogou_weixin (按 intent)
  5. 至少 1 个 fanout 类: friday_bing / juejin
- **friday_google/bing/zhipu 3 引擎** = use_3engines 独立控制 · 不算入 sources_ordered 主槽

## 🔴 site: dork 动态扩展 (v13.87.31 · 用户抓「fanout 自己拓展」)
如果 query 里有明显实体·输出 site_dorks 字段·让 SearchAgent 加 site:xxx.com 追搜:
- 涉及某公司/产品 (Anthropic/OpenAI/Notion) → site_dorks=["site:anthropic.com", "site:openai.com"]
- 涉及某语言/框架 → site_dorks=["site:rust-lang.org", "site:doc.rust-lang.org"]
- 涉及学术 → site_dorks=["site:arxiv.org", "site:papers.nips.cc"]
最多 3 个 dork · 不用堆

## depth_score 规则
- 0-30 · 快查 · use_l30=false 3eng=false loop=false iter=1
- 31-70 · 中度 · use_l30 视情 · 3eng=false loop=可选 iter=1-2
- 71-100 · 深挖 · 3eng=true loop=true iter=2-3 · 全套上

Intent rules:
- fact_lookup: 是什么/定义/API 姿势/版本 · depth 20-30
- trend_recent: 最近/2026/趋势/新鲜 · depth 50-70
- deep_research: 评测/方法论/综述/分析 · depth 80-100
- social_signal: 真实/经验/月入/踩坑/真人 · depth 40-60
- code_technical: 代码/framework/error/bug/工具 · depth 40-60

Source options:
- 中文源: zhihu · xiaohongshu · sogou_weixin · sspai · juejin · bilibili · v2ex
- 英文源: hackernews · github_issues · github_repos · reddit · producthunt · twitter
- Friday (推荐 · 秒返稳): friday_bing · **friday_google (兜底)** · friday_zhipu
- 通用: google (CDP · 慢) · smart_fetch

Rules:
- 不硬堆 · 挑 3-5 source 精准
- friday_google 必进 (除非纯中文社媒)
- 中文 query 主用中文源 · friday_google 兜底
- 一定要有 fallback (至少 friday_google 或 friday_bing)"""


def _log_plan(plan: dict, query: str, elapsed: float = 0):
    """落 .search_plans.jsonl · reflector 消费"""
    try:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "unix_ts": int(time.time()),
            "query": query[:200],
            "plan": plan,
            "elapsed_s": elapsed,
        }
        with open(PLANS_LOG, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _try_recall_similar_plan(query: str) -> Optional[SearchPlan]:
    """从历史 plan 找相似 query 的成功方案 · reflector 学到的
    简单实现: 抽关键词 · 找相同 intent
    """
    if not os.path.exists(PLANS_LOG):
        return None
    try:
        # 只读最近 100 条 · 快
        with open(PLANS_LOG) as f:
            lines = f.readlines()[-100:]
        # 简单 · query 前 5 词命中就算相似
        q_terms = set(re.split(r"\s+", query.lower()))[:8]
        for line in reversed(lines):
            try:
                r = json.loads(line.strip())
                past_q = r.get("query", "")
                past_terms = set(re.split(r"\s+", past_q.lower()))
                overlap = len(q_terms & past_terms)
                if overlap >= 3:
                    # 命中过 · 复用它的 plan
                    p = r.get("plan", {})
                    return SearchPlan(**{k: v for k, v in p.items()
                                          if k in SearchPlan.__dataclass_fields__})
            except Exception:
                continue
    except Exception:
        pass
    return None


def _fallback_plan_by_regex(query: str) -> SearchPlan:
    """LLM 挂了 · 用规则兜底判 intent"""
    q = query.lower()
    has_zh = bool(re.search(r"[一-龥]", query))

    # 明显的信号词
    if any(w in q for w in ["最近", "最新", "2026", "本月", "本周",
                              "recent", "latest", "trend", "2026",
                              "this month", "this week"]):
        return _INTENT_TEMPLATES["trend_recent"]
    if any(w in q for w in ["真实", "经验", "月入", "踩坑", "副业",
                              "变现", "案例", "真人"]):
        return _INTENT_TEMPLATES["social_signal"]
    if any(w in q for w in ["评测", "评估", "综述", "对比", "方法论",
                              "深入", "分析", "evaluation", "survey",
                              "review", "comparison"]):
        return _INTENT_TEMPLATES["deep_research"]
    if any(w in q for w in ["error", "bug", "code", "framework", "sdk",
                              "api", "rust", "python", "javascript",
                              "install"]):
        return _INTENT_TEMPLATES["code_technical"]
    return _INTENT_TEMPLATES["fact_lookup"]


def plan(query: str, use_llm: bool = True,
         use_memory: bool = True, timeout: int = 15) -> SearchPlan:
    """给 query 生成 SearchPlan
    优先级:
      1. 记忆 · 相似 query 有历史成功 plan
      2. LLM haiku · 判 intent · 选 sources
      3. Fallback · regex 规则

    整个流程 < 15s (LLM 8s + 兜底 0s)
    """
    t0 = time.time()

    # 1. 记忆复用
    if use_memory:
        remembered = _try_recall_similar_plan(query)
        if remembered:
            remembered.reason = f"[memory] {remembered.reason}"
            _log_plan(asdict(remembered), query,
                       round(time.time() - t0, 2))
            return remembered

    # 2. LLM 判
    if use_llm:
        try:
            from audit_core import call_llm, try_parse_json
            user_msg = f"User query: {query[:500]}\n\nJSON plan:"
            # v13.93 · 实时读外部 SOP(可被 evo_skill_loop 自动改写)+ 拼接
            # search_meta_learning 学到的技巧(打通之前的断头闭环)
            sop_text = _load_planner_sop() + _load_learned_techniques()
            r = call_llm(sop_text, user_msg,
                          model_override=_MODEL, timeout=timeout)
            if r.get("ok"):
                parsed = try_parse_json(r.get("raw", ""), default={})
                if parsed.get("intent"):
                    # 装成 SearchPlan (只取合法字段)
                    valid_fields = SearchPlan.__dataclass_fields__.keys()
                    filtered = {k: v for k, v in parsed.items()
                                if k in valid_fields}
                    plan_obj = SearchPlan(**filtered)
                    _log_plan(asdict(plan_obj), query,
                               round(time.time() - t0, 2))
                    return plan_obj
        except Exception:
            pass

    # 3. Regex 兜底
    fp = _fallback_plan_by_regex(query)
    fp.reason = f"[regex] {fp.reason}"
    _log_plan(asdict(fp), query, round(time.time() - t0, 2))
    return fp


def plan_with_sop_text(query: str, sop_text: str | None, *,
                        timeout: int = 15) -> SearchPlan:
    """v13.93 · EvoSkill 专用:给定任意候选 SOP 文本(而非从磁盘默认路径读),
    跑一次纯 LLM 查询理解(不读记忆/不落 .search_plans.jsonl,避免评测阶段的
    探索污染生产记忆)。sop_text=None 时用内置 fallback(即"不受任何自定义
    SOP 影响"的基线),用于 evo_skill_loop 的双链路信号采集/回归检测/验证门控。
    """
    try:
        from audit_core import call_llm, try_parse_json
    except Exception:
        return _fallback_plan_by_regex(query)
    system = sop_text if sop_text else _PLANNER_SYSTEM_FALLBACK
    try:
        user_msg = f"User query: {query[:500]}\n\nJSON plan:"
        r = call_llm(system, user_msg, model_override=_MODEL, timeout=timeout)
        if r.get("ok"):
            parsed = try_parse_json(r.get("raw", ""), default={})
            if parsed.get("intent"):
                valid_fields = SearchPlan.__dataclass_fields__.keys()
                filtered = {k: v for k, v in parsed.items() if k in valid_fields}
                return SearchPlan(**filtered)
    except Exception:
        pass
    return _fallback_plan_by_regex(query)


def render_plan_as_text(query: str, plan_obj: SearchPlan) -> str:
    """v13.93 · 把 SearchPlan 渲成人可读文本,供 evo_skill_loop 的 Pairwise
    裁判比较两份检索计划谁更贴合 query(对应博客案例里"对比两条链路实际
    发出的搜索词/召回文档"的思路,这里比较的是"检索计划"而非"最终检索结果",
    成本更低、且直接反映 SOP 对 query understanding 阶段的影响)。"""
    lines = [
        f"Query: {query}",
        f"识别意图(intent): {plan_obj.intent}",
        f"检索源(sources_ordered): {', '.join(plan_obj.sources_ordered) or '(空)'}",
        f"深度分(depth_score): {plan_obj.depth_score}",
        f"是否用近30天过滤: {plan_obj.use_last30days}",
        f"是否并行多引擎: {plan_obj.use_3engines}",
        f"是否用学术源: {plan_obj.use_scholar}",
        f"是否走追搜循环: {plan_obj.use_supervise_loop} (最多 {plan_obj.max_iter} 轮)",
    ]
    if plan_obj.site_dorks:
        lines.append(f"动态 site: dork: {', '.join(plan_obj.site_dorks)}")
    lines.append(f"决策理由: {plan_obj.reason}")
    return "\n".join(lines)


def render_hits_as_text(query: str, plan_obj: SearchPlan, hits: list) -> str:
    """v13.93.1 · 把「检索计划 + 真实召回结果摘要」一起渲成文本,供 Pairwise
    裁判比较。之前 v13.93 版本只渲染了 plan 参数(sources_ordered/depth 等),
    没有真跑检索——这只能反映 SOP 让 planner「说了什么」,反映不了 SOP 让
    最终「召回了什么」,和博客里「对比两条链路实际召回的文档」这个核心
    判据不是一回事,是明显偷工减料。这版把真实召回结果的标题/摘要拼进去,
    让裁判真正比较素材质量,而不只是参数好不好看。"""
    lines = [f"Query: {query}", f"intent={plan_obj.intent} "
             f"sources={','.join(plan_obj.sources_ordered)}", ""]
    if not hits:
        lines.append("(本次检索无召回结果)")
    else:
        lines.append(f"召回 {len(hits)} 条素材:")
        for i, h in enumerate(hits[:8], 1):
            title = (h.get("title") or "")[:80]
            content = (h.get("content") or "")[:200]
            src = h.get("source") or "?"
            lines.append(f"{i}. [{src}] {title}\n   {content}")
    return "\n".join(lines)


def run_fn_for_evo_skill(query: str, sop_text: str | None, *,
                         use_real_search: bool = True,
                         search_timeout_s: int = 25) -> str:
    """v11/evolution/evo_skill_loop.py 的 run_fn 接口实现:
    (query, skill_text|None) -> answer(str)。

    v13.93.1 · 升级为真跑一次检索(SearchAgent.search),answer 是
    「检索计划 + 真实召回素材摘要」,而不是只有计划参数——否则 Pairwise
    裁判判的只是"哪份 plan 写得好看",不是"哪份 plan 实际召回更好的素材",
    信号强度和博客里"对比两条链路实际召回文档"的判据不对等。
    真实检索有网络耗时/失败风险,加超时+异常兜底,失败时退回只渲染 plan
    (v13.93 行为),保证闭环不会因为一次网络抖动就整体挂掉。"""
    plan_obj = plan_with_sop_text(query, sop_text)
    if not use_real_search:
        return render_plan_as_text(query, plan_obj)
    try:
        import concurrent.futures as _fut
        from search_agent import SearchAgent
        _sa = SearchAgent()
        with _fut.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_sa.search, query, mode="fast", top_k=8,
                            target_sources=plan_obj.sources_ordered or None)
            hits = fut.result(timeout=search_timeout_s)
        return render_hits_as_text(query, plan_obj, hits or [])
    except Exception:
        # fail-safe · 真实检索挂了不影响闭环跑下去·退回轻量版
        return render_plan_as_text(query, plan_obj)


def stats(days: int = 7) -> dict:
    """看 planner 学习状态"""
    if not os.path.exists(PLANS_LOG):
        return {"total": 0}
    cutoff = time.time() - days * 86400
    records = []
    try:
        with open(PLANS_LOG) as f:
            for line in f:
                try:
                    r = json.loads(line.strip())
                    if r.get("unix_ts", 0) >= cutoff:
                        records.append(r)
                except Exception:
                    continue
    except Exception:
        return {"total": 0}
    if not records:
        return {"total": 0, "days": days}
    by_intent = {}
    for r in records:
        i = r.get("plan", {}).get("intent", "?")
        by_intent[i] = by_intent.get(i, 0) + 1
    return {
        "total": len(records), "days": days,
        "by_intent": by_intent,
    }


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["plan", "stats", "smoke"])
        p.add_argument("query", nargs="?", default="RAG 评测怎么做")
        p.add_argument("--no-llm", action="store_true")
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "plan":
        plan_obj = plan(args.query, use_llm=not args.no_llm)
        print(json.dumps(asdict(plan_obj), ensure_ascii=False, indent=2))
    elif args.cmd == "smoke":
        # 5 类 query · 每类一个
        tests = [
            "RAG 是什么",  # fact
            "2026 AI 副业最新趋势",  # trend
            "如何深度评测 RAG 系统",  # deep
            "程序员副业月入真实经验",  # social
            "rust async web framework 2026",  # code
        ]
        for q in tests:
            p_obj = plan(q, use_llm=False)  # 只跑 regex 快
            print(f"[{p_obj.intent:<15}] {q}")
            print(f"    sources: {p_obj.sources_ordered}")
            print(f"    l30={p_obj.use_last30days} 3eng={p_obj.use_3engines} "
                  f"loop={p_obj.use_supervise_loop} iter={p_obj.max_iter}")
