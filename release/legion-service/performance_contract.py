#!/usr/bin/env python3
"""端到端性能事件契约：统一阶段、透明度与墙钟分析。"""
from __future__ import annotations

from collections import Counter
from typing import Iterable

STAGES = (
    "accepted", "classified", "context_loaded", "environment_ready",
    "waiting_user", "resumed", "executing", "artifact_ready", "verifying",
    "benchmarking_quality", "final",
)

_EVENT_STAGE = {
    "meta": "accepted",
    "mode_selected": "classified",
    "classification": "classified",
    "context_loaded": "context_loaded",
    "intent_aligned": "context_loaded",
    "reference_context_ready": "context_loaded",
    "reference_selected": "context_loaded",
    "tool_inventory": "context_loaded",
    "skill_inventory": "context_loaded",
    "weapon_candidates": "context_loaded",
    "desktop_session_ready": "environment_ready",
    "desktop_ready": "environment_ready",
    "mobile_ready": "environment_ready",
    "task_created": "accepted",
    "approval": "waiting_user",
    "approval_required": "waiting_user",
    "waiting_user": "waiting_user",
    "needs_software": "waiting_user",
    "needs_account": "waiting_user",
    "needs_confirmation": "waiting_user",
    "task_resumed": "resumed",
    "resume_requested": "resumed",
    "approval_resolved": "resumed",
    "approval_decided": "resumed",
    "task_started": "executing",
    "step_started": "executing",
    "tool_started": "executing",
    "tool_end": "executing",
    "tool_timeout": "executing",
    "artifact_ready": "artifact_ready",
    "first_artifact_reported": "artifact_ready",
    "artifact_recovered": "artifact_ready",
    "artifact_handoff": "artifact_ready",
    "artifact_verified": "verifying",
    "delivery_contract_ready": "artifact_ready",
    "deliverable_ready": "artifact_ready",
    "verification_started": "verifying",
    "gap_identified": "verifying",
    "revision_started": "executing",
    "revision_candidate_ready": "verifying",
    "journey_evidence_ready": "verifying",
    "verification_result": "verifying",
    "verification_recorded": "verifying",
    "benchmark_started": "benchmarking_quality",
    "benchmark_result": "benchmarking_quality",
    "human_frontier_benchmark": "benchmarking_quality",
    "experience_written": "final",
    "task_finalized": "final",
    "final": "final",
}

HEARTBEAT_TYPES = frozenset(("heartbeat", "alive"))


def classify_event(event_type: str, data: dict | None = None) -> dict:
    data = data if isinstance(data, dict) else {}
    kind = str(data.get("t") or event_type or "step")
    text = str(data.get("x") or data.get("reason") or "")
    heartbeat = data.get("hb") is True or data.get("heartbeat") is True or kind in HEARTBEAT_TYPES or (
        kind == "reason" and any(token in text for token in (
            "思考中", "仍在工作", "军团正在判断队形", "总指挥读现场中",
        ))
    )
    stage = str(data.get("stage") or _EVENT_STAGE.get(kind) or (
        "final" if event_type == "final" else "executing"
    ))
    meaningful = not heartbeat and data.get("meaningful") is not False
    return {"kind": kind, "stage": stage, "heartbeat": heartbeat,
            "meaningful": meaningful}


def normalize_event(record: dict, *, fallback_ts: float = 0.0) -> dict:
    event_type = str(record.get("event") or "step")
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    classified = classify_event(event_type, data)
    return {
        "event_id": int(record.get("event_id") or 0),
        "event": event_type,
        "ts": float(record.get("ts") or fallback_ts or 0.0),
        "team_id": str(data.get("team_id") or ""),
        "agent": str(data.get("agent") or "legion"),
        "status": str(data.get("status") or ""),
        "reason": str(data.get("reason") or data.get("x") or ""),
        **classified,
    }


def transparency_evidence(records: Iterable[dict]) -> dict:
    events = [normalize_event(row) for row in records]
    tool_started = {str((row.get("data") or {}).get("call_id") or "") for row in records
                    if classify_event(str(row.get("event") or "step"), row.get("data"))["kind"] == "tool_started"}
    tool_finished = {str((row.get("data") or {}).get("call_id") or "") for row in records
                     if classify_event(str(row.get("event") or "step"), row.get("data"))["kind"]
                     in ("tool_end", "tool_timeout")}
    tool_started.discard("")
    tool_finished.discard("")
    stages = {event["stage"] for event in events}
    waiting = sum(event["stage"] == "waiting_user" for event in events)
    resumed = sum(event["stage"] == "resumed" for event in events)
    checks = {
        "accepted": "accepted" in stages,
        "execution_visible": "executing" in stages or "waiting_user" in stages,
        "tool_pairs_complete": tool_started <= tool_finished,
        "waiting_has_resume": waiting == 0 or resumed > 0,
        "final_visible": "final" in stages,
    }
    return {"checks": checks, "passed": all(checks.values()),
            "tool_started": len(tool_started), "tool_finished": len(tool_finished),
            "unclosed_tool_calls": sorted(tool_started - tool_finished),
            "waiting_events": waiting, "resume_events": resumed,
            "missing": [name for name, present in checks.items() if not present]}


