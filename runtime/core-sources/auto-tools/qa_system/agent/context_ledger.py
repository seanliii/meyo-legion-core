# -*- coding: utf-8 -*-
"""context_ledger · 统一分类 Context 层(2026-08-26)。

用户理念:给 context 不加 control。把 auto session 里散落的 21 个 state 字段,
按 5 个稳定类别渲染成【模型可见的结构化全局状态】,让模型看到"搜过哪些方向、
锁过哪些候选、排除过什么、有哪些冲突证据、哪些路径失败了"——而不是零散、看不见。

设计对照(源码证):
- DeepSeek context 按类别插件注入(packages/context/README.zh.md:session-reference/
  file-reference/time-context 各是一类 ctx provider);
- DeepSeek compaction 按 region/span 分区保留+压缩(compaction-basic/src/region.ts)。

本模块只【读】session 渲染,不改任何写入点(低风险渐进)。挂在 native/auto 两路径的
inject 点(仿 perspective_engine.inject),fail-safe:异常→空串,绝不拖累主流程。

AB 已证(00c):给"搜索路径全景"context,A臂(无)瞎猜2011❌ vs B臂(有)命中2001✅。
本模块把该模式系统化推广到全部 5 类状态。

env:
  MEYO_CONTEXT_LEDGER=1        总开关(默认开)
  MEYO_CTX_<CLASS>=0           单类关闭,便于 AB(GOAL/JOURNEY/CANDIDATE/EVIDENCE/FAILURE)
  MEYO_CTX_BUDGET_CHARS=3000   总渲染预算(字符),超了按类压缩
"""
from __future__ import annotations

import os


def _enabled(cls: str = "") -> bool:
    if os.environ.get("MEYO_CONTEXT_LEDGER", "1") != "1":
        return False
    if cls and os.environ.get(f"MEYO_CTX_{cls}", "1") != "1":
        return False
    return True


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


# ── 5 类渲染器:每个读 session 的相关字段,返回一小段模型可见文本(空则返回 "") ──

def _render_goal(s: dict, budget: int) -> str:
    """1 目标与约束 ← triage 口径约束 + 缺失约束。"""
    triage = s.get("triage") or {}
    cons = triage.get("口径约束") or triage.get("constraints") or []
    missing = s.get("_missing_constraints") or []
    if not cons and not missing:
        return ""
    parts = []
    if cons:
        items = "; ".join(str(c)[:80] for c in (cons if isinstance(cons, list) else [cons])[:6])
        parts.append(f"约束: {items}")
    if missing:
        items = "; ".join(str(c)[:80] for c in missing[:4])
        parts.append(f"仍缺: {items}")
    return _clip("【目标约束】" + " | ".join(parts), budget)


def _render_journey(s: dict, budget: int) -> str:
    """2 全工具执行路径 ← _tool_journey(统一 chokepoint 记录·所有工具所有路径)。

    让模型看到自己完整做过什么(调了哪些工具/参数/结果·哪些空手),尤其反复空手时
    自己判断方向对不对——给 context 不加 control。取代旧 _search_journey(只搜索/接错层)。"""
    journey = s.get("_tool_journey") or []
    if len(journey) < 2:
        return ""
    empties = sum(1 for j in journey if isinstance(j, dict) and j.get("empty"))
    recent = "; ".join(
        f'{j.get("tool", "?")}("{_clip(str(j.get("arg", "")), 36)}")→{"空手" if j.get("empty") else "有果"}'
        for j in journey[-8:] if isinstance(j, dict))
    tail = ("\n→ 反复空手=当前方向/关键词偏了,换【题目独特约束词】做锚点或换竞争假设,别沿同方向撒网。"
            if empties >= 2 else "")
    return _clip(f"【已执行路径·共{len(journey)}步·空手{empties}次】{recent}{tail}", budget)


