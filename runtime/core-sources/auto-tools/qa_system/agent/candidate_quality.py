# -*- coding: utf-8 -*-
"""candidate_quality · 候选实体卫生层

只做两件事：
1. 技术清洗：空值、过长、明显 JSON/URL/堆栈片段不进入候选。
2. 语义判断：候选是否只是题面变量/描述词，交给当前上下文的 llm_call / SoulDriver。

禁止在这里写单题实体、抽象关系词表或语义停用词表。模型能按上下文理解的，
不要写死进规则。fail-safe: 没有 llm_call 时只做技术清洗，不做语义过滤。
"""
from __future__ import annotations

import json
import re


_TECHNICAL_NOISE = ("{", "}", "\n", "\r", "\t", "traceback", "httpconnectionpool")

# 内部 prompt/契约 schema 字段名 —— 模型偶尔把自己 prompt 里的 JSON 键当答案输出
# (2026-08-24 公平实证:SoulDriver 把 answer-contract 的 "disambiguation_keys" 当候选)。
# 这是技术噪音(工程标识符),非题目语义词表,直接拒。
_SCHEMA_KEY_NOISE = frozenset({
    "disambiguation_keys", "gold_method_prior", "grading", "precision",
    "units", "format", "exact_match", "tolerance", "llm_judge",
    "type", "value", "candidate_id", "evidence_ids", "unresolved_constraints",
})


def normalize_candidate(candidate: str) -> str:
    """Normalize surface form without changing identity. Fail-safe -> ''."""
    try:
        c = str(candidate or "").strip().strip('"“”‘’')
        c = re.sub(r"^(?:entity|candidate|answer)\s*[:：]\s*", "", c, flags=re.I)
        c = re.sub(r"\s+", " ", c)
        return c.strip(" .,:;；")
    except Exception:
        return ""


def is_technical_noise(candidate: str) -> bool:
    """Only reject machine-noise strings, not semantic categories."""
    c = normalize_candidate(candidate)
    if not c or len(c) > 120:
        return True
    low = c.lower()
    if low in _SCHEMA_KEY_NOISE:
        return True
    if low.startswith(("http://", "https://")):
        return True
    if any(mark in low for mark in _TECHNICAL_NOISE):
        return True
    return False


def judge_candidate(question: str, candidate: str, *, evidence=None, llm_call=None) -> dict:
    """Ask the current context model whether candidate is a concrete answer entity.

    Returns {keep: bool, reason: str}. No hardcoded semantic word lists.
    """
    cand = normalize_candidate(candidate)
    if is_technical_noise(cand):
        return {"keep": False, "reason": "technical_noise"}
    if llm_call is None:
        return {"keep": True, "reason": "no_semantic_judge"}
    try:
        # 证据窗口从 8 条放宽到 24 条:恢复态 evidence 前几条常是 plan/triage,
        # 真正含 gold 的 tool_log summary/entity_bank 排在后面(实证 a7feb=31·e8cb=shrimp
        # gold 在 tool_log 出现却因判官只看前 8 条看不到→候选被误拒)。放宽让判官看得到。
        ev = "\n".join(
            (str(x.get("title") or "") + " — " + str(x.get("text") or x.get("content") or "")[:240])
            for x in (evidence or [])[:24] if isinstance(x, dict)
        )
        prompt = f"""判断候选是否可以作为题目的【具体答案实体】，而不是题面变量、普通类别词、工具噪声或占位描述。
只根据当前 question/evidence 语义判断，禁止按固定词表判断。

Question: {str(question)[:1200]}
Candidate: {cand}
Evidence snippets:
{ev[:2500]}

输出严格 JSON: {{"keep": true/false, "reason": "一句话"}}
"""
        raw = llm_call(prompt)
        m = re.search(r"\{[\s\S]*\}", str(raw or ""))
        data = json.loads(m.group(0)) if m else {}
        return {"keep": bool(data.get("keep", True)), "reason": str(data.get("reason") or "semantic_judge")[:160]}
    except Exception:
        return {"keep": True, "reason": "judge_failed_failsafe"}


def filter_candidates(candidates, question: str = "", *, evidence=None, llm_call=None, max_cand: int | None = None):
    """Normalize, de-dup, and optionally use SoulDriver/LLM semantic judgment."""
    out = []
    seen = set()
    for cand in candidates or []:
        norm = normalize_candidate(cand)
        key = norm.lower()
        if not norm or key in seen:
            continue
        verdict = judge_candidate(question, norm, evidence=evidence, llm_call=llm_call)
        if not verdict.get("keep", True):
            continue
        seen.add(key)
        out.append(norm)
        if max_cand and len(out) >= max_cand:
            break
    return out
