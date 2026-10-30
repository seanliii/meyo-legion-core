#!/usr/bin/env python3
"""
v13.88.x · api_discovery · 反爬强域名的官方 API/RSS/JSON endpoint 探测+调用
────────────────────────────────────────────────────────────────
用户诉求 (2026-07-24):
  CDP 站硬撞太卡·每次 perf_guard 冷却 20s×N。
  应给每个反爬强域名找到官方 API/RSS/JSON endpoint 替代·记 lesson·
  下次直走 API 不撞 CDP。

设计:
  1. 已知 API 表 (硬编码 lesson · 真跑测过): zhihu/bilibili/juejin/github/
     stackoverflow/reddit/hackernews/csdn(rss)/youtube/wikipedia 等。
  2. discover_api(domain) 先查本地 JSON lesson·没有再 curl 试探
     (api.{domain}, {domain}/api, {domain}/rss)。发现即写回 lesson JSON。
  3. try_api_first(url, domain, query=None) 已知 API 时直接调 API 拿内容·
     不走 CDP。拿不到返回 None (调用方回退原降级链)。
  4. lesson 存 ~/.claude/.cdp_api_alternatives.json (跨会话持久)。
  5. 环境变量 MEYO_QA_API_FIRST=0 可关整个 hook。

CLI:
  python3 api_discovery.py --domain zhihu.com --query "SaaS 续费"
"""

import os
import sys
import json
import time
import subprocess
from pathlib import Path
from urllib.parse import urlparse, quote_plus, urlencode


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


# ─── 常量 ────────────────────────────────────────────────────────
_HOME = Path(os.path.expanduser("~"))
LESSON_PATH = _HOME / ".claude" / ".cdp_api_alternatives.json"
LESSON_PATH.parent.mkdir(parents=True, exist_ok=True)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")


# v13.88.9g · 用户 07-31「测试 query 当真任务在生产里跑·产生报警」
# example_query ("SaaS 续费" 等) 只是 CLI/pytest 演示占位·绝不能在生产当真 query 搜
# 生产路径 (无显式 query·无 URL query 参数) → 返空·让调用方回退 curl/CDP·不搜 demo
# 只在 MEYO_TEST_MODE=1 或 __main__ CLI 里才允许用 example_query
def _demo_query_allowed() -> bool:
    """是否允许用 example_query 占位 (仅测试/CLI)·生产返 False"""
    return os.environ.get("MEYO_TEST_MODE") == "1"


def _example_q(info: dict) -> str:
    """取 example_query·仅测试模式下·否则返空 (生产不搜 demo)"""
    return info.get("example_query", "") if _demo_query_allowed() else ""

