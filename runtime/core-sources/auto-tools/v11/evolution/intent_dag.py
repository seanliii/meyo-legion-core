"""v13.87.45 · intent_dag · 意图理解 DAG 编排 (并行 sub-analyzer)

用户 2026-07-23 抓:
「search agent 能不能在意图理解时候 graph 编排·并行多 query 理解·
 复杂意图场景分流+跳步+选择」

── 病 ──
现役 search_planner · 单 LLM 判所有 (haiku 8s):
- 一次判 intent 5 类
- 一次挑 sources_ordered
- 一次判 depth_score
- 不分流·不并行·不跳步

例:「我副业月入 5k 想扩 5w」被判 social_signal · 但真拆:
- what → 我做啥副业? (需知)
- pain → 我卡哪? (获客/复购/定价)
- constraint → 时间+预算?
- goal → 5w 是每月?一次?

单 planner 判不出这些复杂拆分 · 一 shot 全靠命。

── 现在 ──
IntentDAG · 复杂 query 触发并行 sub-analyzer:

           ┌─→ what_analyzer  (query 是啥·主题/领域)
query  ────┼─→ pain_analyzer  (卡在哪·核心 pain)
           ├─→ constraint_analyzer (约束·时间/预算/能力)
           └─→ goal_analyzer  (目标·数字/期望/终态)
                  ↓
              merger (合并 · 生成 SearchPlan)

3 层触发:
- fast · query 短·直接 planner (不 DAG)
- balanced · query 中长 · what+pain 并行
- deep · query 复杂/多维 · 4 sub-analyzer 全上

结果 · 更精准 intent + 分流 sources
"""
import concurrent.futures
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
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

INTENT_DAG_LOG = os.path.join(V11, ".intent_dag_runs.jsonl")

_MODEL = "haiku"


@dataclass
class SubAnalysis:
    """一个 sub-analyzer 的产物"""
    dimension: str  # what / pain / constraint / goal
    finding: str    # 抽出的关键信息
    keywords: list = field(default_factory=list)
    confidence: int = 50
    elapsed_s: float = 0.0


