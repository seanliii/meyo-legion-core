#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据驱动用户旅程定义、断言记录与统一证据输出。"""
from __future__ import annotations

import json
import os
import platform
import shutil
import threading
import time
import uuid
from pathlib import Path

import evidence_schema

_SCHEMA_VERSION = 1
_REQUIRED_ROLES = {
    "new_user", "returning_user", "complex_task", "approval", "artifacts", "devices",
    "monitoring", "mobile", "recovery", "accessibility", "graph_comprehension",
}
_ALLOWED_ACTIONS = {
    "goto", "click", "fill", "press", "wait_for", "assert_visible", "assert_text",
    "assert_value", "assert_count", "assert_attribute", "assert_no_overflow", "screenshot",
}


def load_definition(path: str | Path) -> dict:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8"))
    checked = validate_definition(value)
    if not checked["ok"]:
        raise ValueError("; ".join(checked["errors"]))
    return value


def validate_definition(value: dict) -> dict:
    errors = []
    if not isinstance(value, dict):
        return {"ok": False, "errors": ["journey definition must be an object"]}
    if value.get("schema_version") != _SCHEMA_VERSION:
        errors.append("unsupported schema_version")
    if not str(value.get("id") or "").strip():
        errors.append("missing journey id")
    if value.get("role") not in _REQUIRED_ROLES:
        errors.append("invalid journey role")
    viewport = value.get("viewport") if isinstance(value.get("viewport"), dict) else {}
    if int(viewport.get("width") or 0) <= 0 or int(viewport.get("height") or 0) <= 0:
        errors.append("viewport width and height are required")
    steps = value.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("journey steps are required")
    else:
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("action") not in _ALLOWED_ACTIONS:
                errors.append(f"invalid action at step {index + 1}")
            if not str(step.get("name") or "").strip():
                errors.append(f"missing name at step {index + 1}")
    required = value.get("required_evidence") or []
    if not isinstance(required, list) or not required:
        errors.append("required_evidence is required")
    matrix = value.get("matrix") if isinstance(value.get("matrix"), list) else []
    for index, entry in enumerate(matrix):
        if not isinstance(entry, dict) or not str(entry.get("id") or "").strip():
            errors.append(f"invalid matrix entry at {index + 1}")
            continue
        if entry.get("role") not in _REQUIRED_ROLES:
            errors.append(f"invalid matrix role at {index + 1}")
        questions = entry.get("must_answer")
        if not isinstance(questions, list) or not questions:
            errors.append(f"missing must_answer at matrix entry {index + 1}")
    return {"ok": not errors, "errors": errors}


def load_matrix(directory: str | Path) -> list[dict]:
    definitions = []
    seen = set()
    for path in sorted(Path(directory).glob("*.json")):
        value = load_definition(path)
        journey_id = value["id"]
        if journey_id in seen:
            raise ValueError(f"duplicate journey id: {journey_id}")
        seen.add(journey_id)
        definitions.append(value)
    return definitions


def matrix_entries(definition: dict) -> list[dict]:
    entries = definition.get("matrix") if isinstance(definition.get("matrix"), list) else []
    return [dict(item) for item in entries if isinstance(item, dict)] or [definition]


def matrix_coverage(definitions: list[dict]) -> dict:
    entries = [entry for definition in definitions for entry in matrix_entries(definition)]
    roles = {str(item.get("role") or "") for item in entries}
    missing = sorted(_REQUIRED_ROLES - roles)
    return {"ok": not missing, "roles": sorted(roles), "missing_roles": missing,
            "journeys": len(entries)}


