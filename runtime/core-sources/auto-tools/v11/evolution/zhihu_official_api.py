"""v13.87 · 知乎官方 API fetcher · 抄进 skill_bridge

来源: https://developer.zhihu.com/download/zhihu_search_skills.zip
      https://developer.zhihu.com/download/global_search_skills.zip

对标 xhs_api (v13.86 · [[shrimp-v13.86-xhs-meituan-api]]):
- 官方 API 通道 · 跳过反爬
- 秒返 · 无 CDP · 无 captcha
- 结构化返回 · 带社会证明信号 (vote_up_count · comment_count)

打通场景 (memory 抓过的痛):
- 知乎 stealth firecrawl 6.5s (v13.61) · 现在 API 若通 <1s
- wigolo default 403 秒回 · stealth 卡死 5min · 现在跳过反爬
- 我们没有的 vote_up_count 用来 rank · 直接抄 wigolo evidence_score

配套 wigolo 亮点 (v13.87 系):
- 亮点 1 · completeness 自评
- 亮点 3 · evidence_score (含 vote_up_count 因子)
- 亮点 4 · health_log
- 亮点 5 · search_cache 落盘

环境变量 (必设):
  ZHIHU_ACCESS_SECRET · 知乎开放平台 Bearer token
  可选: ZHIHU_OPENAPI_BASE_URL (默认 https://developer.zhihu.com)
"""
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


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

_DEFAULT_BASE = "https://developer.zhihu.com"
_TIMEOUT = 8

# v13.87 · 自动加载 .env · 避免 secret 分散
try:
    from env_loader import load_env
    load_env()
except Exception:
    pass


def _get_secret() -> str:
    return os.environ.get("ZHIHU_ACCESS_SECRET", "").strip()


def _get_base_url() -> str:
    return os.environ.get("ZHIHU_OPENAPI_BASE_URL", _DEFAULT_BASE).rstrip("/")


def _request(path: str, params: dict, timeout: int = _TIMEOUT) -> dict:
    """通用 GET Bearer · fail-safe 返 {'code': -1, 'error': ...}"""
    secret = _get_secret()
    if not secret:
        return {"code": -1, "error": "ZHIHU_ACCESS_SECRET_unset"}

    url = f"{_get_base_url()}{path}?{urlencode(params)}"
    req = Request(
        url=url, method="GET",
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Request-Timestamp": str(int(time.time())),
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"code": -2, "error": "non_json", "body": body[:200]}
    except HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        return {"code": err.code, "error": f"http_{err.code}",
                "body": body[:200]}
    except (URLError, TimeoutError) as e:
        return {"code": -3, "error": f"net:{str(e)[:100]}"}
    except Exception as e:
        return {"code": -4, "error": f"exc:{type(e).__name__}:{str(e)[:100]}"}


def _score_completeness(text: str) -> str:
    """本地版·跟 search_cache.score_completeness 一致 · 三档"""
    n = len(text or "")
    if n < 200:
        return "shell"
    if n < 1000:
        return "partial"
    return "full"


def _normalize_item(item: dict, source: str) -> dict:
    """API 返回 → 我们标准 hit 格式 · 抄进 wigolo 亮点 1"""
    if not isinstance(item, dict):
        return None
    title = item.get("Title", "") or item.get("title", "")
    summary = item.get("ContentText", "") or item.get("summary", "")
    url_i = item.get("Url", "") or item.get("url", "")
    author = item.get("AuthorName", "") or item.get("author_name", "")
    edit_ts = item.get("EditTime", 0) or item.get("edit_time", 0)
    vote = item.get("VoteUpCount", 0) or item.get("vote_up_count", 0)
    comment = item.get("CommentCount", 0) or item.get("comment_count", 0)

    return {
        "title": (title or "")[:200],
        "url": url_i,
        "content": (summary or "")[:5000],
        "source": source,
        "completeness": _score_completeness(summary),   # 亮点 1
        "meta": {
            "via": "zhihu_official_api",
            "author": author,
            "vote_up_count": int(vote or 0),
            "comment_count": int(comment or 0),
            "edit_time": int(edit_ts or 0),
            "publish_time": (time.strftime("%Y-%m-%d",
                                           time.localtime(int(edit_ts)))
                             if edit_ts else ""),
            "content_len": len(summary or ""),
        },
    }


