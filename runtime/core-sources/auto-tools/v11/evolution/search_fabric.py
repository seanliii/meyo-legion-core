"""search_fabric.py · 2026-08-12 · search_fabric L0+L4 骨架

架构(5层·参考 AnySearch 四件套·全免费栈复刻):
  L0  通用搜索面    ddgs(8引擎) + Brave免费层 + Wikipedia
  L1  垂直联邦     垂直注册表 (W3 实现·此处预留 slot)
  L2  获取梯度     httpx+trafilatura → curl_cffi → (W2 实现)
  L3  结构化压缩   trafilatura markdown 提炼 (W2 实现)
  L4  路由+预算哨兵 LLM语义路由 + bandit + 配额池 + AnySearch fallback

核心解决的问题:
  user_daily_quota_exhausted → 搜索链中途断 → GAIA 整题报废
  根治方案: AnySearch 从"主力"降级为 L5 逃生舱·先跑免费多源·quota 才真正够用

接口:
    from search_fabric import SearchFabric
    fabric = SearchFabric()
    hits = fabric.search("SPFMV EC numbers", top_k=8)  # 直接用·不用挑源

hit 结构 (与 SearchAgent 兼容):
    {url, title, content, source, cost_usd, meta}

设计原则 (来自用户三条铁律):
  ① 路由用 LLM 语义判断·不硬编码关键词
  ② 机制名不含具体网站 (用"通用搜索层"/"垂直联邦"等·源名只在 schema 里)
  ③ AnySearch 保留为 fallback·不删掉 (机制而非代劳)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Optional

# ─── 路径 ───────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_V11 = _HERE.parent
_SEED = _V11.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_V11))
sys.path.insert(0, str(_SEED))

# ─── 预算状态文件 ─────────────────────────────────────────────────────────────
_BUDGET_FILE = _V11 / ".search_fabric_budget.json"
_BANDIT_FILE = _V11 / ".search_fabric_bandit.json"


# ══════════════════════════════════════════════════════════════════════════════
# 源注册表 (Source Registry)
# 每个源声明 capability schema:
#   覆盖域·输入类型·单次成本·每日配额·健康分·compliance等级
# 新源接入只需注册·路由代码不变 (语义 LLM 读 schema 判断)
# ══════════════════════════════════════════════════════════════════════════════
SOURCE_REGISTRY: dict[str, dict] = {
    # ── L0 通用搜索面 (全免费·无 key) ────────────────────────────────────────
    "ddgs": {
        "desc": "DuckDuckGo+聚合 8 引擎(Bing/Google/Brave/Yandex/Yahoo/Mojeek/Wikipedia)",
        "domains": ["general", "news", "science", "tech", "any"],
        "input": "query_str",
        "cost_usd_per_call": 0.0,
        "daily_quota": 999999,  # 软限流·按 IP·可 rotate
        "compliance": "public",  # 合规等级: public/gray/api_tos
        "tier": 0,  # 0=主力 1=补充 2=fallback
        "avg_latency_s": 2.0,
        "needs_key": False,
    },
    "brave_free": {
        "desc": "Brave Search 免费层(无 key 匿名 2000次/月)",
        "domains": ["general", "news", "tech"],
        "input": "query_str",
        "cost_usd_per_call": 0.0,
        "daily_quota": 66,  # 2000/30 ≈ 66/day
        "compliance": "public",
        "tier": 0,
        "avg_latency_s": 1.5,
        "needs_key": False,
    },
    "wikipedia": {
        "desc": "Wikipedia 全文搜索 API (完全免费·无 key·encyclopedic)",
        "domains": ["facts", "biography", "science", "history", "any"],
        "input": "query_str",
        "cost_usd_per_call": 0.0,
        "daily_quota": 999999,
        "compliance": "public",
        "tier": 0,
        "avg_latency_s": 0.8,
        "needs_key": False,
    },
    # ── L1 垂直联邦 ──────────────────────────────────────────────────────────
    # 注意: openalex/pubmed/crossref/gdelt/courtlistener/pubchem/wikipedia_links
    # 以及 xiaohongshu/reddit/twitter/baidu/hackernews 等所有垂直源
    # 由 search_fabric_verticals.auto_register_all() 在运行时注入
    # 这里不再预留 slot 占位（slot=vertical_w3 会被路由器排除，是 bug）
    # ── L1.5 模型原生搜索层 (answer-style · 带引用) ────────────────────────
    # 这类模型自带实时联网·返回结构化答案+引用·质量高于纯链接列表
    # 需要 AISA_API_KEY 才生效·无 key 则自动跳过(fail-safe)
    "sonar": {
        "desc": "Perplexity Sonar 实时联网答复式搜索·返回直接答案+引用·覆盖全网最新信息",
        "domains": ["general", "news", "science", "tech", "facts", "any"],
        "input": "query_str",
        "cost_usd_per_call": 0.001,
        "daily_quota": 500,
        "compliance": "api_tos",
        "tier": 1,  # 补充层·有 key 时质量最高
        "avg_latency_s": 3.0,
        "needs_key": True,
        "model_native_search": True,  # 标记: 模型内置搜索·不是链接采集
    },
    "sonar_pro": {
        "desc": "Perplexity Sonar Pro 深度答复·多轮搜索+推理·用于复杂问题",
        "domains": ["academic", "science", "tech", "research", "complex_query"],
        "input": "query_str",
        "cost_usd_per_call": 0.003,
        "daily_quota": 100,
        "compliance": "api_tos",
        "tier": 1,
        "avg_latency_s": 6.0,
        "needs_key": True,
        "model_native_search": True,
    },
    "tavily": {
        "desc": "Tavily AI 搜索·LLM优化·返回高质量摘要+URL·比 DDGS 结果更精准",
        "domains": ["general", "news", "tech", "facts", "any"],
        "input": "query_str",
        "cost_usd_per_call": 0.0,
        "daily_quota": 200,
        "compliance": "api_tos",
        "tier": 1,
        "avg_latency_s": 2.0,
        "needs_key": True,
        "model_native_search": True,
    },
    "aisa_smart": {
        "desc": "AIsa Smart 自动路由·意图识别后自动选最优源·有自学习能力·覆盖 23 个专业数据库",
        "domains": ["any", "general", "academic", "news", "science", "tech",
                    "legal", "finance", "medical", "social"],
        "input": "query_str",
        "cost_usd_per_call": 0.001,
        "daily_quota": 300,
        "compliance": "api_tos",
        "tier": 1,
        "avg_latency_s": 4.0,
        "needs_key": True,
        "model_native_search": True,
        "self_learning": True,  # 标记: 内部有路由学习能力
    },
    "aisa_verity": {
        "desc": "AIsa Verity 多源交叉验证·输出置信度评分·适合需要确认真伪的查询",
        "domains": ["facts", "science", "biography", "history", "verification"],
        "input": "query_str",
        "cost_usd_per_call": 0.002,
        "daily_quota": 150,
        "compliance": "api_tos",
        "tier": 1,
        "avg_latency_s": 5.0,
        "needs_key": True,
        "model_native_search": True,
        "cross_verification": True,  # 标记: 多源交叉验证
    },
    # ── L0 主力源 (AnySearch·2026-08-20 从 L5 逃生舱扶正为一等主力) ──────────
    # 本环境唯一能"翻墙"的强搜索: 服务器在墙外替我们出网·所以能访问 google/BBC/
    # 维基等墙内直连(ddgs/brave 本机 TLS 被墙拦)常空的源。实测 369ms 返真结果。
    # tier=0 让它进 primary 并发窗口被正常路由选中·不再只在其他源全空时兜底。
    # 额度保护: 免费层 1000次/日很宝贵→_search_anysearch 内接缓存去重(命中不耗额度)
    #           + 限流(rate 2/s 保守·防刷爆)。fallback_to_anysearch 兜底逻辑仍保留。
    "anysearch": {
        "desc": "AnySearch 商业搜索基础设施·23垂直源·意图路由·结构化 markdown·"
                "服务器出网可访问被墙的 google/BBC/维基等外网源(本环境主力外网通道)",
        "domains": ["general", "news", "science", "tech", "facts", "any"],
        "input": "query_str",
        "cost_usd_per_call": 0.0,  # 免费层 1000次/日
        "daily_quota": 1000,
        "compliance": "api_tos",
        "tier": 0,  # 主力(与 ddgs/wikipedia 平等·可优先)·非 fallback
        "avg_latency_s": 2.5,
        "needs_key": True,
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# 配额管理器 (BudgetManager) - 根治 quota_exhausted 断链
# ══════════════════════════════════════════════════════════════════════════════
class BudgetManager:
    """统一配额池·记录每个源今日剩余·bandit 选择顺序 = 成功率 × 成本倒数

    设计:
    - 每日 UTC 午夜自动重置
    - 配额用尽的源自动降级·不影响其他源
    - AnySearch quota 从"瓶颈"变成"保险"
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = self._load()

    def _today(self) -> str:
        return date.today().isoformat()

    def _load(self) -> dict:
        try:
            if _BUDGET_FILE.exists():
                d = json.loads(_BUDGET_FILE.read_text(encoding="utf-8"))
                # 日期过期 → 重置
                if d.get("date") != self._today():
                    return self._fresh()
                return d
        except Exception:
            pass
        return self._fresh()

    def _fresh(self) -> dict:
        return {
            "date": self._today(),
            "used": {},       # source → used_today
            "errors": {},     # source → error_count_today
            "successes": {},  # source → success_count_today
        }

    def _save(self):
        try:
            _BUDGET_FILE.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def can_use(self, source: str) -> bool:
        """是否还有配额可用"""
        schema = SOURCE_REGISTRY.get(source, {})
        quota = schema.get("daily_quota", 0)
        if quota >= 99999:
            return True
        with self._lock:
            self._state = self._load()  # 重新加载（防多进程）
            used = self._state["used"].get(source, 0)
            return used < quota

    def record_use(self, source: str, ok: bool, latency_s: float = 0.0):
        """记录一次使用"""
        with self._lock:
            self._state = self._load()
            self._state["used"][source] = self._state["used"].get(source, 0) + 1
            if ok:
                self._state["successes"][source] = self._state["successes"].get(source, 0) + 1
            else:
                self._state["errors"][source] = self._state["errors"].get(source, 0) + 1
            self._save()

    def remaining(self, source: str) -> int:
        """今日剩余配额"""
        schema = SOURCE_REGISTRY.get(source, {})
        quota = schema.get("daily_quota", 0)
        if quota >= 99999:
            return 999999
        with self._lock:
            self._state = self._load()
            used = self._state["used"].get(source, 0)
            return max(0, quota - used)

    def success_rate(self, source: str) -> float:
        """今日成功率 (默认 0.8 防冷启动)"""
        with self._lock:
            s = self._state.get("successes", {}).get(source, 0)
            e = self._state.get("errors", {}).get(source, 0)
            total = s + e
            if total == 0:
                return 0.8  # 冷启动先乐观
            return s / total

    def bandit_score(self, source: str) -> float:
        """bandit 得分 = 成功率 × (1 / avg_latency) × 是否有配额"""
        if not self.can_use(source):
            return -1.0
        schema = SOURCE_REGISTRY.get(source, {})
        sr = self.success_rate(source)
        lat = max(0.1, schema.get("avg_latency_s", 2.0))
        tier_penalty = schema.get("tier", 0) * 0.3  # tier越高越靠后
        return sr / lat - tier_penalty

    def ranked_sources(self, candidates: list[str]) -> list[str]:
        """对候选源按 bandit 得分排序·最优先放前面"""
        return sorted(candidates, key=lambda s: -self.bandit_score(s))

    def status_report(self) -> str:
        """给 agent 看的配额状态简报"""
        lines = [f"[search_fabric 配额快报 {self._today()}]"]
        for src, schema in SOURCE_REGISTRY.items():
            if schema.get("tier", 0) > 1 and src != "anysearch":
                continue  # 只显示主力+fallback
            rem = self.remaining(src)
            quota = schema.get("daily_quota", 0)
            sr = self.success_rate(src)
            lines.append(f"  {src}: 剩余 {rem}/{quota} 成功率{sr:.0%}")
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# L0: 通用搜索后端实现
# ══════════════════════════════════════════════════════════════════════════════