# ─── 已知 API 表 · 真跑测过的 lesson ────────────────────────────
# 每条:
#   endpoint: 主 endpoint (带占位 {q})
#   auth:     "none" | "token" | "key"
#   method:   "GET"
#   parser:   如何取内容 · handler 名 (下方实现)
#   example_query: 供 CLI 演示
#   note:     人话说明
KNOWN_APIS = {
    "zhihu.com": {
        # zhihu v4 未认证 401 · 走已有 zhihu_official_api.py 模块 (v13.87 已建·带 secret)
        "endpoint": "MODULE:zhihu_official_api",
        "auth": "token",
        "method": "MODULE",
        "parser": "zhihu_official_module",
        "example_query": "SaaS 续费",
        "note": "zhihu v4 需 secret · 走 zhihu_official_api 模块 · 0.79s (memory: shrimp-v13.87-zhihu-official-api)",
    },
    "bilibili.com": {
        "endpoint": ("https://api.bilibili.com/x/web-interface/search/all/v2?"
                     "keyword={q}"),
        "auth": "none",
        "method": "GET",
        "parser": "bilibili_search",
        "example_query": "SaaS 续费",
        "note": "B 站官方公开 search API · 无 token 也能拿",
    },
    "juejin.cn": {
        "endpoint": "https://api.juejin.cn/search_api/v1/search",
        "auth": "none",
        "method": "POST_JSON",
        "post_body": {"key_word": "{q}", "cursor": "0", "limit": 10,
                       "search_type": 0, "sort_type": 0, "version": 1},
        "parser": "juejin_search",
        "example_query": "SaaS 续费",
        "note": "掘金公开 search_api · JSON POST",
    },
    "csdn.net": {
        "endpoint": "https://so.csdn.net/api/v3/search?q={q}&t=all&p=1&s=0&tm=0&lv=-1&ft=0&l=&u=",
        "auth": "none",
        "method": "GET",
        "parser": "csdn_search",
        "example_query": "SaaS 续费",
        "note": "CSDN 官方 search API · JSON 返回 · 比 web 稳",
    },
    "github.com": {
        "endpoint": "https://api.github.com/search/repositories?q={q}&per_page=10",
        "auth": "none",
        "method": "GET",
        "parser": "github_search",
        "example_query": "meilisearch",
        "note": "GitHub REST API v3 · 未认证 60 req/hour",
    },
    "stackoverflow.com": {
        "endpoint": ("https://api.stackexchange.com/2.3/search/advanced?"
                      "order=desc&sort=relevance&q={q}&site=stackoverflow&pagesize=10"),
        "auth": "none",
        "method": "GET",
        "parser": "stackexchange_search",
        "example_query": "python asyncio timeout",
        "note": "StackExchange API 2.3 · 300 req/day 匿名",
    },
    "reddit.com": {
        "endpoint": "https://old.reddit.com/search.json?q={q}&limit=10&raw_json=1",
        "auth": "none",
        "method": "GET",
        "parser": "reddit_search",
        "example_query": "SaaS churn",
        "note": "old.reddit JSON · 无需 oauth · 添加 UA 即通",
    },
    "news.ycombinator.com": {
        "endpoint": "https://hn.algolia.com/api/v1/search?query={q}&hitsPerPage=15",
        "auth": "none",
        "method": "GET",
        "parser": "hn_algolia_search",
        "example_query": "SaaS pricing",
        "note": "HN Algolia mirror · 已在 web_access_bridge 用过·抽出复用",
    },
    "youtube.com": {
        "endpoint": None,  # 需 GOOGLE_API_KEY · 无 key 时降 invidious
        "endpoint_fallback": "https://invidious.snopyta.org/api/v1/search?q={q}",
        "auth": "key",
        "method": "GET",
        "parser": "invidious_search",
        "example_query": "SaaS churn analysis",
        "note": "YouTube Data API 需 key · 无 key 走 invidious 镜像 JSON",
    },
    "wikipedia.org": {
        "endpoint": ("https://en.wikipedia.org/w/api.php?"
                      "action=query&list=search&srsearch={q}&format=json&srlimit=10"),
        "auth": "none",
        "method": "GET",
        "parser": "wikipedia_search",
        "example_query": "software as a service",
        "note": "MediaWiki API · 稳",
    },
    # ─── 无 search API 但有 RSS/官方 endpoint ─────────────────
    "mp.weixin.qq.com": {
        "endpoint": None,
        "auth": "none",
        "method": "NONE",
        "parser": "sogou_weixin_bridge",
        "example_query": "SaaS 续费",
        "note": ("公众号无公开 search API · sogou 微信频道是唯一入口·"
                 "已在 v13.84/85 接入 (memory: shrimp-v13.85-sogou-weixin-fulltext)"),
    },
    "36kr.com": {
        "endpoint": "https://gateway.36kr.com/api/mis/nav/newsflash/list?b_id=&per_page=20",
        "auth": "none",
        "method": "GET",
        "parser": "kr36_newsflash",
        "example_query": "",
        "note": "36kr 快讯官方 gateway · 无 search 但拿最新流",
    },
    # ─── 反爬极强·无 API 可用·记 lesson 避免撞 ───────────────
    "xiaohongshu.com": {
        "endpoint": None,
        "auth": "none",
        "method": "NONE",
        "parser": None,
        "example_query": "",
        "note": "小红书无公开 API · 必走 bsk click flow (v13.70)·此处不 hook",
    },
    "weibo.com": {
        "endpoint": None,
        "auth": "token",
        "method": "NONE",
        "parser": None,
        "example_query": "",
        "note": "open.weibo.com 需 token 且限制大 · 现阶段跳过 · 走搜索引擎",
    },
    "douyin.com": {
        "endpoint": None,
        "auth": "none",
        "method": "NONE",
        "parser": None,
        "example_query": "",
        "note": "抖音无公开 API · 硬反爬 · 只能走搜索引擎间接",
    },
}


