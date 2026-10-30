#!/usr/bin/env python3
"""v13.88.9 · 2026-07-28 · 搜索 query 净化闸
用户抓「你怎么能拿文件去搜索呢?不得搜问题吗?」+ 截图: Google 搜
  `correction evolver fid KYK2P4BV69HGTNN13` / `pytest test v13 backfill.py`
  → 全是无关 GitHub/PyPI 垃圾

病根: alert_bus 把内部告警日志原文当搜索词丢给 Google。

本闸做两件事:
  1. 判「这条告警是不是可搜的真外部知识问题」
     - 含 fid / hash / .py / 纯内部状态日志 → NOT searchable (不入队·归 harness)
     - 真外部知识问题 (竞品/工具/技术方案) → searchable
  2. searchable 的·抽成干净短 query (先 regex 快判·边界情况上 haiku)

设计原则 (对齐 memory):
  · [[feedback-llm-over-rules-for-semantics]] 语义判断用 LLM·不硬 regex
  · 但 regex 能秒杀的明显垃圾 (fid/.py) 先拦·省 LLM 调用
  · 不碰网络·纯判断+LLM·永不卡 CDP
"""
import re
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ── 明显不可搜的信号 (regex 秒杀·不用 LLM) ──
# fid: 01K 开头 26 位 ULID · hash · .py 文件名 · 纯状态数字
_UNSEARCHABLE_PATTERNS = [
    r"\bfid[=\s:]", r"\b01[A-Z0-9]{24}\b",          # fid / ULID
    r"\bhash[=\s:]", r"[a-f0-9]{16,}",               # hash
    r"\b\w+\.py\b",                                   # .py 文件名
    r"age=\d+\.?\d*h", r"view=\d+",                   # 纯状态数字
    r"heal ok \d+", r"fail \d+",                      # healer 状态
    r"漏派 \d+", r"漏改 \d+", r"监督介入",              # 内部演绎日志
    r"output_gate", r"意图偏离", r"意图对齐",           # 内部 gate 术语
]

# ── 内部组件名前缀 (告警 component·不是搜索词) ──
_INTERNAL_COMPONENTS = {
    "correction_evolver", "production_watchdog", "auto_healer",
    "search_quality_monitor", "alert_search_worker", "wiring_auditor",
    "supervisor_of_supervisors", "code_reach_audit", "data_flow_audit",
    "self_correction_watchdog", "e2e_business_audit", "perf_watchdog",
    "staleness_auto_healer", "monitor_hub", "proposal_consumption_audit",
    # v13.88.9 · 补漏 · 净化前这些组件的内部状态报警混进队列 (113+68 条历史垃圾)
    "resource_footprint", "output_deep_review", "output_reflect",
    "e2e_loop_selftest", "e2e_8ring_selftest", "deduce_hub",
    "self_harness_evolver", "orchestrator_watchdog", "pytest_test",
    "pytest_test_throttle", "pytest_all_pop_test", "deductive_findings",
    # v13.90 · 补漏 · fix_pool 的 bug 字段本质是内部告警/prompt片段(非真搜索需求)·
    #   之前漏在白名单外 → context 自动推断成"search"(宽松放行) → 「System 你是
    #   独立审稿」「用户消息 下面是一条要」这类 prompt 碎片当 query 狂搜(6h 269条)。
    "fix_pool",
}


def _regex_unsearchable(text: str) -> bool:
    """regex 秒判: 明显是内部日志/文件/状态·不可搜"""
    for pat in _UNSEARCHABLE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False


def _strip_component(component: str, msg: str) -> str:
    """去掉 component 前缀·只留告警正文"""
    m = msg.strip()
    if component and m.startswith(component):
        m = m[len(component):].strip()
    return m


