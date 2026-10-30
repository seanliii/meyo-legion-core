#!/usr/bin/env python3
"""同题参考证据包：发现、复核并绑定真实生产矩阵产物。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import desktop_workspace


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def validate(path: str | Path, golden_task: dict | None = None) -> dict:
    target = Path(path).resolve()
    bundle = _load(target)
    if not bundle or target.name != "reference-bundle.json":
        return {"ok": False, "error": "reference bundle not found or invalid"}
    required = ("input", "constraints", "expected_user_outcome")
    golden = bundle.get("golden_task") if isinstance(bundle.get("golden_task"), dict) else {}
    checks = {
        "golden_task": all(str(golden.get(key) or "").strip() for key in required),
        "same_task": not golden_task or all(str(golden.get(key) or "").strip()
                                            == str(golden_task.get(key) or "").strip() for key in required),
        "business_facts": bool(bundle.get("business_facts")) and all((bundle.get("business_facts") or {}).values()),
        "verification": bool(bundle.get("checks")) and all((bundle.get("checks") or {}).values()),
    }
    records = [*(bundle.get("artifacts") or []), *(bundle.get("evidence") or [])]
    integrity = bool(records)
    for record in records:
        file_path = Path(str(record.get("path") or ""))
        if not file_path.is_absolute() or not file_path.is_file() or file_path.is_symlink():
            integrity = False
            break
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if digest != str(record.get("sha256") or "") or file_path.stat().st_size != int(record.get("bytes") or -1):
            integrity = False
            break
    checks["integrity"] = integrity
    return {"ok": all(checks.values()), "path": str(target), "checks": checks, "bundle": bundle}


def discover(uid: str, scenario: str = "", golden_task: dict | None = None, limit: int = 100) -> dict:
    rows = []
    projects = desktop_workspace.list_projects(uid, limit=max(1, min(int(limit or 100), 200)))
    for project in projects.get("projects") or []:
        path = desktop_workspace._project_root(uid, str(project.get("project_id") or "")) / "evidence" / "reference-bundle.json"
        if not path.is_file():
            continue
        checked = validate(path, golden_task)
        if scenario and str((checked.get("bundle") or {}).get("scenario") or "") != str(scenario):
            continue
        if checked.get("ok"):
            rows.append(checked)
    return {"ok": True, "uid": uid, "references": rows, "total": len(rows)}


def as_quality_reference(checked: dict, name: str = "Meyo production matrix") -> dict:
    bundle = checked.get("bundle") if isinstance(checked.get("bundle"), dict) else {}
    path = str(checked.get("path") or "")
    golden = dict(bundle.get("golden_task") or {})
    checks = checked.get("checks") if isinstance(checked.get("checks"), dict) else {}
    return {
        "name": name,
        "work": str(bundle.get("scenario") or "production matrix reference"),
        "url": Path(path).as_uri(),
        "verified": checked.get("ok") is True,
        "verification": {"status": "pass" if checked.get("ok") else "issue",
                         "checks": checks, "path": path,
                         # 摘要始终现场重算；不信 bundle 内自带的 digest 字段。
                         "digest": (hashlib.sha256(Path(path).read_bytes()).hexdigest()
                                    if Path(path).is_file() else "")},
        "comparison": {"same_golden_task": bool(checks.get("golden_task") and checks.get("same_task")),
                       **golden,
                       "artifact_paths": list(bundle.get("artifact_paths") or []),
                       "evidence_paths": list(bundle.get("evidence_paths") or [])},
    }
