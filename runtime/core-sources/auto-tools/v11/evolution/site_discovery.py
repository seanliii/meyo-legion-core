"""v13.87.31 · site_discovery · 从 Google 结果发现新站 + 学 fetch_strategy

用户 2026-07-23 抓:
「Google 先搜可以让你学到什么网站在做啥·这样才可以获取更多内容持续搜索」

现役坑:
- planner 只从 15 个已知 domain 挑 (site_bookmark._KNOWN_STRATEGIES)
- Google 拿到 40K 字全文里的 URL 一次性用完 · 没记忆
- 遇新站不会学·下次同类 query 还是从固定 15 站里挑

── 3 步循环 ──

1. Google 首搜 · 拿 20-30 URLs
2. domain 抽出 → 判「新站」(不在 site_bookmark)
3. 新站入 site_bookmark · 默认 strategy · rating=45 · 首访待观察
4. 后续访问 · 记 avg_len/avg_time · 自动升 rating + 补 strategy

之后 planner 加载 · 会自动挑高 rating 的新发现站
"""
import json
import os
import re
import sys
import time
from typing import Optional
from urllib.parse import urlparse


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

DISCOVERY_LOG = os.path.join(V11, ".site_discovery.jsonl")

# 已知的 domain 类型规则 · LLM 判之前先规则兜底
_DOMAIN_TYPE_HINTS = {
    # 学术
    r"arxiv\.org|semanticscholar|scholar\.google": "academic",
    r"acm\.org|ieee\.org|springer|nature\.com|sciencedirect": "academic",
    # 技术
    r"github\.com|stackoverflow|dev\.to|medium\.com": "technical",
    r"news\.ycombinator|lobsters|reddit\.com/r/programming": "technical_forum",
    # 中文技术
    r"csdn\.net|juejin\.cn|segmentfault|infoq\.cn|oschina": "cn_technical",
    r"zhihu\.com/question|zhuanlan\.zhihu": "cn_knowledge",
    # 中文社媒
    r"xiaohongshu|weibo|douyin|b23\.tv|bilibili": "cn_social",
    r"mp\.weixin|weixin\.qq": "cn_wechat",
    # 商业新闻
    r"36kr\.com|huxiu\.com|geekpark|pingwest": "cn_business",
    r"techcrunch|theverge|wired|bloomberg|ft\.com": "en_business",
    # AI 官方
    r"anthropic\.com|openai\.com|deepmind|huggingface": "ai_official",
}


