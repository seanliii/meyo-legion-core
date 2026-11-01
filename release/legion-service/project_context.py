#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目级共享现场：把目标、DAG、证据、决定与产物作为成员共同事实源。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import contextlib
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import legion_paths as _paths

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

ROOT = Path(os.environ.get("LEGION_PROJECT_CONTEXT_DIR") or _paths.under("project_context"))
_MAX_INLINE = 4000
_MAX_ITEMS = 80
_FRONTIER_START = "<<LEGION_FRONTIER_CONTEXT>>"
_FRONTIER_END = "<<END>>"
_LOCK = threading.RLock()
_CONTEXT_LOCKS: dict[str, threading.RLock] = {}


def _root() -> Path:
    return Path(os.environ.get("LEGION_PROJECT_CONTEXT_DIR") or _paths.under("project_context"))


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "project"))[:120] or "project"


def _path(team_id: str) -> Path:
    return _root() / f"{_safe(team_id)}.json"


def _locks_dir() -> Path:
    return _root() / ".locks"


@contextlib.contextmanager
def _packet_lock(team_id: str):
    key = _safe(team_id)
    with _LOCK:
        thread_lock = _CONTEXT_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        if _fcntl is None:
            yield
            return
        lock_dir = _locks_dir()
        lock_dir.mkdir(parents=True, exist_ok=True)
        with (lock_dir / f"{key}.lock").open("a+", encoding="utf-8") as handle:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load(team_id: str) -> dict:
    try:
        value = json.loads(_path(team_id).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _task_node(task_id: str, value: object, dependencies: dict[str, list[str]]) -> dict:
    node = dict(value) if isinstance(value, dict) else {"task": str(value or "回答问题")}
    return {
        "task_id": str(task_id),
        "role": str(node.get("task") or node.get("prompt") or node.get("role") or "回答问题")[:_MAX_INLINE],
        "assigned_harness": str(node.get("assigned_harness") or node.get("assign_to") or
                                node.get("harness") or node.get("agent") or task_id),
        "depends_on": [str(item) for item in (dependencies.get(str(task_id)) or node.get("depends_on") or [])],
        "dependency_policy": str(node.get("dependency_policy") or "all_succeeded"),
        "verification_role": str(node.get("verification_role") or ""),
    }


def require_owner(record: dict | None, user_id: str = "", conversation_id: str = "") -> None:
    """沿用 sessions.continuation_project_context 的同会话匿名归属约定。"""
    owner = str((record or {}).get("user_id") or "")
    caller = str(user_id or "")
    if not owner or not caller:
        return  # 尚未绑定，或未指定用户的内部维护；调用者须保留已有 owner。
    previous_sid = str((record or {}).get("conversation_id") or "")
    sid = str(conversation_id or previous_sid)
    if owner == caller:
        if owner == "default" and previous_sid and sid != previous_sid:
            raise PermissionError("persisted context belongs to another user or conversation")
        if owner.startswith("sess:") and any(
                value and owner != "sess:" + value for value in (previous_sid, sid)):
            raise PermissionError("persisted context belongs to another user or conversation")
        return
    if previous_sid and sid == previous_sid and {owner, caller} <= {"default", "sess:" + sid}:
        return
    raise PermissionError("persisted context belongs to another user or conversation")


def initialize(team_id: str, objective: str, plan: dict, *, mode: str,
               user_id: str = "", conversation_id: str = "") -> dict:
    """首次认领和后续更新共用已有跨线程/进程锁，覆盖读、判、写。"""
    with _packet_lock(team_id):
        return _initialize_locked(team_id, objective, plan, mode=mode,
                                  user_id=user_id, conversation_id=conversation_id)


def record_task_bindings(team_id: str, tasks: dict, *, user_id: str,
                         conversation_id: str) -> dict:
    """Persist the scheduler's actual assignments without replaying a stale plan.

    This updates task facts only. It cannot create a project, change its owner
    or workspace, or supply an export grant. Other facts may have arrived while
    discovery/binding was running, so re-read them under the existing lock.
    """
    with _packet_lock(team_id):
        packet = load(team_id)
        if not packet:
            raise LookupError("project_context_missing")
        require_owner(packet, user_id, conversation_id)
        if packet.get("conversation_id") != conversation_id:
            raise PermissionError("persisted context belongs to another conversation")
        previous = [row for row in packet.get("tasks") or []
                    if isinstance(row, dict) and row.get("task_id")]
        order = [str(row["task_id"]) for row in previous]
        rows = {str(row["task_id"]): row for row in previous}
        for task_id, value in tasks.items():
            task_id = str(task_id)
            node = _task_node(task_id, value, {})
            if task_id not in rows:
                order.append(task_id)
            rows[task_id] = {**rows.get(task_id, {}), **node}
        packet["tasks"] = [rows[task_id] for task_id in order][-_MAX_ITEMS:]
        packet["updated_at"] = time.time()
        _atomic_write(_path(team_id), packet)
        return packet


def _initialize_locked(team_id: str, objective: str, plan: dict, *, mode: str,
                       user_id: str = "", conversation_id: str = "") -> dict:
    """调用者已持有 _packet_lock；合并原有项目事实，不另建执行现场。"""
    source = plan.get("project_context") if isinstance(plan.get("project_context"), dict) else {}
    packet = _json_safe(source) if source else {}
    previous = load(team_id)
    conversation = str(conversation_id or packet.get("conversation_id") or
                       previous.get("conversation_id") or team_id)
    require_owner(previous, user_id, conversation)
    # 显式调用沿本次身份表示传给 workdir；未指定用户的维护调用保留原归属。
    owner = str(user_id or previous.get("user_id") or packet.get("user_id") or "")
    same_context = bool(previous and str(previous.get("conversation_id") or "") == conversation)
    if same_context:
        for key in ("decisions", "completed", "open_gaps", "verification_findings", "artifacts", "attempts",
                    "source_ref", "input_snapshot", "version_identity", "execution_role", "quality_context",
                    "visibility", "reference_atlas", "frontier_research_contract", "frontier_context",
                    "concept_anchors", "concept_anchors_revision", "concept_anchors_sources",
                    "anchor_channel_conflict", "acceptance", "golden_task", "experience_intent",
                    "north_star", "critical_dimensions", "evaluation_dimensions",
                    "delivery_contract", "quality_protocol", "claim_ceiling", "current_hypothesis",
                    "sample_evidence", "rendered_observations", "human_evidence",
                    "journey_observations", "gap_plan"):
            # A sparse continuation is not a fresh project. Keep the actual
            # observations and unfinished work, while respecting explicit edits.
            if key not in packet:
                if key in plan:
                    packet[key] = _json_safe(plan[key])
                elif previous.get(key) not in (None, "", [], {}):
                    packet[key] = _json_safe(previous[key])
        # 续做计划可能只携带任务状态，工程现场仍以该会话的持久 packet 为准。
        # 两个路径别名成组继承：显式给新 workspace 时不能再补回旧 repo_root。
        if not any(key in packet for key in ("repo_root", "workspace_root")):
            for key in ("repo_root", "workspace_root"):
                if previous.get(key):
                    packet[key] = _json_safe(previous[key])
    tasks = plan.get("tasks") if isinstance(plan.get("tasks"), dict) else {}
    dependencies = {}
    hierarchy = plan.get("hierarchy") if isinstance(plan.get("hierarchy"), dict) else {}
    for task_id, value in tasks.items():
        node = value if isinstance(value, dict) else {}
        deps = node.get("depends_on", node.get("dependencies", [])) if isinstance(node, dict) else []
        if isinstance(deps, str):
            deps = [deps]
        dependencies[str(task_id)] = [str(item) for item in (deps or [])]
    for child, parent in hierarchy.items():
        child, parent = str(child), str(parent)
        dependencies.setdefault(child, [])
        if parent and parent not in dependencies[child]:
            dependencies[child].append(parent)
    previous_task_rows = [item for item in (packet.get("tasks") or
                                             (previous if same_context else {}).get("tasks") or [])
                          if isinstance(item, dict) and str(item.get("task_id") or "")]
    current_task_rows = [_task_node(str(task_id), value, dependencies)
                         for task_id, value in tasks.items()]
    task_order = [str(item.get("task_id")) for item in previous_task_rows]
    task_map = {str(item.get("task_id")): item for item in previous_task_rows}
    for item in current_task_rows:
        task_id = str(item.get("task_id"))
        if task_id not in task_map:
            task_order.append(task_id)
        task_map[task_id] = item
    merged_tasks = [task_map[task_id] for task_id in task_order if task_id in task_map][-_MAX_ITEMS:]
    import quality_context as _quality
    previous_quality = previous.get("quality_context") if same_context else {}
    previous_quality = previous_quality if isinstance(previous_quality, dict) else {}
    quality = _quality.reconcile(previous_quality, packet.get("quality_context"),
                                 plan.get("quality_context"))
    if quality:
        quality["context_ref"] = {"path": str(_path(team_id)), "field": "quality_context"}
        if previous_quality and quality != previous_quality:
            quality["revision"] = max(int(quality.get("revision") or 0),
                                      int(previous_quality.get("revision") or 0) + 1)
    packet.update({
        "schema_version": 1,
        "team_id": str(team_id),
        "conversation_id": conversation,
        "user_id": owner,
        "objective": str(packet.get("objective") or objective),
        "source_ref": _json_safe(plan.get("source_ref") or packet.get("source_ref") or {}),
        "input_snapshot": _json_safe(plan.get("input_snapshot") or packet.get("input_snapshot") or {}),
        "version_identity": _json_safe(plan.get("version_identity") or packet.get("version_identity") or {}),
        "execution_role": str(plan.get("execution_role") or packet.get("execution_role") or "champion"),
        "mode": str(packet.get("mode") or mode),
        "captain": str(plan.get("captain") or packet.get("captain") or ""),
        "north_star": _json_safe(packet.get("north_star") or plan.get("north_star") or {}),
        "critical_dimensions": _json_safe(packet.get("critical_dimensions") or
                                          plan.get("critical_dimensions") or []),
        "claim_ceiling": str(packet.get("claim_ceiling") or plan.get("claim_ceiling") or "UNVALIDATED"),
        "quality_protocol": _json_safe(packet.get("quality_protocol") or plan.get("quality_protocol") or {}),
        "captain_thesis": _json_safe(packet.get("captain_thesis") or plan.get("captain_thesis") or {}),
        "audition_evidence": _json_safe(packet.get("audition_evidence") or plan.get("captain_bids") or []),
        "current_stage": str(packet.get("current_stage") or "execution"),
        "tasks": merged_tasks,
        "delivery_contract": _json_safe(plan.get("delivery_contract") or packet.get("delivery_contract") or {}),
        "acceptance": _json_safe(plan.get("acceptance") or packet.get("acceptance") or []),
        "resource_budget": _json_safe(plan.get("resource_budget") or packet.get("resource_budget") or {}),
        "golden_task": _json_safe(packet.get("golden_task") or {}),
        "evaluation_dimensions": _json_safe(packet.get("evaluation_dimensions") or []),
        "experience_intent": _json_safe(packet.get("experience_intent") or plan.get("experience_intent") or {}),
        "frontier_research_contract": _json_safe(packet.get("frontier_research_contract") or
                                                  plan.get("frontier_research_contract") or {}),
        "reference_atlas": _json_safe(plan.get("reference_atlas") or packet.get("reference_atlas") or {}),
        "quality_context": _json_safe(quality),
        "quality_context_revision": int(quality.get("revision") or 0),
        "journey_observations": _json_safe(packet.get("journey_observations") or []),
        "gap_plan": _json_safe(packet.get("gap_plan") or []),
        "role_contexts": _json_safe(packet.get("role_contexts") or {}),
        "delivery_contract_path": str(packet.get("delivery_contract_path") or ""),
        "candidate_tasks": _json_safe(packet.get("candidate_tasks") or {}),
        "decisions": _json_safe(packet.get("decisions") or []),
        "completed": _json_safe(packet.get("completed") or []),
        "open_gaps": _json_safe(packet.get("open_gaps") or []),
        "verification_findings": _json_safe(packet.get("verification_findings") or []),
        "artifacts": _json_safe(packet.get("artifacts") or []),
        "attempts": _json_safe(packet.get("attempts") or []),
        "visibility": _json_safe(packet.get("visibility") or {}),
        "updated_at": time.time(),
    })
    packet["packet_path"] = str(_path(team_id))
    _atomic_write(_path(team_id), packet)
    return packet


def _artifact_row(value: object, owner: str, task_id: str) -> dict | None:
    if not isinstance(value, dict):
        return None
    path = str(value.get("path") or value.get("url") or "")
    if not path:
        return None
    provenance = value.get("provenance") if isinstance(value.get("provenance"), dict) else {}
    return {
        "path": path,
        "name": str(value.get("name") or os.path.basename(path)),
        "kind": str(value.get("kind") or value.get("mime") or "file"),
        "bytes": int(value.get("bytes") or 0),
        "sha256": str(provenance.get("sha256") or value.get("sha256") or ""),
        "verified": bool(provenance.get("verified") or value.get("verified")),
        "owner": str(owner),
        "task_id": str(task_id),
        "lineage": _json_safe(value.get("lineage") or {}),
    }


def _frontier_context(final: str) -> dict:
    text = str(final or "")
    start = text.find(_FRONTIER_START)
    if start < 0:
        return {}
    start += len(_FRONTIER_START)
    end = text.find(_FRONTIER_END, start)
    if end < 0:
        return {}
    try:
        value = json.loads(text[start:end].strip())
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# ── 概念锚点完整 schema(P0-3·治R3:锚点持久化不再有损)─────────────────────────
# 旧版只留5字段(dimension/reference/concept/evidence_url/is_crown),acceptance/
# evidence_requirements/artifact_refs/source_sha/revision 全丢——下游只能消费散文,
# 逐锚验收/证据要求/lineage 无从谈起。这里每锚保留完整结构,revision 单调递增,
# 下游结果必须声明消费的 revision(验证报告 anchors_revision)。
_ANCHOR_LIST_FIELDS = ("acceptance", "evidence_requirements", "artifact_refs")


def _normalize_anchor_row(row: object, *, source_sha: str = "", revision: int = 0) -> dict:
    """把任一来源(协议块/锚点文件/直接调用)的锚点行归一为完整 schema;垃圾行返回空。"""
    if not isinstance(row, dict):
        return {}
    dimension = str(row.get("dimension") or "").strip()
    if not dimension:
        return {}
    try:
        row_revision = int(row.get("revision") or 0)
    except (TypeError, ValueError):
        row_revision = 0
    normalized = {
        "dimension": dimension[:80],
        "reference": str(row.get("reference") or "")[:120],
        "concept": str(row.get("concept") or "")[:600],
        "evidence_url": str(row.get("evidence_url") or row.get("url") or "")[:500],
        "is_crown": bool(row.get("is_crown")),
        # P0-3:以下字段旧 schema 全丢,现在必须保留——它们是"逐锚验收"的全部依据。
        "required_behavior": str(row.get("required_behavior") or "")[:800],
        "source_sha": str(row.get("source_sha") or source_sha or "")[:64],
        "revision": row_revision or int(revision or 0),
    }
    for field in _ANCHOR_LIST_FIELDS:
        values = row.get(field)
        if isinstance(values, str):
            values = [values]
        normalized[field] = [str(item)[:400] for item in (values or [])
                             if str(item or "").strip()][:8]
    return normalized


def _commit_anchor_rows(packet: dict, rows: list, *, change_reason: str,
                        sources: list | None = None) -> None:
    """统一提交锚点表进 packet(带 revision 递增+quality_context 同步)。"""
    packet["concept_anchors"] = rows[-_MAX_ITEMS:]
    packet["concept_anchors_revision"] = int(packet.get("concept_anchors_revision") or 0) + 1
    if sources:
        packet["concept_anchors_sources"] = (list(packet.get("concept_anchors_sources") or [])
                                              + list(sources))[-_MAX_ITEMS:]
    quality = dict(packet.get("quality_context") or {})
    quality["concept_anchors"] = packet["concept_anchors"]
    quality["concept_anchors_revision"] = packet["concept_anchors_revision"]
    quality["revision"] = max(1, int(quality.get("revision") or 0) + 1)
    quality["change_reason"] = str(change_reason)[:120]
    quality["updated_at"] = time.time()
    packet["quality_context"] = quality
    packet["quality_context_revision"] = quality["revision"]


def _append_unique_facts(existing: object, incoming: object) -> list:
    """Merge observations without replacing another callback's recorded facts."""
    rows = _json_safe(existing) if isinstance(existing, list) else []
    seen = {json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows}
    for row in incoming if isinstance(incoming, list) else []:
        row = _json_safe(row)
        identity = json.dumps(row, ensure_ascii=False, sort_keys=True)
        if identity not in seen:
            rows.append(row)
            seen.add(identity)
    return rows


def _member_gap_id(task_id: str, harness: str, row: dict) -> str:
    local_id = str(row.get("gap_id") or "").strip()
    problem = str(row.get("problem") or row.get("finding") or row.get("defect") or "").strip()
    identity = [str(task_id), str(harness), "gap_id" if local_id else "problem", local_id or problem]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return "member-gap-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _merge_member_gaps(existing: object, incoming: list[dict], reports: dict, *,
                       only_open: bool = False) -> list:
    """Update only the same member's gap; report versions remain immutable."""
    import quality_context as _quality

    updates = {gap["gap_id"]: gap for gap in incoming}
    rows, applied = [], set()
    for row in existing if isinstance(existing, list) else []:
        provenance = (row.get("provenance") or {}) if isinstance(row, dict) else {}
        identity = str(row.get("gap_id") or "") if isinstance(row, dict) else ""
        if provenance.get("kind") == "member_report" and not identity.startswith("member-gap-"):
            # Resolve the previous report-hash identity through its saved raw
            # payload, including no-id gaps previously assigned G-001/G-002.
            report_id, separator, index = identity.rpartition(":G")
            report = reports.get(report_id) or {}
            raw_gaps = (report.get("payload") or {}).get("gap_plan") or []
            raw_gaps = [value if isinstance(value, dict) else {"problem": value}
                        for value in raw_gaps if _quality.normalize_gaps([value])]
            if (separator and index.isdigit() and 0 < int(index) <= len(raw_gaps)
                    and report.get("task_id") == provenance.get("task_id")
                    and report.get("harness") == provenance.get("harness")):
                identity = _member_gap_id(report["task_id"], report["harness"], raw_gaps[int(index) - 1])
        update = updates.get(identity)
        same_member = bool(update and provenance.get("kind") == "member_report"
                           and provenance.get("task_id") == update["provenance"]["task_id"]
                           and provenance.get("harness") == update["provenance"]["harness"])
        if not same_member:
            rows.append(_json_safe(row))
            continue
        if identity not in applied and (not only_open or update["status"] != "closed"):
            rows.append(_json_safe(update))
        applied.add(identity)
    rows.extend(_json_safe(gap) for identity, gap in updates.items()
                if identity not in applied and (not only_open or gap["status"] != "closed"))
    return rows