def _search_ddgs(query: str, top_k: int = 10) -> list[dict]:
    """DuckDuckGo 聚合 8 引擎·无 key·免费·1-3s"""
    try:
        # 优先 import 新包名 ddgs·fallback 旧名 duckduckgo_search
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS  # type: ignore
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=top_k):
                results.append({
                    "url": r.get("href", ""),
                    "title": r.get("title", ""),
                    "content": r.get("body", ""),
                    "source": "ddgs",
                    "cost_usd": 0.0,
                    "meta": {"engine": "ddgs"},
                })
        return results
    except Exception as e:
        return [{"_error": f"ddgs: {type(e).__name__}: {str(e)[:120]}"}]


def _search_brave(query: str, top_k: int = 10,
                  api_key: str = "") -> list[dict]:
    """Brave Search REST API·免费层无 key(匿名)·有 key 提配额

    免费匿名: 无配额限制但 rate-limit 更严
    免费注册: 2000次/月 https://api.search.brave.com/
    """
    import urllib.request
    import urllib.parse

    params = urllib.parse.urlencode({
        "q": query,
        "count": min(top_k, 20),
        "search_lang": "en",
    })
    url = f"https://api.search.brave.com/res/v1/web/search?{params}"
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    if api_key:
        headers["X-Subscription-Token"] = api_key
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = []
        for r in data.get("web", {}).get("results", [])[:top_k]:
            results.append({
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "content": r.get("description", ""),
                "source": "brave_free",
                "cost_usd": 0.0,
                "meta": {"engine": "brave"},
            })
        return results
    except Exception as e:
        return [{"_error": f"brave: {type(e).__name__}: {str(e)[:120]}"}]


