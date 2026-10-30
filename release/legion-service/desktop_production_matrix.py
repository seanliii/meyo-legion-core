#!/usr/bin/env python3
"""云电脑生产矩阵：用业务事实验收 IDE、浏览器、Office、设计与跨应用项目。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import desktop_golden_task
import desktop_quality_verifier
import desktop_workspace

SCENARIOS = {
    "ide": {
        "title": "IDE 代码项目",
        "goal": "打开真实仓库、修改代码、运行测试并交付可复现 README",
        "required": ["deliverables/app.py", "deliverables/test_app.py", "evidence/test-results.json"],
        "facts": ["source_nonempty", "test_command", "tests_passed", "readme_reproducible"],
    },
    "browser": {
        "title": "浏览器调研项目",
        "goal": "用 Chromium 访问多个独立来源、下载材料并生成可打开报告",
        "required": ["deliverables/research.html", "evidence/sources.json"],
        "facts": ["independent_sources", "browser_trace", "report_opened", "downloads_recorded"],
    },
    "office": {
        "title": "Office 交付项目",
        "goal": "生成并真实打开 Excel、PPT 与 PDF",
        "required": ["deliverables/report.xlsx", "deliverables/deck.pptx", "deliverables/report.pdf"],
        "facts": ["xlsx_opened", "pptx_opened", "pdf_opened", "content_consistent"],
    },
    "design": {
        "title": "设计图片项目",
        "goal": "生成可用视觉设计，验证尺寸、像素与真实导出",
        "required": ["deliverables/poster.svg", "deliverables/poster.png"],
        "facts": ["svg_rendered", "image_opened", "nonempty_pixels", "export_verified"],
    },
    "cross_app": {
        "title": "跨应用交付项目",
        "goal": "把浏览器材料整理进办公或设计产物，并完成下载回传",
        "required": ["deliverables/brief.pdf", "evidence/sources.json", "evidence/workflow.json"],
        "facts": ["browser_research", "content_transferred", "artifact_opened", "download_verified"],
    },
}

Runner = Callable[[dict], dict]
Screenshot = Callable[[str], dict]


def _inside(root: Path, relative: str) -> Path | None:
    try:
        target = (root / str(relative or "")).resolve()
        return target if target.is_relative_to(root.resolve()) and target.is_file() and not target.is_symlink() else None
    except (OSError, ValueError):
        return None


def _json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _scenario_facts(scenario: str, root: Path, output: dict) -> dict[str, bool]:
    declared = output.get("business_facts") if isinstance(output.get("business_facts"), dict) else {}
    facts = {key: bool(declared.get(key)) for key in SCENARIOS[scenario]["facts"]}
    if scenario == "ide":
        source = _inside(root, "deliverables/app.py")
        tests = _json(root / "evidence/test-results.json")
        facts.update({
            "source_nonempty": bool(source and len(source.read_text(encoding="utf-8", errors="ignore").strip()) >= 10),
            "test_command": bool(isinstance(tests, dict) and str(tests.get("command") or "").strip()),
            "tests_passed": bool(isinstance(tests, dict)
                                 and (tests.get("passed") is True or tests.get("status") == "passed")
                                 and int(tests.get("total") or tests.get("tests_run") or 0) > 0
                                 and int(tests.get("exit_code") or 0) == 0),
            "readme_reproducible": bool((root / "README.md").is_file()
                                        and "```" in (root / "README.md").read_text(encoding="utf-8", errors="ignore")),
        })
    elif scenario == "browser":
        sources = _json(root / "evidence/sources.json")
        rows = sources if isinstance(sources, list) else sources.get("sources", []) if isinstance(sources, dict) else []
        domains = {urlparse(str(row.get("url") or "")).netloc for row in rows if isinstance(row, dict)}
        workflow = _json(root / "evidence/workflow.json")
        facts.update({
            "independent_sources": len({domain for domain in domains if domain}) >= 2,
            "browser_trace": bool(isinstance(workflow, dict) and workflow.get("browser_trace")),
            "report_opened": bool(isinstance(workflow, dict) and workflow.get("report_opened")),
            "downloads_recorded": bool(isinstance(workflow, dict) and isinstance(workflow.get("downloads"), list)),
        })
    elif scenario == "office":
        opened = _json(root / "evidence/opened.json")
        facts.update({key: bool(isinstance(opened, dict) and opened.get(key)) for key in SCENARIOS[scenario]["facts"]})
    elif scenario == "design":
        opened = _json(root / "evidence/opened.json")
        validation = desktop_quality_verifier._validate_file(root / "deliverables/poster.png") \
            if (root / "deliverables/poster.png").is_file() else {"ok": False}
        facts.update({
            "svg_rendered": bool(isinstance(opened, dict) and opened.get("svg_rendered")),
            "image_opened": bool(isinstance(opened, dict) and opened.get("image_opened")),
            "nonempty_pixels": bool(validation.get("ok") and validation.get("width", 0) > 1 and validation.get("height", 0) > 1),
            "export_verified": bool(isinstance(opened, dict) and opened.get("export_verified")),
        })
    elif scenario == "cross_app":
        workflow = _json(root / "evidence/workflow.json")
        if isinstance(workflow, dict):
            facts.update({key: bool(workflow.get(key)) for key in SCENARIOS[scenario]["facts"]})
    return facts


def verify_scenario(uid: str, project_id: str, scenario: str, output: dict | None = None) -> dict:
    spec = SCENARIOS.get(str(scenario or ""))
    if not spec:
        return {"ok": False, "error": "unknown scenario"}
    root = desktop_workspace._project_root(uid, project_id)
    files, missing, validations = [], [], {}
    for relative in spec["required"]:
        path = _inside(root, relative)
        if not path:
            missing.append(relative)
            continue
        files.append(relative)
        validations[relative] = desktop_quality_verifier._validate_file(path)
    file_checks = {relative: bool(validations.get(relative, {}).get("ok")) for relative in spec["required"]}
    facts = _scenario_facts(scenario, root, dict(output or {}))
    screenshots = [row for row in desktop_workspace._collect_project_files(root)
                   if row["path"].startswith("evidence/")
                   and row["path"].lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    checks = {"required_files": not missing and all(file_checks.values()),
              "business_facts": bool(facts) and all(facts.values()),
              "screenshot": bool(screenshots),
              "readme": (root / "README.md").is_file() and (root / "README.md").stat().st_size > 0}
    gaps = missing + [key for key, passed in file_checks.items() if not passed] \
        + [key for key, passed in facts.items() if not passed]
    return {"ok": all(checks.values()), "scenario": scenario, "checks": checks,
            "file_checks": file_checks, "business_facts": facts, "validations": validations,
            "screenshots": [row["path"] for row in screenshots], "remaining_gaps": gaps}


def _reference_bundle(root: Path, scenario: str, verification: dict, output: dict,
                      golden_task: dict | None = None) -> dict:
    spec = SCENARIOS[scenario]
    artifacts, evidence = [], []
    for row in desktop_workspace._collect_project_files(root):
        relative = str(row["path"])
        if (not relative.startswith(("deliverables/", "evidence/"))
                or relative == "evidence/reference-bundle.json"):
            continue
        path = root / relative
        record = {"path": str(path.resolve()), "relative_path": relative, "bytes": path.stat().st_size,
                  "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        (artifacts if relative.startswith("deliverables/") else evidence).append(record)
    bundle = {
        "schema_version": 1,
        "scenario": scenario,
        "golden_task": dict(golden_task or desktop_golden_task.canonical(
            input_value=spec["goal"],
            constraints=["real persistent cloud desktop", "required artifacts and business facts"],
            expected_user_outcome=spec["title"],
        )),
        "artifact_paths": [row["path"] for row in artifacts],
        "evidence_paths": [row["path"] for row in evidence],
        "artifacts": artifacts,
        "evidence": evidence,
        "business_facts": dict(verification.get("business_facts") or {}),
        "checks": dict(verification.get("checks") or {}),
        "runner_output": {key: value for key, value in output.items() if key not in {"stdout", "stderr"}},
        "generated_at": time.time(),
    }
    target = root / "evidence" / "reference-bundle.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"path": str(target.resolve()), "bundle": bundle}


def run_scenario(uid: str, scenario: str, runner: Runner | None = None,
                 screenshot: Screenshot | None = None, golden_task: dict | None = None,
                 parent_quality_task: dict | None = None) -> dict:
    spec = SCENARIOS.get(str(scenario or ""))
    if not spec:
        return {"ok": False, "error": "unknown scenario"}
    project = desktop_workspace.create_project(uid, spec["title"], spec["goal"])
    if not project.get("ok"):
        return project
    project_id = project["project"]["project_id"]
    task = desktop_workspace.create_task(
        uid, project_id, spec["goal"], execution_spec={"kind": "matrix_scenario",
                                                       "scenario": scenario,
                                                       "required_artifacts": spec["required"],
                                                       "golden_task": dict(golden_task or {}),
                                                       "parent_quality_task": dict(parent_quality_task or {})})["task"]
    if runner is None:
        return {"ok": True, "state": "queued", "scenario": scenario,
                "project_id": project_id, "task_id": task["task_id"],
                "required": list(spec["required"]), "goal": spec["goal"]}
    desktop_workspace.mark_running(uid, project_id, task["task_id"], {"scenario": scenario})
    output = runner({"uid": uid, "project_id": project_id, "task_id": task["task_id"],
                     "scenario": scenario, "goal": spec["goal"], "required": list(spec["required"])}) or {}
    if screenshot is not None:
        shot = screenshot(uid)
        if shot.get("ok") and shot.get("path"):
            source = _inside(Path(desktop_workspace._project_root(uid, project_id)).parents[1], str(shot["path"]))
            target = desktop_workspace._project_root(uid, project_id) / "evidence" / "final.png"
            if source:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
    verification = verify_scenario(uid, project_id, scenario, output)
    reference = _reference_bundle(desktop_workspace._project_root(uid, project_id), scenario,
                                  verification, output, golden_task=golden_task) if verification["ok"] else None
    finalized = desktop_workspace.finalize(uid, project_id, task["task_id"],
                                           "completed" if verification["ok"] else "partial",
                                           "" if verification["ok"] else "；".join(verification["remaining_gaps"]))
    return {"ok": verification["ok"] and finalized.get("ok"), "scenario": scenario,
            "project_id": project_id, "task_id": task["task_id"],
            "output": output, "verification": verification, "reference_bundle": reference,
            "delivery": finalized}


def run_matrix(uid: str = "default", runner: Runner | None = None,
               screenshot: Screenshot | None = None) -> dict:
    rows = [run_scenario(uid, name, runner=runner, screenshot=screenshot) for name in SCENARIOS]
    completed = runner is not None and all(row.get("ok") for row in rows)
    return {"ok": completed, "rows": rows,
            "passed": sum(bool(row.get("ok")) for row in rows) if runner is not None else 0,
            "total": len(rows), "generated_at": time.time(),
            "execution": "real" if runner else "queued"}
