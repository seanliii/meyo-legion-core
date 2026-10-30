#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团统一工作台 ViewModel：任务板、组织图、审批、产物与平台入口。"""
from __future__ import annotations

import pathlib
import re
from typing import Any

import external_agent_registry as external
import team_state


def _task_title(text: str, key: str) -> str:
    """任务卡人话标题(用户 2026-09-07:右栏甩完整 prompt+JSON 看不懂):
    去掉 {json}/[json] 块与"以下是…:"引导语,取剩下的第一句;提炼不出退回 key。"""
    t = str(text or "")
    t = re.sub(r"\{[\s\S]*?\}", " ", t)
    t = re.sub(r"\[[\s\S]*?\]", " ", t)
    t = re.sub(r"以下是[^：]*[:：]", " ", t)
    t = re.sub(r"用户原始目标[:：]", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ，,；;。.")
    for sep in ("。", "；", "，"):
        if len(t) > 70 and sep in t[:90]:
            t = t[: t.index(sep)]
            break
    return (t[:70] or key) if t else key


def _platforms() -> list[dict[str, Any]]:
    rows = []
    for item in external.probe().get("agents", []):
        role = item.get("execution_role")
        if role not in ("control-plane", "ui-platform") and item["key"] != "cowagent":
            continue
        url = None
        if item["key"] == "cowagent" and item.get("available"):
            url = "http://127.0.0.1:9899"
        elif item["key"] == "paperclip" and item.get("available"):
            url = "http://127.0.0.1:3100"
        elif item["key"] == "grok-bot-reconstructed" and item.get("available"):
            url = "file:///Applications/Grok%20Bot%200.18%20Reconstructed.app"
        rows.append({"key": item["key"], "name": item["name"], "role": role,
                     "status": item["status"], "available": item["available"],
                     "url": url, "capabilities": item.get("capabilities") or [],
                     "provenance": item.get("provenance") or {"official": True}})
    return rows


def _graph(question: str, captain: str, tasks: list[dict], agents: list[dict],
           approvals: list[dict], artifacts: list[dict], legacy_edges: list[dict]) -> dict:
    nodes = [{"id": "objective:root", "type": "objective", "label": question or "当前目标",
              "status": "running"}]
    edges, blockers = [], []
    def add_edge(source: str, target: str, kind: str, reason: str, *,
                 status: str = "active", payload: str = "", evidence_count: int = 0) -> None:
        edges.append({"id": f"edge:{len(edges)}", "from": source, "to": target,
                      "type": kind, "reason": reason, "status": status,
                      "payload": payload, "evidence_count": int(evidence_count),
                      "updated_at": None})
    task_by_key = {row["key"]: row for row in tasks}
    for task in tasks:
        task_id = "task:" + task["key"]
        nodes.append({"id": task_id, "type": "task", "label": task.get("title") or task["text"] or task["key"],
                      "status": task["status"], "agent": task["key"],
                      "artifact_count": task["artifact_count"], "error": task["error"]})
        add_edge("objective:root", task_id, "contains", "目标拆分出的任务",
                 status="satisfied" if task["status"] == "done" else "active",
                 payload=task["text"] or task["key"], evidence_count=task["artifact_count"])
        if task["status"] in {"blocked", "failed", "error", "timeout", "waiting_approval"}:
            reason = task["error"] or ("等待用户批准" if task["status"] == "waiting_approval" else "任务受阻")
            blockers.append({"node_id": task_id, "reason": reason})
            add_edge(task_id, "objective:root", "blocks", reason, status="blocked", payload=reason)
        for dependency in task["dependencies"]:
            dep = task_by_key.get(dependency) or {}
            satisfied = dep.get("status") == "done"
            add_edge("task:" + dependency, task_id, "depends_on",
                     f"{task['key']} 需要 {dependency} 的产出",
                     status="satisfied" if satisfied else "waiting",
                     payload=f"{dependency} 的产物与事实", evidence_count=int(dep.get("artifact_count") or 0))
    for agent in agents:
        agent_id = "agent:" + agent["key"]
        nodes.append({"id": agent_id, "type": "agent", "label": agent["key"],
                      "status": agent["status"], "captain": agent["captain"]})
        if agent["key"] in task_by_key:
            task = task_by_key[agent["key"]]
            add_edge(agent_id, "task:" + agent["key"], "executes", "负责执行该任务",
                     status="satisfied" if task["status"] == "done" else "active",
                     payload=task["text"] or task["key"])
    for approval in approvals:
        approval_id = "approval:" + str(approval.get("id") or len(nodes))
        label = str(approval.get("capability") or approval.get("tool") or "需要用户确认")
        nodes.append({"id": approval_id, "type": "approval", "label": label,
                      "status": approval.get("status") or "pending", "reason": approval.get("reason") or ""})
        agent = str(approval.get("agent") or approval.get("key") or "")
        if agent:
            add_edge(approval_id, "task:" + agent, "approves",
                     approval.get("reason") or "高风险动作需要用户确认",
                     status="waiting" if approval.get("status") == "pending" else "satisfied",
                     payload=label)
    for index, artifact in enumerate(artifacts):
        artifact_id = str(artifact.get("artifact_id") or "artifact:" + str(index))
        evidence_id = str(artifact.get("evidence_id") or artifact_id.replace("artifact:", "evidence:", 1))
        name = str(artifact.get("name") or artifact.get("path") or "产物")
        provenance = artifact.get("provenance") if isinstance(artifact.get("provenance"), dict) else {}
        verified = bool(provenance.get("verified") and provenance.get("sha256"))
        nodes.append({"id": artifact_id, "type": "artifact", "label": name,
                      "status": "available", "path": artifact.get("path") or "",
                      "sha256": provenance.get("sha256") or "", "verified": verified})
        nodes.append({"id": evidence_id, "type": "evidence", "label": "文件校验 · " + name,
                      "status": "verified" if verified else "missing",
                      "sha256": provenance.get("sha256") or "",
                      "verification": provenance.get("verification") or "缺少可核验来源"})
        task_id = "task:" + str(artifact.get("task_id") or artifact.get("agent") or "")
        add_edge(task_id, artifact_id, "produces", "任务产生的交付物",
                 status="satisfied", payload=name, evidence_count=1 if verified else 0)
        add_edge(evidence_id, artifact_id, "verifies", "校验交付物内容与来源",
                 status="satisfied" if verified else "waiting",
                 payload=str(provenance.get("verification") or "尚无校验记录"),
                 evidence_count=1 if verified else 0)
    for edge in legacy_edges:
        if edge.get("kind") == "handoff":
            add_edge("agent:" + str(edge.get("from") or ""),
                     "agent:" + str(edge.get("to") or ""), "hands_off",
                     "失败或阶段性接力", status="satisfied", payload="现场、断点与已有产物")
    ordered = []
    remaining = {task["key"] for task in tasks}
    while remaining:
        ready = sorted(key for key in remaining
                       if all(dep not in remaining for dep in task_by_key[key]["dependencies"]))
        if not ready:
            ready = sorted(remaining)
        ordered.extend("task:" + key for key in ready)
        remaining.difference_update(ready)
    active = next((task for task in tasks if task["status"] in {"running", "blocked", "waiting_approval"}),
                  tasks[-1] if tasks else None)
    pending = next((item for item in approvals if item.get("status") == "pending"), None)
    evidence_names = [str(item.get("name") or item.get("path") or "") for item in artifacts]
    edge_summary = {status: sum(edge["status"] == status for edge in edges)
                    for status in ("active", "waiting", "blocked", "satisfied")}
    stages = []
    critical_keys = {item.removeprefix("task:") for item in ordered}
    for index, task_id in enumerate(ordered):
        key = task_id.removeprefix("task:")
        task = task_by_key[key]
        status = "done" if task["status"] == "done" else \
            "blocked" if task["status"] in {"blocked", "failed", "error", "timeout"} else \
            "waiting_user" if task["status"] == "waiting_approval" else \
            "running" if task["status"] == "running" else "queued"
        stages.append({"index": index + 1, "node_id": task_id,
                       "label": task.get("title") or task["text"] or key, "status": status,
                       "agent": key, "is_critical": key in critical_keys,
                       "next_action": "处理审批" if status == "waiting_user" else
                                      "查看阻塞" if status == "blocked" else
                                      "查看结果" if status == "done" else "等待执行"})
    levels = {
        "macro": {"nodes": [node["id"] for node in nodes if node["type"] in {"objective", "task"}],
                  "edges": [edge["id"] for edge in edges if edge["type"] in {"contains", "depends_on", "blocks"}]},
        "meso": {"nodes": [node["id"] for node in nodes if node["type"] in {"task", "agent", "approval", "artifact"}],
                 "edges": [edge["id"] for edge in edges if edge["type"] in {"executes", "approves", "produces", "hands_off", "depends_on", "uses_tool", "uses_skill"}]},
        "micro": {"nodes": [node["id"] for node in nodes], "edges": [edge["id"] for edge in edges]},
    }
    return {"nodes": nodes, "edges": edges, "critical_path": ordered, "blockers": blockers,
            "edge_summary": edge_summary, "levels": levels, "stages": stages,
            "filters": {
                "agents": sorted({node["label"] for node in nodes if node["type"] == "agent"}),
                "statuses": sorted({str(node.get("status") or "") for node in nodes if node.get("status")}),
                "types": sorted({node["type"] for node in nodes}),
            },
            "focus_node_id": "task:" + active["key"] if active else "objective:root",
            "answers": {
                "where": (active.get("title") or active["text"]) if active else "尚未拆分任务",
                "why": blockers[0]["reason"] if blockers else "当前没有阻塞",
                "who": active["key"] if active else captain,
                "user_action": ("请批准「" + str(pending.get("capability") or "待确认动作") + "」") if pending else "当前无需你处理",
                "evidence": "、".join(name for name in evidence_names if name) or "尚未产生可验证产物",
            }}


def build(team_id: str) -> dict:
    state = team_state.load(team_id)
    if not state:
        return {"ok": False, "error": "team not found", "team_id": team_id}
    captain = str(state.get("captain") or "unselected")
    tasks = []
    try:
        import approval_state
        approvals = approval_state.list_approvals(team_id).get("approvals") or []
    except Exception:
        approvals = []
    artifacts = []
    edges = []
    for key, task in (state.get("tasks") or {}).items():
        result = task.get("result") or {}
        task_artifacts = list(result.get("artifacts") or [])
        events = list(result.get("events") or [])
        tool_usage, skill_usage = [], []
        for event in events:
            if not isinstance(event, dict):
                continue
            tool = str(event.get("tool") or event.get("name") or "")
            if tool:
                row = {"tool": tool, "provider": event.get("provider") or event.get("source_harness"),
                       "status": event.get("status") or event.get("kind"), "ms": event.get("ms")}
                if tool.startswith("agent_tool:"):
                    parts = tool.split(":", 2); row["provider"] = parts[1] if len(parts) > 2 else row["provider"]
                tool_usage.append(row)
            skill = event.get("skill") or event.get("skill_name")
            if skill:
                skill_usage.append({"skill": skill, "source": event.get("source"), "version": event.get("version")})
        tasks.append({"key": key, "text": task.get("text", ""),
                      "title": _task_title(task.get("text", ""), key),
                      "status": task.get("status", "pending"),
                      "assigned_harness": task.get("assigned_harness") or task.get("agent") or key,
                      "dependencies": task.get("dependencies") or [], "attempt": task.get("attempt_seq", 0),
                      "started_at": task.get("started_at"), "finished_at": task.get("finished_at"),
                      "checkpoint": result.get("checkpoint") or result.get("native_checkpoint"),
                      "workdir": result.get("workdir"), "native_events": result.get("native_events") or [],
                      "context_budget": dict(result.get("context_budget") or {}),
                      "resumed_native": bool(result.get("resumed_native")),
                      "artifact_count": len(task_artifacts), "error": result.get("error", ""),
                      "model_used": result.get("model_used"), "elapsed": result.get("elapsed"),
                      "usage": result.get("usage"),
                      "tool_usage": tool_usage, "skill_usage": skill_usage,
                      "relay_by": result.get("relay_by"), "handoff_by": result.get("handoff_by")})
        artifacts.extend({"agent": key, "task_id": key, **artifact}
                         for artifact in task_artifacts if isinstance(artifact, dict))
        edges.extend({"from": dep, "to": key, "kind": "dependency"} for dep in task.get("dependencies") or [])
    for event in state.get("events") or []:
        if event.get("t") == "captain_handoff":
            edges.append({"from": event.get("previous"), "to": event.get("captain"), "kind": "handoff"})
    agents = []
    for key, member in (state.get("members") or {}).items():
        agents.append({"key": key, "status": member.get("status", "idle"),
                       "attempts": member.get("attempts", 0), "captain": key == captain})
        if key != captain:
            edges.append({"from": captain, "to": key, "kind": "command"})
    artifact_lineage = []
    for index, artifact in enumerate(artifacts):
        task_key = str(artifact.get("task_id") or artifact.get("agent") or "")
        task = next((item for item in tasks if item["key"] == task_key), {})
        provenance = artifact.get("provenance") if isinstance(artifact.get("provenance"), dict) else {}
        artifact_id = "artifact:" + str(index)
        evidence_id = artifact_id.replace("artifact:", "evidence:", 1)
        lineage = {"objective": {"id": "objective:root", "label": str(state.get("question") or "当前目标")},
                   "task": {"id": "task:" + task_key, "label": task.get("text") or task_key},
                   "agent": {"id": "agent:" + task_key, "label": task_key},
                   "tools": list(task.get("tool_usage") or []), "skills": list(task.get("skill_usage") or []),
                   "evidence": {"id": evidence_id, "verified": bool(provenance.get("verified") and provenance.get("sha256")),
                                "sha256": provenance.get("sha256") or "",
                                "verification": provenance.get("verification") or "缺少可核验来源"}}
        artifact.update({"artifact_id": artifact_id, "evidence_id": evidence_id, "lineage": lineage})
        artifact_lineage.append({"artifact_id": artifact_id, "name": artifact.get("name") or artifact.get("path") or "产物",
                                 **lineage})
    graph = _graph(str(state.get("question") or ""), captain, tasks, agents, approvals, artifacts, edges)
    graph_nodes = graph["nodes"]
    graph_edges = graph["edges"]
    for task in tasks:
        task_id = "task:" + task["key"]
        for index, usage in enumerate(task["tool_usage"]):
            tool = str(usage.get("tool") or "工具")
            node_id = f"tool:{task['key']}:{index}"
            graph_nodes.append({"id": node_id, "type": "tool", "label": tool,
                                "status": usage.get("status") or "used",
                                "provider": usage.get("provider") or task["key"]})
            graph_edges.append({"id": f"edge:{len(graph_edges)}", "from": task_id, "to": node_id,
                                "type": "uses_tool", "reason": "执行任务时调用工具",
                                "status": "satisfied", "payload": tool, "evidence_count": 1,
                                "updated_at": None})
        for index, usage in enumerate(task["skill_usage"]):
            skill = str(usage.get("skill") or "Skill")
            node_id = f"skill:{task['key']}:{index}"
            graph_nodes.append({"id": node_id, "type": "skill", "label": skill,
                                "status": "used", "version": usage.get("version") or ""})
            graph_edges.append({"id": f"edge:{len(graph_edges)}", "from": task_id, "to": node_id,
                                "type": "uses_skill", "reason": "执行任务时使用 Skill",
                                "status": "satisfied", "payload": skill, "evidence_count": 1,
                                "updated_at": None})
    graph["levels"]["micro"] = {"nodes": [node["id"] for node in graph_nodes],
                                  "edges": [edge["id"] for edge in graph_edges]}
    graph["filters"]["types"] = sorted({node["type"] for node in graph_nodes})
    return {"ok": True, "team_id": team_id, "question": state.get("question", ""),
            "captain": captain, "captain_generation": state.get("captain_generation", 0),
            "agents": agents, "tasks": tasks, "edges": edges, "graph": graph,
            "approvals": approvals, "artifacts": artifacts, "artifact_lineage": artifact_lineage,
            "platforms": _platforms(), "updated_at": state.get("updated_at")}