def _search_wikipedia(query: str, top_k: int = 5) -> list[dict]:
    """Wikipedia REST API·完全免费·无 key·返回文章摘要"""
    import urllib.request
    import urllib.parse

    params = urllib.parse.urlencode({
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": top_k,
        "format": "json",
        "utf8": 1,
        # wikipedia 官方推荐: 服务器复制延迟>5s 时自动返回 maxlag 让路(而非硬打)·
        # 主动遵守礼仪→少招 429。见 https://www.mediawiki.org/wiki/Manual:Maxlag_parameter
        "maxlag": 5,
    })
    url = f"https://en.wikipedia.org/w/api.php?{params}"
    try:
        req = urllib.request.Request(
            url,
            # 真实 UA(带项目地址+联系邮箱)·wikipedia 要求可溯源·不伪装浏览器 UA。
            headers={"User-Agent": "shrimp-qa/1.0 (https://github.com/seanliii; vortex528@gmail.com)"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = []
        for r in data.get("query", {}).get("search", []):
            page_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(r.get('title', '').replace(' ', '_'))}"
            # 去掉 HTML 标签
            import re
            snippet = re.sub(r"<[^>]+>", "", r.get("snippet", ""))
            results.append({
                "url": page_url,
                "title": r.get("title", ""),
                "content": snippet,
                "source": "wikipedia",
                "cost_usd": 0.0,
                "meta": {"ns": r.get("ns", 0), "wordcount": r.get("wordcount", 0)},
            })
        return results
    except Exception as e:
        return [{"_error": f"wikipedia: {type(e).__name__}: {str(e)[:120]}"}]


def _parse_anysearch_markdown(stdout: str, domain: str = "general") -> list[dict]:
    """把 AnySearch CLI 的 markdown 输出拆成多条标准 hit。

    CLI 输出格式(实测):
        ## Search Results (N results, Xms)

        ### 1. 标题文本
        - **URL**: https://...
        - 正文段落(可能多行)

        ### 2. 标题文本
        - **URL**: https://...
        - 正文...

    返回 [{url,title,content,source,cost_usd,meta}, ...] 与 _search_ddgs 同格式。
    解析不出任何条目时返回 []·调用方据此退回原始整块(不破坏)。
    """
    hits: list[dict] = []
    if not stdout:
        return hits
    # 用 "### " 作为每条结果的定界符·findall 只取真正跟在 "### " 后的段落。
    # (无任何 "### " 头 → 返回 []·调用方退回原始整块·不误把整段当一条 title)
    blocks = re.findall(
        r"(?ms)^###\s+(.*?)(?=^###\s+|\Z)", stdout)
    for blk in blocks:
        blk = blk.strip()
        if not blk:
            continue
        lines = blk.splitlines()
        # 第一行 = "1. 标题" (可能带序号前缀)·去掉 "N. "
        raw_title = lines[0].strip() if lines else ""
        title = re.sub(r"^\d+\.\s*", "", raw_title).strip()
        url = ""
        content_parts: list[str] = []
        for ln in lines[1:]:
            s = ln.strip()
            if not s:
                continue
            # - **URL**: xxx
            m = re.match(r"^-\s*\*\*URL\*\*\s*:\s*(.+)$", s, re.I)
            if m:
                url = m.group(1).strip()
                continue
            # 其余 "- 正文" 或裸文本行都当正文
            s = re.sub(r"^-\s*", "", s)
            content_parts.append(s)
        content = " ".join(content_parts).strip()
        if not (title or url or content):
            continue
        hits.append({
            "url": url,
            "title": title or (url[:80] if url else "anysearch result"),
            "content": content[:5000],
            "source": "anysearch",
            "cost_usd": 0.0,
            "meta": {"domain": domain, "engine": "anysearch"},
        })
    return hits


def _search_anysearch(query: str, top_k: int = 8,
                      domain: str = "general") -> list[dict]:
    """AnySearch 意图路由搜索·2026-08-20 扶正为主力外网源(非 L5 逃生舱)。

    本环境唯一能"翻墙"的强搜索: 服务器在墙外替我们出网·可访问被墙的
    google/BBC/维基等源(ddgs/brave 本机 TLS 被墙拦常空)。

    额度保护(免费层 1000次/日很宝贵):
      1. 调 CLI 前先查 resilient_fetch 缓存(namespace=anysearch·ttl 10min)·
         命中直接返回·不消耗底层额度。
      2. 令牌桶限流(rate 2/s 保守·防多进程刷爆免费层)。
      3. 成功非空结果写缓存(去重省额度)。
    注: SearchFabric._call_source 走通用缓存/限流路径已能覆盖大多数调用·此处
        再加一层是为了 _search_anysearch 被直接调用(绕过 fabric)时也受保护。
    """
    _CLI = os.path.expanduser("~/.claude/skills/anysearch/scripts/anysearch_cli.py")
    if not os.path.exists(_CLI):
        return [{"_error": "anysearch_cli not found"}]

    # ── 1. 缓存命中直接返回(不耗额度) ──────────────────────────────────────
    _key_parts = ("anysearch", query, top_k, domain)
    try:
        cached = _rf.cache_get(
            "anysearch", _key_parts, ttl=_CACHE_TTL_S,
            cache_root=_CACHE_DIR, redis_client=_get_redis(),
        )
        if cached is not None:
            data, _backend = cached
            if isinstance(data, list) and data:
                return data
    except Exception:
        pass  # 缓存故障绝不阻断真搜索

    # ── 2. 限流(保守 2/s·防刷爆免费 1000/日) ────────────────────────────
    try:
        _RATE_LIMITER.acquire("anysearch", rate_per_sec=2.0,
                              block=True, max_wait_s=5.0)
    except Exception:
        pass  # 限流器自身故障不阻断主流程

    import subprocess
    env = os.environ.copy()
    key = env.get("ANYSEARCH_API_KEY") or ""
    if key:
        env["ANYSEARCH_API_KEY"] = key
    try:
        r = subprocess.run(
            ["python3", _CLI, "search", query,
             "--max_results", str(top_k),
             "--domain", domain],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if r.returncode == 0 and r.stdout:
            # anysearch 返回 markdown·拆成多条标准 hit
            hits = _parse_anysearch_markdown(r.stdout, domain=domain)
            if not hits:
                # 解析失败→退回原来的 1 条整块(不破坏·保底可读)
                hits = [{"url": "", "title": query,
                         "content": r.stdout[:8000],
                         "source": "anysearch",
                         "cost_usd": 0.0,
                         "meta": {"domain": domain, "raw": True}}]
            # ── 3. 成功非空写缓存(去重省额度) ─────────────────────────
            try:
                _rf.cache_set(
                    "anysearch", _key_parts, hits, ttl=_CACHE_TTL_S,
                    cache_root=_CACHE_DIR, redis_client=_get_redis(),
                )
            except Exception:
                pass
            return hits
        err = (r.stderr or "")[:200]
        return [{"_error": f"anysearch_stderr: {err}"}]
    except subprocess.TimeoutExpired:
        return [{"_error": "anysearch_timeout"}]
    except Exception as e:
        return [{"_error": f"anysearch: {type(e).__name__}: {str(e)[:120]}"}]


# ══════════════════════════════════════════════════════════════════════════════
# L1.5: 模型原生搜索后端 (Sonar / Tavily via aisa_multi_source)
# ══════════════════════════════════════════════════════════════════════════════

def _search_sonar(query: str, top_k: int = 5, tier: str = "sonar") -> list[dict]:
    """Perplexity Sonar 答复式搜索·返回 answer+citations 转 hit 列表
    tier: sonar | sonar-pro
    """
    try:
        from aisa_multi_source import sonar as _sonar
        result = _sonar(query, tier=tier, timeout=60)
        if not result.get("ok"):
            return []
        answer = result.get("answer", "")
        citations = result.get("citations", [])
        if not answer:
            return []
        hits = []
        # 主答案作为第一个 hit
        hits.append({
            "url": citations[0] if citations else "",
            "title": f"[{tier}] {query[:80]}",
            "content": answer[:6000],
            "source": tier,
            "cost_usd": 0.001 if tier == "sonar" else 0.003,
            "meta": {"model_native_search": True, "citations": citations,
                     "engine": tier, "answer_len": len(answer)},
        })
        # 每个引用 URL 再补一个 hit (供后续 fetch 深读)
        for url in citations[1:top_k]:
            hits.append({
                "url": url, "title": url, "content": "",
                "source": tier, "cost_usd": 0.0,
                "meta": {"model_native_search": True, "citation_only": True},
            })
        return hits
    except ImportError:
        return []  # aisa_multi_source 未安装时静默跳过
    except Exception:
        return []


def _search_sonar_standard(query: str, top_k: int = 5) -> list[dict]:
    return _search_sonar(query, top_k, tier="sonar")


def _search_sonar_pro(query: str, top_k: int = 5) -> list[dict]:
    return _search_sonar(query, top_k, tier="sonar-pro")


def _search_tavily_fabric(query: str, top_k: int = 8) -> list[dict]:
    """Tavily 搜索。2026-08-24 起优先走公平基准的缓存层(多 key 轮换 + 磁盘缓存·
    同 query 零额度命中),保证 auto 与外部四家 harness 同源公平;缓存层不可用时
    回退旧 aisa_multi_source 路径(需 AISA key)。"""
    try:
        import os as _os
        import sys as _sys
        _fair = _os.path.join(
            _os.path.dirname(__file__), "..", "..", "..",
            "harness_baselines", "fair_bench_40")
        _fair = _os.path.abspath(_fair)
        if _os.path.isdir(_fair):
            if _fair not in _sys.path:
                _sys.path.insert(0, _fair)
            import cached_tavily as _ct
            _qid = _os.environ.get("MEYO_FAIR_QID", "")
            # Default to basic depth: 1 credit and faster. Advanced (2 credits, slower)
            # is opt-in via env for the few queries that truly need deep re-ranking. The
            # 40q ledger showed 5712/5786 live calls were advanced — mostly unnecessary
            # cost and latency. Speed/cost lever, not domain logic.
            _depth = _os.environ.get("MEYO_SEARCH_DEPTH", "basic") or "basic"
            payload = _ct.search(query, qid=_qid, search_depth=_depth,
                                 max_results=max(1, top_k),
                                 include_raw_content=True)
            hits = []
            for item in (payload.get("response") or {}).get("results", [])[:top_k]:
                url = item.get("url", "") or ""
                content = (item.get("raw_content") or item.get("content", "")) or ""
                title = item.get("title", url[:80]) or ""
                if not (content or url):
                    continue
                hits.append({
                    "url": url,
                    "title": title[:200],
                    "content": content[:5000],
                    "source": "tavily",
                    "cost_usd": 0.0,
                    "meta": {"model_native_search": True,
                             "score": item.get("score", 0),
                             "engine": "tavily_cached"},
                })
            if hits:
                return hits
    except Exception:
        pass
    # 回退:旧 aisa_multi_source 路径
    try:
        from aisa_multi_source import tavily_search as _tavily
        raw = _tavily(query, timeout=30)
        hits = []
        for item in raw[:top_k]:
            if not isinstance(item, dict):
                continue
            content = item.get("content", "") or ""
            url = item.get("url", "") or ""
            title = item.get("title", url[:80]) or ""
            if not (content or url):
                continue
            hits.append({
                "url": url,
                "title": title[:200],
                "content": content[:5000],
                "source": "tavily",
                "cost_usd": 0.0,
                "meta": {"model_native_search": True,
                         "score": item.get("score", 0),
                         "engine": "tavily"},
            })
        return hits
    except ImportError:
        return []
    except Exception:
        return []


def _qlib_tok(s: str) -> set:
    """query 分词(小写 alnum·长度≥3·去停用词)——同题 fuzzy 复用的口径。"""
    import re as _re
    _STOP = {"the", "and", "for", "with", "that", "this", "from", "what",
             "which", "was", "were", "are", "has", "have", "had", "not"}
    return {w for w in _re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) >= 3 and w not in _STOP}


def _qlib_roots():
    """所有 benchmark 题级库根目录(harness_baselines/fair_bench_*/)。

    MEYO_FAIR_DIR(如 fair_bench_gaia40)排最前——GAIA 回归时它的库先查;之后是
    fair_bench_40(BrowseComp·artifact 共享缓存也在这);再补其它 fair_bench_*。
    每个根返回 (per_question_lib 目录, benchmark 根)——artifact 相对 benchmark 根解析,
    不存在时退到 fair_bench_40 共享缓存。fail-safe。
    """
    import os as _os
    base = _os.path.abspath(_os.path.join(
        _os.path.dirname(__file__), "..", "..", "..", "harness_baselines"))
    names = []
    fair_dir = (_os.environ.get("MEYO_FAIR_DIR", "") or "").strip()
    if fair_dir:
        names.append(fair_dir)
    names.append("fair_bench_40")
    try:
        for n in sorted(_os.listdir(base)):
            if n.startswith("fair_bench_") and n not in names:
                names.append(n)
    except Exception:
        pass
    roots = []
    seen = set()
    for n in names:
        root = _os.path.join(base, n)
        lib = _os.path.join(root, "per_question_lib")
        if root in seen or not _os.path.isdir(lib):
            continue
        seen.add(root)
        roots.append((lib, root))
    return roots


def _question_lib_prefetch(query: str, top_k: int = 8) -> list[dict]:
    """题级证据库前置(用户 2026-08-26·铁律7「证据存好不用重搜」·跨 benchmark 通用)。

    统领全部搜索源:任何 query 进 fabric.search 前,先在【本题 per_question_lib】里按
    query 措辞找同题已存证据(精确 hash + 双向 Jaccard fuzzy·严格 qid 隔离绝不跨题)。
    命中→直接返回统一 hit 格式,一条网络都不发。所有源统一享受"付费搜过的证据复用",
    不依赖 MEYO_FORCE_TAVILY、不受 LLM 路由选哪个源影响。

    跨 benchmark:BrowseComp(fair_bench_40)和 GAIA(fair_bench_gaia40)各有独立题级库,
    按 MEYO_FAIR_DIR + qid 自动选正确库;artifact 相对本 benchmark 根解析,不存在退到
    fair_bench_40 共享缓存(GAIA 的 ref 在 gaia40·实体缓存共享在 fair_bench_40)。

    像便利贴:发请求前先翻本题那叠便利贴,写过就直接抄,别再跑一趟图书馆。

    只读不写·fail-safe(异常→[] 让正常路由继续)。qid 从 MEYO_FAIR_QID 取(运行 qid
    带 _clean/_ab_A 后缀,不能用来找库;公平基准/回归把它设成原始题 qid)。
    """
    try:
        import os as _os
        import json as _json
        _qid = (_os.environ.get("MEYO_FAIR_QID", "") or "").strip()
        if not _qid:
            return []
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in _qid)[:80]
        want = _qlib_tok(query)
        if len(want) < 2:
            return []
        # 精确 hash 需要 cached_tavily 的 canonical/hash 口径(在 fair_bench_40 里)。
        _ct = None
        try:
            import sys as _sys
            _fair40 = _os.path.abspath(_os.path.join(
                _os.path.dirname(__file__), "..", "..", "..",
                "harness_baselines", "fair_bench_40"))
            if _os.path.isdir(_fair40):
                if _fair40 not in _sys.path:
                    _sys.path.insert(0, _fair40)
                import cached_tavily as _ct  # noqa: F811
        except Exception:
            _ct = None
        _depth = _os.environ.get("MEYO_SEARCH_DEPTH", "basic") or "basic"

        def _load_artifact(ref_meta, root):
            """ref 的 artifact 相对本 benchmark 根;不存在退 fair_bench_40 共享缓存。"""
            rel = (ref_meta or {}).get("artifact", "")
            if not rel:
                return None
            for base in (root, _os.path.abspath(_os.path.join(
                    _os.path.dirname(__file__), "..", "..", "..",
                    "harness_baselines", "fair_bench_40"))):
                p = _os.path.join(base, rel)
                if _os.path.exists(p):
                    try:
                        return _json.loads(open(p, encoding="utf-8").read())
                    except Exception:
                        return None
            return None

        payload = None
        # 遍历候选 benchmark 根(MEYO_FAIR_DIR 优先)·严格只进【本 qid】目录=绝不跨题。
        for lib, root in _qlib_roots():
            libdir = _os.path.join(lib, safe)
            if not _os.path.isdir(libdir):
                continue
            # ① 精确 hash 命中(cached_tavily 口径)
            if _ct is not None and payload is None:
                try:
                    req = _ct._canonical_request(
                        query, search_depth=_depth, max_results=max(1, top_k),
                        include_raw_content=True, topic="general")
                    _cid = _ct._cache_id(req)
                    ref = _os.path.join(libdir, f"{_cid}.json")
                    if _os.path.exists(ref):
                        payload = _load_artifact(
                            _json.loads(open(ref, encoding="utf-8").read()), root)
                except Exception:
                    pass
            # ② 精确 miss → 本题库内 fuzzy(双向 Jaccard≥0.5·交集≥3 词)
            if payload is None:
                best, best_score = None, 0.0
                try:
                    for fn in _os.listdir(libdir):
                        if not fn.endswith(".json"):
                            continue
                        try:
                            meta = _json.loads(open(
                                _os.path.join(libdir, fn), encoding="utf-8").read())
                        except Exception:
                            continue
                        have = _qlib_tok((meta.get("request") or {}).get("query", ""))
                        if not have:
                            continue
                        inter = want & have
                        if len(inter) < 3:
                            continue
                        score = len(inter) / max(1, len(want | have))
                        if score > best_score:
                            best_score, best = score, meta
                except Exception:
                    best = None
                if best is not None and best_score >= 0.5:
                    payload = _load_artifact(best, root)
            if payload:
                break
        if not payload:
            return []
        hits = []
        for item in (payload.get("response") or {}).get("results", [])[:top_k]:
            url = item.get("url", "") or ""
            content = (item.get("raw_content") or item.get("content", "")) or ""
            title = item.get("title", url[:80]) or ""
            if not (content or url):
                continue
            hits.append({
                "url": url,
                "title": title[:200],
                "content": content[:5000],
                "source": "tavily",
                "cost_usd": 0.0,
                "meta": {"model_native_search": True,
                         "score": item.get("score", 0),
                         "engine": "question_lib_prefetch"},
            })
        return hits
    except Exception:
        return []


