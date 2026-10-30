"""v13.87.11 · search skill forger · 学 query→source 优化

用户 2026-07-22 抛：「skillforge · 每次知道如何优化搜索结果了吗」

现役 skill_bridge (v13.79) 是给 browser 交互 agent 用的
本文件 · 给搜索本身建独立 forger

工作方式:
1. 每次搜完记 (query_pattern, source, quality_score) 到 .search_skills.jsonl
2. quality_score = ok_ratio × avg_completeness_weight × domain_quality
3. 新 query 触发时:
   - 抽 pattern (关键词/语言/意图)
   - 查历史 top-3 匹配 pattern 的最优 source
   - 直接派发 · skip LLM plan_queries
4. exhaustive=True 时保留 LLM plan (穷尽模式不走捷径)

对比之前:
- 每次搜都走 LLM plan_queries (opus/haiku 请求·数秒·耗钱)
- 现在: 相似 query 直接命中历史 top → 秒返 · 省钱 · 快

Pattern 抽法 (轻量):
- lang: 是否含中文
- has_technical: 是否含 API/SDK/framework/model/agent 类词
- has_experience: 是否含 真实/经验/月入/踩坑 类词
- has_topic_tag: 关键名词 top-3 (jieba/re 抽)

不用 LLM 抽 pattern (省 · 快)
"""
import json
import os
import re
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
SKILLS_LOG = os.path.join(V11, ".search_skills.jsonl")


# 简单意图信号词
_TECHNICAL_WORDS = re.compile(
    r"\b(?:api|sdk|framework|model|agent|rust|python|golang|"
    r"react|kubernetes|docker|redis|kafka|llm|rag|token|"
    r"embedding|vector|prompt|inference)\b", re.I)
_EXPERIENCE_WORDS = re.compile(
    r"(真实|经验|月入|踩坑|副业|实操|案例|复盘|经历|收入|变现|真人|真的)")
_NEWS_WORDS = re.compile(r"(今天|昨天|最近|本周|本月|上周|新鲜|最新)")


def _extract_pattern(query: str) -> dict:
    """轻量抽 query 语义特征 · 不用 LLM"""
    if not query:
        return {}
    has_chinese = bool(re.search(r"[一-龥]", query))
    return {
        "lang": "zh" if has_chinese else "en",
        "has_technical": bool(_TECHNICAL_WORDS.search(query)),
        "has_experience": bool(_EXPERIENCE_WORDS.search(query)),
        "has_news": bool(_NEWS_WORDS.search(query)),
        "len": len(query),
    }


def _pattern_key(pat: dict) -> str:
    """把 pattern 序列化成 key · 用于聚合"""
    return f"{pat.get('lang','?')}_" \
           f"t{'1' if pat.get('has_technical') else '0'}_" \
           f"e{'1' if pat.get('has_experience') else '0'}_" \
           f"n{'1' if pat.get('has_news') else '0'}"


def _quality_score(hits: list) -> float:
    """给一次搜索结果算综合质量分 (0.0-1.0)"""
    if not hits:
        return 0.0
    total = len(hits)
    if total == 0:
        return 0.0
    # completeness 加权
    weight_map = {"full": 1.0, "partial": 0.5, "shell": 0.1}
    completeness_sum = sum(weight_map.get(h.get("completeness", "shell"), 0.1)
                            for h in hits if isinstance(h, dict))
    comp_avg = completeness_sum / total
    # url 有效性
    ok_ratio = sum(1 for h in hits
                   if isinstance(h, dict) and h.get("url", "").startswith("http")) / total
    return round((comp_avg * 0.7 + ok_ratio * 0.3), 3)