# ─── lesson 读写 ────────────────────────────────────────────────
def _load_lessons() -> dict:
    """加载本地 lesson · 与 KNOWN_APIS 合并 (lesson 覆盖已知)"""
    base = dict(KNOWN_APIS)
    if LESSON_PATH.exists():
        try:
            with open(LESSON_PATH, "r", encoding="utf-8") as f:
                learned = json.load(f)
            if isinstance(learned, dict):
                base.update(learned)
        except Exception:
            pass
    return base


def _save_lesson(domain: str, entry: dict) -> None:
    """写回 lesson (只写 KNOWN_APIS 里没有的·避免覆盖硬编码)"""
    existing = {}
    if LESSON_PATH.exists():
        try:
            with open(LESSON_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}
    existing[domain] = entry
    try:
        with open(LESSON_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ─── 域名 normalize ─────────────────────────────────────────────
def _normalize_domain(domain: str) -> str:
    d = (domain or "").lower().strip()
    if d.startswith("www."):
        d = d[4:]
    return d


def _match_lesson(domain: str, lessons: dict) -> tuple:
    """endswith 匹配 · 支持子域"""
    d = _normalize_domain(domain)
    if d in lessons:
        return d, lessons[d]
    for k, v in lessons.items():
        if d.endswith("." + k) or d == k:
            return k, v
    return None, None


# ─── curl 探测 ──────────────────────────────────────────────────
def _curl(url: str, timeout: int = 5, method: str = "GET",
          post_json: dict = None) -> tuple:
    """返回 (status, body_or_none)"""
    try:
        args = ["curl", "-sS", "-o", "-", "-w", "\n__STATUS__%{http_code}",
                "--max-time", str(timeout), "-A", _UA]
        if method == "POST_JSON":
            args += ["-X", "POST", "-H", "Content-Type: application/json",
                     "--data", json.dumps(post_json or {})]
        args.append(url)
        r = subprocess.run(args, capture_output=True, text=True,
                            timeout=timeout + 2)
        out = r.stdout or ""
        if "__STATUS__" in out:
            body, _, status = out.rpartition("__STATUS__")
            return int(status.strip() or 0), body
        return 0, out
    except Exception:
        return 0, None


def discover_api(domain: str) -> dict:
    """
    发现某域名的 API/RSS endpoint。
    先查 lesson JSON · 没找到 curl 试探 (api.{d}, {d}/api, {d}/rss)。
    返回 {"has_api": bool, "endpoint": ..., "auth": ..., "example_query": ...,
          "source": "known|learned|probed|none"}
    """
    d = _normalize_domain(domain)
    if not d:
        return {"has_api": False, "source": "none", "reason": "empty domain"}

    lessons = _load_lessons()
    key, hit = _match_lesson(d, lessons)
    if hit is not None:
        _has = bool(hit.get("endpoint") or hit.get("endpoint_fallback")
                     or (hit.get("parser") == "sogou_weixin_bridge")
                     or (hit.get("method") == "MODULE"))
        return {
            "has_api": _has,
            "endpoint": hit.get("endpoint") or hit.get("endpoint_fallback"),
            "auth": hit.get("auth", "none"),
            "example_query": hit.get("example_query", ""),
            "note": hit.get("note", ""),
            "parser": hit.get("parser"),
            "method": hit.get("method", "GET"),
            "source": "known" if key in KNOWN_APIS else "learned",
            "matched_key": key,
        }

    # 未知域名 · 探测 3 个常见入口
    probes = [
        (f"https://api.{d}/", "api_subdomain"),
        (f"https://{d}/api/", "api_path"),
        (f"https://{d}/rss", "rss_path"),
        (f"https://{d}/feed", "feed_path"),
    ]
    for url, tag in probes:
        status, body = _curl(url, timeout=4)
        if status in (200, 401, 403) and body:
            # 200 = 通 · 401/403 = 存在但要认证 (也是 lesson)
            is_json = body.lstrip().startswith(("{", "["))
            is_xml = "<rss" in body[:500].lower() or "<feed" in body[:500].lower()
            if status == 200 and (is_json or is_xml):
                entry = {
                    "endpoint": url,
                    "auth": "none",
                    "method": "GET",
                    "parser": "generic_json" if is_json else "generic_rss",
                    "example_query": "",
                    "note": f"probed 200 via {tag}",
                }
                _save_lesson(d, entry)
                return {
                    "has_api": True, "endpoint": url, "auth": "none",
                    "example_query": "", "note": entry["note"],
                    "parser": entry["parser"], "method": "GET",
                    "source": "probed", "matched_key": d,
                }
    # 记录负 lesson (不 save · 避免污染 · 只返回)
    return {"has_api": False, "source": "none",
             "reason": f"no known API and probes failed for {d}"}


# ─── 具体 parser (每个域名一个) ─────────────────────────────────
def _fmt_zhihu(data: dict) -> str:
    lines = ["# 知乎 · 官方 search_v3 API"]
    for h in (data.get("data") or [])[:15]:
        obj = h.get("object") or {}
        typ = obj.get("type") or h.get("type") or "?"
        title = (obj.get("title") or obj.get("question", {}).get("name")
                  or obj.get("name") or "")
        excerpt = obj.get("excerpt") or ""
        url = (obj.get("url") or "").replace("api.zhihu.com/", "www.zhihu.com/")
        if not title and not excerpt:
            continue
        lines.append(f"- [{typ}] {title}")
        if excerpt:
            lines.append(f"  {excerpt[:200]}")
        if url:
            lines.append(f"  {url}")
    return "\n".join(lines)


def _fmt_bilibili(data: dict) -> str:
    lines = ["# B 站 · web-interface/search/all/v2"]
    for group in (data.get("data", {}).get("result") or []):
        gtype = group.get("result_type") or ""
        for it in (group.get("data") or [])[:8]:
            title = (it.get("title") or "").replace("<em class=\"keyword\">", "").replace("</em>", "")
            desc = it.get("description") or ""
            author = it.get("author") or ""
            bvid = it.get("bvid") or ""
            url = f"https://www.bilibili.com/video/{bvid}" if bvid else it.get("arcurl", "")
            if not title:
                continue
            lines.append(f"- [{gtype}] {title} · {author}")
            if desc:
                lines.append(f"  {desc[:150]}")
            if url:
                lines.append(f"  {url}")
    return "\n".join(lines)


def _fmt_juejin(data: dict) -> str:
    lines = ["# 掘金 · search_api/v1/search"]
    for it in (data.get("data") or [])[:15]:
        info = it.get("result_model") or it
        title = info.get("article_info", {}).get("title") or info.get("title") or ""
        brief = info.get("article_info", {}).get("brief_content") or ""
        aid = info.get("article_info", {}).get("article_id") or info.get("article_id")
        url = f"https://juejin.cn/post/{aid}" if aid else ""
        if not title:
            continue
        lines.append(f"- {title}")
        if brief:
            lines.append(f"  {brief[:180]}")
        if url:
            lines.append(f"  {url}")
    return "\n".join(lines)


def _fmt_csdn(data: dict) -> str:
    lines = ["# CSDN · so.csdn.net/api/v3/search"]
    for it in (data.get("result_vos") or [])[:15]:
        title = (it.get("title") or "").replace("<em>", "").replace("</em>", "")
        desc = (it.get("description") or "").replace("<em>", "").replace("</em>", "")
        url = it.get("url") or ""
        if not title:
            continue
        lines.append(f"- {title}")
        if desc:
            lines.append(f"  {desc[:180]}")
        if url:
            lines.append(f"  {url}")
    return "\n".join(lines)


def _fmt_github(data: dict) -> str:
    lines = ["# GitHub · api.github.com/search/repositories"]
    for r in (data.get("items") or [])[:15]:
        full = r.get("full_name") or ""
        desc = r.get("description") or ""
        stars = r.get("stargazers_count") or 0
        lang = r.get("language") or ""
        url = r.get("html_url") or ""
        lines.append(f"- [{stars}⭐ {lang}] {full}")
        if desc:
            lines.append(f"  {desc[:200]}")
        lines.append(f"  {url}")
    return "\n".join(lines)


def _fmt_stackexchange(data: dict) -> str:
    lines = ["# StackOverflow · api.stackexchange.com"]
    for it in (data.get("items") or [])[:15]:
        title = it.get("title") or ""
        score = it.get("score") or 0
        ans = it.get("answer_count") or 0
        url = it.get("link") or ""
        lines.append(f"- [{score}⬆ {ans}💬] {title}")
        if url:
            lines.append(f"  {url}")
    return "\n".join(lines)


def _fmt_reddit(data: dict) -> str:
    lines = ["# Reddit · old.reddit search.json"]
    for c in (data.get("data", {}).get("children") or [])[:15]:
        d_ = c.get("data") or {}
        title = d_.get("title") or ""
        sub = d_.get("subreddit_name_prefixed") or ""
        ups = d_.get("ups") or 0
        cmts = d_.get("num_comments") or 0
        url = f"https://old.reddit.com{d_.get('permalink','')}"
        lines.append(f"- [{ups}⬆ {cmts}💬 {sub}] {title}")
        lines.append(f"  {url}")
    return "\n".join(lines)


def _fmt_hn(data: dict) -> str:
    lines = ["# Hacker News · Algolia search"]
    for h in (data.get("hits") or [])[:15]:
        title = h.get("title") or h.get("story_title") or ""
        pts = h.get("points") or 0
        cmts = h.get("num_comments") or 0
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID','')}"
        lines.append(f"- [{pts}⬆ {cmts}💬] {title}")
        lines.append(f"  {url}")
    return "\n".join(lines)


def _fmt_invidious(data: list) -> str:
    lines = ["# YouTube (via invidious 镜像)"]
    if not isinstance(data, list):
        return ""
    for v in data[:15]:
        title = v.get("title") or ""
        author = v.get("author") or ""
        vid = v.get("videoId") or ""
        url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
        lines.append(f"- {title} · {author}")
        if url:
            lines.append(f"  {url}")
    return "\n".join(lines)


def _fmt_wikipedia(data: dict) -> str:
    lines = ["# Wikipedia · MediaWiki API"]
    for it in (data.get("query", {}).get("search") or [])[:15]:
        title = it.get("title") or ""
        snippet = (it.get("snippet") or "").replace(
            "<span class=\"searchmatch\">", "").replace("</span>", "")
        url = f"https://en.wikipedia.org/wiki/{quote_plus(title.replace(' ', '_'))}"
        lines.append(f"- {title}")
        if snippet:
            lines.append(f"  {snippet[:200]}")
        lines.append(f"  {url}")
    return "\n".join(lines)


def _fmt_36kr(data: dict) -> str:
    lines = ["# 36kr · 快讯 gateway"]
    items = data.get("data", {}).get("items") or data.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    for it in items[:15]:
        title = it.get("title") or it.get("catchword") or ""
        desc = it.get("description") or it.get("summary") or ""
        lines.append(f"- {title}")
        if desc:
            lines.append(f"  {desc[:180]}")
    return "\n".join(lines)


_PARSER_MAP = {
    "zhihu_search":       _fmt_zhihu,
    "bilibili_search":    _fmt_bilibili,
    "juejin_search":      _fmt_juejin,
    "csdn_search":        _fmt_csdn,
    "github_search":      _fmt_github,
    "stackexchange_search": _fmt_stackexchange,
    "reddit_search":      _fmt_reddit,
    "hn_algolia_search":  _fmt_hn,
    "invidious_search":   _fmt_invidious,
    "wikipedia_search":   _fmt_wikipedia,
    "kr36_newsflash":     _fmt_36kr,
}


# ─── query 抽取 (从 URL) ────────────────────────────────────────
def _extract_query_from_url(url: str) -> str:
    """从 search URL 抽 q/query/keyword/wd 参数"""
    try:
        from urllib.parse import parse_qs
        q = parse_qs(urlparse(url).query)
        for k in ("q", "query", "keyword", "wd", "kw", "s"):
            if k in q and q[k]:
                return q[k][0]
    except Exception:
        pass
    return ""


# ─── try_api_first · 核心 hook ─────────────────────────────────
def try_api_first(url: str, domain: str = None, query: str = None) -> dict:
    """
    尝试用官方 API 拿内容·避开 CDP。
    参数:
      url: 原始 URL (供 fallback 抽 query)
      domain: 已 parse 好的 domain (省一次 urlparse)
      query: 显式传入的查询词 (无则从 URL 抽)
    返回:
      {"ok": bool, "content": str, "via": "api:{parser}", "note": "..."}
      拿不到返回 {"ok": False, ...} · 调用方回退 CDP 链
    """
    if os.environ.get("MEYO_QA_API_FIRST", "1") == "0":
        return {"ok": False, "reason": "MEYO_QA_API_FIRST=0 disabled"}

    if not domain:
        domain = urlparse(url).netloc
    info = discover_api(domain)
    if not info.get("has_api"):
        return {"ok": False, "reason": info.get("reason", "no api")}

    # v13.88.67 · 用户 07-28 抓「读 github issue URL 返 meilisearch trending」
    # 病根: issue/pull/blob/tree/user 主页 URL 没 search query · 降到 example_query
    # 治: 这类 URL 直接跳 api-first · 让 curl 真读 HTML
    if not query:  # 无显式 query 时才判
        _skip_api_first_paths = (
            "/issues/", "/pull/", "/pulls/", "/blob/", "/tree/",
            "/commit/", "/releases/", "/wiki/", "/actions",
        )
        if any(p in url for p in _skip_api_first_paths):
            return {"ok": False,
                    "reason": "issue/pull/blob URL · 无 query · 跳 api-first",
                    "handoff": "curl"}
        # github.com/user 或 github.com/org · 也不该走 search API
        # (search API 需要 query · 不能凭 user 名回 trending)
        from urllib.parse import urlparse as _up
        _path = _up(url).path.strip("/")
        _parts = [p for p in _path.split("/") if p]
        if len(_parts) <= 1 and domain in ("github.com", "www.github.com"):
            return {"ok": False,
                    "reason": "github 用户/组织主页 · 无 query · 跳 api-first",
                    "handoff": "curl"}

    parser_name = info.get("parser")
    endpoint = info.get("endpoint")
    method = info.get("method", "GET")

    # 特殊: sogou_weixin_bridge · 走 web_access_bridge 已有 sogou 通路
    if parser_name == "sogou_weixin_bridge":
        # 不做事·让原降级链走 · 但记 note 表明"已知走 sogou"
        return {"ok": False, "reason": "handled by sogou pathway in bridge",
                 "handoff": "sogou"}

    if not endpoint or not parser_name:
        return {"ok": False, "reason": "endpoint or parser missing"}

    # MODULE 类·委托给已有 python 模块 (不 curl)
    if method == "MODULE" and parser_name == "zhihu_official_module":
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from zhihu_official_api import search_zhihu_official
            q = query or _extract_query_from_url(url) or _example_q(info)
            if not q:
                return {"ok": False, "reason": "no query for zhihu module"}
            t0 = time.time()
            hits = search_zhihu_official(q, count=8)
            dt = round(time.time() - t0, 3)
            if not hits:
                return {"ok": False, "reason": "zhihu module empty",
                         "time_s": dt}
            lines = ["# 知乎 · zhihu_official_api 模块"]
            for h in hits[:10]:
                title = h.get("title") or ""
                excerpt = (h.get("text") or h.get("excerpt") or "")[:200]
                u = h.get("url") or ""
                vote = h.get("vote_up_count") or h.get("voteup_count") or 0
                lines.append(f"- [{vote}⬆] {title}")
                if excerpt:
                    lines.append(f"  {excerpt}")
                if u:
                    lines.append(f"  {u}")
            return {"ok": True, "content": "\n".join(lines),
                     "via": "api:zhihu_official_module", "time_s": dt,
                     "note": info.get("note", ""),
                     "domain": "zhihu.com"}
        except Exception as e:
            return {"ok": False, "reason": f"zhihu module error: {e}"}

    # query 优先级: 显式 > URL 参数 > example_query (仅 MEYO_TEST_MODE·生产不搜 demo)
    q = query or _extract_query_from_url(url) or _example_q(info)
    if "{q}" in endpoint and not q:
        return {"ok": False, "reason": "no query to inject"}

    # 拼 endpoint
    matched_key = info.get("matched_key") or _normalize_domain(domain)
    lessons = _load_lessons()
    _, entry = _match_lesson(matched_key, lessons)
    post_body = None
    if method == "POST_JSON" and entry:
        raw_body = entry.get("post_body") or {}
        # 替换 {q}
        post_body = {}
        for k, v in raw_body.items():
            if isinstance(v, str):
                post_body[k] = v.replace("{q}", q)
            else:
                post_body[k] = v
        target = endpoint
    else:
        target = endpoint.replace("{q}", quote_plus(q))

    t0 = time.time()
    status, body = _curl(target, timeout=8, method=method, post_json=post_body)
    dt = round(time.time() - t0, 3)
    if status != 200 or not body:
        return {"ok": False, "reason": f"http {status}", "time_s": dt}

    # 尝试 JSON parse
    try:
        data = json.loads(body)
    except Exception:
        # 有些 endpoint 返回 XML (RSS) · 走 generic
        if parser_name == "generic_rss":
            return {"ok": True, "content": body[:20000],
                     "via": "api:generic_rss", "time_s": dt,
                     "note": info.get("note", "")}
        return {"ok": False, "reason": "non-json body", "time_s": dt}

    fn = _PARSER_MAP.get(parser_name)
    if not fn:
        # generic fallback
        return {"ok": True, "content": json.dumps(data, ensure_ascii=False)[:20000],
                 "via": f"api:{parser_name or 'generic'}", "time_s": dt,
                 "note": info.get("note", "")}

    try:
        content = fn(data)
    except Exception as e:
        return {"ok": False, "reason": f"parser error: {e}", "time_s": dt}

    if not content or len(content) < 50:
        return {"ok": False, "reason": "empty content from api", "time_s": dt}

    return {
        "ok": True,
        "content": content,
        "via": f"api:{parser_name}",
        "time_s": dt,
        "endpoint": target[:200],
        "note": info.get("note", ""),
        "domain": _normalize_domain(domain),
    }


# ─── CLI ────────────────────────────────────────────────────────
def _cli():
    # v13.88.9g · CLI 是手动/测试入口·允许用 example_query 演示占位
    os.environ.setdefault("MEYO_TEST_MODE", "1")
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--domain", required=False, help="域名 (e.g. zhihu.com)")
    ap.add_argument("--query", default=None, help="查询词")
    ap.add_argument("--list", action="store_true", help="列出所有已知 API")
    ap.add_argument("--probe", action="store_true",
                     help="仅探测·不发请求")
    args = ap.parse_args()

    if args.list:
        lessons = _load_lessons()
        print(f"# 已知 API 表 · {len(lessons)} 域名 (lesson: {LESSON_PATH})\n")
        for d, e in sorted(lessons.items()):
            has = "yes" if (e.get("endpoint") or
                              e.get("parser") == "sogou_weixin_bridge") else "no"
            parser = e.get("parser") or "-"
            auth = e.get("auth") or "?"
            note = (e.get("note") or "")[:60]
            print(f"[{has:3}] {d:28} auth={auth:5} parser={parser:22} note={note}")
        return

    if not args.domain:
        print("用法: python3 api_discovery.py --domain zhihu.com --query \"SaaS 续费\"")
        print("     python3 api_discovery.py --list")
        sys.exit(2)

    info = discover_api(args.domain)
    print(f"[discover_api] domain={args.domain}")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print()

    if args.probe:
        return
    if not info.get("has_api"):
        print("(无可用 API · 跳过 try_api_first)")
        return

    q = args.query or info.get("example_query") or ""
    print(f"[try_api_first] query={q!r}")
    fake_url = f"https://{args.domain}/search?q={quote_plus(q)}"
    r = try_api_first(fake_url, domain=args.domain, query=q)
    ok = r.get("ok")
    print(f"  ok={ok}  via={r.get('via','-')}  time_s={r.get('time_s','-')}  "
           f"reason={r.get('reason','-')}")
    content = r.get("content") or ""
    if content:
        print(f"  content_len={len(content)}")
        print("  --- preview ---")
        print("\n".join("  " + ln for ln in content.splitlines()[:20]))


if __name__ == "__main__":
    try:
        _cli()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
