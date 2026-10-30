#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团工具审批状态机：持久、幂等、可断线恢复。"""
from __future__ import annotations

import time
from typing import Any

import team_state

_TERMINAL = {"approved", "denied", "expired"}


def request(team_id: str, agent: str, capability: str, arguments: dict[str, Any] | None = None,
            task_key: str = "", reason: str = "", ttl_s: int = 3600) -> dict:
    with team_state._state_lock(team_id):
        state = team_state.load(team_id)
        if not state:
            return {"ok": False, "error": "team not found"}
        state.setdefault("approvals", {})
        seq = int(state.get("approval_seq", 0)) + 1
        state["approval_seq"] = seq
        approval_id = f"approval-{seq}"
        now = time.time()
        row = {"id": approval_id, "team_id": team_id, "task_key": task_key,
               "agent": agent, "capability": capability, "arguments": arguments or {},
               "reason": reason[:1000], "status": "pending", "version": 1,
               "requested_at": now, "expires_at": now + max(1, int(ttl_s)),
               "decided_at": None, "decision_by": "", "decision_reason": ""}
        state["approvals"][approval_id] = row
        state["events"].append({"t": "approval_required", **row, "ts": now})
        state["events"] = state["events"][-500:]
        team_state._write(state)
    team_state.append_event(team_id, {"t": "approval_required", **row})
    return {"ok": True, "approval": row}


def list_approvals(team_id: str, include_terminal: bool = True) -> dict:
    expire_pending(team_id)
    state = team_state.load(team_id)
    if not state:
        return {"ok": False, "error": "team not found", "approvals": []}
    rows = list((state.get("approvals") or {}).values())
    if not include_terminal:
        rows = [row for row in rows if row.get("status") == "pending"]
    rows.sort(key=lambda row: row.get("requested_at", 0), reverse=True)
    return {"ok": True, "team_id": team_id, "approvals": rows}


def decide(team_id: str, approval_id: str, decision: str, actor: str = "user",
           reason: str = "", expected_version: int | None = None) -> dict:
    decision = str(decision or "").lower()
    if decision not in ("approved", "denied", "approve", "deny"):
        return {"ok": False, "error": "decision must be approve/deny"}
    status = "approved" if decision in ("approved", "approve") else "denied"
    with team_state._state_lock(team_id):
        state = team_state.load(team_id)
        row = ((state or {}).get("approvals") or {}).get(approval_id)
        if not row:
            return {"ok": False, "error": "approval not found"}
        if expected_version is not None and int(row.get("version", 1)) != int(expected_version):
            return {"ok": False, "error": "approval version conflict", "approval": row}
        if row.get("status") in _TERMINAL:
            return {"ok": row.get("status") == status, "status": "unchanged", "approval": row}
        now = time.time()
        if now >= float(row.get("expires_at") or 0):
            row.update({"status": "expired", "decided_at": now, "version": int(row.get("version", 1)) + 1})
        else:
            row.update({"status": status, "decided_at": now, "decision_by": actor[:200],
                        "decision_reason": reason[:1000], "version": int(row.get("version", 1)) + 1})
        state["events"].append({"t": "approval_decided", **row, "ts": now})
        state["events"] = state["events"][-500:]
        team_state._write(state)
    team_state.append_event(team_id, {"t": "approval_decided", **row})
    return {"ok": row["status"] == status, "approval": row}


def expire_pending(team_id: str) -> int:
    with team_state._state_lock(team_id):
        state = team_state.load(team_id)
        if not state:
            return 0
        now = time.time(); changed = []
        for row in (state.get("approvals") or {}).values():
            if row.get("status") == "pending" and now >= float(row.get("expires_at") or 0):
                row.update({"status": "expired", "decided_at": now,
                            "version": int(row.get("version", 1)) + 1})
                state["events"].append({"t": "approval_decided", **row, "ts": now})
                changed.append(dict(row))
        if changed:
            state["events"] = state["events"][-500:]
            team_state._write(state)
    for row in changed:
        team_state.append_event(team_id, {"t": "approval_decided", **row})
    return len(changed)