def _llm_purify(text: str, context: str = "alert") -> dict:
    """边界情况·用 haiku 判 searchable + 抽 query
    返回 {"searchable": bool, "query": str, "reason": str}

    context 决定判据框架 (v13.88.9N · 用户 08-02 抓「SWE-bench/GAIA 搜 0 hits」):
      · "alert"  — 输入是系统告警日志·默认保守 (进程卡/文件没消费=内部状态·不搜)
      · "search" — 输入是主动搜索 query (search_agent 门面直调)·
                   默认它就是真问题·只拦真正的内部状态泄漏 (prompt原文/fid/日志)。
                   病根: 旧 prompt 一律当「告警」判·把「SWE-bench 多少道题」这类
                   客观事实问题误判成「系统内部状态」→ searchable=false → 返 [] 空搜。
                   事实类「X 有多少/全称/谁提出/怎么实现」是典型可搜外部知识·必须放行。
    """
    if context == "search":
        prompt = f"""下面是一条要拿去搜索引擎搜的 query。判断它是不是「能搜到有用外部知识」的真问题。

query 原文:
{text[:300]}

判断标准 (默认可搜·只拦真正的内部泄漏):
- 可搜 searchable=true (绝大多数): 询问客观事实/数据/概念/工具/技术方案。
  例: 「SWE-bench Verified 多少道题」「GAIA benchmark 一共多少题」「MCP 全称谁提出」
     「某数据集多少张图」「某模型多少层」——这些是公开事实·搜索引擎能查到·必须放行。
     ⚠ 「某 benchmark 有多少题/多少任务/多少参数」是外部公开知识·绝不是系统内部状态!
- 不可搜 searchable=false (少数·真泄漏才拦): query 明显是本系统内部运行状态或
  被误当搜索词的日志/prompt 原文·例: 含 fid=/hash/.py 文件名/「某进程卡了」
  /「某告警」/「你是独立审稿」这类对话或系统元描述。

若可搜·抽成干净短语 (去内部术语/口水词·留实体+数字+疑问核心·25 字内·保留英文工具名)。

只输出 JSON:
{{"searchable": true/false, "query": "干净搜索短语 或 空", "reason": "10 字理由"}}"""
    else:
        prompt = f"""判断下面这条系统告警·是不是「能拿去搜索引擎搜到有用外部知识」的真问题。

告警原文:
{text[:300]}

判断标准:
- 若是「系统内部状态」(某进程卡了·某文件没消费·某 hash·某 fid·测试没跑) → 不可搜·searchable=false
- 若是「需要外部知识才能解决」(某工具怎么选·某技术怎么实现·竞品有啥功能) → 可搜·searchable=true

若可搜·把它改写成一句干净的中文搜索短语 (去掉内部术语/文件名/组件名·只留真问题·15 字内)。

只输出 JSON:
{{"searchable": true/false, "query": "干净搜索短语 或 空", "reason": "10 字理由"}}"""
    try:
        # experience_writer/problem_planner 在 v11/ (evolution 的父目录)·需加 path
        _v11 = os.path.dirname(HERE)
        if _v11 not in sys.path:
            sys.path.insert(0, _v11)
        from problem_planner import _llm
        r = _llm(prompt, max_tokens=200)
        if isinstance(r, tuple):
            r = r[0]
        m = re.search(r"\{[\s\S]*\}", r or "")
        if not m:
            return {"searchable": False, "query": "", "reason": "llm_no_json"}
        d = json.loads(m.group(0))
        return {
            "searchable": bool(d.get("searchable", False)),
            "query": (d.get("query", "") or "").strip()[:60],
            "reason": d.get("reason", "")[:40],
        }
    except Exception as e:
        # LLM 挂·保守判不可搜 (宁可不搜·不搜垃圾)
        return {"searchable": False, "query": "", "reason": f"llm_err:{type(e).__name__}"}


def purify(component: str, msg: str, use_llm: bool = True,
           context: str = None) -> dict:
    """净化入口·判是否可搜 + 抽干净 query
    返回 {"searchable": bool, "query": str, "reason": str, "via": "regex"/"llm"}

    context (v13.88.9N):
      · "alert"  — 输入是告警日志 (alert_bus/fix_pool)·保守判 (默认给内部组件)
      · "search" — 输入是主动搜索 query (search_agent 门面)·默认可搜·只拦真泄漏
      · None     — 自动: component in 内部组件 → alert·否则 search
    """
    body = _strip_component(component, msg)

    # v13.88.9N · context 自动推断: 内部运维组件的输入按告警判·其余按搜索 query 判
    if context is None:
        context = "alert" if component in _INTERNAL_COMPONENTS else "search"

    # 1. component 本身是内部运维组件 → 报警一律走内部诊断·不搜外网
    # v13.88.9 · 原逻辑要求 body 也命中 regex 才拒·导致 deductive_findings>8h 这类漏到 LLM
    # 这些组件 (watchdog/reflect/selftest/footprint) 的报警本质是内部状态·外网搜不到解
    if component in _INTERNAL_COMPONENTS:
        return {"searchable": False, "query": "", "reason": "内部运维组件·走内部诊断", "via": "regex"}

    # 2. regex 秒杀明显垃圾
    if _regex_unsearchable(body):
        return {"searchable": False, "query": "", "reason": "含fid/文件/状态", "via": "regex"}

    # 3. 太短或空 → 不搜
    if len(body) < 6:
        return {"searchable": False, "query": "", "reason": "太短", "via": "regex"}

    # 4. 边界情况·上 LLM 判语义 (context 决定判据框架)
    if use_llm:
        r = _llm_purify(body, context=context)
        r["via"] = "llm"
        return r

    # 无 LLM 兜底·保守放行但用原文压短语
    return {"searchable": True, "query": body[:60], "reason": "no_llm_fallback", "via": "none"}


if __name__ == "__main__":
    # 自测: 拿真队列样本跑
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-queue", action="store_true", help="重放真队列判净化")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    if args.test_queue:
        from pathlib import Path
        V11 = Path(HERE).parent
        q = V11 / ".alert_search_queue.jsonl"
        seen = set()
        stats = {"searchable": 0, "unsearchable": 0, "dup": 0}
        for line in q.read_text().splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            comp = d.get("component", "")
            query = d.get("query", "")
            if query in seen:
                stats["dup"] += 1
                continue
            seen.add(query)
            r = purify(comp, query, use_llm=not args.no_llm)
            tag = "✅可搜" if r["searchable"] else "🚫不搜"
            stats["searchable" if r["searchable"] else "unsearchable"] += 1
            newq = f" → 「{r['query']}」" if r["searchable"] else ""
            print(f"{tag} [{r['via']}] {r['reason']}{newq}")
            print(f"      原: {query[:70]}")
        print(f"\n═══ 净化统计 ═══")
        print(f"  可搜: {stats['searchable']} · 不搜(拦垃圾): {stats['unsearchable']} · 去重: {stats['dup']}")