def checkpoint_evidence(before: dict | None, after: dict | None) -> dict:
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    completed_before = list(before.get("completed_steps") or [])
    completed_after = list(after.get("completed_steps") or [])
    prefix_preserved = completed_after[:len(completed_before)] == completed_before
    no_duplicates = len(completed_after) == len({jsonable_step(step) for step in completed_after})
    checkpoint_before = before.get("checkpoint") or {}
    checkpoint_after = after.get("checkpoint") or {}
    checkpoint_preserved = not checkpoint_before or all(
        checkpoint_after.get(key) == value for key, value in checkpoint_before.items())
    checks = {"completed_prefix_preserved": prefix_preserved,
              "no_completed_step_duplicates": no_duplicates,
              "checkpoint_preserved": checkpoint_preserved}
    return {"checks": checks, "passed": all(checks.values()),
            "before_steps": len(completed_before), "after_steps": len(completed_after),
            "missing": [name for name, present in checks.items() if not present]}


def jsonable_step(value) -> str:
    import json
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def quality_evidence(final: dict | None, records: Iterable[dict] = (),
                     profile: str = "frontier") -> dict:
    final = final if isinstance(final, dict) else {}
    results = final.get("results") if isinstance(final.get("results"), list) else []
    artifacts = []
    for result in results:
        if isinstance(result, dict):
            artifacts.extend(item for item in (result.get("artifacts") or []) if isinstance(item, dict))
    events = [normalize_event(row) for row in records]
    stages = {event["stage"] for event in events}
    available = [item for item in artifacts if item.get("url") or item.get("path")]
    delivered = int(final.get("delivered_count") or 0) > 0 and not final.get("all_failed")
    checks = {"delivered": delivered}
    if profile in ("answer", "analysis"):
        checks["substantive_answer"] = any(
            isinstance(result, dict) and str(result.get("final") or "").strip()
            for result in results)
    if profile in ("analysis", "artifact", "frontier"):
        outcome = final.get("outcome") if isinstance(final.get("outcome"), dict) else {}
        verification = outcome.get("verification") if isinstance(outcome.get("verification"), dict) else {}
        reports = final.get("verification_reports") if isinstance(final.get("verification_reports"), list) else []
        explicit_pass = bool(verification.get("passed")) or bool(reports) and all(
            str(report.get("verdict") or "unverified") == "pass"
            for report in reports if isinstance(report, dict))
        checks["verification"] = explicit_pass
        checks["verification_complete"] = not bool(outcome.get("verification_incomplete"))
    if profile in ("artifact", "frontier"):
        checks["artifact"] = bool(available)
    if profile == "frontier":
        checks["named_benchmark"] = "benchmarking_quality" in stages or bool(final.get("human_benchmark"))
        checks["experience_writeback"] = any(event["kind"] == "experience_written" for event in events) \
                                        or bool(final.get("experience_written"))
    return {"checks": checks, "passed": all(checks.values()),
            "artifact_count": len(available),
            "missing": [name for name, present in checks.items() if not present]}


def analyze_events(records: Iterable[dict]) -> dict:
    events = sorted((normalize_event(row) for row in records),
                    key=lambda row: (row["ts"], row["event_id"]))
    if not events:
        return {"count": 0, "meaningful_count": 0, "max_silence_ms": None,
                "max_meaningful_silence_ms": None, "stage_first_ms": {},
                "stages": {}, "duplicates": 0, "out_of_order": 0}
    start = events[0]["ts"]
    gaps = [(events[index]["ts"] - events[index - 1]["ts"]) * 1000
            for index in range(1, len(events))]
    meaningful = [event for event in events if event["meaningful"]]
    meaningful_gaps = [(meaningful[index]["ts"] - meaningful[index - 1]["ts"]) * 1000
                       for index in range(1, len(meaningful))]
    stage_first = {}
    for event in events:
        stage_first.setdefault(event["stage"], round((event["ts"] - start) * 1000, 1))
    ids = [event["event_id"] for event in events if event["event_id"]]
    return {
        "count": len(events),
        "meaningful_count": len(meaningful),
        "heartbeat_count": len(events) - len(meaningful),
        "max_silence_ms": round(max(gaps), 1) if gaps else 0.0,
        "max_meaningful_silence_ms": round(max(meaningful_gaps), 1) if meaningful_gaps else 0.0,
        "stage_first_ms": stage_first,
        "stages": dict(Counter(event["stage"] for event in events)),
        "duplicates": len(ids) - len(set(ids)),
        "out_of_order": sum(ids[index] <= ids[index - 1] for index in range(1, len(ids))),
    }
