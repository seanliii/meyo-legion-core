'v13.87 · Friday 通用搜索统一包装 · km.sankuai.com/collabpage/2713327163\n\n用户 2026-07-22 抓：「用文档里的 API · 不是知乎的 · 通用的」\n\nFriday universal-search endpoint (美团内网):\n  http://agi.sankuai.com/tools/universal-search/api/v1\n\n支持 sources (2026-07-22 实测):\n- ✅ xhc      · 小红书 · 内部 · 无配额门 · 秒返\n- ✅ zh       · 知乎   · 内部 · 无配额门 · 秒返\n- ✅ dpNote   · 大众点评笔记 · 内部 · 无配额门\n- ⚠️ bing-search-v3    · $12.5/千 · 需申请 RPM 配额\n- ⚠️ google-search     · $5/千   · 日限 1万 · 需申请 RPM 配额\n- ⚠️ baidu-search-v2   · ¥14.4/千 · 需申请 RPM 配额\n- ⚠️ tencent-search    · ¥27.6/千 · 需申请 RPM 配额\n- ⚠️ bocha-search      · ¥3.6/千  · 需申请 RPM 配额\n- ⚠️ zhipu-search      · ¥7-35/千 · 多档 · 需申请 RPM 配额\n- ⚠️ quark-search      · ¥16.2/千 · 需申请 RPM 配额\n\n认证：Authorization: Bearer <FRIDAY_APP_ID>\n现役 token  (v13.86 xhs 用) · 内部 3 源已白名单\n外部 7 引擎需要走 Friday 控制台申请 RPM/TPM\n\n替代关系:\n- v13.86 search_xiaohongshu_api → 走 xhc source (旧实现能保留·功能一致)\n- v13.87 zhihu_official_api.search_zhihu_official → 应替换为走 zh source\n\n统一好处:\n- 一份代码调 10+ 引擎\n- 每引擎特有参数（bing sites/baidu 时间范围/tencent insite）都能透传\n- fallbackEngine 参数支持主备切换\n- 死链检测（对 quark 开）自动过滤\n'
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 韧性接入 (2026-08-20 · 治「friday RPM limit/inner:1029/429 被 except 静默吞成空」)──
#   复用公共模块 resilient_fetch: 按 source 限流 + 退避重试(补 friday 特有限流串)
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
try:
    import resilient_fetch as _rf  # 同目录公共韧性模块
except Exception:  # pragma: no cover - 拿不到就退回裸调(不破坏原行为)
    _rf = None

_FRIDAY_RATE_PER_SEC = 5
_FRIDAY_RETRY_BACKOFFS = (1.0, 2.0)
# friday 特有限流/超发信号(除通用 429/timeout 外补进重试判据·小写匹配)
_FRIDAY_TRANSIENT_EXTRA = ("inner:1029", "rpm limit", "too many requests")
if _rf is not None:
    _FRIDAY_RATE_LIMITER = _rf.RateLimiter(rate_root=_HERE / ".friday_ratelimit")
else:
    _FRIDAY_RATE_LIMITER = None

# ── 必错源路由屏蔽 (2026-08-20 · 用户明确要「路由屏蔽·安全」) ──────────────────
#   真实健康数据(见 shrimp-search-429 记忆): 这几个 friday 源长期 ~0% 成功——
#     baidu-search-v2: 全返空 (624 次 0 成功)
#     tencent/quark/bocha: RPM 配额未申请 → "inner:1029 RPM limit"/Too Many Requests 必失败
#   做法=路由屏蔽(非删函数体·不破坏任何调用方): search() 入口把它们从 sources 剔除,
#   全被剔除时短路返回[](别白打网络等超时)。需要时可 MEYO_ENABLE_DEAD_FRIDAY=1 恢复。
_DEAD_FRIDAY_SOURCES = {
    "baidu-search-v2", "baidu-search", "baidu",
    "tencent-search", "quark-search", "bocha-search",
}
_DEAD_FRIDAY_ON = _os_sf_dead = os.environ.get("MEYO_ENABLE_DEAD_FRIDAY", "0") == "1"


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


