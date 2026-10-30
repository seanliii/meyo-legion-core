#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首脑作战上下文：只提供真实现场，不替模型做语义决策。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import orchestrator
from meyo_harnesses import MEYO_HARNESSES, health


@dataclass
class CaptainContext:
    goal: str
    acceptance: list[str] = field(default_factory=list)
    conversation_history: list[dict[str, Any]] = field(default_factory=list)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    long_term_memory: list[dict[str, Any]] = field(default_factory=list)
    task_memory: dict[str, Any] = field(default_factory=dict)
    members: list[dict[str, Any]] = field(default_factory=list)
    toolbox_summary: dict[str, Any] = field(default_factory=dict)
    permissions: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    delivery_contract: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def prompt_block(self, max_chars: int = 30000) -> str:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        return "【首脑作战上下文·仅事实，不是固定路由命令】\n" + payload[:max_chars]


def build_context(goal: str, *, acceptance: list[str] | None = None,
                  conversation_history: list[dict[str, Any]] | None = None,
                  recent_events: list[dict[str, Any]] | None = None,
                  long_term_memory: list[dict[str, Any]] | None = None,
                  memory_dir: str | Path | None = None,
                  task_memory: dict[str, Any] | None = None,
                  toolbox: dict[str, Any] | None = None,
                  permissions: dict[str, Any] | None = None,
                  budgets: dict[str, Any] | None = None,
                  delivery_contract: dict[str, Any] | None = None,
                  artifacts: list[dict[str, Any]] | None = None,
                  include_histories: bool = False,
                  member_catalog: list[dict[str, Any]] | None = None) -> CaptainContext:
    members = []
    snapshot = health.health_snapshot()
    catalog = member_catalog or [
        {"key": item.key, "display_name": item.display_name,
         "cli_hint": item.cli_hint, "available": True}
        for item in MEYO_HARNESSES.values()
    ]
    for row in catalog:
        key = row["key"]
        history = orchestrator.read_harness_history(key, limit=3) if include_histories else []
        members.append({**row, "circuit_open": key in snapshot.get("circuits_open", []),
                        "recent_history": history,
                        "source_hint": _source_hint(key)})
    memories = list(long_term_memory or _read_memory(memory_dir))
    return CaptainContext(
        goal=goal,
        acceptance=list(acceptance or []),
        conversation_history=list(conversation_history or [])[-20:],
        recent_events=list(recent_events or [])[-50:],
        long_term_memory=memories,
        task_memory=dict(task_memory or {}),
        members=members,
        toolbox_summary=dict(toolbox or {}),
        permissions=dict(permissions or {}),
        budgets=dict(budgets or {}),
        delivery_contract=dict(delivery_contract or {}),
        artifacts=list(artifacts or []),
    )


def _read_memory(memory_dir: str | Path | None) -> list[dict[str, Any]]:
    if not memory_dir:
        return []
    root = Path(memory_dir)
    rows = []
    for path in sorted(root.glob("*.json"))[-10:]:
        try:
            rows.append({"path": str(path), "value": json.loads(path.read_text())})
        except Exception:
            continue
    return rows


def _source_hint(key: str) -> str:
    mapping = {
        "auto": "seed-users-system/qa_system/agent/",
        "cc": "harness_baselines/claude-code-oss/",
        "codex": "harness_baselines/codex/codex-rs/",
        "deepseek": "harness_baselines/deepseek-harness/",
        "hermes": "harness_baselines/hermes-agent/",
        "penguin": "harness_baselines/penguin-harness/",
        "omnigent": "harness_baselines/ecosystem/omnigent/",
    }
    return mapping.get(key, "harness_baselines/")
