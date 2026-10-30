#!/usr/bin/env python3
"""Append-only QA events and deterministic run-state projection."""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "qa-events-v1"
EVENT_TYPES = {
    "task_started", "context_interpreted", "goal_selected", "action_proposed",
    "tool_selected", "tool_started", "tool_succeeded", "tool_failed",
    "state_projected",
    "evidence_observed", "evidence_linked", "candidate_created",
    "candidate_updated", "candidate_compared", "candidate_rejected",
    "candidate_locked", "gap_updated", "checkpoint_saved",
    "checkpoint_restored", "answer_proposed", "answer_verified",
    "answer_delivered", "run_degraded", "run_stopped",
}
CANDIDATE_STATES = {
    "observed", "hypothesized", "investigated", "supported", "contradicted",
    "unresolved", "compared", "selected", "rejected", "delivery_verified",
}
_ALLOWED_TRANSITIONS = {
    "observed": {"hypothesized", "rejected"},
    "hypothesized": {"investigated", "rejected"},
    "investigated": {"supported", "contradicted", "unresolved", "rejected"},
    "supported": {"compared", "contradicted", "rejected"},
    "contradicted": {"investigated", "rejected"},
    "unresolved": {"investigated", "supported", "contradicted", "rejected"},
    "compared": {"selected", "rejected", "investigated"},
    "selected": {"delivery_verified", "contradicted", "rejected"},
    "rejected": set(),
    "delivery_verified": set(),
}
_LOCK = threading.Lock()


