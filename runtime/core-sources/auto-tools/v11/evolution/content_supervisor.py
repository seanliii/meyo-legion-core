"""v13.87.15 · 通用内容监督器 · 3 场景 (search/reply/writer) 都问自己

用户 2026-07-22 抓正:「不是每次监督搜索是否主动问自己·还得加每次回复我的时候·
还有回复 agent 也需要在回复时候」

3 场景 · 一个 supervisor 统一判决:

    场景             触发点                  判决什么
    ─────           ─────                    ──────
    search          fanout 后                evidence 够回答 goal 吗
    reply           准备回复前              手上信息够写有价值回复吗
    writer (内容)   evidence 后 write 前     素材够写高质量帖吗
    self_reply      我 (Claude) 回复用户前   我理解用户 · 有硬数据 · 该多问吗

跟现有 search_supervisor 关系:
- search_supervisor 已在 fanout 尾接了 · 保留兼容
- 本文件抽通用 supervise_content · 3 场景都能调
- search_supervisor 内部也可以改用本文件的通用逻辑 (下次)

判决 5 件事:
- is_sufficient  · 信息够回答 goal 吗
- confidence     · 0-100
- missing        · 缺什么 (概念/数字/真实案例/最新时间)
- next_action    · stop / search_more / dig_deeper / ask_user
- next_query     · 建议下一步 query · 空则 action=stop 或 ask_user

判决维度按场景调:
- search: 覆盖 goal 的多角度 3+ 权威源
- reply: 有具体数字/踩坑/真跑证据·不是空泛点评
- writer: 有 3+ pain / 3+ pitfall / 2+ numbers · 支撑千字帖
- self_reply: 用户具体诉求已经理解·关键数据已确认

参考 memory:
- [[shrimp-v13.66-llm-judge]] LLM 独立 subprocess 干净上下文
- [[shrimp-v13.72-anti-facepalm-agent]] 3 层反证
- [[feedback-verify-before-claim]] 说话前必查证
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

SUPERVISOR_LOG = V11 / ".content_supervisor.jsonl"

# haiku 快 · 每次场景都跑
_MODEL = os.environ.get("MEYO_CONTENT_SUPERVISOR_MODEL", "haiku")

# 各场景专用 system prompt · 判决维度不同
_PROMPTS = {
    "search": """You are a search supervisor. Given (goal, hits), decide if evidence is enough.

Return JSON only:
{"is_sufficient": bool, "confidence": 0-100, "missing": "one line",
 "next_action": "stop|search_more|dig_deeper", "next_query": "or empty",
 "reason": "one line why"}

sufficient rules:
- factual goal + 2+ authoritative sources with concrete data → sufficient
- broad topic + 3+ full completeness diverse sources → sufficient
- quantitative goal (数字/收入/占比) + no concrete numbers → NOT sufficient
- experience goal (真实/踩坑) + only theoretical → NOT sufficient""",

    "reply": """You are a reply-quality supervisor. Given (post_context, my_evidence), \
judge if I have enough to write a valuable reply.

Return JSON only:
{"is_sufficient": bool, "confidence": 0-100, "missing": "one line",
 "next_action": "stop|search_more|dig_deeper|ask_user", "next_query": "or empty",
 "reason": "one line why"}

sufficient rules for reply:
- have concrete stdout/numbers/screenshot 真跑证据 → sufficient
- only paraphrase of original post (无自己数据) → NOT sufficient (太水)
- have external similar case for comparison → sufficient
- 空泛点评"很有启发/学到了"→ NOT sufficient (客套无价值)

next_action:
- search_more: 需外部同类经验做对比
- dig_deeper: 需真跑某个命令验证
- ask_user: 用户上下文缺关键信息
- stop: 素材够 · 可以写""",

    "writer": """You are a content-writer supervisor. Given (topic, evidence_pool), \
judge if evidence pool supports a 1000+ 字 quality post.

Return JSON only:
{"is_sufficient": bool, "confidence": 0-100, "missing": "one line",
 "next_action": "stop|search_more|dig_deeper", "next_query": "or empty",
 "reason": "one line why"}

sufficient rules for writer:
- 3+ pain points from real users → yes
- 3+ pitfalls/踩坑 with concrete detail → yes
- 2+ concrete numbers (百分比/时间/收入) → yes
- only concept/definition (无 3 者中任一) → NOT sufficient
- 老素材 (>30 days) 无近期案例 → NOT sufficient""",

    "self_reply": """You are a self-reply supervisor for Claude replying to a user. \
Given (user_message, my_context, my_draft_reply), judge if my reply is grounded.

Return JSON only:
{"is_sufficient": bool, "confidence": 0-100, "missing": "one line",
 "next_action": "stop|search_more|dig_deeper|ask_user", "next_query": "or empty",
 "reason": "one line why"}