def _record_member_quality(packet: dict, context: dict, task_id: str, harness: str) -> None:
    """Project an explicit member report, retaining its unverified original.

    Normalized gaps deliberately have a small schema. Their evidence_refs point
    back to the immutable payload, so normalization cannot discard attribution.
    """
    if not any(isinstance(context.get(field), list) and context[field]
               for field in ("sources", "journey_observations", "gap_plan")):
        return
    import quality_context as _quality

    payload = _json_safe(context)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    identity = json.dumps([str(task_id), str(harness), digest], ensure_ascii=False)
    report_id = "member-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    quality = dict(packet.get("quality_context") or {})
    reports = dict(quality.get("member_reports") or {})
    if report_id in reports:
        return
    reference = {
        "path": str(_path(str(packet["team_id"])).resolve()),
        "json_pointer": f"/quality_context/member_reports/{report_id}/payload",
        "sha256": digest,
    }
    try:
        from artifact_store import put_bytes
        reference = {**put_bytes(str(packet["team_id"]), str(task_id),
                                  canonical.encode("utf-8"), name="member-report.json"),
                     "json_pointer": ""}
    except (OSError, ValueError):
        # Keep the original in the owner packet on storage failure. Strict
        # candidate snapshots must not expose this shared-file fallback.
        pass
    provenance = {"kind": "member_report", "task_id": str(task_id), "harness": str(harness),
                  "independent_verification": False, "report_ref": reference}
    reports[report_id] = {**provenance, "payload": payload, "recorded_at": time.time()}
    quality["member_reports"] = reports
    locator = reference["path"] + "#" + reference["json_pointer"]
    # Only data fields are projected. Claims about reviewer identity, quality
    # acceptance, or release readiness remain statements in the original payload.
    source_rows = []
    for row in context.get("sources") or []:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        paths = row.get("evidence_paths") or row.get("artifacts") or []
        paths = paths if isinstance(paths, list) else [paths]
        references = [row.get(field) for field in ("path", "snapshot_ref", "evidence_ref")]
        row["evidence_paths"] = _append_unique_facts(
            paths, [value for value in references if isinstance(value, str) and value])
        source_rows.append(row)
    gap_rows = []
    for row in context.get("gap_plan") or []:
        row = dict(row) if isinstance(row, dict) else {"problem": row}
        refs = row.get("evidence_refs") or row.get("evidence_ids") or []
        refs = refs if isinstance(refs, list) else [refs]
        if isinstance(row.get("evidence_ref"), str):
            refs = _append_unique_facts(refs, [row["evidence_ref"]])
        row["evidence_refs"] = refs
        gap_rows.append(row)
    normalized = _quality.normalize({"sources": source_rows})
    source_ids = {}
    sources = []
    for index, source in enumerate(normalized["sources"], 1):
        # reconcile omits empty defaults on sparse resumes; persist that same
        # canonical form without losing anything from the original report.
        source = {key: value for key, value in source.items() if value not in (None, "", [], {})}
        original_id = source["source_id"]
        source["reported_source_id"] = original_id
        source["source_id"] = f"{report_id}:S{index}"
        source_ids[original_id] = source["source_id"]
        source["provenance"] = _json_safe(provenance)
        source["evidence_paths"] = _append_unique_facts(source.get("evidence_paths"), [locator])
        sources.append(source)
    quality["sources"] = _append_unique_facts(quality.get("sources"), sources)

    observations = []
    for row in context.get("journey_observations") or []:
        if not isinstance(row, dict):
            continue
        observation = {key: _json_safe(value) for key, value in row.items()
                       if key not in ("role", "verified", "provenance", "source_task_id",
                                      "independent_verification")}
        refs = row.get("evidence_refs") or []
        refs = refs if isinstance(refs, list) else [refs]
        refs = [ref for ref in [*refs, row.get("evidence_ref"), row.get("path")]
                if isinstance(ref, str) and ref]
        ids = row.get("source_ids") or ([row["source_id"]] if row.get("source_id") else [])
        ids = ids if isinstance(ids, list) else [ids]
        observation.update({
            "provenance": _json_safe(provenance), "verified": False,
            "source_task_id": str(task_id),
            "source_ids": [source_ids.get(str(value), str(value)) for value in ids],
            "evidence_refs": _append_unique_facts([source_ids.get(ref, ref) for ref in refs], [locator]),
        })
        if isinstance(row.get("source_id"), str):
            observation["source_id"] = source_ids.get(row["source_id"], row["source_id"])
        observations.append(observation)
    packet["journey_observations"] = _append_unique_facts(
        packet.get("journey_observations"), observations)
    quality["journey_observations"] = _append_unique_facts(
        quality.get("journey_observations"), observations)

    gaps = []
    for row in gap_rows:
        normalized_gaps = _quality.normalize_gaps([row])
        if not normalized_gaps:
            continue
        gap = normalized_gaps[0]
        gap["reported_gap_id"] = str(row.get("gap_id") or "").strip()
        gap["gap_id"] = _member_gap_id(task_id, harness, row)
        gap["owner_task"] = str(task_id)
        gap["provenance"] = _json_safe(provenance)
        gap["source"] = "member_report"
        gap["independent_verification"] = False
        gap["reported_status"] = gap["status"]
        gap["evidence_refs"] = _append_unique_facts(
            [source_ids.get(ref, ref) for ref in gap.get("evidence_refs") or []], [locator])
        gaps.append(gap)
    quality["gap_plan"] = _merge_member_gaps(quality.get("gap_plan"), gaps, reports)
    packet["gap_plan"] = _merge_member_gaps(packet.get("gap_plan"), gaps, reports)
    packet["open_gaps"] = _merge_member_gaps(packet.get("open_gaps"), gaps, reports, only_open=True)
    quality["revision"] = max(1, int(quality.get("revision") or 0) + 1)
    quality["change_reason"] = "member_report"
    quality["updated_at"] = time.time()
    packet["quality_context"] = quality
    packet["quality_context_revision"] = quality["revision"]