class JourneyRecorder:
    def __init__(self, definition: dict, output_root: str | Path, *, commit: str | None = None):
        checked = validate_definition(definition)
        if not checked["ok"]:
            raise ValueError("; ".join(checked["errors"]))
        self.definition = definition
        self.started_at = time.time()
        self.run_id = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.directory = Path(output_root) / self.run_id / definition["id"]
        self.directory.mkdir(parents=True, exist_ok=False)
        self.commit = commit if commit is not None else evidence_schema._commit(self.directory)
        self.assertions: list[dict] = []
        self.console_errors: list = []
        self.network_errors: list = []
        self.artifact_paths: list[tuple[Path, str]] = []
        self._lock = threading.RLock()
        self._write_json("definition.json", definition)

    def _write_json(self, name: str, value) -> Path:
        target = self.directory / name
        temp = target.with_suffix(f".tmp.{os.getpid()}")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)
        return target

    def assertion(self, name: str, ok: bool, detail="", *, step: str = "") -> bool:
        row = {"name": str(name), "ok": bool(ok), "detail": str(detail)[:4000],
               "step": str(step), "recorded_at": time.time()}
        with self._lock:
            self.assertions.append(row)
        return row["ok"]

    def console_error(self, message) -> None:
        with self._lock:
            self.console_errors.append(str(message)[:4000])

    def network_error(self, *, url: str, status: int | None = None, error="") -> None:
        with self._lock:
            self.network_errors.append({"url": str(url), "status": status, "error": str(error)[:1000]})

    def add_artifact(self, path: str | Path, kind: str = "artifact") -> None:
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = self.directory / "artifacts" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        self.artifact_paths.append((destination, kind))

    def attach_page(self, page) -> None:
        page.on("pageerror", lambda error: self.console_error(error))
        page.on("console", lambda message: self.console_error(message.text)
                if message.type == "error" else None)
        page.on("requestfailed", lambda request: self.network_error(
            url=request.url, error=request.failure or "request failed"))
        page.on("response", lambda response: self.network_error(url=response.url, status=response.status)
                if response.status >= 400 and not response.url.endswith("/favicon.ico") else None)

    def finish(self) -> dict:
        finished_at = time.time()
        failures = [row["name"] for row in self.assertions if not row["ok"]]
        required = set(self.definition.get("required_evidence") or [])
        self._write_json("assertions.json", self.assertions)
        self._write_json("console.json", self.console_errors)
        self._write_json("network.json", self.network_errors)
        generated = {
            "definition": self.directory / "definition.json",
            "assertions": self.directory / "assertions.json",
            "console": self.directory / "console.json",
            "network": self.directory / "network.json",
        }
        missing = sorted(name for name in required if name not in generated and not (self.directory / name).exists())
        blockers = failures + (["console errors present"] if self.console_errors else []) + \
            (["network errors present"] if self.network_errors else []) + \
            [f"missing required evidence: {name}" for name in missing]
        artifact_rows = [evidence_schema.file_evidence(path, root=self.directory, kind=kind)
                         for path, kind in self.artifact_paths]
        for name, path in generated.items():
            artifact_rows.append(evidence_schema.file_evidence(path, root=self.directory, kind=name))
        manifest = {"artifacts": artifact_rows, "required": sorted(required), "missing": missing}
        self._write_json("artifacts.json", manifest)
        envelope = evidence_schema.new_envelope(
            subject_type="journey", subject_id=self.definition["id"],
            evidence_type="frontend_journey", run_id=self.run_id, sequence=1,
            target=str(self.definition.get("target") or "feature:development-verification-center"),
            trigger="release-matrix", status="pass" if not blockers and self.assertions else "issue",
            runner="journey_runner.py", commit=self.commit,
            environment={"python": platform.python_version(), "platform": platform.platform(),
                         "viewport": self.definition.get("viewport") or {}},
            started_at=self.started_at, finished_at=finished_at, assertions=self.assertions,
            artifacts=artifact_rows, console_errors=self.console_errors,
            network_errors=self.network_errors, blockers=blockers,
            metrics={"passed": sum(row["ok"] for row in self.assertions),
                     "total": len(self.assertions),
                     "pass_rate": round(sum(row["ok"] for row in self.assertions) /
                                        len(self.assertions), 6) if self.assertions else 0},
            evidence_id=f"journey-{self.definition['id']}-{self.run_id}")
        written = evidence_schema.write_envelope(self.directory / "evidence.json", envelope)
        verdict = {
            "journey_id": self.definition["id"], "role": self.definition["role"],
            "status": written["status"], "passed": sum(row["ok"] for row in self.assertions),
            "total": len(self.assertions), "blockers": blockers,
            "evidence_id": written["evidence_id"], "evidence_digest": written["envelope_digest"],
            "started_at": self.started_at, "finished_at": finished_at,
        }
        self._write_json("verdict.json", verdict)
        return verdict


