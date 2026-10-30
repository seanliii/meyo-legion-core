"""v13.87.12 · 卡住自动搜+记住

用户 2026-07-22:「不懂知道监督·然后去搜索了吗·然后记住·下次直接搜」

三步闭环:
1. detect_stuck(goal, ctx) → 判断是否卡住 (信号: 0 hit / low completeness / repeat_query)
2. auto_research(goal) → 触发 deep_research 拉 evidence
3. remember(goal, evidence, urls) → 落 skill_bridge · 下次相似 goal 命中

对齐 memory:
- [[feedback-be-proactive-research-tools.md]] "卡住必须主动搜第三方"
- [[shrimp-v13.79-skill-forge-bridge.md]] skill_bridge 现役 · 复用

不建独立数据库 · 复用 skill_bridge + search_skill_forger
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

STUCK_LOG = V11 / ".stuck_events.jsonl"


def _log(record: dict) -> None:
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    record.setdefault("unix_ts", int(time.time()))
    try:
        with open(STUCK_LOG, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def detect_stuck(goal: str, recent_hits: list = None,
                 repeat_query_count: int = 0,
                 error_count: int = 0) -> dict:
    """轻量 stuck 判定 (不用 LLM)
    信号:
    - hits 全 shell (骨架 · 内容不够)
    - hits 空 (0 结果)
    - 同 query 3+ 次重复 (轮空)
    - fail_rate > 50%
    """
    hits = recent_hits or []
    reasons = []
    is_stuck = False

    if not hits:
        reasons.append("0_hits")
        is_stuck = True
    else:
        shells = sum(1 for h in hits
                     if isinstance(h, dict)
                     and h.get("completeness") == "shell")
        if shells == len(hits) and len(hits) >= 3:
            reasons.append("all_shell")
            is_stuck = True

    if repeat_query_count >= 3:
        reasons.append(f"repeat_query:{repeat_query_count}")
        is_stuck = True

    if error_count >= 3:
        reasons.append(f"error:{error_count}")
        is_stuck = True

    return {"stuck": is_stuck, "reasons": reasons,
            "goal": goal[:200]}


def find_previous_solution(goal: str) -> list:
    """v13.79 skill_bridge + skill_forger 已存的复用
    - 先查 skill_bridge (完整 skill)
    - 后查 search_skill_forger (source 建议)
    """
    result = {"reusable_skills": [], "suggested_sources": []}
    try:
        from skill_bridge import find_reusable_skill
        r = find_reusable_skill(goal)
        if r and r.get("reusable"):
            result["reusable_skills"] = r.get("candidates") or []
    except Exception:
        pass
    try:
        from search_skill_forger import suggest_sources
        s = suggest_sources(goal, top_k=3, min_samples=2)
        if s:
            result["suggested_sources"] = s
    except Exception:
        pass
    return result


def auto_research(goal: str, context: str = "",
                   exhaustive: bool = False) -> dict:
    """触发 deep_research 主流水线拉 evidence
    - 复用现役 agent_search.deep_research
    - exhaustive=True 走穷尽 5 轮
    """
    t0 = time.time()
    try:
        sys.path.insert(0, str(V11))
        from agent_search import AgentSearch
        sa = AgentSearch()
        result = sa.deep_research(role="general_agent", topic=goal,
                                    exhaustive=exhaustive)
        elapsed = round(time.time() - t0, 1)
        summary = {
            "ok": True,
            "goal": goal[:200],
            "elapsed_s": elapsed,
            "evidence_count": len(result.get("evidence", [])),
            "queries_used": result.get("queries", []),
        }
        _log({"type": "auto_research", **summary})
        return {**summary, "full_result": result}
    except Exception as e:
        _log({"type": "auto_research_err",
              "goal": goal[:200], "err": str(e)[:200]})
        return {"ok": False, "err": str(e)[:200], "goal": goal[:200]}


def browser_fallback(goal: str, max_steps: int = 12) -> dict:
    """v13.88.9L-18 · 最后兜底: 常规搜索全失败 → 升级到 InteractiveBrowserAgent
    用浏览器真交互(输入框输入/点击/滚动/详情页·goal驱动loop)去找。
    根因(体检查实·接线断): interactive_browser_agent.run_goal 代码完整(observe→
    decide→act→check→reflect 五段loop)·但全仓仅 search_supervisor 一行注释提及·
    没任何模块 import 调用 → 最强能力空转。这里接进 stuck 兜底=它的天然位置
    (常规HTTP/API搜索全失败·才值得开浏览器真交互·load贵)。
    fail-safe: import/异常/CDP不可用一律返回 ok=False·不阻塞。
    """
    t0 = time.time()
    try:
        sys.path.insert(0, str(V11 / "evolution"))
        from interactive_browser_agent import run_goal
        r = run_goal(goal, max_steps=max_steps)
        elapsed = round(time.time() - t0, 1)
        # run_goal 返回结构容错取 (可能是 dict 或有 .get)
        found = r.get("findings") or r.get("results") or r.get("extracted") or [] \
            if isinstance(r, dict) else []
        summary = {
            "ok": bool(found),
            "goal": goal[:200],
            "elapsed_s": elapsed,
            "found_count": len(found) if hasattr(found, "__len__") else 0,
            "via": "interactive_browser_agent",
        }
        _log({"type": "browser_fallback", **summary})
        return {**summary, "full_result": r}
    except Exception as e:
        _log({"type": "browser_fallback_err",
              "goal": goal[:200], "err": str(e)[:200]})
        return {"ok": False, "err": str(e)[:200], "goal": goal[:200],
                "via": "interactive_browser_agent"}


def remember(goal: str, urls: list, notes: str = "") -> dict:
    """落 skill_bridge · 下次相似 goal 命中"""
    try:
        from skill_bridge import on_session_done
        # 用简单 KB 结构 · 塞 goal 和 urls
        kb = {"goal": goal, "urls": urls[:20], "notes": notes,
              "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        # 直接写 KB 文件让 skill_bridge 消费
        kb_dir = V11 / "research_kb"
        kb_dir.mkdir(exist_ok=True)
        sid = f"stuck_{int(time.time())}"
        kb_path = kb_dir / f"{sid}.json"
        with open(kb_path, "w") as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
        r = on_session_done(sid, str(kb_path))
        _log({"type": "remember", "goal": goal[:200],
              "kb_path": str(kb_path), "forge_result": r})
        return r
    except Exception as e:
        _log({"type": "remember_err",
              "goal": goal[:200], "err": str(e)[:200]})
        return {"ok": False, "err": str(e)[:200]}


def close_loop(goal: str, recent_hits: list = None,
                context: str = "", exhaustive: bool = False) -> dict:
    """完整闭环:
    1. 判断卡住?
    2. 卡住 → 先查历史复用
    3. 复用没·auto_research 深搜
    4. 拿到结果 → remember
    """
    stuck = detect_stuck(goal, recent_hits=recent_hits)
    if not stuck["stuck"]:
        return {"action": "no_stuck", **stuck}

    _log({"type": "stuck_detected", **stuck})

    # 先查历史
    prev = find_previous_solution(goal)
    if prev["reusable_skills"] or prev["suggested_sources"]:
        _log({"type": "reused", "goal": goal[:200], **prev})
        return {"action": "reused_previous",
                "stuck_reasons": stuck["reasons"], **prev}

    # 深搜
    research = auto_research(goal, context, exhaustive=exhaustive)
    # v13.88.9L-18 · 深搜失败 or 0证据 → 升级 InteractiveBrowserAgent 浏览器真交互兜底
    #   (接线修复: 最强能力原来空转·常规搜索全失败才开浏览器·load贵值得)
    if not research.get("ok") or research.get("evidence_count", 0) == 0:
        _log({"type": "escalate_to_browser", "goal": goal[:200],
              "reason": "research_failed" if not research.get("ok") else "0_evidence"})
        browser = browser_fallback(goal)
        if browser.get("ok"):
            return {"action": "browser_fallback_ok",
                    "stuck_reasons": stuck["reasons"],
                    "found_count": browser.get("found_count"),
                    "via": "interactive_browser_agent"}
        # 浏览器也没拿到 → 才算真失败
        if not research.get("ok"):
            return {"action": "research_and_browser_failed",
                    "research": research, "browser": browser}

    # 记住
    full = research.get("full_result", {})
    urls = []
    for ev in (full.get("evidence") or [])[:20]:
        u = ev.get("url", "") if isinstance(ev, dict) else ""
        if u:
            urls.append(u)
    memory = remember(goal, urls, notes=context[:500])

    return {"action": "researched_and_remembered",
            "stuck_reasons": stuck["reasons"],
            "evidence_count": research.get("evidence_count"),
            "urls_saved": len(urls),
            "memory": memory}


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["detect", "loop", "find", "stats"])
        p.add_argument("--goal", default="")
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "detect":
        print(json.dumps(detect_stuck(args.goal), ensure_ascii=False, indent=2))
    elif args.cmd == "find":
        print(json.dumps(find_previous_solution(args.goal),
                          ensure_ascii=False, indent=2))
    elif args.cmd == "loop":
        r = close_loop(args.goal, recent_hits=[])  # 无 hits → 判 stuck
        print(json.dumps({k: v for k, v in r.items() if k != "full_result"},
                          ensure_ascii=False, indent=2))
    elif args.cmd == "stats":
        # 读 STUCK_LOG 汇总
        if not STUCK_LOG.exists():
            print("no stuck events yet"); sys.exit(0)
        events = []
        with open(STUCK_LOG) as f:
            for line in f:
                try:
                    events.append(json.loads(line.strip()))
                except:
                    continue
        by_type = {}
        for e in events:
            t = e.get("type", "?")
            by_type[t] = by_type.get(t, 0) + 1
        print(json.dumps({"total": len(events), "by_type": by_type},
                          ensure_ascii=False, indent=2))