V11 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 加载 .env
try:
    from env_loader import load_env
    load_env()
except Exception:
    pass

_ENDPOINT_PROD = "http://agi.sankuai.com/tools/universal-search/api/v1"
_ENDPOINT_TEST = "https://agi.ai.test.sankuai.com/tools/universal-search/api/v1"

# v13.87.2 · 双 token · 2026-07-22 实测发现权限互补
# 内部源 token (v13.86 xhs dev 用) → xhc/zh/dpNote 白名单
_INTERNAL_APPID = ""
# 外部引擎 token (LLM 请求真在用) → bing/google/zhipu 已开 RPM
_EXTERNAL_APPID = os.environ.get("MEYO_FALLBACK_KEY","")

# 各 source 归属 (根据实测 2026-07-22)
_INTERNAL_SOURCES = {"xhc", "zh", "dpNote"}
_EXTERNAL_SOURCES = {
    "bing-search-v3", "bing", "google-search", "zhipu-search",
    # 下面几个 RPM 未开 · 但走 external 走对 (等 RPM 配额时无需改代码)
    "baidu-search-v2", "baidu-search",
    "tencent-search", "bocha-search", "quark-search",
}


def _get_appid_for_sources(sources: list) -> str:
    """v13.87.2 · 自动挑 token · 上游无感知
    - env FRIDAY_APP_ID 或 MEITUAN_UNIVERSAL_SEARCH_TOKEN 显式指定 → 用
    - 否则按 sources 归属挑
    - 多源混装 · 先看第一个 · 混外部+内部会挂 (Friday 侧限制)·上游别混
    """
    override = (os.environ.get("MEITUAN_UNIVERSAL_SEARCH_TOKEN")
                or os.environ.get("FRIDAY_APP_ID"))
    if override:
        return override
    if not sources:
        return _INTERNAL_APPID
    first = sources[0]
    if first in _EXTERNAL_SOURCES:
        return _EXTERNAL_APPID
    return _INTERNAL_APPID


def _get_appid() -> str:
    """保留兼容 · 默认内部 token"""
    return (os.environ.get("MEITUAN_UNIVERSAL_SEARCH_TOKEN")
            or os.environ.get("FRIDAY_APP_ID")
            or _INTERNAL_APPID)


def _get_endpoint() -> str:
    """默认走 prod · env MEYO_FRIDAY_ENV=test 走 test"""
    if os.environ.get("MEYO_FRIDAY_ENV", "").lower() == "test":
        return _ENDPOINT_TEST
    return _ENDPOINT_PROD


def _log_health(source_name: str, record: dict) -> None:
    """走 fetcher_health · 每个 source 独立健康度"""
    try:
        from fetcher_health import health_log
        health_log(f"friday_{source_name}", record)
    except Exception:
        pass


def _put_cache(url: str, content: str, source: str,
               meta: Optional[dict] = None) -> None:
    try:
        from search_cache import cache_put
        if url and content:
            cache_put(url, content, f"friday_{source}", meta or {})
    except Exception:
        pass


def _score_completeness(text: str) -> str:
    n = len(text or "")
    if n < 200:
        return "shell"
    if n < 1000:
        return "partial"
    return "full"


def _normalize_item(item: dict, source: str) -> dict:
    """API 返回 → 我们标准 hit 格式"""
    if not isinstance(item, dict):
        return None
    title = item.get("title", "")
    snippet = item.get("snippet", "")
    content = item.get("content", "") or snippet
    url = item.get("url", "")
    src = item.get("source", source)
    pt = item.get("publish_time", "")

    return {
        "title": (title or "")[:200],
        "url": url,
        "content": (content or "")[:5000],
        "source": f"friday_{source}",
        "completeness": _score_completeness(content),
        "meta": {
            "via": "friday_universal",
            "friday_source": src,
            "publish_time": pt,
            "content_len": len(content or ""),
            "has_snippet": bool(snippet),
        },
    }