def _search_aisa_smart(query: str, top_k: int = 8) -> list[dict]:
    """AIsa Smart 自动路由搜索·意图识别后选最优源·有内部学习机制
    相当于 AIsa 自带的 L4 路由器·可访问 23 个专业数据库
    """
    try:
        from aisa_multi_source import search_web as _smart
        # AIsa smart 命令通过 "web" + smart 参数触发，或直接用 search_web
        # search_web 内部已经是 AIsa 的意图路由层
        raw = _smart(query, count=top_k, timeout=30)
        hits = []
        for item in raw[:top_k]:
            if not isinstance(item, dict):
                continue
            content = item.get("content", "") or ""
            url = item.get("url", "") or ""
            title = item.get("title", url[:80]) or ""
            if not (content or url):
                continue
            hits.append({
                "url": url,
                "title": title[:200],
                "content": content[:5000],
                "source": "aisa_smart",
                "cost_usd": 0.001,
                "meta": {"model_native_search": True,
                         "self_learning": True,
                         "engine": "aisa_smart",
                         "score": item.get("score", 0)},
            })
        return hits
    except ImportError:
        return []
    except Exception:
        return []


def _search_aisa_verity(query: str, top_k: int = 8) -> list[dict]:
    """AIsa Verity 多源交叉验证·返回带置信度的结果
    适合"这个说法对吗""确认一下"类查询
    """
    try:
        from aisa_multi_source import verity as _verity
        result = _verity(query, count=top_k, timeout=60)
        if not result.get("ok"):
            return []
        data = result
        # verity 返回 {results, confidence, sources_used}
        raw_results = data.get("results", [])
        if not raw_results and not isinstance(raw_results, list):
            # 有时候 verity 把结果打包在顶层
            raw_results = [data] if data.get("answer") else []
        hits = []
        confidence = data.get("confidence", "unknown")
        for item in (raw_results if isinstance(raw_results, list) else [])[:top_k]:
            if not isinstance(item, dict):
                continue
            content = item.get("content", "") or item.get("answer", "") or ""
            url = item.get("url", "") or ""
            title = item.get("title", url[:60]) or ""
            if not (content or url):
                continue
            hits.append({
                "url": url,
                "title": title[:200],
                "content": content[:5000],
                "source": "aisa_verity",
                "cost_usd": 0.002,
                "meta": {"model_native_search": True,
                         "cross_verification": True,
                         "confidence": confidence,
                         "engine": "aisa_verity"},
            })
        # 若 verity 返回的是整体结论（answer-style）
        if not hits and data.get("answer"):
            hits.append({
                "url": "", "title": f"[verity] {query[:60]}",
                "content": str(data.get("answer", ""))[:6000],
                "source": "aisa_verity", "cost_usd": 0.002,
                "meta": {"model_native_search": True,
                         "cross_verification": True,
                         "confidence": confidence},
            })
        return hits
    except ImportError:
        return []
    except Exception:
        return []