def record_search(query: str, source: str, hits: list,
                   elapsed_s: float = 0.0) -> dict:
    """一次搜完后记录 · 落 .search_skills.jsonl"""
    if not query or not source:
        return {}
    pat = _extract_pattern(query)
    pat_key = _pattern_key(pat)
    score = _quality_score(hits or [])
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "unix_ts": int(time.time()),
        "query": query[:200],
        "pattern_key": pat_key,
        "pattern": pat,
        "source": source,
        "count": len(hits or []),
        "quality": score,
        "elapsed_s": round(elapsed_s, 2),
    }
    try:
        with open(SKILLS_LOG, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return record


def suggest_sources(query: str, top_k: int = 3,
                     min_samples: int = 3,
                     min_quality: float = 0.4) -> list:
    """给新 query 建议 top-k source · 基于历史 pattern 命中
    - 抽 query pattern
    - 找相同 pattern_key 的历史记录
    - 按 quality 聚合到 (source, avg_quality, samples)
    - 至少 min_samples 才推荐
    """
    if not query or not os.path.exists(SKILLS_LOG):
        return []
    pat = _extract_pattern(query)
    pat_key = _pattern_key(pat)

    # 汇总 (source → [quality]) for same pattern_key
    by_source = {}
    try:
        with open(SKILLS_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("pattern_key") != pat_key:
                    continue
                src = r.get("source", "")
                q = r.get("quality", 0)
                if src:
                    by_source.setdefault(src, []).append(q)
    except Exception:
        return []

    # 计算 avg + 过滤
    scored = []
    for src, quals in by_source.items():
        if len(quals) < min_samples:
            continue
        avg = sum(quals) / len(quals)
        if avg < min_quality:
            continue
        scored.append({
            "source": src, "avg_quality": round(avg, 3),
            "samples": len(quals),
        })
    scored.sort(key=lambda x: -x["avg_quality"])
    return scored[:top_k]


def stats(days: int = 7) -> dict:
    """看学习成果 · 供 daily_report 消费"""
    if not os.path.exists(SKILLS_LOG):
        return {"total": 0}
    cutoff = time.time() - days * 86400
    records = []
    try:
        with open(SKILLS_LOG) as f:
            for line in f:
                try:
                    r = json.loads(line.strip())
                    if r.get("unix_ts", 0) >= cutoff:
                        records.append(r)
                except Exception:
                    continue
    except Exception:
        return {"total": 0}
    total = len(records)
    if total == 0:
        return {"total": 0, "days": days}
    # 按 pattern_key 聚
    by_pattern = {}
    for r in records:
        pk = r.get("pattern_key", "unknown")
        by_pattern.setdefault(pk, []).append(r)
    pattern_summary = {}
    for pk, rs in by_pattern.items():
        top_src = {}
        for r in rs:
            s = r.get("source", "?")
            top_src.setdefault(s, []).append(r.get("quality", 0))
        best = max(top_src.items(),
                    key=lambda kv: sum(kv[1]) / len(kv[1]))
        pattern_summary[pk] = {
            "records": len(rs),
            "best_source": best[0],
            "best_avg_quality": round(sum(best[1]) / len(best[1]), 3),
        }
    return {
        "total": total, "days": days,
        "unique_patterns": len(by_pattern),
        "patterns": pattern_summary,
    }


if __name__ == "__main__":
    try:
        import sys
        if len(sys.argv) > 1 and sys.argv[1] == "stats":
            print(json.dumps(stats(days=int(sys.argv[2]) if len(sys.argv) > 2 else 7),
                             ensure_ascii=False, indent=2))
        elif len(sys.argv) > 1 and sys.argv[1] == "suggest":
            q = sys.argv[2] if len(sys.argv) > 2 else "AI 副业"
            print(json.dumps(suggest_sources(q), ensure_ascii=False, indent=2))
        else:
            # smoke
            record_search("RAG 评测", "friday_zh", [
                {"url": "https://z.com/1", "content": "x" * 1200, "completeness": "full"},
                {"url": "https://z.com/2", "content": "y" * 500, "completeness": "partial"},
            ], elapsed_s=0.5)
            record_search("chatgpt 副业月入多少", "xhs_api", [
                {"url": "https://xhs.com/1", "content": "a" * 1500, "completeness": "full"},
            ])
            record_search("chatgpt 变现真实经验", "friday_zh", [
                {"url": "https://z.com/3", "content": "b" * 200, "completeness": "shell"},
            ])
            record_search("RAG framework agent", "friday_bing", [
                {"url": "https://ar.com/1", "content": "c" * 2000, "completeness": "full"},
            ])
            print(json.dumps(stats(days=1), ensure_ascii=False, indent=2))
            print()
            print("suggest for '副业变现真实':")
            print(json.dumps(suggest_sources("副业变现真实", min_samples=1),
                              ensure_ascii=False, indent=2))
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise
