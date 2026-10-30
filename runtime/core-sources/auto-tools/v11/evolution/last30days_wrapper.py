"""v13.87.26 · last30days skill 包装

用户 memory [[use-last30days-skill-for-search.md]] 硬门:
    「搜索时使用 last30days Skill 全网搜集信息 · 而不是只用内置的 WebSearch」

真相 (2026-07-23):
- ~/.claude/skills/last30days 早在
- 我 v13.87 25 commit 一次没用
- 违反用户明令 · 本次补

skill 覆盖 8 源:
    Reddit / X (Twitter) / YouTube / TikTok / Hacker News /
    Polymarket / GitHub / web

要求:
- python3.12+
- SCRAPECREATORS_API_KEY (在 .env)

现役接线:
- SearchAgent deep mode 追加 last30days 搜真人评论
- 补齐 memory 硬门
"""
import json
import os
import subprocess
import sys
import time
from typing import Optional


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

_SCRIPT = os.path.expanduser("~/.claude/skills/last30days/scripts/last30days.py")

try:
    from env_loader import load_env
    load_env()
except Exception:
    pass


def available() -> bool:
    """检查 skill 可用 · v13.87.27 修正:
    用户 2026-07-23 抓正:「https://github.com/mvanhorn/last30days-skill 不需要 key 啊」
    真相 (README 确认):
      - Reddit / HN / Polymarket / GitHub / arXiv / Techmeme 零配置免费
      - SCRAPECREATORS_API_KEY 只 TikTok/IG/Threads/LinkedIn 需要
      - 我上一轮硬求 KEY 判假绿 · 是我错

    现在: script 存在就 available · 有 key 只是解锁更多源
    """
    return os.path.exists(_SCRIPT)


def has_scrapecreators() -> bool:
    """是否有付费源 key (TikTok/IG 等要)"""
    return bool(os.environ.get("SCRAPECREATORS_API_KEY", "").strip())


def _log(record: dict):
    try:
        from fetcher_health import health_log
        health_log("last30days", record)
    except Exception:
        pass


def _put_cache(url: str, content: str, meta: Optional[dict] = None):
    try:
        from search_cache import cache_put
        if url and content:
            cache_put(url, content, "last30days", meta or {})
    except Exception:
        pass


def _score_completeness(text: str) -> str:
    n = len(text or "")
    if n < 200:
        return "shell"
    if n < 1000:
        return "partial"
    return "full"


def _normalize(item: dict, source: str) -> Optional[dict]:
    """v13.87.27 · last30days 真实字段
    每 item: {title, url, body, snippet, engagement, engagement_score,
              published_at, author, source, source_quality, local_relevance,
              why_relevant, freshness, item_id, ...}
    """
    if not isinstance(item, dict):
        return None
    title = item.get("title", "")
    # body 是全文 · snippet 是摘要
    content = (item.get("body", "") or item.get("snippet", "") or
               item.get("text", "") or "")
    url = item.get("url", "") or item.get("permalink", "")
    return {
        "title": (title or "")[:200],
        "url": url,
        "content": (content or "")[:5000],
        "source": f"last30days_{source}",
        "completeness": _score_completeness(content),
        "meta": {
            "via": "last30days",
            "source": source,
            "author": item.get("author", ""),
            "engagement": item.get("engagement", {}),
            "engagement_score": item.get("engagement_score", 0),
            "local_relevance": item.get("local_relevance", 0),
            "why_relevant": item.get("why_relevant", ""),
            "publish_time": item.get("published_at", ""),
            "freshness": item.get("freshness", ""),
            "source_quality": item.get("source_quality", 0),
            "content_len": len(content or ""),
        },
    }


# v13.87.27 · 零配置免费源清单 (无 key)
_FREE_SOURCES = ["reddit", "hn", "polymarket", "github", "arxiv", "techmeme"]


def search(topic: str, sources: Optional[list] = None,
           quick: bool = False, timeout: int = 90,
           free_only: bool = True) -> list:
    """last30days 搜过去 30 天真人内容
    sources: 显式指定 · 默认 _FREE_SOURCES (无 key)
    free_only: True 时未指定 sources 就只用免费源
    quick: True 走低延迟
    返 list of hit

    v13.87.27:
      - reddit/hn/github/polymarket/arxiv/techmeme 免费 (README 确认)
      - x/youtube/tiktok/instagram 等需 key
    """
    # 未指定 · 走免费源
    if sources is None:
        sources = list(_FREE_SOURCES) if free_only else None
    t0 = time.time()
    health = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "topic": topic[:120],
              "sources": sources or []}

    if not available():
        health["ok"] = False
        health["err"] = "skill_unavailable_or_no_api_key"
        _log(health)
        return []

    args = [sys.executable, _SCRIPT, topic, "--emit", "json"]
    if sources:
        args += ["--search", ",".join(sources)]
    if quick:
        args.append("--quick")

    try:
        r = subprocess.run(args, capture_output=True, text=True,
                             timeout=timeout)
        health["elapsed_s"] = round(time.time() - t0, 2)

        if r.returncode != 0:
            health["ok"] = False
            health["err"] = f"rc={r.returncode}·{r.stderr[:200]}"
            _log(health)
            return []
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            health["ok"] = False
            health["err"] = "non_json"
            _log(health)
            return []

        # v13.87.27 · 真实结构 (2026-07-23 直调验证):
        # top-level: {items_by_source, clusters, ranked_candidates, ...}
        # items_by_source.<source>: list of {title,url,body,snippet,engagement,...}
        hits = []
        ibs = data.get("items_by_source", {}) if isinstance(data, dict) else {}
        for src_name, items in ibs.items():
            if not isinstance(items, list):
                continue
            for it in items[:5]:  # 每源 top 5
                h = _normalize(it, src_name)
                if h:
                    hits.append(h)
                    _put_cache(h["url"], h["content"], {
                        "title": h["title"], "source": src_name})

        health["ok"] = len(hits) > 0
        health["count"] = len(hits)
        _log(health)
        return hits
    except subprocess.TimeoutExpired:
        health["elapsed_s"] = timeout
        health["ok"] = False
        health["err"] = "timeout"
        _log(health)
        return []
    except Exception as e:
        health["ok"] = False
        health["err"] = f"exc:{type(e).__name__}"
        _log(health)
        return []


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["check", "search"])
        p.add_argument("topic", nargs="?", default="AI agent")
        p.add_argument("--sources", default="", help="reddit,x,hn 等")
        p.add_argument("--quick", action="store_true")
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "check":
        print(f"available: {available()}")
        print(f"script: {_SCRIPT}")
        print(f"SCRAPECREATORS_API_KEY: {bool(os.environ.get('SCRAPECREATORS_API_KEY'))}")
    elif args.cmd == "search":
        srcs = args.sources.split(",") if args.sources else None
        r = search(args.topic, sources=srcs, quick=args.quick)
        print(json.dumps({"n": len(r), "hits": r[:3]},
                          ensure_ascii=False, indent=2)[:1500])
