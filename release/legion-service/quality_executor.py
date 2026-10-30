#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可执行 L0-L4 质量矩阵：快层产合同证据，发布层产真实分级证据。"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import evidence_schema

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

LEVEL_SPECS = {
    "L0": {
        "title": "基础交付与首轮健康",
        "description": "短答、自交付、清洁终态和基础运行资格",
        "pytest": ["tests/test_harness_selfcheck.py", "tests/test_runtime_manifest.py",
                   "tests/test_harness_failure_matrix.py"],
        "scripts": [], "kind": "quality_l0", "slow": False,
    },
    "L1": {
        "title": "工具、错误回流与文件落盘",
        "description": "共享工具权限、运行上下文、事件闭环和当前任务产物",
        "pytest": ["tests/test_legion_permissions.py", "tests/test_runtime_context_delivery.py",
                   "tests/test_events_phase_mailbox.py"],
        "scripts": [], "kind": "quality_l1", "slow": False,
    },
    "L2": {
        "title": "源码、结构与引用语义",
        "description": "源码身份、编排合同、派单池和骨架关键行为",
        "pytest": ["tests/test_harness_deep_review_contracts.py",
                   "tests/test_dispatch_pool_guards.py"],
        "scripts": [], "kind": "quality_l2", "slow": False,
    },
    "L3": {
        "title": "复杂交付与真实打开",
        "description": "产品验收、用户旅程、浏览器真实渲染与下载",
        "pytest": ["tests/test_verification_center.py"],
        "scripts": ["tests/test_verification_center_browser.py",
                    "tests/test_frontend_user_journeys.py"],
        "kind": "quality_l3", "slow": True,
    },
    "L4": {
        "title": "恢复、取消与用户隔离",
        "description": "跨轮恢复、取消、持久任务、人类确认和多用户隔离",
        "pytest": ["tests/test_cancellation.py", "tests/test_native_resume_and_semantic.py",
                   "tests/test_mobile_tasks.py", "tests/test_desktop_workspace.py",
                   "tests/test_isolation_env_guards.py"],
        "scripts": [], "kind": "quality_l4", "slow": True,
    },
}


def normalize_levels(levels=None) -> list[str]:
    values = [str(item).upper() for item in (levels or LEVEL_SPECS)]
    invalid = sorted(set(values) - set(LEVEL_SPECS))
    if invalid:
        raise ValueError("unknown quality levels: " + ",".join(invalid))
    return [level for level in LEVEL_SPECS if level in values]


def plan(levels=None, *, live_harnesses: list[str] | None = None,
         phase: str = "release") -> dict:
    selected = levels_for_phase(levels, phase)
    rows = []
    for level in selected:
        spec = LEVEL_SPECS[level]
        commands = []
        if spec["pytest"]:
            commands.append([sys.executable, "-m", "pytest", *spec["pytest"], "-q"])
        commands.extend([sys.executable, name] for name in spec["scripts"])
        rows.append({"level": level, "title": spec["title"], "description": spec["description"],
                     "slow": spec["slow"], "commands": commands,
                     "live_harnesses": list(live_harnesses or []) if level == "L0" else []})
    return {"phase": phase, "levels": rows,
            "fast": [row["level"] for row in rows if not row["slow"]],
            "slow": [row["level"] for row in rows if row["slow"]]}


def levels_for_phase(levels=None, phase: str = "release") -> list[str]:
    selected = normalize_levels(levels)
    phase = str(phase or "release").lower()
    if phase not in {"fast", "release"}:
        raise ValueError("quality phase must be fast or release")
    return [level for level in selected if phase == "release" or not LEVEL_SPECS[level]["slow"]]


def _level_input_files(level: str, files: list[str] | None) -> list[str]:
    """复用只依赖明确登记的功能文件、测试入口和执行器本身；键统一为仓库相对路径。"""
    spec = LEVEL_SPECS[level]
    service_prefix = "release/legion-service/"
    owned = [service_prefix + name for name in [*spec["pytest"], *spec["scripts"],
                                                 "quality_executor.py", "evidence_schema.py"]]
    return list(dict.fromkeys([*(files or []), *owned]))


