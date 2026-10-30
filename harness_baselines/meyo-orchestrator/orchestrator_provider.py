#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一首脑可靠性层：超时、换人、证据续接和交付门。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import orchestrator
from meyo_harnesses import health

TERMINAL = frozenset({"completed", "failed", "cancelled", "idle"})


class OrchestratorProvider(Protocol):
    def list_agents(self) -> list[dict[str, Any]]: ...
    def create_child(self, parent_id: str, agent_id: str, title: str) -> dict[str, Any]: ...
    def send(self, session_id: str, message: str) -> dict[str, Any]: ...
    def get_status(self, session_id: str) -> dict[str, Any]: ...
    def get_history(self, session_id: str, limit: int = 30) -> list[dict[str, Any]]: ...
    def cancel(self, session_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DeliveryContract:
    required_files: tuple[str, ...] = ()
    required_answer_terms: tuple[str, ...] = ()
    min_answer_chars: int = 1

    def evaluate(self, answer: str, root: Path | None = None) -> dict[str, Any]:
        missing_files = []
        for value in self.required_files:
            path = Path(value)
            if not path.is_absolute() and root is not None:
                path = root / path
            if not path.is_file() or path.stat().st_size == 0:
                missing_files.append(str(path))
        missing_terms = [term for term in self.required_answer_terms if term not in answer]
        return {
            "passed": len(answer.strip()) >= self.min_answer_chars and not missing_files and not missing_terms,
            "missing_files": missing_files,
            "missing_answer_terms": missing_terms,
            "answer_chars": len(answer.strip()),
        }


@dataclass(frozen=True)
class RecoveryPolicy:
    self_heal_attempts: int = 1
    successor_heal_attempts: int = 1
    context_required_fields: tuple[str, ...] = (
        "task", "failure", "evidence", "attempts", "delivery_contract"
    )


@dataclass
class ChildRun:
    session_id: str
    agent_id: str
    agent_name: str
    title: str
    started_at: float
    last_activity_at: float
    attempts: list[str] = field(default_factory=list)


class HttpOmnigentProvider:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _request(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=payload,
            headers={"Content-Type": "application/json"} if payload else {},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)

    def list_agents(self) -> list[dict[str, Any]]:
        """合并模板 Agent 与 session-scoped Agent；按名称保留最新可用身份。"""
        templates = list(self._request("/v1/agents?limit=1000").get("data") or [])
        sessions = list(self._request("/v1/sessions?limit=1000").get("data") or [])
        by_name: dict[str, dict[str, Any]] = {}
        for agent in templates:
            name = str(agent.get("name") or "")
            if name:
                by_name[name] = dict(agent)
        for session in sessions:
            name = str(session.get("agent_name") or "")
            agent_id = session.get("agent_id")
            if not name or not agent_id:
                continue
            row = by_name.setdefault(name, {"name": name, "id": agent_id})
            row.setdefault("id", agent_id)
            if "session_id" not in row:
                row["session_id"] = session.get("id")
                row["session_status"] = session.get("status")
                row["runner_id"] = session.get("runner_id")
                row["runner_online"] = session.get("runner_online")
                row["updated_at"] = session.get("updated_at")
        return list(by_name.values())

    def resolve_agent(self, name: str) -> dict[str, Any] | None:
        matches = [agent for agent in self.list_agents() if agent.get("name") == name]
        if not matches:
            return None
        return max(matches, key=lambda item: int(item.get("updated_at") or 0))

    def ensure_parent(self, agent_name: str, *, workspace: str, title: str) -> dict[str, Any]:
        agent = self.resolve_agent(agent_name)
        if agent is None:
            raise LookupError(f"registered agent not found: {agent_name}")
        existing = agent.get("session_id")
        if existing and agent.get("runner_online") is True:
            return self.get_status(str(existing))
        hosts = list(self._request("/v1/hosts").get("hosts") or [])
        host = next((item for item in hosts if item.get("status") == "online"), None)
        if host is None:
            raise RuntimeError("no_online_host")
        return self._request("/v1/sessions", {
            "agent_id": agent["id"], "title": title,
            "workspace": workspace, "host_id": host["host_id"],
        })

    def create_child(self, parent_id: str, agent_id: str, title: str) -> dict[str, Any]:
        return self._request("/v1/sessions", {
            "agent_id": agent_id, "parent_session_id": parent_id, "title": title,
        })

    def send(self, session_id: str, message: str) -> dict[str, Any]:
        return self._request(f"/v1/sessions/{session_id}/events", {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": message}]},
        })

    def get_status(self, session_id: str) -> dict[str, Any]:
        return self._request(f"/v1/sessions/{session_id}")

    def get_history(self, session_id: str, limit: int = 30) -> list[dict[str, Any]]:
        quoted = urllib.parse.urlencode({"limit": limit, "order": "asc"})
        return list(self._request(f"/v1/sessions/{session_id}/items?{quoted}").get("data") or [])

    def cancel(self, session_id: str) -> dict[str, Any]:
        try:
            return self._request(f"/v1/sessions/{session_id}/events", {
                "type": "stop_session", "data": {},
            })
        except urllib.error.HTTPError:
            return self._request(f"/v1/sessions/{session_id}/events", {
                "type": "interrupt", "data": {},
            })


def assistant_text(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        if item.get("type") == "function_call_output":
            output = item.get("output")
            if isinstance(output, str) and output.strip():
                parts.append(output)
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(parts)


def healthy_agent_candidates(provider: OrchestratorProvider, excluded: set[str]) -> list[dict[str, Any]]:
    registry = {row["key"]: row for row in orchestrator.list_harnesses() if row.get("available")}
    candidates = []
    for agent in provider.list_agents():
        name = str(agent.get("name") or "")
        if not name.startswith("meyo-") or name in excluded:
            continue
        key = name.removeprefix("meyo-")
        if key in registry and not health.circuit_open(key):
            candidates.append(agent)
    return candidates


class DeterministicSupervisor:
    def __init__(self, provider: OrchestratorProvider, *, child_timeout_s: float = 180.0,
                 poll_s: float = 2.0, max_replacements: int = 1,
                 recovery_policy: RecoveryPolicy | None = None,
                 event_sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.provider = provider
        self.child_timeout_s = child_timeout_s
        self.poll_s = poll_s
        self.max_replacements = max_replacements
        self.recovery_policy = recovery_policy or RecoveryPolicy()
        self.event_sink = event_sink

    def run_child(self, parent_id: str, agent_id: str, agent_name: str, title: str,
                  task: str, delivery_contract: dict[str, Any] | None = None) -> dict[str, Any]:
        used = {agent_name}
        evidence: list[str] = []
        attempts: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        current_id, current_agent, current_name = agent_id, agent_id, agent_name
        contract = dict(delivery_contract or {})
        for replacement in range(self.max_replacements + 1):
            attempt_title = title if replacement == 0 else f"{title}-retry-{replacement + 1}"
            created = self.provider.create_child(parent_id, current_id, attempt_title)
            session_id = str(created.get("id") or created.get("conversation_id"))
            packet = self._context_packet(task, evidence, attempts, contract, failure=None)
            self.provider.send(session_id, packet)
            self._emit(events, self._event("run", session_id, current_name, replacement))
            result = self._observe(session_id)
            attempts.append({"session_id": session_id, "agent": current_name, **result})
            if result["answer"] and self._delivery_passed(result["answer"], contract):
                return self._completed(result["answer"], attempts, events, contract)
            if result["answer"]:
                result["status"] = "delivery_failed"
                result["delivery_gaps"] = self._delivery_gaps(result["answer"], contract)
                evidence.append(result["answer"])

            # 原成员先获得一次自诊断、自修机会；handoff 不是第一反应。
            for heal_index in range(self.recovery_policy.self_heal_attempts):
                failure = self._failure_packet(result, session_id, current_name)
                self._emit(events, self._event("diagnose", session_id, current_name, heal_index))
                self.provider.send(
                    session_id,
                    self._self_heal_prompt(task, evidence, attempts, contract, failure),
                )
                self._emit(events, self._event("self_repair", session_id, current_name, heal_index))
                result = self._observe(session_id)
                attempts.append({"session_id": session_id, "agent": current_name,
                                 "heal": heal_index + 1, **result})
                if result["answer"] and self._delivery_passed(result["answer"], contract):
                    return self._completed(result["answer"], attempts, events, contract)
                if result["answer"]:
                    result["status"] = "delivery_failed"
                    result["delivery_gaps"] = self._delivery_gaps(result["answer"], contract)
                    evidence.append(result["answer"])

            self.provider.cancel(session_id)
            self._emit(events, self._event("cancel", session_id, current_name, replacement))
            if result["answer"]:
                evidence.append(result["answer"])
            if replacement >= self.max_replacements:
                break
            candidates = healthy_agent_candidates(self.provider, used)
            if not candidates:
                break
            chosen = candidates[0]
            self._emit(events, self._event("handoff", session_id, current_name, replacement,
                                      to=str(chosen["name"])))
            current_id = str(chosen["id"])
            current_name = str(chosen["name"])
            used.add(current_name)
        return self._failed(attempts, events, contract)

    def _observe(self, session_id: str) -> dict[str, Any]:
        last_activity = time.monotonic()
        last_count = -1
        while True:
            status = self.provider.get_status(session_id)
            history = self.provider.get_history(session_id)
            if len(history) != last_count:
                last_activity = time.monotonic()
                last_count = len(history)
            answer = assistant_text(history)
            state = str(status.get("status") or "")
            if answer or state in {"completed", "failed", "cancelled"}:
                return {"status": state or "completed", "answer": answer,
                        "history_items": len(history)}
            if time.monotonic() - last_activity >= self.child_timeout_s:
                return {"status": "timeout", "answer": answer,
                        "history_items": len(history)}
            time.sleep(self.poll_s)

    @staticmethod
    def _delivery_evaluation(answer: str, contract: dict[str, Any]) -> dict[str, Any]:
        check = DeliveryContract(
            required_files=tuple(contract.get("required_files") or ()),
            required_answer_terms=tuple(contract.get("required_answer_terms") or ()),
            min_answer_chars=int(contract.get("min_answer_chars") or 1),
        )
        root = Path(contract["root"]) if contract.get("root") else None
        return check.evaluate(answer, root)

    def _delivery_passed(self, answer: str, contract: dict[str, Any]) -> bool:
        return self._delivery_evaluation(answer, contract)["passed"]

    def _delivery_gaps(self, answer: str, contract: dict[str, Any]) -> dict[str, Any]:
        return self._delivery_evaluation(answer, contract)

    def _context_packet(self, task: str, evidence: list[str], attempts: list[dict[str, Any]],
                        contract: dict[str, Any], failure: dict[str, Any] | None) -> str:
        payload = {"task": task, "failure": failure, "evidence": evidence[-6:],
                   "attempts": attempts[-6:], "delivery_contract": contract}
        return "【可自愈任务上下文】\n" + json.dumps(payload, ensure_ascii=False, indent=2)

    def _self_heal_prompt(self, task: str, evidence: list[str], attempts: list[dict[str, Any]],
                          contract: dict[str, Any], failure: dict[str, Any]) -> str:
        return self._context_packet(task, evidence, attempts, contract, failure) + (
            "\n请先诊断刚才失败的根因，使用已有工具/源码/上下文自主修复后继续原任务。"
            "不要只解释错误；必须实际重试并满足 delivery_contract。")

    @staticmethod
    def _failure_packet(result: dict[str, Any], session_id: str, agent: str) -> dict[str, Any]:
        return {"session_id": session_id, "agent": agent,
                "status": result.get("status"), "history_items": result.get("history_items")}

    def _emit(self, events: list[dict[str, Any]], event: dict[str, Any]) -> None:
        events.append(event)
        if self.event_sink is not None:
            self.event_sink(dict(event))

    @staticmethod
    def _event(stage: str, session_id: str, agent: str, sequence: int, **extra) -> dict[str, Any]:
        return {"stage": stage, "session_id": session_id, "agent": agent,
                "sequence": sequence, "at": time.time(), **extra}

    def _completed(self, answer: str, attempts: list[dict[str, Any]],
                   events: list[dict[str, Any]], contract: dict[str, Any]) -> dict[str, Any]:
        self._emit(events, self._event("delivery", attempts[-1]["session_id"],
                                  attempts[-1]["agent"], len(events)))
        return {"status": "completed", "answer": answer, "attempts": attempts,
                "events": events, "scores": self._scores(attempts, events, contract)}

    def _failed(self, attempts: list[dict[str, Any]], events: list[dict[str, Any]],
                contract: dict[str, Any]) -> dict[str, Any]:
        return {"status": "failed", "answer": "", "attempts": attempts,
                "events": events, "scores": self._scores(attempts, events, contract)}

    def _scores(self, attempts: list[dict[str, Any]], events: list[dict[str, Any]],
                contract: dict[str, Any]) -> dict[str, float]:
        stages = {event["stage"] for event in events}
        healed = any(attempt.get("heal") and attempt.get("answer") for attempt in attempts)
        autonomy = 1.0 if healed or (attempts and attempts[-1].get("answer")) else 0.0
        required = set(self.recovery_policy.context_required_fields)
        packet_fields = {"task", "failure", "evidence", "attempts", "delivery_contract"}
        context_integrity = len(required & packet_fields) / max(1, len(required))
        flow_required = {"run", "delivery"} if attempts and attempts[-1].get("answer") else {"run", "diagnose", "self_repair", "cancel"}
        flow_integrity = len(flow_required & stages) / len(flow_required)
        return {"autonomy": autonomy, "context_integrity": context_integrity,
                "flow_integrity": flow_integrity}
