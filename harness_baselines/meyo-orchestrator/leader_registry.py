#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首脑候选目录与基于可验证契约的准入选择。"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class LeaderCapabilities:
    persistent_children: bool
    parallel_dispatch: bool
    resume: bool
    hard_cancel: bool
    inbox: bool
    dynamic_registry: bool
    deterministic_supervision: bool
    delivery_gate: bool
    self_healing: bool
    context_integrity: bool
    flow_integrity: bool

    @property
    def score(self) -> int:
        return sum(asdict(self).values())


@dataclass(frozen=True)
class LeaderCandidate:
    key: str
    name: str
    capabilities: LeaderCapabilities
    verified: bool
    role: str

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "verified": self.verified,
                "role": self.role, "score": self.capabilities.score,
                "capabilities": asdict(self.capabilities)}


CANDIDATES = {
    "omnigent": LeaderCandidate(
        "omnigent", "Omnigent + Meyo Supervisor",
        LeaderCapabilities(True, True, True, True, True, True, True, True, True, True, True),
        True, "default",
    ),
    "polly": LeaderCandidate(
        "polly", "Polly",
        LeaderCapabilities(True, True, True, True, True, False, False, False, False, False, False),
        True, "software-engineering-template",
    ),
    "autoloop": LeaderCandidate(
        "autoloop", "Meyo Autoloop",
        LeaderCapabilities(False, False, False, False, False, True, True, True, False, True, True),
        True, "fallback",
    ),
    "cascade": LeaderCandidate(
        "cascade", "Meyo Confidence Cascade",
        LeaderCapabilities(False, False, False, False, False, True, True, True, False, True, True),
        True, "qa-specialist",
    ),
}


def list_leaders() -> list[dict[str, Any]]:
    return [candidate.to_dict() for candidate in CANDIDATES.values()]


def select_default() -> dict[str, Any]:
    eligible = [candidate for candidate in CANDIDATES.values()
                if candidate.verified and candidate.capabilities.delivery_gate
                and candidate.capabilities.deterministic_supervision
                and candidate.capabilities.self_healing
                and candidate.capabilities.context_integrity
                and candidate.capabilities.flow_integrity]
    selected = max(eligible, key=lambda candidate: candidate.capabilities.score)
    return selected.to_dict()