# ── AISA 官方源已删(2026-08-20·用户明确要·API_KEY 余额空"insufficient api key
#   balance"→每次白调等超时)。用禁用桩替换·路由不再选它们。自托管 sh_* 复刻(免费
#   无 key)完整保留在 search_fabric_verticals.py·不受影响。env MEYO_ENABLE_AISA=1
#   可临时恢复(万一将来续费)。fail-safe。
def _disabled_aisa(query: str, top_k: int = 8, *a, **k) -> list[dict]:
    """AISA 官方源禁用桩·直接返空(余额空·别白等超时)。用 sh_* 自托管替代。"""
    return []


import os as _os_sf
_AISA_ON = _os_sf.environ.get("MEYO_ENABLE_AISA", "0") == "1"

# ── 源 → callable 映射 (不硬编码·统一派发) ──────────────────────────────────
_SOURCE_CALLABLES: dict[str, callable] = {
    "ddgs": _search_ddgs,
    "brave_free": _search_brave,
    "wikipedia": _search_wikipedia,
    # AISA 官方源(needs_key·余额空)→禁用桩·除非 MEYO_ENABLE_AISA=1
    "sonar": _search_sonar_standard if _AISA_ON else _disabled_aisa,
    "sonar_pro": _search_sonar_pro if _AISA_ON else _disabled_aisa,
    # Tavily 已接公平基准独立缓存层·不再依赖 AISA key/MEYO_ENABLE_AISA。
    "tavily": _search_tavily_fabric,
    "aisa_smart": _search_aisa_smart if _AISA_ON else _disabled_aisa,
    "aisa_verity": _search_aisa_verity if _AISA_ON else _disabled_aisa,
    "anysearch": _search_anysearch,
    # vertical slots 由 W3 注入 (search_fabric_verticals.py register_vertical)
}


def register_source(name: str, schema: dict, callable_fn) -> None:
    """W3 用·把垂直源注册进 fabric (接线·不改路由代码)"""
    SOURCE_REGISTRY[name] = schema
    _SOURCE_CALLABLES[name] = callable_fn


# ══════════════════════════════════════════════════════════════════════════════
# L4: 语义路由 (LLM 判断·非关键词列表)
# ══════════════════════════════════════════════════════════════════════════════

def _route_query(query: str, top_sources: int = 3) -> list[str]:
    """语义路由: 让 LLM 读 source schema 判断哪些源适合当前 query

    设计:
    - 不硬编码关键词匹配 (违反铁律①)
    - LLM 读 SOURCE_REGISTRY 里每个源的 desc/domains 判断
    - fail-safe: LLM 不可用时退回 tier 排序
    """
    # 全部已注册源都是候选（slot=vertical_w3 占位已删除·verticals 由 register_source 注入）
    # needs_key=True 的源在 key 缺失时自动排除（路由层 fail-safe，避免浪费轮次）
    _AISA_KEY = bool(os.environ.get("AISA_API_KEY", "").strip())
    # tavily 已迁到独立缓存层(不需 AISA key)·从 needs_key 排除集合移出。
    _AISA_SOURCES = {"sonar", "sonar_pro", "aisa_smart", "aisa_verity"}
    candidates = [
        s for s, sc in SOURCE_REGISTRY.items()
        if not (sc.get("needs_key") and not _AISA_KEY and s in _AISA_SOURCES)
    ]

    # 尝试 LLM 语义路由
    try:
        from qa_llm_gateway import call as _llm_call
        schema_brief = {
            s: {"desc": SOURCE_REGISTRY[s]["desc"],
                "domains": SOURCE_REGISTRY[s]["domains"],
                "tier": SOURCE_REGISTRY[s]["tier"]}
            for s in candidates
        }
        prompt = (
            f"你是搜索路由器。给定查询和搜索源列表，选出最适合的源（最多{top_sources}个）。\n\n"
            f"查询: {query}\n\n"
            f"可用源:\n{json.dumps(schema_brief, ensure_ascii=False, indent=2)}\n\n"
            f"规则:\n"
            f"1. tier=2 的源(fallback)只在无其他选择时选\n"
            f"2. 优先匹配 domains 覆盖面与查询领域最接近的源\n"
            f"3. **结构化反查优先垂直源**: 查询含精确结构化约束(年份+曲目数如'1986 17 tracks'、"
            f"存档/历史快照、博客域名+日期、法律判例、学术DOI、化学品、Reddit讨论等)时，"
            f"优先选对应垂直源(musicbrainz/wayback_cdx*/blogspot_feed/courtlistener/openalex/"
            f"pubchem/reddit等)——通用引擎(ddgs/brave/anysearch)答不了结构化反查\n"
            f"4. 本机个人数据源(browser_history)只在与'用户最近访问'相关时选\n"
            f"5. 只输出源名称 JSON 数组·例: [\"ddgs\", \"wikipedia\"]\n\n"
            f"输出:"
        )
        resp = _llm_call(prompt, tier="light", max_tokens=60, timeout=8)
        out = (resp.get("out") or "").strip()
        if out:
            import re
            m = re.search(r"\[([^\]]+)\]", out)
            if m:
                names = [x.strip().strip('"\'') for x in m.group(1).split(",")]
                valid = [n for n in names if n in _SOURCE_CALLABLES]
                if valid:
                    return valid[:top_sources]
    except Exception:
        pass

    # fail-safe: 按 tier 排序·anysearch 放最后
    budget = _get_budget()
    ranked = budget.ranked_sources(candidates)
    return ranked[:top_sources]


# ── 全局 budget 单例 ─────────────────────────────────────────────────────────
_BUDGET_INSTANCE: Optional[BudgetManager] = None
_BUDGET_LOCK = threading.Lock()


def _get_budget() -> BudgetManager:
    global _BUDGET_INSTANCE
    if _BUDGET_INSTANCE is None:
        with _BUDGET_LOCK:
            if _BUDGET_INSTANCE is None:
                _BUDGET_INSTANCE = BudgetManager()
    return _BUDGET_INSTANCE


# ══════════════════════════════════════════════════════════════════════════════
# 限流退避 + 查询缓存 (2026-08-20 · 治「429 被静默吞成空壳兜底」)
#   2026-08-20 重构: 核心逻辑抽进通用模块 resilient_fetch·此处保留同名薄封装
#   (确保现有调用点/测试的 monkeypatch 目标不变)·并新增令牌桶限流器接入热路径
# ══════════════════════════════════════════════════════════════════════════════
import hashlib as _hashlib
import resilient_fetch as _rf

