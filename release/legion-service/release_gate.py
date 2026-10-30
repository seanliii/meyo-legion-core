#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人工评审、机器证据、版本谱系与安全阻塞的统一发布门。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import evidence_schema

_DEFAULT_REQUIRED = ("frontend-user-journeys",)


def _review_gate(review: dict | None) -> dict:
    if not review:
        return {"ok": False, "blockers": ["验收中心没有这项功能的产品记录"]}
    import verification_reviews
    gate = verification_reviews.product_gate(review)
    return {"ok": bool(gate.get("release_ready")), "blockers": list(gate.get("blockers") or []),
            "quality_score": review.get("quality_score"),
            "current_version": str(review.get("current_version") or ""),
            "target_version": str(review.get("target_version") or "")}


def _latest_records(paths: list[str | Path] | None) -> tuple[dict[str, dict], list[str]]:
    latest, invalid = {}, []
    for path in paths or []:
        result = evidence_schema.read_envelope(path)
        if not result.get("ok"):
            invalid.append(f"机器证据无效：{Path(path).name} · {'；'.join(result.get('errors') or [])}")
            continue
        envelope = result["evidence"]
        subject = envelope.get("subject") if isinstance(envelope.get("subject"), dict) else {}
        subject_id = str(subject.get("id") or "")
        finished = float(envelope.get("finished_at") or envelope.get("started_at") or 0)
        previous = latest.get(subject_id)
        if previous is None or finished > float(previous.get("finished_at") or previous.get("started_at") or 0):
            latest[subject_id] = envelope
    return latest, invalid


def _evidence_gate(paths: list[str | Path] | None, *, required_subjects: tuple[str, ...],
                   commit: str = "", required_run_id: str = "",
                   max_age_seconds: float = evidence_schema.DEFAULT_MAX_AGE_SECONDS,
                   now: float | None = None, input_root: str | Path | None = None) -> dict:
    now = float(now or time.time())
    latest, blockers = _latest_records(paths)
    satisfied, stale, records, freshness_rows = [], [], [], {}
    required = tuple(dict.fromkeys(str(item) for item in required_subjects if str(item)))
    missing = sorted(set(required) - set(latest))
    blockers.extend(f"缺少必需机器证据：{subject}" for subject in missing)
    selected_runs = set()
    for subject_id in required:
        envelope = latest.get(subject_id)
        if not envelope:
            continue
        records.append(envelope)
        fresh = evidence_schema.freshness(
            envelope, current_commit=commit, required_run_id=required_run_id,
            max_age_seconds=max_age_seconds, now=now, input_root=input_root)
        freshness_rows[subject_id] = fresh
        run_id = str((envelope.get("lineage") or {}).get("run_id") or "")
        if run_id:
            selected_runs.add(run_id)
        if envelope.get("status") != "pass":
            blockers.append(f"机器证据未通过：{subject_id}")
        elif "commit mismatch" in fresh["reasons"]:
            blockers.append(f"机器证据版本不一致：{subject_id}")
        elif "run mismatch" in fresh["reasons"]:
            blockers.append(f"机器证据不属于本次验收：{subject_id}")
        elif "evidence expired" in fresh["reasons"]:
            stale.append(subject_id)
            blockers.append(f"机器证据已过期：{subject_id}")
        elif "inputs changed" in fresh["reasons"]:
            blockers.append(f"机器证据输入已变化：{subject_id}")
        elif envelope.get("blockers"):
            blockers.append(f"机器证据仍有阻塞：{subject_id}")
        elif not fresh["current"]:
            blockers.append(f"机器证据不再代表当前状态：{subject_id}")
        else:
            satisfied.append(subject_id)
    if not required_run_id and len(required) > 1 and len(selected_runs) > 1:
        blockers.append("必需机器证据不属于同一次验收运行")
    digest_payload = [{"id": row.get("evidence_id"), "digest": row.get("envelope_digest")}
                      for row in sorted(records, key=lambda item: str(item.get("evidence_id") or ""))]
    return {
        "ok": not blockers,
        "blockers": blockers,
        "required_evidence": list(required),
        "satisfied_evidence": sorted(set(satisfied)),
        "stale_evidence": sorted(set(stale)),
        "run_ids": sorted(selected_runs),
        "freshness": freshness_rows,
        "evidence_digest": hashlib.sha256(json.dumps(digest_payload, sort_keys=True).encode()).hexdigest(),
    }


def _journey_matrix_gate(path: str | Path | None) -> dict:
    if not path:
        return {"ok": True, "blockers": [], "required": [], "passed": []}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "blockers": [f"旅程矩阵不可读：{exc}"], "required": [], "passed": []}
    blockers = [str(item) for item in (value.get("blockers") or [])]
    if value.get("release_ready") is not True and not blockers:
        blockers.append("旅程矩阵尚未通过")
    return {"ok": not blockers, "blockers": blockers,
            "required": list(value.get("required") or []), "passed": list(value.get("passed") or []),
            "missing": list(value.get("missing") or []), "failed": list(value.get("failed") or [])}


def unified_release_gate(*, target: str, review: dict | None,
                         evidence_paths: list[str | Path] | None = None,
                         required_subjects: tuple[str, ...] = _DEFAULT_REQUIRED,
                         security_blockers: list[str] | None = None,
                         journey_matrix_path: str | Path | None = None,
                         commit: str = "", required_run_id: str = "",
                         max_age_seconds: float = evidence_schema.DEFAULT_MAX_AGE_SECONDS,
                         now: float | None = None, input_root: str | Path | None = None) -> dict:
    review_result = _review_gate(review)
    evidence_result = _evidence_gate(
        evidence_paths, required_subjects=required_subjects, commit=commit,
        required_run_id=required_run_id, max_age_seconds=max_age_seconds,
        now=now, input_root=input_root)
    security = [str(item) for item in (security_blockers or []) if str(item).strip()]
    matrix_result = _journey_matrix_gate(journey_matrix_path)
    blockers = list(review_result["blockers"]) + list(evidence_result["blockers"])
    blockers.extend(matrix_result["blockers"])
    blockers.extend(f"安全阻塞：{item}" for item in security)
    level = "ready" if not blockers else "candidate" if review_result["ok"] else "blocked"
    return {
        "ok": not blockers,
        "release_ready": not blockers,
        "level": level,
        "target": str(target),
        "commit": str(commit),
        "required_run_id": str(required_run_id),
        "blockers": blockers,
        "warnings": [],
        "review_gate": review_result,
        "evidence_gate": evidence_result,
        "journey_matrix_gate": matrix_result,
        "security_gate": {"ok": not security, "blockers": security},
        "required_evidence": evidence_result["required_evidence"],
        "satisfied_evidence": evidence_result["satisfied_evidence"],
        "stale_evidence": evidence_result["stale_evidence"],
        "run_ids": evidence_result["run_ids"],
        "evidence_digest": evidence_result["evidence_digest"],
    }
