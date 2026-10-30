#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一机器证据结构、谱系、新鲜度、版本对比与原子落盘。"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

_SCHEMA_VERSION = 2
_SUPPORTED_SCHEMA_VERSIONS = {1, 2}
_STATUSES = {"pass", "issue", "pending", "historical"}
_SUBJECT_TYPES = {"journey", "feature", "task", "release", "claim", "quality", "performance"}
_EVIDENCE_TYPES = {
    "generic", "fast_contract", "quality_l0", "quality_l1", "quality_l2", "quality_l3",
    "quality_l4", "frontend_journey", "skeleton", "harness_health", "performance",
    "desktop_delivery", "mobile_business", "repair", "release_observation", "frontier_reference",
}
DEFAULT_MAX_AGE_SECONDS = 7 * 86400
_LOCK = threading.RLock()


def _git(args: list[str], cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _commit(cwd: Path | None = None) -> str:
    return _git(["rev-parse", "HEAD"], cwd)


def new_run_id(prefix: str = "verify") -> str:
    clean = re.sub(r"[^a-z0-9-]+", "-", str(prefix or "verify").lower()).strip("-") or "verify"
    return f"{clean}-{int(time.time())}-{uuid.uuid4().hex[:10]}"


def source_snapshot(cwd: str | Path | None = None, *, inputs: dict | None = None,
                    environment: dict | None = None) -> dict:
    root = Path(cwd).resolve() if cwd else None
    env = {
        "python": platform.python_version(),
        "platform": platform.system().lower(),
        "machine": platform.machine().lower(),
    }
    if isinstance(environment, dict):
        env.update(environment)
    return {
        "commit": _commit(root),
        "branch": _git(["rev-parse", "--abbrev-ref", "HEAD"], root),
        "dirty": bool(_git(["status", "--porcelain"], root)),
        "environment": env,
        "inputs": dict(inputs or {}),
    }


def file_evidence(path: str | Path, *, root: str | Path | None = None, kind: str = "") -> dict:
    source = Path(path).resolve()
    base = Path(root).resolve() if root else source.parent
    try:
        stored_path = str(source.relative_to(base))
    except ValueError:
        stored_path = str(source)
    data = source.read_bytes()
    return {
        "path": stored_path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "mime": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
        "kind": str(kind or "artifact"),
    }


def new_envelope(*, subject_type: str, subject_id: str, status: str = "pending",
                 runner: str = "", commit: str = "", version: str = "",
                 environment: dict | None = None, inputs: dict | None = None,
                 started_at: float | None = None, finished_at: float | None = None,
                 assertions: list | None = None, artifacts: list | None = None,
                 console_errors: list | None = None, network_errors: list | None = None,
                 claims: list | None = None, blockers: list | None = None,
                 evidence_id: str = "", evidence_type: str = "generic",
                 run_id: str = "", parent_evidence_id: str = "", sequence: int = 0,
                 attempt: int = 1, trigger: str = "", target: str = "",
                 metrics: dict | None = None, baseline: dict | None = None,
                 links: list | None = None) -> dict:
    envelope = {
        "schema_version": _SCHEMA_VERSION,
        "evidence_id": evidence_id or f"ev-{uuid.uuid4().hex}",
        "evidence_type": str(evidence_type or "generic"),
        "subject": {"type": str(subject_type), "id": str(subject_id), "target": str(target or "")},
        "lineage": {
            "run_id": str(run_id or new_run_id()),
            "parent_evidence_id": str(parent_evidence_id or ""),
            "sequence": max(0, int(sequence or 0)),
            "attempt": max(1, int(attempt or 1)),
            "trigger": str(trigger or "manual"),
        },
        "source": {
            "runner": str(runner), "commit": str(commit), "version": str(version),
            "environment": environment if isinstance(environment, dict) else {},
            "inputs": inputs if isinstance(inputs, dict) else {},
        },
        "started_at": float(started_at or time.time()),
        "finished_at": float(finished_at or 0),
        "status": str(status),
        "assertions": list(assertions or []),
        "artifacts": list(artifacts or []),
        "console_errors": list(console_errors or []),
        "network_errors": list(network_errors or []),
        "claims": list(claims or []),
        "blockers": [str(item) for item in (blockers or []) if str(item).strip()],
        "metrics": dict(metrics or {}),
        "baseline": dict(baseline or {}),
        "links": [dict(item) for item in (links or []) if isinstance(item, dict)],
    }
    checked = validate(envelope)
    if not checked["ok"]:
        raise ValueError("; ".join(checked["errors"]))
    return envelope


def validate(envelope: dict) -> dict:
    errors = []
    if not isinstance(envelope, dict):
        return {"ok": False, "errors": ["evidence must be an object"]}
    schema_version = envelope.get("schema_version")
    if schema_version not in _SUPPORTED_SCHEMA_VERSIONS:
        errors.append("unsupported schema_version")
    if not str(envelope.get("evidence_id") or "").strip():
        errors.append("missing evidence_id")
    subject = envelope.get("subject") if isinstance(envelope.get("subject"), dict) else {}
    if subject.get("type") not in _SUBJECT_TYPES:
        errors.append("invalid subject type")
    if not str(subject.get("id") or "").strip():
        errors.append("missing subject id")
    if envelope.get("status") not in _STATUSES:
        errors.append("invalid status")
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    if not str(source.get("runner") or "").strip():
        errors.append("missing source runner")
    if schema_version == 2:
        evidence_type = str(envelope.get("evidence_type") or "")
        if evidence_type not in _EVIDENCE_TYPES:
            errors.append("invalid evidence_type")
        lineage = envelope.get("lineage") if isinstance(envelope.get("lineage"), dict) else {}
        if not str(lineage.get("run_id") or "").strip():
            errors.append("missing run_id")
        if not isinstance(lineage.get("sequence"), int) or int(lineage.get("sequence") or 0) < 0:
            errors.append("invalid evidence sequence")
        if not isinstance(lineage.get("attempt"), int) or int(lineage.get("attempt") or 0) < 1:
            errors.append("invalid evidence attempt")
    for mapping_name in ("environment", "inputs"):
        if source.get(mapping_name) is not None and not isinstance(source.get(mapping_name), dict):
            errors.append(f"source {mapping_name} must be an object")
    for name, digest in (source.get("inputs") or {}).items():
        value = str(digest or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
            errors.append(f"input digest is invalid: {name}")
    for artifact in envelope.get("artifacts") or []:
        if not isinstance(artifact, dict) or not str(artifact.get("path") or "").strip():
            errors.append("artifact path is required")
            continue
        digest = str(artifact.get("sha256") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
            errors.append(f"artifact digest is invalid: {artifact.get('path')}")
    return {"ok": not errors, "errors": errors}


def verify_artifacts(envelope: dict, root: str | Path) -> dict:
    root_path = Path(root).resolve()
    missing, mismatched = [], []
    for artifact in envelope.get("artifacts") or []:
        path = Path(str(artifact.get("path") or ""))
        resolved = path if path.is_absolute() else root_path / path
        if not resolved.is_file():
            missing.append(str(path))
            continue
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual != str(artifact.get("sha256") or ""):
            mismatched.append(str(path))
    return {"ok": not missing and not mismatched, "missing": missing, "mismatched": mismatched}


def evidence_digest(envelope: dict) -> str:
    payload = dict(envelope)
    payload.pop("envelope_digest", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def freshness(envelope: dict, *, current_commit: str = "", required_run_id: str = "",
              max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS, now: float | None = None,
              input_root: str | Path | None = None) -> dict:
    """判断证据是否仍能代表当前代码与输入；旧证据保留为 historical，不伪装当前通过。"""
    now = float(now or time.time())
    reasons = []
    status = str(envelope.get("status") or "pending")
    if status != "pass":
        reasons.append(f"evidence status is {status}")
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    source_commit = str(source.get("commit") or "")
    if current_commit and source_commit != current_commit:
        reasons.append("commit mismatch")
    lineage = envelope.get("lineage") if isinstance(envelope.get("lineage"), dict) else {}
    run_id = str(lineage.get("run_id") or "")
    if required_run_id and run_id != required_run_id:
        reasons.append("run mismatch")
    finished = float(envelope.get("finished_at") or 0)
    age = None if not finished else max(0.0, now - finished)
    if max_age_seconds > 0 and (age is None or age > max_age_seconds):
        reasons.append("evidence expired")
    changed_inputs = []
    if input_root is not None:
        root = Path(input_root).resolve()
        for raw_path, expected in (source.get("inputs") or {}).items():
            path = Path(str(raw_path))
            resolved = path if path.is_absolute() else root / path
            try:
                actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
            except OSError:
                actual = "missing"
            if actual != str(expected):
                changed_inputs.append(str(raw_path))
        if changed_inputs:
            reasons.append("inputs changed")
    current = not reasons
    state = "pass" if current else "issue" if status == "issue" else "pending" if status == "pending" else "historical"
    return {
        "current": current, "state": state, "reasons": reasons, "age_seconds": age,
        "run_id": run_id, "commit": source_commit, "changed_inputs": changed_inputs,
    }


def compare(current: dict, baseline: dict | None) -> dict:
    """比较两份同类证据，输出可解释的指标、断言和阻塞变化。"""
    baseline = baseline if isinstance(baseline, dict) else {}
    current_metrics = current.get("metrics") if isinstance(current.get("metrics"), dict) else {}
    baseline_metrics = baseline.get("metrics") if isinstance(baseline.get("metrics"), dict) else {}
    metric_delta = {}
    for name in sorted(set(current_metrics) | set(baseline_metrics)):
        before, after = baseline_metrics.get(name), current_metrics.get(name)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            metric_delta[name] = {"before": before, "after": after, "delta": round(after - before, 6)}
    def assertion_map(row: dict) -> dict:
        return {str(item.get("id") or item.get("name") or item.get("journey") or index): bool(item.get("ok"))
                for index, item in enumerate(row.get("assertions") or []) if isinstance(item, dict)}
    old_assertions, new_assertions = assertion_map(baseline), assertion_map(current)
    regressions = sorted(name for name, ok in new_assertions.items() if not ok and old_assertions.get(name) is True)
    improvements = sorted(name for name, ok in new_assertions.items() if ok and old_assertions.get(name) is False)
    old_blockers = {str(item) for item in baseline.get("blockers") or []}
    new_blockers = {str(item) for item in current.get("blockers") or []}
    return {
        "baseline_evidence_id": str(baseline.get("evidence_id") or ""),
        "current_evidence_id": str(current.get("evidence_id") or ""),
        "metric_delta": metric_delta,
        "assertion_regressions": regressions,
        "assertion_improvements": improvements,
        "introduced_blockers": sorted(new_blockers - old_blockers),
        "resolved_blockers": sorted(old_blockers - new_blockers),
        "status_changed": str(baseline.get("status") or "") != str(current.get("status") or ""),
    }


def write_envelope(path: str | Path, envelope: dict) -> dict:
    checked = validate(envelope)
    if not checked["ok"]:
        raise ValueError("; ".join(checked["errors"]))
    payload = dict(envelope)
    payload["envelope_digest"] = evidence_digest(payload)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        temp = target.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)
    return payload


def read_envelope(path: str | Path, *, verify_files: bool = True) -> dict:
    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [str(exc)], "path": str(target)}
    checked = validate(payload)
    expected = str(payload.get("envelope_digest") or "")
    actual = evidence_digest(payload)
    if expected and expected != actual:
        checked["errors"].append("envelope digest mismatch")
        checked["ok"] = False
    artifact_check = verify_artifacts(payload, target.parent) if verify_files else {"ok": True}
    if not artifact_check["ok"]:
        checked["errors"].append("artifact evidence mismatch")
        checked["ok"] = False
    return {**checked, "evidence": payload, "artifact_check": artifact_check, "path": str(target)}


def default_store_root() -> Path:
    import legion_paths
    return Path(legion_paths.home_under("verification-center", "evidence"))


def store_envelope(envelope: dict, root: str | Path | None = None) -> dict:
    lineage = envelope.get("lineage") if isinstance(envelope.get("lineage"), dict) else {}
    run_id = re.sub(r"[^A-Za-z0-9_.-]", "-", str(lineage.get("run_id") or "legacy"))[:120]
    sequence = max(0, int(lineage.get("sequence") or 0))
    evidence_type = re.sub(r"[^A-Za-z0-9_.-]", "-", str(envelope.get("evidence_type") or "generic"))[:80]
    subject = envelope.get("subject") if isinstance(envelope.get("subject"), dict) else {}
    subject_id = re.sub(r"[^A-Za-z0-9_.-]", "-", str(subject.get("id") or "evidence"))[:100]
    path = Path(root or default_store_root()) / run_id / f"{sequence:03d}-{evidence_type}-{subject_id}.json"
    written = write_envelope(path, envelope)
    return {"path": str(path), "evidence": written}


def list_envelopes(root: str | Path | None = None, *, target: str = "", limit: int = 500) -> list[dict]:
    base = Path(root or default_store_root())
    if not base.is_dir():
        return []
    rows = []
    for path in sorted(base.glob("*/*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:max(1, limit)]:
        checked = read_envelope(path)
        envelope = checked.get("evidence") or {}
        subject = envelope.get("subject") if isinstance(envelope.get("subject"), dict) else {}
        if target and str(subject.get("target") or "") != target:
            continue
        rows.append({**checked, "path": str(path)})
    return rows


def latest_by_type(envelopes: list[dict]) -> dict[str, dict]:
    latest = {}
    for envelope in envelopes:
        evidence_type = str(envelope.get("evidence_type") or "generic")
        timestamp = float(envelope.get("finished_at") or envelope.get("started_at") or 0)
        current = latest.get(evidence_type)
        if current is None or timestamp > float(current.get("finished_at") or current.get("started_at") or 0):
            latest[evidence_type] = envelope
    return latest


def from_frontend_report(report_path: str | Path, *, commit: str = "", run_id: str = "",
                         target: str = "feature:development-verification-center",
                         parent_evidence_id: str = "") -> dict:
    path = Path(report_path)
    report = json.loads(path.read_text(encoding="utf-8"))
    failures = [str(item) for item in (report.get("failures") or [])]
    assertions = []
    for journey, rows in (report.get("journeys") or {}).items():
        for row in rows if isinstance(rows, list) else []:
            assertions.append({"journey": journey, "name": str(row.get("name") or ""),
                               "ok": row.get("ok") is True, "detail": str(row.get("detail") or "")})
    artifacts = [file_evidence(path, root=path.parent, kind="journey-report")]
    for screenshot in sorted(path.parent.glob("*.png")):
        artifacts.append(file_evidence(screenshot, root=path.parent, kind="screenshot"))
    console_errors = list(report.get("console_errors") or [])
    network_errors = list(report.get("failed_requests") or []) + list(report.get("http_errors") or [])
    passed, total = int(report.get("passed") or 0), int(report.get("total") or 0)
    blockers = failures + (["console errors present"] if console_errors else []) + \
        (["network errors present"] if network_errors else [])
    return new_envelope(
        subject_type="journey", subject_id="frontend-user-journeys",
        evidence_type="frontend_journey", run_id=run_id or new_run_id("frontend"),
        parent_evidence_id=parent_evidence_id, target=target,
        status="pass" if total > 0 and passed == total and not blockers else "issue",
        runner="test_frontend_user_journeys.py", commit=commit or _commit(path.parent),
        environment={"browser": "chromium", "python": sys.version.split()[0]},
        started_at=report.get("started_at"), finished_at=report.get("finished_at"),
        assertions=assertions, artifacts=artifacts, console_errors=console_errors,
        network_errors=network_errors, blockers=blockers,
        metrics={"passed": passed, "total": total, "pass_rate": round(passed / total, 6) if total else 0},
        evidence_id=f"frontend-journeys-{int(path.stat().st_mtime)}")
