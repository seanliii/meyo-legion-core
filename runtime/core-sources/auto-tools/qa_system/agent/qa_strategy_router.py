#!/usr/bin/env python3
"""Question-structure router for retrieval and verification strategies."""
from __future__ import annotations

import re
from typing import Any

SIMPLE_FACT = "simple_fact"
INVERTED_ENTITY = "inverted_multi_constraint_entity"
MULTI_HOP = "multi_hop_reasoning"
OPEN_RESEARCH = "open_research"
FRESHNESS = "freshness_time_anchored"
GENERAL = "general_search"

_STRATEGIES = {
    SIMPLE_FACT: {
        "retrieval": "single_search_extract", "max_search_ratio": 0.20,
        "verify_reserve_ratio": 0.20, "fallback": GENERAL,
    },
    INVERTED_ENTITY: {
        "retrieval": "candidate_first_constraint_matrix", "max_search_ratio": 0.55,
        "verify_reserve_ratio": 0.30, "fallback": GENERAL,
    },
    MULTI_HOP: {
        "retrieval": "interleaved_retrieval_reasoning", "max_search_ratio": 0.60,
        "verify_reserve_ratio": 0.25, "fallback": GENERAL,
    },
    OPEN_RESEARCH: {
        "retrieval": "evidence_first_iterative_deepening", "max_search_ratio": 0.75,
        "verify_reserve_ratio": 0.15, "fallback": GENERAL,
    },
    FRESHNESS: {
        "retrieval": "time_anchored_freshness_first", "max_search_ratio": 0.60,
        "verify_reserve_ratio": 0.25, "fallback": GENERAL,
    },
    GENERAL: {
        "retrieval": "parallel_search", "max_search_ratio": 0.60,
        "verify_reserve_ratio": 0.25, "fallback": GENERAL,
    },
}


def route(question: str, *, claims: list[dict[str, Any]] | None = None,
          triage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Route by question structure, never by benchmark/domain-specific answers."""
    text = str(question or "").strip()
    low = text.casefold()
    triage = triage or {}
    claims = list(claims or [])
    route_name = GENERAL
    reason = "fallback_general"

    # Freshness means the answer must reflect the present/latest state, not merely any time word.
    # Narrative past references ("last year I visited ...") are NOT freshness — matching them
    # misroutes inverted-entity questions. Require present/latest intent words.
    freshness = bool(re.search(
        r"(?i)\b(?:today|currently|latest|most recent|as of (?:today|now|this)|"
        r"right now|newest|up to date|up-to-date)\b", text))
    open_research = bool(re.search(
        r"(?i)\b(?:compare|survey|overview|landscape|pros and cons|research report|"
        r"best practices|state of the art|comprehensive)\b", text))
    multi_hop = bool(re.search(
        r"(?i)\b(?:whose|who then|which then|the .* that .* who|after .* before|"
        r"connected to|related to .* through)\b", text))
    try:
        import inverted_entity_pipeline as inverted
        inverted_match = inverted.detect(text, claims)
    except Exception:
        inverted_match = False

    # Inverted multi-constraint entity identification is the most specific structure; it takes
    # precedence over freshness/open-research/multi-hop when the question describes an unknown
    # entity through several testable relations.
    if inverted_match:
        route_name, reason = INVERTED_ENTITY, "unknown_entity_with_multiple_relations"
    elif freshness:
        route_name, reason = FRESHNESS, "explicit_present_or_latest_intent"
    elif open_research:
        route_name, reason = OPEN_RESEARCH, "open_ended_comparison_or_survey"
    elif multi_hop or len(triage.get("unknown_entities") or []) >= 2:
        route_name, reason = MULTI_HOP, "dependent_entity_chain"
    else:
        word_count = len(re.findall(r"\S+", text))
        if word_count <= 14 and len(claims) <= 1:
            route_name, reason = SIMPLE_FACT, "short_single_claim_question"

    return {"route": route_name, "reason": reason, **_STRATEGIES[route_name]}


def should_fallback(strategy_state: dict[str, Any], *, elapsed_s: float,
                    produced: int) -> bool:
    """A routed strategy gets one bounded attempt, then falls back when it yields nothing."""
    if not strategy_state or strategy_state.get("fallback_used"):
        return False
    if strategy_state.get("route") == GENERAL:
        return False
    budget = float(strategy_state.get("strategy_budget_s") or 0)
    return produced <= 0 and budget > 0 and elapsed_s >= budget


def fallback(strategy_state: dict[str, Any]) -> dict[str, Any]:
    updated = dict(strategy_state or {})
    updated["previous_route"] = updated.get("route")
    updated["route"] = updated.get("fallback") or GENERAL
    updated.update(_STRATEGIES.get(updated["route"], _STRATEGIES[GENERAL]))
    updated["fallback_used"] = True
    updated["fallback_reason"] = "zero_strategy_yield"
    return updated
