#!/usr/bin/env python3
"""
v13.32 · search_memory · 部件 1 修 · 搜索记忆库
────────────────────────────────────────────────────────────
用户 loop deductive: "0 结果重拆·同话题不该搜多遍"
现状: search_meta_learning 只记 DNA · 没记"query → 最佳 URL"

功能:
1. record_win(query, url, confidence) · 记录哪条 query 找到了哪个高质 URL
2. remember(query, similar_threshold=0.6) · 新 query 来了 · 找语义相似的历史 query · 直接返回它当年的最佳 URL
3. 语义相似度: 简单实现·关键词重合度 (Jaccard)

数据:
  v11/.search_memory.jsonl · 一行一记录
  格式: {"query", "urls": [...], "conf_avg", "ts"}
"""
import os, sys, json, re
from datetime import datetime
from collections import defaultdict


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
V11 = os.path.join(BASE, "v11")
MEM_LOG = os.path.join(V11, ".search_memory.jsonl")


def _keywords(query: str) -> set:
    """提取关键词 · 中文 2-6 字 + 英文 3+ 字"""
    kws = set()
    for w in re.findall(r'[一-龥]{2,6}|[a-zA-Z]{3,}', query.lower()):
        kws.add(w)
    return kws


def _jaccard(a: set, b: set) -> float:
    if not a or not b: return 0.0
    return len(a & b) / len(a | b)


def record_win(query: str, url: str, confidence: float = 0.5, extras: dict = None):
    """记录一次成功: query 找到 url · confidence 是从 deep_extract 拿的"""
    if not query or not url or not url.startswith('http'):
        return
    entry = {
        "ts": datetime.now().isoformat(timespec='seconds'),
        "query": query[:200],
        "url": url,
        "confidence": round(float(confidence), 2),
        "keywords": list(_keywords(query))[:10],
    }
    if extras:
        entry.update({k: v for k, v in extras.items() if not k.startswith("_")})
    try:
        with open(MEM_LOG, "a", encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def remember(new_query: str, similar_threshold: float = 0.5,
              top_k: int = 3) -> list:
    """给新 query · 找历史相似 query · 返 top_k 曾经的最佳 URL"""
    if not os.path.exists(MEM_LOG):
        return []
    new_kws = _keywords(new_query)
    if not new_kws:
        return []
    candidates = []
    try:
        for line in open(MEM_LOG, encoding='utf-8'):
            try:
                r = json.loads(line)
                sim = _jaccard(new_kws, set(r.get("keywords", [])))
                if sim >= similar_threshold:
                    candidates.append({
                        "url": r["url"],
                        "title_hint": r.get("query", "")[:80],
                        "confidence": r.get("confidence", 0),
                        "similarity": round(sim, 2),
                        "when": r.get("ts", ""),
                    })
            except Exception:
                continue
    except Exception:
        pass
    # 按 sim * confidence 排
    candidates.sort(key=lambda c: -(c["similarity"] * c["confidence"]))
    return candidates[:top_k]


def stats() -> dict:
    """返回记忆库总量 + 高 confidence 数"""
    if not os.path.exists(MEM_LOG):
        return {"total": 0}
    total = 0
    hi_conf = 0
    urls = set()
    try:
        for line in open(MEM_LOG, encoding='utf-8'):
            try:
                r = json.loads(line)
                total += 1
                if r.get("confidence", 0) >= 0.7:
                    hi_conf += 1
                urls.add(r.get("url", ""))
            except Exception:
                continue
    except Exception:
        pass
    return {"total": total, "high_confidence": hi_conf,
            "unique_urls": len(urls)}


if __name__ == "__main__":
    try:
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("--record", nargs=3, help="--record 'query' 'url' confidence")
        ap.add_argument("--remember", help="用新 query 查历史相似")
        ap.add_argument("--stats", action="store_true", default=True)
        args = ap.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.record:
        record_win(args.record[0], args.record[1], float(args.record[2]))
        print("✅ 记录")
    elif args.remember:
        r = remember(args.remember)
        print(f"找到 {len(r)} 条历史相似:")
        for x in r:
            print(f"  sim={x['similarity']} conf={x['confidence']} · {x['url'][:80]}")
            print(f"    原 query: {x['title_hint']}")
    else:
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