def _hash_inputs(files: list[str] | None) -> dict[str, str]:
    values = {}
    for name in sorted(set(str(item) for item in (files or []) if str(item))):
        path = Path(name)
        if not path.is_absolute():
            repo_path, service_path = REPO / path, HERE / path
            path = repo_path if repo_path.exists() else service_path
        try:
            values[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return values


def _run_command(command: list[str], env: dict, timeout: float) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(command, cwd=HERE, env=env, capture_output=True, text=True,
                                timeout=timeout)
        return {"command": command, "ok": result.returncode == 0, "returncode": result.returncode,
                "elapsed_s": round(time.monotonic() - started, 3),
                "stdout": result.stdout[-12000:], "stderr": result.stderr[-6000:]}
    except subprocess.TimeoutExpired as exc:
        return {"command": command, "ok": False, "returncode": 124,
                "elapsed_s": round(time.monotonic() - started, 3),
                "stdout": str(exc.stdout or "")[-12000:], "stderr": "quality command timed out"}
    except OSError as exc:
        return {"command": command, "ok": False, "returncode": 127,
                "elapsed_s": round(time.monotonic() - started, 3),
                "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}


def _latest_record(target: str, kind: str, root: str | Path | None) -> dict:
    rows = [item for item in evidence_schema.list_envelopes(root, target=target)
            if item.get("ok") and str((item.get("evidence") or {}).get("evidence_type") or "") == kind]
    return max(rows, key=lambda item: float((item.get("evidence") or {}).get("finished_at") or 0),
               default={})


def _latest_for(target: str, kind: str, root: str | Path | None) -> dict:
    return (_latest_record(target, kind, root).get("evidence") or {})


def _reusable(previous: dict, inputs: dict[str, str], *, live: bool,
              max_age_seconds: float) -> bool:
    if not inputs or previous.get("status") != "pass" or previous.get("blockers"):
        return False
    source = previous.get("source") if isinstance(previous.get("source"), dict) else {}
    environment = source.get("environment") if isinstance(source.get("environment"), dict) else {}
    if dict(source.get("inputs") or {}) != inputs or (live and not environment.get("live")):
        return False
    finished = float(previous.get("finished_at") or 0)
    return bool(finished and (max_age_seconds <= 0 or time.time() - finished <= max_age_seconds))


def _reuse_envelope(*, target: str, level: str, previous: dict, run_id: str, sequence: int,
                    commit: str, inputs: dict[str, str], trigger: str) -> dict:
    spec = LEVEL_SPECS[level]
    previous_metrics = previous.get("metrics") if isinstance(previous.get("metrics"), dict) else {}
    metrics = dict(previous_metrics)
    metrics.update({"reused": True, "reuse_saved_s": float(previous_metrics.get("elapsed_s") or 0)})
    return evidence_schema.new_envelope(
        subject_type="quality", subject_id=f"quality-{level}", target=target,
        evidence_type=spec["kind"], run_id=run_id,
        parent_evidence_id=str(previous.get("evidence_id") or ""), sequence=sequence,
        attempt=int((previous.get("lineage") or {}).get("attempt") or 0) + 1,
        trigger=trigger, status="pass", runner="quality_executor.py:reuse",
        commit=commit, environment={"python": sys.version.split()[0], "mode": "release",
                                    "level": level, "live": bool((previous.get("source") or {})
                                                                  .get("environment", {}).get("live")),
                                    "reused": True},
        inputs=inputs, started_at=time.time(), finished_at=time.time(),
        assertions=list(previous.get("assertions") or []), metrics=metrics,
        baseline={"evidence_id": previous.get("evidence_id"), "status": previous.get("status"),
                  "metrics": previous_metrics},
        links=[{"rel": "reused-from", "evidence_id": previous.get("evidence_id")}],
        claims=[{"level": level, "description": spec["description"],
                 "reuse_reason": "关联源码与测试输入哈希未变化，复用已通过机器证据"}])


def _live_l0(keys: list[str], run_dir: Path, timeout: float, env: dict) -> dict | None:
    if not keys:
        return None
    output = run_dir / "live-l0.json"
    command = [sys.executable, str(REPO / "dev" / "verify_all_harness_modes.py"),
               "--keys", ",".join(keys), "--single-only", "--batch", "1",
               "--budget", str(max(10, int(timeout))),
               "--output", str(output)]
    row = _run_command(command, env, max(timeout * max(1, len(keys)), timeout + 60))
    if output.is_file():
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
    else:
        report = {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    row["report"] = report
    row["ok"] = bool(row["ok"] and summary.get("single_passed") == summary.get("single_total")
                     and int(summary.get("single_total") or 0) == len(keys))
    return row


def record_fast_contract(report: dict, *, target: str, files: list[str] | None = None,
                         run_id: str = "", evidence_root: str | Path | None = None,
                         parent_evidence_id: str = "") -> dict:
    run_id = run_id or evidence_schema.new_run_id("fast")
    inputs = _hash_inputs(files)
    ok = report.get("ok") is True
    assertions = [{"id": "related-contracts", "name": "相关改动合同", "ok": ok,
                   "detail": f"{len(report.get('commands') or [])} 组 · {report.get('elapsed', 0)}s"}]
    blockers = [] if ok else ["相关改动合同未通过"]
    envelope = evidence_schema.new_envelope(
        subject_type="quality", subject_id="fast-contract", target=target,
        evidence_type="fast_contract", run_id=run_id, parent_evidence_id=parent_evidence_id,
        sequence=0, trigger="git-diff", status="pass" if ok else "issue",
        runner="harness_selfcheck.py", commit=evidence_schema._commit(REPO),
        environment={"python": sys.version.split()[0], "mode": "fast"}, inputs=inputs,
        finished_at=time.time(), assertions=assertions, blockers=blockers,
        metrics={"elapsed_s": float(report.get("elapsed") or 0),
                 "commands": len(report.get("commands") or []), "cached": bool(report.get("cached"))},
        claims=[{"output": str(report.get("stdout") or "")[-6000:]}])
    return evidence_schema.store_envelope(envelope, evidence_root)


def execute_levels(*, target: str, levels=None, files: list[str] | None = None,
                   run_id: str = "", evidence_root: str | Path | None = None,
                   live_harnesses: list[str] | None = None, live: bool = False,
                   timeout: float = 600, trigger: str = "release", phase: str = "release",
                   reuse: bool = True,
                   max_reuse_age_seconds: float = evidence_schema.DEFAULT_MAX_AGE_SECONDS) -> dict:
    selected = levels_for_phase(levels, phase)
    run_id = run_id or evidence_schema.new_run_id("release")
    root = Path(evidence_root or evidence_schema.default_store_root())
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    commit = evidence_schema._commit(REPO)
    started_at = time.time()
    stored, failures, executed_levels, reused_levels = [], [], [], []
    saved_seconds = 0.0
    with tempfile.TemporaryDirectory(prefix="quality-executor-") as temp_root:
        env = dict(os.environ)
        env["MEYO_LEGION_DIR"] = str(Path(temp_root) / "meyo")
        env["LEGION_HOME_DIR"] = str(Path(temp_root) / "legion")
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        for sequence, level in enumerate(selected, start=1):
            spec = LEVEL_SPECS[level]
            inputs = _hash_inputs(_level_input_files(level, files))
            previous_record = _latest_record(target, spec["kind"], root)
            previous = previous_record.get("evidence") or {}
            if reuse and _reusable(previous, inputs, live=bool(level == "L0" and live),
                                    max_age_seconds=max_reuse_age_seconds):
                envelope = _reuse_envelope(
                    target=target, level=level, previous=previous, run_id=run_id,
                    sequence=sequence, commit=commit, inputs=inputs, trigger=trigger)
                saved = evidence_schema.store_envelope(envelope, root)
                comparison = evidence_schema.compare(saved["evidence"], previous)
                stored.append({"level": level, "path": saved["path"], "evidence": saved["evidence"],
                               "comparison": comparison, "reused": True,
                               "reused_from": previous_record.get("path") or ""})
                reused_levels.append(level)
                saved_seconds += float((previous.get("metrics") or {}).get("elapsed_s") or 0)
                continue
            executed_levels.append(level)
            commands = []
            if spec["pytest"]:
                commands.append([sys.executable, "-m", "pytest", *spec["pytest"], "-q"])
            commands.extend([sys.executable, name] for name in spec["scripts"])
            results = [_run_command(command, env, timeout) for command in commands]
            live_result = _live_l0(list(live_harnesses or []), run_dir, timeout, env) \
                if level == "L0" and live else None
            if live_result:
                results.append(live_result)
            ok = bool(results) and all(row["ok"] for row in results)
            assertions = [{"id": f"{level}-command-{index + 1}",
                           "name": " ".join(row["command"][-4:]), "ok": row["ok"],
                           "detail": f"rc={row['returncode']} · {row['elapsed_s']}s"}
                          for index, row in enumerate(results)]
            blockers = [f"{level} 未通过：{' '.join(row['command'][-4:])}"
                        for row in results if not row["ok"]]
            log_path = run_dir / "logs" / f"{sequence:03d}-{level}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("\n\n".join(
                "$ " + " ".join(row["command"]) + "\n" + row.get("stdout", "") + row.get("stderr", "")
                for row in results), encoding="utf-8")
            envelope = evidence_schema.new_envelope(
                subject_type="quality", subject_id=f"quality-{level}", target=target,
                evidence_type=spec["kind"], run_id=run_id,
                parent_evidence_id=str(previous.get("evidence_id") or ""),
                sequence=sequence, attempt=int((previous.get("lineage") or {}).get("attempt") or 0) + 1,
                trigger=trigger, status="pass" if ok else "issue", runner="quality_executor.py",
                commit=commit, environment={"python": sys.version.split()[0], "mode": "release",
                                                "level": level, "live": bool(live_result)},
                inputs=inputs, started_at=started_at, finished_at=time.time(), assertions=assertions,
                artifacts=[evidence_schema.file_evidence(log_path, root=run_dir, kind="quality-log")],
                blockers=blockers, metrics={"elapsed_s": round(sum(row["elapsed_s"] for row in results), 3),
                                             "commands": len(results), "passed": sum(row["ok"] for row in results),
                                             "total": len(results)},
                baseline={"evidence_id": previous.get("evidence_id"),
                          "status": previous.get("status"), "metrics": previous.get("metrics") or {}},
                claims=[{"level": level, "description": spec["description"]}])
            saved = evidence_schema.store_envelope(envelope, root)
            stored.append({"level": level, "path": saved["path"], "evidence": saved["evidence"],
                           "comparison": evidence_schema.compare(saved["evidence"], previous)})
            if not ok:
                failures.append({"level": level, "blockers": blockers, "evidence_id": envelope["evidence_id"]})
    finished_at = time.time()
    return {"ok": bool(selected) and not failures, "target": target, "run_id": run_id,
            "phase": phase, "commit": commit, "levels": selected,
            "executed_levels": executed_levels, "reused_levels": reused_levels,
            "saved_seconds": round(saved_seconds, 3),
            "elapsed_s": round(finished_at - started_at, 3),
            "started_at": started_at, "finished_at": finished_at, "evidence": stored,
            "failures": failures, "required_subjects": [f"quality-{level}" for level in selected]}


def evidence_paths(*, target: str = "", run_id: str = "",
                   evidence_root: str | Path | None = None) -> list[str]:
    rows = evidence_schema.list_envelopes(evidence_root, target=target)
    paths = []
    for row in rows:
        envelope = row.get("evidence") or {}
        lineage = envelope.get("lineage") if isinstance(envelope.get("lineage"), dict) else {}
        if run_id and str(lineage.get("run_id") or "") != run_id:
            continue
        paths.append(str(row["path"]))
    return paths
