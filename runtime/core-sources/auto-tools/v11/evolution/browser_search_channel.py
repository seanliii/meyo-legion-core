"""browser_search_channel.py · 2026-08-20 · web-access 浏览器翻墙搜索通道

第二条翻墙通道(AnySearch 是第一条·额度1000/日耗尽时降级到此·无次数限制但慢)。

原理: 借道 web-access proxy(localhost:3457→本地 Chrome 9225)打开 bing 搜索页,
用页面内 JS 解析结果列表,返回标准 hit 格式。因为走的是本地真实浏览器,
天然跟随用户本地网络环境(能翻墙),不受 urllib 裸连被 TLS 墙拦的限制。

实测(2026-08-20): 借道 bing 搜 "Joe Williams Everton Europa League FC Krasnodar"
→ 解析出 4 条真结果(含 globalsportsarchive/transfermarkt 的 Everton vs FC Krasnodar
2014-12-11 权威比赛数据)·证明这条通道能翻墙且可当搜索源用。

接入方式(等 search_fabric.py 空出来后·由调用方 register_source 挂进 fabric):
    from browser_search_channel import search_via_browser
    register_source("browser_bing", {...schema...}, search_via_browser)
"""
from __future__ import annotations

import time
import urllib.parse
from pathlib import Path
import sys

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_HERE.parent) not in sys.path:  # v11/ 下有 global_sources
    sys.path.insert(0, str(_HERE.parent))

# 韧性: 复用 resilient_fetch 缓存(浏览器搜索慢·同 query 缓存价值高)
try:
    import resilient_fetch as _rf
except Exception:  # pragma: no cover
    _rf = None

_CACHE_TTL_S = 900  # 浏览器搜索慢·缓存久一点(15min)

# bing 结果解析 JS(li.b_algo 是 bing 每条结果的容器)
_PARSE_JS = """
Array.from(document.querySelectorAll('li.b_algo')).slice(0, MAXN).map(li => {
  const a = li.querySelector('h2 a');
  const p = li.querySelector('.b_caption p, .b_algoSlug, .b_snippet');
  return {
    title: a ? a.innerText : '',
    url: a ? a.href : '',
    content: p ? p.innerText : ''
  };
}).filter(x => x.title && x.url)
"""


def _get_gs():
    """懒加载 global_sources(它持有 web-access proxy 的真实调用函数)"""
    import global_sources as gs
    return gs


def search_via_browser(query: str, top_k: int = 6, **kwargs) -> list[dict]:
    """借道本地浏览器翻墙搜索(bing)·返回标准 hit 列表

    返回: [{url, title, content, source:"browser_bing", cost_usd, meta}, ...]
    失败/proxy 不可用: [{"_error": ...}]  (_transient 标记让上层可重试/降级)
    """
    # 1. 查缓存(省一次慢速浏览器往返)
    if _rf is not None:
        try:
            cached = _rf.cache_get("browser_bing", [query, top_k], ttl=_CACHE_TTL_S)
            if cached is not None:
                hits, _backend = cached
                if hits:
                    return hits
        except Exception:
            pass

    try:
        gs = _get_gs()
    except Exception as e:
        return [{"_error": f"browser_channel_import: {type(e).__name__}: {str(e)[:100]}",
                 "_transient": False}]

    # 2. proxy 健康检查(不健康→显式故障·让上层降级·不伪装成功)
    try:
        if not gs._sogou_proxy_ok():
            return [{"_error": "browser_proxy_down:localhost:3457", "_transient": True}]
    except Exception as e:
        return [{"_error": f"browser_proxy_check: {type(e).__name__}", "_transient": True}]

    url = "https://www.bing.com/search?q=" + urllib.parse.quote(query)
    tid = ""
    try:
        tid = gs._sogou_proxy_new_tab(url)
        if not tid:
            err = getattr(gs, "_SOGOU_LAST_TAB_ERR", "no_tid")
            return [{"_error": f"browser_new_tab_fail:{err}", "_transient": True}]

        # 等页面加载(bing 结果需渲染)
        time.sleep(5)

        js = _PARSE_JS.replace("MAXN", str(max(1, top_k)))
        res = gs._sogou_proxy_eval(tid, js, timeout=15)

        if not isinstance(res, list):
            return [{"_error": f"browser_parse_non_list:{str(res)[:80]}", "_transient": True}]

        hits = []
        for r in res:
            if not isinstance(r, dict):
                continue
            t = (r.get("title") or "").strip()
            u = (r.get("url") or "").strip()
            if not (t and u):
                continue
            hits.append({
                "url": u,
                "title": t,
                "content": (r.get("content") or "").strip()[:2000],
                "source": "browser_bing",
                "cost_usd": 0.0,
                "meta": {"via": "web_access_proxy", "engine": "bing", "translayer": True},
            })

        if not hits:
            # proxy 通道空 → 降级 playwright headless(第3条翻墙通道·更独立)
            hits = _search_via_playwright(query, top_k)
            if hits and not hits[0].get("_error"):
                _store_cache(query, top_k, hits)
            return hits

        # 3. 写缓存(只缓存成功非空)
        _store_cache(query, top_k, hits)
        return hits
    except Exception as e:
        # proxy 通道异常 → 降级 playwright headless
        pw = _search_via_playwright(query, top_k)
        if pw and not pw[0].get("_error"):
            _store_cache(query, top_k, pw)
            return pw
        return [{"_error": f"browser_search: {type(e).__name__}: {str(e)[:100]}",
                 "_transient": True}]
    finally:
        if tid:
            try:
                gs._sogou_proxy_close(tid)
            except Exception:
                pass