def _render_candidate(s: dict, budget: int) -> str:
    """3 候选与已排除 ← _candidate_hypotheses / _candidate_vault。"""
    hyp = s.get("_candidate_hypotheses") or []
    vault = s.get("_candidate_vault") or []
    cands = []
    for c in list(hyp)[:6] + list(vault)[:6]:
        if isinstance(c, dict):
            name = c.get("entity") or c.get("answer") or c.get("text") or ""
            st = c.get("status") or c.get("verdict") or ""
            if name:
                cands.append(f"{_clip(str(name), 40)}{f'[{st}]' if st else ''}")
        elif c:
            cands.append(_clip(str(c), 40))
    if not cands:
        return ""
    # 去重保序
    seen, uniq = set(), []
    for x in cands:
        if x not in seen:
            seen.add(x); uniq.append(x)
    return _clip("【候选与状态】" + "; ".join(uniq[:8]), budget)


def _render_evidence(s: dict, budget: int) -> str:
    """4 证据池 ← evidence(按信息量取 top 标题/要点)。"""
    ev = s.get("evidence") or []
    if not ev:
        return ""
    titles = []
    for e in ev:
        if isinstance(e, dict):
            t = e.get("title") or e.get("url") or ""
            if t:
                titles.append(_clip(str(t), 60))
        if len(titles) >= 8:
            break
    if not titles:
        return ""
    return _clip(f"【已有证据{len(ev)}条·要点】" + "; ".join(titles), budget)


def _render_failure(s: dict, budget: int) -> str:
    """5 失败路径与错误 ← _pilot_failed_paths(已证有效)。"""
    failed = s.get("_pilot_failed_paths") or []
    if not failed:
        return ""
    items = []
    for f in failed[-6:]:
        # 只取人话字段(path/reason/desc/query);内部事件对象(带 event_type/seq)不是给
        # 模型看的失败描述,跳过——否则塞原始 state_projected 事件污染 context。
        if isinstance(f, dict):
            if f.get("event_type") or ("seq" in f and "path" not in f and "reason" not in f):
                continue
            desc = f.get("path") or f.get("reason") or f.get("desc") or f.get("query") or ""
        else:
            desc = str(f)
        desc = desc.strip() if isinstance(desc, str) else str(desc)
        if desc:
            items.append(_clip(desc, 70))
    if not items:
        return ""
    return _clip("【已失败路径·别重走】" + "; ".join(items), budget)


_RENDERERS = [
    ("GOAL", _render_goal),
    ("JOURNEY", _render_journey),
    ("CANDIDATE", _render_candidate),
    ("EVIDENCE", _render_evidence),
    ("FAILURE", _render_failure),
]


def render(session: dict, budget_chars: int | None = None) -> str:
    """渲染统一分类 context。fail-safe:任何异常→空串。

    返回给模型的多段结构化状态,每类一段。总长受 budget 约束(按类均分)。
    """
    try:
        if not session or not _enabled():
            return ""
        if budget_chars is None:
            try:
                budget_chars = int(os.environ.get("MEYO_CTX_BUDGET_CHARS", "3000") or 3000)
            except Exception:
                budget_chars = 3000
        active = [(cls, fn) for cls, fn in _RENDERERS if _enabled(cls)]
        if not active:
            return ""
        per = max(200, budget_chars // len(active))
        blocks = []
        for cls, fn in active:
            try:
                seg = fn(session, per)
            except Exception:
                seg = ""
            if seg:
                blocks.append(seg)
        if not blocks:
            return ""
        return ("[当前全局状态·结构化(供你自主判断方向·不是命令)]\n"
                + "\n".join(blocks))
    except Exception:
        return ""


def inject(prompt: str, session: dict, budget_chars: int | None = None) -> str:
    """把分类 context 注入 prompt 末尾(仿 perspective_engine.inject)。fail-safe→原 prompt。"""
    try:
        block = render(session, budget_chars)
        if not block:
            return prompt
        return f"{prompt}\n\n{block}"
    except Exception:
        return prompt