def _merge_frontier_context(packet: dict, context: dict, task_id: str, *, harness: str = "") -> None:
    """Persist research authored by the Legion as shared context for downstream nodes."""
    if not context:
        return
    research = dict(packet.get("frontier_context") or {})
    for field, value in _json_safe(context).items():
        if field in {"task_id", "harness", "source_task_id", "source_kind",
                     "role", "verified", "independent_verification", "provenance", "claim_ceiling"}:
            continue  # Self-declared authority remains in the raw member report.
        research[field] = (_append_unique_facts(research.get(field), value)
                           if isinstance(value, list) else value)
    research.update({"source_task_id": str(task_id), "source_harness": str(harness),
                     "source_kind": "member_report", "updated_at": time.time()})
    packet["frontier_context"] = research
    # 概念锚点表(重放#3/#4实锤:研究浅/锚点不结构化→全链自洽地平庸):研究在
    # LEGION_FRONTIER_CONTEXT 里给出 concept_anchors 数组时,持久化为全军团共同对照事实,
    # 翻译官任务书/验证契约/首脑协调全部逐锚对照这张表。
    # P0-3:每锚保留完整 schema(acceptance/evidence_requirements/artifact_refs/source_sha/
    # revision/is_crown),不再只有损压缩到5字段。
    anchors = context.get("concept_anchors") if isinstance(context.get("concept_anchors"), list) else None
    if anchors:
        rows = [row for row in (_normalize_anchor_row(item) for item in anchors) if row]
        if rows:
            _commit_anchor_rows(packet, rows, change_reason="legion_frontier_research",
                                sources=[{"channel": "protocol_block", "task_id": str(task_id),
                                          "rows": len(rows), "raw_anchors": _json_safe(anchors),
                                          "ingested_at": time.time()}])
    quality = dict(packet.get("quality_context") or {})
    quality["frontier_context"] = research
    quality["revision"] = max(1, int(quality.get("revision") or 0) + 1)
    quality["change_reason"] = "legion_frontier_research"
    quality["updated_at"] = time.time()
    packet["quality_context"] = quality
    packet["quality_context_revision"] = quality["revision"]
    _record_member_quality(packet, context, task_id, harness)
    if "gap_plan" in context and packet["quality_context"].get("member_reports"):
        # The frontier view is current state too; version history lives in the
        # raw reports, not in an accumulating list of obsolete open projections.
        research["gap_plan"] = _json_safe([
            gap for gap in packet.get("gap_plan") or []
            if isinstance(gap, dict) and (gap.get("provenance") or {}).get("kind") == "member_report"])
        packet["quality_context"]["frontier_context"] = research