@dataclass(frozen=True)
class QAEvent:
    event_type: str
    run_id: str
    session_id: str
    question_id: str
    seq: int
    timestamp: float
    payload: dict[str, Any] = field(default_factory=dict)
    caused_by: int | None = None
    schema_version: str = SCHEMA_VERSION

    def validate(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unsupported event type: {self.event_type}")
        if not all((self.run_id, self.session_id, self.question_id)):
            raise ValueError("run_id, session_id and question_id are required")
        if self.seq < 1:
            raise ValueError("event seq must be positive")
        if self.caused_by is not None and self.caused_by >= self.seq:
            raise ValueError("caused_by must refer to an earlier event")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "QAEvent":
        event = cls(
            event_type=str(raw.get("event_type") or ""),
            run_id=str(raw.get("run_id") or ""),
            session_id=str(raw.get("session_id") or ""),
            question_id=str(raw.get("question_id") or ""),
            seq=int(raw.get("seq") or 0),
            timestamp=float(raw.get("timestamp") or 0),
            payload=dict(raw.get("payload") or {}),
            caused_by=raw.get("caused_by"),
            schema_version=str(raw.get("schema_version") or SCHEMA_VERSION),
        )
        event.validate()
        return event


class EventStore:
    def __init__(self, path: str | Path, *, run_id: str, session_id: str,
                 question_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self.session_id = session_id
        self.question_id = question_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> list[QAEvent]:
        if not self.path.exists():
            return []
        events = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(QAEvent.from_dict(json.loads(line)))
        for expected, event in enumerate(events, 1):
            if event.seq != expected:
                raise ValueError(f"non-contiguous event stream at seq {event.seq}")
        return events

    def append(self, event_type: str, payload: dict[str, Any] | None = None,
               *, caused_by: int | None = None) -> QAEvent:
        with _LOCK:
            seq = len(self.read()) + 1
            event = QAEvent(event_type, self.run_id, self.session_id,
                            self.question_id, seq, time.time(), payload or {}, caused_by)
            encoded = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event


@dataclass
class ProjectedState:
    goal: str = ""
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    tool_health: dict[str, dict[str, Any]] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    failed_paths: list[dict[str, Any]] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    answer: str = ""
    answer_candidate_id: str = ""
    verified_candidate_id: str = ""
    delivered_candidate_id: str = ""
    answer_verified: bool = False
    deliverable: bool = False
    degraded: bool = False
    stopped: bool = False
    last_seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_value(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _require_known_evidence(state: ProjectedState, evidence_ids: list[str]) -> None:
    unknown = sorted(set(evidence_ids) - set(state.evidence))
    if unknown:
        raise ValueError(f"unknown evidence references: {', '.join(unknown)}")


def _transition(state: ProjectedState, candidate: dict[str, Any], target: str,
                evidence_ids: list[str]) -> None:
    if target not in CANDIDATE_STATES:
        raise ValueError(f"unknown candidate state: {target}")
    current = str(candidate.get("state") or "observed")
    if target != current and target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"illegal candidate transition: {current} -> {target}")
    if target in {"supported", "compared", "selected", "delivery_verified"}:
        if not evidence_ids:
            raise ValueError(f"candidate state {target} requires evidence references")
        _require_known_evidence(state, evidence_ids)
    candidate["state"] = target
    candidate["evidence_ids"] = sorted(set(candidate.get("evidence_ids", []) + evidence_ids))


def project(events: Iterable[QAEvent | dict[str, Any]]) -> ProjectedState:
    state = ProjectedState()
    expected = 1
    for raw in events:
        event = raw if isinstance(raw, QAEvent) else QAEvent.from_dict(raw)
        if event.seq != expected:
            raise ValueError(f"non-contiguous event stream at seq {event.seq}")
        expected += 1
        state.last_seq = event.seq
        payload = event.payload
        if event.event_type == "goal_selected":
            state.goal = str(payload.get("goal") or "")
        elif event.event_type in {"action_proposed", "tool_selected", "tool_started",
                                  "tool_succeeded", "tool_failed"}:
            state.actions.append({"seq": event.seq, "event_type": event.event_type, **payload})
            if event.event_type == "tool_failed" or not payload.get("state_changed", True):
                state.failed_paths.append({"seq": event.seq, "event_type": event.event_type, **payload})
            if event.event_type in {"tool_succeeded", "tool_failed"}:
                tool = str(payload.get("tool") or "unknown")
                health = state.tool_health.setdefault(tool, {"successes": 0, "failures": 0})
                health["successes" if event.event_type == "tool_succeeded" else "failures"] += 1
        elif event.event_type == "state_projected":
            state.actions.append({"seq": event.seq, "event_type": event.event_type, **payload})
            if not payload.get("state_changed", False):
                state.failed_paths.append({"seq": event.seq, "event_type": event.event_type,
                                           **payload})
        elif event.event_type in {"evidence_observed", "evidence_linked"}:
            evidence_id = str(payload.get("evidence_id") or "")
            if evidence_id:
                state.evidence[evidence_id] = dict(payload)
        elif event.event_type == "candidate_created":
            candidate_id = str(payload.get("candidate_id") or "")
            if not candidate_id:
                raise ValueError("candidate_created requires candidate_id")
            evidence_ids = list(payload.get("evidence_ids") or [])
            _require_known_evidence(state, evidence_ids)
            state.candidates[candidate_id] = {
                **payload, "state": "observed", "evidence_ids": evidence_ids,
                "supporting_evidence_ids": evidence_ids, "contradicting_evidence_ids": [],
                "unresolved_constraints": list(payload.get("unresolved_constraints") or []),
            }
        elif event.event_type in {"candidate_updated", "candidate_compared",
                                  "candidate_rejected", "candidate_locked"}:
            candidate_id = str(payload.get("candidate_id") or "")
            if candidate_id not in state.candidates:
                raise ValueError(f"unknown candidate: {candidate_id}")
            default = {"candidate_updated": "investigated", "candidate_compared": "compared",
                       "candidate_rejected": "rejected", "candidate_locked": "selected"}[event.event_type]
            evidence_ids = list(payload.get("evidence_ids") or [])
            _transition(state, state.candidates[candidate_id], str(payload.get("state") or default),
                        evidence_ids)
            state.candidates[candidate_id].update({k: v for k, v in payload.items()
                                                   if k not in {"state", "evidence_ids"}})
            candidate = state.candidates[candidate_id]
            candidate["supporting_evidence_ids"] = sorted(set(
                candidate.get("supporting_evidence_ids", [])
                + list(payload.get("supporting_evidence_ids") or evidence_ids)))
            candidate["contradicting_evidence_ids"] = sorted(set(
                candidate.get("contradicting_evidence_ids", [])
                + list(payload.get("contradicting_evidence_ids") or [])))
            candidate["unresolved_constraints"] = list(
                payload.get("unresolved_constraints", candidate.get("unresolved_constraints", [])) or [])
        elif event.event_type == "gap_updated":
            state.gaps = list(payload.get("gaps") or [])
        elif event.event_type == "answer_proposed":
            state.answer = str(payload.get("answer") or "")
            state.answer_candidate_id = str(payload.get("candidate_id") or "")
        elif event.event_type == "answer_verified":
            state.answer_verified = bool(payload.get("passed"))
            candidate_id = str(payload.get("candidate_id") or "")
            evidence_ids = list(payload.get("evidence_ids") or [])
            if state.answer_verified:
                if candidate_id not in state.candidates:
                    raise ValueError(f"unknown candidate: {candidate_id}")
                candidate = state.candidates[candidate_id]
                if candidate.get("state") != "selected":
                    raise ValueError("answer verification requires selected candidate")
                if state.answer_candidate_id != candidate_id:
                    raise ValueError("verified candidate does not match proposed answer candidate")
                if _normalize_value(state.answer) != _normalize_value(candidate.get("value")):
                    raise ValueError("proposed answer does not match selected candidate")
                if candidate.get("contradicting_evidence_ids") or candidate.get("unresolved_constraints"):
                    raise ValueError("candidate has unresolved contradictions or constraints")
                _transition(state, candidate, "delivery_verified", evidence_ids)
                state.verified_candidate_id = candidate_id
        elif event.event_type == "answer_delivered":
            candidate_id = str(payload.get("candidate_id") or "")
            if not state.answer_verified or candidate_id != state.verified_candidate_id:
                raise ValueError("answer cannot be delivered before matching verification")
            if _normalize_value(payload.get("answer")) != _normalize_value(state.answer):
                raise ValueError("delivered answer does not match verified answer")
            state.deliverable = True
            state.delivered_candidate_id = candidate_id
        elif event.event_type == "run_degraded":
            state.degraded = True
            state.actions.append({"seq": event.seq, "event_type": event.event_type, **payload})
        elif event.event_type == "run_stopped":
            state.stopped = True
        if "remaining_budget" in payload:
            state.budget = dict(payload["remaining_budget"])
    return state


def replay_file(path: str | Path) -> ProjectedState:
    raw = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
           if line.strip()]
    return project(raw)


def evidence_id(source: Any, content: Any) -> str:
    """Return the stable ID used by every engine and candidate reference."""
    import hashlib
    material = "%s\n%s" % (str(source or "").strip(), str(content or "").strip())
    return "ev_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


class RunState:
    """Single write/read boundary for canonical QA state.

    Engine adapters may still expose legacy session fields during migration, but new
    decisions and delivery consume this service's deterministic projection.
    """

    def __init__(self, store: EventStore):
        self.store = store

    @classmethod
    def from_session(cls, session: dict[str, Any]) -> "RunState":
        path = str(session.get("_event_log_path") or "")
        if not path:
            raise ValueError("session has no canonical event log")
        qid = str(session.get("qid") or "unknown")
        return cls(EventStore(
            path, run_id=str(session.get("run_id") or qid),
            session_id=str(session.get("session_id") or qid), question_id=qid))

    def state(self) -> ProjectedState:
        return project(self.store.read())

    def observe_tool_result(self, *, tool: str, arguments: dict[str, Any],
                            summary: str, succeeded: bool,
                            elapsed_seconds: float = 0.0,
                            evidence_records: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
        """Persist one tool observation and return its deterministic projection delta."""
        before = self.state()
        started = self.store.append("tool_started", {"tool": tool, "arguments": arguments})
        finished = self.store.append("tool_succeeded" if succeeded else "tool_failed", {
            "tool": tool, "arguments": arguments, "summary": str(summary)[:1000],
            "elapsed_seconds": round(float(elapsed_seconds or 0), 4),
        }, caused_by=started.seq)
        observed_ids = []
        for record in evidence_records:
            content = "\n".join(str(record.get(key) or "") for key in
                                  ("title", "content", "snippet", "url"))[:6000]
            if content.strip():
                observed = self.observe_evidence(
                    source=str(record.get("url") or record.get("source") or tool),
                    content=content, title=str(record.get("title") or "")[:500],
                    tool=tool, caused_by=finished.seq)
                observed_ids.append(observed.payload["evidence_id"])
        after = self.state()
        delta = {
            "tool_event_seq": finished.seq,
            "evidence_ids": observed_ids,
            "evidence_added": max(0, len(after.evidence) - len(before.evidence)),
            "candidates_added": max(0, len(after.candidates) - len(before.candidates)),
            "gaps_before": list(before.gaps), "gaps_after": list(after.gaps),
        }
        delta["state_changed"] = bool(
            delta["evidence_added"] or delta["candidates_added"]
            or delta["gaps_before"] != delta["gaps_after"])
        projected = self.store.append("state_projected", delta, caused_by=finished.seq)
        delta["projection_event_seq"] = projected.seq
        return delta

    def observe_evidence(self, *, source: str, content: str, title: str = "",
                         tool: str = "", caused_by: int | None = None,
                         extra: dict[str, Any] | None = None) -> QAEvent:
        eid = evidence_id(source, content)
        current = self.state()
        if eid in current.evidence:
            existing = current.evidence[eid]
            return QAEvent(
                "evidence_observed", self.store.run_id, self.store.session_id,
                self.store.question_id, current.last_seq, float(existing.get("timestamp") or 0),
                existing)
        payload = {"evidence_id": eid, "source": source, "content": content,
                   "title": title, "tool": tool}
        payload.update(extra or {})
        return self.store.append("evidence_observed", payload, caused_by=caused_by)

    def observe_candidate(self, value: str, *, origin: str,
                          evidence_ids: list[str] | None = None,
                          unresolved_constraints: list[str] | None = None) -> str:
        """Idempotently record a model-interpreted candidate from observed evidence."""
        return self.create_candidate(
            value, origin=origin, evidence_ids=evidence_ids,
            unresolved_constraints=unresolved_constraints)

    def nominate_from_observation(self, *, candidate: str, origin: str,
                                  relation_path: str,
                                  evidence_records: Iterable[dict[str, Any]],
                                  unresolved_constraints: list[str] | None = None) -> str | None:
        """Promote a SoulDriver candidate only when the observation grounds it."""
        value = str(candidate or "").strip()
        relation = str(relation_path or "").strip()
        if not value or not relation:
            return None
        ids = []
        value_key = _normalize_value(value)
        for record in evidence_records:
            content = "\n".join(str(record.get(key) or "") for key in
                                  ("title", "content", "snippet", "url"))[:6000]
            if value_key not in _normalize_value(content):
                continue
            observed = self.observe_evidence(
                source=str(record.get("url") or record.get("source") or origin),
                content=content, title=str(record.get("title") or "")[:500], tool=origin)
            ids.append(observed.payload["evidence_id"])
        if not ids:
            return None
        return self.observe_candidate(
            value, origin=origin, evidence_ids=sorted(set(ids)),
            unresolved_constraints=unresolved_constraints)

    def create_candidate(self, value: str, *, origin: str,
                         evidence_ids: list[str] | None = None,
                         unresolved_constraints: list[str] | None = None) -> str:
        import hashlib
        candidate_id = "cand_" + hashlib.sha256(
            _normalize_value(value).encode("utf-8")).hexdigest()[:12]
        state = self.state()
        if candidate_id in state.candidates:
            return candidate_id
        event = self.store.append("candidate_created", {
            "candidate_id": candidate_id, "value": str(value).strip(), "origin": origin,
            "evidence_ids": list(evidence_ids or []),
            "unresolved_constraints": list(unresolved_constraints or []),
        })
        self.store.append("candidate_updated", {
            "candidate_id": candidate_id, "state": "hypothesized"}, caused_by=event.seq)
        return candidate_id

    def transition_candidate(self, candidate_id: str, state: str, *,
                             evidence_ids: list[str] | None = None,
                             supporting_evidence_ids: list[str] | None = None,
                             contradicting_evidence_ids: list[str] | None = None,
                             unresolved_constraints: list[str] | None = None,
                             reason: str = "") -> QAEvent:
        event_type = {"compared": "candidate_compared", "selected": "candidate_locked",
                      "rejected": "candidate_rejected"}.get(state, "candidate_updated")
        return self.store.append(event_type, {
            "candidate_id": candidate_id, "state": state,
            "evidence_ids": list(evidence_ids or []),
            "supporting_evidence_ids": list(supporting_evidence_ids or []),
            "contradicting_evidence_ids": list(contradicting_evidence_ids or []),
            "unresolved_constraints": list(unresolved_constraints or []),
            "reason": reason,
        })

    def update_gaps(self, gaps: list[str], *, reason: str = "") -> QAEvent:
        return self.store.append("gap_updated", {"gaps": list(gaps), "reason": reason})

    def decision_context(self) -> dict[str, Any]:
        state = self.state()
        return {
            "goal": state.goal,
            "evidence": state.evidence,
            "candidates": state.candidates,
            "gaps": state.gaps,
            "failed_paths": state.failed_paths[-12:],
            "actions": state.actions[-24:],
            "tool_health": state.tool_health,
            "budget": state.budget,
            "last_seq": state.last_seq,
        }

    def project_legacy_views(self, session: dict[str, Any]) -> ProjectedState:
        """Populate read-only compatibility views from canonical events."""
        state = self.state()
        session["_canonical_projection"] = state.to_dict()
        session["tool_log"] = [
            {"tool": action.get("tool", ""), "args": action.get("arguments", {}),
             "summary": action.get("summary", ""),
             "elapsed_s": action.get("elapsed_seconds", 0),
             "event_seq": action.get("seq"), "derived_from": "canonical_projection"}
            for action in state.actions
            if action.get("event_type") in {"tool_succeeded", "tool_failed"}
        ][-400:]
        session["loop_events"] = [
            {"ev": ("hard_cap" if action.get("event_type") == "run_degraded"
                    and action.get("reason") == "hard_cap" else action.get("event_type")),
             "tool": action.get("tool", ""), "which": action.get("which"),
             "step": action.get("step"), "llm_calls": action.get("llm_calls"),
             "seq": action.get("seq"), "state_changed": action.get("state_changed"),
             "derived_from": "canonical_projection"}
            for action in state.actions
        ][-400:]
        session["_missing_constraints"] = list(state.gaps)
        session["_pilot_failed_paths"] = list(state.failed_paths[-12:])
        session["_event_candidate_ids"] = list(state.candidates)
        session["_candidate_hypotheses"] = [
            {"name": item.get("value", ""), "candidate_id": candidate_id,
             "state": item.get("state"), "relation_path": item.get("reason", ""),
             "source": "canonical_projection"}
            for candidate_id, item in state.candidates.items()
            if item.get("state") not in {"rejected", "delivery_verified"}
        ][-20:]
        session["identity_states"] = {
            candidate_id: {
                "entity": item.get("value", ""), "state": item.get("state"),
                "reason": item.get("reason", ""),
                "coverage": {"evidence_ids": list(item.get("evidence_ids") or [])},
                "derived_from": "canonical_projection",
            }
            for candidate_id, item in state.candidates.items()
        }
        selected = [item for item in state.candidates.values()
                    if item.get("state") in {"selected", "delivery_verified"}]
        if selected:
            item = selected[0]
            session["_answer_vault"] = {
                "answer": item.get("value", ""), "strength": 2,
                "tool": "canonical_projection", "verified": item.get("state") == "delivery_verified",
            }
            session["_ga_verified_pass"] = {
                "cand": item.get("value", ""), "cand_key": _normalize_value(item.get("value")),
                "evidence_ids": list(item.get("evidence_ids") or []),
                "derived_from": "canonical_projection",
            }
        else:
            session.pop("_answer_vault", None)
            session.pop("_ga_verified_pass", None)
        return state

    def verify_and_deliver(self, candidate_id: str, answer: str) -> ProjectedState:
        state = self.state()
        candidate = state.candidates.get(candidate_id)
        if not candidate or candidate.get("state") != "selected":
            raise ValueError("delivery requires one selected candidate")
        if _normalize_value(answer) != _normalize_value(candidate.get("value")):
            raise ValueError("answer does not match selected candidate")
        if candidate.get("contradicting_evidence_ids") or candidate.get("unresolved_constraints"):
            raise ValueError("candidate has unresolved contradictions or constraints")
        evidence_ids = list(candidate.get("evidence_ids") or [])
        proposed = self.store.append("answer_proposed", {
            "answer": candidate.get("value"), "candidate_id": candidate_id})
        verified = self.store.append("answer_verified", {
            "passed": True, "candidate_id": candidate_id, "evidence_ids": evidence_ids,
        }, caused_by=proposed.seq)
        self.store.append("answer_delivered", {
            "answer": candidate.get("value"), "candidate_id": candidate_id,
        }, caused_by=verified.seq)
        return self.state()
