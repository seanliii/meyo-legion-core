#!/usr/bin/env python3
"""Generic state for inverted multi-constraint entity identification.

Inverted questions describe an unknown entity through several independently testable
relations and ask for its unique name. Broad keyword search over the whole description
retrieves term-relevant but entity-irrelevant pages. This module keeps a candidate-first
state so retrieval verifies named candidates against each constraint instead of hoping a
single page states every relation.

No benchmark/domain answer rules: routing and state use only question structure and the
generic constraint schema. Model priors may only produce ``hypothesized`` candidates; a
candidate becomes ``supported`` solely from grounded per-constraint evidence.
fail-safe: every function tolerates missing keys; ``MEYO_INVERTED_ENTITY=0`` disables detect.
"""
from __future__ import annotations

import os
import re
from typing import Any


def _enabled() -> bool:
    return os.environ.get("MEYO_INVERTED_ENTITY", "1").strip().lower() not in (
        "0", "false", "off", "no")


def detect(question: str, claims: list[dict[str, Any]] | None = None) -> bool:
    """Detect an unknown entity constrained by several independently testable relations."""
    if not _enabled():
        return False
    text = str(question or "").strip()
    if not text:
        return False
    asks_identity = bool(re.search(
        r"(?i)\b(?:who|which|what|identify|find|name of|could you help me find)\b", text))
    packed = list(claims or [])
    if packed:
        constraint_count = len(packed)
    else:
        parts = [part.strip() for part in re.split(
            r"[,;]|\b(?:and|also|which|who|that|where|while)\b", text, flags=re.I)
                 if len(part.strip()) >= 8]
        constraint_count = len(parts)
    relation_signals = len(re.findall(
        r"(?i)\b(?:between|within|near|nearby|from|to|before|after|born|won|founded|"
        r"located|member|played|released|meters?|kilometers?|miles?|north|south|east|west)\b",
        text))
    return asks_identity and constraint_count >= 3 and relation_signals >= 2


def initialize(session: dict[str, Any], question: str,
               claims: list[dict[str, Any]]) -> dict[str, Any]:
    state = session.setdefault("_inverted_entity", {})
    state["active"] = detect(question, claims)
    state["claims"] = [dict(claim) for claim in claims]
    state.setdefault("candidates", {})
    state.setdefault("failed_queries", [])
    state.setdefault("stage", "candidate_generation")
    return state


def generate_hypotheses(question: str, *, llm_call=None, max_candidates: int = 5) -> list[str]:
    """Generate unverified candidate names from model prior; never creates evidence."""
    prompt = (
        "This is an inverted entity-identification question. Generate a small diverse set of "
        "plausible named entities to investigate. These are hypotheses only, not answers or "
        "evidence. Prefer exact canonical names and avoid explanations.\n\nQuestion:\n"
        + str(question or "")[:1500]
        + '\n\nReturn strict JSON: {"candidates":["name 1","name 2"]}'
    )
    try:
        import json
        if llm_call is not None:
            raw = str(llm_call(prompt) or "")
        else:
            import qa_llm_gateway as _gw
            _r = _gw.call(prompt, tier="mid", timeout=16, max_tokens=300)
            raw = (_r.get("out") or "") if isinstance(_r, dict) else str(_r or "")
        match = re.search(r"\{[\s\S]*\}", raw)
        data = json.loads(match.group(0)) if match else {}
        names = []
        for value in data.get("candidates") or []:
            name = " ".join(str(value or "").split()).strip()
            if 2 <= len(name) <= 160 and name.casefold() not in {x.casefold() for x in names}:
                names.append(name)
        return names[:max(1, int(max_candidates or 5))]
    except Exception:
        return []


def register_hypothesis(session: dict[str, Any], candidate: str, *, origin: str) -> bool:
    """Record an unverified hypothesis without promoting the canonical lifecycle."""
    value = " ".join(str(candidate or "").split()).strip()
    if not value:
        return False
    state = session.setdefault("_inverted_entity", {"active": True, "claims": [],
                                                     "candidates": {}, "failed_queries": []})
    key = value.casefold()
    record = state.setdefault("candidates", {}).setdefault(key, {
        "value": value, "state": "hypothesized", "origin": origin,
        "constraints": {}, "evidence_ids": [],
    })
    record["origin"] = origin
    state["stage"] = "constraint_verification"
    return True


