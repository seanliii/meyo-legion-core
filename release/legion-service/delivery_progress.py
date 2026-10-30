#!/usr/bin/env python3
"""增量交付进度：合同满足时发可见里程碑，final 仍等待必要验证。"""
from __future__ import annotations

import threading
from typing import Callable


def milestone(results: list[dict], delivery_contract: dict | None,
              *, verification_required: bool = False,
              verification_reports: list[dict] | None = None,
              event_cb: Callable[[dict], None] | None = None) -> dict:
    import task_outcome
    contract = task_outcome.verify_delivery_contract(
        results, delivery_contract or {}, event_cb=event_cb)
    history = [row for row in (verification_reports or []) if isinstance(row, dict)]
    reports = task_outcome.terminal_verification_reports(history)
    verification_passed = bool(reports) and all(
        str(row.get("verdict") or "unverified") == "pass" for row in reports)
    contract_ready = bool(contract["specified"] and contract["satisfied"])
    deliverable = contract_ready and (not verification_required or verification_passed)
    return {"contract_ready": contract_ready, "verification_required": verification_required,
            "verification_passed": verification_passed, "deliverable": deliverable,
            "artifact_count": contract.get("verified_count", 0),
            "missing": contract.get("missing", []), "contract": contract}


def _text(value: object, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _references(packet: dict | None) -> list[dict]:
    value = packet if isinstance(packet, dict) else {}
    frontier = value.get("frontier_context") if isinstance(value.get("frontier_context"), dict) else {}
    rows = []
    for item in [*(frontier.get("named_references") or []),
                 *(frontier.get("dimension_leaders") or [])]:
        if not isinstance(item, dict):
            continue
        url = _text(item.get("url"), 2000)
        name = _text(item.get("name") or item.get("work") or item.get("title"), 200)
        if url and not any(row["url"] == url for row in rows):
            rows.append({"name": name or url, "url": url,
                         "dimension": _text(item.get("dimension") or item.get("strength"), 120),
                         "evidence_state": _text(item.get("evidence_state") or "candidate", 80)})
    return rows[:6]


def _open_gaps(packet: dict | None) -> list[dict]:
    value = packet if isinstance(packet, dict) else {}
    rows = value.get("open_gaps") or value.get("gap_plan") or []
    open_rows = [dict(item) if isinstance(item, dict) else {"problem": str(item)} for item in rows
                 if not isinstance(item, dict) or str(item.get("status") or "open") != "closed"]
    severity = {"critical": 4, "high": 3, "medium": 2, "low": 1}

    def weight(row: dict) -> float:
        try:
            return float(row.get("impact_weight") or 0)
        except (TypeError, ValueError):
            return 0.0

    return sorted(open_rows, key=lambda row: (
        -weight(row), -severity.get(str(row.get("severity") or "").lower(), 0)))


class MilestoneEmitter:
    """Translate existing delivery facts into deduplicated user-visible milestones."""

    def __init__(self, emit: Callable[[dict], None], delivery_contract: dict | None,
                 verification_required: bool = False):
        self.emit = emit
        self.delivery_contract = delivery_contract or {}
        self.verification_required = verification_required
        self.sent_contract = False
        self.sent_deliverable = False
        self.sent: set[str] = set()
        self._lock = threading.Lock()

    def _once(self, key: str, event: dict) -> None:
        with self._lock:
            if key in self.sent:
                return
            self.sent.add(key)
        self.emit({"agent": "commander", "status": "ready", **event})

    def announce_plan(self, plan: dict | None, packet: dict | None = None) -> None:
        source = plan if isinstance(plan, dict) else {}
        shared = packet if isinstance(packet, dict) else {}
        intent = source.get("experience_intent") or shared.get("experience_intent") or {}
        golden = source.get("golden_task") or shared.get("golden_task") or {}
        outcome = _text((intent or {}).get("desired_outcome") or
                        (golden or {}).get("expected_user_outcome") or
                        (intent or {}).get("core_need"), 500)
        if outcome:
            self._once("intent_aligned", {"t": "intent_aligned", "stage": "context_loaded",
                                           "experience_intent": intent, "golden_task": golden,
                                           "x": f"已对齐本次用户结果：{outcome}"})
        contract = source.get("frontier_research_contract") \
            if isinstance(source.get("frontier_research_contract"), dict) else \
            shared.get("frontier_research_contract") \
            if isinstance(shared.get("frontier_research_contract"), dict) else {}
        atlas = contract.get("reference_atlas") if isinstance(contract.get("reference_atlas"), dict) else {}
        if atlas:
            self._once("reference_context_ready", {
                "t": "reference_context_ready", "stage": "context_loaded",
                "reference_atlas": atlas,
                "sources": [{"name": "世界级交付 Context Atlas", "url": atlas.get("km_url") or ""}],
                "x": "分类参考入口已就绪；军团将按本题自主选择和补充参照"})

    def observe_artifact(self, event: dict | None) -> None:
        value = event if isinstance(event, dict) else {}
        url = _text(value.get("url"), 2000)
        path = _text(value.get("path"), 2000)
        if not url and not path:
            return
        self._once("first_artifact_reported", {
            "t": "first_artifact_reported", "stage": "artifact_ready",
            "artifact_url": url, "artifact_path": path,
            "name": _text(value.get("name") or "首个候选产物", 300),
            "artifact_kind": _text(value.get("ext") or value.get("kind"), 80),
            "x": ("第一份候选产物已上报；你现在可以打开检查，军团会继续验证与返工"
                  if url else "第一份候选产物路径已记录；军团会继续读回验证与返工")})

    def observe_context(self, packet: dict | None, *, include_gaps: bool = True,
                        include_journey: bool = True) -> None:
        value = packet if isinstance(packet, dict) else {}
        references = _references(value)
        if references:
            self._once("reference_selected", {
                "t": "reference_selected", "stage": "context_loaded", "sources": references,
                "x": "军团已选择与本题直接相关的参照：" +
                     "、".join(row["name"] for row in references[:3])})
        quality = value.get("quality_context") if isinstance(value.get("quality_context"), dict) else {}
        current_review = (str(value.get("current_stage") or "") == "direction_reviewed" or
                          str(quality.get("change_reason") or "") == "legion_direction_review")
        gaps = _open_gaps(value) if include_gaps and current_review else []
        if gaps:
            gap = gaps[0]
            problem = _text(gap.get("problem") or gap.get("finding") or gap.get("defect"), 500)
            if problem:
                self._once("gap_identified", {
                    "t": "gap_identified", "stage": "verifying", "gap": gap,
                    "x": f"方向评审已记录当前优先 Gap：{problem}"})
        journey = [row for row in (value.get("journey_observations") or [])
                   if isinstance(row, dict)] if include_journey else []
        passed = [row for row in journey if (row.get("verified") is True or
                  str(row.get("status") or row.get("verdict") or "").lower()
                  in {"pass", "passed", "done", "completed"})
                  and bool(row.get("evidence_refs") or row.get("evidence") or row.get("artifact_paths"))]
        if journey and len(passed) == len(journey):
            self._once("journey_evidence_ready", {
                "t": "journey_evidence_ready", "stage": "verifying", "status": "reported",
                "checks": len(passed),
                "x": f"成员已提交 {len(passed)} 项旅程通过记录及证据引用，等待独立复验"})

    def revision(self, *, started: bool, owner: str = "", round_number: int = 1,
                 artifact_count: int = 0) -> None:
        kind = "revision_started" if started else "revision_candidate_ready"
        count = f"（{artifact_count} 件产物）" if artifact_count else ""
        self._once(f"{kind}:{round_number}", {
            "t": kind, "stage": "executing" if started else "verifying",
            "status": "running" if started else "ready",
            "owner": owner, "round": round_number, "artifact_count": artifact_count,
            "x": (f"{owner or '主创'} 正在针对已记录 Gap 返工作品" if started else
                  f"第 {round_number} 轮返工候选已交付{count}，等待独立复验变化")})

    def update(self, results: list[dict], verification_reports: list[dict] | None = None) -> dict:
        state = milestone(results, self.delivery_contract,
                          verification_required=self.verification_required,
                          verification_reports=verification_reports,
                          event_cb=self.emit)
        if state["contract_ready"]:
            self.sent_contract = True
            self._once("delivery_contract_ready", {
                "t": "delivery_contract_ready", "stage": "artifact_ready",
                "artifact_count": state["artifact_count"],
                "x": "用户要求的交付物已就绪" +
                     ("·继续完成必要验证" if self.verification_required else "")})
        if state["deliverable"]:
            self.sent_deliverable = True
            self._once("deliverable_ready", {
                "t": "deliverable_ready", "stage": "artifact_ready",
                "artifact_count": state["artifact_count"],
                "x": "交付合同与必要验证均已满足·结果可立即查看"})
        return state