def _record_report_errors(recorder: JourneyRecorder, report: dict) -> None:
    for error in report.get("console_errors") or []:
        recorder.console_error(error)
    for request in report.get("failed_requests") or []:
        request = request if isinstance(request, dict) else {"error": request}
        recorder.network_error(url=request.get("url") or "", error=request.get("error") or "")
    for response in report.get("http_errors") or []:
        response = response if isinstance(response, dict) else {"error": response}
        recorder.network_error(url=response.get("url") or "", status=response.get("status"),
                               error=response.get("error") or "")


def record_legacy_report(definition: dict, report: dict, output_root: str | Path, *, commit: str | None = None) -> dict:
    recorder = JourneyRecorder(definition, output_root, commit=commit)
    for journey, rows in (report.get("journeys") or {}).items():
        for row in rows if isinstance(rows, list) else []:
            recorder.assertion(str(row.get("name") or "unnamed assertion"), row.get("ok") is True,
                               row.get("detail") or "", step=journey)
    _record_report_errors(recorder, report)
    return recorder.finish()


def record_matrix_report(definition: dict, report: dict, output_root: str | Path, *,
                         commit: str | None = None) -> dict:
    """把综合浏览器报告按矩阵项拆成独立证据；未映射项如实阻塞，不冒充覆盖。"""
    matrix = matrix_entries(definition)
    assignments = report.get("matrix_assignments") if isinstance(report.get("matrix_assignments"), dict) else {}
    results = []
    for entry in matrix:
        journey_id = str(entry.get("id") or "")
        role_definition = {
            "schema_version": _SCHEMA_VERSION,
            "id": journey_id,
            "title": entry.get("title") or journey_id,
            "role": entry.get("role"),
            "release_required": entry.get("release_required", True),
            "viewport": entry.get("viewport") or definition.get("viewport") or {"width": 1440, "height": 1000},
            "required_evidence": definition.get("required_evidence") or ["definition", "assertions", "console", "network"],
            "steps": entry.get("steps") or definition.get("steps") or [
                {"name": "兼容综合旅程", "action": "wait_for", "target": "body"}],
        }
        recorder = JourneyRecorder(role_definition, output_root, commit=commit)
        sources = [str(item) for item in (assignments.get(journey_id) or [])]
        rows = [row for source in sources for row in ((report.get("journeys") or {}).get(source) or [])]
        if not sources:
            recorder.assertion("矩阵项已有独立浏览器证据", False, "matrix_assignments 未映射")
        elif not rows:
            recorder.assertion("矩阵项已有独立浏览器断言", False, "映射旅程没有断言")
        else:
            for row in rows:
                recorder.assertion(str(row.get("name") or "unnamed assertion"), row.get("ok") is True,
                                   row.get("detail") or "", step=" / ".join(sources))
        _record_report_errors(recorder, report)
        results.append(recorder.finish())
    summary = aggregate_release_matrix(output_root, [definition])
    summary["verdicts"] = results
    target = Path(output_root) / "matrix-release.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(f".tmp.{os.getpid()}")
    temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, target)
    return summary


def aggregate_release_matrix(run_root: str | Path, definitions: list[dict]) -> dict:
    latest = {}
    for verdict_path in Path(run_root).glob("run-*/*/verdict.json"):
        try:
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        journey_id = str(verdict.get("journey_id") or "")
        previous = latest.get(journey_id)
        if not previous or float(verdict.get("finished_at") or 0) > float(previous.get("finished_at") or 0):
            latest[journey_id] = verdict
    entries = [entry for definition in definitions for entry in matrix_entries(definition)]
    required_ids = {item["id"] for item in entries if item.get("release_required", True)}
    missing = sorted(required_ids - set(latest))
    failed = sorted(journey_id for journey_id in required_ids & set(latest)
                    if latest[journey_id].get("status") != "pass")
    coverage = matrix_coverage(definitions)
    blockers = [f"missing journey: {item}" for item in missing] + \
        [f"journey failed: {item}" for item in failed] + \
        [f"missing journey role: {item}" for item in coverage["missing_roles"]]
    return {"ok": not blockers, "release_ready": not blockers, "blockers": blockers,
            "required": sorted(required_ids), "passed": sorted(required_ids - set(missing) - set(failed)),
            "missing": missing, "failed": failed, "coverage": coverage, "latest": latest}