def update_matrix(session: dict[str, Any], candidate: str,
                  verdicts: list[dict[str, Any]], evidence_ids: list[str]) -> dict[str, Any] | None:
    """Update one candidate's constraint matrix; supported requires every claim satisfied."""
    state = session.get("_inverted_entity") or {}
    record = (state.get("candidates") or {}).get(str(candidate or "").casefold())
    if not record:
        return None
    for verdict in verdicts or []:
        claim_id = str(verdict.get("id") or verdict.get("claim_id") or "")
        status = str(verdict.get("state") or "unknown")
        if claim_id and status in {"satisfied", "contradicted", "unknown"}:
            record["constraints"][claim_id] = {
                "state": status,
                "evidence_ids": list(dict.fromkeys(verdict.get("evidence_ids") or evidence_ids)),
            }
    record["evidence_ids"] = list(dict.fromkeys(record.get("evidence_ids", []) + evidence_ids))
    required_claim_ids = {str(claim.get("id") or "") for claim in state.get("claims") or []
                          if str(claim.get("id") or "")}
    matrix = record["constraints"]
    if any(item.get("state") == "contradicted" for item in matrix.values()):
        record["state"] = "contradicted"
    elif (required_claim_ids and required_claim_ids.issubset(matrix)
          and all(matrix[claim_id].get("state") == "satisfied"
                  for claim_id in required_claim_ids)):
        record["state"] = "supported"
    else:
        record["state"] = "investigated"
    return record


def next_verification_query(session: dict[str, Any]) -> dict[str, str]:
    """Choose one candidate x unresolved constraint; never emit a bare constraint query."""
    state = session.get("_inverted_entity") or {}
    claims = state.get("claims") or []
    candidates = [item for item in (state.get("candidates") or {}).values()
                  if item.get("state") != "contradicted"]
    candidates.sort(key=lambda item: (-len(item.get("evidence_ids") or []), item.get("value", "")))
    failed = {str(item).casefold() for item in state.get("failed_queries") or []}
    for candidate in candidates:
        matrix = candidate.get("constraints") or {}
        for claim in claims:
            claim_id = str(claim.get("id") or "")
            if (matrix.get(claim_id) or {}).get("state") == "satisfied":
                continue
            query = " ".join((str(candidate.get("value") or ""),
                              str(claim.get("text") or ""))).strip()[:300]
            if query and query.casefold() not in failed:
                return {"candidate": candidate["value"], "claim_id": claim_id, "query": query}
    return {"candidate": "", "claim_id": "", "query": ""}


def record_query_result(session: dict[str, Any], query: str, *, named_hits: int,
                        total_hits: int) -> bool:
    """Return True when a low-yield path should pivot back to candidate generation."""
    state = session.setdefault("_inverted_entity", {})
    if query:
        state.setdefault("failed_queries", []).append(query)
        del state["failed_queries"][:-20]
    rate = float(named_hits) / max(1, int(total_hits or 0))
    state["last_entity_hit_rate"] = round(rate, 4)
    pivot = int(total_hits or 0) >= 8 and named_hits == 0
    if pivot:
        state["stage"] = "candidate_generation"
        state["pivot_reason"] = "zero_entity_hit_rate"
    return pivot


def failure_report(session: dict[str, Any]) -> dict[str, Any]:
    """Structured "no legal candidate" summary instead of a bare empty result."""
    state = session.get("_inverted_entity") or {}
    rows = []
    for record in (state.get("candidates") or {}).values():
        matrix = record.get("constraints") or {}
        rows.append({
            "candidate": record.get("value"),
            "state": record.get("state"),
            "satisfied": sum(1 for item in matrix.values() if item.get("state") == "satisfied"),
            "contradicted": sum(1 for item in matrix.values() if item.get("state") == "contradicted"),
            "unresolved": [claim.get("id") for claim in state.get("claims") or []
                           if (matrix.get(str(claim.get("id") or "")) or {}).get("state")
                           != "satisfied"],
        })
    rows.sort(key=lambda item: (-item["satisfied"], item["contradicted"]))
    return {"stage": state.get("stage"), "entity_hit_rate": state.get("last_entity_hit_rate"),
            "candidates": rows[:8]}