def search(query: str, sources: list, top_k: int = 10,
           is_fast: bool = True, timeout: int = 10,
           fallback_engine: Optional[str] = None,
           source_params: Optional[dict] = None) -> list:
    """通用搜索 · 一份代码打所有 Friday sources

    Args:
      query: 搜索词
      sources: list · 如 ["zh"] / ["bing-search-v3"] / ["zh", "xhc"]
      top_k: 返回条数 (综合相似度 topK)
      is_fast: True 只拿 API 简略 · False 会爬正文 (慢·耗资源)
      timeout: 秒 · 传给 API
      fallback_engine: 主 source 全挂时降级引擎
      source_params: 各 source 专用参数
        - bingSearchParam: {startDate, endDate, sites}
        - baiduSearchV2Param: {searchRecencyFilter, blockWebsites, searchFilter}
        - tencentSearchParam: {insite, fromTime, toTime, mode}
        - zhipuSearchParam: {insite, searchEngine, searchRecencyFilter, contentSize}
        - dpSearchParam: {dpId, myCityId, longitude, latitude, sort, userId}
        - bochaSearchParam: {freshness, summary, include, exclude}
        - quarkSearchParam: {timeRange, category, engineType, location, contents}
        - xhcSearchParam / zhSearchParam: {startDate, endDate}
        - contentFillParam (is_fast=False 时): {maxRetryTimes, cache}

    返回: [hit, ...] · fail-safe 挂了返 []
    """
    if not query or not sources:
        return []

    # 必错源路由屏蔽: 剔除长期 ~0% 成功的死源(baidu/tencent/quark/bocha)·
    # 全被剔除→短路返回[](不白打网络)。MEYO_ENABLE_DEAD_FRIDAY=1 可恢复。
    if not _DEAD_FRIDAY_ON:
        _kept = [s for s in sources if s not in _DEAD_FRIDAY_SOURCES]
        if len(_kept) != len(sources):
            _dropped = [s for s in sources if s in _DEAD_FRIDAY_SOURCES]
            if not _kept:
                _log_health("_".join(sources)[:40], {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "unix_ts": int(time.time()),
                    "query": query[:120], "sources": sources, "top_k": top_k,
                    "is_fast": is_fast, "ok": False, "count": 0,
                    "elapsed_s": 0.0,
                    "err": f"[ROUTE_BLOCKED] all dead sources dropped: {_dropped}",
                })
                return []
            sources = _kept

    source_key = "_".join(sources)[:40]  # health log 用 · 多源拼名
    t0 = time.time()
    health = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "unix_ts": int(time.time()),
        "query": query[:120], "sources": sources, "top_k": top_k,
        "is_fast": is_fast,
        "ok": False, "count": 0, "elapsed_s": 0.0, "err": "",
    }

    payload = {
        "query": query,
        "sources": sources,
        "topK": top_k,
        "isFast": is_fast,
        "timeout": timeout * 1000,   # API 用毫秒
    }
    if fallback_engine:
        payload["fallbackEngine"] = fallback_engine
    if source_params:
        payload.update(source_params)

    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # v13.87.2 · 自动挑 token · 双 token 权限互补
    appid = _get_appid_for_sources(sources)
    health["appid_last6"] = appid[-6:] if appid else ""

    # ── 韧性接入 (2026-08-20) ──────────────────────────────────────────────
    # 请求前按 source 限流·请求包退避重试(inner:1029/RPM limit/Too Many Requests/timeout
    # 视为 transient·补进重试判据)·empty(真没结果)不重试。核心 curl+解析逻辑不变。
    if _FRIDAY_RATE_LIMITER is not None:
        _FRIDAY_RATE_LIMITER.acquire(f"friday:{source_key}",
                                     rate_per_sec=_FRIDAY_RATE_PER_SEC, block=True)

    def _attempt():
        """单次请求 + 分类·返回带标记的 dict(供 with_retry 判断可否重试)。
        {"__err__": marker}      → 通道故障(curl/异常/outer/inner)·可能 transient
        {"__data__": data}       → 拿到可用响应(交给下游做 empty/标准化·不重试)
        """
        try:
            r = subprocess.run(
                ["curl", "-sS", "--max-time", str(timeout + 5),
                 "--location", _get_endpoint(),
                 "--header", f"Authorization: Bearer {appid}",
                 "--header", "Content-Type: application/json",
                 "--data-binary", "@-"],
                input=payload_bytes, capture_output=True,
                timeout=timeout + 8,
            )
            if r.returncode != 0 or not r.stdout:
                return {"__err__": f"curl_fail:rc={r.returncode}"}
            data = json.loads(r.stdout.decode("utf-8", errors="replace"))
        except Exception as e:
            return {"__err__": f"exc:{type(e).__name__}:{str(e)[:120]}"}

        # 外层 status
        if data.get("status", -1) != 0:
            return {"__err__": f"outer:{data.get('status')}·{str(data.get('message',''))[:60]}"}

        inner_local = data.get("data") or {}
        inner_code_local = inner_local.get("code", 0)
        if inner_code_local != 0:
            err_map_l = inner_local.get("internal_err_map") or {}
            err_txt = "|".join(f"{k}:{v}" for k, v in err_map_l.items())[:200]
            return {"__err__": f"inner:{inner_code_local}·{err_txt}"}
        return {"__data__": data}

    def _friday_is_transient(res):
        """curl/outer/inner 错误里命中 429/超时/inner:1029/RPM limit/Too Many Requests
        → 可重试。empty(真没结果)由下游处理·不进这里·不重试。"""
        if not isinstance(res, dict):
            return False
        marker = res.get("__err__")
        if not marker:
            return False  # __data__(拿到响应)不重试
        low = str(marker).lower()
        if _rf is not None and _rf.is_transient_error(marker):
            return True
        return any(m in low for m in _FRIDAY_TRANSIENT_EXTRA)

    if _rf is not None:
        res = _rf.with_retry(_attempt, is_transient_fn=_friday_is_transient,
                             max_retries=2, backoffs=_FRIDAY_RETRY_BACKOFFS)
    else:
        res = _attempt()

    health["elapsed_s"] = round(time.time() - t0, 3)

    # 通道故障(重试耗尽仍失败)·保留原有 health.err 语义 + fail-safe 返 []
    if not isinstance(res, dict) or "__data__" not in res:
        marker = res.get("__err__", "unknown") if isinstance(res, dict) else "unknown"
        # 还原原有 err 前缀语义(curl_fail/exc/outer/inner 原样保留在 marker 里)
        health["err"] = marker
        _log_health(source_key, health)
        return []

    data = res["__data__"]
    inner = data.get("data") or {}
    inner_code = inner.get("code", 0)

    # 有些引擎 code=0 但 results 空 · 附 internal_err_map (如 RPM limit)
    err_map = inner.get("internal_err_map") or {}
    raw_results = inner.get("results") or []

    if not raw_results:
        if err_map:
            first_err = list(err_map.values())[0][:120]
            health["err"] = f"empty_with_err:{first_err}"
        else:
            health["err"] = "empty"
        _log_health(source_key, health)
        return []

    # 标准化
    results = []
    total_len = 0
    comp_dist = {}
    for it in raw_results[:top_k]:
        h = _normalize_item(it, source_key)
        if not h:
            continue
        results.append(h)
        total_len += len(h["content"])
        c = h["completeness"]
        comp_dist[c] = comp_dist.get(c, 0) + 1
        _put_cache(h["url"], h["content"], source_key, {
            "title": h.get("title", ""),
            "friday_source": h["meta"].get("friday_source", ""),
        })

    health["ok"] = len(results) > 0
    health["count"] = len(results)
    health["avg_content_len"] = (int(total_len / len(results))
                                  if results else 0)
    health["completeness_dist"] = comp_dist
    _log_health(source_key, health)
    return results