def record_progress_artifact(team_id: str, task_id: str, event: dict, *,
                             assigned_harness: str = "") -> dict:
    """Persist an artifact_ready event before the Harness reaches a terminal result."""
    with _packet_lock(team_id):
        return _record_progress_artifact_unlocked(
            team_id, task_id, event, assigned_harness=assigned_harness)


def _record_progress_artifact_unlocked(team_id: str, task_id: str, event: dict, *,
                                       assigned_harness: str = "") -> dict:
    packet = load(team_id)
    if not packet:
        return {}
    owner = str(event.get("agent") or assigned_harness or task_id)
    value = dict(event)
    value.setdefault("path", event.get("url") or event.get("name"))
    artifact = _artifact_row(value, owner, str(task_id))
    if not artifact:
        return packet
    artifacts = [item for item in (packet.get("artifacts") or []) if isinstance(item, dict)]
    known = {(str(item.get("path")), str(item.get("task_id"))) for item in artifacts}
    if (artifact["path"], str(task_id)) not in known:
        artifact["progressive"] = True
        artifacts.append(artifact)
    packet["artifacts"] = artifacts[-_MAX_ITEMS:]
    packet["updated_at"] = time.time()
    _atomic_write(_path(team_id), packet)
    return packet


def record_result(team_id: str, task_id: str, result: dict, *,
                  assigned_harness: str = "", continuation: dict | None = None) -> dict:
    with _packet_lock(team_id):
        return _record_result_unlocked(
            team_id, task_id, result, assigned_harness=assigned_harness,
            continuation=continuation)


# ── P0-2 Artifact 双通道摄取(治R2:正确文件存在 packet 0 条)──────────────────
# 根因:锚点摄取只走 final 协议块单通道;研究把锚点表写成 concept_anchors.json 落盘时,
# 文件明明存在 packet 却 0 条,翻译官/验证全部拿不到共享事实。
# kb-final 实锤(2026-09-08):cc 交付《觅游军团_B站视频_概念锚点表.md》质量世界级,但
# 中文文件名不含 concept_anchors 关键词→被误判'无锚点文件'→降级 incomplete→白烧两棒接力。
# 修:文件名匹配扩到中文(概念锚点/锚点表)+内容特征兜底(表格含维度/对标/参照列)。
_ANCHOR_FILE_PATTERN = re.compile(
    r"concept[_-]?anchors?|概念锚点|锚点表", re.IGNORECASE)
_ANCHOR_CONTENT_HINT = re.compile(r"维度|对标|参照|锚点", re.IGNORECASE)


def _parse_anchor_markdown(text: str) -> list[dict]:
    """从 Markdown 表格解析锚点行。表头必须含'维度/dimension'列才开始收行——
    参照表(参照|链接|同题证据)不是锚点表,不硬映射错位列,交给文档级兜底。"""
    rows: list[dict] = []
    keys: list[str] | None = None

    def _map_col(key: str) -> str | None:
        k = key.strip().lower()
        if k in ("dimension", "维度", "锚点维度", "夺冠维度", "体验维度"):
            return "dimension"
        if k in ("reference", "对标", "参照"):
            return "reference"
        if k in ("concept", "概念", "配方"):
            return "concept"
        if k in ("evidence_url", "url", "链接"):
            return "evidence_url"
        if k in ("is_crown", "桂冠", "是否桂冠"):
            return "is_crown"
        return None

    for line in str(text or "").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells or all(re.fullmatch(r":?-{2,}:?|:-+:|-+", cell) for cell in cells if cell):
            continue
        if keys is None:
            mapped = [_map_col(c) for c in cells]
            if "dimension" in mapped:
                keys = mapped
            continue
        row = {key: cells[idx] for idx, key in enumerate(keys)
               if key and idx < len(cells)}
        if row.get("dimension"):
            rows.append(row)
    return rows


