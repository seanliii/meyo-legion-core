"""v13.87.26 · AIsa multi-source-search skill 包装

用户 2026-07-23 抓正:「之前给你搜索那么多 skill 你都用上了吗?」
真相:
- ~/.claude/skills/multi-source-search 早就存在
- .env 里 AISA_API_KEY 也装了
- v13.87 我 25 个 commit · 0 处用过 · 完全忘了这层能力
- llm_judge 早抓过我编造调用 07-19 correction_pending
- facepalm 早 refuted 我 Perplexity 假断言 07-18

skill 提供 9 命令:
    web / scholar / smart / sonar / sonar-pro / sonar-reasoning-pro /
    sonar-deep-research / tavily-search / tavily-extract / tavily-crawl /
    tavily-map / verity

现役状态:
- API key: sk-d2n3... (在 .env)
- balance: 空 (2026-07-23 实测)
- 接线: 已就位 · 用户充值后一行开

对齐 memory:
- [[shrimp-v13.87-search-agent-facade]] SearchAgent 门面
- [[feedback-existing-tools-first-not-build]] 接线 > 新建
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

_SKILL_SCRIPT = os.path.expanduser(
    "~/.claude/skills/multi-source-search/scripts/search_client.py")

# 加载 .env (AISA_API_KEY)
try:
    from env_loader import load_env
    load_env()
except Exception:
    pass


def _log_health(source: str, record: dict) -> None:
    try:
        from fetcher_health import health_log
        health_log(f"aisa_{source}", record)
    except Exception:
        pass


def _put_cache(url: str, content: str, source: str,
                meta: Optional[dict] = None) -> None:
    try:
        from search_cache import cache_put
        if url and content:
            cache_put(url, content, f"aisa_{source}", meta or {})
    except Exception:
        pass


def _score_completeness(text: str) -> str:
    n = len(text or "")
    if n < 200:
        return "shell"
    if n < 1000:
        return "partial"
    return "full"


def _normalize_hit(item: dict, source: str) -> dict:
    """AIsa 返回 → 我们标准 hit 格式"""
    if not isinstance(item, dict):
        return None
    title = item.get("title", "")
    content = item.get("content", "") or item.get("snippet", "") or ""
    url = item.get("url", "") or item.get("link", "")
    return {
        "title": (title or "")[:200],
        "url": url,
        "content": (content or "")[:5000],
        "source": f"aisa_{source}",
        "completeness": _score_completeness(content),
        "meta": {
            "via": "aisa_multi_source",
            "aisa_source": source,
            "publish_time": item.get("published_date", ""),
            "score": item.get("score", 0),
            "content_len": len(content or ""),
        },
    }


def available() -> bool:
    """检查 AIsa skill 可用 · script 存在 + AISA_API_KEY 有"""
    if not os.path.exists(_SKILL_SCRIPT):
        return False
    return bool(os.environ.get("AISA_API_KEY", "").strip())


def _run(cmd_args: list, timeout: int = 30) -> dict:
    """subprocess 调 skill · fail-safe 返 {ok, data|err}"""
    if not available():
        return {"ok": False, "err": "aisa_unavailable"}
    try:
        r = subprocess.run(
            [sys.executable, _SKILL_SCRIPT] + cmd_args,
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            return {"ok": False, "err": f"rc={r.returncode}",
                    "stderr": r.stderr[:200]}
        try:
            data = json.loads(r.stdout)
            # AIsa 错误约定
            if isinstance(data, dict) and data.get("error"):
                return {"ok": False, "err": data["error"]}
            return {"ok": True, "data": data}
        except json.JSONDecodeError:
            return {"ok": False, "err": "non_json",
                    "stdout": r.stdout[:200]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": "timeout"}
    except Exception as e:
        return {"ok": False, "err": f"exc:{type(e).__name__}:{str(e)[:80]}"}


# ═════ 9 便捷入口 ═════

def search_web(query: str, count: int = 5, timeout: int = 15) -> list:
    """AIsa web 通用搜"""
    t0 = time.time()
    health = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "query": query[:120], "count_asked": count}
    r = _run(["web", "--query", query, "--count", str(count)], timeout)
    health["elapsed_s"] = round(time.time() - t0, 2)
    if not r["ok"]:
        health["ok"] = False
        health["err"] = r["err"]
        _log_health("web", health)
        return []
    data = r["data"]
    results = data.get("results") if isinstance(data, dict) else data
    if not isinstance(results, list):
        _log_health("web", {**health, "ok": False,
                              "err": "no_results_list"})
        return []
    hits = []
    for it in results[:count]:
        h = _normalize_hit(it, "web")
        if h:
            hits.append(h)
            _put_cache(h["url"], h["content"], "web",
                         {"title": h["title"]})
    health["ok"] = len(hits) > 0
    health["count"] = len(hits)
    _log_health("web", health)
    return hits


def search_scholar(query: str, count: int = 5,
                    year_from: Optional[int] = None,
                    timeout: int = 30) -> list:
    """AIsa scholar 学术搜索 (比 Friday arxiv 强)"""
    args = ["scholar", "--query", query, "--count", str(count)]
    if year_from:
        args.extend(["--year-from", str(year_from)])
    t0 = time.time()
    r = _run(args, timeout)
    health = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "query": query[:120], "elapsed_s": round(time.time() - t0, 2)}
    if not r["ok"]:
        _log_health("scholar", {**health, "ok": False, "err": r["err"]})
        return []
    data = r["data"]
    results = data.get("results") if isinstance(data, dict) else data
    hits = [_normalize_hit(it, "scholar")
             for it in (results or [])[:count]]
    hits = [h for h in hits if h]
    _log_health("scholar", {**health, "ok": len(hits) > 0,
                              "count": len(hits)})
    return hits


def sonar(query: str, tier: str = "sonar", timeout: int = 60) -> dict:
    """Perplexity Sonar · 答复式搜索 (带引用)
    tier: sonar | sonar-pro | sonar-reasoning-pro | sonar-deep-research
    返 {answer, citations, ...}
    """
    valid = {"sonar", "sonar-pro", "sonar-reasoning-pro",
             "sonar-deep-research"}
    if tier not in valid:
        return {"ok": False, "err": f"invalid_tier:{tier}"}
    t0 = time.time()
    tm = timeout if tier != "sonar-deep-research" else 300
    r = _run([tier, "--query", query], tm)
    health = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "query": query[:120], "tier": tier,
              "elapsed_s": round(time.time() - t0, 2)}
    if not r["ok"]:
        _log_health(tier, {**health, "ok": False, "err": r["err"]})
        return {"ok": False, "err": r["err"]}
    data = r["data"]
    _log_health(tier, {**health, "ok": True})
    return {"ok": True, "answer": data.get("answer", ""),
            "citations": data.get("citations", []),
            "raw": data}


def tavily_search(query: str, timeout: int = 30) -> list:
    """Tavily 搜索 (AI 优化)"""
    t0 = time.time()
    r = _run(["tavily-search", "--query", query], timeout)
    health = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "query": query[:120], "elapsed_s": round(time.time() - t0, 2)}
    if not r["ok"]:
        _log_health("tavily", {**health, "ok": False, "err": r["err"]})
        return []
    data = r["data"]
    results = data.get("results") if isinstance(data, dict) else data
    hits = [_normalize_hit(it, "tavily") for it in (results or [])]
    hits = [h for h in hits if h]
    _log_health("tavily", {**health, "ok": len(hits) > 0,
                             "count": len(hits)})
    return hits


def tavily_extract(urls: list, timeout: int = 60) -> list:
    """Tavily 从 URL 抽内容 (代替我手搓 fetch)"""
    if not urls:
        return []
    url_arg = ",".join(urls[:10])  # 一次最多 10
    r = _run(["tavily-extract", "--urls", url_arg], timeout)
    if not r["ok"]:
        return []
    data = r["data"]
    results = data.get("results") if isinstance(data, dict) else data
    return [_normalize_hit(it, "tavily_extract")
            for it in (results or []) if _normalize_hit(it, "tavily_extract")]


def verity(query: str, count: int = 10, timeout: int = 60) -> dict:
    """AIsa 多源置信度评分 (核心能力·跟我 supervise_loop 概念重叠但官方版更强)
    返 {results, confidence, sources_used, ...}
    """
    t0 = time.time()
    r = _run(["verity", "--query", query, "--count", str(count)], timeout)
    health = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "query": query[:120], "elapsed_s": round(time.time() - t0, 2)}
    if not r["ok"]:
        _log_health("verity", {**health, "ok": False, "err": r["err"]})
        return {"ok": False, "err": r["err"]}
    data = r["data"]
    _log_health("verity", {**health, "ok": True,
                             "confidence": data.get("confidence") if isinstance(data, dict) else None})
    return {"ok": True, **(data if isinstance(data, dict) else {"raw": data})}


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["web", "scholar", "sonar", "tavily",
                                         "verity", "check"])
        p.add_argument("query", nargs="?", default="RAG evaluation")
        p.add_argument("--tier", default="sonar")
        p.add_argument("--top", type=int, default=5)
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "check":
        print(f"available: {available()}")
        print(f"script: {_SKILL_SCRIPT}")
        print(f"AISA_API_KEY: {bool(os.environ.get('AISA_API_KEY'))}")
    elif args.cmd == "web":
        r = search_web(args.query, args.top)
        print(json.dumps({"n": len(r), "hits": r[:3]},
                          ensure_ascii=False, indent=2)[:1500])
    elif args.cmd == "scholar":
        r = search_scholar(args.query, args.top)
        print(json.dumps({"n": len(r), "hits": r[:3]},
                          ensure_ascii=False, indent=2)[:1500])
    elif args.cmd == "sonar":
        r = sonar(args.query, tier=args.tier)
        print(json.dumps(r, ensure_ascii=False, indent=2)[:1500])
    elif args.cmd == "tavily":
        r = tavily_search(args.query)
        print(json.dumps({"n": len(r), "hits": r[:3]},
                          ensure_ascii=False, indent=2)[:1500])
    elif args.cmd == "verity":
        r = verity(args.query, args.top)
        print(json.dumps(r, ensure_ascii=False, indent=2)[:1500])
