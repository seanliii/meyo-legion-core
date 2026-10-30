#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收中心行动层：持久任务、军团修复、分级复验与发布门收口。"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import evidence_schema
import legion_paths as _paths
import quality_executor
import release_gate
import verification_reviews as reviews

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows fallback
    _fcntl = None

_LOCK = threading.RLock()
_THREADS: dict[str, threading.Thread] = {}
_TERMINAL = {"completed", "failed", "cancelled", "blocked"}
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
_SKELETON_MANIFEST = _HERE / "skeleton_manifest.json"
_SKELETON_EVIDENCE = _HERE / "skeleton_change_evidence.json"
_SEVERITY_WEIGHT = {"P0": 100, "P1": 70, "P2": 40, "P3": 10}
_DIRECT_USER_EVIDENCE = {
    "frontend_journey", "desktop_delivery", "mobile_business", "release_observation",
    "quality_l3", "quality_l4", "desktop-task", "mobile-task", "frontend",
}


def _root() -> Path:
    return Path(_paths.home_under("verification-center", "workflow"))


def _safe(value: str) -> str:
    value = str(value or "")
    if not value or len(value) > 160 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-" for char in value):
        raise ValueError("invalid workflow id")
    return value


def _path(run_id: str) -> Path:
    return _root() / (_safe(run_id) + ".json")


