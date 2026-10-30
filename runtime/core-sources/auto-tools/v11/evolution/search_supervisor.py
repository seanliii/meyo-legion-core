"""v13.87.13 · search supervisor · 每次搜后 LLM 反问「够不够」

用户 2026-07-22 抓正:「不是卡住·是有个监督的·每次问自己是不是需要搜·
是不是信息足够充分了·不充分就搜」

跟 stuck_auto_search 的差别:
    stuck                  supervisor
    ─────                  ──────────
    规则触发 (0/shell/重复)  每次都跑 (100% 触发)
    救命场景               主动进化
    不用 LLM               LLM 判决内容
    低频                    高频

设计:
- 每次 fanout / deep_read 完成后调 supervise(goal, hits)
- LLM (haiku 快) 判 3 件事:
    is_sufficient   够不够回答 goal
    confidence      0-100
    missing         缺什么
    next_action     stop / search_more / switch_source / dig_deeper
- next_action=search_more → 自动追一轮 fanout · 新 query 由 LLM 给
- next_action=switch_source → 换 source 重打
- next_action=dig_deeper → 对现有 URL 走 smart_fetch 深挖

参考现役:
- llm_judge (v13.66) · 单次判决 · 我们复用它的独立 subprocess 模式
- interactive_browser_agent.check_goal_met · 类似判决但给 browser 用

不建独立 LLM 客户端 · 走 audit_core.call_llm
"""
import json
import os
import re
import sys
import time
from pathlib import Path


# v13.88.79 · 静默监控统一接入 · 用户 07-28「所有静默监控都到齐了吗」
def _alert(severity: str, msg: str):
    try:
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.abspath(__file__)))
        from alert_bus import notify as _n
        _n(_o.path.basename(__file__).replace(".py",""), severity, msg)
    except Exception:
        pass


V11 = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SUPERVISOR_LOG = V11 / ".search_supervisor.jsonl"

# haiku 快 · 每次搜完都跑 · 0.1s 成本可接受
_MODEL = os.environ.get("MEYO_SUPERVISOR_MODEL", "haiku")

_SYSTEM_PROMPT = """You are a search supervisor. Given (goal, hits), decide if evidence is enough.

Return JSON only, no prose:
{
  "is_sufficient": true|false,
  "confidence": 0-100,
  "missing": "one line what's missing, empty if sufficient",
  "next_action": "stop|search_more|switch_source|dig_deeper",
  "next_query": "if search_more or switch_source, suggest a new query, empty otherwise",
  "reason": "one line why"
}

is_sufficient rules:
- goal is fact lookup + hits have 2+ authoritative sources with concrete data → sufficient
- goal is broad topic + hits have 3+ full completeness diverse sources → sufficient
- goal is quantitative (数字/收入/占比) + no concrete numbers in hits → NOT sufficient
- goal is 真实经验/踩坑 + only theoretical hits → NOT sufficient

next_action:
- stop: is_sufficient=true
- search_more: current source good but need more coverage
- switch_source: current source wrong (英文 topic 走了中文 source 等)
- dig_deeper: hits have good URLs but content shell/short, need to fetch full page

Confidence < 60 → is_sufficient=false always."""