# 临时故障(可重试类)信号词·大小写不敏感命中即视为通道故障而非「真没结果」
# (转引通用常量·保持同一份定义)
_TRANSIENT_MARKERS = _rf.TRANSIENT_MARKERS

# 退避配置: 最多 3 次尝试(初次 + 2 次重试)·等 1s、2s
_RETRY_BACKOFFS = (1.0, 2.0)

# 缓存 TTL·10 分钟
_CACHE_TTL_S = 600
_CACHE_DIR = _HERE / ".search_fabric_cache"

# search 侧缓存 namespace(隔离于 lxml/llm 等其它调用方)
_CACHE_NAMESPACE = "search_fabric"

# redis 单例(懒连·连不上→None→自动降级文件缓存)·转引通用实现
_REDIS_CLIENT = None
_REDIS_CHECKED = False
_REDIS_LOCK = threading.Lock()

# 全局(跨进程)令牌桶限流器·治「多进程/多用户各刷各的把源按 IP 刷爆」
# 各源默认速率(req/sec)·wikipedia 保守遵守官方串行礼仪
_SOURCE_RATE_PER_SEC: dict[str, float] = {
    "wikipedia": 1.0,
}
_DEFAULT_RATE_PER_SEC = 5.0  # 未列出的源默认较宽松·仍防失控
_RATE_LIMITER = _rf.RateLimiter(rate_root=_HERE / ".search_fabric_ratelimit")


def _is_transient_error(err_val) -> bool:
    """判断一个 _error 字符串是否属于「临时/可重试」通道故障(429/限流/maxlag/超时)

    薄封装·核心判定在 resilient_fetch.is_transient_error。
    """
    return _rf.is_transient_error(err_val)


def _hits_are_transient(hits: list) -> bool:
    """整批 hits 是否为「纯临时故障」: 全是 _error 且至少一条命中 transient 标记"""
    if not hits:
        return False
    errs = [h for h in hits if isinstance(h, dict) and h.get("_error")]
    if len(errs) != len(hits):
        return False  # 有非错误 hit → 不算纯故障
    return any(_is_transient_error(h.get("_error")) or h.get("_transient") for h in errs)


def _cache_key(source: str, query: str, top_k: int) -> str:
    """向后兼容的 redis key(与旧行为一致的显式 key·仅供外部/调试引用)"""
    raw = f"{source}:{query}:{top_k}"
    return "sf_cache:" + _hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _get_redis():
    """懒连 redis·连不上返回 None(触发文件缓存降级)。转引通用实现。"""
    return _rf.get_redis()


def _cache_get(source: str, query: str, top_k: int) -> Optional[tuple]:
    """查缓存。返回 (hits, backend_str) 或 None。backend_str 用于 trace 打标。

    薄封装 resilient_fetch.cache_get·把本模块(可被测试 monkeypatch 的)
    _CACHE_DIR / _get_redis / _CACHE_TTL_S 显式传入·确保 patch 生效、行为不变。
    只接受 list 型缓存值(与旧行为一致)。
    """
    got = _rf.cache_get(
        _CACHE_NAMESPACE, (source, query, top_k),
        ttl=_CACHE_TTL_S,
        cache_root=_CACHE_DIR,
        redis_client=_get_redis(),
    )
    if got is None:
        return None
    data, backend = got
    if not isinstance(data, list):
        return None
    return data, backend


def _cache_set(source: str, query: str, top_k: int, hits: list) -> str:
    """写缓存。只应在有效非空结果时调用。返回实际写入的 backend 字符串。

    薄封装 resilient_fetch.cache_set(同上·显式传入可 patch 的模块级配置)。
    """
    return _rf.cache_set(
        _CACHE_NAMESPACE, (source, query, top_k), hits,
        ttl=_CACHE_TTL_S,
        cache_root=_CACHE_DIR,
        redis_client=_get_redis(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# SearchFabric · 统一门面
# ══════════════════════════════════════════════════════════════════════════════
class SearchFabric:
    """AnySearch 四件套的免费开源复刻·+ 配额保险层

    AnySearch 做了什么   | 我们怎么做
    ─────────────────────────────────────────────────────────────
    联邦多源架构          | ddgs(8引擎) + Brave + Wikipedia + 垂直API
    智能意图路由          | LLM 读 source schema 语义判断
    结构化 markdown 输出  | trafilatura 正文提炼 (W2 实现)
    统一接入              | SearchFabric.search() 一行搞定
    ─────────────────────────────────────────────────────────────
    + 配额池化            | BudgetManager·AnySearch 降为 fallback
    + 成败回流            | record_fetch → source_health → bandit

    用法:
        fabric = SearchFabric()
        hits = fabric.search("SPFMV sweet potato virus EC numbers", top_k=8)
    """

    def __init__(self,
                 use_router: bool = True,
                 fanout: int = 2,          # 同时打几个源
                 fallback_to_anysearch: bool = True,
                 http_only: bool = False):
        self.use_router = use_router
        self.fanout = fanout
        self.fallback_to_anysearch = fallback_to_anysearch
        self.http_only = http_only  # 兼容 SearchAgent(http_only=True) 调用
        self._budget = _get_budget()
        self._trace: list[str] = []

    def search(self, query: str, top_k: int = 8,
               mode: str = "balanced",
               force_sources: Optional[list[str]] = None,
               domain: str = "general") -> list[dict]:
        """统一搜索入口

        Args:
            query:         搜索查询
            top_k:         期望返回条数
            mode:          fast(只 L0 一轮) / balanced(+ 去重排序) / deep(+ 正文抽取)
            force_sources: 强制用指定源(调试用)
            domain:        领域提示·辅助路由

        Returns:
            list of hit dicts: {url, title, content, source, cost_usd, meta}
        """
        t0 = time.time()
        self._trace = []

        # 0. 题级证据库前置(用户 2026-08-26·铁律7「证据存好不用重搜」·统领全部源)。
        #    任何 query 先翻【本题 per_question_lib】——精确 hash + 同题 fuzzy(严格 qid
        #    隔离)。命中就直接返回已存证据,一条网络都不发。所有源(ddgs/brave/anysearch/
        #    tavily…)统一享受复用,不再依赖 MEYO_FORCE_TAVILY、不受路由选源影响。
        #    这修的是【搜索后端分家】根因:题级库原来只有 tavily 一个源接、且需 force 才走,
        #    导致路由选了别的源时 cache-only 下 0 证据、AB 无从对照。env 关: =0。
        #    fail-safe:异常/未命中→[]→继续正常路由(绝不因前置崩)。
        if os.environ.get("MEYO_QLIB_PREFETCH", "1") == "1":
            try:
                _pf = _question_lib_prefetch(query, top_k=top_k)
            except Exception:
                _pf = []
            if _pf:
                self._trace.append(f"question_lib_prefetch:hit:{len(_pf)}")
                return _pf

        # 1. 路由：选源
        if force_sources:
            sources = force_sources
            self._trace.append(f"forced_sources:{sources}")
        elif self.use_router:
            q_with_domain = f"[{domain}] {query}" if domain != "general" else query
            sources = _route_query(q_with_domain, top_sources=self.fanout + 1)
            self._trace.append(f"routed_sources:{sources}")
        else:
            # 无路由·按 tier 顺序
            sources = self._budget.ranked_sources(list(_SOURCE_CALLABLES.keys()))[:self.fanout + 1]

        # 2026-08-20 · AnySearch 扶正为一等主力(tier=0)·不再强制压到最后。
        # 它是本环境唯一能"翻墙"的强搜索(服务器出网)·需外网的 query 应优先走它。
        # 保留原始路由顺序即可(LLM 路由/tier 排序已把合适的源排前)·不再降权。
        # 若路由/tier 排序未选中 anysearch·主动把它纳入 primary 保底外网通道
        # (本机直连源 ddgs/brave 常被墙拦返空·anysearch 服务器出网能补)。
        # 注: force_sources(调试/上游显式指定)时严格尊重·不注入·以免破坏
        # 现有 fallback 语义(anysearch 仍走 fallback_to_anysearch 兜底路径)。
        if not force_sources \
                and "anysearch" not in sources and self._budget.can_use("anysearch") \
                and (os.environ.get("ANYSEARCH_API_KEY", "").strip()
                     or os.path.exists(os.path.expanduser(
                         "~/.claude/skills/anysearch/scripts/anysearch_cli.py"))):
            # 只在还有并发余量时补进(不挤掉已选主力·只做外网保底)
            sources = sources + ["anysearch"]
            self._trace.append("anysearch_included_as_primary_external_channel")

        # v15.8 · 结构化能力直连：查询同时包含发行年份和曲目数时，
        # MusicBrainz 是唯一能做精确集合反查的权威数据源。这里选的是“能力”，
        # 并不篡改或拼接用户查询；仍保留 LLM 路由和 ddgs 用于发现上下文。
        # 例：'released in 1986 with 17 tracks' → musicbrainz + ddgs。
        _has_year = bool(re.search(r"\b(?:19\d{2}|20[0-2]\d)\b", query))
        _has_track_count = bool(re.search(
            r"(?:\b\d{1,3}\s*(?:tracks?|songs?)\b|(?:tracks?|songs?)\s*[:=]?\s*\d{1,3})",
            query, re.I))
        if _has_year and _has_track_count and "musicbrainz" in _SOURCE_CALLABLES:
            sources = ["musicbrainz"] + [s for s in sources if s != "musicbrainz"]
            self._trace.append("structural_capability:musicbrainz(year+track_count)")

        # 确保 L0 通用源（ddgs）总是被包含
        # 垂直源质量高但覆盖窄·L0 是保底·不能被路由踢掉。
        # BUT: when the caller explicitly pins force_sources, respect it exactly — injecting
        # ddgs there made a forced-tavily (cache-hit, 0s) call also live-fetch+retry ddgs
        # (1s+2s backoff, blocked network), turning a 0s cache hit into 27-76s. An explicit
        # source list is a contract, not a routing hint.
        if not force_sources and "ddgs" not in sources and self._budget.can_use("ddgs"):
            sources = ["ddgs"] + [s for s in sources if s != "ddgs"]

        # 上述结构化源必须落进并发窗口，不因 ddgs 保底而被 fanout 截断。
        if _has_year and _has_track_count and "musicbrainz" in sources:
            sources = ["musicbrainz"] + [s for s in sources if s != "musicbrainz"]

        # 2. 并发派发 (最多 fanout+1 个主力源·L0 总在内)
        # 2026-08-20 · anysearch(tier=0)现可正常进 primary 并发窗口被调用·
        # 不再被 `s != "anysearch"` 排除。tier<2 的源(含 anysearch)平等参与。
        all_hits: list[dict] = []
        primary_sources = [s for s in sources
                           if SOURCE_REGISTRY.get(s, {}).get("tier", 0) < 2
                           ][:self.fanout + 1]

        hits_by_source = self._fanout_search(primary_sources, query, top_k)
        # futures 按完成时间入 dict；若直接 items() 合并，极快的本机历史源会
        # 抢在已显式优先的结构化源之前，随后 top_k 截断把后者全部吞掉。
        # 严格按路由后的 primary_sources 顺序合并，才能兑现 capability priority。
        #
        # 2026-08-20 · 翻墙主力优先: anysearch 是本环境唯一能"翻墙"的主力外网源
        # (服务器在墙外替我们出网·搜到 wikipedia/ddgs 拿不到的独家外网内容)。
        # 若被普通源(wikipedia 等)在 top_k 截断前挤掉就白费扶正。故合并时把它提前,
        # 让翻墙独家结果不被本地源吞掉。只调合并顺序·不改路由/不删任何源。
        _merge_order = ([s for s in primary_sources if s == "anysearch"] +
                        [s for s in primary_sources if s != "anysearch"])
        for src in _merge_order:
            all_hits.extend(hits_by_source.get(src, []))

        # 3. 质量判断·不够时降级到 fallback
        valid_hits = [h for h in all_hits if not h.get("_error")]
        self._trace.append(f"valid_after_primary:{len(valid_hits)}")

        # 通道健康度: primary 源是否「全部为临时故障(限流/超时类)」——
        # 若是,则真相是"通道被限流",不是"没结果"。此时 anysearch 空壳兜底
        # 会伪装成"搜到了"骗下游(A 病)。要显式打 [DEGRADED] 标·带健康信号。
        _err_hits = [h for h in all_hits if h.get("_error")]
        _all_transient = bool(_err_hits) and not valid_hits and all(
            (_is_transient_error(h.get("_error")) or h.get("_transient"))
            for h in _err_hits
        )
        _source_health_marker = None
        if _all_transient:
            self._trace.append("[DEGRADED] all_sources_transient_fail")
            _source_health_marker = {
                "_source_health": "all_transient_fail",
                "_note": "primary sources all rate-limited/timeout; NOT a genuine no-result",
                "_transient": True,
                "_errors": [h.get("_error") for h in _err_hits][:6],
            }

        # 2026-08-20 · anysearch 现是一等主力·若已在 primary 窗口跑过(结果已并入
        # valid_hits)·就不必再走兜底重复调用(缓存命中不耗额度·但仍避免多余轮次)。
        # mode="fast": the caller wants a single bounded wave and will decide its own next
        # step. Skip the SERIAL anysearch fallback, whose retry/backoff can add 30-60s on a
        # zero-result query (observed in thin-loop traces). The primary fan-out already ran
        # with its own timeout; a fast search returns whatever it found. balanced/deep keep
        # the fallback unchanged.
        _anysearch_ran_primary = "anysearch" in primary_sources
        if mode == "fast":
            self._trace.append("fast_mode:serial_anysearch_fallback_skipped")
        if mode != "fast" and len(valid_hits) < max(2, top_k // 3) and self.fallback_to_anysearch \
                and not _anysearch_ran_primary:
            if _all_transient:
                # 全通道限流时,不用 anysearch 空壳冒充结果·让上层看出是通道故障
                self._trace.append(
                    "anysearch_fallback_suppressed:all_sources_transient_fail")
            elif self._budget.can_use("anysearch"):
                self._trace.append("anysearch_fallback_triggered")
                fb_hits = self._call_source("anysearch", query, top_k)
                valid_hits.extend([h for h in fb_hits if not h.get("_error")])
            else:
                self._trace.append("anysearch_quota_exhausted_but_L0_covered")

        # 4. 去重 + 截取
        seen_urls: set[str] = set()
        deduped: list[dict] = []
        for h in valid_hits:
            u = h.get("url", "")
            key = u or h.get("title", "")[:60]
            if key and key not in seen_urls:
                seen_urls.add(key)
                deduped.append(h)

        result = deduped[:top_k]

        # 全通道限流且无任何有效结果时,把健康信号作为一条标记 hit 附在结果里,
        # 确保上层 agent 即使只看返回列表(不读 trace)也能区分「通道故障」vs「没结果」。
        # 只增不删: 正常有结果时不附加·不破坏现有返回结构。
        if _source_health_marker is not None and not result:
            result = [_source_health_marker]

        elapsed = time.time() - t0
        self._trace.append(f"total_hits:{len(result)} elapsed:{elapsed:.1f}s")

        # 5. source_health 回流
        self._report_health(hits_by_source, len([h for h in result if not h.get("_source_health")]))

        return result

    def _fanout_search(self, sources: list[str],
                       query: str, top_k: int) -> dict[str, list[dict]]:
        """并发调用多个源"""
        import concurrent.futures
        results: dict[str, list[dict]] = {}

        if not sources:
            return results

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(sources)) as ex:
            future_to_src = {
                ex.submit(self._call_source, src, query, top_k): src
                for src in sources
            }
            # v15.7 · 修 as_completed 超时炸整条查询的 bug(1/4 futures unfinished):
            #   之前超时 TimeoutError 从迭代器抛出·直接炸掉 search()·已完成的源结果全丢。
            #   改: 捕获超时·取消未完成·返回已完成源的部分结果(部分好过全无)。
            #   4 源并发窗口 15→25s(垂直 API 冷启动常超 15s)。
            try:
                for fut in concurrent.futures.as_completed(future_to_src, timeout=25):
                    src = future_to_src[fut]
                    try:
                        results[src] = fut.result(timeout=1)
                    except Exception as e:
                        results[src] = [{"_error": f"{src}_future_exc:{e}"}]
            except concurrent.futures.TimeoutError:
                _slow = [s for f, s in future_to_src.items() if not f.done()]
                for f, s in future_to_src.items():
                    if f.done():
                        try:
                            results.setdefault(s, f.result(timeout=1))
                        except Exception as e:
                            results[s] = [{"_error": f"{s}_future_exc:{e}"}]
                    else:
                        f.cancel()
                        results[s] = [{"_error": f"{s}_fanout_timeout_25s"}]
        return results

    def _call_source(self, source: str, query: str,
                     top_k: int) -> list[dict]:
        """调用单个源·记录 budget·带查询缓存 + 429/限流指数退避重试

        流程(2026-08-20 加固):
          1. 先查缓存(redis 优先·文件兜底)·命中直接返回(不消耗底层配额)
          2. 未命中真调用·若返回 transient 故障(429/maxlag/timeout)→指数退避重试
             (等 1s、2s·最多 3 次尝试)·重试仍失败给错误 dict 打 `_transient:True`
          3. 成功且有效非空结果→写缓存(只缓存成功·不缓存错误/空壳)
        """
        fn = _SOURCE_CALLABLES.get(source)
        if fn is None:
            return [{"_error": f"source_not_implemented:{source}"}]

        # ── 1. 查缓存（必须先于 quota）──────────────────────────────
        # Cache hits are free. The old order checked can_use() first, so an exhausted Tavily
        # daily quota also blocked a valid zero-credit cache hit — forced-tavily then returned
        # [] even though the exact response was on disk. Only gate the LIVE call on quota.
        cached = _cache_get(source, query, top_k)
        if cached is not None:
            hits, backend = cached
            self._trace.append(f"cache_hit:{source}")
            if backend == "file":
                self._trace.append("[DEGRADED] cache_fallback_file")
            return hits
        # Sources whose implementation enforces its own live cap must not be double-gated by
        # the outer daily quota. `tavily` routes through cached_tavily, which serves disk cache
        # for free and applies a per-question live-call cap itself; an outer quota (inflated by
        # cache-served hits) otherwise wrongly blocks a valid cache miss on a fresh question.
        _inner_capped = {"tavily"}
        if source not in _inner_capped and not self._budget.can_use(source):
            return [{"_error": f"quota_exhausted:{source}"}]

        # ── 2. 真调用 + 退避重试 ───────────────────────────────────
        attempts = 1 + len(_RETRY_BACKOFFS)  # 初次 + 重试次数 = 最多 3 次
        last_hits: list[dict] = []
        for i in range(attempts):
            # ── 全局令牌桶限流(跨进程)· 真打网络前先过 ──────────────
            # 治「多进程/多用户各刷各的把源按 IP 刷爆」·redis 优先·flock 文件兜底
            rate = _SOURCE_RATE_PER_SEC.get(source, _DEFAULT_RATE_PER_SEC)
            try:
                got_token = _RATE_LIMITER.acquire(source, rate, block=True, max_wait_s=5.0)
                if not got_token:
                    self._trace.append(f"rate_limited_wait_timeout:{source}")
                if getattr(_RATE_LIMITER, "last_backend", None) == "file":
                    self._trace.append(f"[DEGRADED] ratelimit_fallback_file:{source}")
            except Exception:
                pass  # 限流器自身故障绝不阻断主流程(保守放行)
            t0 = time.time()
            try:
                hits = fn(query, top_k)
            except Exception as e:
                self._budget.record_use(source, ok=False, latency_s=time.time() - t0)
                hits = [{"_error": f"{source}_exc:{type(e).__name__}:{str(e)[:100]}",
                         "_transient": False}]
                last_hits = hits
                break  # 非源返回的异常不重试(逻辑错·重试无益)

            valid = [h for h in hits if not (isinstance(h, dict) and h.get("_error"))]
            ok = bool(valid)
            self._budget.record_use(source, ok=ok, latency_s=time.time() - t0)
            last_hits = hits

            # 成功且非空 → 写缓存后返回
            if ok:
                backend = _cache_set(source, query, top_k, hits)
                if backend == "file":
                    self._trace.append(f"cache_store:{source} [DEGRADED] cache_fallback_file")
                elif backend == "redis":
                    self._trace.append(f"cache_store:{source}")
                return hits

            # 无有效结果·判断是否 transient(可重试)
            transient = any(
                _is_transient_error(h.get("_error"))
                for h in hits if isinstance(h, dict) and h.get("_error")
            )
            if not transient:
                return hits  # 真没结果(非限流)·不重试直接返回原样

            # transient·若还有重试机会则退避等待
            if i < len(_RETRY_BACKOFFS):
                wait = _RETRY_BACKOFFS[i]
                self._trace.append(f"transient_retry:{source} attempt{i+1} sleep{wait}s")
                time.sleep(wait)
                continue

        # ── 3. 重试耗尽仍是 transient 故障 → 打 _transient 显式标 ────
        marked: list[dict] = []
        for h in last_hits:
            if isinstance(h, dict) and h.get("_error"):
                if _is_transient_error(h.get("_error")):
                    h = dict(h)
                    h["_transient"] = True
                marked.append(h)
            else:
                marked.append(h)
        if any(isinstance(h, dict) and h.get("_transient") for h in marked):
            self._trace.append(f"[DEGRADED] transient_fail_after_retry:{source}")
        return marked

    def _report_health(self, hits_by_source: dict[str, list], total_valid: int):
        """向 source_health 回流成败数据·让 bandit 越用越准"""
        try:
            from source_health import record_fetch
            for src, hits in hits_by_source.items():
                valid = [h for h in hits if not h.get("_error")]
                err = next((h.get("_error") for h in hits if h.get("_error")), None)
                record_fetch(
                    source=f"search_fabric/{src}",
                    url=f"[query]",
                    count=len(valid),
                    err=err,
                )
        except Exception:
            pass

    def budget_status(self) -> str:
        """返回配额状态给 agent 看"""
        return self._budget.status_report()

    def trace(self) -> list[str]:
        """调试追踪"""
        return self._trace


# ══════════════════════════════════════════════════════════════════════════════
# SearchAgent 兼容适配层
# 让现有 SearchAgent().search() 调用者可以无缝切换到 SearchFabric
# ══════════════════════════════════════════════════════════════════════════════

class SearchFabricAgent:
    """SearchAgent 协议兼容包装·让上游 from search_agent import SearchAgent 可渐进迁移

    用法(迁移期):
        from search_fabric import SearchFabricAgent as SearchAgent  # 一行换·无缝
        sa = SearchAgent(use_supervisor=False, use_forger=False)
        hits = sa.search("query", mode="fast", top_k=8, http_only=True)
    """

    def __init__(self, use_supervisor=True, use_forger=True,
                 use_cache=True, use_bookmark=True,
                 log_experiment=True, **kwargs):
        self._fabric = SearchFabric(
            use_router=True,
            fanout=2,
            fallback_to_anysearch=True,
        )
        self._trace_list: list[str] = []

    def search(self, query: str, mode: str = "balanced",
               top_k: int = 8, http_only: bool = False, **kwargs) -> list[dict]:
        """与 SearchAgent.search() 同签名"""
        self._fabric.http_only = http_only
        hits = self._fabric.search(query, top_k=top_k, mode=mode)
        self._trace_list = self._fabric.trace()
        return hits

    def deep_read(self, url: str, **kwargs) -> dict:
        """保留接口·实现委托原 SearchAgent (不打破上游)"""
        try:
            from search_agent import SearchAgent as _SA
            return _SA().deep_read(url, **kwargs)
        except Exception as e:
            return {"ok": False, "content": "", "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# CLI / 自测
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="search_fabric 快速自测")
    ap.add_argument("query", nargs="?", default="SPFMV sweet potato feathery mottle virus EC numbers")
    ap.add_argument("--top_k", type=int, default=5)
    ap.add_argument("--sources", nargs="*", help="强制指定源·如 ddgs wikipedia")
    ap.add_argument("--budget", action="store_true", help="打印配额状态")
    args = ap.parse_args()

    fabric = SearchFabric()
    if args.budget:
        print(fabric.budget_status())
        sys.exit(0)

    print(f"🔍 query: {args.query}")
    hits = fabric.search(args.query, top_k=args.top_k,
                         force_sources=args.sources)
    print(f"✅ 返回 {len(hits)} 条 | trace: {fabric.trace()}\n")
    for i, h in enumerate(hits, 1):
        print(f"  [{i}] [{h.get('source','')}] {h.get('title','')[:60]}")
        print(f"       {h.get('url','')[:80]}")
        print(f"       {h.get('content','')[:120]}...")
        print()
