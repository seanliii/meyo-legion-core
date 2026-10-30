#!/usr/bin/env python3
"""Event-driven control for one native agent step."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class StepDecision:
    trigger: str
    action: str
    goal: str
    candidate_id: str = ""
    gaps: tuple[str, ...] = ()
    failed_paths: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide(projection: Any, delta: dict[str, Any] | None = None, *,
           budget_closing: bool = False) -> StepDecision:
    """Choose the only control transition that may enter the next model step."""
    candidates = dict(getattr(projection, "candidates", {}) or {})
    gaps = tuple(str(item) for item in (getattr(projection, "gaps", []) or []) if str(item))
    failed_paths = tuple((getattr(projection, "failed_paths", []) or [])[-3:])
    selected = [(candidate_id, item) for candidate_id, item in candidates.items()
                if item.get("state") in {"selected", "delivery_verified"}]
    if selected:
        candidate_id, candidate = selected[0]
        return StepDecision(
            trigger="candidate_selected", action="deliver",
            goal="Deliver the selected candidate without further retrieval.",
            candidate_id=candidate_id, gaps=gaps, failed_paths=failed_paths)
    if budget_closing:
        return StepDecision(
            trigger="budget_closing", action="converge",
            goal="Use only supported evidence to select or abstain; do not start broad retrieval.",
            gaps=gaps, failed_paths=failed_paths)
    if delta is not None and not delta.get("state_changed", False):
        return StepDecision(
            trigger="no_projection_delta", action="pivot",
            goal="Choose a structurally different action that targets an unresolved gap.",
            gaps=gaps, failed_paths=failed_paths)
    return StepDecision(
        trigger="state_changed" if delta else "turn_started", action="advance",
        goal=("Resolve the highest-value remaining gap." if gaps
              else "Interpret new evidence, compare grounded candidates, and select the next action."),
        gaps=gaps, failed_paths=failed_paths)


def render(decision: StepDecision, projection: Any) -> str:
    """Render one compact model-visible state update instead of stacked hints."""
    candidates = []
    for candidate_id, item in (getattr(projection, "candidates", {}) or {}).items():
        candidates.append({
            "id": candidate_id,
            "value": str(item.get("value") or "")[:160],
            "state": item.get("state"),
            "evidence_ids": list(item.get("evidence_ids") or [])[:8],
            "unresolved_constraints": list(item.get("unresolved_constraints") or [])[:6],
        })
    payload = {
        "control": decision.to_dict(),
        "candidates": candidates[-8:],
        "evidence_count": len(getattr(projection, "evidence", {}) or {}),
        "last_seq": int(getattr(projection, "last_seq", 0) or 0),
    }
    return "[canonical-step-state]\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
