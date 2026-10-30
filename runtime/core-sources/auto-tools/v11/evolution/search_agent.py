"""v13.87.20 · SearchAgent · 统一门面 (Facade)

用户 2026-07-23 抓正:
「你是一个整体 search agent 被大家使用 · 为啥 CDP 一会不行 friday 一会忘了 ·
 里面是庞大系统没有整体入口? 别的 session 接你还要挑 6 个组件」

── 根因 ──
v13.87 造了 13 个搜索相关组件 · 20+ public 函数散在各文件
上游要用 · 必须懂每个 · 挑组合 · 手动串
结果 · 分裂/健忘/别人接不进/自己 6 commit 才发现"没接主链路"

── 本文件 ──
一个类 · 一个方法 · 上游一行搞定:
    from search_agent import SearchAgent
    hits = SearchAgent().search("RAG 评测", mode="balanced")

内部帮你串:
  1. search_cache 查 24h TTL · 命中直返
  2. search_skill_forger 挑历史最优 source
  3. friday_universal (zh/xhc/bing/google/zhipu 自动 token 路由)
  4. fanout 派发 → agent_search.AgentSearch (老流水线兼容)
  5. fetcher_decor 装饰器自动落 health / completeness / cache
  6. search_supervisor.supervise_loop 循环判够不够 → 追搜
  7. search_graph 降级路径 (可选)
  8. site_bookmark 记 full 结果
  9. stuck_auto_search 兜底

── 门面 API ──

    search(query, mode='balanced', **kwargs) → list[hit]
        mode: fast (只查 cache + 一次 fanout)
             balanced (+ supervisor 一轮判决)
             deep (+ supervise_loop 循环追搜 · 直到 sufficient 或 max=3)

    hit 结构 (统一):
        {url, title, content, source, completeness, meta, evidence_score?}

── 兼容 ──
不删任何组件 · 全部保留独立可调
Facade 只是"聚合"层 · 让 90% 场景一行搞定
"""
import os
import sys
import time
from typing import Optional

# v13.88.76 · 用户 07-28 抓「所有静默监控都到齐了吗」
# search 静默失败 (friday 0 hits / CDP 挂) 前不 alert
def _alert(severity: str, msg: str):
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from alert_bus import notify
        notify("search_agent", severity, msg)
    except Exception:
        pass


# v13.88.83 · 用户 07-28「google 质量差·最好短语」
# 长整句 → 抽核心短语 (工具名/实体/2-4 关键词)·搜索引擎对短语相关性高
def _to_search_phrase(query: str, max_len: int = 25) -> str:
    """整句压短语 · 保留工具名/实体/数字·去虚词"""
    import re
    q = query.strip()
    if len(q) <= max_len:
        return q
    # 保英文工具名/产品名 (Claude/GLM/GA4/BUILD 等大写或含数字的 token)
    keep_en = re.findall(r'[A-Za-z][A-Za-z0-9.\-]{2,}', q)
    # 中文 2-6 字实体 (取前几个高信息词)
    cn_tokens = re.findall(r'[一-鿿]{2,6}', q)
    # 去虚词
    STOP = {"实测", "对比", "发布", "首日", "后", "的", "了", "在", "和", "与",
            "怎么", "如何", "为啥", "反直觉", "发现"}
    cn_keep = [t for t in cn_tokens if t not in STOP][:3]
    # 组合: 英文名 + 前 3 中文实体
    parts = keep_en[:4] + cn_keep
    phrase = " ".join(dict.fromkeys(parts))  # 去重保序
    result = phrase[:40] if phrase else q[:max_len]
    return result or q[:max_len]

V11 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEED_ROOT = os.path.dirname(V11)  # seed-users-system/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, V11)
sys.path.insert(0, SEED_ROOT)  # for search_engine (顶层 py)

# 自动加载 .env (secret)
try:
    from env_loader import load_env
    load_env()
except Exception:
    pass