# ═════ 便捷入口 · 常用 source 直接暴露 ═════

def search_zh(query: str, top_k: int = 10, days: int = 0) -> list:
    """知乎 · 内部源 · 秒返"""
    params = {}
    if days > 0:
        import datetime
        end = datetime.date.today()
        start = end - datetime.timedelta(days=days)
        params["zhSearchParam"] = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }
    return search(query, ["zh"], top_k=top_k, source_params=params or None)


def search_xhc(query: str, top_k: int = 10, days: int = 0) -> list:
    """小红书 · 内部源 · 秒返"""
    params = {}
    if days > 0:
        import datetime
        end = datetime.date.today()
        start = end - datetime.timedelta(days=days)
        params["xhcSearchParam"] = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }
    return search(query, ["xhc"], top_k=top_k, source_params=params or None)


def search_dp(query: str, dp_id: str, city_id: str = "",
              top_k: int = 10) -> list:
    """大众点评笔记 · 内部源"""
    params = {"dpSearchParam": {"dpId": dp_id}}
    if city_id:
        params["dpSearchParam"]["intentCityIdField"] = city_id
    return search(query, ["dpNote"], top_k=top_k, source_params=params)


def search_bing(query: str, top_k: int = 10, sites: Optional[list] = None,
                start_date: str = "", end_date: str = "") -> list:
    """Bing · 外部 · 需 RPM 配额"""
    params = {}
    bp = {}
    if sites:
        bp["sites"] = sites
    if start_date:
        bp["startDate"] = start_date
    if end_date:
        bp["endDate"] = end_date
    if bp:
        params["bingSearchParam"] = bp
    return search(query, ["bing-search-v3"], top_k=top_k,
                   source_params=params or None)