@contextlib.contextmanager
def _run_lock(run_id: str):
    lock_path = _root() / (_safe(run_id) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


@contextlib.contextmanager
def _target_lock(target: str, kind: str):
    digest = hashlib.sha256(f"{kind}\0{target}".encode("utf-8")).hexdigest()[:24]
    lock_path = _root() / ("target-" + digest + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


def _write(row: dict) -> dict:
    target = _path(row["run_id"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return row


def _claim_run(target: str, kind: str, row: dict) -> tuple[dict, bool]:
    """跨进程原子完成“查重+排队”，避免多个 server worker 重复启动同一工作流。"""
    with _LOCK, _target_lock(target, kind):
        active = _active_for(target, kind)
        if active:
            return active, False
        _write(row)
        return row, True


def _pid_alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def get(run_id: str) -> dict | None:
    try:
        row = json.loads(_path(run_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if row.get("status") in {"queued", "running"} and run_id not in _THREADS:
        heartbeat = float(row.get("heartbeat_at") or row.get("updated_at") or 0)
        owner_alive = _pid_alive(row.get("owner_pid"))
        if not owner_alive and time.time() - heartbeat > 30:
            with _run_lock(run_id):
                current = json.loads(_path(run_id).read_text(encoding="utf-8"))
                current_heartbeat = float(current.get("heartbeat_at") or current.get("updated_at") or 0)
                if current.get("status") in {"queued", "running"} and not _pid_alive(current.get("owner_pid")) \
                        and time.time() - current_heartbeat > 30:
                    current["status"] = "interrupted"
                    current["stage"] = "interrupted"
                    current["message"] = "执行进程失联且心跳过期；现场已保留，可重新复验"
                    current["updated_at"] = time.time()
                    _write(current)
                row = current
    return row


def list_runs(target: str = "", limit: int = 100) -> list[dict]:
    if not _root().is_dir():
        return []
    rows = []
    for path in sorted(_root().glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        row = get(path.stem)
        if not row or target and row.get("target") != target:
            continue
        rows.append(row)
        if len(rows) >= max(1, min(int(limit), 500)):
            break
    return rows


def _event(row: dict, stage: str, message: str, **data) -> None:
    with _LOCK, _run_lock(row["run_id"]):
        if row.get("run_id") in _THREADS:
            current = row
        else:
            try:
                current = json.loads(_path(row["run_id"]).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                current = row
        current["status"] = "running" if stage not in _TERMINAL else stage
        current["stage"] = stage
        current["message"] = str(message)[:2000]
        current["updated_at"] = time.time()
        current["heartbeat_at"] = current["updated_at"]
        events = list(current.get("events") or [])
        events.append({"seq": len(events) + 1, "at": current["updated_at"], "stage": stage,
                       "message": current["message"], "data": data})
        current["events"] = events[-500:]
        _write(current)
        row.update(current)


def _start_worker(row: dict, work, *, name: str, prepare=None) -> dict:
    """A claimed run must not remain queued if preparation or Thread.start fails."""
    try:
        prepared = prepare() if prepare else None
        if isinstance(prepared, dict) and prepared.get("ok") is False:
            row.setdefault("result", {})["preparation"] = prepared
            _write(row)
            _event(row, "blocked", "任务准备被拒绝：" + str(prepared.get("error") or ""))
            return {**prepared, "run": row}
        thread = threading.Thread(target=work, name=name, daemon=True)
        _THREADS[row["run_id"]] = thread
        thread.start()
    except Exception as exc:
        _THREADS.pop(row["run_id"], None)
        error = f"{type(exc).__name__}: {exc}"
        row.setdefault("result", {})["start_error"] = error
        _write(row)
        _event(row, "failed", "任务未启动：" + error)
        current = reviews.get(row["target"])
        reviews.save(row["target"], str(current.get("verdict") or "pending"),
                     str(current.get("note") or ""), str(row.get("actor") or ""),
                     metadata={"machine_status": "interrupted", "upgrade_status": "blocked",
                               "machine_summary": row["message"]})
        return {"ok": False, "error": error, "run": row}
    return {"ok": True, "run": row}


def _start_followup(row: dict, kind: str, start, **kwargs) -> None:
    """Persist the handoff outcome; the parent's own work is already terminal."""
    failure_status = "blocked"
    try:
        follow = start(row["target"], **kwargs)
        if not isinstance(follow, dict) or (
                follow.get("ok") and not (follow.get("run") or {}).get("run_id")):
            raise ValueError("followup_run_id_required")
    except Exception as exc:
        follow = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        failure_status = "failed"
    result = row.setdefault("result", {})
    result[f"{kind}_start"] = {key: value for key, value in follow.items() if key != "run"}
    result[f"{kind}_run_id"] = (follow.get("run") or {}).get("run_id")
    # The reverify worker has left _THREADS, so _event will reload this file.
    _write(row)
    if not follow.get("ok"):
        message = "后续任务未启动：" + str(follow.get("error") or "unknown_error")
        _event(row, failure_status, message, followup_kind=kind)
        current = reviews.get(row["target"])
        reviews.save(row["target"], str(current.get("verdict") or "pending"),
                     str(current.get("note") or ""), str(row.get("actor") or ""),
                     metadata={"machine_status": "interrupted" if failure_status == "failed" else "issue",
                               "upgrade_status": "blocked", "machine_summary": message})


def _levels(review: dict, requested: list[str] | None) -> list[str]:
    values = requested or review.get("required_levels") or ["L0", "L1", "L2", "L3", "L4"]
    return quality_executor.normalize_levels(values)


def _active_for(target: str, kind: str) -> dict | None:
    return next((row for row in list_runs(target, 20)
                 if row.get("kind") == kind and row.get("status") in {"queued", "running"}), None)


def _commit() -> str:
    return evidence_schema._commit(Path(__file__).resolve().parents[2])


def start_reverify(target: str, *, levels: list[str] | None = None,
                   live_harnesses: list[str] | None = None, actor: str = "verification-center",
                   phase: str = "release", continuous: bool | None = None,
                   cycle: int = 1, user_id: str = "", conversation_id: str = "") -> dict:
    review = reviews.get(target)
    if not review:
        return {"ok": False, "error": "先在验收中心创建产品记录"}
    if not review.get("tracked_files"):
        return {"ok": False, "error": "先填写关联文件，复验才能锁定真实影响面"}
    if not (review.get("required_levels") or levels):
        return {"ok": False, "error": "先声明发布所需 L0-L4 验收等级"}
    phase = str(phase or "release").lower()
    required_levels = _levels(review, levels)
    selected = quality_executor.levels_for_phase(required_levels, phase)
    if not selected:
        return {"ok": False, "error": "当前阶段没有可执行的验收等级"}
    continuous = bool(review.get("continuous_reverify", True) if continuous is None else continuous)
    run_id = evidence_schema.new_run_id("fast-verify" if phase == "fast" else "release-verify")
    row = {"run_id": run_id, "kind": "reverify", "target": target, "actor": actor,
           "user_id": user_id, "conversation_id": conversation_id,
           "status": "queued", "stage": "queued",
           "message": "快速开发验收已排队" if phase == "fast" else "发布复验已排队",
           "phase": phase, "cycle": max(1, int(cycle)), "continuous": continuous,
           "levels": selected, "live_harnesses": list(live_harnesses or []),
           "owner_pid": os.getpid(), "heartbeat_at": time.time(),
           "created_at": time.time(), "updated_at": time.time(), "events": [], "result": {}}
    claimed, created = _claim_run(target, "reverify", row)
    if not created:
        return {"ok": True, "status": "already_running", "run": claimed}
    row = claimed
    previous_status = str(review.get("upgrade_status") or "not_planned")
    next_status = "in_progress" if previous_status in {"planned", "in_progress", "blocked"} else previous_status

    def prepare():
        return reviews.save(
            target, str(review.get("verdict") or "pending"), str(review.get("note") or ""), actor,
            metadata={"machine_status": "queued", "evidence_run_id": run_id,
                      "upgrade_status": next_status, "required_levels": required_levels,
                      "require_machine_gate": True, "require_lifecycle_gate": True})

    def work():
        try:
            _event(row, "running", "正在执行快速开发验收" if phase == "fast" else "正在执行发布质量验收",
                   levels=selected, phase=phase)
            current = reviews.get(target)
            result = quality_executor.execute_levels(
                target=target, levels=selected, files=current.get("tracked_files") or [],
                run_id=run_id, live_harnesses=list(live_harnesses or []),
                live=bool(live_harnesses), trigger="verification-center", phase=phase, reuse=True)
            recorded = reviews.record_machine_result(target, result, actor=actor)
            row["result"] = {"execution": result, "review": recorded.get("review") or {}}
            cycle_limit = int((recorded.get("review") or current).get("max_auto_cycles") or 2)
            _event(row, "completed" if result.get("ok") else "failed",
                   (f"{phase == 'fast' and '快速开发验收' or '发布复验'}通过 · "
                    f"执行 {len(result.get('executed_levels') or [])} 层 · "
                    f"复用 {len(result.get('reused_levels') or [])} 层 · "
                    f"节省约 {result.get('saved_seconds') or 0}s")
                   if result.get("ok") else "复验发现阻塞，已写入产品待办",
                   failed_levels=[item.get("level") for item in result.get("failures") or []],
                   executed_levels=result.get("executed_levels") or [],
                   reused_levels=result.get("reused_levels") or [],
                   saved_seconds=result.get("saved_seconds") or 0)
            if not result.get("ok") and continuous and row["cycle"] < cycle_limit:
                _event(row, "failed", "发现回归，准备自动交给军团修复；修复后将继续复验",
                       cycle=row["cycle"])
                row["followup"] = {"kind": "repair", "phase": phase,
                                   "cycle": row["cycle"] + 1}
                _write(row)
        except Exception as exc:
            review_now = reviews.get(target)
            reviews.save(target, str(review_now.get("verdict") or "pending"),
                         str(review_now.get("note") or ""), actor,
                         metadata={"machine_status": "interrupted", "upgrade_status": "blocked",
                                   "machine_summary": f"{type(exc).__name__}: {exc}"})
            _event(row, "failed", f"复验异常：{type(exc).__name__}: {exc}")
        finally:
            _THREADS.pop(run_id, None)
            followup = row.get("followup") if isinstance(row.get("followup"), dict) else {}
            if followup.get("kind") == "repair":
                _start_followup(
                    row, "repair", start_repair, actor=actor, auto_reverify=True,
                    reverify_phase=str(followup.get("phase") or "release"),
                    cycle=int(followup.get("cycle") or 1),
                    user_id=user_id, conversation_id=conversation_id)

    return _start_worker(row, work, name=f"verification-{run_id[-12:]}", prepare=prepare)


def _extract_json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
    candidates = [text]
    if "```" in text:
        candidates.extend(part.split("```", 1)[0] for part in text.split("```json")[1:])
    left, right = text.find("{"), text.rfind("}")
    if 0 <= left < right:
        candidates.append(text[left:right + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate.strip())
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            continue
    return {}


def _root_cause_question(review: dict) -> str:
    facts = {
        "finding_id": review.get("source_finding_id"), "kind": review.get("source_finding_kind"),
        "target": review.get("target"), "feature": review.get("feature"),
        "evidence": review.get("evidence"), "machine_summary": review.get("machine_summary"),
        "skeleton_zones": review.get("skeleton_zones") or [],
    }
    return ("这是验收中心自动发现但尚未定位源码根因的问题。请只做调查，不改任何文件：读取真实运行 revision、"
            "源码和失败现场，提出可证伪根因；至少给出一个仓库内相对 file、正整数 line、该行原文 quote、"
            "runtime_evidence 和 verifier。若证据不足必须返回 verified=false，不得猜路径。末尾只输出 JSON："
            "{\"verified\":true|false,\"root_cause\":\"\",\"member\":\"\",\"dimension\":\"\","
            "\"source_root\":\"绝对路径\",\"revision\":\"\",\"file\":\"相对路径\",\"line\":1,"
            "\"quote\":\"该行原文\",\"runtime_evidence\":\"\",\"verifier\":\"\"}。现场：\n" +
            json.dumps(facts, ensure_ascii=False, indent=2))


def start_root_cause_investigation(target: str, *, actor: str = "verification-center",
                                  user_id: str = "", conversation_id: str = "") -> dict:
    """只读定位并独立核验源码根因；核验通过前不允许进入修复或骨架升级。"""
    import legion_core
    if (legion_core._personal_responses_planner_enabled()
            and not legion_core._planner_identity_valid(user_id, conversation_id)):
        return {"ok": False, "error": "planner_identity_required"}
    review = reviews.get(target)
    if not review:
        return {"ok": False, "error": "产品记录不存在"}
    if str(review.get("root_cause_status") or "unresolved") == "verified" and review.get("tracked_files"):
        return {"ok": True, "status": "already_verified", "review": review}
    run_id = evidence_schema.new_run_id("root-cause")
    row = {"run_id": run_id, "kind": "root_cause", "target": target, "actor": actor,
           "user_id": user_id, "conversation_id": conversation_id,
           "status": "queued", "stage": "queued", "message": "源码根因调查已排队",
           "owner_pid": os.getpid(), "heartbeat_at": time.time(),
           "created_at": time.time(), "updated_at": time.time(), "events": [], "result": {}}
    claimed, created = _claim_run(target, "root_cause", row)
    if not created:
        return {"ok": True, "status": "already_running", "run": claimed}
    row = claimed

    def work():
        try:
            import legion_core
            _event(row, "running", "军团正在只读核对真实 revision、源码 file:line 与运行证据")
            question = _root_cause_question(reviews.get(target))
            valid = legion_core._harness_keys()
            investigator = legion_core._pick_relay_harness("", set(), valid) or ""
            verifier = legion_core._pick_relay_harness("", {investigator}, valid) or ""
            if not investigator or not verifier:
                raise RuntimeError("至少需要两个可运行且彼此独立的 Harness 才能核验根因")
            plan = {"tasks": {
                "investigate": {"task": question, "assigned_harness": investigator,
                                "assignment_locked": True, "verification_role": "root_cause_investigator",
                                "depends_on": []},
                "verify": {"task": question + "\n独立复核上游的源码引用、revision 与运行证据；"
                                    "只有逐项可复现时才返回 verified=true，不得相信其自报结论。",
                           "assigned_harness": verifier, "assignment_locked": True,
                           "verification_role": "root_cause_auditor",
                           "depends_on": ["investigate"], "dependency_policy": "all_finished"}},
                    "verification": {"enabled": False},
                    "delivery_contract": {"required_artifacts": [], "require_final": True},
                    "_user_selected_mode": True}
            events = []
            result = legion_core.dispatch(
                question, plan=plan, cb=lambda event: events.append(event), qhash=run_id,
                mode="orchestrate", user_id=user_id, conversation_id=conversation_id)
            candidate_rows = {str(item.get("task_id") or ""): item for item in result.get("results") or []}
            investigation_row = candidate_rows.get("investigate") or {}
            verification_row = candidate_rows.get("verify") or {}
            investigation = _extract_json_object(str(investigation_row.get("final") or ""))
            actual_investigator = str(investigation_row.get("harness") or "")
            actual_verifier = str(verification_row.get("harness") or "")
            payload = _extract_json_object(str(verification_row.get("final") or ""))
            same_reference = bool(investigation) and all(
                str(payload.get(name) or "") == str(investigation.get(name) or "")
                for name in ("revision", "file", "line", "quote"))
            if (actual_investigator != investigator or actual_verifier != verifier
                    or actual_investigator == actual_verifier
                    or investigation_row.get("via_relay") is True or verification_row.get("via_relay") is True
                    or payload.get("verified") is not True or not same_reference
                    or str(payload.get("runtime_evidence") or "") in {"", "runtime_not_tested"}):
                reviews.save(target, str(review.get("verdict") or "issue"), str(review.get("note") or ""), actor,
                             metadata={"root_cause_status": "unresolved",
                                       "machine_summary": str(payload.get("root_cause") or "根因证据不足")},
                             _workflow_token=reviews._WORKFLOW_TOKEN)
                row["result"] = {"dispatch": result, "events": events[-200:],
                                 "investigation": investigation, "finding": payload}
                _event(row, "blocked", "源码根因尚未被独立核验；保持骨架锁定")
                return
            try:
                import source_audit
                recorded = source_audit.record({
                    "audit_id": f"root-{review.get('source_finding_id') or run_id}",
                    "finding_id": str(review.get("source_finding_id") or ""),
                    "target": target,
                    "member": str(payload.get("member") or review.get("source_finding_kind") or "system"),
                    "dimension": str(payload.get("dimension") or review.get("source_finding_kind") or "quality"),
                    "claim": str(payload.get("root_cause") or ""), "source_root": str(_REPO),
                    "revision": str(payload.get("revision") or ""), "file": str(payload.get("file") or ""),
                    "line": int(payload.get("line") or 0), "quote": str(payload.get("quote") or ""),
                    "runtime_evidence": payload.get("runtime_evidence") or "runtime_not_tested",
                    "verifier": actual_verifier, "status": "verified",
                }, "default")
            except Exception as exc:
                recorded = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if not recorded.get("ok"):
                reviews.save(target, str(review.get("verdict") or "issue"), str(review.get("note") or ""), actor,
                             metadata={"root_cause_status": "contradicted",
                                       "machine_summary": "源码引用核验失败：" + str(recorded.get("error") or "未知错误")},
                             _workflow_token=reviews._WORKFLOW_TOKEN)
                row["result"] = {"dispatch": result, "events": events[-200:], "finding": payload,
                                 "source_audit": recorded}
                _event(row, "blocked", "源码引用无法复现；保持骨架锁定")
                return
            entry = recorded["entry"]
            root = Path(str(entry["source_root"])); absolute = (root / str(entry["file"])).resolve()
            try:
                tracked = absolute.relative_to(_REPO).as_posix()
            except ValueError:
                tracked = ""
            if not tracked:
                raise ValueError("根因文件不在当前产品仓库，不能作为本仓骨架升级依据")
            saved = reviews.save(
                target, str(review.get("verdict") or "issue"), str(review.get("note") or ""), actor,
                metadata={"tracked_files": list(dict.fromkeys([*(review.get("tracked_files") or []), tracked])),
                          "root_cause": str(entry.get("claim") or entry.get("quote") or ""),
                          "root_cause_status": "verified", "root_cause_evidence": [entry],
                          "upgrade_status": "planned" if set(review.get("skeleton_zones") or []) & {"red", "yellow"}
                          else "blocked", "next_action": "根因已核验；执行最小修复后自动复验"},
                _workflow_token=reviews._WORKFLOW_TOKEN)
            row["result"] = {"dispatch": result, "events": events[-200:], "source_audit": recorded,
                             "review": saved.get("review") or {}}
            _event(row, "completed", "源码根因已独立核验并绑定影响文件；现在才允许进入最小修复")
        except Exception as exc:
            _event(row, "failed", f"根因调查异常：{type(exc).__name__}: {exc}")
        finally:
            _THREADS.pop(run_id, None)

    return _start_worker(row, work, name=f"root-cause-{run_id[-12:]}")


def _repair_question(review: dict) -> str:
    gate = reviews.product_gate(review)
    facts = {
        "target": review.get("target"), "feature": review.get("feature"),
        "current_version": review.get("current_version"), "target_version": review.get("target_version"),
        "user_value": review.get("user_value"), "acceptance_criteria": review.get("acceptance_criteria"),
        "machine_summary": review.get("machine_summary"), "machine_comparison": review.get("machine_comparison"),
        "risk": review.get("risk"), "blockers": gate.get("blockers"),
        "tracked_files": review.get("tracked_files") or [], "evidence_paths": review.get("evidence_paths") or [],
    }
    return ("这是开发验收中心发现的一项真实产品质量任务。请先读取目标相关源码、现有机器证据和失败现场，"
            "自主判断根因并完成可逆修复；不要只写建议，不要把错误藏在兜底后。修复后运行对口的最小验证，"
            "交付改动文件、结果与仍未解决风险。产品现场：\n" + json.dumps(facts, ensure_ascii=False, indent=2))


def start_repair(target: str, *, actor: str = "verification-center",
                 auto_reverify: bool = True, reverify_phase: str = "release",
                 cycle: int = 1, user_id: str = "", conversation_id: str = "") -> dict:
    import legion_core
    _personal_planner = legion_core._personal_responses_planner_enabled()
    if _personal_planner and not legion_core._planner_identity_valid(user_id, conversation_id):
        # actor is display/audit metadata and may originate in a request body.
        # A legacy caller without a trusted subject cannot borrow service-owner
        # authority or fall back to the company CC runner.
        return {"ok": False, "error": "planner_identity_required"}
    review = reviews.get(target)
    if not review:
        return {"ok": False, "error": "先在验收中心创建产品记录"}
    if review.get("source_finding_id") and str(review.get("root_cause_status") or "unresolved") \
            not in {"verified", "not_required"}:
        return {"ok": False, "error": "自动发现的问题尚未完成源码级根因核验；禁止先改代码或骨架"}
    if not review.get("tracked_files"):
        return {"ok": False, "error": "先填写关联文件，军团才能读取正确源码现场"}
    run_id = evidence_schema.new_run_id("repair")
    row = {"run_id": run_id, "kind": "repair", "target": target, "actor": actor,
           "user_id": user_id, "conversation_id": conversation_id,
           "status": "queued", "stage": "queued", "message": "军团修复已排队",
           "owner_pid": os.getpid(), "heartbeat_at": time.time(),
           "levels": _levels(review, None), "auto_reverify": bool(auto_reverify),
           "reverify_phase": str(reverify_phase or "release"), "cycle": max(1, int(cycle)),
           "created_at": time.time(), "updated_at": time.time(), "events": [], "result": {}}
    claimed, created = _claim_run(target, "repair", row)
    if not created:
        return {"ok": True, "status": "already_running", "run": claimed}
    row = claimed

    def prepare():
        if str(review.get("upgrade_status") or "not_planned") in {"released", "observing", "ready"}:
            blocked = reviews.record_repair(target, run_id=run_id, status="blocked",
                                           summary="发现回归，已阻止当前版本继续发布", actor=actor)
            if isinstance(blocked, dict) and blocked.get("ok") is False:
                return blocked
        return reviews.record_repair(target, run_id=run_id, status="in_progress",
                                     summary="军团正在读取源码与失败证据并修复", actor=actor)

    def work():
        try:
            import legion_core
            _event(row, "running", "军团正在读取源码、证据与失败现场")
            events = []
            question = _repair_question(reviews.get(target))
            planned = legion_core.plan_request_with_model(
                question, user_id=user_id, conversation_id=conversation_id,
                cancel_check=lambda: legion_core.is_cancelled(run_id))
            if not planned.get("ok") and (
                    _personal_planner or planned.get("cancelled") or
                    __import__("error_envelope").requires_execution_pause(planned)):
                row["result"] = {"planning": planned}
                reviews.record_repair(target, run_id=run_id, status="blocked",
                                      summary="规划未完成，保留运行事实；未派出默认成员", actor=actor)
                _event(row, "cancelled" if planned.get("cancelled") else "blocked",
                       "修复规划未完成，未派出默认成员")
                return
            plan = dict(planned) if planned.get("ok") else {
                "tasks": {"repair": {"task": question, "assigned_harness": "cc",
                                       "depends_on": [], "direction": "programming"}},
                "_planner_generated": True,
                "verification": {"enabled": True},
                "delivery_contract": {"required_artifacts": [], "require_final": True},
            }
            plan.setdefault("verification", {"enabled": True})
            plan.setdefault("delivery_contract", {"required_artifacts": [], "require_final": True})
            _event(row, "running", "军团已完成修复分工，开始执行", plan_reason=plan.get("reason"))
            result = legion_core.dispatch_persistent(
                question, plan=plan,
                cb=lambda event: events.append(event), qhash=run_id, mode="orchestrate",
                user_id=user_id, conversation_id=conversation_id, max_rounds=4)
            row["result"] = {"dispatch": result, "events": events[-200:]}
            delivered = int(result.get("delivered_count") or 0) > 0 and not result.get("all_failed")
            if not delivered:
                reviews.record_repair(target, run_id=run_id, team_id=str(result.get("team_id") or ""),
                                      status="blocked", summary="军团未形成可交付修复", actor=actor)
                _event(row, "blocked", "军团未形成可交付修复，保留完整失败现场")
                return
            reviews.record_repair(target, run_id=run_id, team_id=str(result.get("team_id") or ""),
                                  status="in_progress", summary="修复已交付，等待机器复验", actor=actor)
            _event(row, "completed", "修复已交付，准备执行同一目标的机器复验",
                   team_id=result.get("team_id"), delivered_count=result.get("delivered_count"))
            if auto_reverify:
                _start_followup(
                    row, "reverify", start_reverify, levels=_levels(reviews.get(target), None), actor=actor,
                    phase=reverify_phase, continuous=True, cycle=cycle,
                    user_id=user_id, conversation_id=conversation_id)
        except Exception as exc:
            reviews.record_repair(target, run_id=run_id, status="blocked",
                                  summary=type(exc).__name__, actor=actor)
            _event(row, "failed", f"修复异常：{type(exc).__name__}")
        finally:
            _THREADS.pop(run_id, None)

    return _start_worker(row, work, name=f"repair-{run_id[-12:]}", prepare=prepare)


def adopt_evidence_issue(item: dict, *, actor: str = "verification-center") -> dict:
    evidence_id = str(item.get("id") or "")
    if not evidence_id:
        return {"ok": False, "error": "缺 evidence id"}
    target = str(((item.get("details") or {}).get("subject") or {}).get("target") or
                 f"evidence:{evidence_id}")
    existing = reviews.get(target)
    if existing:
        return {"ok": True, "review": existing, "status": "already_tracked"}
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    lineage = details.get("lineage") if isinstance(details.get("lineage"), dict) else {}
    tracked = list(((details.get("source") or {}).get("inputs") or {}).keys())
    manifest = _read_json(_SKELETON_MANIFEST)
    zones = _zones_for(tracked, manifest) if tracked else []
    protected = bool(set(zones) & {"red", "yellow"})
    return reviews.save(
        target, "issue", str(item.get("summary") or "机器证据发现问题"), actor,
        feature=str(item.get("title") or item.get("kind") or "机器质量问题"),
        priority="P1", upgrade_status="blocked", machine_status="issue",
        source_finding_id=evidence_id, source_finding_kind=str(item.get("kind") or "machine_evidence"),
        skeleton_zones=zones, root_cause_status="unresolved" if protected or not tracked else "not_required",
        evidence_ids=[evidence_id], evidence_run_id=str(lineage.get("run_id") or ""),
        evidence=str(item.get("digest") or evidence_id), tracked_files=tracked,
        require_machine_gate=True, require_lifecycle_gate=True,
        acceptance_criteria="对应机器证据在当前提交重新执行并通过",
        next_action=("先定位并独立核验源码根因；根因未明禁止改骨架" if protected or not tracked
                     else "让军团读取失败证据和相关源码，完成修复后自动复验"),
        _workflow_token=reviews._WORKFLOW_TOKEN)


def run_quality_operator_once(machine_items: list[dict] | None = None, *,
                              actor: str = "quality-loop", execute: bool = True,
                              allow_root_cause_investigation: bool = True,
                              user_id: str = "", conversation_id: str = "") -> dict:
    """每轮只推进一个最高用户影响项；骨架观察与发布动作永不自动执行。"""
    if machine_items is None:
        try:
            import verification_center
            machine_items = verification_center.machine_evidence().get("items") or []
        except Exception:
            machine_items = []
    intelligence = quality_intelligence(machine_items)
    by_id = {str(row.get("id") or ""): row for row in intelligence.get("findings") or []}
    ranked = [row for row in intelligence.get("user_outcome_ranking") or []
              if str(row.get("finding_id") or "") in by_id]
    selected = next((by_id[str(row["finding_id"])] for row in ranked
                     if str(by_id[str(row["finding_id"])].get("category") or "")
                     != "skeleton-observation"
                     and str(by_id[str(row["finding_id"])].get("evidence_status") or "current")
                     != "historical"), None)
    base = {"ok": True, "generated_at": time.time(), "execute": bool(execute),
            "top_user_outcome": intelligence.get("top_user_outcome"),
            "skeleton": intelligence.get("skeleton") or {},
            "finding": selected or {}}
    if selected is None:
        return {**base, "status": "observe", "reason": "没有可执行用户结果缺口；骨架漂移仅供观察"}
    if not execute:
        return {**base, "status": "planned", "reason": selected.get("suggested_action") or "待执行"}
    adopted = adopt_intelligence_finding(selected, actor=actor)
    if not adopted.get("ok"):
        return {**base, "ok": False, "status": "blocked", "adoption": adopted}
    review = adopted.get("review") or reviews.get(str(selected.get("target") or ""))
    target = str(review.get("target") or selected.get("target") or "")
    decision = reviews.product_manager_decision(review, reviews.product_gate(review))
    if decision.get("action") == "investigate_root_cause" and not allow_root_cause_investigation:
        return {**base, "status": "planned", "target": target, "adoption": adopted,
                "decision": decision, "reason": "低负载轮不启动双成员根因调查"}
    if not decision.get("autonomous"):
        return {**base, "status": "waiting_human", "target": target,
                "adoption": adopted, "decision": decision}
    action = run_recommended(target, actor=actor, user_id=user_id, conversation_id=conversation_id)
    return {**base, "status": "started" if action.get("ok") else "blocked",
            "target": target, "adoption": adopted, "decision": decision, "action": action}


def run_recommended(target: str, *, actor: str = "verification-center",
                    user_id: str = "", conversation_id: str = "") -> dict:
    """执行系统给出的唯一下一动作；发布、观察和价值确认仍保留人类决策。"""
    review = reviews.get(target)
    if not review:
        return {"ok": False, "error": "产品记录不存在"}
    gate = reviews.product_gate(review)
    decision = reviews.product_manager_decision(review, gate)
    action = decision["action"]
    if action == "investigate_root_cause":
        return start_root_cause_investigation(target, actor=actor,
                                             user_id=user_id, conversation_id=conversation_id)
    if action in {"repair"}:
        return start_repair(target, actor=actor, auto_reverify=True,
                            user_id=user_id, conversation_id=conversation_id)
    if action == "reverify_release":
        return start_reverify(target, actor=actor, phase="release", continuous=True,
                              user_id=user_id, conversation_id=conversation_id)
    if action == "mark_ready":
        return transition(target, "ready", actor=actor, note="机器证据闭合，系统推进到待发布")
    if action == "mark_validated":
        return transition(target, "validated", actor=actor, note="发布后价值证据闭合")
    if action in {"release", "observe", "record_outcome", "complete_brief"}:
        return {"ok": False, "requires_human": True, "decision": decision,
                "error": "该动作需要产品负责人确认或补充信息"}
    return {"ok": True, "status": "stable", "decision": decision}


def transition(target: str, state: str, *, actor: str = "verification-center", note: str = "") -> dict:
    review = reviews.get(target)
    if not review:
        return {"ok": False, "error": "产品记录不存在"}
    state = str(state or "")
    if state in {"released", "observing", "validated"}:
        decision = release_decision(target)
        if not decision.get("release_ready"):
            return {"ok": False, "error": "发布门未通过", "decision": decision}
    return reviews.save(target, str(review.get("verdict") or "pending"),
                        str(review.get("note") or note or ""), actor,
                        metadata={"upgrade_status": state}, _workflow_token=reviews._WORKFLOW_TOKEN)


def release_decision(target: str) -> dict:
    review = reviews.get(target)
    if not review:
        return {"ok": False, "release_ready": False, "blockers": ["没有产品记录"]}
    paths = [path for path in review.get("evidence_paths") or [] if Path(path).is_file()]
    if not paths and review.get("evidence_run_id"):
        paths = quality_executor.evidence_paths(
            target=target, run_id=str(review.get("evidence_run_id") or ""))
    required = tuple(review.get("required_evidence") or [])
    return release_gate.unified_release_gate(
        target=target, review=review, evidence_paths=paths, required_subjects=required,
        commit=_commit(), required_run_id=str(review.get("evidence_run_id") or ""),
        input_root=Path(__file__).resolve().parents[2])


def _read_json(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _git_output(args: list[str], repo: Path) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _dirty_paths(repo: Path) -> set[str]:
    changed = set(_git_output(["diff", "--name-only", "HEAD", "--"], repo).splitlines())
    changed.update(_git_output(["ls-files", "--others", "--exclude-standard"], repo).splitlines())
    return {item.strip() for item in changed if item.strip()}


def _zone_index(manifest: dict) -> dict[str, str]:
    index = {}
    for name, row in (manifest.get("files") or {}).items():
        path = str((row or {}).get("path") or "")
        zone = str((row or {}).get("zone") or "green")
        if path:
            index[path] = zone
        index.setdefault(str(name), zone)
    return index


def _zones_for(files: list[str], manifest: dict) -> list[str]:
    index = _zone_index(manifest)
    zones = set()
    for raw in files:
        path = str(raw or "").strip().replace("\\", "/")
        zone = index.get(path) or index.get(Path(path).name) or "green"
        zones.add(zone)
    return sorted(zones, key=lambda value: {"red": 0, "yellow": 1, "green": 2}.get(value, 3))


def _evidence_digest(names: list[str], manifest: dict, repo: Path) -> str:
    refs = manifest.get("files") or {}
    parts = []
    for name in sorted(names):
        path = repo / str((refs.get(name) or {}).get("path") or name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        parts.append(f"{name}:{digest}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _ticket_age(row: dict, now: float) -> float | None:
    value = str(row.get("generated_at") or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, now - parsed.timestamp())
    except ValueError:
        return None


def skeleton_upgrade_plan(*, repo: str | Path | None = None,
                          manifest_path: str | Path | None = None,
                          evidence_path: str | Path | None = None,
                          now: float | None = None) -> dict:
    """判断当前结构漂移该保持、补证据还是更新骨架；只出计划，绝不自动改红黄区。"""
    root = Path(repo or _REPO).resolve()
    manifest_file = Path(manifest_path or _SKELETON_MANIFEST)
    evidence_file = Path(evidence_path or _SKELETON_EVIDENCE)
    manifest, evidence = _read_json(manifest_file), _read_json(evidence_file)
    refs = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    if not refs:
        return {"status": "observer_unavailable", "legal": True, "release_blocking": False,
                "protected_drift": [], "zones": {"red": [], "yellow": []},
                "reasons": ["骨架观察基线不可读；不影响提交或发布"],
                "recommended_command": "", "required_steps": [],
                "requires_human_approval": False, "automatic_mutation_allowed": False}
    drift = {"red": [], "yellow": []}
    for name, row in refs.items():
        zone = str((row or {}).get("zone") or "green")
        if zone not in drift:
            continue
        path = root / str((row or {}).get("path") or name)
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        if digest != str((row or {}).get("content_sha256") or ""):
            drift[zone].append(name)
    dirty = _dirty_paths(root)
    protected_paths = {str((refs.get(name) or {}).get("path") or name): name for name in refs}
    dirty_protected = sorted(protected_paths[path] for path in dirty if path in protected_paths)
    changed = sorted(drift["red"] + drift["yellow"])
    if not changed:
        return {"status": "clean", "legal": True, "release_blocking": False,
                "protected_drift": [], "zones": drift, "dirty_protected": dirty_protected,
                "reasons": ["当前代码与骨架观察基线一致"], "recommended_command": "",
                "required_steps": [], "requires_human_approval": False,
                "automatic_mutation_allowed": False}
    return {
        "status": "advisory_drift", "legal": True, "release_blocking": False,
        "protected_drift": changed, "zones": drift, "dirty_protected": dirty_protected,
        "reasons": [
            "检测到骨架观察基线漂移；该信息不阻断提交、测试或发布",
            "请按本次改动影响面运行最小相关验证，避免为历史累计漂移重复跑全量矩阵",
        ],
        "recommended_command": "python3.12 tests/test_skeleton_guard.py --update-manifest",
        "required_steps": [], "requires_human_approval": False,
        "automatic_mutation_allowed": False,
        "evidence_status": "optional",
    }


def _finding(source: str, category: str, title: str, *, severity: str = "P2",
             recurrence: int = 1, evidence: list | None = None,
             evidence_status: str = "current",
             tracked_files: list | None = None, target: str = "",
             root_cause_status: str = "unresolved", root_cause: str = "",
             root_cause_evidence: list | None = None, user_outcome: str = "",
             experience_dimension: str = "", direct_user_evidence: bool = False,
             affected_users: int = 0, failure_rate: float | None = None,
             release_blocking: bool = False, next_validation: str = "",
             evidence_ids: list | None = None, evidence_paths: list | None = None,
             evidence_run_id: str = "") -> dict:
    proof = [str(item)[:1000] for item in (evidence or []) if str(item).strip()]
    files = list(dict.fromkeys(str(item) for item in (tracked_files or []) if str(item)))[:100]
    identity = hashlib.sha256(
        f"{source}:{category}:{' '.join(str(title).lower().split())}".encode()).hexdigest()[:24]
    cause_status = str(root_cause_status or "unresolved")
    if cause_status not in {"not_required", "unresolved", "localized", "verified", "contradicted"}:
        cause_status = "unresolved"
    return {"id": identity, "source": source, "category": category, "title": str(title)[:500],
            "severity": severity if severity in _SEVERITY_WEIGHT else "P2",
            "evidence_status": str(evidence_status or "current"),
            "recurrence": max(1, int(recurrence or 1)), "evidence": proof,
            "tracked_files": files, "target": str(target or ""),
            "root_cause_status": cause_status, "root_cause": str(root_cause or "")[:4000],
            "root_cause_evidence": [dict(item) for item in (root_cause_evidence or [])
                                    if isinstance(item, dict)][:50],
            "user_outcome": str(user_outcome or title)[:1000],
            "experience_dimension": str(experience_dimension or category)[:160],
            "direct_user_evidence": bool(direct_user_evidence),
            "affected_users": max(0, int(affected_users or 0)),
            "failure_rate": (max(0.0, min(1.0, float(failure_rate)))
                             if isinstance(failure_rate, (int, float)) else None),
            "release_blocking": bool(release_blocking),
            "next_validation": str(next_validation or "")[:1200],
            "evidence_ids": list(dict.fromkeys(str(item) for item in (evidence_ids or []) if str(item)))[:50],
            "evidence_paths": list(dict.fromkeys(str(item) for item in (evidence_paths or []) if str(item)))[:100],
            "evidence_run_id": str(evidence_run_id or "")[:160]}


def _user_outcome_priority(finding: dict) -> dict:
    """只按用户结果与证据事实排优先级；红黄区只决定修法，不给价值加分。"""
    severity = {"P0": 80, "P1": 60, "P2": 35, "P3": 10}.get(
        str(finding.get("severity") or "P2"), 35)
    recurrence = min(45, max(0, int(finding.get("recurrence") or 0)) * 5)
    affected = min(60, int(12 * __import__("math").log2(max(1, int(
        finding.get("affected_users") or 0)) + 1)))
    failure_rate = finding.get("failure_rate")
    failure_impact = round(70 * float(failure_rate)) if isinstance(failure_rate, (int, float)) else 0
    direct = 120 if finding.get("direct_user_evidence") else 0
    release = 80 if finding.get("release_blocking") else 0
    evidence = 20 if finding.get("evidence") else 0
    actionable = 15 if finding.get("next_validation") else 0
    score = severity + recurrence + affected + failure_impact + direct + release + evidence + actionable
    evidence_strength = "direct" if finding.get("direct_user_evidence") else \
        "indirect" if finding.get("evidence") else "missing"
    next_validation = str(finding.get("next_validation") or "")
    if not next_validation:
        if evidence_strength == "direct":
            next_validation = "在相同用户、输入、约束和预期结果下重跑失败旅程，并保留可复核产物"
        else:
            next_validation = "先新增直接用户旅程或真实交付验收，证明该缺口确实阻断用户结果"
    reasons = [f"{finding.get('severity') or 'P2'} 用户影响"]
    if finding.get("direct_user_evidence"):
        reasons.append("来自真实用户旅程/设备/交付证据")
    if finding.get("release_blocking"):
        reasons.append("正在阻塞发布或价值闭环")
    if isinstance(failure_rate, (int, float)):
        reasons.append(f"失败比例 {float(failure_rate):.0%}")
    if int(finding.get("affected_users") or 0) > 0:
        reasons.append(f"影响 {int(finding['affected_users'])} 个用户/样本")
    if int(finding.get("recurrence") or 0) > 1:
        reasons.append(f"已复发 {int(finding['recurrence'])} 次")
    return {"score": int(score), "evidence_strength": evidence_strength,
            "user_outcome": str(finding.get("user_outcome") or finding.get("title") or ""),
            "experience_dimension": str(finding.get("experience_dimension") or ""),
            "next_validation": next_validation, "reasons": reasons}


def _verified_source_findings(uid: str = "default") -> list[dict]:
    """读取独立核验过的源码结论；未核验、自报或引用失效的记录绝不变成骨架依据。"""
    try:
        import source_audit
        entries = source_audit.list_entries(uid, status="verified", limit=200).get("entries") or []
    except Exception:
        return []
    rows = []
    for entry in entries:
        root = Path(str(entry.get("source_root") or ""))
        file_name = str(entry.get("file") or "")
        try:
            absolute = (root / file_name).resolve()
            relative = absolute.relative_to(_REPO).as_posix()
        except (OSError, ValueError):
            continue
        checked = source_audit.verify_reference(
            str(root), file_name, int(entry.get("line") or 0), str(entry.get("quote") or ""),
            str(entry.get("revision") or ""))
        if not checked.get("ok"):
            continue
        rows.append({**entry, "tracked_file": relative})
    return rows


def _root_cause_match(finding: dict, audits: list[dict]) -> dict | None:
    """按稳定目标/分类/成员身份关联已核验源码；不从自由文本猜文件路径。"""
    target = str(finding.get("target") or "")
    category = str(finding.get("category") or "")
    source = str(finding.get("source") or "")
    finding_id = str(finding.get("id") or "")
    for entry in reversed(audits):
        if finding_id and str(entry.get("finding_id") or "") == finding_id:
            return entry
        if target and str(entry.get("target") or "") == target:
            return entry
        dimensions = {str(entry.get("dimension") or ""), str(entry.get("member") or "")}
        if (category and category in dimensions) or (source and source in dimensions):
            return entry
    return None


def _default_intelligence_sources() -> dict:
    performance_rows, pitfalls, sessions, coverage, model_health = [], [], [], {}, {}
    try:
        import harness_performance
        performance_rows = harness_performance.table().get("rows") or []
    except Exception:
        pass
    try:
        import pitfall_registry
        pitfalls = pitfall_registry.search(unresolved_only=True, limit=100)
    except Exception:
        pass
    try:
        import verification_center
        sessions = verification_center.query_sessions(limit=200).get("items") or []
    except Exception:
        pass
    try:
        import journey_runner
        coverage = journey_runner.matrix_coverage(
            journey_runner.load_matrix(_HERE / "journeys" / "definitions"))
    except Exception as exc:
        coverage = {"ok": False, "missing_roles": ["matrix_unreadable"], "error": str(exc)}
    try:
        model_health = _read_json(_paths.home_under("model_health.json"))
    except Exception:
        pass
    return {"performance_rows": performance_rows, "pitfalls": pitfalls,
            "sessions": sessions, "journey_coverage": coverage, "model_health": model_health}


def quality_intelligence(machine_items: list[dict] | None = None, *, sources: dict | None = None,
                         repo: str | Path | None = None,
                         manifest_path: str | Path | None = None,
                         evidence_path: str | Path | None = None,
                         audit_uid: str = "default") -> dict:
    """汇聚已知质量面并判断先修功能还是提议升级骨架；未知面通过新增 source 接口扩展。"""
    source = dict(_default_intelligence_sources() if sources is None else sources)
    manifest = _read_json(manifest_path or _SKELETON_MANIFEST)
    verified_audits = list(source.get("source_audits") or []) if sources is not None \
        else _verified_source_findings(audit_uid)
    findings = []
    evidence_rows = list(machine_items or [])
    current_evidence_keys = set()
    for item in evidence_rows:
        if str(item.get("status") or "") != "pass":
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        subject = details.get("subject") if isinstance(details.get("subject"), dict) else {}
        current_evidence_keys.add((str(item.get("kind") or ""), str(subject.get("target") or ""),
                                   str(subject.get("id") or "")))
    for item in evidence_rows:
        status = str(item.get("status") or "")
        if status not in {"issue", "missing", "historical"}:
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        subject = details.get("subject") if isinstance(details.get("subject"), dict) else {}
        evidence_key = (str(item.get("kind") or ""), str(subject.get("target") or ""),
                        str(subject.get("id") or ""))
        if status == "historical" and evidence_key in current_evidence_keys:
            continue
        inputs = list(((details.get("source") or {}).get("inputs") or {}).keys())
        inputs.extend(str(value) for value in details.get("files") or [])
        blockers = list(details.get("blockers") or [])
        metrics = details.get("metrics") if isinstance(details.get("metrics"), dict) else {}
        assertions = [row for row in details.get("assertions") or [] if isinstance(row, dict)]
        failed_assertions = sum(row.get("ok") is not True for row in assertions)
        failure_rate = metrics.get("failure_rate")
        if not isinstance(failure_rate, (int, float)) and assertions:
            failure_rate = failed_assertions / len(assertions)
        kind = str(item.get("kind") or "evidence")
        direct_user = kind in _DIRECT_USER_EVIDENCE or any(
            str(row.get("journey") or row.get("user_outcome") or "").strip() for row in assertions)
        user_outcome = str(details.get("user_outcome") or details.get("expected_user_outcome") or
                           subject.get("expected_user_outcome") or item.get("title") or
                           item.get("summary") or "用户交付结果")
        lineage = details.get("lineage") if isinstance(details.get("lineage"), dict) else {}
        findings.append(_finding(
            "machine_evidence", kind,
            str(item.get("title") or item.get("summary") or "机器证据异常"),
            severity="P1" if status in {"issue", "missing"} else "P2",
            recurrence=int(details.get("recurrence") or metrics.get("failures") or max(1, failed_assertions)),
            evidence=blockers or [item.get("summary") or status], evidence_status=status,
            tracked_files=inputs,
            target=str(subject.get("target") or ""), user_outcome=user_outcome,
            experience_dimension=str(details.get("experience_dimension") or kind),
            direct_user_evidence=direct_user,
            affected_users=int(metrics.get("affected_users") or metrics.get("users") or
                               metrics.get("samples") or len(assertions) or 0),
            failure_rate=failure_rate,
            release_blocking=bool(details.get("release_blocking") or direct_user),
            next_validation=(f"在同一输入与约束下重跑 {subject.get('id') or kind}，验证“{user_outcome}”并保留产物"
                             if direct_user else
                             f"为“{user_outcome}”补一条直接用户旅程或真实交付验证"),
            evidence_ids=[str(item.get("id") or "")],
            evidence_paths=[str(details.get("path") or "")],
            evidence_run_id=str(lineage.get("run_id") or "")))
    for row in reviews.load().values():
        gate = reviews.product_gate(row)
        if gate.get("release_ready"):
            continue
        decision = reviews.product_manager_decision(row, gate)
        user_outcome = str(row.get("user_value") or row.get("acceptance_criteria") or
                           row.get("feature") or row.get("target"))
        findings.append(_finding(
            "product_review", "release_blocker", str(row.get("feature") or row.get("target")),
            severity=str(row.get("priority") or "P2"), evidence=gate.get("blockers") or [],
            tracked_files=row.get("tracked_files") or [], target=str(row.get("target") or ""),
            user_outcome=user_outcome, experience_dimension="overall_delivery",
            direct_user_evidence=False, release_blocking=True,
            next_validation=("执行系统建议“" + str(decision.get("label") or "补齐发布证据") +
                             "”，随后在相同用户结果合同下复验")))
    for row in source.get("performance_rows") or []:
        n, lower = int(row.get("n") or 0), float(row.get("success_lb") or 0)
        if n < 5 or lower >= .5:
            continue
        findings.append(_finding(
            "harness_performance", "runtime_reliability",
            f"{row.get('harness')} 在 {row.get('scene')}/{row.get('mode')} 的稳定性下界仅 {lower:.0%}",
            severity="P1" if n >= 10 and lower < .35 else "P2", recurrence=n,
            evidence=[f"good={row.get('good')} bad={row.get('bad')} n={n}"]))
    for row in source.get("pitfalls") or []:
        open_count = max(0, int(row.get("seen") or 0) - int(row.get("resolved") or 0))
        if open_count < 2:
            continue
        findings.append(_finding(
            "pitfall_registry", str(row.get("category") or "pitfall"), str(row.get("finding") or "重复失败模式"),
            severity="P1" if open_count >= 3 else "P2", recurrence=open_count,
            evidence=row.get("evidence") or []))
    sessions = source.get("sessions") or []
    failed = [row for row in sessions if str(row.get("status") or "") == "failed"]
    if len(failed) >= 2 and len(failed) / max(1, len(sessions)) >= .1:
        findings.append(_finding(
            "session_history", "delivery_failure_trend",
            f"最近会话失败 {len(failed)}/{len(sessions)}，已形成可重复趋势",
            severity="P1" if len(failed) / len(sessions) >= .2 else "P2", recurrence=len(failed),
            evidence=[str(row.get("sid") or "") for row in failed[:10]]))
    coverage = source.get("journey_coverage") if isinstance(source.get("journey_coverage"), dict) else {}
    missing_roles = list(coverage.get("missing_roles") or [])
    if missing_roles:
        findings.append(_finding(
            "journey_matrix", "coverage_gap", "关键用户角色没有独立旅程证据",
            severity="P0", recurrence=len(missing_roles), evidence=missing_roles))
    model_totals = {}
    for harness, models in (source.get("model_health") or {}).items():
        for model, raw in (models or {}).items() if isinstance(models, dict) else []:
            if not isinstance(raw, dict):
                continue
            row = model_totals.setdefault(str(model), {"good": 0, "bad": 0, "harnesses": set()})
            row["good"] += int(raw.get("good") or 0); row["bad"] += int(raw.get("bad") or 0)
            row["harnesses"].add(str(harness))
    try:
        import harness_performance
        wilson = harness_performance._wilson
    except Exception:
        wilson = lambda good, bad: good / max(1, good + bad)
    for model, row in model_totals.items():
        n = row["good"] + row["bad"]
        lower = float(wilson(row["good"], row["bad"]))
        if n >= 10 and lower < .35:
            findings.append(_finding(
                "model_health", "model_reliability", f"模型 {model} 跨家稳定性资格赛失败",
                severity="P1", recurrence=n,
                evidence=[f"good={row['good']} bad={row['bad']} wilson_lb={lower:.3f}",
                          "harnesses=" + ",".join(sorted(row["harnesses"]))]))
    for finding in findings:
        matched = _root_cause_match(finding, verified_audits)
        if matched:
            tracked = list(finding.get("tracked_files") or [])
            tracked.append(str(matched.get("tracked_file") or ""))
            finding["tracked_files"] = list(dict.fromkeys(path for path in tracked if path))[:100]
            finding["root_cause_status"] = "verified"
            finding["root_cause"] = str(matched.get("claim") or matched.get("quote") or "")[:4000]
            finding["root_cause_evidence"] = [{key: matched.get(key) for key in
                                                ("audit_id", "member", "dimension", "source_root", "revision",
                                                 "file", "line", "quote", "runtime_evidence", "verifier")}]
        finding["zones"] = _zones_for(finding["tracked_files"], manifest) if finding["tracked_files"] else []
        finding["score"] = (_SEVERITY_WEIGHT[finding["severity"]]
                            + min(30, finding["recurrence"] * 3)
                            + (25 if "red" in finding["zones"] else 12 if "yellow" in finding["zones"] else 0))
        finding["user_outcome_priority"] = _user_outcome_priority(finding)
        protected = bool(set(finding["zones"]) & {"red", "yellow"})
        if finding["tracked_files"] and not protected and finding["root_cause_status"] == "unresolved":
            finding["root_cause_status"] = "not_required"
        finding["decision_state"] = (
            "skeleton_candidate" if protected and finding["root_cause_status"] == "verified" else
            "root_cause_required" if protected or not finding["tracked_files"] else "feature_fix")
        finding["suggested_action"] = (
            "准备骨架升级提案" if protected and finding["root_cause_status"] == "verified" else
            "先定位并独立核验源码根因" if protected or not finding["tracked_files"] else
            "先在功能层修复并补证据")
    deduped = {}
    for finding in findings:
        current = deduped.get(finding["id"])
        if current is None or finding["score"] > current["score"]:
            deduped[finding["id"]] = finding
    ordered = sorted(deduped.values(), key=lambda row: (-row["score"], row["title"]))
    user_ordered = sorted(ordered, key=lambda row: (
        -int((row.get("user_outcome_priority") or {}).get("score") or 0), row["title"]))
    top_user_outcome = None
    if user_ordered:
        winner = user_ordered[0]
        priority = dict(winner.get("user_outcome_priority") or {})
        top_user_outcome = {"finding_id": winner["id"], "target": winner.get("target") or "",
                            "title": winner["title"], "severity": winner["severity"],
                            "suggested_action": winner.get("suggested_action") or "",
                            **priority}
    skeleton = skeleton_upgrade_plan(repo=repo, manifest_path=manifest_path, evidence_path=evidence_path)
    protected = [row for row in ordered if set(row.get("zones") or []) & {"red", "yellow"}]
    justified = [row for row in protected if row["severity"] in {"P0", "P1"}
                 and (row["severity"] == "P0" or row["recurrence"] >= 2)
                 and row["evidence"] and row["tracked_files"]
                 and row.get("root_cause_status") == "verified"
                 and row.get("root_cause_evidence")]
    unresolved = [row for row in ordered if not row.get("tracked_files") or (
        set(row.get("zones") or []) & {"red", "yellow"}
        and row.get("root_cause_status") != "verified")]
    refs = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    drift_paths = {str((refs.get(name) or {}).get("path") or name)
                   for name in skeleton.get("protected_drift") or []}
    drift_justified = [row for row in justified
                       if drift_paths & set(row.get("tracked_files") or [])]
    if skeleton["protected_drift"]:
        focus = "prepare_skeleton_proposal" if drift_justified else "investigate_root_cause"
        worth = bool(drift_justified)
        skeleton["status"] = "advisory_drift"
        skeleton["reasons"] = list(dict.fromkeys(skeleton.get("reasons", []) + [
            "骨架漂移只作架构观察，不阻断提交、测试或发布",
        ]))
    elif justified:
        focus, worth = "prepare_skeleton_proposal", True
        skeleton.update({"status": "proposal_ready", "legal": True, "release_blocking": False,
                         "reasons": ["重复高影响问题已定位到受保护链路；建议最小可逆修复，但不设置发布门"],
                         "recommended_command": "按影响面实施最小修复并运行直接相关验证"})
    elif unresolved:
        focus, worth = "investigate_root_cause", False
        skeleton.update({"status": "root_cause_recommended", "legal": True,
                         "release_blocking": False,
                         "reasons": ["建议先核验源码根因；该建议不阻断其他已验证功能发布"],
                         "recommended_command": "按需发起 source_audit，核验运行 revision、源码引用和真实行为"})
    elif ordered:
        focus, worth = "fix_feature_first", False
    else:
        focus, worth = "observe", False
    skeleton["legal"] = True
    skeleton["release_blocking"] = False
    skeleton["requires_human_approval"] = False
    skeleton["worth_upgrading"] = worth
    skeleton["candidate_finding_ids"] = [row["id"] for row in justified]
    return {"ok": True, "generated_at": time.time(), "findings": ordered,
            "counts": {"total": len(ordered), "p0": sum(row["severity"] == "P0" for row in ordered),
                       "p1": sum(row["severity"] == "P1" for row in ordered),
                       "protected": len(protected), "skeleton_candidates": len(justified),
                       "root_cause_required": len(unresolved)},
            "recommended_focus": focus, "top_user_outcome": top_user_outcome,
            "user_outcome_ranking": [{"finding_id": row["id"], "target": row.get("target") or "",
                                      "title": row["title"], **dict(row.get("user_outcome_priority") or {})}
                                     for row in user_ordered[:20]],
            "skeleton": skeleton,
            "coverage": {"sources": ["machine_evidence", "product_review", "harness_performance",
                                       "pitfall_registry", "session_history", "journey_matrix", "model_health",
                                       "verified_source_audit"],
                         "unknown_problems_claimed": False,
                         "contract": "新增问题类型必须沉淀为新信号源或旅程，下一轮自动发现"}}


def adopt_intelligence_finding(finding: dict, *, actor: str = "verification-center") -> dict:
    """把自动发现转成正式产品待办；红黄区只登记提案，不自动改骨架。"""
    finding_id = str(finding.get("id") or "")
    if not finding_id:
        return {"ok": False, "error": "finding id is required"}
    target = str(finding.get("target") or f"feature:auto-{finding_id}")
    existing = reviews.get(target)
    if existing:
        return {"ok": True, "status": "already_tracked", "review": existing}
    protected = bool(set(finding.get("zones") or []) & {"red", "yellow"})
    rooted = bool(finding.get("tracked_files") and (
        not protected or (finding.get("root_cause_status") == "verified"
                          and finding.get("root_cause_evidence"))))
    note = "；".join(str(item) for item in finding.get("evidence") or [])[:3000]
    return reviews.save(
        target, "issue", note, actor, feature=str(finding.get("title") or "自动发现质量问题"),
        priority=str(finding.get("severity") or "P2"), upgrade_status="planned" if protected and rooted else "blocked",
        machine_status="issue", evidence="\n".join(str(item) for item in finding.get("evidence") or []),
        evidence_ids=finding.get("evidence_ids") or [], evidence_paths=finding.get("evidence_paths") or [],
        evidence_run_id=str(finding.get("evidence_run_id") or ""),
        tracked_files=finding.get("tracked_files") or [], require_machine_gate=True,
        require_lifecycle_gate=True, continuous_reverify=True,
        source_finding_id=finding_id, source_finding_kind=str(finding.get("category") or ""),
        skeleton_zones=finding.get("zones") or [], root_cause=str(finding.get("root_cause") or ""),
        root_cause_status=str(finding.get("root_cause_status") or "unresolved"),
        root_cause_evidence=finding.get("root_cause_evidence") or [],
        acceptance_criteria=("用户结果“" + str(finding.get("user_outcome") or finding.get("title") or "") +
                             "”在相同输入与约束下由机器证据重新执行并通过"),
        risk=("涉及受保护骨架；系统只生成合法升级计划，禁止自动改 manifest" if protected
              else "功能层质量问题；优先局部修复，不扩大骨架影响面"),
        next_action=("根因已独立核验；先做最小候选修复，再按骨架四步法生成机器票据"
                     if protected and rooted else
                     "先让军团定位真实运行 revision 与源码 file:line，并由独立成员核验；根因未明禁止改骨架"
                     if not rooted else "让军团做最小功能修复并自动复验"),
        _workflow_token=reviews._WORKFLOW_TOKEN)


def dashboard(machine_items: list[dict] | None = None) -> dict:
    queue = reviews.work_queue(machine_items)
    rows = reviews.load()
    recent_runs = list_runs(limit=100)
    active = [row for row in recent_runs if row.get("status") not in _TERMINAL]
    releases = {target: release_decision(target) for target in rows}
    intelligence = quality_intelligence(machine_items)
    intelligence_items = [{
        "id": row["id"], "kind": row["category"], "title": row["title"], "status": "issue",
        "summary": row["suggested_action"],
        "details": {"blockers": row["evidence"], "recurrence": row["recurrence"],
                    "source": {"inputs": {path: "" for path in row["tracked_files"]}},
                    "subject": {"target": row["target"]}, "finding": row},
    } for row in intelligence["findings"]]
    queue = reviews.work_queue([*(machine_items or []), *intelligence_items])
    return {"ok": True, "queue": queue, "active_runs": active,
            "recent_runs": recent_runs[:30], "summary": reviews.summary(rows),
            "current_commit": _commit(),
            "release_decisions": releases, "quality_intelligence": intelligence,
            "product_decisions": {target: reviews.product_manager_decision(row, row.get("product_gate"))
                                  for target, row in rows.items()},
            "release_notes": reviews.release_notes()["markdown"]}