class SearchAgent:
    """搜索 agent 门面 · 一个类串所有 v13.87 搜索组件

    用法:
        sa = SearchAgent()
        hits = sa.search("RAG 评测", mode="balanced", top_k=10)

    构造参数 (都可选):
        use_cache=True       启用 search_cache 24h 查
        use_forger=True      启用 skill_forger 挑源
        use_supervisor=True  启用 supervisor 判够不够 (balanced/deep 才生效)
        use_bookmark=True    启用 site_bookmark 记
        use_stuck=True       启用 stuck_auto_search 兜底
        max_supervise_iter=3 deep 模式 supervisor 循环上限
    """

    def __init__(self, use_cache=True, use_forger=True, use_supervisor=True,
                 use_bookmark=True, use_stuck=True,
                 max_supervise_iter=3, use_deep_google=False,
                 log_experiment=True):
        self.use_cache = use_cache
        self.use_forger = use_forger
        self.use_supervisor = use_supervisor
        self.use_bookmark = use_bookmark
        self.use_stuck = use_stuck
        self.max_supervise_iter = max_supervise_iter
        # v13.88.9g · demo/探针搜索传 False → 不写 search_experiment 记录
        # 避免测试 query 被当真任务算进成功率·误触 circuit_open 报警 (用户 07-31)
        self.log_experiment = log_experiment
        # v13.87.21 · deep mode 触发 isFast=false 真爬每 URL 正文
        self.use_deep_google = use_deep_google
        # lazy 初始化 · 不阻塞构造
        self._agent_search = None
        self._trace = []
        # v13.88.9O · 净化闸拒绝标志 · _retry_if_poor 靠它判断
        #   「原 query 是不可搜的脏内容」·不是「搜了但没结果」
        self._last_purify_rejected = False

    # ═════ v13.88.18 · deep_read + dead_link_gate ═════
    def deep_read(self, url: str, prefer: str = "auto") -> dict:
        """深读一个 URL · 前置 dead_link 检查·再走 web_access_bridge

        v13.88.18 · 撤 BrowserSkill (用户「垃圾」)·加 404 检死不重试
        用户 2026-07-24 抓「一直 404 自己发现不了吗·不知道优化和记录吗」
        """
        # v13.88.18 · 死链检测 · 已知死 → 直接返回 skip
        try:
            from dead_link_gate import is_known_dead, check_url_alive, mark_dead
            if is_known_dead(url):
                return {"ok": False, "content": "", "url": url,
                         "via": "dead_link_kb_skip", "skipped_dead": True}
            # 现场检 (快·5s HEAD)
            alive_r = check_url_alive(url, timeout=8)
            if not alive_r.get("alive"):
                mark_dead(url, alive_r.get("kind", "?"), alive_r.get("reason", ""))
                return {"ok": False, "content": "", "url": url,
                         "via": f"dead_link:{alive_r.get('kind')}",
                         "skipped_dead": True,
                         "reason": alive_r.get("reason", "")}
        except ImportError:
            pass  # gate 缺失·继续老流程

        try:
            from web_access_bridge import WebAccess
            wa = WebAccess()
            return wa.read_url(url, prefer=prefer)
        except Exception as e:
            return {"ok": False, "content": "",
                     "error": f"{type(e).__name__}:{e}", "url": url}

    def deep_read_batch(self, urls: list, max_workers: int = 5) -> list:
        """批量深读 · 前置过滤死链 + 并发"""
        # v13.88.18 · 先过滤死链 · 别浪费并发
        try:
            from dead_link_gate import is_known_dead
            urls_alive = [u for u in urls if not is_known_dead(u)]
            n_skipped = len(urls) - len(urls_alive)
            if n_skipped:
                print(f"    [dead_link] skip {n_skipped}/{len(urls)} 已知死链")
            urls = urls_alive
        except ImportError:
            pass

        try:
            from web_access_bridge import WebAccess
            wa = WebAccess()
            return wa.read_urls(urls, max_workers=max_workers)
        except Exception as e:
            self._trace.append(f"deep_read_batch_err:{e}")
            return []

    # ═════ 内部 · lazy 加载底层 agent_search ═════
    def _get_fanout(self):
        """兼容: 老 AgentSearch 保留 · 用它 fanout 派发到 SOURCE_CATALOG"""
        if self._agent_search is not None:
            return self._agent_search
        try:
            from agent_search import AgentSearch
            from problem_planner import _llm as llm_call
            from search_engine import SearchEngine
            self._agent_search = AgentSearch(SearchEngine(""), llm_call)
            return self._agent_search
        except Exception as e:
            self._trace.append(f"agent_search_init_err:{type(e).__name__}")
            return None

    # ═════ 组件 · 都 fail-safe ═════
    def _try_cache(self, query: str) -> list:
        if not self.use_cache:
            return []
        try:
            from search_memory import remember
            hits = remember(query, similar_threshold=0.5, top_k=3)
            if hits:
                self._trace.append(f"cache_hit:{len(hits)}")
                return hits
        except Exception:
            pass
        return []

    def _suggest_sources(self, query: str) -> list:
        if not self.use_forger:
            return []
        try:
            from search_skill_forger import suggest_sources
            return suggest_sources(query, top_k=3, min_samples=2) or []
        except Exception:
            return []

    def _suggest_backends_by_domain(self, urls: list) -> dict:
        """v13.87.25 · 升级到 fetch_strategy (primary + fallback + depth + tier)
        用户 2026-07-23 抓「学网站怎么用 · 记 fetch_strategy 组合」
        给 url list · 返 {domain: strategy_dict} · 优先 DB · 再手动规则 · 最后推断
        """
        if not self.use_bookmark or not urls:
            return {}
        try:
            from urllib.parse import urlparse
            from site_bookmark import get_strategy
            domains = set()
            for u in urls:
                if isinstance(u, str) and u.startswith("http"):
                    d = urlparse(u).netloc.lower()
                    if d:
                        domains.add(d)
            r = {}
            for d in domains:
                s = get_strategy(d)
                if s and s.get("primary"):
                    r[d] = s
            if r:
                self._trace.append(f"strategy_hit:{len(r)}/{len(domains)} 站有策略")
            return r
        except Exception:
            return {}

    # v13.88.9h · HTTP 直连(非 CDP)通道白名单 · http_only 快通道用
    #   这些源走网络/API·不占本地 Chrome·load 高也不卡·秒返
    #   对应 agent_search._fanout_by_target 里非 cdp_sources 的源
    HTTP_CHANNELS = ["friday_bing", "friday_google", "friday_zhipu",
                     "zhihu_global", "hackernews", "github_issues",
                     "github_repos", "sogou_weixin", "bing_cn", "bing_en",
                     # v56f · AnySearch 走独立出口·绕Google反爬·HTTP不开Chrome·算快通道
                     "anysearch",
                     # v56i · firecrawl 云端·不开本地Chrome·难题补充源
                     "firecrawl_search",
                     # v56k · wigolo 本地18引擎融合·REST常驻·带评分·源最全
                     "wigolo",
                     # v56n · Tavily·准和全+answer字段直接抽答·补精确抽取缺口
                     "tavily"]

    def _fanout(self, query: str, sources: list = None,
                 top_k: int = 10, http_only: bool = False,
                 _purified: bool = False) -> list:
        """核心搜索 · 走 AgentSearch.fanout (已带装饰器 + friday token 路由)

        http_only=True · 只走 HTTP 直连通道 (friday/hackernews/github/sogou)·
          绕过 CDP · load 高也秒返 · 给 fix_pool/qa_graphs 等快通道调用方
          ── 用户「那么多搜索工具为啥还笨」根治:统一入口也能提供快通道·
             不用再各自 import friday 手撸

        _purified: 内部标志·重搜 variant 已是干净 query·跳过提纯 (防重复提纯)
        """
        sa = self._get_fanout()
        if not sa:
            return []

        # v13.88.9L · query 提纯收口点 (公共底层·search/search_smart/重搜全经这)
        # 病根: 提纯只在 search() 里·search_smart 走 planner→_fanout 绕过 → 脏query
        #   走 smart 路径还搜垃圾。移到 _fanout 一处覆盖全路径 (收口>补丁)。
        # 提纯优先·不是知识拒绝: 从脏query抽真实意图再搜·抽不出真问题才不搜。
        if not _purified:
            try:
                _q0 = (query or "").strip()
                # v13.88.9L-7 · 病根:只对长query(>25)净化·短脏词漏网
                #   (如「System 你是独立审稿 只看你被喂的」20字≤25→跳过净化→脏query直搜)
                # 修:短query若含 prompt/系统指令/告警/对话元描述 特征词也净化;
                #    纯技术短词(如「RAG 评测」无特征词)继续放行·不加LLM开销。
                _DIRTY_SIGNALS = [
                    "你在", "你是", "系统", "审稿", "判断下面", "用户消息",
                    "用户问的", "我的回复", "对上意图", "残词", "重犯", "告警",
                    "goal", "Goal", "System", "system", "独立", "沙箱",
                    "工作目录", "被喂", "tail", "agent 通信", "总线",
                ]
                _ql = _q0.lower()
                _looks_dirty_short = len(_q0) <= 25 and any(
                    _w.lower() in _ql for _w in _DIRTY_SIGNALS)
                if len(_q0) > 25 or _looks_dirty_short:
                    import search_query_purifier as _sqp
                    # v13.88.9N · context="search"·门面直调的是真搜索 query·
                    #   默认可搜·只拦真内部泄漏。病根: 旧默认按「告警」判·把
                    #   「SWE-bench 多少道题」误判成系统内部状态→返[]空搜(3道benchmark题0hits真因)。
                    _pr = _sqp.purify(component="search_agent", msg=_q0,
                                      use_llm=True, context="search")
                    _clean = (_pr.get("query") or "").strip()
                    if _pr.get("searchable") and _clean:
                        if _clean != _q0:
                            self._trace.append(
                                f"purify[{_pr.get('via')}]:「{_q0[:20]}」→「{_clean[:20]}」")
                        query = _clean
                    elif not _pr.get("searchable"):
                        # 纯指令/日志·提纯不出真问题 → 不搜 (记审计·不告警)
                        self._trace.append(
                            f"purify_noquery:{_pr.get('reason','')}")
                        # v13.88.9O · 病根修复(用户抓「goal current 用户消息」
                        #   搜狗微信垃圾结果): 净化闸拒绝后必须让 _retry_if_poor
                        #   知道「原 query 本身不可搜」·否则 quality=empty 会
                        #   触发 regex 变体重试·从脏query碎片抽出「goal」「current」
                        #   这种伪实体词·还带 _purified=True 绕过净化闸再搜一次
                        #   (search_purify_reject.jsonl 拒了·search_experiments.jsonl
                        #   却记着 fast_retry 命中·就是这条绕过链路)。
                        self._last_purify_rejected = True
                        try:
                            import time as _t, json as _j
                            _rl = os.path.join(os.path.dirname(
                                os.path.dirname(os.path.abspath(__file__))),
                                ".search_purify_reject.jsonl")
                            with open(_rl, "a", encoding="utf-8") as _rf:
                                _rf.write(_j.dumps(
                                    {"ts": int(_t.time()), "q": _q0[:100],
                                     "reason": _pr.get("reason", "")},
                                    ensure_ascii=False) + "\n")
                        except Exception:
                            pass
                        return []
                    else:
                        self._last_purify_rejected = False
            except Exception as _pe:
                self._trace.append(f"purify_err:{type(_pe).__name__}·放行")

        # v13.88.9L · 每次搜索上总线 (用户: 全系统搜索可见·治「谁又搜了我让他搜了吗」)
        # 从调用栈推断 who (哪个组件发起的搜索)·全可见可叫停
        try:
            import agent_comm as _ac
            import traceback as _tb
            _who = "search_agent"
            for _fr in reversed(_tb.extract_stack()[:-1]):
                _fn = os.path.basename(_fr.filename)
                if _fn not in ("search_agent.py", "agent_comm.py", "traceback.py"):
                    _who = _fn[:-3] if _fn.endswith(".py") else _fn
                    break
            _ac.log_search(_who, query, source=str(sources or "auto")[:40],
                           trigger="auto")
        except Exception:
            pass

        # 构造 query dict list · 每 source 一条
        if not sources:
            # 智能默认 · zh query → zhihu+xhs · en query → hackernews+friday_bing
            import re
            _zh_chars = len(re.findall(r"[一-龥]", query))
            _en_chars = len(re.findall(r"[A-Za-z]", query))
            # v13.88.9L · 治「search_agent 为啥这么慢」(39s根因):
            #   1. has_zh 太粗·「RAG evaluation 方法」含1个中文字就全判中文源
            #      → 走 sogou_weixin(25s超时)/xiaohongshu(20s)慢源。改看主体语言:
            #      英文字符 >= 中文字符 → 英文技术词主体 → 走英文快源
            #   2. fast 模式慢源移出默认: sogou_weixin/xiaohongshu 抓页面/反爬慢
            #      → 只留 zhihu_global(API直连快)+friday_bing(HTTP快·实测9/10)
            #      慢源留给 balanced/deep (显式选或补搜时才用)·不删源(可用·只是不进fast默认)
            has_zh = _zh_chars > _en_chars   # 主体中文才判中文 (原来 >0 就判·误伤英文技术词)
            # 默认源一律用快源 (zhihu_global API直连 + friday_bing HTTP·实测9/10且快)
            # 慢源(sogou_weixin 25s超时 / xiaohongshu 20s)不进默认·balanced/deep 要广度时
            # 由 planner/补搜显式传 sources·不删源(仍可用·只是不当默认拖慢)
            # v56f(2026-08-05) · P0 优化: 英文默认源 hackernews(Q0.349全场最差)→
            #   anysearch(绕Google反爬·意图路由·实测秒返带全文·质量远超hackernews/google系)。
            #   中文源保持 zhihu_global+friday_bing(Q0.72/0.89 已够好)。
            #   anysearch 作为 google 系(CDP 0.395/friday_google 0.46)的高质量低资源替代。
            if http_only:
                sources = (["zhihu_global", "friday_bing"]
                           if has_zh else ["friday_bing", "anysearch"])
            elif has_zh:
                sources = ["zhihu_global", "friday_bing"]
            else:
                sources = ["friday_bing", "anysearch"]
        elif http_only:
            # 显式 sources 也过滤·只留 HTTP 通道·CDP 源被剔
            _allow = set(self.HTTP_CHANNELS)
            _kept = [s for s in sources if s in _allow]
            if _kept:
                sources = _kept
            else:
                # 传入全是 CDP 源·映射到等价 HTTP 通道兜底
                sources = ["friday_bing"]
            self._trace.append(f"http_only:{sources}")
        # v13.88.84 · 自愈联动·跳被 search_quality_monitor 禁用的死源
        try:
            import json as _j
            _dis_p = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), ".search_disabled_sources.json")
            if os.path.exists(_dis_p):
                _disabled = set(_j.load(open(_dis_p)).keys())
                _before = list(sources)
                sources = [s for s in sources if s not in _disabled] or _before
                if len(sources) < len(_before):
                    self._trace.append(f"skip_dead_sources:{set(_before)-set(sources)}")
        except Exception:
            pass
        query_dicts = [{"query": query,
                        "lang": "zh" if any("一" <= c <= "龥" for c in query) else "en",
                        "target_source": s} for s in sources]
        try:
            hits = sa.fanout(query_dicts, per_source=max(3, top_k // len(sources) + 1))
            # 过滤 _meta_only
            hits = [h for h in (hits or [])
                    if isinstance(h, dict) and not h.get("_meta_only")]
            self._trace.append(f"fanout:{len(hits)} from {sources}")
            return hits
        except Exception as e:
            self._trace.append(f"fanout_err:{type(e).__name__}:{str(e)[:60]}")
            return []

    def _hits_quality(self, hits: list) -> str:
        """判搜索质量 · ok/empty/shell (v13.88.9)"""
        if not hits:
            return "empty"
        good = sum(1 for h in hits if isinstance(h, dict)
                   and (h.get("completeness") in ("full", "partial")
                        or len(h.get("content", "") or "") > 500))
        return "ok" if good else "shell"

    def _rewrite_variants(self, query: str) -> list:
        """v13.88.9 · query 多变体改写 · 用户「能改一开始就蠢然后纠正不过来的问题吗」
        不是只压一次短语·而是生成多个改写角度·让实验挑最优。

        v13.88.9L-63 · 治「死模板派生 query」反例([[feedback-llm-over-rules-for-semantics]]):
          原来只压短语一个角度·抽不出「更宽/更窄」的真不同变体·实验没得挑。
          规则派生本身补成 3 个真不同角度(压短语/更窄/更宽)。
          LLM 语义派生走 env 开关默认关: 实测 switch_query_llm haiku 20s+且常只回原词·
          对搜索重试热路径弊大于利(慢)·故不默认启用·只在显式开关时优先·失败回退规则。
          [[feedback-goal-driven-loop-not-hardcode]] 不为改而改·热路径先保速度。
        """
        # ── LLM 优先(env 开关·默认关): 慢·仅显式开启时用·失败回退规则 ──
        if os.environ.get("MEYO_SEARCH_LLM_REWRITE", "0") == "1":
            try:
                import agent_comm as _ac
                _ok, _ = _ac.can_spawn_more()
            except Exception:
                _ok = False
            if _ok:
                try:
                    llm_vs = self._llm_rewrite_variants(query)
                    if llm_vs:
                        return llm_vs[:2]
                except Exception:
                    pass  # 落到规则兜底

        # ── 规则派生(默认·快): 3 个真不同角度·非只压一次短语 ──
        import re
        variants = []
        # 角度1: 压核心短语(去虚词·留实体)
        try:
            phrase = _to_search_phrase(query, max_len=20)
            if phrase and phrase != query:
                variants.append(phrase)
        except Exception:
            pass
        en = re.findall(r'[A-Za-z][A-Za-z0-9.\-]{2,}', query)
        cn = [t for t in re.findall(r'[一-鿿]{2,6}', query)
              if t not in {"怎么", "如何", "为啥", "问题", "监控", "执行"}]
        # 角度2: 更窄·只留最高信息 2-3 词(实体名词)
        minimal = " ".join(dict.fromkeys(en[:2] + cn[:2]))
        if minimal and minimal not in variants and minimal != query:
            variants.append(minimal)
        # 角度3: 更宽·只留 1 个最核心实体(英文名优先·中文次之)·去限定
        broad = (en[0] if en else (cn[0] if cn else ""))
        if broad and broad not in variants and broad != query:
            variants.append(broad)
        return variants[:2]  # 最多 2 变体·控成本

    def _llm_slim_query(self, query: str) -> str:
        """v56u · LLM 精简长 query 为核心检索词串。治「从句包裹的长题搜索被带偏」。
        理解题意抽核心实体+要查的东西·如「Book of Esther首个地名的1977首相」→
        「Prime Minister India April 1977」。用 haiku(快)·失败回空(调用方跳过)。"""
        _gw = None
        try:
            from qa_llm_gateway import call as _gw
        except Exception:
            try:  # qa_system/agent 不在 path 时补一下
                import os as _os, sys as _sys
                _qa = _os.path.join(_os.path.dirname(_os.path.dirname(
                    _os.path.dirname(_os.path.abspath(__file__)))), "qa_system", "agent")
                if _qa not in _sys.path:
                    _sys.path.insert(0, _qa)
                from qa_llm_gateway import call as _gw
            except Exception:
                return ""
        # v56w · 语言一致约束: 英文题精简结果保持英文(搜英文源命中率高·治haiku
        #   偶发把英文题精简成中文『印度总理 以斯帖记』→搜英文答案搜不到)。
        import re as _reL
        _is_en_q = len(_reL.findall(r"[A-Za-z]", query)) > len(_reL.findall(r"[一-龥]", query))
        _lang_hint = ("输出必须是英文关键词(问题是英文·英文源命中率高)。"
                      if _is_en_q else "")
        try:
            r = _gw("你是搜索关键词提取器。把下面问题压成一行搜索关键词(理解题意·把间接"
                    "指代解开成具体实体·去疑问词/从句·空格分隔)。" + _lang_hint +
                    "严格只输出关键词那一行·"
                    "不要标题/不要列表/不要解释/不要markdown:\n" + query,
                    timeout=15, tier="light")
            out = (r.get("out") if isinstance(r, dict) else str(r)).strip()
            if not out:
                return ""
            # 挑最像关键词的一行(非空·非标题·无 ** / - 前缀)
            for ln in out.split("\n"):
                ln = ln.strip().lstrip("-*# ").strip()
                if ln and "关键词" not in ln and not ln.startswith("*") and 3 < len(ln) < 120:
                    return ln
            return ""
        except Exception:
            return ""

    def _llm_rewrite_variants(self, query: str) -> list:
        """v13.88.9L-63 · 复用 deep_research_agent.switch_query_llm 语义换词器·
        不重造([[feedback-existing-tools-first-not-build]])。返语义变体。
        注: 实测 haiku 20s+·仅 MEYO_SEARCH_LLM_REWRITE=1 时启用(慢·非热路径默认)。"""
        from deep_research_agent import switch_query_llm
        sq = switch_query_llm(goal=query, current_query=query,
                              tried=list(getattr(self, "_tried_queries", [])),
                              model="haiku", timeout=12)
        cands = [c for c in (sq.get("candidates") or [])
                 if c and c.strip() and c.strip() != query]
        return list(dict.fromkeys(cands))

    def _retry_if_poor(self, query: str, hits: list,
                        target_sources, top_k: int,
                        http_only: bool = False) -> list:
        """v13.88.9 · 重试下沉 · 搜完判质量差 → 多变体改写重搜·选最优·记实验

        v13.88.9O · 病根修复(用户抓「goal current 用户消息」搜狗微信垃圾结果):
          原逻辑对「净化闸判定不可搜的脏 query」也照常走 regex 变体重试·
          _rewrite_variants 是纯正则抽词(无语义判断)·从脏话/prompt碎片里
          抽出「goal」「current」这类伪实体词当变体·还带 _purified=True
          强制跳过净化闸再搜一次 → 垃圾结果照样打出去。
          修:若第一次 _fanout 已经是被净化闸拒绝(_last_purify_rejected)·
          说明 query 本身就不可搜·任何基于它的 regex 拆词变体依然是脏的·
          直接放弃重试(不是「搜了没结果」而是「压根不该搜」)。
        """
        import time as _t
        # 原 query 被净化闸判定不可搜 → 变体也必脏·直接放弃·不重试
        if getattr(self, "_last_purify_rejected", False):
            self._trace.append("retry_skipped:purify_rejected_origin")
            self._last_purify_rejected = False  # 用完复位·不污染下次调用
            return hits

        quality = self._hits_quality(hits)
        # 记第一次实验
        self._log_experiment(query, hits, quality, "fast", tries=1,
                             query_original=query)
        if quality == "ok":
            return hits

        # 质量差 · 逐变体重搜 · 谁 ok 用谁
        # v13.88.9O · variant 不再豁免净化(_purified=False)·regex 拆词产物
        #   可能仍是脏碎片·必须让净化闸再判一次·真正安全的才放行
        best_hits, best_q = hits, quality
        for i, nq in enumerate(self._rewrite_variants(query), 1):
            t0 = _t.time()
            try:
                rhits = self._fanout(nq, sources=target_sources, top_k=top_k,
                                     http_only=http_only, _purified=False)
            except Exception:
                rhits = []
            if getattr(self, "_last_purify_rejected", False):
                self._trace.append(f"retry_variant{i}_purify_rejected:「{nq[:20]}」")
                self._last_purify_rejected = False
                continue
            rq = self._hits_quality(rhits)
            self._log_experiment(nq, rhits, rq, "fast_retry", tries=i + 1,
                                 query_original=query,
                                 latency_ms=(_t.time() - t0) * 1000)
            self._trace.append(f"retry_variant{i}:「{nq[:20]}」→{rq}")
            if rq == "ok":
                return rhits
            if rq == "shell" and best_q == "empty":
                best_hits, best_q = rhits, rq
        return best_hits

    def _log_experiment(self, query, hits, quality, strategy,
                        tries=1, query_original="", latency_ms=0):
        """记搜索实验 (接 search_experiment)"""
        # v13.88.9g · demo/探针搜索不落实验记录·不污染成功率统计
        if not getattr(self, "log_experiment", True):
            return
        try:
            from search_experiment import record
            record(query, hits, strategy=strategy, latency_ms=latency_ms,
                   query_original=query_original, backend="searchagent",
                   extra={"try": tries, "quality_seen": quality})
        except Exception:
            pass

    def _supervise(self, query: str, hits: list, source: str = "fanout") -> dict:
        """判够不够 · 返 decision"""
        if not self.use_supervisor:
            return {"is_sufficient": True, "next_action": "stop",
                    "confidence": 100, "reason": "supervisor_off"}
        try:
            from search_supervisor import supervise
            return supervise(query, hits, source=source)
        except Exception as e:
            self._trace.append(f"supervise_err:{type(e).__name__}")
            return {"is_sufficient": len(hits) > 0,
                    "next_action": "stop", "confidence": 50,
                    "reason": "supervisor_fail"}

    def _record_forger(self, query: str, source: str, hits: list) -> None:
        if not self.use_forger:
            return
        try:
            from search_skill_forger import record_search
            record_search(query, source, hits)
        except Exception:
            pass

    def _try_stuck_fallback(self, query: str, hits: list, mode: str = "balanced") -> list:
        """空/极稀薄时 · stuck_auto_search 兜底

        v13.88.9L-hard · 接线修复(depends_on 查实): interactive_browser_agent.run_goal
          代码完整(observe→decide→act→check→reflect 5段loop)·但只有 stuck_auto_search
          import 它·而 SearchAgent 的 stuck 兜底以前只查历史(find_previous_solution)·
          从不升级到浏览器真交互 → 最强能力在 search() 主路径上空转。
          现在: deep 模式 + 常规搜索全空 → 升级 browser_fallback(浏览器真交互找)。
          门槛严(deep only + hits 全空 + can_spawn)·浏览器 load 贵·不轻易开。
        """
        if not self.use_stuck or hits:
            return hits
        try:
            from stuck_auto_search import find_previous_solution
            prev = find_previous_solution(query)
            if prev and (prev.get("reusable_skills") or
                          prev.get("suggested_sources")):
                self._trace.append(f"stuck_reused:{prev}")
                return hits  # skills 是 hint · 上游可拿
            # v13.88.9L-hard · deep 模式 + 全空 → 浏览器真交互兜底(fail-safe)
            if mode == "deep":
                try:
                    import agent_comm as _ac
                    _ok, _why = _ac.can_spawn_more()
                except Exception:
                    _ok = True  # 拿不到闸门状态时放行(browser 内部自己也节流)
                if _ok:
                    try:
                        from stuck_auto_search import browser_fallback
                        br = browser_fallback(query, max_steps=12)
                        if br.get("ok"):
                            fr = br.get("full_result") or {}
                            found = (fr.get("findings") or fr.get("results")
                                     or fr.get("extracted") or []) if isinstance(fr, dict) else []
                            new = []
                            for f in (found or [])[:8]:
                                if isinstance(f, dict):
                                    new.append({
                                        "url": f.get("url", ""),
                                        "title": f.get("title", "")[:120],
                                        "content": (f.get("content") or f.get("text") or "")[:5000],
                                        "source": "interactive_browser_agent",
                                        "completeness": "full",
                                    })
                            self._trace.append(
                                f"browser_fallback:ok found={br.get('found_count')} "
                                f"materialized={len(new)}")
                            if new:
                                return new
                        else:
                            self._trace.append(
                                f"browser_fallback:no ({br.get('err','empty')[:40]})")
                    except Exception as _be:
                        self._trace.append(f"browser_fallback_err:{type(_be).__name__}")
                else:
                    self._trace.append(f"browser_fallback_skip_load:{_why[:30]}")
        except Exception:
            pass
        return hits

    # ═════ v13.87.33 · DAG 入口 · 真编舞 (不是线性 pipeline) ═════
    def search_dag(self, query: str, top_k: int = 10) -> list:
        """DAG 编舞版 · 用户 2026-07-23 goal:
        「变 graph · 灵活编排 · 首搜并行 · 条件跳追搜 · tag-based 直搜」

        跟 search_smart 差别:
          smart: 线性 planner→fanout→dorks→supervise
          dag:   拓扑 planner→[fanout || site_top] merge → sup
                 → if_low_conf [dorks || 3engines] merge → sup → reflect
                 并行 · 条件跳 · 满意早停
        """
        self._trace = []
        t0 = time.time()

        # v13.87.46 · intent_dag 前置 · 复杂 query 走 4 路并行分析 (用户 Q6)
        _enriched_query = query
        try:
            from intent_dag import analyze as _intent_analyze, _is_complex_query
            _mode = _is_complex_query(query)  # 'fast'|'balanced'|'deep'
            if _mode in ("balanced", "deep"):
                _iu = _intent_analyze(query, mode=_mode)
                _pain = (_iu.get("pain") or {}).get("value", "") if isinstance(_iu.get("pain"), dict) else str(_iu.get("pain", ""))
                _what = (_iu.get("what") or {}).get("value", "") if isinstance(_iu.get("what"), dict) else str(_iu.get("what", ""))
                if _pain and len(_pain) > 5:
                    _enriched_query = f"{query} [pain={_pain[:60]}]"
                    self._trace.append(f"intent_dag[{_mode}]: pain={_pain[:40]}")
                elif _what:
                    self._trace.append(f"intent_dag[{_mode}]: what={_what[:40]}")
        except Exception as _ie:
            self._trace.append(f"intent_dag_skip:{type(_ie).__name__}:{str(_ie)[:60]}")

        # 1. planner
        try:
            from search_planner import plan as _plan
            plan_obj = _plan(_enriched_query, use_llm=True, use_memory=True, timeout=12)
            self._trace.append(f"planner: {plan_obj.intent}")
        except Exception:
            return self.search(query, mode="balanced", top_k=top_k)

        # 2. DAG 编舞
        try:
            from search_dag import run_dag
            dag_r = run_dag(plan_obj, query)
            hits = dag_r.get("hits", [])
            for s in dag_r.get("trace", []):
                self._trace.append(
                    f"[{s['node']}] ok={s['ok']} "
                    f"{s['elapsed_s']}s Δ{s['hits_delta']} "
                    f"{s.get('trigger_reason','')[:40]}"
                )
            self._trace.append(f"dag_total:{dag_r.get('total_elapsed_s')}s")
            return hits[:top_k]
        except Exception as e:
            self._trace.append(f"dag_err:{type(e).__name__}·降 smart")
            return self.search_smart(query, top_k=top_k)

    # ═════ v13.87.28 · smart 入口 · planner 挑组合 · reflector 反思 ═════
    def search_smart(self, query: str, top_k: int = 10,
                       reflect: bool = True) -> list:
        """智能搜 · planner 看 query 挑 sources · 反思学习
        用户 2026-07-23 goal 「不硬来·根据任务组合·反思学习」

        流程:
          1. planner.plan(query) → SearchPlan(intent, sources, options)
          2. 按 plan 执行 (跳 target_sources 硬编码)
          3. supervisor 判 (按 plan.use_supervise_loop)
          4. reflector 反思 · 落规则

        跟 search(mode='deep') 差别:
          · search mode: 硬跑全套
          · search_smart: LLM 挑 subset · 快而准
        """
        self._trace = []
        t0 = time.time()

        # 1. planner 决 (haiku · 8s)
        try:
            from search_planner import plan as _plan
            plan_obj = _plan(query, use_llm=True, use_memory=True, timeout=12)
            self._trace.append(
                f"planner: intent={plan_obj.intent} conf={plan_obj.confidence}")
            self._trace.append(
                f"  sources={plan_obj.sources_ordered} "
                f"l30={plan_obj.use_last30days} "
                f"3e={plan_obj.use_3engines} loop={plan_obj.use_supervise_loop}")
        except Exception as e:
            self._trace.append(f"planner_err:{type(e).__name__}")
            plan_obj = None

        # 兜底 · planner 挂就走 balanced
        if not plan_obj:
            return self.search(query, mode="balanced", top_k=top_k)

        # 2. 按 plan 执行
        # v13.87.29 · depth_score 决定行为 · 不再硬 mode
        depth = plan_obj.depth_score
        self._trace.append(f"depth_score={depth}")
        hits = []

        # 2a. cache 查 (总是走)
        cached = self._try_cache(query)
        if cached:
            # 低深度 · 记忆命中直返 · fact_lookup 深度 0-30
            if depth <= 30 and plan_obj.max_iter == 1:
                self._trace.append(f"cache_hit·depth={depth}·直返")
                if reflect:
                    self._reflect_after(query, plan_obj, cached[:top_k],
                                          None, time.time() - t0)
                return cached[:top_k]

        # 2b. 按 planner 挑的 sources 跑 fanout
        target_sources = plan_obj.sources_ordered or None
        hits = self._fanout(query, sources=target_sources, top_k=top_k)

        # v13.87.31 · site: dork 动态扩展
        # 用户 2026-07-23 抓「fanout 自己拓展」
        if hits and getattr(plan_obj, "site_dorks", None):
            try:
                # v13.88.83 · 用户抓「google 质量差」·实测 google 3/10·bing 9/10
                # dork 追搜改 bing 优先·google 兜底
                from friday_universal import search_bing, search_google
                for dork in plan_obj.site_dorks[:3]:
                    dork_q = f"{query} {dork}"
                    dork_hits = search_bing(dork_q, top_k=3) or search_google(dork_q, top_k=3)
                    if dork_hits:
                        hits.extend(dork_hits)
                        self._trace.append(f"dork:{dork} +{len(dork_hits)}")
            except Exception as e:
                self._trace.append(f"dork_err:{type(e).__name__}")

        # v13.87.31 · Google 种子层 · 从首搜 URL 发现新站
        # 用户 2026-07-23 抓「Google 先搜可以学到什么网站在做啥」
        if hits and self.use_bookmark:
            try:
                from site_discovery import discover_from_hits
                d_r = discover_from_hits(hits, query=query)
                if d_r.get("learned_count", 0) > 0:
                    self._trace.append(
                        f"site_discovery: +{d_r['learned_count']} 新站学到"
                        f" · 已知 {len(d_r.get('already_known', []))}"
                    )
            except Exception as e:
                self._trace.append(f"discovery_err:{type(e).__name__}")

        # 2c. last30days (按 plan)
        if plan_obj.use_last30days:
            try:
                from last30days_wrapper import (available as _l30_ok,
                                                  search as _l30_search)
                if _l30_ok():
                    l30_hits = _l30_search(query, quick=True, timeout=60)
                    if l30_hits:
                        hits.extend(l30_hits[:5])
                        self._trace.append(f"last30days:+{len(l30_hits[:5])}")
            except Exception as e:
                self._trace.append(f"l30_err:{type(e).__name__}")

        # 2d. 3 引擎并行 (按 plan · 且英文 query)
        if plan_obj.use_3engines and hits:
            try:
                import concurrent.futures
                from friday_universal import (search_google_deep,
                                                 search_bing_deep, search)
                is_en = not any("一" <= c <= "龥" for c in query)
                if is_en:
                    # v13.88.9 · 禁 google 首发 (用户: 机房 IP 必弹验证码·卡死)
                    # bing+zhipu 多源冗余·google 仅 use_deep_google 显式开
                    jobs = [
                        ("bing", lambda: search_bing_deep(query, top_k=5, timeout=45)),
                        ("zhipu", lambda: search(query, ["zhipu-search"], top_k=5, timeout=15)),
                    ]
                    if self.use_deep_google:
                        jobs.insert(0, ("google", lambda: search_google_deep(query, top_k=5, timeout=45)))
                    with concurrent.futures.ThreadPoolExecutor(
                            max_workers=3) as ex:
                        futs = {ex.submit(fn): name for name, fn in jobs}
                        for f in concurrent.futures.as_completed(futs, timeout=90):
                            name = futs[f]
                            try:
                                r = f.result() or []
                                if r:
                                    hits.extend(r)
                                    self._trace.append(f"3eng_{name}:+{len(r)}")
                            except Exception:
                                pass
            except Exception:
                pass

        # 2e. supervisor + loop (按 plan) · v13.87.31 · 早停规则
        sup_dec = None
        if plan_obj.use_supervise_loop:
            sup_dec = self._supervise(query, hits,
                                        source=f"smart_{plan_obj.intent}")
            self._trace.append(f"supervise: {sup_dec.get('next_action')} "
                                f"conf={sup_dec.get('confidence')}")

            # v13.87.31 早停 (2 条件任一):
            # 1. conf >= 85 · 明显充分
            # 2. full hits >= 3 · 已经有 3 条深度内容
            full_hits = sum(1 for h in hits
                            if isinstance(h, dict)
                            and not h.get("_meta_only")
                            and h.get("completeness") == "full")
            if (sup_dec.get("confidence", 0) >= 85 or full_hits >= 3):
                self._trace.append(
                    f"早停: conf={sup_dec.get('confidence')} "
                    f"full_hits={full_hits} · 满足即停")
            else:
                # 循环追搜
                iters = 0
                while iters < plan_obj.max_iter and not sup_dec.get("is_sufficient"):
                    iters += 1
                    nq = sup_dec.get("next_query", "")
                    if not nq:
                        break
                    self._trace.append(f"追搜 [{iters}]: {nq[:60]}")
                    more = self._fanout(nq, sources=target_sources,
                                          top_k=top_k // 2, _purified=True)
                    hits.extend(more)
                    sup_dec = self._supervise(nq, hits,
                                                source=f"追搜_{iters}")
                    # 追搜后再判早停
                    full_hits = sum(1 for h in hits
                                    if isinstance(h, dict)
                                    and not h.get("_meta_only")
                                    and h.get("completeness") == "full")
                    if (sup_dec.get("is_sufficient")
                            or sup_dec.get("confidence", 0) >= 85
                            or full_hits >= 5):
                        self._trace.append(
                            f"早停 after iter {iters}: "
                            f"conf={sup_dec.get('confidence')} "
                            f"full={full_hits}")
                        break

        # 2f. bookmark 救 shell
        # (复用 deep mode 那段逻辑 · 走 _suggest_backends_by_domain)
        if hits and self.use_bookmark:
            shell_urls = [h.get("url", "") for h in hits
                          if isinstance(h, dict)
                          and h.get("completeness") == "shell"
                          and h.get("url", "").startswith("http")]
            if shell_urls:
                strategies = self._suggest_backends_by_domain(shell_urls)
                if strategies:
                    self._trace.append(
                        f"strategy_hit:{len(strategies)}/{len(shell_urls)} 站")

        elapsed = round(time.time() - t0, 2)
        self._trace.append(f"total:{elapsed}s · returned:{len(hits[:top_k])}")

        # 3. reflector 反思 (每次都跑 · 学下次)
        if reflect:
            self._reflect_after(query, plan_obj, hits[:top_k],
                                  sup_dec, elapsed)

        return hits[:top_k]

    def _reflect_after(self, query: str, plan_obj, hits: list,
                        sup_dec, elapsed: float):
        """异步反思 · 不阻塞返回"""
        try:
            from search_reflector import reflect_once
            from dataclasses import asdict
            reflect_once(
                query=query,
                intent=plan_obj.intent,
                plan=asdict(plan_obj),
                hits=hits,
                supervisor_decision=sup_dec,
                elapsed_s=elapsed,
                timeout=15,
            )
            self._trace.append("reflected")
        except Exception:
            pass

    # ═════ 主入口 · 一行调用 ═════
    def search(self, query: str, mode: str = "balanced",
               top_k: int = 10, target_sources: Optional[list] = None,
               http_only: bool = False, **kwargs) -> list:
        """一行搜 · 内部自动串所有 v13.87 组件

        mode:
          fast     · 一次 fanout · 无 supervisor · 500ms 秒返
          balanced · fanout + 一次 supervisor 判决 (默认 · 3-5s)
          deep     · fanout + supervise_loop 循环追搜 · 直到 sufficient (10-30s)

        http_only · True 时只走 HTTP 直连通道·绕 CDP·load 高不卡 (快通道)

        返: [{url, title, content, source, completeness, meta}]
        """
        self._trace = []
        t0 = time.time()
        # v13.88.9h · http_only 隐含 fast·且强制关 balanced/deep 的 friday CDP 扩展
        if http_only:
            mode = "fast"

        # v13.88.9L · 提纯已收口到 _fanout (公共底层·覆盖 search/search_smart/重搜)
        # 此处不再重复提纯·避免双重 LLM 调用
        # v56w(2026-08-06) · 保留原始 query·治「_to_search_phrase 规则截断把长题砍成
        #   垃圾(如30词Morarji题→『Who was the Prime』4词)·既丢答案线索·又让下面
        #   _wc>12 的 LLM 精简分支永不触发」。LLM 精简必须基于原题(理解从句语义)·
        #   词数判断也用原题词数。
        _orig_query = query
        # v13.88.83 · 用户 07-28 抓「google 质量差·最好短语·搜索很多 404」
        # 长整句 (>25 字) → 抽核心短语 · google/bing 对短语相关性高
        query = _to_search_phrase(query)

        # 1. cache 查
        if self.use_cache:
            cached = self._try_cache(query)
            if cached and mode == "fast":
                self._trace.append("returned_from_cache")
                return cached[:top_k]

        # 2. skill_forger 挑 source
        suggested = self._suggest_sources(query)
        if suggested and not target_sources:
            target_sources = [s["source"] for s in suggested]
            self._trace.append(f"forger_picked:{target_sources}")

        # v56j(2026-08-05) · anysearch 保底注入(治「forger历史经验只挑friday_bing·
        #   把新源anysearch挤掉→单次搜索命中率低3/8」)。
        # v13.94(2026-08-12) · search_fabric 降级改造:
        #   原逻辑「英文题总是注入 anysearch」导致它消耗 1000次/日 quota·
        #   quota 耗尽时整题报废(user_daily_quota_exhausted 断链)。
        #   新逻辑: 先走 search_fabric L0(ddgs+brave+wikipedia·免费无限)·
        #   只在 L0 没有足够结果时才把 anysearch 加进 fallback·
        #   quota 从"瓶颈"变"保险"·断链不可能再发生。
        import re as _re2
        _is_en = len(_re2.findall(r"[A-Za-z]", query)) > len(_re2.findall(r"[一-龥]", query))
        if _is_en and target_sources and "anysearch" not in target_sources:
            # 先检查 search_fabric L0 配额·有余量则不注入 anysearch(留着做保险)
            _fabric_has_budget = True
            try:
                from search_fabric import _get_budget as _sfb
                _bm = _sfb()
                # L0 至少 ddgs 还能用·就不需要 anysearch 保底
                _fabric_has_budget = _bm.can_use("ddgs")
            except Exception:
                pass
            if _fabric_has_budget:
                # L0 够用·anysearch 不作主力保底 (节省 quota·等真的 L0 都断了再用)
                self._trace.append("anysearch_skipped_L0_available")
            else:
                # L0 也耗尽了·才注入 anysearch 作最终 fallback
                target_sources = list(target_sources) + ["anysearch"]
                self._trace.append("anysearch_ensured_L0_exhausted")

        # v56n(2026-08-05) · tavily 保底注入(治「QA主链绕过planner·tavily接了src_map
        #   但QA没调用→难题精确答案(0.1777/EC编号)Tavily能抽但没进证据池」)。tavily的
        #   answer字段直接给综合答案·补觅游「从长文抽精确数字」缺口。英文题保底加。
        if _is_en and target_sources and "tavily" not in target_sources:
            target_sources = list(target_sources) + ["tavily"]
            self._trace.append("tavily_ensured")

        # v56t(2026-08-06) · 长 query 精简+多路(用户「长query拆成精简query·精简query有用」)。
        #   实证: 完整GAIA原题「...first place mentioned in Book of Esther」搜不到Morarji·
        #   精简「Prime Minister of India April 1977」就命中。长啰嗦query带偏搜索引擎。
        #   做法: query 词数 >12 时·除原query外·再并行搜1个精简版(_rewrite_variants首个=压核心)·
        #   合并去重。多路覆盖·命中率↑·且为下游抽答提供含答案证据。热路径规则精简(不调LLM·快)。
        # v56w · 词数判断用原题(_to_search_phrase 已把 query 截短·会误判为短query)
        _wc = len(_orig_query.split())
        hits = self._fanout(query, sources=target_sources, top_k=top_k,
                            http_only=http_only)
        # 长 query 总是并行搜精简版(多路覆盖)·因为「搜到内容」≠「搜到含答案的内容」·
        #   完整原题常被从句带偏(实证Morarji题:原题9条不含答案·精简版命中)。
        if _wc > 12:
            try:
                # v56u · 规则精简对从句包裹的题出垃圾(April who was the)·改 LLM 精简·
                #   能理解「Book of Esther第一个地名→波斯/印度·要查首相」抽出核心检索词。
                #   长query值得一次haiku(~2s)换命中(实证:原题搜不到·精简版命中Morarji)。
                # v56w · 喂原题(_orig_query)·让 LLM 理解完整从句语义抽核心·
                #   不能喂被 _to_search_phrase 截断后的垃圾。
                _slim = self._llm_slim_query(_orig_query)
                if _slim and _slim.strip() and _slim.strip().lower() != _orig_query.lower():
                    self._trace.append(f"slim_query:「{_orig_query[:18]}」→「{_slim[:34]}」")
                    _slim_hits = self._fanout(_slim, sources=target_sources,
                                              top_k=top_k, http_only=http_only)
                    _seen = {h.get("url") for h in hits if h.get("url")}
                    hits = hits + [h for h in _slim_hits
                                   if h.get("url") and h.get("url") not in _seen]
            except Exception as _se:
                self._trace.append(f"slim_err:{type(_se).__name__}")

        # v13.88.9 · 重试下沉 · 用户「搜索重试不是 SearchAgent 的职责吗?」
        # fast 模式搜完判质量·空/全壳 → 多变体改写重搜 1 轮·选最优
        # 这才能纠正「一开始 query 就蠢·搜不到」的问题 (用户 Q7)
        if mode == "fast":
            hits = self._retry_if_poor(query, hits, target_sources, top_k,
                                       http_only=http_only)

        # v13.87.26 · deep mode · last30days skill (memory 硬门明确用户明令)
        # [[use-last30days-skill-for-search]] 「搜索优先用 last30days · 而不是 WebSearch」
        # 8 源: reddit/x/youtube/tiktok/hn/polymarket/github/web
        if mode == "deep" and hits:
            try:
                from last30days_wrapper import (available as _l30_ok,
                                                  search as _l30_search)
                if _l30_ok():
                    # 挑 3 源避免太慢
                    l30_hits = _l30_search(query,
                                             sources=["reddit", "hn", "web"],
                                             quick=True, timeout=60)
                    if l30_hits:
                        hits.extend(l30_hits[:10])
                        self._trace.append(f"last30days:+{len(l30_hits[:10])} "
                                            f"(memory 硬门)")
            except Exception as e:
                self._trace.append(f"last30days_err:{type(e).__name__}")

        # v13.87.27 · aisa 拆 (用户 2026-07-23 明确「aisa 先不用了」)
        # 保留 wrapper 文件 · 上游可显式调 · 但 SearchAgent 默认不走 aisa

        # v13.87.25 · deep mode · Google/Bing/Zhipu 三引擎并行 · 广度 3 → 30+
        # v40 · balanced 也走 Friday isFast=false 平推 (P0)
        # 用户 2026-07-24 抓「CDP 排队才是真瓶颈·不是 worker 数」
        # Friday 服务端爬正文 · 不占本地 CDP · 深读量降 75%
        # 仅当 MEYO_QA_BALANCED_DEEP_FRIDAY=0 时才关
        _bal_deep_friday = os.environ.get("MEYO_QA_BALANCED_DEEP_FRIDAY", "1") == "1"
        if (mode == "deep" or self.use_deep_google or (mode == "balanced" and _bal_deep_friday)) and hits:
            try:
                import concurrent.futures
                from friday_universal import (search_google_deep,
                                                 search_bing_deep, search)
                is_en = not any("一" <= c <= "龥" for c in query)
                jobs = []
                if is_en or "google" in (target_sources or []):
                    # v13.88.9 · 禁 google 首发避验证码 · bing+zhipu 冗余
                    # google_deep 仅 use_deep_google 或 显式 target_sources 含 google 时加
                    jobs = [
                        ("bing_deep",
                         lambda: search_bing_deep(query, top_k=10, timeout=60)),
                        ("zhipu",
                         lambda: search(query, ["zhipu-search"],
                                         top_k=10, timeout=15)),
                    ]
                    if self.use_deep_google or "google" in (target_sources or []):
                        jobs.insert(0, ("google_deep",
                            lambda: search_google_deep(query, top_k=10, timeout=60)))
                if jobs:
                    with concurrent.futures.ThreadPoolExecutor(
                            max_workers=3) as ex:
                        futs = {ex.submit(fn): name for name, fn in jobs}
                        broadened = 0
                        for f in concurrent.futures.as_completed(
                                futs, timeout=90):
                            name = futs[f]
                            try:
                                r = f.result() or []
                                if r:
                                    hits.extend(r)
                                    broadened += len(r)
                                    self._trace.append(
                                        f"broad_{name}:+{len(r)}")
                            except Exception as e:
                                self._trace.append(
                                    f"broad_{name}_err:{type(e).__name__}")
                        if broadened:
                            self._trace.append(
                                f"广度总补 +{broadened} 条 (3 引擎并行)")
            except Exception as e:
                self._trace.append(f"broad_err:{type(e).__name__}")

        # v13.87.25 · Q4 · 学网站怎么用 · fetch_strategy 组合救援
        # shell 的 hit · 用 primary → fallback 组合抓
        if mode == "deep" and hits:
            shell_urls = [h.get("url", "") for h in hits
                          if isinstance(h, dict)
                          and h.get("completeness") == "shell"
                          and h.get("url", "").startswith("http")]
            if shell_urls:
                strategies = self._suggest_backends_by_domain(shell_urls)
                if strategies:
                    try:
                        from search_agent_orchestrator import smart_fetch
                        from urllib.parse import urlparse
                        import concurrent.futures as _cf

                        # v13.87 · A 优化 · 用户 2026-07-23 抓「Q1 又 5 min」
                        # 前:串行 for h in hits · 30 shell × 8s smart_fetch = 4 min
                        # 后:5 并发 ThreadPoolExecutor · 30 shell / 5 × 8s = ~50s
                        def _rescue_one(h):
                            if not isinstance(h, dict):
                                return None
                            if h.get("completeness") != "shell":
                                return None
                            url = h.get("url", "")
                            dom = urlparse(url).netloc.lower()
                            strat = strategies.get(dom)
                            if not strat:
                                return None
                            for backend in [strat.get("primary"),
                                             strat.get("fallback")]:
                                if not backend or backend == "cdp":
                                    continue
                                try:
                                    r = smart_fetch(url, prefer=backend)
                                    c = r.get("content", "")
                                    if c and len(c) > 500:
                                        return (h, c[:5000], backend, strat)
                                except Exception:
                                    continue
                            return None

                        rescued = 0
                        # 只对 shell hit 做 rescue · 避开无 strategy 的
                        cand = [h for h in hits if isinstance(h, dict)
                                and h.get("completeness") == "shell"]
                        with _cf.ThreadPoolExecutor(max_workers=5) as ex:
                            futs = [ex.submit(_rescue_one, h) for h in cand]
                            for f in _cf.as_completed(futs, timeout=180):
                                try:
                                    r = f.result()
                                except Exception:
                                    continue
                                if not r:
                                    continue
                                h, content, used_backend, strat = r
                                h["content"] = content
                                n = len(content)
                                h["completeness"] = ("full" if n > 1000
                                                       else "partial")
                                h["meta"] = h.get("meta") or {}
                                h["meta"]["rescued_by"] = used_backend
                                h["meta"]["strategy_source"] = strat.get(
                                    "source", "unknown")
                                rescued += 1
                        if rescued:
                            self._trace.append(
                                f"strategy_rescue:{rescued} 救活 "
                                f"(v13.87 · 5 并发 primary→fallback)")
                    except Exception as e:
                        self._trace.append(
                            f"strategy_rescue_err:{type(e).__name__}")

        # 4. supervisor (balanced/deep)
        if mode in ("balanced", "deep"):
            dec = self._supervise(query, hits, source="fanout")
            self._trace.append(f"supervise:{dec.get('next_action')}·"
                                f"conf={dec.get('confidence')}")

            # 5. deep 模式循环追搜
            if mode == "deep" and not dec.get("is_sufficient"):
                iters = 0
                while iters < self.max_supervise_iter:
                    iters += 1
                    nq = dec.get("next_query", "")
                    if not nq:
                        break
                    self._trace.append(f"追搜 [{iters}]: {nq}")
                    more = self._fanout(nq, sources=target_sources,
                                          top_k=top_k // 2, _purified=True)
                    hits.extend(more)
                    dec = self._supervise(nq, hits, source=f"追搜_{iters}")
                    if dec.get("is_sufficient"):
                        break

        # 6. stuck 兜底 (deep 且全空时升级浏览器真交互 · v13.88.9L-hard)
        hits = self._try_stuck_fallback(query, hits, mode=mode)

        # 7. forger 记录 (供下次挑源)
        if hits and self.use_forger:
            # 每个 source 分开记
            by_source = {}
            for h in hits:
                s = h.get("source", "unknown")
                by_source.setdefault(s, []).append(h)
            for s, hs in by_source.items():
                self._record_forger(query, s, hs)

        elapsed = round(time.time() - t0, 2)
        self._trace.append(f"total:{elapsed}s · returned:{len(hits[:top_k])}")

        # 附 trace 供上游 debug (最后一条 meta only)
        result = hits[:top_k]
        return result

    def get_trace(self) -> list:
        """看上一次 search 的调用轨迹"""
        return list(self._trace)


# ═════ 快捷函数 · 上游最省心 ═════
_default_agent = None


def search(query: str, mode: str = "balanced", top_k: int = 10,
           **kwargs) -> list:
    """模块级快捷 · 用默认 SearchAgent 单例
        from search_agent import search
        hits = search("RAG 评测")
    """
    global _default_agent
    if _default_agent is None:
        _default_agent = SearchAgent()
    return _default_agent.search(query, mode=mode, top_k=top_k, **kwargs)


if __name__ == "__main__":
    try:
        import argparse, json
        p = argparse.ArgumentParser(description="SearchAgent · 统一门面")
        p.add_argument("query", nargs="?", default="RAG 评测")
        p.add_argument("--mode", default="balanced",
                        choices=["fast", "balanced", "deep"])
        p.add_argument("--top", type=int, default=5)
        p.add_argument("--sources", default="",
                        help="逗号分隔 · 如 'zhihu,friday_bing'")
        p.add_argument("--trace", action="store_true", help="显示 trace")
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    sa = SearchAgent()
    srcs = args.sources.split(",") if args.sources else None
    hits = sa.search(args.query, mode=args.mode, top_k=args.top,
                       target_sources=srcs)
    print(f"返 {len(hits)} 条")
    for h in hits[:5]:
        m = h.get("meta", {})
        print(f"  · {h.get('title','')[:60]}")
        print(f"    src={h.get('source','?')} comp={h.get('completeness','?')} "
              f"len={m.get('content_len', len(h.get('content','')))}")
    if args.trace:
        print()
        print("=== trace ===")
        for step in sa.get_trace():
            print(f"  · {step}")