def search_google(query: str, top_k: int = 10) -> list:
    """Google · 外部 · $5/千次 · 需 RPM 配额 · 日限 1 万
    默认 isFast=True · 只 snippet 150 字
    深度用 search_google_deep
    """
    return search(query, ["google-search"], top_k=top_k)


def search_google_deep(query: str, top_k: int = 5,
                        timeout: int = 60) -> list:
    """v13.87.21 · Google 深挖 · isFast=False 让 Friday 服务端真爬每个 URL 正文
    用户 2026-07-23:「Google 深挖到底 · 都有啥方式」
    - isFast=false · 服务端解析每个 URL · content 从 150 字 → 1000-3000 字
    - 耗时增加 · 10-30s · 但拿到全文
    - RPM 消耗 * top_k 不变 · 每 URL 爬正文额外算
    - top_k 建议 3-5 (每条爬正文都要时间)
    """
    return search(query, ["google-search"], top_k=top_k,
                   is_fast=False, timeout=timeout,
                   source_params={"contentFillParam": {
                       "maxRetryTimes": 1, "cache": True}})


def search_bing_deep(query: str, top_k: int = 5,
                      timeout: int = 60) -> list:
    """v13.87.21 · Bing 深挖 · isFast=False 真爬每 URL 正文"""
    return search(query, ["bing-search-v3"], top_k=top_k,
                   is_fast=False, timeout=timeout,
                   source_params={"contentFillParam": {
                       "maxRetryTimes": 1, "cache": True}})


def search_baidu(query: str, top_k: int = 10,
                  recency: str = "",
                  block_sites: Optional[list] = None,
                  only_sites: Optional[list] = None) -> list:
    """百度基础搜索 · 外部
    recency: week/month/semiyear/year
    """
    params = {}
    bp = {}
    if recency:
        bp["searchRecencyFilter"] = recency
    if block_sites:
        bp["blockWebsites"] = block_sites
    if only_sites:
        bp["searchFilter"] = {"match": {"site": only_sites}}
    if bp:
        params["baiduSearchV2Param"] = bp
    return search(query, ["baidu-search-v2"], top_k=top_k,
                   source_params=params or None)