def _log_health(fetcher: str, record: dict) -> None:
    """亮点 4 · 走 fetcher_health"""
    try:
        from fetcher_health import health_log
        health_log(fetcher, record)
    except Exception:
        # fallback · 直接落盘
        path = os.path.join(V11, f".{fetcher}_health.jsonl")
        try:
            with open(path, "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


def _put_cache(hit: dict, fetcher: str) -> None:
    """亮点 5 · 落 search_cache"""
    try:
        from search_cache import cache_put
        if hit.get("url") and hit.get("content"):
            cache_put(hit["url"], hit["content"], fetcher, {
                "title": hit.get("title", ""),
                "author": hit.get("meta", {}).get("author", ""),
                "vote_up_count": hit.get("meta", {}).get("vote_up_count", 0),
            })
    except Exception:
        pass


def search_zhihu_official(query: str, count: int = 5,
                           timeout: int = _TIMEOUT) -> list:
    """知乎搜索 · v13.87.1 (整合)
    - 优先走 Friday universal-search sources=[zh] (统一 · 秒返)
    - 降级走原知乎开发者 API (ZHIHU_ACCESS_SECRET)
    - 都挂返 [] · fail-safe
    """
    # 优先 Friday · 现役 token 已白名单
    try:
        from friday_universal import search_zh
        r = search_zh(query, top_k=count)
        if r:
            return r
    except Exception:
        pass
    # 降级 · 原开发者 API 实现 (需 ZHIHU_ACCESS_SECRET)
    return _search_zhihu_via_developer_api(query, count, timeout)


def _search_zhihu_via_developer_api(query: str, count: int = 5,
                                     timeout: int = _TIMEOUT) -> list:
    """原 v13.87 实现 · 保留兼容 · 需 ZHIHU_ACCESS_SECRET"""
    t0 = time.time()
    count = max(1, min(10, int(count)))
    health = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "unix_ts": int(time.time()),
        "query": query, "count_asked": count,
        "ok": False, "count": 0, "elapsed_s": 0.0, "err": "",
    }
    resp = _request("/api/v1/content/zhihu_search",
                     {"Query": query, "Count": str(count)},
                     timeout=timeout)
    health["elapsed_s"] = round(time.time() - t0, 3)

    if resp.get("code") != 0 and "Code" not in resp:
        health["err"] = str(resp.get("error", resp))[:120]
        _log_health("zhihu_official", health)
        return []

    # 兼容 skill 层已封装 · 也接原始 API
    if "items" in resp:
        raw_items = resp.get("items") or []
    elif "Data" in resp:
        data = resp.get("Data") or {}
        raw_items = data.get("Items") or []
    else:
        raw_items = []

    results = []
    total_content = 0
    comp_dist = {}
    for it in raw_items[:count]:
        h = _normalize_item(it, "zhihu")
        if not h:
            continue
        results.append(h)
        total_content += len(h["content"])
        c = h["completeness"]
        comp_dist[c] = comp_dist.get(c, 0) + 1
        _put_cache(h, "zhihu_official")

    health["ok"] = len(results) > 0
    health["count"] = len(results)
    health["avg_content_len"] = (int(total_content / len(results))
                                  if results else 0)
    health["completeness_dist"] = comp_dist
    _log_health("zhihu_official", health)
    return results