def _store_cache(query: str, top_k: int, hits: list) -> None:
    if _rf is not None:
        try:
            _rf.cache_set("browser_bing", [query, top_k], hits, ttl=_CACHE_TTL_S)
        except Exception:
            pass


def _search_via_playwright(query: str, top_k: int = 6) -> list[dict]:
    """第3条翻墙通道: playwright headless(无 GUI·纯 Python·不依赖外部 3457 服务)

    实测(2026-08-20): headless chromium 借道 bing 搜 Joe Williams 题→6 条真结果·
    比 web-access proxy 更独立(不需外部代理服务·沙盒无 GUI 也能跑)。作为 proxy 通道
    挂掉时的兜底。
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return [{"_error": f"playwright_import:{type(e).__name__}", "_transient": False}]
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            try:
                pg = b.new_page()
                pg.goto("https://www.bing.com/search?q=" + urllib.parse.quote(query),
                        timeout=25000)
                pg.wait_for_timeout(3000)
                res = pg.evaluate(
                    "() => Array.from(document.querySelectorAll('li.b_algo'))"
                    ".slice(0, %d).map(li => {"
                    "const a = li.querySelector('h2 a');"
                    "const c = li.querySelector('.b_caption p, .b_algoSlug, .b_snippet');"
                    "return {title: a?a.innerText:'', url: a?a.href:'', content: c?c.innerText:''}"
                    "}).filter(x => x.title && x.url)" % max(1, top_k)
                )
            finally:
                b.close()
        hits = []
        for r in (res or []):
            t = (r.get("title") or "").strip()
            u = (r.get("url") or "").strip()
            if t and u:
                hits.append({
                    "url": u, "title": t,
                    "content": (r.get("content") or "").strip()[:2000],
                    "source": "browser_bing_pw", "cost_usd": 0.0,
                    "meta": {"via": "playwright_headless", "engine": "bing", "translayer": True},
                })
        if not hits:
            return [{"_error": "playwright_empty", "_transient": False}]
        return hits
    except Exception as e:
        return [{"_error": f"playwright_search:{type(e).__name__}:{str(e)[:100]}",
                 "_transient": True}]


# ── CLI 自测 ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys as _sys
    q = _sys.argv[1] if len(_sys.argv) > 1 else "Joe Williams Everton FC Krasnodar"
    print(f"🌐 借道浏览器翻墙搜索: {q}")
    hits = search_via_browser(q, top_k=5)
    if hits and hits[0].get("_error"):
        print(f"❌ {hits[0]['_error']}")
    else:
        print(f"✅ {len(hits)} 条")
        for i, h in enumerate(hits, 1):
            print(f"  [{i}] {h.get('title', '')[:60]}")
            print(f"       {h.get('url', '')[:75]}")