def _document_level_anchor(path: str) -> dict:
    """文档级锚点兜底:研究交付整本锚点文档(散文配方/参照表形态,列不出标准行)时,
    把文档自身作为一条共享事实摄入——标题+路径+首段结论,下游知道'锚点文档在哪'
    可读全文,而不是丢掉全部研究。(kb-final实锤:cc的《概念锚点表.md》世界级但
    列不出标准锚点行,旧逻辑判'无锚点'→降级→白烧两棒接力。)"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(60000)
    except OSError:
        return {}
    title, summary = "", ""
    for line in text.splitlines():
        s = line.strip().lstrip("#>").strip()
        if s and not title:
            title = s[:80]
        elif s and not summary and len(s) > 20:
            summary = s[:400]
        if title and summary:
            break
    return {"dimension": f"研究锚点文档:{title or os.path.basename(path)}",
            "reference": "军团研究交付物", "concept": summary,
            "evidence_url": "", "is_crown": False,
            "artifact_ref": path, "document_level": True}


def _read_anchor_artifact_rows(artifact: dict, *, raw_rows: list | None = None) -> list[dict]:
    """读取单个 artifact 里的锚点行(JSON 优先,Markdown 表格兼容)。非锚点文件返回空。

    判定:文件名命中(中英文)或 .md 文件名含'锚点';.md 内容含锚点表格特征才收,
    防把任意 Markdown 当锚点表。
    """
    path = str(artifact.get("path") or "")
    if not path or not os.path.isfile(path):
        return []
    base = os.path.basename(path)
    name_hit = bool(_ANCHOR_FILE_PATTERN.search(base))
    if not name_hit:
        # 内容特征兑底:仅对 .md 文件做轻量预检(文件名不含锚点但表格含维度/对标列)。
        if not base.lower().endswith(".md"):
            return []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                head = handle.read(4000)
        except OSError:
            return []
        if not (_ANCHOR_CONTENT_HINT.search(head) and "|" in head):
            return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(200000)
    except OSError:
        return []
    rows: list[dict] = []
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            # 键名兼容:concept_anchors(标准)/anchors(hermes 实锤形态)——认锚点行结构
            # 不认键名:行内含 dimension/id+reference 类字段即收。
            rows = (value.get("concept_anchors") or value.get("anchors")
                    or value.get("anchor_rows") or [])
        elif isinstance(value, list):
            rows = value
    except (json.JSONDecodeError, ValueError, TypeError):
        rows = _parse_anchor_markdown(text)
    rows = [row for row in rows if isinstance(row, dict)]
    if raw_rows is not None:
        # Preserve source fields before compatibility aliases add index keys.
        # The artifact path remains the reference for the original document.
        raw_rows.extend(_json_safe(rows))
    # 结构级兼容:行没有 dimension 键但有锚点语义键(id+reference/recipe)时归一
    normalized_rows = []
    for row in rows:
        if not row.get("dimension") and (row.get("id") or row.get("dimension_key")):
            row = {**row, "dimension": str(row.get("id") or row.get("dimension_key"))}
        if row.get("dimension"):
            normalized_rows.append(row)
    rows = normalized_rows
    # 文档级兜底:文件名是锚点文档但解析不出标准行(散文/参照表形态)时,
    # 整文档作为一条共享事实摄入——研究交付物不丢,下游可读全文。
    if not rows and name_hit:
        doc = _document_level_anchor(path)
        if doc:
            rows = [doc]
    return rows


def _ingest_anchor_artifacts_unlocked(packet: dict, result: dict, task_id: str) -> None:
    """P0-2 双通道摄取:final 协议块之外的显式 concept_anchors 文件也能进 packet。

    schema 校验 + SHA-256/task/revision 记录;与既有索引同维度不同概念时记
    anchor_channel_conflict，同时按真实来源保留双方描述，不替成员二选一。
    纯新增维度视为补充直接合并(研究的合法增量)。
    """
    channel_rows: list[dict] = []
    sources: list[dict] = []
    for value in [*(result.get("artifacts") or []), *(result.get("discovered_artifacts") or [])]:
        if not isinstance(value, dict):
            continue
        raw_rows: list = []
        rows = _read_anchor_artifact_rows(value, raw_rows=raw_rows)
        if not rows:
            continue
        path = str(value.get("path") or "")
        source_sha = ""
        try:
            source_sha = hashlib.sha256(Path(path).read_bytes()[:1000000]).hexdigest()
        except OSError:
            pass
        channel_rows.extend(rows)
        sources.append({"channel": "artifact_file", "path": path, "task_id": str(task_id),
                        "source_sha": source_sha, "rows": len(rows),
                        "raw_anchors": raw_rows, "ingested_at": time.time()})
    if not channel_rows:
        return
    normalized = [row for row in (_normalize_anchor_row(item) for item in channel_rows) if row]
    if not normalized:
        return
    # Record each source even when the index cannot choose one concept.
    # Reuse this packet's existing bounded source history, not a second store.
    packet["concept_anchors_sources"] = (list(packet.get("concept_anchors_sources") or [])
                                         + sources)[-_MAX_ITEMS:]
    existing = [row for row in (packet.get("concept_anchors") or []) if isinstance(row, dict)]
    if existing:
        existing_map = {str(row.get("dimension")): str(row.get("concept") or "") for row in existing}
        conflicts = [row for row in normalized
                     if str(row.get("dimension")) in existing_map
                     and existing_map[str(row.get("dimension"))]
                     and str(row.get("concept") or "")
                     and existing_map[str(row.get("dimension"))] != str(row.get("concept") or "")]
        if conflicts:
            conflict = (packet.get("anchor_channel_conflict")
                        if isinstance(packet.get("anchor_channel_conflict"), dict) else {})
            conflict.update({"unresolved": True,
                             "conflict_dimensions": [str(row.get("dimension")) for row in conflicts][:12],
                             "protocol_rows": len(existing), "artifact_rows": len(normalized),
                             # Raw rows are projected only through the task-scoped
                             # source list, not duplicated into this legacy index.
                             "artifact_sources": [{k: v for k, v in source.items()
                                                   if k != "raw_anchors"} for source in sources],
                             "updated_at": time.time()})
            packet["anchor_channel_conflict"] = conflict
            return
        # 纯新增维度:合并补充,不覆盖已有行。
        known = {str(row.get("dimension")) for row in existing}
        fresh = [row for row in normalized if str(row.get("dimension")) not in known]
        if not fresh:
            return
        _commit_anchor_rows(packet, existing + fresh, change_reason="concept_anchor_artifact_ingest")
        return
    _commit_anchor_rows(packet, normalized, change_reason="concept_anchor_artifact_ingest")


def _record_result_unlocked(team_id: str, task_id: str, result: dict, *,
                            assigned_harness: str = "",
                            continuation: dict | None = None) -> dict:
    packet = load(team_id)
    if not packet:
        return {}
    owner = str(result.get("assigned_harness") or result.get("harness") or assigned_harness or task_id)
    task_id = str(task_id)
    status = str(result.get("status") or "error")
    summary = str(result.get("final") or result.get("error") or status)
    row = {
        "task_id": task_id,
        "harness": owner,
        "status": status,
        "summary": summary[:_MAX_INLINE],
        "error_envelope": _json_safe(result.get("error_envelope") or {}),
        "native_facts": _json_safe(result.get("native_facts") or {}),
        "native_checkpoint": _json_safe(result.get("native_checkpoint") or {}),
        "continuation": _json_safe(continuation or {}),
        "recorded_at": time.time(),
    }
    attempts = [item for item in (packet.get("attempts") or [])
                if not (isinstance(item, dict) and item.get("task_id") == task_id
                        and item.get("harness") == owner and item.get("status") == status)]
    attempts.append(row)
    packet["attempts"] = attempts[-_MAX_ITEMS:]
    completed = [item for item in (packet.get("completed") or [])
                 if not (isinstance(item, dict) and item.get("task_id") == task_id)]
    if status == "done":
        completed.append({"task_id": task_id, "harness": owner, "summary": summary[:_MAX_INLINE]})
    packet["completed"] = completed[-_MAX_ITEMS:]
    artifacts = [item for item in (packet.get("artifacts") or []) if isinstance(item, dict)]
    known = {(str(item.get("path")), str(item.get("task_id"))) for item in artifacts}
    for value in [*(result.get("artifacts") or []), *(result.get("discovered_artifacts") or [])]:
        artifact = _artifact_row(value, owner, task_id)
        if artifact and (artifact["path"], task_id) not in known:
            artifacts.append(artifact)
            known.add((artifact["path"], task_id))
    packet["artifacts"] = artifacts[-_MAX_ITEMS:]
    journey = [item for item in (result.get("journey_observations") or []) if isinstance(item, dict)]
    if journey:
        packet["journey_observations"] = _append_unique_facts(packet.get("journey_observations"), journey)
    _merge_frontier_context(packet, _frontier_context(result.get("final") or ""), task_id, harness=owner)
    # P0-2 双通道摄取:final 协议块之外,显式落盘的 concept_anchors 文件也进 packet。
    _ingest_anchor_artifacts_unlocked(packet, result, task_id)
    # 独立验证由执行器单独调用 record_verification；这里不重复写，确保每份评审只有一条事实。
    packet["updated_at"] = time.time()
    _atomic_write(_path(team_id), packet)
    return packet


def record_coordination(team_id: str, captain: str, proposal: dict) -> dict:
    """持久记录真实首脑的阶段判断和任务调整，供后续轮次与恢复读取。"""
    with _packet_lock(team_id):
        return _record_coordination_unlocked(team_id, captain, proposal)


def _record_coordination_unlocked(team_id: str, captain: str, proposal: dict) -> dict:
    packet = load(team_id)
    if not packet:
        return {}
    row = {"kind": "captain_coordination", "captain": str(captain),
           "sequence": int(proposal.get("sequence") or 0),
           "action": (None if proposal.get("error") and not proposal.get("action")
                      else str(proposal.get("action") or "continue")),
           "reason": str(proposal.get("reason") or "")[:_MAX_INLINE],
           "task_updates": _json_safe(proposal.get("task_updates") or {}),
           "messages": _json_safe(proposal.get("messages") or []),
           "next_plan": _json_safe(proposal.get("next_plan") or {}),
           "model_used": str(proposal.get("model_used") or ""),
           "recorded_at": time.time()}
    # 原提案和运行回执并列保留。不能只记“首脑要继续”，却丢掉它的提案
    # 未能进入执行图这一事实；旧记录缺失的字段也不补造为成功。
    for field in ("ok", "error", "protocol_feedback", "protocol_recovery", "execution_result_ref"):
        if field in proposal:
            row[field] = _json_safe(proposal[field])
    decisions = [item for item in (packet.get("decisions") or []) if isinstance(item, dict)]
    decisions.append(row)
    packet["decisions"] = decisions[-_MAX_ITEMS:]
    packet["captain"] = str(captain)
    packet["current_stage"] = "coordinating"
    packet["updated_at"] = time.time()
    _atomic_write(_path(team_id), packet)
    return packet


def record_verification(team_id: str, report: dict) -> dict:
    """Persist reviewer facts immediately so downstream nodes see the latest direction evidence."""
    with _packet_lock(team_id):
        return _record_verification_unlocked(team_id, report)


def record_concept_anchors(team_id: str, anchors: list) -> dict:
    """持久化研究的概念锚点表为全军团共同对照事实(逐锚维度/对标/概念/参照URL)。

    研究浅→翻译官降档→实现平庸全链自洽地错,根在“世界级长什么样”没有共同事实源。
    锚点表进 packet 后:翻译官任务书必须逐锚提炼、验证者逐锚判定、首脑逐锚对比后再决定续跑。
    只收结构化行(dimension/reference/concept/evidence_url),垃圾行直接丢弃。
    """
    if not anchors:
        return {}
    rows = [row for row in (_normalize_anchor_row(item) for item in anchors) if row]
    if not rows:
        return {}
    with _packet_lock(team_id):
        packet = load(team_id)
        if not packet:
            return {}
        _commit_anchor_rows(packet, rows, change_reason="concept_anchor_protocol",
                            sources=[{"channel": "direct_call", "rows": len(rows),
                                      "ingested_at": time.time()}])
        packet["updated_at"] = time.time()
        _atomic_write(_path(team_id), packet)
        return packet


# ── P0-1 研究结构化完成门(治R1:占位句/仅Markdown均 done)─────────────────────────
# 根因:_frontier_context 静默返回空→无协议块的研究照样 done→全链在零共享事实下自洽地平庸。
# 门只检查协议/锚点/URL 这些运行完整性事实,不判研究内容好坏(内容好坏由锚点表对照)。
_PLACEHOLDER_OPENERS = ("现在我将", "我将要", "接下来我将", "我将先", "我会先", "我马上",
                        "稍后我将", "我打算", "让我先", "正在整理", "即将开始")


def _looks_like_placeholder_research(final: str) -> bool:
    """将来时承诺占位检测:短文本+将来时开头+无任何实质内容(URL/协议块/数据)。"""
    text = str(final or "").strip()
    if not text or len(text) >= 600:
        return False
    if "<<LEGION_FRONTIER_CONTEXT>>" in text or "http" in text:
        return False
    head = text[:120]
    return any(head.startswith(opener) or opener in head for opener in _PLACEHOLDER_OPENERS)


def evaluate_research_completion(final: str, artifacts: list | None = None) -> dict:
    """研究结构化完成门(P0-1):协议解析成功 + concept_anchors 非空覆盖全维度
    + 至少一个桂冠锚 + URL 可回访;失败改 incomplete/revise 禁止 done。

    只看协议完整性事实,不替 harness 判断研究内容质量。
    """
    context = _frontier_context(final) if isinstance(final, str) else {}
    anchors = ([row for row in (context.get("concept_anchors") or []) if isinstance(row, dict)]
               if isinstance(context, dict) else [])
    valid = [row for row in (_normalize_anchor_row(item) for item in anchors) if row]
    # 文件通道兜底:final 没协议块但锚点文件已落盘时,锚点算数(双通道之一即可)。
    file_rows: list[dict] = []
    for value in (artifacts or []):
        if isinstance(value, dict):
            file_rows.extend(_read_anchor_artifact_rows(value))
    file_valid = [row for row in (_normalize_anchor_row(item) for item in file_rows) if row]
    rows = valid or file_valid
    reasons: list[str] = []
    if not isinstance(context, dict) or not context:
        if not file_valid:
            reasons.append("final无<<LEGION_FRONTIER_CONTEXT>>协议块且无concept_anchors文件")
    if not rows:
        reasons.append("concept_anchors为空:研究未覆盖任何维度锚点")
    else:
        if not any(row.get("is_crown") for row in rows):
            reasons.append("concept_anchors缺少至少一个品类桂冠锚(is_crown)")
        url_rows = [row for row in rows if str(row.get("evidence_url") or "").startswith("http")]
        if len(url_rows) < max(1, (len(rows) + 1) // 2):
            reasons.append(f"锚点evidence_url可回访比例不足({len(url_rows)}/{len(rows)},需过半)")
    if _looks_like_placeholder_research(final):
        reasons.append("final为将来时承诺占位,无实质研究内容")
    return {"ok": not reasons, "reasons": reasons, "anchor_count": len(rows),
            "crown_count": sum(1 for row in rows if row.get("is_crown")),
            "channel": "protocol_block" if valid else ("artifact_file" if file_valid else "none")}


def mark_frontier_context_missing(team_id: str, reason: str) -> dict:
    """P0-4:零锚事实显式落 packet(下游阻断/放行门/首脑都能看到)。"""
    with _packet_lock(team_id):
        packet = load(team_id)
        if not packet:
            return {}
        rows = [row for row in (packet.get("concept_anchors") or [])
                if isinstance(row, dict) and row.get("dimension")]
        if rows:
            packet.pop("frontier_context_missing", None)
        else:
            packet["frontier_context_missing"] = {
                "reason": str(reason or "research finished without anchors")[:1000],
                "updated_at": time.time()}
        packet["updated_at"] = time.time()
        _atomic_write(_path(team_id), packet)
        return packet


def mark_effect_downgrade(team_id: str, task_id: str, downgrade: str) -> dict:
    """P0-7:effect-based done 降级事实落 packet(首脑下一轮据此重派)。"""
    with _packet_lock(team_id):
        packet = load(team_id)
        if not packet:
            return {}
        rows = [item for item in (packet.get("effect_downgrades") or []) if isinstance(item, dict)]
        rows.append({"task_id": str(task_id), "downgrade": str(downgrade)[:500],
                     "recorded_at": time.time()})
        packet["effect_downgrades"] = rows[-_MAX_ITEMS:]
        packet["updated_at"] = time.time()
        _atomic_write(_path(team_id), packet)
        return packet


def set_claim_ceiling(team_id: str, value: str, reason: str = "") -> dict:
    """P1-3:真人证据不可得时立即封 claim ceiling 为 BELOW_FRONTIER。"""
    with _packet_lock(team_id):
        packet = load(team_id)
        if not packet:
            return {}
        packet["claim_ceiling"] = str(value or "BELOW_FRONTIER")[:64]
        packet["claim_ceiling_reason"] = str(reason)[:1000]
        quality = dict(packet.get("quality_context") or {})
        quality["claim_ceiling"] = packet["claim_ceiling"]
        quality["revision"] = max(1, int(quality.get("revision") or 0) + 1)
        quality["updated_at"] = time.time()
        packet["quality_context"] = quality
        packet["quality_context_revision"] = quality["revision"]
        packet["updated_at"] = time.time()
        _atomic_write(_path(team_id), packet)
        return packet


def _record_verification_unlocked(team_id: str, report: dict) -> dict:
    packet = load(team_id)
    if not packet:
        return {}
    safe_report = _json_safe(report)
    report_id = str(report.get("report_id") or
                    f"verification:{report.get('task_id') or ''}:{report.get('role') or ''}")
    safe_report["report_id"] = report_id
    reports = [item for item in (packet.get("verification_findings") or [])
               if isinstance(item, dict) and str(item.get("report_id") or "") != report_id]
    reports.append(safe_report)
    packet["verification_findings"] = reports[-_MAX_ITEMS:]
    if str(report.get("role") or "") == "direction_reviewer":
        quality = dict(packet.get("quality_context") or {})
        quality.setdefault("north_star", _json_safe(packet.get("north_star") or {}))
        quality.setdefault("critical_dimensions", _json_safe(packet.get("critical_dimensions") or []))
        quality.setdefault("claim_ceiling", str(packet.get("claim_ceiling") or "UNVALIDATED"))
        for name in ("current_hypothesis", "claim_ceiling"):
            if report.get(name):
                quality[name] = _json_safe(report[name])
                packet[name] = _json_safe(report[name])
        for name in ("sample_evidence", "rendered_observations", "journey_observations", "human_evidence"):
            if isinstance(report.get(name), list):
                quality[name] = (_append_unique_facts(packet.get(name), report[name])
                                 if name == "journey_observations"
                                 else _json_safe(report[name])[-_MAX_ITEMS:])
                packet[name] = _json_safe(quality[name])
        if isinstance(report.get("gap_plan"), list):
            try:
                import quality_context
                quality = quality_context.merge_gaps(
                    quality, report["gap_plan"], reason="legion_direction_review")
            except Exception:
                quality["gap_plan"] = _json_safe(report["gap_plan"])[-_MAX_ITEMS:]
            packet["gap_plan"] = _json_safe(quality.get("gap_plan") or [])[-_MAX_ITEMS:]
            packet["open_gaps"] = [item for item in packet["gap_plan"]
                                   if str(item.get("status") or "open") in {"open", "improved", "inconclusive"}]
        decision = str(report.get("recommended_action") or "").strip()
        if decision:
            decisions = [item for item in (packet.get("decisions") or []) if isinstance(item, dict)]
            decisions.append({"kind": "direction_review", "task_id": str(report.get("task_id") or ""),
                              "action": decision, "evidence": _json_safe(report.get("evidence") or []),
                              "recorded_at": time.time()})
            packet["decisions"] = decisions[-_MAX_ITEMS:]
        quality["revision"] = max(1, int(quality.get("revision") or 0) + 1)
        quality["change_reason"] = "legion_direction_review"
        quality["updated_at"] = time.time()
        packet["quality_context"] = quality
        packet["quality_context_revision"] = quality["revision"]
        packet["current_stage"] = "direction_reviewed"
    packet["updated_at"] = time.time()
    _atomic_write(_path(team_id), packet)
    return packet


def _task_view(packet: dict, task_id: str) -> dict:
    tasks = [item for item in (packet.get("tasks") or []) if isinstance(item, dict)]
    current = next((item for item in tasks if str(item.get("task_id")) == str(task_id)), {})
    deps = set(str(item) for item in (current.get("depends_on") or []))
    downstream = [str(item.get("task_id")) for item in tasks
                  if str(task_id) in {str(value) for value in (item.get("depends_on") or [])}]
    return {"current": current, "direct_dependencies": sorted(deps), "downstream": downstream}


def _snapshot_artifacts(team_id: str, source: dict, artifacts: list) -> list:
    """只投影已落盘原文，不写 packet、不创建新的存储或读取入口。

    lineage_ref 指向原 packet 的 JSON Pointer；sha256/bytes 对应该节点的
    UTF-8 JSON（ensure_ascii=False, sort_keys=True, separators=(",", ":")）。
    读回时校验节点正文；定位失效或正文变化后重新获取 snapshot，
    不因 packet 中无关字段更新就否定仍然一致的原文。
    """
    visibility = source.get("visibility")
    if isinstance(visibility, dict) and visibility.get("candidate_isolation") == "strict":
        return artifacts  # 不给隔离候选增加通向其他候选原文的引用。
    try:
        path = _path(team_id)
        if path.is_symlink():
            return artifacts
        raw = path.read_bytes()
        persisted = json.loads(raw)
        if persisted != source:
            return artifacts  # 内存现场尚未落盘或文件已更新，不能伪造可读引用。
    except (OSError, ValueError, TypeError):
        return artifacts

    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")

    references = {}
    for index, artifact in enumerate(persisted.get("artifacts") or []):
        lineage = artifact.get("lineage") if isinstance(artifact, dict) else None
        if not isinstance(lineage, dict) or not lineage:
            continue
        data = canonical(lineage)
        digest = hashlib.sha256(data).hexdigest()
        references.setdefault(digest, {
            "path": str(path.resolve()), "json_pointer": f"/artifacts/{index}/lineage",
            "sha256": digest, "bytes": len(data), "encoding": "json-canonical-utf8",
        })
    projected = []
    for artifact in artifacts:
        row = dict(artifact) if isinstance(artifact, dict) else artifact
        lineage = row.get("lineage") if isinstance(row, dict) else None
        if isinstance(lineage, dict) and lineage:
            data = canonical(lineage)
            ref = references.get(hashlib.sha256(data).hexdigest())
            if ref and len(data) > len(canonical(ref)) + 4:
                row.pop("lineage")
                row["lineage_ref"] = dict(ref)
        projected.append(row)
    return projected


def _project_member_reports(source: dict, allowed_tasks: set[str] | None = None) -> None:
    """Read-only projection: current facts plus original-report references."""
    quality = source.get("quality_context")
    reports = quality.get("member_reports") if isinstance(quality, dict) else None
    if not isinstance(reports, dict) or not reports:
        return
    strict = allowed_tasks is not None
    shared_path = str(_path(str(source.get("team_id") or "")).resolve())
    selected = {key: row for key, row in reports.items()
                if isinstance(row, dict)
                and (not strict or str(row.get("task_id") or "") in allowed_tasks)}

    def project(value):
        if isinstance(value, list):
            return [safe for row in value if (safe := project(row)) is not None]
        if isinstance(value, dict):
            provenance = value.get("provenance") or {}
            owner = (provenance.get("task_id") if isinstance(provenance, dict) else None)
            owner = owner or value.get("source_task_id") or value.get("task_id") or value.get("owner_task")
            if strict and owner and str(owner) not in allowed_tasks:
                return None
            return {key: safe for key, item in value.items()
                    if (safe := project(item)) is not None}
        if isinstance(value, str):
            if strict and (shared_path in value or str(_path(str(source.get("team_id") or ""))) in value):
                return None
            if _FRONTIER_START in value:
                before, _, report = value.partition(_FRONTIER_START)
                _, end, after = report.partition(_FRONTIER_END)
                # Completion/attempt previews may end halfway through the raw
                # protocol. Its original is already available through the index.
                return before + (after if end else "")
        return value

    index = {}
    for key, report in selected.items():
        row = {field: value for field, value in report.items() if field != "payload"}
        row["available_fields"] = list((report.get("payload") or {}).keys())
        reference = row.get("report_ref") or {}
        if strict and (not reference.get("artifact_id")
                       or not reference.get("path") or shared_path in str(reference.get("path"))):
            row.pop("report_ref", None)
            row["projection_status"] = "unavailable"
            row["projection_reason"] = "original_report_has_no_task_scoped_reference"
        index[key] = project(row)
    for field in ("sources", "journey_observations", "gap_plan", "rendered_observations",
                  "reference_patterns", "human_evidence"):
        if field in quality:
            quality[field] = project(quality[field]) or []
    for field in ("journey_observations", "gap_plan", "open_gaps", "verification_findings",
                  "rendered_observations", "sample_evidence", "human_evidence", "completed", "attempts"):
        if field in source:
            source[field] = project(source[field]) or []
    quality["member_reports"] = index
    # A merged frontier can contain unbounded free-form notes and mixed
    # authors. Project its documented decision fields from the selected
    # original reports; unknown fields remain available through the index.
    frontier = {"source_kind": "member_report", "source_report_ids": list(selected)}
    for field in ("analysis", "reference_patterns", "named_references", "dimension_leaders",
                  "concept_anchors", "implementation_brief", "dimension_acceptance",
                  "experience_dimensions", "query_ledger"):
        rows = []
        for report in selected.values():
            value = (report.get("payload") or {}).get(field)
            if isinstance(value, list):
                rows = _append_unique_facts(rows, value)
            elif isinstance(value, dict):
                rows = _append_unique_facts(rows, [value])
        if rows:
            frontier[field] = project(rows) or []
    for field in ("sources", "journey_observations", "gap_plan"):
        frontier[field] = _json_safe(quality.get(field) or [])
    source["frontier_context"] = _json_safe(frontier)
    quality["frontier_context"] = _json_safe(frontier)
    if strict:
        source["quality_context"] = project(quality) or {}
        source.pop("packet_path", None)


def task_workspace(packet: dict, task_id: str) -> str:
    """Project root or this task's real allocation, never a guessed shared root."""
    strict = (packet.get("visibility") or {}).get("candidate_isolation") == "strict"
    root = packet.get("repo_root") or packet.get("workspace_root")
    if root and (not strict or not task_id):
        return str(root)
    if not task_id:
        return ""
    task = next((row for row in packet.get("tasks") or []
                 if isinstance(row, dict) and row.get("task_id") == task_id), None)
    if not task:
        return ""
    from legion_sandbox import owned_workdir_allocations
    rows = owned_workdir_allocations(
        packet.get("team_id"), packet.get("user_id"), packet.get("conversation_id"))
    return next((row["root"] for row in reversed(rows)
                 if row["task_id"] == task_id and row["harness_key"] == task.get("assigned_harness")), "")


