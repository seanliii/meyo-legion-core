#!/usr/bin/env python3
"""云电脑前沿质量运行账本：目标、参照、执行、评审与返工的持久事实源。"""
from __future__ import annotations

from copy import deepcopy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import desktop_workspace
import quality_context

RUN_STATES = {
    "profiling", "scouting", "executing", "verifying", "reviewing", "revising",
    "delivered", "parity", "below_frontier", "blocked",
}
TERMINAL_STATES = {"delivered", "parity", "below_frontier", "blocked"}
VERDICTS = {"VERIFIED_NARROW_WIN", "PARITY_OR_INCONCLUSIVE", "BELOW_FRONTIER"}
DEFAULT_DIMENSIONS = (
    ("domain_depth", "领域深度"),
    ("originality", "原创性"),
    ("reliability", "可靠性"),
    ("aesthetics", "审美"),
    ("completeness", "完成度"),
    ("evidence", "证据与复现"),
    ("time_to_value", "交付效率"),
)
_LOCK = threading.RLock()


def _run_root(uid: str, project_id: str, task_id: str) -> Path:
    return desktop_workspace._meta_root(uid, project_id) / "quality" / desktop_workspace._safe(task_id, "task")


def _run_path(uid: str, project_id: str, task_id: str) -> Path:
    return _run_root(uid, project_id, task_id) / "run.json"


def _events_path(uid: str, project_id: str, task_id: str) -> Path:
    return _run_root(uid, project_id, task_id) / "events.jsonl"


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _event(uid: str, project_id: str, task_id: str, kind: str, data: dict) -> dict:
    path = _events_path(uid, project_id, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    try:
        rows = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass
    row = {"event_id": len(rows) + 1, "type": str(kind)[:80], "data": data, "at": time.time()}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    desktop_workspace.append_event(uid, project_id, task_id, f"quality_{kind}", data)
    return row


def create(uid: str, project_id: str, task_id: str, profile: dict | None = None,
           *, max_rounds: int = 3, wall_clock_s: int = 7200) -> dict:
    task = desktop_workspace.get_task(uid, project_id, task_id, include_events=False)
    if not task.get("ok"):
        return task
    path = _run_path(uid, project_id, task_id)
    with _LOCK:
        existing = _load(path)
        if existing:
            return {"ok": True, "status": "unchanged", "run": existing}
        now = time.time()
        dimensions = [
            {"key": key, "name": name, "weight": round(1 / len(DEFAULT_DIMENSIONS), 6),
             "criteria": [], "candidate_score": None, "reference_score": None}
            for key, name in DEFAULT_DIMENSIONS
        ]
        run = {
            "schema_version": 1, "uid": task["task"]["uid"], "project_id": task["task"]["project_id"],
            "task_id": task["task"]["task_id"], "state": "profiling", "version": 1,
            "profile": dict(profile or {}), "references": [], "golden_task": {},
            "quality_context": {}, "context_history": [], "role_contexts": {},
            "journey_observations": [], "gap_plan": [],
            "north_star": "", "decisions": [], "contracts": [], "organization": {},
            "dimensions": dimensions, "claims": [], "evidence": [], "reviews": [],
            "round": 0, "max_rounds": max(1, min(int(max_rounds or 3), 8)),
            "wall_clock_s": max(60, min(int(wall_clock_s or 7200), 86400)),
            "verdict": "", "verified_strengths": [], "remaining_gaps": [],
            "metrics": {"started_at": now, "first_value_at": None, "last_meaningful_at": now,
                        "longest_silence_s": 0.0, "finished_at": None, "wall_clock_s": None},
            "created_at": now, "updated_at": now,
        }
        _atomic(path, run)
        _event(uid, project_id, task_id, "created", {"state": "profiling"})
    return {"ok": True, "run": run}


def role_contexts_for_run(run: dict, *, context_path: Path | None = None) -> dict:
    """Derive views from this run's canonical QC; retain role-specific notes/evidence.

    No reconciliation, research or planning occurs here. Custom roles not named
    in the current organization remain valid; only QC-owned projection fields
    are replaced. The existing run file is the lookup target for omitted sources.
    """
    context = run.get("quality_context") if isinstance(run.get("quality_context"), dict) else {}
    saved = run.get("role_contexts") if isinstance(run.get("role_contexts"), dict) else {}
    roles = {str(key): str(value.get("role") or key)
             for key, value in saved.items() if isinstance(value, dict)}
    organization = run.get("organization") if isinstance(run.get("organization"), dict) else {}
    for node in organization.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or node.get("owner_role") or "").strip()
        if node_id:
            roles[node_id] = str(node.get("owner_role") or roles.get(node_id) or node_id)
    if not roles:
        return {}
    if context_path is None and all(run.get(key) for key in ("uid", "project_id", "task_id")):
        context_path = _run_path(run["uid"], run["project_id"], run["task_id"])
    reference = ({"path": str(context_path), "field": "quality_context"} if context_path is not None
                 else deepcopy(context.get("context_ref")))
    views = {}
    for node_id, role in roles.items():
        previous = saved.get(node_id) if isinstance(saved.get(node_id), dict) else {}
        projected = quality_context.role_snapshot(context, role)
        prior_projection = quality_context.role_snapshot(context, str(previous.get("role") or role))
        derived_keys = set(projected) | set(prior_projection) | {
            "sources", "query_ledger", "source_index", "source_count", "query_count",
            "source_index_omitted", "context_ref",
        }
        view = {key: deepcopy(value) for key, value in previous.items() if key not in derived_keys}
        view.update(projected)
        if isinstance(reference, dict):
            view["context_ref"] = deepcopy(reference)
        views[node_id] = view
    return views


