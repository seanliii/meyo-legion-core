# -*- coding: utf-8 -*-
"""candidate_provider · candidate/search/judge provider boundary.

Unifies candidate extraction, hygiene, multi-hypothesis ranking and short-loop
advice. This is intentionally thin: it delegates scoring to existing
constraint_ledger / multi_hypothesis / verify_as_search, while exposing one
stable provider-style API for auto/native.
"""
from __future__ import annotations

import os


def _enabled():
    return os.environ.get("MEYO_CANDIDATE_PROVIDER", "1").strip().lower() not in ("0", "false", "off", "no")


def candidates(question, evidence, *, session=None, extra=None, llm_call=None, max_cand=8):
    if not _enabled():
        return []
    try:
        import multi_hypothesis as _mh
        return _mh.generate_candidates(
            question, evidence, session=session, extra=extra, llm_call=llm_call,
            max_cand=max_cand)
    except Exception:
        return []


def rank(question, evidence, *, session=None, claims=None, triage=None,
         extra=None, llm_call=None):
    if not _enabled():
        return {"best": None, "ranked": [], "advice": ""}
    try:
        import multi_hypothesis as _mh
        return _mh.select_best(
            question, evidence, session=session, claims=claims, triage=triage,
            extra=extra, llm_call=llm_call)
    except Exception:
        return {"best": None, "ranked": [], "advice": ""}


def verify_counter(candidate, question, evidence, *, constraints=None, search_fn=None,
                   scope=None, llm_call=None):
    if not _enabled() or not candidate:
        return {"verdict": "INSUFFICIENT_EVIDENCE", "reason": "disabled_or_empty"}
    try:
        import verify_as_search as _vas
        return _vas.verify_by_search(
            candidate, question, evidence, constraints=constraints, search_fn=search_fn,
            scope=scope, llm_call=llm_call)
    except Exception as exc:
        return {"verdict": "INSUFFICIENT_EVIDENCE", "reason": f"provider_error:{type(exc).__name__}"}


def short_loop_advice(question, evidence, *, session=None, candidate=""):
    """Return compact next-query advice: discriminative query -> evidence -> update."""
    try:
        import information_gain_planner as _ig
        failed = (session or {}).get("_failed_queries") if isinstance(session, dict) else None
        llm_call = (session or {}).get("_llm_call") if isinstance(session, dict) else None
        q = _ig.next_query(question, evidence=evidence, candidate=candidate, failed=failed, llm_call=llm_call)
        if q:
            return ("[短环路] 下一步只做一个判别性 query → 读证据 → 更新候选；"
                    f"建议 query: {q[:160]}。别并发撒网。")
    except Exception:
        pass
    return ""