def snapshot(team_id: str, task_id: str = "", *, packet: dict | None = None,
             max_items: int = 40, execution_depth: str = "standard") -> dict:
    """Return a bounded project view; visibility policy can isolate PK candidate evidence."""
    source = _json_safe(packet) if isinstance(packet, dict) else load(team_id)
    if not source:
        return {}
    visibility = source.get("visibility") if isinstance(source.get("visibility"), dict) else {}
    isolation = str(visibility.get("candidate_isolation") or "")
    allowed_tasks = {str(task_id)}
    if isolation == "strict" and task_id:
        task_view = _task_view(source, task_id)
        # PK 候选之间无依赖；只有显式非候选上游才可共享，不能借 depends_on 绕过隔离。
        candidate_ids = {str(key) for key in (source.get("candidate_tasks") or {})}
        allowed_tasks |= {dep for dep in (task_view.get("direct_dependencies") or [])
                          if dep not in candidate_ids}
        source["completed"] = [item for item in (source.get("completed") or [])
                               if not isinstance(item, dict) or str(item.get("task_id") or "") in allowed_tasks]
        source["attempts"] = [item for item in (source.get("attempts") or [])
                              if not isinstance(item, dict) or str(item.get("task_id") or "") in allowed_tasks]
        source["artifacts"] = [item for item in (source.get("artifacts") or [])
                               if not isinstance(item, dict) or str(item.get("task_id") or "") in allowed_tasks]
        source["concept_anchors_sources"] = [
            item for item in (source.get("concept_anchors_sources") or [])
            if isinstance(item, dict) and str(item.get("task_id") or "") in allowed_tasks
        ]
    _project_member_reports(source, allowed_tasks if isolation == "strict" and task_id else None)
    view = {
        "schema_version": source.get("schema_version", 1),
        "team_id": source.get("team_id") or team_id,
        "conversation_id": source.get("conversation_id") or "",
        "user_id": source.get("user_id") or "",
        "objective": source.get("objective") or "",
        "source_ref": source.get("source_ref") or {},
        "input_snapshot": source.get("input_snapshot") or {},
        "version_identity": source.get("version_identity") or {},
        "execution_role": source.get("execution_role") or "champion",
        "mode": source.get("mode") or "",
        "captain": source.get("captain") or "",
        "north_star": source.get("north_star") or {},
        "critical_dimensions": source.get("critical_dimensions") or [],
        "claim_ceiling": source.get("claim_ceiling") or "UNVALIDATED",
        "quality_protocol": source.get("quality_protocol") or {},
        "captain_thesis": source.get("captain_thesis") or {},
        "audition_evidence": source.get("audition_evidence") or [],
        "current_stage": source.get("current_stage") or "",
        "task_view": _task_view(source, task_id) if task_id else {},
        "tasks": ([item for item in (source.get("tasks") or [])
                   if isolation != "strict" or not task_id
                   or str(item.get("task_id") or "") in allowed_tasks][:max_items]),
        "delivery_contract": source.get("delivery_contract") or {},
        "acceptance": source.get("acceptance") or [],
        "resource_budget": source.get("resource_budget") or {},
        "golden_task": source.get("golden_task") or {},
        "evaluation_dimensions": source.get("evaluation_dimensions") or [],
        "experience_intent": source.get("experience_intent") or {},
        "frontier_research_contract": source.get("frontier_research_contract") or {},
        "reference_atlas": source.get("reference_atlas") or {},
        "frontier_context": source.get("frontier_context") or {},
        "quality_context": source.get("quality_context") or {},
        "quality_context_revision": source.get("quality_context_revision") or 0,
        "current_hypothesis": source.get("current_hypothesis") or "",
        "sample_evidence": list(source.get("sample_evidence") or [])[-max_items:],
        "rendered_observations": list(source.get("rendered_observations") or [])[-max_items:],
        "journey_observations": list(source.get("journey_observations") or [])[-max_items:],
        "gap_plan": list(source.get("gap_plan") or [])[-max_items:],
        "concept_anchors": list(source.get("concept_anchors") or [])[-max_items:],
        "concept_anchors_revision": int(source.get("concept_anchors_revision") or 0),
        "concept_anchors_sources": list(source.get("concept_anchors_sources") or [])[-max_items:],
        "anchor_channel_conflict": source.get("anchor_channel_conflict") or {},
        "human_evidence": list(source.get("human_evidence") or [])[-max_items:],
        "effect_downgrades": list(source.get("effect_downgrades") or [])[-max_items:],
        "role_context": (source.get("role_contexts") or {}).get(str(task_id), {}),
        "delivery_contract_path": source.get("delivery_contract_path") or "",
        "candidate_task": (source.get("candidate_tasks") or {}).get(str(task_id), {}),
        "visibility": visibility,
        "decisions": list(source.get("decisions") or [])[-max_items:],
        "completed": list(source.get("completed") or [])[-max_items:],
        "open_gaps": list(source.get("open_gaps") or [])[-max_items:],
        "verification_findings": list(source.get("verification_findings") or [])[-max_items:],
        "artifacts": list(source.get("artifacts") or [])[-max_items:],
        "attempts": list(source.get("attempts") or [])[-max_items:],
        "packet_path": str(source.get("packet_path") or _path(team_id)),
        "updated_at": source.get("updated_at"),
    }
    if isolation == "strict" and task_id and (source.get("quality_context") or {}).get("member_reports"):
        view.pop("packet_path", None)
    # Running in an inherited project needs the same directory facts as the
    # full packet. Strict independent candidates retain their separate roots.
    if isolation != "strict" or not task_id:
        for field in ("repo_root", "workspace_root"):
            if source.get(field):
                view[field] = source[field]
    if not (view.get("repo_root") or view.get("workspace_root")):
        allocated = task_workspace(source, task_id)
        if allocated:
            view["workspace_root"] = allocated
    if str(execution_depth or "standard") == "quick":
        # Quick 是同一 packet 的最小投影，不是另一套事实源。只保留完成单点任务所需的目标、
        # 当前节点、明确验收、真实缺口/产物引用与恢复路径；世界级研究档案待升级后再读取完整 packet。
        view = {key: view.get(key) for key in (
            "schema_version", "team_id", "conversation_id", "user_id", "repo_root",
            "workspace_root", "objective", "source_ref",
            "input_snapshot", "version_identity", "execution_role", "mode", "current_stage",
            "task_view", "delivery_contract", "acceptance", "open_gaps", "verification_findings",
                "artifacts", "attempts", "packet_path", "updated_at")
                if view.get(key) not in (None, "", [], {})}
    if isolation != "strict" and view.get("artifacts"):
        view["artifacts"] = _snapshot_artifacts(
            team_id, source, list(view.get("artifacts") or []))
    canonical = json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    view["snapshot_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return view


def from_gate_snapshot(team_id: str, objective: str, gate_snapshot: dict, *,
                       mode: str, artifacts: list[dict] | None = None,
                       resource_budget: dict | None = None) -> dict:
    """Build a caller-provided packet for another iteration from verifier facts."""
    packet = {
        "schema_version": 1,
        "team_id": str(team_id),
        "objective": str(objective),
        "mode": str(mode),
        "current_stage": "revision",
        "north_star": _json_safe(gate_snapshot.get("north_star") or {}),
        "critical_dimensions": _json_safe(gate_snapshot.get("critical_dimensions") or []),
        "claim_ceiling": str(gate_snapshot.get("claim_ceiling") or "UNVALIDATED"),
        "quality_protocol": _json_safe(gate_snapshot.get("quality_protocol") or {}),
        "captain_thesis": _json_safe(gate_snapshot.get("captain_thesis") or {}),
        "audition_evidence": _json_safe(gate_snapshot.get("audition_evidence") or []),
        "resource_budget": _json_safe(resource_budget or {}),
        "completed": _json_safe(gate_snapshot.get("passed_checks") or []),
        "open_gaps": _json_safe(gate_snapshot.get("failed_checks") or []),
        "verification_findings": _json_safe(gate_snapshot.get("failed_checks") or []),
        "experience_intent": _json_safe(gate_snapshot.get("experience_intent") or {}),
        "frontier_research_contract": _json_safe(gate_snapshot.get("frontier_research_contract") or {}),
        "quality_context": _json_safe(gate_snapshot.get("quality_context") or {}),
        "quality_context_revision": int((gate_snapshot.get("quality_context") or {}).get("revision") or 0),
        "journey_observations": _json_safe(gate_snapshot.get("journey_observations") or []),
        "gap_plan": _json_safe(gate_snapshot.get("gap_plan") or []),
        "role_contexts": _json_safe(gate_snapshot.get("role_contexts") or {}),
        "artifacts": _json_safe(artifacts or []),
        "visibility": {},
        "decisions": [],
        "attempts": [],
        "updated_at": time.time(),
    }
    packet["packet_path"] = str(_path(team_id))
    return packet
