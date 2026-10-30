#!/usr/bin/env python3
"""持续目标运行账本：每次唤醒都区分进行中、可立即做、阻塞和完成。"""
from __future__ import annotations

import time
from copy import deepcopy
from typing import Any

TERMINAL = {"completed", "cancelled"}
RUNNING = {"running", "waiting_external"}


def _rows(value: Any) -> list[dict]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def reconcile(goal: str, state: dict | None = None, *, background: list[dict] | None = None,
              evidence: list[dict] | None = None) -> dict:
    """Build the next work ledger from observable facts, without guessing task semantics."""
    current = deepcopy(state) if isinstance(state, dict) else {}
    items = _rows(current.get("items"))
    bg_by_id = {str(item.get("id") or item.get("task_id") or ""): item
                for item in _rows(background) if str(item.get("id") or item.get("task_id") or "")}
    for item in items:
        identity = str(item.get("background_id") or item.get("id") or "")
        observed = bg_by_id.get(identity)
        if observed:
            item["background"] = observed
            if observed.get("done") is True:
                item["state"] = "completed" if observed.get("ok") else "blocked"
                item["blocker"] = "" if observed.get("ok") else str(observed.get("error") or "background failed")
            elif observed.get("running") is True:
                item["state"] = "running"
    active_ids = {str(item.get("background_id") or item.get("id") or "") for item in items}
    for identity, observed in bg_by_id.items():
        if identity not in active_ids:
            items.append({"id": identity, "title": str(observed.get("title") or identity),
                          "state": "running" if observed.get("running") else "queued",
                          "background_id": identity, "background": observed, "depends_on": []})
    by_id = {str(item.get("id") or ""): item for item in items}
    for item in items:
        if item.get("state") in TERMINAL | RUNNING | {"blocked"}:
            continue
        deps = [str(value) for value in item.get("depends_on") or []]
        blocked = [dep for dep in deps if by_id.get(dep, {}).get("state") == "blocked"]
        pending = [dep for dep in deps if by_id.get(dep, {}).get("state") not in TERMINAL]
        if blocked:
            item["state"] = "blocked"
            item["blocker"] = "blocked dependencies: " + ", ".join(blocked)
        elif pending:
            item["state"] = "queued"
        else:
            item["state"] = "ready"
    ready = [item for item in items if item.get("state") == "ready"]
    running = [item for item in items if item.get("state") in RUNNING]
    blocked = [item for item in items if item.get("state") == "blocked"]
    completed = [item for item in items if item.get("state") in TERMINAL]
    return {"schema_version": 1, "goal": str(goal), "items": items,
            "ready": ready, "running": running, "blocked": blocked, "completed": completed,
            "evidence": _rows(evidence) or _rows(current.get("evidence")),
            "next_action": (ready[0].get("next_action") or ready[0].get("title") if ready else
                            "supervise running work" if running else
                            # blocked 的 next_action 优先带 item 自己的具体指引(如"处理手机确认")，
                            # 只有缺失才退回泛化文案——用户要的是可操作下一步，不是一句废话。
                            (blocked[0].get("next_action") or "resolve blocker") if blocked else "complete"),
            "complete": bool(items) and len(completed) == len(items), "updated_at": time.time()}


def should_wait_only(ledger: dict | None) -> bool:
    """Waiting is valid only when no independent ready work remains."""
    value = ledger if isinstance(ledger, dict) else {}
    return bool(value.get("running")) and not value.get("ready")