def _log(record: dict):
    record.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    try:
        with open(INTENT_DAG_LOG, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ═════ 4 sub-analyzer · 各自 LLM 独立并行 ═════

_SYSTEM_ANALYZERS = {
    "what": """You analyze a query · extract WHAT it is about.
Return JSON: {"topic": "领域/主题", "keywords": ["关键词1","2","3"], "confidence": 0-100}""",

    "pain": """You analyze a query · extract THE PAIN behind it.
Return JSON: {"pain": "核心 pain (一句话)", "category": "获客|复购|定价|技术|其他", "confidence": 0-100}""",

    "constraint": """You analyze a query · extract CONSTRAINTS.
Return JSON: {"time": "时间限制", "budget": "预算", "skill_gap": "能力缺", "confidence": 0-100}""",

    "goal": """You analyze a query · extract THE GOAL.
Return JSON: {"target": "目标·带数字", "timeframe": "何时·周/月/年", "success_criteria": "怎么算成", "confidence": 0-100}""",
}


def _run_analyzer(dimension: str, query: str,
                    timeout: int = 12) -> SubAnalysis:
    """一个 sub-analyzer 独立跑"""
    t0 = time.time()
    system = _SYSTEM_ANALYZERS.get(dimension, "")
    if not system:
        return SubAnalysis(dimension=dimension, finding="",
                             elapsed_s=round(time.time() - t0, 2))
    try:
        from audit_core import call_llm, try_parse_json
        r = call_llm(system, f"Query: {query[:300]}\n\nJSON:",
                      model_override=_MODEL, timeout=timeout)
        if r.get("ok"):
            parsed = try_parse_json(r.get("raw", ""), default={})
            # 汇总 · dim 相关字段 → finding
            finding_parts = []
            keywords = []
            for k, v in parsed.items():
                if k == "confidence":
                    continue
                if k == "keywords":
                    if isinstance(v, list):
                        keywords.extend(v)
                    continue
                if isinstance(v, str) and v:
                    finding_parts.append(f"{k}={v}")
                elif isinstance(v, list):
                    finding_parts.append(f"{k}={','.join(str(x) for x in v[:3])}")
            return SubAnalysis(
                dimension=dimension,
                finding="; ".join(finding_parts),
                keywords=keywords,
                confidence=parsed.get("confidence", 50),
                elapsed_s=round(time.time() - t0, 2),
            )
    except Exception:
        pass
    return SubAnalysis(dimension=dimension, finding="",
                         elapsed_s=round(time.time() - t0, 2))


def _is_complex_query(query: str) -> str:
    """判 query 复杂度: fast / balanced / deep"""
    if not query:
        return "fast"
    n = len(query)
    if n < 20:
        return "fast"
    # 复杂信号 · 数字+意图动词+对比
    signals = 0
    if re.search(r"\d", query):
        signals += 1
    if re.search(r"(想|要|如何|怎么|为啥|扩|提升|降|从|到)", query):
        signals += 1
    if re.search(r"(vs|对比|比较|差别|哪个更|还是)", query):
        signals += 1
    if len(query) > 50:
        signals += 1
    if signals >= 3:
        return "deep"
    if signals >= 2:
        return "balanced"
    return "fast"


def analyze(query: str, mode: str = "auto") -> dict:
    """并行跑 sub-analyzer · 合并 · 返 intent 视图

    mode:
      auto · _is_complex_query 判
      fast · 不走 DAG · 直调老 planner
      balanced · what + pain 2 并行
      deep · 4 sub 全上
    """
    t0 = time.time()
    if mode == "auto":
        mode = _is_complex_query(query)

    result = {
        "query": query[:200],
        "mode": mode,
        "sub_analyses": [],
        "merged_intent": {},
        "elapsed_s": 0,
    }

    if mode == "fast":
        # 不走 DAG · 直调老 planner
        try:
            from search_planner import plan as _plan
            p = _plan(query, use_llm=True, use_memory=True, timeout=15)
            result["merged_intent"] = asdict(p)
            result["elapsed_s"] = round(time.time() - t0, 2)
            _log({"type": "intent_dag_fast", **result})
            return result
        except Exception:
            pass

    # balanced / deep · 并行
    dims_to_run = ["what", "pain"] if mode == "balanced" \
                    else ["what", "pain", "constraint", "goal"]
    subs = []
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(dims_to_run)) as ex:
        futs = {ex.submit(_run_analyzer, d, query): d
                for d in dims_to_run}
        for f in concurrent.futures.as_completed(futs, timeout=30):
            try:
                s = f.result()
                subs.append(s)
            except Exception:
                pass

    result["sub_analyses"] = [asdict(s) for s in subs]

    # merge · 合并成 SearchPlan
    try:
        from search_planner import plan as _plan
        # 把 sub findings 加进 query 给 planner 用作 hint
        subs_hint = " | ".join(f"[{s.dimension}] {s.finding}"
                                for s in subs if s.finding)
        enriched = f"{query}\n\n[sub-analysis]\n{subs_hint}" if subs_hint else query
        p = _plan(enriched, use_llm=True, use_memory=True, timeout=15)
        result["merged_intent"] = asdict(p)
    except Exception as e:
        result["merge_err"] = str(e)[:100]

    result["elapsed_s"] = round(time.time() - t0, 2)
    _log({"type": f"intent_dag_{mode}", **result})
    return result


if __name__ == "__main__":
    try:
        import argparse
        p = argparse.ArgumentParser()
        p.add_argument("cmd", choices=["analyze", "smoke"])
        p.add_argument("query", nargs="?", default="我副业月入 5k 想扩到 5w 怎么办")
        p.add_argument("--mode", default="auto",
                        choices=["auto", "fast", "balanced", "deep"])
        args = p.parse_args()
    except Exception as _e:
        import traceback as _tb
        _alert("CRITICAL", f"{__file__.split('/')[-1]} 挂: {type(_e).__name__}: {str(_e)[:150]}")
        _tb.print_exc()
        raise

    if args.cmd == "analyze":
        r = analyze(args.query, mode=args.mode)
        print(json.dumps(r, ensure_ascii=False, indent=2)[:3000])
    elif args.cmd == "smoke":
        tests = [
            ("RAG 是什么", "fast"),
            ("2026 AI 副业趋势 有哪些", "balanced"),
            ("我副业月入 5k 想扩到 5w 从时间投入 8h/w 增到 20h/w 怎么破", "deep"),
        ]
        for q, expected in tests:
            r = analyze(q, mode="auto")
            mode = r.get("mode")
            mark = "✅" if mode == expected else f"⚠️ 预期 {expected}"
            print(f"[{mode:<10}] {mark} · {q[:60]}")
            for s in r.get("sub_analyses", []):
                print(f"    · [{s['dimension']}] {s['finding'][:100]}")
            mi = r.get("merged_intent", {})
            print(f"    → intent={mi.get('intent')} sources={mi.get('sources_ordered',[])[:3]}")
            print()