def get(uid: str, project_id: str, task_id: str, include_events: bool = True) -> dict:
    path = _run_path(uid, project_id, task_id)
    run = _load(path)
    if not run:
        return {"ok": False, "error": "quality run not found"}
    # Old persisted views may predate the current QC. Reading projects them
    # without rewriting historical evidence or advancing the run state.
    run["role_contexts"] = role_contexts_for_run(run, context_path=path)
    result = {"ok": True, "run": run}
    if include_events:
        rows = []
        try:
            rows = [json.loads(line) for line in _events_path(uid, project_id, task_id).read_text(encoding="utf-8").splitlines()]
        except (OSError, json.JSONDecodeError):
            pass
        result["events"] = rows
    return result


def update(uid: str, project_id: str, task_id: str, *, state: str | None = None,
           patch: dict | None = None, meaningful: bool = True) -> dict:
    path = _run_path(uid, project_id, task_id)
    with _LOCK:
        run = _load(path)
        if not run:
            return {"ok": False, "error": "quality run not found"}
        if run.get("state") in TERMINAL_STATES:
            return {"ok": False, "error": "quality run is terminal", "run": run}
        if state is not None:
            if state not in RUN_STATES:
                return {"ok": False, "error": "invalid quality state"}
            run["state"] = state
        for key, value in dict(patch or {}).items():
            if key not in {"uid", "project_id", "task_id", "created_at", "schema_version"}:
                run[key] = value
        run["role_contexts"] = role_contexts_for_run(run, context_path=path)
        now = time.time()
        metrics = run.setdefault("metrics", {})
        previous = float(metrics.get("last_meaningful_at") or run.get("created_at") or now)
        if meaningful:
            silence = max(0.0, now - previous)
            metrics["longest_silence_s"] = max(float(metrics.get("longest_silence_s") or 0), silence)
            metrics["last_meaningful_at"] = now
            if metrics.get("first_value_at") is None and state not in {None, "profiling", "scouting"}:
                metrics["first_value_at"] = now
        run.update({"version": int(run.get("version") or 1) + 1, "updated_at": now})
        _atomic(path, run)
        _event(uid, project_id, task_id, "updated", {"state": run["state"], "version": run["version"]})
    return {"ok": True, "run": run}


def add_evidence(uid: str, project_id: str, task_id: str, evidence: dict) -> dict:
    current = get(uid, project_id, task_id, include_events=False)
    if not current.get("ok"):
        return current
    run = current["run"]
    rows = list(run.get("evidence") or [])
    item = {"evidence_id": str(evidence.get("evidence_id") or f"E-{len(rows)+1:03d}"),
            "kind": str(evidence.get("kind") or "file")[:80],
            "path": str(evidence.get("path") or "")[:4000],
            "claim_ids": [str(value)[:120] for value in evidence.get("claim_ids") or []],
            "sha256": str(evidence.get("sha256") or "")[:128],
            "verified": bool(evidence.get("verified")),
            "details": dict(evidence.get("details") or {})}
    rows.append(item)
    return update(uid, project_id, task_id, patch={"evidence": rows})


def finalize(uid: str, project_id: str, task_id: str, verdict: str, *,
             verified_strengths: list | None = None, remaining_gaps: list | None = None,
             report: dict | None = None) -> dict:
    if verdict not in VERDICTS:
        return {"ok": False, "error": "invalid quality verdict"}
    path = _run_path(uid, project_id, task_id)
    with _LOCK:
        run = _load(path)
        if not run:
            return {"ok": False, "error": "quality run not found"}
        now = time.time()
        metrics = run.setdefault("metrics", {})
        metrics.update({"finished_at": now,
                        "wall_clock_s": round(now - float(metrics.get("started_at") or run.get("created_at") or now), 3)})
        state = {"VERIFIED_NARROW_WIN": "delivered", "PARITY_OR_INCONCLUSIVE": "parity",
                 "BELOW_FRONTIER": "below_frontier"}[verdict]
        run.update({"state": state, "verdict": verdict,
                    "verified_strengths": list(verified_strengths or []),
                    "remaining_gaps": list(remaining_gaps or []), "report": dict(report or {}),
                    "version": int(run.get("version") or 1) + 1, "updated_at": now})
        run["role_contexts"] = role_contexts_for_run(run, context_path=path)
        _atomic(path, run)
        _event(uid, project_id, task_id, "finalized", {"state": state, "verdict": verdict})
    return {"ok": True, "run": run}