def search_global_official(query: str, count: int = 10,
                            filter_expr: str = "",
                            search_db: str = "",
                            timeout: int = _TIMEOUT) -> list:
    """知乎全网搜索 API · v13.87
    参数:
      count: 1-20
      filter_expr: host==\"...\" AND publish_time>=...
      search_db: all|realtime|static
    """
    t0 = time.time()
    count = max(1, min(20, int(count)))
    health = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "unix_ts": int(time.time()),
        "query": query, "count_asked": count,
        "filter": filter_expr[:80] if filter_expr else "",
        "search_db": search_db, "ok": False, "count": 0,
        "elapsed_s": 0.0, "err": "",
    }
    params = {"Query": query, "Count": str(count)}
    if filter_expr:
        params["Filter"] = filter_expr
    if search_db:
        params["SearchDB"] = search_db

    resp = _request("/api/v1/content/global_search", params, timeout=timeout)
    health["elapsed_s"] = round(time.time() - t0, 3)

    if resp.get("code") != 0 and "Code" not in resp:
        health["err"] = str(resp.get("error", resp))[:120]
        _log_health("zhihu_global", health)
        return []

    if "items" in resp:
        raw_items = resp.get("items") or []
    elif "Data" in resp:
        data = resp.get("Data") or {}
        raw_items = data.get("Items") or []
    else:
        raw_items = []

    results = []
    total_content = 0
    comp_dist = {}
    for it in raw_items[:count]:
        h = _normalize_item(it, "global_web")
        if not h:
            continue
        results.append(h)
        total_content += len(h["content"])
        c = h["completeness"]
        comp_dist[c] = comp_dist.get(c, 0) + 1
        _put_cache(h, "zhihu_global")

    health["ok"] = len(results) > 0
    health["count"] = len(results)
    health["avg_content_len"] = (int(total_content / len(results))
                                  if results else 0)
    health["completeness_dist"] = comp_dist
    _log_health("zhihu_global", health)
    return results


# ═════ 亮点 3 · evidence_score 加 vote_up 因子 ═════

def score_with_social_signal(hit: dict, query: str = "") -> dict:
    """在 search_agent_orchestrator.score_evidence 上加社会证明因子
    - vote_up 归一化: log2(vote+1)/16  (2^16=65536 上限)
    - 权重 15% · 挤走 recency 一点
    """
    try:
        from search_agent_orchestrator import score_evidence
    except Exception:
        return {"final": 0.5, "components": {}, "explanation": "no_scorer"}

    base = score_evidence(hit, query)
    vote = int(hit.get("meta", {}).get("vote_up_count", 0))
    if vote > 0:
        import math
        social = min(1.0, math.log2(vote + 1) / 16.0)
        # 重新加权 · 加 social 15%
        comp = base["components"]
        final = (comp["completeness_boost"] * 0.30 +
                 comp["domain_quality"] * 0.25 +
                 comp["lexical_alignment"] * 0.20 +
                 comp["recency"] * 0.10 +
                 social * 0.15)
        base["final"] = round(final, 3)
        base["components"]["social_proof"] = round(social, 3)
        base["explanation"] += f" social={social:.2f}(vote={vote})"
    return base


def search_recent(query: str, days: int = 30, count: int = 10,
                   host: str = "") -> list:
    """v13.87 · 最近 N 天知乎新鲜帖
    - 实测 publish_time>= 过滤生效
    - host== 语法暂未测通·先不默认加
    """
    import datetime
    cutoff = int((datetime.datetime.now()
                   - datetime.timedelta(days=days)).timestamp())
    parts = [f"publish_time>={cutoff}"]
    if host:
        parts.append(f'host=="{host}"')
    return search_global_official(query, count=count,
                                    filter_expr=" AND ".join(parts))


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("mode", choices=["zhihu", "global", "score-demo"])
        p.add_argument("query", nargs="?", default="RAG")
        p.add_argument("--count", type=int, default=5)
        p.add_argument("--filter", default="")
        p.add_argument("--search-db", default="")
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.mode == "score-demo":
        demo_hit = {
            "url": "https://zhuanlan.zhihu.com/p/123",
            "content": "RAG 评测综述 · 主流框架 RAGAS TruLens 说明 " * 20,
            "completeness": "full",
            "meta": {"vote_up_count": 512, "publish_time": "2026-05-01"},
        }
        r = score_with_social_signal(demo_hit, args.query)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif args.mode == "zhihu":
        r = search_zhihu_official(args.query, args.count)
        print(json.dumps({"count": len(r), "hits": r[:3]},
                          ensure_ascii=False, indent=2))
    else:
        r = search_global_official(args.query, args.count,
                                    args.filter, args.search_db)
        print(json.dumps({"count": len(r), "hits": r[:3]},
                          ensure_ascii=False, indent=2))
