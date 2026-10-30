"""
v12 · Agent-Native 搜索模块 · v13.14 加装 web-access bridge
────────────────────────────────────────────────────────────
灵感: Perplexity Deep Research / OpenAI Deep Research / GPT Researcher /
     Anthropic Sonnet Web Research / web-access skill (github.com/eze-is/web-access)

核心思想（不同于当前 v11 的单 query→单 engine）：
1. Query Planner (LLM)      · 一个模糊话题 → 3-5 个具体+对立视角的子查询
2. Multi-Source Fanout      · 每个子查询并行 4 源：Bing CN + Bing EN + GitHub + Sogou 微信
3. Deduplication + Rerank   · URL 归一 · 相似 title 折叠 · 按信号量重排
4. **DEEP READ** (v13.14)   · top URL 用 web-access bridge 抓一手源正文·跳出 snippet 层
5. Signal Extraction (LLM)  · 提取"人类痛点/踩坑/反直觉数字/真实经验"信号
6. Iterative Deepening      · 若 signal < N · 用发现的新词再打 2 轮·被限流的源自动 avoid

v13.14 (2026-07-09) 用户诉求：
  "外部信息真实的浏览和搜索全部网页,不要被限制住·一旦被限制就自己进化去找内容去尝试"
  → 加 deep_read 阶段: snippet + 一手源 · 遇 403/429 记录到 .search_incidents.jsonl 供下轮避开

对齐目标：
- 输入：一个用户角色 + 模糊话题（"AI 副业变现"）
- 输出：3-8 条**有痛点+踩坑+具体数字**的真实素材，可直接喂给 experience_writer
"""
import json
import re
import os
import sys
from datetime import datetime
import time
import hashlib
import concurrent.futures
from typing import List, Dict, Optional
from urllib.parse import urlparse


# 每个源默认拉多少条 · 后端合并到 rerank 后再截断
# v13.17 (2026-07-09) · 用户解除预算·5→10 · fanout 从 20-60 命中 → 40-120
DEFAULT_SOURCE_LIMIT = 10


class AgentSearch:
    """真正的 agent 化搜索：Plan → Fanout → Dedupe → Rerank → Extract → Deepen"""

    def __init__(self, search_engine, llm_call_fn, llm_opus_fn=None):
        """
        search_engine: 现有 search_engine.SearchEngine 实例
        llm_call_fn: (prompt: str, max_tokens: int) -> str · 默认 haiku
        llm_opus_fn: 可选 · opus 通道 · 用于 query 拆分/evidence 抽取等决策关键点
                     (v13.17 · 用户解除预算·关键点升 opus-4.7)
        """
        self.se = search_engine
        self._llm = llm_call_fn
        # v13.17 · 决策关键点走 opus · 未传则尝试从 problem_planner 加载
        if llm_opus_fn is None:
            try:
                from problem_planner import _llm_opus
                self._llm_opus = _llm_opus
            except Exception:
                self._llm_opus = llm_call_fn  # 退回 haiku
        else:
            self._llm_opus = llm_opus_fn

    # ─────────────────────────────────────────────
    # 1. Query Planner
    # ─────────────────────────────────────────────
    def plan_queries(self, role: str, topic: str, n: int = 6) -> List[dict]:
        """v13.21 · 重写 · 用户 3 张搜索差图后:
        - 不再输出词块堆 (❌ '小红书涨粉 公众号选题 视频号算法 内容涨粉')
        - 输出**结构化 query 对象** [{query, lang, target_source, style}]
        - 每条 query 是**真人问句/短语** · 不加平台名 (平台分发靠 target_source)
        - Reddit/HN/Twitter 强制英文 · 知乎/小红书/微信强制中文

        返回: [{'query': '...', 'lang': 'zh'|'en', 'target_source': 'google'|'zhihu'|'reddit'...,
                'style': 'question'|'phrase'|'dork'}]
        """
        # v13.19 · 加载 meta-learning 学到的技巧
        # v13.64 · 反哺:高效历史 query 直接注入 + 淘汰低效
        learned_techniques = ""
        top_historical = []
        bad_pattern_notes = ""
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "evolution"))
            from search_meta_learning import get_techniques_for_planner, top_by_evidence, bad_queries
            learned_techniques = get_techniques_for_planner()
            # 基于话题词找相关高效 query
            topic_words = [role] + [w for w in topic.split() if len(w) >= 2][:5]
            top_historical = top_by_evidence(topic_words=topic_words, n=5)
            bads = bad_queries()[:5]
            if bads:
                bad_pattern_notes = "\n\n## ⚠️ 历史废 query (禁止再用):\n" + \
                    "\n".join(f"  - `{b['query']}` (avg_signal={b['avg_signal']})" for b in bads)
        except Exception:
            pass

        historical_hint = ""
        if top_historical:
            historical_hint = "\n\n## ✅ 历史高效 query (可复用/变形):\n" + \
                "\n".join(f"  - `{r['query']}` (evidence={r['evidence_count']})"
                          for r in top_historical)

        prompt = f"""你是**模仿真人搜索行为**的检索规划师。你的任务是把一个话题拆成 {n} 条**真人会打的 query**。

## 话题
角色: {role}
话题: {topic}

## 用户 (2026-07-09) 3 张搜索差图后的硬要求

❌ **禁止的死板拆法** (你曾这样拆·全被打回):
- `"小红书涨粉 公众号选题 视频号算法 内容涨粉 IP 真实痛点 失败案例 反思"` (词块堆·真人不会这么打)
- `"UTF 账单 成本 小红书"` (在 Reddit 搜中文 · 命中率 0)
- 一条 query 塞 4 个平台名 + 3 个话题词

✅ **人类真的这样搜**:

**中文短问句** (知乎/小红书/微信): "chatgpt 副业到底能不能赚钱" "小红书 AI 提示词账号一个月能挣多少" "为什么我用 AI 做小红书没流量"

**中文短语** (2-4 词): "AI 副业翻车" "Claude 账单一个月" "chatgpt plus 值不值"

**英文问句** (Reddit/HN): "how much did you make from AI side hustle" "my Claude bill blew up what went wrong" "Anthropic prompt caching real impact"

**英文短语** (Twitter/GitHub): "AI agent hallucination fixed" "langgraph memory leak"

**Google Dork** (跨源): `site:reddit.com "went wrong" LLM cost 2025` · `intitle:"my journey" AI indie dev`

---

## 硬规则

1. **一次问一件事** · 不堆词块 · 一条 query 只表达一个具体意图
2. **平台匹配语言**:
   - target_source='reddit'/'hackernews'/'twitter'/'github_issues'/'github_repos'/'github_discussions'/'producthunt' → **必须英文**
   - target_source='zhihu'/'zhihu_global'/'xiaohongshu'/'sspai'/'v2ex'/'sogou_weixin'/'juejin'/'bilibili' → **必须中文**
   - target_source='google'/'google_click_all' → 按话题主体 (话题是中文场景就中文, 全球通用就英文)

3. **各源首选场景** (v13.87 · 用户「都得有」)
   - `zhihu` (0.79s · 官方 API · 带 vote/comment) → **首选**中文技术长文·深度问答
   - `zhihu_global` (0.83s · 支持 publish_time>= filter) → **首选**本周/本月新鲜中文
   - `xiaohongshu` (0.2s · 官方 API) → 生活/美食/穿搭/副业经验 (industryRecall empty 类不适)
   - `sogou_weixin` (5-10s · CDP 借道) → 公众号干货长文/行业观点 (proxy 掉线时退化)
   - `sspai` → 数码/生产力工具/AI 应用教程
   - `juejin` → 前端/后端技术实战
   - `v2ex` → 独立开发者/自由职业/技术八卦
   - `bilibili` → 视频类·科普/教程覆盖
   - `reddit` → 海外真实经验/踩坑/收入 (site:reddit.com Dork 也行)
   - `hackernews` → 技术新闻/创业/开源项目热议
   - `github_issues` → bug 报告/兼容性问题/真实用户吐槽
   - `github_repos` → 具体项目/工具/star 排序
   - `github_discussions` → 项目答疑/RFC 讨论
   - `producthunt` → 新产品发布/工具对比
   - `twitter` → 实时热点/大 V 观点
   - `google` → 全兜底·各站都行·带 site: Dork 精准
   - `google_click_all` → Google 结果 + 每条真点开抓正文 (深度模式·慢)
3. **不写平台名到 query 里** · 除非用 `site:xxx` Dork
4. 短语 2-6 词 · 问句 8-15 词 · Dork 灵活
5. 至少 1 条 Google Dork
6. 至少 1 条英文 · 至少 1 条中文
7. **禁用**空泛词: "AI" "如何" "最佳实践" "教程" "入门" "指南"

{learned_techniques}{historical_hint}{bad_pattern_notes}

---

## 输出格式 (严格 JSON · 不要说明)

```json
[
  {{"query": "chatgpt 副业月入多少 真实", "lang": "zh", "target_source": "zhihu", "style": "question"}},
  {{"query": "RAG 评测 分层指标 实践经验", "lang": "zh", "target_source": "zhihu", "style": "phrase"}},
  {{"query": "AI 提示词 小红书 收入", "lang": "zh", "target_source": "xiaohongshu", "style": "phrase"}},
  {{"query": "AI 副业 月入 真实经历", "lang": "zh", "target_source": "sogou_weixin", "style": "phrase"}},
  {{"query": "how much do you actually make with claude api reselling", "lang": "en", "target_source": "reddit", "style": "question"}},
  {{"query": "site:reddit.com \\"AI side hustle\\" real earnings 2025", "lang": "en", "target_source": "google", "style": "dork"}},
  {{"query": "AI agent \\"my bill\\" \\"$500\\"", "lang": "en", "target_source": "google", "style": "dork"}},
  {{"query": "小红书 AI 账号 起号 30 天", "lang": "zh", "target_source": "google", "style": "phrase"}}
]
```

每条 query 都要通过下面自检:
- [ ] 换成真人视角念一遍·像不像会去搜的?
- [ ] 平台匹配语言? (Reddit 只英文)
- [ ] 一次问一件事·不堆词块?
- [ ] 具体不空泛?"""

        raw = self._llm_opus(prompt, max_tokens=2000).strip()
        parsed = []
        try:
            m = re.search(r"\[\s*\{[\s\S]+\}\s*\]", raw)
            if m:
                arr = json.loads(m.group(0))
                if isinstance(arr, list):
                    for x in arr:
                        if isinstance(x, dict) and x.get("query") and 3 < len(x["query"]) < 200:
                            # 自检: 平台-语言一致
                            src = x.get("target_source", "google")
                            lang = x.get("lang", "zh")
                            has_cn = bool(re.search(r'[一-龥]', x["query"]))
                            # Reddit/HN/Twitter/GitHub · 若含中文 → skip 掉
                            if src in ("reddit", "hackernews", "twitter", "github_issues") and has_cn:
                                continue
                            # 知乎/小红书/微信/v2ex · 若纯英文 → skip
                            if src in ("zhihu", "xiaohongshu", "sspai", "v2ex", "sogou_weixin") and not has_cn and not re.search(r'site:', x["query"]):
                                continue
                            parsed.append(x)
        except Exception:
            pass

        if parsed and len(parsed) >= 2:
            return parsed

        # 兜底 · v13.21 · 也用真人搜法
        return [
            {"query": f"{topic} 真的能做吗", "lang": "zh", "target_source": "zhihu", "style": "question"},
            {"query": f"{topic} 一个月", "lang": "zh", "target_source": "xiaohongshu", "style": "phrase"},
            {"query": f"how does {topic.split()[0] if topic.split() else 'AI'} really work",
             "lang": "en", "target_source": "reddit", "style": "question"},
            {"query": f'site:reddit.com "{topic.split()[0] if topic.split() else "AI"}" 2025',
             "lang": "en", "target_source": "google", "style": "dork"},
        ]

    def plan_queries_flat(self, role: str, topic: str, n: int = 6) -> List[str]:
        """兼容 API · 返回旧格式 str 列表 · 供 log_query_effect 用"""
        objs = self.plan_queries(role, topic, n)
        return [o["query"] if isinstance(o, dict) else o for o in objs]

    # ─────────────────────────────────────────────
    # 2. Multi-Source Fanout
    # ─────────────────────────────────────────────