def search_hn(query: str, top_k: int = 5, by_date: bool = False) -> list:
    """v13.88.58 · 2026-07-28 · hackernews 直连 Algolia API
    Friday 里 hackernews source 不接入 · 返 400 invalid sources
    直调 https://hn.algolia.com/api/v1 · 免费无 quota
    by_date=True → 按时间排 (search_by_date endpoint)"""
    import urllib.parse, urllib.request
    t0 = time.time()
    endpoint = "search_by_date" if by_date else "search"
    url = (f"https://hn.algolia.com/api/v1/{endpoint}"
           f"?query={urllib.parse.quote(query)}&hitsPerPage={top_k}")
    health = {
        "ts": datetime.now().isoformat(timespec="seconds"),  # v13.88.58 · 统一本地时间
        "unix_ts": int(time.time()),
        "query": query, "sources": ["hackernews"], "top_k": top_k,
        "is_fast": True, "fetcher": "algolia_direct",
    }
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "meyo-hn/1"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        health["ok"] = False
        health["err"] = f"algolia_fail:{type(e).__name__}:{str(e)[:80]}"
        health["elapsed_s"] = round(time.time() - t0, 3)
        _log_health("hackernews", health)
        return []

    hits = data.get("hits") or []
    results = []
    total_len = 0
    for it in hits[:top_k]:
        title = (it.get("title") or it.get("story_title") or "").strip()
        u = it.get("url") or (
            f"https://news.ycombinator.com/item?id={it.get('objectID')}"
            if it.get("objectID") else "")
        content = ((it.get("story_text") or it.get("comment_text") or title or "")
                   .strip()[:5000])
        if not (title and u):
            continue
        r = {
            "title": title, "url": u, "content": content,
            "content_hash": hashlib.sha1(content.encode()).hexdigest()[:16] if content else "",
            "source": "hackernews",
            "completeness": _score_completeness(content),
            "score": it.get("points") or 0,
            "author": it.get("author") or "",
            "created_at": it.get("created_at") or "",
        }
        results.append(r)
        total_len += len(content)
        _put_cache(u, content, "hackernews",
                   {"title": title, "author": r["author"]})

    health["ok"] = True
    health["count"] = len(results)
    health["elapsed_s"] = round(time.time() - t0, 3)
    health["avg_content_len"] = int(total_len / max(1, len(results)))
    health["err"] = ""
    _log_health("hackernews", health)
    return results


def search_multi(query: str, sources: list, top_k: int = 5,
                  fallback: Optional[str] = None) -> list:
    """多引擎并搜 · 一个请求打多个 sources · Friday 侧 rerank 合并
    v13.88.58 · hackernews 从 sources 里剥出来直调 Algolia · 剩余交给 Friday"""
    hn_hits = []
    real_sources = [s for s in sources if s not in ("hackernews", "hackernews_github")]
    if any(s in sources for s in ("hackernews", "hackernews_github")):
        hn_hits = search_hn(query, top_k=top_k)
    other_hits = []
    if real_sources:
        other_hits = search(query, real_sources, top_k=top_k,
                             fallback_engine=fallback)
    return (hn_hits + other_hits)[:top_k * 2]


# ═════ CLI ═════

if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("action", choices=["probe", "zh", "xhc", "bing",
                                            "google", "baidu", "multi", "raw"])
        p.add_argument("query", nargs="?", default="RAG")
        p.add_argument("--top", type=int, default=3)
        p.add_argument("--sources", default="")
        p.add_argument("--days", type=int, default=0)
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.action == "probe":
        # 探测各 source 可用性
        probes = ["xhc", "zh", "dpNote", "bing-search-v3",
                  "google-search", "baidu-search-v2", "tencent-search",
                  "bocha-search", "quark-search"]
        for s in probes:
            params = None
            if s == "dpNote":
                params = {"dpSearchParam": {"dpId": "demo"}}
            r = search(args.query, [s], top_k=2, source_params=params)
            mark = "✅" if r else "⚠️"
            print(f"  {mark} {s:<20} {len(r)} results")
    elif args.action == "raw":
        srcs = args.sources.split(",") if args.sources else ["zh"]
        r = search(args.query, srcs, top_k=args.top)
        print(json.dumps({"count": len(r), "hits": r[:3]},
                          ensure_ascii=False, indent=2))
    else:
        fn = {"zh": search_zh, "xhc": search_xhc, "bing": search_bing,
              "google": search_google, "baidu": search_baidu,
              "multi": lambda q, top_k: search_multi(
                  q, args.sources.split(","), top_k=top_k)}[args.action]
        if args.action in ("zh", "xhc"):
            r = fn(args.query, top_k=args.top, days=args.days)
        else:
            r = fn(args.query, top_k=args.top)
        print(json.dumps({"count": len(r), "hits": r[:3]},
                          ensure_ascii=False, indent=2))