def _log(record: dict) -> None:
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    record.setdefault("unix_ts", int(time.time()))
    try:
        with open(SUPERVISOR_LOG, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _summarize_hits(hits: list, max_items: int = 5,
                    per_hit: int = 800, total_cap: int = 6000) -> str:
    """给 LLM 看的 hits 摘要。
    v13.88.9L-63 · 同「证据截断」同类根因: preview 原每条只 150 字·
      判「这条 hit 够不够好」的关键信息常埋在 150 字后→监督 LLM 看不到真质量·
      逼它凭标题/开头瞎判(0.923 虚高的一角)。抬到 800 字/条·加总上限 6000 防喂爆判决 LLM。
    """
    if not hits:
        return "(no hits)"
    lines = []
    used = 0
    for i, h in enumerate(hits[:max_items]):
        if not isinstance(h, dict):
            continue
        title = h.get("title", "")[:80]
        url = h.get("url", "")[:80]
        comp = h.get("completeness", "?")
        # 剩余预算内给足 preview·答案常在开头之后
        budget = max(150, min(per_hit, total_cap - used))
        content_preview = (h.get("content", "") or "")[:budget].replace("\n", " ")
        used += len(content_preview)
        lines.append(f"[{i+1}] {title}\n    url={url}\n    "
                      f"comp={comp}  preview={content_preview}...")
        if used >= total_cap:
            lines.append(f"  (preview 预算已满·后续条略)")
            break
    if len(hits) > max_items:
        lines.append(f"  (还有 {len(hits) - max_items} 条略)")
    return "\n".join(lines)


def supervise(goal: str, hits: list, source: str = "",
               already_searched: list = None,
               timeout: int = 20) -> dict:
    """一次监督调用 · 返 next_action 决策
    - already_searched: 之前搜过的 query list · 避免推荐重复
    """
    hits = hits or []
    already = already_searched or []

    user_msg = (
        f"Goal (user wants to know):\n{goal[:500]}\n\n"
        f"Source used: {source}\n"
        f"Already searched: {json.dumps(already[-5:], ensure_ascii=False)}\n\n"
        f"Hits ({len(hits)} total):\n{_summarize_hits(hits)}\n\n"
        f"Judge JSON:"
    )

    t0 = time.time()
    try:
        from audit_core import call_llm, try_parse_json
        r = call_llm(_SYSTEM_PROMPT, user_msg,
                      model_override=_MODEL, timeout=timeout)
        elapsed = round(time.time() - t0, 2)
        if r.get("ok"):
            parsed = try_parse_json(r.get("raw", ""), default={
                "is_sufficient": len(hits) >= 5,  # v13.87.46 · 5 条起才够·1-4 太少
                "confidence": 40 if len(hits) < 5 else 55,
                "next_action": "search_more" if len(hits) < 5 else "stop",
                "reason": "no_json_fallback",
            })
            # v13.87.46 · conf<65 硬拉 sufficient=False · 别让 conf=50 冒充够
            _conf = parsed.get("confidence", 0)
            if _conf < 65 and parsed.get("is_sufficient"):
                parsed["is_sufficient"] = False
                parsed["reason"] = f"{parsed.get('reason','')}|conf<65_downgrade"
                if parsed.get("next_action") == "stop":
                    parsed["next_action"] = "search_more"
            parsed["_elapsed_s"] = elapsed
            _log({
                "goal": goal[:200],
                "source": source,
                "hits_count": len(hits),
                "decision": parsed,
            })
            return parsed
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        _log({"goal": goal[:200], "err": str(e)[:200],
              "elapsed_s": elapsed})

    # v13.87.46 · LLM 挂 · ≥5 hits 才判 sufficient · <5 强制搜多
    _enough = len(hits) >= 5
    return {
        "is_sufficient": _enough,
        "confidence": 55 if _enough else 30,
        "next_action": "stop" if _enough else "search_more",
        "reason": "llm_err",
        "_elapsed_s": round(time.time() - t0, 2),
    }


def supervise_loop(goal: str, initial_hits: list, source: str,
                    max_iterations: int = 3,
                    already_searched: list = None) -> dict:
    """完整闭环 · 直到充分或达到 max_iterations
    每一轮:
    - supervise 判决
    - is_sufficient → stop
    - search_more/switch_source → 加 next_query 供上游追搜
    - dig_deeper → 加 dig_urls 供上游深挖
    返 { final_action, iterations, all_hits, decisions, next_queries }
    """
    all_hits = list(initial_hits or [])
    decisions = []
    next_queries = []
    dig_urls = []
    already = list(already_searched or [])

    for i in range(max_iterations):
        d = supervise(goal, all_hits, source=source,
                       already_searched=already)
        decisions.append(d)

        if d.get("is_sufficient") or d.get("next_action") == "stop":
            break

        action = d.get("next_action", "stop")
        nq = d.get("next_query", "")
        if action in ("search_more", "switch_source") and nq:
            if nq not in already:
                next_queries.append(nq)
                already.append(nq)
        elif action == "dig_deeper":
            # 挑 shell/partial 的 URL 深挖
            for h in all_hits:
                if isinstance(h, dict) and h.get("completeness") in ("shell", "partial"):
                    u = h.get("url", "")
                    if u and u not in dig_urls:
                        dig_urls.append(u)
            break  # 深挖不再循环 supervise · 上游拿 dig_urls 处理

        # 只有一次 supervise 会调用上游追搜·上游要循环调 supervise_loop 才多轮
        break

    return {
        "final_action": decisions[-1].get("next_action") if decisions else "stop",
        "final_confidence": decisions[-1].get("confidence") if decisions else 0,
        "is_sufficient": (decisions[-1].get("is_sufficient")
                          if decisions else False),
        "iterations": len(decisions),
        "next_queries": next_queries,
        "dig_urls": dig_urls,
        "decisions": decisions,
    }


def stats(days: int = 7) -> dict:
    """看 supervisor 学到啥·给 daily_report 消费"""
    if not SUPERVISOR_LOG.exists():
        return {"total": 0}
    cutoff = time.time() - days * 86400
    records = []
    try:
        with open(SUPERVISOR_LOG) as f:
            for line in f:
                try:
                    r = json.loads(line.strip())
                    if r.get("unix_ts", 0) >= cutoff:
                        records.append(r)
                except:
                    continue
    except Exception:
        return {"total": 0}

    total = len(records)
    if total == 0:
        return {"total": 0, "days": days}

    # 决策分布
    action_dist = {}
    sufficient_cnt = 0
    for r in records:
        d = r.get("decision", {})
        a = d.get("next_action", "?")
        action_dist[a] = action_dist.get(a, 0) + 1
        if d.get("is_sufficient"):
            sufficient_cnt += 1

    return {
        "total": total, "days": days,
        "sufficient_rate": round(sufficient_cnt / total, 3),
        "action_dist": action_dist,
    }


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["judge", "loop", "stats"])
        p.add_argument("--goal", default="")
        p.add_argument("--hits-file", default="")
        p.add_argument("--source", default="test")
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    else:
        # smoke · 用 mock hits
        mock_hits_sufficient = [
            {"title": "RAG 评测综述", "url": "https://arxiv.org/1",
             "content": "指标 Recall@K MRR ..." * 100,
             "completeness": "full"},
            {"title": "如何做 RAG 评估", "url": "https://z.com/2",
             "content": "分层 · 检索层用 Hit Rate ..." * 80,
             "completeness": "full"},
            {"title": "评估方法综合对比", "url": "https://blog.com/3",
             "content": "总结 5 家评估工具 ..." * 60,
             "completeness": "full"},
        ]
        mock_hits_thin = [
            {"title": "标题", "url": "https://x.com/1",
             "content": "很短",
             "completeness": "shell"},
        ]
        goal = args.goal or "如何评估 RAG 系统的检索质量"

        print(f"=== supervise 充分 hits ===")
        r1 = supervise(goal, mock_hits_sufficient, source="friday_zh")
        print(json.dumps(r1, ensure_ascii=False, indent=2))
        print()
        print(f"=== supervise 稀薄 hits ===")
        r2 = supervise(goal, mock_hits_thin, source="hackernews",
                        already_searched=["RAG evaluation"])
        print(json.dumps(r2, ensure_ascii=False, indent=2))