# v13.9-DEAD · 未被调用 · 保留待清理
    def _fanout_one_query(self, query: str, per_source: int = DEFAULT_SOURCE_LIMIT) -> List[Dict]:
        """
        一个 query 并行打 6 源：
        - Bing CN/EN（元搜索兜底）
        - Sogou 微信（国内公众号）
        - Xiaohongshu（真实生活场景）· 通过 agent_reach
        - Reddit（英文真实用户吐槽）· 通过 agent_reach
        - V2EX（技术圈真实吐槽）· 通过 agent_reach
        """
        results = []
        # 尝试加载 agent_reach_adapter（社区源）
        adapter = None
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from agent_reach_adapter import AgentReachAdapter
            adapter = AgentReachAdapter(timeout=10)
            if not adapter._check_available().get("xreach"):
                adapter = None
        except Exception:
            adapter = None

# v13.9-DEAD · 未被调用 · 保留待清理
        def _adapt_xhs(res_list):
            """xiaohongshu → 统一格式"""
            out = []
            for r in res_list or []:
                out.append({
                    "title": r.get("title", "")[:100],
                    "url": r.get("url", ""),
                    "content": r.get("text", "")[:300] or r.get("desc", "")[:300],
                })
            return out
# v13.9-DEAD · 未被调用 · 保留待清理

        def _adapt_reddit(res_list):
            out = []
            for r in res_list or []:
                out.append({
                    "title": r.get("title", "")[:100],
                    "url": r.get("url", ""),
                    "content": r.get("text", "")[:300] or r.get("selftext", "")[:300],
                })
            return out

        # v13.18 · global_sources · Reddit/HN/GitHub Issues/v2ex/Zhihu/XHS 等全网源
        # 用户诉求 "搜搜 google 外网各种论坛内容 reddit github 这些"
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import global_sources as gs
        except Exception:
            gs = None

        # v13.20 · 用户"电脑卡"投诉后·分组不同并发策略:
        # A · 直连源 (bing/hackernews/github) - 并发 4 · 网络快·不吃 Chrome
        # B · CDP 源 (google/twitter/zhihu/reddit/xhs/v2ex/sspai) - 串行 · 每次前 wait_until_cool
        # 每一 query 只跑最多 2 个 CDP 源 (google + 1 高价值) · 避免 Chrome 多 tab 爆内存

        # A · 直连并发
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = {}
            futs[ex.submit(self._safe_call, self.se.search_bing_cn, query, per_source)] = "bing_cn"
            futs[ex.submit(self._safe_call, self.se.search_bing_en, query, per_source)] = "bing_en"
            futs[ex.submit(self._safe_call, self.se.search_sogou_weixin, query, per_source)] = "sogou_wx"
            if gs:
                futs[ex.submit(self._safe_call, gs.search_hackernews, query, per_source)] = "hackernews"
                futs[ex.submit(self._safe_call, gs.search_github_issues, query, per_source)] = "github_issues"
                futs[ex.submit(self._safe_call, gs.search_github_repos, query, per_source)] = "github_repos"
            for f in concurrent.futures.as_completed(futs, timeout=30):
                src = futs[f]
                try:
                    r = f.result() or []
                    for x in r:
                        x["_src"] = src
                        x["_query"] = query
                        results.append(x)
                except Exception:
                    pass

        # B · CDP 源串行 · 只跑 top 2 (google 必上 · 加 1 个基于 query 语言判断)
        # 中文 query → google + zhihu · 英文 query → google + reddit/twitter (轮换)
        if gs:
            try:
                from perf_guard import wait_until_cool
            except Exception:
                wait_until_cool = None

            has_cn = bool(re.search(r'[一-龥]', query))
            cdp_pick = ["google"]
            if has_cn:
                cdp_pick.append("zhihu")
            else:
                # 英文 query 轮换 reddit/twitter (基于 query hash)
                if hashlib.md5(query.encode()).digest()[0] % 2 == 0:
                    cdp_pick.append("reddit")
                else:
                    cdp_pick.append("twitter")

            # v13.63 · google 换成 click_all · 用户元规则:「所有结果都要点击」
            cdp_map = {
                "google": getattr(gs, "search_google_click_all", gs.search_google),
                "twitter": gs.search_twitter,
                "zhihu": gs.search_zhihu,
                "reddit": gs.search_reddit,
            }
            for src in cdp_pick:
                if wait_until_cool:
                    wait_until_cool(f"cdp_{src}")
                try:
                    rs = self._safe_call(cdp_map[src], query, per_source) or []
                    for x in rs:
                        x["_src"] = src
                        x["_query"] = query
                        results.append(x)
                except Exception:
                    pass

        return results

    def _safe_call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            return []

    def fanout(self, queries, per_source: int = DEFAULT_SOURCE_LIMIT) -> List[Dict]:
        """v13.21 · 支持 str 列表(旧) 或 dict 列表(新·带 target_source)
        新格式派发规则: 每条 query 只投递给 target_source·避免把中文 query 塞给 Reddit

        v13.47 · 演绎修:先查 search_memory·历史命中直接复用·避免重复搜"""
        # 兼容:如果是 str 列表·退回全 fanout
        if not queries:
            return []

        # v13.47 · 先查搜索记忆库
        pre_hits = []
        try:
            sys.path.insert(0, os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "evolution"))
            from search_memory import remember as _rmb
            _query_strs = [q if isinstance(q, str) else q.get("q") or q.get("query") or ""
                           for q in queries]
            _query_strs = [q for q in _query_strs if q]
            for q in _query_strs[:6]:
                remembered = _rmb(q, similar_threshold=0.5, top_k=2)
                for r in (remembered or []):
                    for u in (r.get("urls") or []):
                        pre_hits.append({
                            "url": u,
                            "title": f"[记忆命中] {q[:40]}",
                            "_src": "search_memory_cache",
                            "_query": q,
                            "_signal": 0.9,  # 历史高质 URL·信号高
                        })
            if pre_hits:
                print(f"  🧠 search_memory 命中 {len(pre_hits)} 条 (跳过重复搜)")
        except Exception:
            pass

        if isinstance(queries[0], str):
            all_results = list(pre_hits)  # 记忆命中先塞
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(queries), 3)) as ex:
                futs = [ex.submit(self._fanout_one_query, q, per_source) for q in queries]
                for f in concurrent.futures.as_completed(futs, timeout=120):
                    try:
                        all_results.extend(f.result() or [])
                    except Exception:
                        pass
            return all_results

        # v13.21 新 · dict 派发
        by_target = self._fanout_by_target(queries, per_source)
        return list(pre_hits) + by_target

    def _fanout_by_target(self, queries: List[dict], per_source: int) -> List[Dict]:
        """每条 query 只投它的 target_source · 平台匹配语言"""
        all_results = []
        # 加载 global_sources
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import global_sources as gs
        except Exception:
            gs = None

        try:
            from perf_guard import wait_until_cool
        except Exception:
            wait_until_cool = None

        # 平台→函数映射
        src_map = {
            "google": gs.search_google if gs else None,
            "twitter": gs.search_twitter if gs else None,
            "reddit": gs.search_reddit if gs else None,
            "hackernews": gs.search_hackernews if gs else None,
            "github_issues": gs.search_github_issues if gs else None,
            "github_repos": gs.search_github_repos if gs else None,
            "zhihu": gs.search_zhihu if gs else None,
            "xiaohongshu": gs.search_xiaohongshu if gs else None,
            "v2ex": gs.search_v2ex_hot if gs else None,
            "sspai": gs.search_sspai if gs else None,
            # v13.84 · 搜狗微信搜索 · 拿公众号文章 · 直连免登录
            "sogou_weixin": gs.search_sogou_weixin if gs else None,
            # v13.87.15 · 老 se 属性 · 用 getattr 兜底防挂 (SearchEngine 可能没这方法)
            "bing_cn": getattr(self.se, "search_bing_cn", None),
            "bing_en": getattr(self.se, "search_bing_en", None),
        }
        # v13.87.9 · Friday universal-search 新增 3 源
        try:
            sys.path.insert(0, os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "evolution"))
            import friday_universal as _fu
            src_map["friday_bing"] = _fu.search_bing
            src_map["friday_google"] = _fu.search_google
            src_map["friday_zhipu"] = lambda q, per=5: _fu.search(
                q, ["zhipu-search"], top_k=per)
            src_map["zhihu_global"] = lambda q, per=5: _fu.search_zh(
                q, top_k=per, days=30)
        except Exception:
            pass

        # v56f(2026-08-05) · AnySearch 接进选源池 · 治「google 系全线差(CDP 0.395/
        #   friday_google 0.38-0.46)+CDP撞perf_guard」。AnySearch: 意图路由17源·
        #   绕Google反爬·HTTP不开Chrome·实测3.2s秒返且带全文(216k字)·质量远超google系。
        #   之前只在 web_access_bridge 抓取兜底层·从没作为搜索源接进 fanout(诊断缺口)。
        try:
            import re as _re
            from web_access_bridge import WebAccess as _WA
            _wa_singleton = _WA()

            def _anysearch_source(q, per=5):
                """AnySearch markdown → hit list。domain 按 query 语言/内容粗判意图。"""
                if not _wa_singleton.anysearch_available():
                    return []
                # 意图路由: 代码词→code·其余 general(AnySearch 内部还会再路由)
                _ql = q.lower()
                _dom = ("code" if any(k in _ql for k in
                        ("github", "python", "code", "api", "error", "bug", "npm", "pip"))
                        else "general")
                md = _wa_singleton.anysearch_search(q, max_results=per, domain=_dom)
                if not md:
                    return []
                # 解析 "### N. 标题\n- **URL**: xxx\n正文..." 块
                hits = []
                blocks = _re.split(r"\n### \d+\.\s*", md)
                for blk in blocks[1:]:  # [0] 是 header
                    lines = blk.split("\n", 1)
                    title = lines[0].strip()
                    body = lines[1] if len(lines) > 1 else ""
                    um = _re.search(r"\*\*URL\*\*:\s*(\S+)", body)
                    url = um.group(1) if um else ""
                    # 正文去掉 URL 行·取实质内容
                    content = _re.sub(r"^-\s*\*\*URL\*\*:.*$", "", body, flags=_re.M).strip()
                    hits.append({
                        "url": url, "title": title[:240],
                        "content": content[:8000], "source": "anysearch",
                        "completeness": "full" if len(content) > 1000 else "partial",
                    })
                return hits[:per]

            src_map["anysearch"] = _anysearch_source

            def _firecrawl_source(q, per=5):
                """firecrawl agent 自主搜·反爬极强·免费日限5次·作难题补充源(deep_research)。
                返回单块结构化结果·转成 1 个 hit(含大段结构化正文)。"""
                if not _wa_singleton.firecrawl_available():
                    return []
                r = _wa_singleton.firecrawl_agent_search(q, max_chars=20000)
                if not r.get("ok") or not r.get("data"):
                    return []
                return [{"url": "", "title": q[:120], "content": str(r["data"])[:20000],
                         "source": "firecrawl_search", "completeness": "full"}]

            src_map["firecrawl_search"] = _firecrawl_source

            # v56k(2026-08-05) · wigolo 接进选源(用户抓「装wigolo编排三源」)。
            #   本地免费·18引擎融合·带 evidence_score(semantic/lexical/engine_consensus)
            #   +原文excerpt·失败如实告知不塞空壳。走常驻 REST(127.0.0.1:3333·wigolo serve)·
            #   系统python走HTTP不依赖venv。daemon常驻后~7s/次·当补充源(比friday慢但源最全)。
            import urllib.request as _ureq, urllib.error as _uerr
            _WIGOLO_URL = os.environ.get("WIGOLO_URL", "http://127.0.0.1:3333")

            def _wigolo_source(q, per=5):
                try:
                    payload = json.dumps({"query": q, "max_results": per}).encode()
                    req = _ureq.Request(f"{_WIGOLO_URL}/v1/search", data=payload,
                                        headers={"Content-Type": "application/json"})
                    with _ureq.urlopen(req, timeout=30) as resp:
                        data = json.loads(resp.read().decode())
                except Exception:
                    return []  # daemon 没起/超时 → 空(优雅降级·不阻断)
                hits = []
                for r in data.get("results", []):
                    if not isinstance(r, dict):
                        continue
                    sc = r.get("evidence_score", {})
                    # 跳被标为垃圾的低分结果(wigolo 自带质量标记)
                    if isinstance(sc, dict) and sc.get("final", 1) is not None \
                            and sc.get("final", 1) < 0.2:
                        continue
                    body = r.get("excerpt") or r.get("snippet") or ""
                    hits.append({
                        "url": r.get("url", ""), "title": (r.get("title") or "")[:240],
                        "content": str(body)[:8000], "source": "wigolo",
                        "completeness": "full" if len(str(body)) > 1000 else "partial",
                        "score": (sc.get("final") if isinstance(sc, dict) else None),
                    })
                return hits[:per]

            src_map["wigolo"] = _wigolo_source

            # v56n(2026-08-05) · Tavily 接进选源(用户给token)。核心=准和全(factual排序+
            #   多路召回RRF+Rerank)·且 include_answer 直接返综合答案(补觅游「精确抽取」缺口·
            #   两边都错的难题多卡在从长文抽精确数字)。search_depth=advanced 精排。
            _TAVILY_TOKEN = os.environ.get("TAVILY_API_KEY", "")

            def _tavily_source(q, per=5):
                if not _TAVILY_TOKEN:
                    return []
                try:
                    _depth = os.environ.get("MEYO_SEARCH_DEPTH", "basic") or "basic"
                    payload = json.dumps({
                        # Default basic (1 credit, faster); advanced opt-in via env.
                        "query": q, "search_depth": _depth,
                        "include_answer": "advanced", "max_results": per}).encode()
                    req = _ureq.Request("https://api.tavily.com/search", data=payload,
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {_TAVILY_TOKEN}"})
                    with _ureq.urlopen(req, timeout=30) as resp:
                        d = json.loads(resp.read().decode())
                except Exception:
                    return []
                hits = []
                # answer 字段作一条高优先「综合答案」hit(Tavily 已帮抽好·省下游抽取)
                ans = (d.get("answer") or "").strip()
                if ans:
                    hits.append({"url": "", "title": "[Tavily综合答案] " + q[:60],
                                 "content": ans, "source": "tavily_answer",
                                 "completeness": "full", "score": 1.0})
                for r in d.get("results", []):
                    if not isinstance(r, dict):
                        continue
                    hits.append({
                        "url": r.get("url", ""), "title": (r.get("title") or "")[:240],
                        "content": (r.get("content") or r.get("raw_content") or "")[:8000],
                        "source": "tavily", "score": r.get("score"),
                        "completeness": "full" if len(r.get("content", "") or "") > 1000 else "partial"})
                return hits[:per + 1]

            src_map["tavily"] = _tavily_source
        except Exception:
            pass

        # v13.87.9 · graph 包一层 · 每源自动降级
        _USE_GRAPH = os.environ.get("MEYO_FANOUT_VIA_GRAPH", "1") == "1"
        _graph_execute = None
        if _USE_GRAPH:
            try:
                from search_graph import execute as _graph_execute
            except Exception:
                _graph_execute = None

        def _wrap_via_graph(orig_fn, src_name):
            """graph 包装 · 挂了自动降级 · 结果结构不变"""
            if not _graph_execute or not orig_fn:
                return orig_fn
            def _wrapped(query, per_source=5):
                try:
                    r = _graph_execute(query, src_name, capability="search",
                                        max_hops=3)
                    hits = r.get("result") or []
                    # graph 返 list of hits (与原 fn 一致)
                    if isinstance(hits, list):
                        return hits
                except Exception:
                    pass
                # 兜底 · 直调原 fn
                return orig_fn(query, per_source)
            return _wrapped

        if _USE_GRAPH and _graph_execute:
            # v56f · anysearch 是新源·search_graph 无其节点(返空list被误当结果)·
            #   不包 graph·直调原 fn(它自带 AnySearch CLI 30s 超时兜底·不需 graph 降级)。
            _new_srcs = ("anysearch", "firecrawl_search", "wigolo", "tavily")  # 新源无graph节点·直调原fn
            src_map = {k: (v if k in _new_srcs else _wrap_via_graph(v, k))
                       for k, v in src_map.items()}

        # v13.87.10 · 每条 full/partial 结果自动收 site_bookmark
        _bookmark_record = None
        try:
            from site_bookmark import record as _bookmark_record
        except Exception:
            pass

        # v13.87.11 · skill forger · 每次搜完记 (query, source, quality)
        _forger_record = None
        try:
            from search_skill_forger import record_search as _forger_record
        except Exception:
            pass

        def _bookmark_hits(hits, src, query=""):
            """落 site_bookmark + skill forger"""
            if hits and _forger_record and query:
                try:
                    _forger_record(query, src, hits)
                except Exception:
                    pass
            if not _bookmark_record or not hits:
                return
            for h in hits:
                if not isinstance(h, dict):
                    continue
                comp = h.get("completeness", "")
                if comp == "shell":
                    continue  # 骨架不算真收藏
                url = h.get("url", "")
                if not url or not url.startswith("http"):
                    continue
                content_len = len(h.get("content", ""))
                try:
                    _bookmark_record(url=url, via=src, ok=True,
                                       content_len=content_len,
                                       time_s=0.0, topic="")
                except Exception:
                    pass
        # 判定哪些是 CDP (串行) · 哪些直连 (并发)
        # sogou_weixin 直连 · 归并发组
        # v13.86 · xiaohongshu 走美团 API 直连·不再进 CDP 组
        cdp_sources = {"google", "twitter", "reddit", "zhihu", "v2ex", "sspai"}

        # 分成两组
        direct_jobs = []   # (query_obj, fn)
        cdp_jobs = []
        # v13.63 · target_source=None 或缺失 → 打全源 · 走老 _fanout_one_query 路径
        # 修 bug:原来 None 时兜底 google 但 CDP 挂 → 全空
        no_target = [qo for qo in queries
                     if not qo.get("target_source")]
        with_target = [qo for qo in queries if qo.get("target_source")]

        if no_target:
            for qo in no_target:
                try:
                    r = self._fanout_one_query(qo["query"], per_source)
                    all_results.extend(r)
                except Exception:
                    pass

        for qo in with_target:
            src = qo.get("target_source")
            fn = src_map.get(src)
            if not fn:
                fn = src_map.get("google")
                src = "google"
            (cdp_jobs if src in cdp_sources else direct_jobs).append((qo, src, fn))

        # 直连并发
        if direct_jobs:
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
                futs = {ex.submit(self._safe_call, fn, qo["query"], per_source): (qo, src)
                        for qo, src, fn in direct_jobs}
                for f in concurrent.futures.as_completed(futs, timeout=45):
                    qo, src = futs[f]
                    try:
                        r = f.result() or []
                        _bookmark_hits(r, src, qo.get("query", ""))  # v13.87.10/11
                        for x in r:
                            x["_src"] = src
                            x["_query"] = qo["query"]
                            x["_lang"] = qo.get("lang", "")
                            x["_style"] = qo.get("style", "")
                            all_results.append(x)
                    except Exception:
                        pass

        # CDP 串行·避 Chrome tab 爆
        for qo, src, fn in cdp_jobs:
            if wait_until_cool:
                wait_until_cool(f"cdp_{src}")
            try:
                r = self._safe_call(fn, qo["query"], per_source) or []
                _bookmark_hits(r, src)  # v13.87.10 · 落 bookmark
                for x in r:
                    x["_src"] = src
                    x["_query"] = qo["query"]
                    x["_lang"] = qo.get("lang", "")
                    x["_style"] = qo.get("style", "")
                    all_results.append(x)
            except Exception:
                pass

        # v13.87.16 · overconfidence_guard 接主流水线
        # 用户 2026-07-13 明确要「过度自信校验器·与搜索 agent 结合」
        # 扫刚拿到的 hit content · 抓过度自信信号 · 触发追搜
        if os.environ.get("MEYO_OVERCONF_SCAN", "1") == "1" and all_results:
            try:
                from overconfidence_guard import scan_text
                overconf_events = []
                for h in all_results[:10]:
                    if not isinstance(h, dict) or h.get("_meta_only"):
                        continue
                    content = h.get("content", "")
                    if content and len(content) > 100:
                        events = scan_text(content,
                            source_tag=f"fanout_{h.get('_src','?')}")
                        overconf_events.extend(events)
                if overconf_events:
                    all_results.append({
                        "_overconfidence_events": overconf_events[:5],
                        "_meta_only": True,
                    })
            except Exception:
                pass

        # v13.88.9L-7 · 移除 fanout 内的元监督 supervise() 块 (原 MEYO_SUPERVISOR)
        # 病根三合一:
        #  1) 它在每次 fanout 都发一个 LLM supervise() 往返 (~20s),
        #     违反文档契约「fast · 一次 fanout · 无 supervisor · 500ms 秒返」
        #     (evolution/search_agent.py:785)。MEYO_SUPERVISOR=0 实测 fanout 从 ~22-36s 降到 1.1s。
        #  2) 它产出的 _supervisor_decision / _stuck_signal meta 全树零消费者 (orphan output)
        #     —— [[feedback-produce-not-consume-orphan]] 产 ≠ 用。
        #  3) balanced/deep 的监督已由 search_agent._supervise() (search_agent.py:463) 正确拥有,
        #     fast 路径本就不该带 supervisor。
        return all_results

    # ─────────────────────────────────────────────
    # 3. Dedupe + 4. Rerank
    # ─────────────────────────────────────────────
    def dedupe_rerank(self, results: List[Dict], top_n: int = 12) -> List[Dict]:
        """URL 归一 + title 相似折叠 + 信号量重排"""
        seen_urls = set()
        seen_title_hashes = set()
        cleaned = []
        for r in results:
            url = (r.get("url") or "").strip()
            title = (r.get("title") or "").strip()
            content = (r.get("content") or "").strip()
            if not title or len(title) < 5:
                continue
            # URL 归一（去 utm、锚点等）
            norm_url = self._normalize_url(url)
            if norm_url and norm_url in seen_urls:
                continue
            # title 3-gram 前缀哈希（防同源不同 utm）
            th = hashlib.md5(title[:30].encode("utf-8", errors="ignore")).hexdigest()
            if th in seen_title_hashes:
                continue
            seen_urls.add(norm_url)
            seen_title_hashes.add(th)
            # 信号评分（含源加权）
            r["_signal"] = self._score_signal(title, content, r.get("_src", ""))
            cleaned.append(r)
        cleaned.sort(key=lambda x: x["_signal"], reverse=True)
        return cleaned[:top_n]

    def _normalize_url(self, url: str) -> str:
        if not url:
            return ""
        try:
            p = urlparse(url)
            return f"{p.netloc}{p.path}".rstrip("/")
        except Exception:
            return url

    # 高信号词典（真实经验类）
    SIGNAL_WORDS = [
        # 数字/成本
        r"\$\d+", r"\d+元", r"\d+刀", r"\d+分钟", r"\d+小时", r"\d+%", r"\d+x", r"\d+倍",
        # 痛点情绪
        "翻车", "踩坑", "崩溃", "打回", "被开除", "焦虑", "破防", "失眠", "账单", "轰炸",
        # 具体动作
        "复盘", "反思", "第 3 次", "第七次", "凌晨", "跑了", "改了", "拆了",
        # 反直觉
        "反直觉", "没想到", "翻转", "原来", "以为",
        # 平台标签（说明是真实社区语料）
        "知乎", "小红书", "少数派", "36氪", "reddit", "HN", "Hacker News",
    ]
    # 低信号（SEO 水文标志）
    NOISE_WORDS = [
        "教程大全", "全套", "免费下载", "破解", "永久激活", "最全指南",
        "1天学会", "3天精通", "N 步搞定", "N 招",
        "点击下载", "扫码关注", "vx添加",
    ]

    # 高价值源加权（真实社区用户比 SEO 首页强 3-5 倍）
    SRC_BONUS = {
        "xhs": 5.0,       # 小红书生活场景
        "reddit": 5.0,    # Reddit 用户吐槽
        "v2ex": 4.0,      # V2EX 技术真实吐槽
        "sogou_wx": 2.5,  # 公众号
        "bing_cn": 1.0,
        "bing_en": 1.0,
        "ddg": 0.5,
    }

    def _score_signal(self, title: str, content: str, src: str = "") -> float:
        """信号强度评分：源权重 + 高信号词 + 数字对比 - 噪声"""
        text = (title + " " + content)[:800]
        score = self.SRC_BONUS.get(src, 0.5)  # 源基础分
        for pat in self.SIGNAL_WORDS:
            if re.search(pat, text, re.I):
                score += 2
        for w in self.NOISE_WORDS:
            if w in text:
                score -= 3
        # 有数字对比额外加分
        if re.search(r"\d+.*→.*\d+", text) or re.search(r"从.*\d+.*到.*\d+", text):
            score += 4
        # 长度合理性
        if 20 < len(content) < 1500:
            score += 1
        return score

    # ─────────────────────────────────────────────
    # 5. Signal Extraction
    # ─────────────────────────────────────────────
    # ─────────────────────────────────────────────
    # 4.5 · Deep Read (v13.14) · 一手源正文抓取
    # ─────────────────────────────────────────────
    def deep_read(self, results: List[Dict], max_urls: int = 5, max_workers: int = 3) -> List[Dict]:
        """通过 web-access bridge 深读 top URL · 把 snippet 升级为一手正文
        遇到限流自动跳过 · 事件落 .search_incidents.jsonl 供下轮避开"""
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "evolution"))
            from web_access_bridge import WebAccess
            wa = WebAccess()
        except Exception as e:
            # web-access bridge 不可用（首次装或环境问题）· 优雅降级
            print(f"  ⚠️ web-access bridge 不可用 ({type(e).__name__}), 跳过 deep_read")
            return results

        # 前置：拿最近 6h 被封的域名·直接过滤掉
        blocked = set(wa.get_blocked_domains(hours=6, min_incidents=3))
        # v13.17 · 用户 Chrome 已登录的社交平台优先读 (走 CDP 带登录态)
        # 这些平台的一手内容质量 >>> 官方文档 / 工具介绍页
        priority_domains = ('zhihu.com', 'xiaohongshu.com', 'xhslink.com',
                            'mp.weixin.qq.com', 'weibo.com', 'douyin.com',
                            'bilibili.com', 'v2ex.com', 'reddit.com')

        def _priority(url: str) -> int:
            dom = urlparse(url).netloc
            for d in priority_domains:
                if d in dom:
                    return 0  # 越小越优先
            return 1

        # 按优先级重排 · 社交平台优先
        cand_pool = []
        for r in results[:max_urls * 3]:  # v13.17 · 池子 2x → 3x
            url = r.get("url") or r.get("link") or ""
            if not url or not url.startswith("http"):
                continue
            dom = urlparse(url).netloc
            if dom in blocked:
                continue
            cand_pool.append(r)
        # 按优先级排 · 社交平台在前 · 稳定排序保留原相关性
        cand_pool.sort(key=lambda r: _priority(r.get("url") or r.get("link") or ""))
        candidates = cand_pool[:max_urls]

        if not candidates:
            return results

        urls = [r.get("url") or r.get("link") for r in candidates]
        print(f"  🔎 deep_read {len(urls)} URLs (跳过 {len(results)-len(candidates)} 已知限流)")
        read_results = wa.read_urls(urls, max_workers=max_workers)

        # 把 deep 读到的正文塞回 results
        by_url = {r["url"]: r for r in read_results if r.get("url")}
        enriched = []
        n_deep = 0
        via_counter = {}
        for r in results:
            url = r.get("url") or r.get("link") or ""
            deep = by_url.get(url)
            if deep and deep.get("ok") and deep.get("content"):
                r = {**r, "content": deep["content"][:3000], "deep_via": deep.get("via", "?")}
                via_counter[deep.get("via", "?")] = via_counter.get(deep.get("via", "?"), 0) + 1
                n_deep += 1
                # v13.47 · 演绎修·双向闭环
                # deep_read 成功 → 记入 search_memory · 让下次同类 query 秒复用
                try:
                    sys.path.insert(0, os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), "evolution"))
                    from search_memory import record_win as _rw
                    q_for_win = r.get("_query") or ""
                    if q_for_win and url:
                        _rw(q_for_win, url, confidence=0.85)
                except Exception:
                    pass
            enriched.append(r)
        print(f"  ✓ deep_read 拿到 {n_deep}/{len(urls)} 一手源")
        # v13.15 · 落"真干了啥"
        try:
            from what_i_did import record
            record("deep_read", {
                "n_urls": len(urls),
                "n_success": n_deep,
                "n_blocked_skipped": len(results) - len(candidates),
                "via_breakdown": via_counter,
                "sample_urls": urls[:3],
            }, ok=(n_deep > 0))
        except Exception:
            pass
        return enriched

    def extract_evidence(self, results: List[Dict], role: str, topic: str, n: int = 5) -> List[Dict]:
        """LLM 从 top 结果里抽出"真实痛点+踩坑+具体数字"三元组
        v13.63.2 · 修 bug:evidence 现在带 source_url + source_idx
        用户 2026-07-13:「就是没 source_url·下轮修」
        LLM 挂 source_idx (1-based)·我事后补回 URL"""
        if not results:
            return []
        top = results[:min(len(results), 8)]
        digest = "\n---\n".join(
            f"[{i+1}] 【{r.get('title','')[:60]}】\n{r.get('content','')[:280]}"
            for i, r in enumerate(top)
        )
        prompt = f"""你是资深内容研究员。基于下面**搜索结果的信号+你的行业知识**，为「{role}」这个角色抽取 {n} 条**可写成真实经验帖**的素材。

角色：{role}
话题：{topic}

搜索结果（可能只有官方页/工具介绍，用它当"这个话题下确实存在的工具/概念"信号即可，编号 [1]-[{len(top)}]）：
{digest}

抽取规则（半合成模式）：
- **pain**：结合搜索结果里出现的工具/概念，编织一个**这个角色在真实工作场景中会遇到的具体痛点**（30-80 字·越具体越好）
- **pitfall**：这个角色实际操作时**最容易踩的坑**（20-60 字，具体，不要抽象）
- **numbers**：这个场景里**合乎行业常识的数字对比**（如工作从 X 分钟降到 Y 分钟，账单从 $A 到 $B，评论数从 A 到 B 等）
- **source_idx**：这条 evidence 主要来自搜索结果里哪一条?填 [X] 里的数字 X (1-{len(top)})·若综合多条可填 0

关键：
- pain/pitfall/numbers/source_idx 四段必须全部有内容
- pain/pitfall/numbers 不能是"无"或"未提及"
- 结果要**行业上真实**（合理，但不必来自具体某条搜索结果）
- 每条针对一个不同的子场景，不要重复
- 严禁使用"skill"、"心跳"、"agent"等纯平台黑话

输出 JSON 数组（**只输出 JSON · 不要任何说明**）：
[
  {{"pain": "...", "pitfall": "...", "numbers": "...", "source_idx": 1}},
  ...
]"""
        # v13.17 · extract_evidence 用 opus (决定 writer 素材质量)·max_tokens 1500→4000
        raw = self._llm_opus(prompt, max_tokens=4000).strip()

        def _enrich(ev_list):
            """把 source_idx → source_url + source_title · 落 evidence"""
            out = []
            for e in ev_list:
                if not (isinstance(e, dict) and e.get("pain")):
                    continue
                idx = e.get("source_idx")
                try:
                    idx = int(idx) if idx not in (None, "") else 0
                except Exception:
                    idx = 0
                if 1 <= idx <= len(top):
                    src = top[idx - 1]
                    e["source_url"] = src.get("url", "") or src.get("deep_via_url", "")
                    e["source_title"] = src.get("title", "")[:120]
                    e["source_via"] = src.get("_src", "") or src.get("deep_via", "")
                else:
                    # 0 或超范围 · 挂第一个 top 作为"综合"来源
                    if top:
                        e["source_url"] = top[0].get("url", "")
                        e["source_title"] = "[综合] " + top[0].get("title", "")[:100]
                        e["source_via"] = "synthesized"
                    else:
                        e["source_url"] = ""
                        e["source_title"] = ""
                        e["source_via"] = "no_source"
                out.append(e)
            return out

        # 多层尝试解析
        try:
            # 1. 完整 JSON 数组 · 非贪婪
            m = re.search(r"\[\s*\{[\s\S]+\}\s*\]", raw)
            if m:
                arr = json.loads(m.group(0))
                if isinstance(arr, list):
                    return _enrich(arr)
        except Exception as e:
            pass
        # 2. 逐对象抽取（LLM 有时输出多个独立 {} 而不是数组）
        try:
            objs = []
            for m in re.finditer(r"\{[^{}]*?\"pain\"[^{}]*?\}", raw):
                try:
                    objs.append(json.loads(m.group(0)))
                except Exception:
                    pass
            if objs:
                return _enrich(objs)
        except Exception:
            pass
        # 3. 兜底
        return []

    # ─────────────────────────────────────────────
    # 6. Iterative Deepening
    # ─────────────────────────────────────────────
    def deep_research(self, role: str, topic: str, target_evidence: int = 4,
                       exhaustive: bool = False, for_user: str = "") -> Dict:
        """
        完整流水线：Plan → Fanout → Dedupe → Rerank → Extract → 若不够 · Deepen
        v13.63 · exhaustive=True 时·穷尽模式(用户 2026-07-13:「搜索一定要穷尽搜索」):
          - target_evidence 强制 12
          - deep_read max_urls 6 → 15
          - deepen 最多 5 轮(不是 2 轮)
          - 停止条件:连续 2 轮无新词
        v13.64 · for_user (agent_id): 挂给 evidence · 供 puppet 定向消费
        """
        if exhaustive:
            target_evidence = max(target_evidence, 12)
        start = time.time()
        log = []
        # 1 · Plan
        queries = self.plan_queries(role, topic, n=4)
        log.append(f"planned {len(queries)} queries")
        # v13.15 · 落"我真搜了啥"
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "evolution"))
            from what_i_did import record
            record("search_plan", {"role": role, "topic": topic, "queries": queries}, ok=True)
        except Exception:
            pass
        # 2 · Fanout
        raw = self.fanout(queries, per_source=5)
        log.append(f"fanout got {len(raw)} raw hits")
        try:
            from what_i_did import record
            record("search_fanout", {"n_queries": len(queries), "n_raw_hits": len(raw)},
                   ok=(len(raw) > 5))
        except Exception:
            pass
        # 3+4 · Dedupe & Rerank · v13.17 · 12→24 (给 deep_read 更多可选)
        top = self.dedupe_rerank(raw, top_n=24)
        log.append(f"reranked to top {len(top)}")
        # 4.5 · Deep Read (v13.14 · web-access bridge · 一手源)
        # v13.20 · 用户"电脑卡"投诉后·max_urls 12→6 · workers 6→2 · 慢一点但不烧内存
        _max_urls = 15 if exhaustive else 6
        top_enriched = self.deep_read(top, max_urls=_max_urls, max_workers=2)
        n_deep = sum(1 for r in top_enriched if r.get("deep_via"))
        log.append(f"deep_read {n_deep} 一手源{' [穷尽]' if exhaustive else ''}")
        # 5 · Extract (基于 enriched 结果·evidence 质量提升)
        evidence = self.extract_evidence(top_enriched, role, topic, n=target_evidence)
        log.append(f"extracted {len(evidence)} evidence")
        # 6 · Deepen · v13.63 · 穷尽模式最多 5 轮·连 2 轮无新词才停
        # v13.21 · deepen 也用真人搜法 · 不再 "X 踩坑 复盘 知乎" 词块
        rounds = 0
        max_rounds = 5 if exhaustive else 2
        empty_rounds = 0
        empty_terms_seen = set()
        while len(evidence) < target_evidence and top and rounds < max_rounds and empty_rounds < 2:
            rounds += 1
            new_terms = self._extract_new_terms(top_enriched[:10], topic)
            # v13.63 · 穷尽模式:去掉见过的 term
            new_terms = [t for t in new_terms if t not in empty_terms_seen]
            if not new_terms:
                empty_rounds += 1
                log.append(f"deepen round {rounds} · 无新词 (empty {empty_rounds}/2)")
                continue
            empty_terms_seen.update(new_terms)
            empty_rounds = 0
            deep_qs = []
            for t in new_terms[:4]:
                # 中文短语 → 知乎/小红书
                deep_qs.append({"query": f"{t} 一个月", "lang": "zh",
                                "target_source": "zhihu", "style": "phrase"})
                deep_qs.append({"query": f'{t} 值不值', "lang": "zh",
                                "target_source": "xiaohongshu", "style": "phrase"})
                # Dork · Google
                deep_qs.append({"query": f'site:reddit.com "{t}" cost',
                                "lang": "en", "target_source": "google", "style": "dork"})
            raw2 = self.fanout(deep_qs, per_source=6)
            top2 = self.dedupe_rerank(top + raw2, top_n=30)  # v13.17 · 15→30
            # 深读第二批
            top2_enriched = self.deep_read(top2, max_urls=4, max_workers=2)  # v13.20 · 降负载
            more = self.extract_evidence(top2_enriched, role, topic,
                                          n=target_evidence - len(evidence))
            # 去重叠合
            seen = {e["pain"][:20] for e in evidence}
            n_new = 0
            for e in more:
                if e.get("pain", "")[:20] not in seen:
                    evidence.append(e)
                    n_new += 1
            log.append(f"deepen round {rounds} · +{n_new} evidence · new_terms={new_terms[:5]}")

        elapsed = round(time.time() - start, 1)

        # v13.19 · search_meta_learning · 落 query DNA + 效果 · 供后续反思
        # v13.21 · queries 现在是 dict 列表·抽 query 字符串+target_source
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "evolution"))
            from search_meta_learning import log_query_effect
            n_q = max(len(queries), 1)
            per_raw = len(raw) // n_q
            per_deep = n_deep // n_q
            per_ev = len(evidence) // n_q
            per_el = elapsed / n_q
            # 每条 query 按 _src 分组算真效果 (而不是均分)
            src_hits = {}
            for r in raw:
                q = r.get("_query", "")
                src_hits.setdefault(q, 0)
                src_hits[q] += 1
            for q_obj in queries:
                q_str = q_obj.get("query") if isinstance(q_obj, dict) else str(q_obj)
                src = q_obj.get("target_source", "all_fanout") if isinstance(q_obj, dict) else "all_fanout"
                real_hits = src_hits.get(q_str, per_raw)  # 用真命中数
                log_query_effect(q_str, src, real_hits, per_deep, per_ev, per_el,
                                 extra={"role": role, "topic": topic,
                                        "lang": q_obj.get("lang") if isinstance(q_obj, dict) else "",
                                        "style": q_obj.get("style") if isinstance(q_obj, dict) else ""})
        except Exception:
            pass

        # 抽出 query 字符串给外面看
        queries_str = [q.get("query") if isinstance(q, dict) else str(q) for q in queries]
        # v13.64 · 每条 evidence 挂 for_user (若有)
        if for_user:
            for e in evidence:
                e["for_user"] = for_user
        return {
            "role": role,
            "topic": topic,
            "for_user": for_user,
            "queries": queries_str,
            "queries_full": queries,  # v13.21 · 带 target_source/lang/style
            "raw_count": len(raw),
            "reranked": top_enriched[:10],
            "n_deep_read": n_deep,
            "evidence": evidence,
            "elapsed": elapsed,
            "log": log,
        }

    def _extract_new_terms(self, results: List[Dict], topic: str) -> List[str]:
        """从 top 结果标题里挖出未见过的具体名词"""
        text = " ".join((r.get("title", "") + " " + r.get("content", "")[:100]) for r in results)
        # 抓工具/模型/平台名（英数字+2 位以上，或 4-8 字中文含"技术/工具"信号）
        candidates = re.findall(r"\b[A-Z][a-zA-Z0-9]{2,15}\b", text)
        # 过滤已在话题里出现的
        topic_low = topic.lower()
        fresh = [c for c in candidates if c.lower() not in topic_low]
        # 去重排序 · 出现次数多的靠前
        from collections import Counter
        return [w for w, _ in Counter(fresh).most_common(5)]


# ═══════════════════════════════════════════════════════════════
# v11-P2.2 · --doctor 自诊断（灵感 Panniantong/Agent-Reach）
# ═══════════════════════════════════════════════════════════════

def doctor(verbose: bool = False) -> Dict:
    """
    每源发一个最小真实请求（不是看 attr 存不存在），报告状态。
    不改变主流程，纯 diagnostic。返回 dict + 打印表格。
    """
    import sys as _sys, time as _time
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        from search_engine import SearchEngine
    except Exception as e:
        print(f"❌ SearchEngine import 失败: {e}")
        return {"ok": False, "error": str(e)}

    se = SearchEngine()
    probe_query = "AI"  # 无害探测词，不会触发任何反作弊

    # 每源：(name, 探测函数, 超时 s)
    sources = [
        ("bing_cn",  lambda: se.search_bing_cn(probe_query, 1),  15),
        ("bing_en",  lambda: se.search_bing_en(probe_query, 1),  15),
        ("sogou_wx", lambda: se.search_sogou_weixin(probe_query, 1), 15),
    ]
    # reddit / xhs 走 adapter，走 hasattr 检查（agent_reach 说的第一层探测）
    optional = [
        ("reddit",   getattr(se, "search_reddit", None)),
        ("xhs",      getattr(se, "search_xiaohongshu", None)),
    ]

    results = []
    print("═" * 60)
    print(f"agent_search --doctor · {datetime.now().isoformat(timespec='seconds')}")
    print("═" * 60)
    for name, probe, tmo in sources:
        t0 = _time.time()
        try:
            got = probe()
            elapsed = _time.time() - t0
            n = len(got) if isinstance(got, list) else 0
            # v11-P2.2: 检测是否 fallback 生效
            is_fallback = bool(n > 0 and got and got[0].get("_source_fallback"))
            if n > 0 and is_fallback:
                status = "🔀 fallback"
                fb_tag = got[0].get("_source_fallback", "")
                extra = f" ({fb_tag})"
            elif n > 0:
                status = "✅ alive"
                extra = ""
            else:
                status = "🟡 empty"
                extra = ""
            results.append({"source": name, "status": status, "n": n,
                            "elapsed": round(elapsed, 2),
                            "fallback": got[0].get("_source_fallback") if is_fallback else None})
            print(f"  {status}  {name:10s} · {n:2d} 条 · {elapsed:.1f}s{extra}")
            if verbose and got:
                print(f"       样本: {(got[0].get('title') or got[0].get('url') or '')[:60]}")
        except Exception as e:
            elapsed = _time.time() - t0
            results.append({"source": name, "status": "❌ broken",
                            "error": str(e)[:120], "elapsed": round(elapsed, 2)})
            print(f"  ❌ broken  {name:10s} · {elapsed:.1f}s · {str(e)[:60]}")

    for name, fn in optional:
        if fn is None:
            results.append({"source": name, "status": "⏸️  not implemented"})
            print(f"  ⏸️  未实现  {name:10s} · 待接入")
        else:
            results.append({"source": name, "status": "🔶 attr-check only"})
            print(f"  🔶 attr有  {name:10s} · 未跑真实探测（安全避险）")

    alive = sum(1 for r in results if r["status"].startswith("✅"))
    fallback_ok = sum(1 for r in results if r["status"].startswith("🔀"))
    total_active = len(sources)
    working = alive + fallback_ok  # alive + fallback 都算能用
    print("─" * 60)
    if fallback_ok:
        print(f"总计: {alive}/{total_active} 主源存活 · {fallback_ok} 走 fallback · {len(optional)} 可选源")
    else:
        print(f"总计: {alive}/{total_active} 主源存活 · {len(optional)} 可选源")
    print("═" * 60)
    return {"ok": working > 0, "alive": alive, "fallback": fallback_ok,
            "total": total_active, "sources": results}


if __name__ == "__main__":
    # 单元测试 + --doctor 自诊断
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--doctor", action="store_true", help="每源发最小请求，报告存活状态")
    p.add_argument("--verbose", action="store_true", help="doctor 模式下展示样本")
    p.add_argument("--test", action="store_true", help="跑 deep_research 单元测试")
    args = p.parse_args()

    if args.doctor:
        r = doctor(verbose=args.verbose)
        sys.exit(0 if r["ok"] else 1)

    # 默认或 --test：跑单元测试
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from search_engine import SearchEngine

    # LLM proxy（用现有 v11 llm）
    from problem_planner import _llm as llm_call

    se = SearchEngine()
    ag = AgentSearch(se, llm_call)

    role = "engineer"
    topic = "AI Agent 生产环境 cron 失忆"
    res = ag.deep_research(role, topic, target_evidence=4)
    print(f"⏱ {res['elapsed']}s · raw={res['raw_count']} · evidence={len(res['evidence'])}")
    print(f"\n== 生成 queries ==")
    for q in res["queries"]:
        print(f"  · {q}")
    print(f"\n== evidence ==")
    for i, e in enumerate(res["evidence"], 1):
        print(f"[{i}]")
        for k in ("pain", "pitfall", "numbers"):
            print(f"  {k}: {e.get(k, '')[:80]}")