sufficient rules for self reply:
- claim contains 具体版本/文件/数字 · 都有磁盘证据 → sufficient
- claim references memory · memory 真读过 → sufficient
- claim is inference · confidence 显式标 · 是低 → mark ok
- claim contains "已完成/已接线" but no wiring_auditor check → NOT sufficient
- have unstated assumption that could 打脸 → NOT sufficient · ask_user"""
}


def _log(record: dict) -> None:
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    record.setdefault("unix_ts", int(time.time()))
    try:
        with open(SUPERVISOR_LOG, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _summarize(items, max_items=5, item_key=None):
    """摘要 list · 给 LLM 看"""
    if not items:
        return "(empty)"
    lines = []
    for i, x in enumerate(items[:max_items]):
        if isinstance(x, dict):
            if item_key and item_key in x:
                lines.append(f"[{i+1}] {str(x[item_key])[:120]}")
            else:
                # 抽 title/url/content
                t = str(x.get("title", ""))[:60]
                c = str(x.get("content", ""))[:100].replace("\n", " ")
                lines.append(f"[{i+1}] {t} · {c}")
        else:
            lines.append(f"[{i+1}] {str(x)[:120]}")
    if len(items) > max_items:
        lines.append(f"  (还有 {len(items) - max_items} 略)")
    return "\n".join(lines)


def supervise_content(scenario: str, goal: str, evidence,
                       context: str = "", draft: str = "",
                       timeout: int = 20, llm_call=None) -> dict:
    """通用监督入口
    scenario: search / reply / writer / self_reply
    goal: 用户目标 / 帖子话题 / 用户消息
    evidence: hits list / evidence pool
    context: 场景相关上下文
    draft: writer/self_reply 场景的草稿
    """
    if scenario not in _PROMPTS:
        return {"is_sufficient": True, "confidence": 100,
                "next_action": "stop",
                "reason": f"unknown_scenario:{scenario}"}

    # v13.88.29 · 2026-07-27 · revert · 用户抓「15 词表薄反而降 supervisor 精度」

    prompt = _PROMPTS[scenario]

    # 组装 user_msg 按场景
    if scenario == "search":
        user_msg = (
            f"Goal: {goal[:500]}\n\n"
            f"Hits ({len(evidence or [])} total):\n"
            f"{_summarize(evidence)}\n\nJudge JSON:")
    elif scenario == "reply":
        user_msg = (
            f"Post to reply to:\n{context[:400]}\n\n"
            f"My evidence gathered ({len(evidence or [])} items):\n"
            f"{_summarize(evidence)}\n\nJudge JSON:")
    elif scenario == "writer":
        # evidence 可能是 dict {pain: [], pitfall: [], numbers: []}
        if isinstance(evidence, dict):
            summary_parts = []
            for k in ["pain", "pitfall", "numbers", "hits"]:
                if k in evidence:
                    v = evidence[k]
                    summary_parts.append(f"{k} ({len(v)}):\n"
                                          + _summarize(v, max_items=3))
            ev_str = "\n\n".join(summary_parts)
        else:
            ev_str = _summarize(evidence)
        user_msg = (
            f"Topic: {goal[:300]}\n\n"
            f"Evidence pool:\n{ev_str}\n\nJudge JSON:")
    elif scenario == "self_reply":
        user_msg = (
            f"User message:\n{goal[:500]}\n\n"
            f"My context (memories/files/data checked):\n{context[:1500]}\n\n"
            f"My draft reply:\n{draft[:2000]}\n\nJudge JSON:")

    t0 = time.time()
    try:
        from audit_core import call_llm, try_parse_json
        # 调用方可注入统一网关，避免 QA 场景被本机 Claude CLI 登录态卡住。
        caller = llm_call or call_llm
        r = caller(prompt, user_msg, model_override=_MODEL,
                   timeout=timeout)
        elapsed = round(time.time() - t0, 2)
        if r.get("ok"):
            _ev_n = (len(evidence) if isinstance(evidence, list)
                     else len(evidence or {}))
            parsed = try_parse_json(r.get("raw", ""), default={
                "is_sufficient": _ev_n >= 5,
                "confidence": 40 if _ev_n < 5 else 55,
                "next_action": "search_more" if _ev_n < 5 else "stop",
                "reason": "no_json_fallback",
            })
            # v13.87.46 · conf<65 硬拉 sufficient=False
            _conf = parsed.get("confidence", 0)
            if _conf < 65 and parsed.get("is_sufficient"):
                parsed["is_sufficient"] = False
                parsed["reason"] = f"{parsed.get('reason','')}|conf<65_downgrade"
                if parsed.get("next_action") == "stop":
                    parsed["next_action"] = "search_more"
            # v13.88.9 · 用户抓「is_sufficient 永远 False 就重搜同一 goal·续费搜几十遍」
            if scenario == "search":
                parsed = _apply_loop_break(goal, parsed)
            parsed["_elapsed_s"] = elapsed
            parsed["_scenario"] = scenario
            _log({
                "scenario": scenario,
                "goal": (goal or "")[:200],
                "evidence_count": (len(evidence) if isinstance(evidence, list)
                                    else len(evidence or {})),
                "decision": parsed,
            })
            return parsed
    except Exception as e:
        _log({"scenario": scenario, "err": str(e)[:200],
              "elapsed_s": round(time.time() - t0, 2)})

    # v13.87.46 · LLM 挂 · ≥5 evidence 才判 sufficient
    _ev_n = (len(evidence) if isinstance(evidence, list)
             else len(evidence or {}))
    _enough = _ev_n >= 5
    parsed = {
        "is_sufficient": _enough,
        "confidence": 55 if _enough else 30,
        "next_action": "stop" if _enough else "search_more",
        "reason": "llm_err",
        "_elapsed_s": round(time.time() - t0, 2),
        "_scenario": scenario,
    }
    # v13.88.9 · LLM 挂时最易死循环 (永远 search_more)·loop_break 也覆盖 fallback
    if scenario == "search":
        parsed = _apply_loop_break(goal, parsed)
    return parsed


def _apply_loop_break(goal: str, parsed: dict) -> dict:
    """v13.88.9 · 同一 goal 已判不够重搜 >= N 次 → 强制 switch_topic·不原地重来"""
    try:
        import sys as _sys, os as _os
        _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
        from search_loop_guard import loop_break
        lb = loop_break(goal, parsed.get("is_sufficient", False),
                        parsed.get("next_action", ""))
        if lb.get("force_stop"):
            parsed["next_action"] = lb["override_action"]
            parsed["is_sufficient"] = False
            parsed["reason"] = f"{parsed.get('reason','')}|{lb['reason']}"
            parsed["_loop_break"] = True
    except Exception:
        pass
    return parsed


def stats(days: int = 7) -> dict:
    """看 4 场景决策统计 · 供 daily_report 消费"""
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

    if not records:
        return {"total": 0, "days": days}

    by_scenario = {}
    for r in records:
        s = r.get("scenario", "?")
        d = r.get("decision", {})
        by_scenario.setdefault(s, {"count": 0, "sufficient": 0,
                                     "actions": {}})
        by_scenario[s]["count"] += 1
        if d.get("is_sufficient"):
            by_scenario[s]["sufficient"] += 1
        a = d.get("next_action", "?")
        by_scenario[s]["actions"][a] = \
            by_scenario[s]["actions"].get(a, 0) + 1

    # 计算 sufficient rate
    summary = {}
    for s, v in by_scenario.items():
        summary[s] = {
            "count": v["count"],
            "sufficient_rate": round(v["sufficient"] / v["count"], 3),
            "actions": v["actions"],
        }
    return {"total": len(records), "days": days,
            "by_scenario": summary}


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["judge", "stats", "smoke"])
        p.add_argument("--scenario", default="reply",
                         choices=["search", "reply", "writer", "self_reply"])
        p.add_argument("--goal", default="chatgpt 副业月入")
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=2))
    elif args.cmd == "smoke":
        # 4 场景冒烟
        # 1 · search
        r1 = supervise_content("search", "如何评估 RAG 系统",
            [{"title": "RAG 综述", "content": "recall MRR " * 100,
              "completeness": "full"}])
        print("[search]", json.dumps(r1, ensure_ascii=False)[:200])

        # 2 · reply · 手上有真跑 stdout
        r2 = supervise_content("reply",
            goal="回复楼主关于 RAG 评估",
            evidence=[{"title": "我真跑 recall@5=0.82",
                        "content": "跑 200 题 · Hit=0.82 MRR=0.68"}],
            context="楼主问：我该用什么指标评估 RAG")
        print("[reply-yes]", json.dumps(r2, ensure_ascii=False)[:200])

        # 3 · reply · 只有客套没真数据
        r3 = supervise_content("reply",
            goal="回复", evidence=[], context="楼主问评估")
        print("[reply-no]", json.dumps(r3, ensure_ascii=False)[:200])

        # 4 · writer
        r4 = supervise_content("writer", goal="AI 副业变现",
            evidence={"pain": ["低价卷"], "pitfall": [], "numbers": []})
        print("[writer-thin]", json.dumps(r4, ensure_ascii=False)[:200])
    elif args.cmd == "judge":
        r = supervise_content(args.scenario, args.goal, evidence=[])
        print(json.dumps(r, ensure_ascii=False, indent=2))