def _log(record: dict):
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    record.setdefault("unix_ts", int(time.time()))
    try:
        with open(DISCOVERY_LOG, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _classify_domain(domain: str) -> str:
    """规则兜底判 domain 类型"""
    for pattern, dtype in _DOMAIN_TYPE_HINTS.items():
        if re.search(pattern, domain):
            return dtype
    return "unknown"


def _infer_strategy(domain_type: str) -> dict:
    """根据 domain 类型 · 推荐 fetch strategy"""
    if domain_type == "academic":
        return {"primary": "friday_bing", "fallback": "curl",
                "depth_mode": "deep", "anti_bot_tier": 0}
    if domain_type == "technical":
        return {"primary": "curl", "fallback": "friday_bing",
                "depth_mode": "balanced", "anti_bot_tier": 0}
    if domain_type == "cn_technical":
        return {"primary": "webclaw", "fallback": "stealth",
                "depth_mode": "balanced", "anti_bot_tier": 2}
    if domain_type == "cn_knowledge":
        return {"primary": "friday_zh", "fallback": "stealth",
                "depth_mode": "deep", "anti_bot_tier": 3}
    if domain_type == "cn_social":
        return {"primary": "xhs_api", "fallback": "cdp",
                "depth_mode": "deep", "anti_bot_tier": 3}
    if domain_type == "cn_wechat":
        return {"primary": "sogou_weixin", "fallback": "cdp",
                "depth_mode": "deep", "anti_bot_tier": 3}
    if domain_type in ("technical_forum", "en_business"):
        return {"primary": "curl", "fallback": "friday_bing",
                "depth_mode": "balanced", "anti_bot_tier": 1}
    if domain_type == "cn_business":
        return {"primary": "webclaw", "fallback": "curl",
                "depth_mode": "balanced", "anti_bot_tier": 1}
    # unknown · 默认试 curl
    return {"primary": "curl", "fallback": "friday_bing",
            "depth_mode": "balanced", "anti_bot_tier": 1}


def discover_from_hits(hits: list, query: str = "") -> dict:
    """给 Google/Bing 首搜的 hits · 抽 domain · 判新站 · 落 bookmark
    返回 {new_domains: [...], already_known: [...], learned: {...}}
    """
    if not hits:
        return {"new_domains": [], "already_known": [], "learned": {}}

    # 抽全部 domain
    domains_from_hits = set()
    for h in hits:
        if not isinstance(h, dict):
            continue
        url = h.get("url", "")
        if url and url.startswith("http"):
            try:
                d = urlparse(url).netloc.lower()
                if d:
                    domains_from_hits.add(d)
            except Exception:
                continue

    if not domains_from_hits:
        return {"new_domains": [], "already_known": [], "learned": {}}

    # 查 site_bookmark 现有 domain
    try:
        from site_bookmark import (get_strategy, learn_strategy,
                                     record_tag_hit, _extract_tags_from_query)
    except Exception:
        return {"new_domains": [], "already_known": list(domains_from_hits),
                "learned": {}, "err": "no_site_bookmark"}

    # v13.87.33 · 抽 query 的 tag · 待会分给 domain
    _tags = _extract_tags_from_query(query) if query else []

    new_domains = []
    already_known = []
    learned = {}

    for d in domains_from_hits:
        existing = get_strategy(d)
        if existing and existing.get("primary"):
            already_known.append(d)
        else:
            # 新站 · 判类型 → 推 strategy → 学
            dtype = _classify_domain(d)
            strat = _infer_strategy(dtype)
            ok = learn_strategy(
                d, strat["primary"], strat["fallback"],
                strat["depth_mode"], strat["anti_bot_tier"]
            )
            if ok:
                new_domains.append({
                    "domain": d, "type": dtype, "strategy": strat,
                })
                learned[d] = strat

    # v13.87.33 · 给所有 domain (含已知) 加 tag score
    # 按每 hit 的 content_len 决定 score+ 幅度
    if _tags:
        tag_records = 0
        for h in hits:
            if not isinstance(h, dict):
                continue
            url = h.get("url", "")
            if not url or not url.startswith("http"):
                continue
            try:
                d = urlparse(url).netloc.lower()
                content = h.get("content", "") or ""
                if record_tag_hit(d, _tags, len(content)):
                    tag_records += 1
            except Exception:
                continue
        learned["_tag_records"] = tag_records

    result = {
        "new_domains": new_domains,
        "already_known": already_known[:10],  # 太多不列
        "learned_count": len(learned),
        "learned": learned,
        "query": query[:120],
    }
    _log({"type": "discover_batch", **result,
          "hits_total": len(hits),
          "domains_total": len(domains_from_hits)})
    return result


def get_recent_discoveries(days: int = 7, min_rating: int = 30) -> list:
    """给 planner 加载 · 最近学到的高质新站 · 用于 sources_ordered 扩展"""
    try:
        import sqlite3
        db = os.path.join(V11, ".bookmarks.db")
        if not os.path.exists(db):
            return []
        c = sqlite3.connect(db, timeout=3)
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(time.time() - days * 86400))
        rows = c.execute(
            "SELECT domain, hits, rating, primary_backend, depth_mode "
            "FROM sites WHERE first_seen >= ? AND rating >= ? "
            "ORDER BY hits DESC, rating DESC LIMIT 20",
            (cutoff, min_rating)
        ).fetchall()
        c.close()
        return [{"domain": r[0], "hits": r[1], "rating": r[2],
                  "primary": r[3], "depth_mode": r[4]}
                for r in rows]
    except Exception:
        return []


def stats() -> dict:
    """给 daily_report 消费"""
    if not os.path.exists(DISCOVERY_LOG):
        return {"total_batches": 0}
    total_batches = 0
    total_new_domains = 0
    try:
        with open(DISCOVERY_LOG) as f:
            for line in f:
                try:
                    r = json.loads(line.strip())
                    if r.get("type") == "discover_batch":
                        total_batches += 1
                        total_new_domains += r.get("learned_count", 0)
                except Exception:
                    continue
    except Exception:
        pass
    recent = get_recent_discoveries(days=7)
    return {
        "total_batches": total_batches,
        "total_new_domains_learned": total_new_domains,
        "recent_7d": len(recent),
        "recent_top5": recent[:5],
    }


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["smoke", "stats", "recent"])
        args = p.parse_args()
        if args.cmd == "smoke":
            # 假 hits · 试发现
            mock_hits = [
                {"url": "https://arxiv.org/abs/1234"},
                {"url": "https://sebastianraschka.com/blog/rag"},
                {"url": "https://towardsdatascience.com/rag-eval"},
                {"url": "https://github.com/langchain/rag"},  # 已知
                {"url": "https://blog.langchain.dev/rag-eval"},  # 新
            ]
            r = discover_from_hits(mock_hits, query="RAG evaluation")
            print(json.dumps(r, ensure_ascii=False, indent=2))
        elif args.cmd == "stats":
            print(json.dumps(stats(), ensure_ascii=False, indent=2))
        elif args.cmd == "recent":
            print(json.dumps(get_recent_discoveries(days=30),
                              ensure_ascii=False, indent=2))
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
