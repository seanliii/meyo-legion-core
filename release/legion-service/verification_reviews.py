#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机产品质量、升级生命周期与修复复验账本；不修改原始交付。"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from pathlib import Path

import evidence_schema
import legion_paths as _paths

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows fallback
    _fcntl = None

_LOCK = threading.RLock()
_VERDICTS = {"pending", "pass", "issue"}
_PRIORITIES = {"P0", "P1", "P2", "P3"}
_UPGRADE_STATUSES = {
    "not_planned", "planned", "in_progress", "ready", "released", "observing", "validated",
    "blocked", "rolled_back",
}
_TRANSITIONS = {
    "not_planned": {"not_planned", "planned", "in_progress", "blocked"},
    "planned": {"planned", "in_progress", "blocked", "not_planned"},
    "in_progress": {"in_progress", "ready", "blocked", "planned"},
    "ready": {"ready", "released", "in_progress", "blocked"},
    "released": {"released", "observing", "rolled_back", "blocked"},
    "observing": {"observing", "validated", "released", "rolled_back", "blocked"},
    "validated": {"validated", "observing", "rolled_back", "blocked"},
    "blocked": {"blocked", "planned", "in_progress", "ready", "not_planned"},
    "rolled_back": {"rolled_back", "planned", "in_progress"},
}
_MACHINE_STATUSES = {"not_run", "queued", "running", "partial", "pass", "issue", "interrupted"}
_ROOT_CAUSE_STATUSES = {"not_required", "unresolved", "localized", "verified", "contradicted"}
_DIMENSIONS = ("user_value", "reliability", "speed", "ux", "completeness")
_DIMENSION_WEIGHTS = {"user_value": .25, "reliability": .25, "speed": .15, "ux": .15,
                      "completeness": .20}
_TARGET_RE = re.compile(r"^(session|artifact|native|feature|evidence|desktop|mobile):[^\x00-\x1f]{1,700}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+\-/]{0,79}$")
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SCHEMA_VERSION = 6
_WORKFLOW_TOKEN = object()
_REPO = Path(__file__).resolve().parents[2]
_COMMIT_CACHE: dict[str, float | str] = {"value": "", "at": 0.0}


def _path() -> Path:
    return Path(_paths.home_under("verification-center", "reviews.json"))


@contextlib.contextmanager
def _file_locked():
    lock_path = _path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


def _load_unlocked() -> dict[str, dict]:
    try:
        value = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    for target, row in list(value.items()):
        if not isinstance(row, dict):
            value.pop(target, None)
            continue
        row.setdefault("target", target)
        row.setdefault("schema_version", int(row.get("schema_version") or 1))
        row.setdefault("upgrade_status", "not_planned")
        row.setdefault("machine_status", "not_run")
        row.setdefault("workflow_history", [])
        row.setdefault("version_history", [])
        gate = product_gate(row)
        row["quality_assessment"] = gate["quality_assessment"]
        row["product_gate"] = gate
        row["product_manager_decision"] = product_manager_decision(row, gate)
    return value


def load() -> dict[str, dict]:
    with _LOCK, _file_locked():
        return _load_unlocked()


def get(target: str) -> dict:
    row = dict(load().get(str(target or "")) or {})
    if row:
        row["product_gate"] = product_gate(row)
    return row


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _optional_score(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        score = int(value)
    except (TypeError, ValueError):
        raise ValueError("quality_score must be an integer from 0 to 100")
    if not 0 <= score <= 100:
        raise ValueError("quality_score must be an integer from 0 to 100")
    return score


def _dimensions(value) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    result = {}
    for name in _DIMENSIONS:
        raw = source.get(name)
        if raw in (None, ""):
            continue
        try:
            score = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"dimension {name} must be an integer from 1 to 5")
        if not 1 <= score <= 5:
            raise ValueError(f"dimension {name} must be an integer from 1 to 5")
        result[name] = score
    return result


def _version(value) -> str:
    value = _text(value, 80)
    if value and not _VERSION_RE.fullmatch(value):
        raise ValueError("version contains unsupported characters")
    return value


def _timestamp(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError("timestamp must be numeric")
    return result if result > 0 else None


def _string_list(value, limit: int = 100) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(_text(item, 1000) for item in value if _text(item, 1000)))[:limit]


def _commit_id(value) -> str:
    value = _text(value, 64)
    if value and not _COMMIT_RE.fullmatch(value):
        raise ValueError("commit must be a 7-64 character hexadecimal SHA")
    return value.lower()


def _bounded_int(value, *, default: int, minimum: int, maximum: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return result


def _current_commit() -> str:
    now = time.monotonic()
    if _COMMIT_CACHE["value"] and now - float(_COMMIT_CACHE["at"]) < 5:
        return str(_COMMIT_CACHE["value"])
    value = evidence_schema._commit(_REPO)
    _COMMIT_CACHE.update({"value": value, "at": now})
    return value


def _quality_label(score: int | None, threshold: int) -> str:
    if score is None:
        return "未评分"
    if score >= 90:
        return "优秀"
    if score >= threshold:
        return "良好"
    return "待改进" if score >= 60 else "不合格"


def _evidence_health(row: dict) -> dict:
    """读取当前升级绑定的证据，返回可发布覆盖率；证据缺失或跨版本时绝不沿用绿灯。"""
    required = _string_list(row.get("required_evidence"))
    if not required:
        required = [f"quality-{level}" for level in _string_list(row.get("required_levels"), 5)
                    if level in {"L0", "L1", "L2", "L3", "L4"}]
    latest: dict[str, dict] = {}
    invalid = []
    for raw_path in _string_list(row.get("evidence_paths"), 200):
        checked = evidence_schema.read_envelope(raw_path)
        if not checked.get("ok"):
            invalid.append({"path": raw_path, "reasons": list(checked.get("errors") or ["不可读"])})
            continue
        envelope = checked.get("evidence") or {}
        subject = envelope.get("subject") if isinstance(envelope.get("subject"), dict) else {}
        if str(subject.get("target") or "") not in {"", str(row.get("target") or "")}:
            invalid.append({"path": raw_path, "reasons": ["目标不匹配"]})
            continue
        subject_id = str(subject.get("id") or "")
        finished = float(envelope.get("finished_at") or envelope.get("started_at") or 0)
        previous = latest.get(subject_id)
        if previous is None or finished > float(previous.get("finished_at") or previous.get("started_at") or 0):
            latest[subject_id] = envelope
    current_commit = str(row.get("target_commit") or _current_commit() or "")
    required_run_id = str(row.get("evidence_run_id") or "")
    satisfied, stale, freshness_rows = [], [], {}
    for subject_id in required:
        envelope = latest.get(subject_id)
        if not envelope:
            continue
        fresh = evidence_schema.freshness(
            envelope, current_commit=current_commit, required_run_id=required_run_id,
            input_root=_REPO)
        freshness_rows[subject_id] = fresh
        if fresh.get("current") and not envelope.get("blockers"):
            satisfied.append(subject_id)
        else:
            stale.append(subject_id)
    missing = sorted(set(required) - set(latest))
    coverage = 1.0 if not required else len(satisfied) / len(required)
    public = {
        "required": required, "satisfied": sorted(satisfied), "missing": missing,
        "stale": sorted(stale), "invalid": invalid, "coverage": round(coverage, 4),
        "current": bool(required and coverage == 1 and not invalid),
        "commit": current_commit, "run_id": required_run_id,
    }
    return {**public, "_records": latest, "_freshness": freshness_rows}


def _value_metric_contract(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    metrics = {}
    for raw_name, raw_rule in (source.get("metrics") or {}).items() if isinstance(source.get("metrics"), dict) else []:
        name = str(raw_name or "").strip()[:120]
        rule = raw_rule if isinstance(raw_rule, dict) else {}
        direction = str(rule.get("direction") or "increase").lower()
        if not name or direction not in {"increase", "decrease", "maintain"}:
            continue
        normalized = {"direction": direction}
        for key in ("minimum", "maximum", "minimum_delta", "minimum_improvement_ratio"):
            if isinstance(rule.get(key), (int, float)):
                normalized[key] = float(rule[key])
        metrics[name] = normalized
    return {"metrics": metrics,
            "minimum_sample_size": max(1, min(1_000_000, int(source.get("minimum_sample_size") or 1)))}


def _observation_value_proof(row: dict) -> dict:
    """只用绑定目标版本的 release_observation 机器证据证明价值，不信人工总结文本。"""
    contract = _value_metric_contract(row.get("value_metric_contract"))
    required = contract["metrics"]
    candidates = []
    paths = list(dict.fromkeys([*_string_list(row.get("observation_evidence_paths"), 100),
                                *_string_list(row.get("evidence_paths"), 200)]))
    for path in paths:
        checked = evidence_schema.read_envelope(path)
        envelope = checked.get("evidence") or {}
        subject = envelope.get("subject") if isinstance(envelope.get("subject"), dict) else {}
        source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
        if (checked.get("ok") and envelope.get("evidence_type") == "release_observation"
                and str(subject.get("target") or "") == str(row.get("target") or "")):
            target_commit = str(row.get("target_commit") or "")
            target_version = str(row.get("target_version") or "")
            binding_ok = (not target_commit or str(source.get("commit") or "") == target_commit) \
                and (not target_version or str(source.get("version") or "") == target_version)
            # 观察任务本身失败同样是有效的价值回退证据；新鲜度检查忽略业务 status，
            # 随后由指标/断言/status 统一判定 proven 或 regressed。
            freshness_probe = {**envelope, "status": "pass"}
            fresh = evidence_schema.freshness(
                freshness_probe, current_commit=target_commit,
                max_age_seconds=float(row.get("observation_window_hours") or 24) * 3600,
                input_root=_REPO)
            if binding_ok and fresh.get("current"):
                candidates.append((float(envelope.get("finished_at") or envelope.get("started_at") or 0),
                                   path, envelope))
    if not required:
        return {"status": "unconfigured", "proven": False, "contract": contract,
                "checks": [], "blockers": ["尚未配置机器可判定的升级价值指标"]}
    if not candidates:
        return {"status": "missing", "proven": False, "contract": contract,
                "checks": [], "blockers": ["缺少绑定目标版本的 release_observation 机器证据"]}
    _, path, envelope = max(candidates, key=lambda item: item[0])
    metrics = envelope.get("metrics") if isinstance(envelope.get("metrics"), dict) else {}
    baseline_raw = envelope.get("baseline") if isinstance(envelope.get("baseline"), dict) else {}
    baseline = baseline_raw.get("metrics") if isinstance(baseline_raw.get("metrics"), dict) else baseline_raw
    sample_size = metrics.get("sample_size", metrics.get("samples", metrics.get("users", 0)))
    assertions = [item for item in envelope.get("assertions") or [] if isinstance(item, dict)]
    checks, blockers = [], []
    if not assertions or any(item.get("ok") is not True for item in assertions):
        blockers.append("发布后用户结果断言不完整或未全部通过")
    if not str((envelope.get("baseline") or {}).get("evidence_id") or ""):
        blockers.append("观察证据未绑定发布前基线 evidence_id")
    if not isinstance(sample_size, (int, float)) or float(sample_size) < contract["minimum_sample_size"]:
        blockers.append(f"观察样本不足：{sample_size or 0}/{contract['minimum_sample_size']}")
    for name, rule in required.items():
        after, before = metrics.get(name), baseline.get(name)
        ok, reasons = isinstance(after, (int, float)), []
        if not ok:
            reasons.append("缺少当前指标")
        if rule["direction"] in {"increase", "decrease"} and not isinstance(before, (int, float)):
            ok = False; reasons.append("缺少基线指标")
        if ok and "minimum" in rule and float(after) < rule["minimum"]:
            ok = False; reasons.append(f"低于最低值 {rule['minimum']}")
        if ok and "maximum" in rule and float(after) > rule["maximum"]:
            ok = False; reasons.append(f"高于最高值 {rule['maximum']}")
        delta = float(after) - float(before) if ok and isinstance(before, (int, float)) else None
        signed = delta if rule["direction"] == "increase" else -delta if delta is not None else None
        if ok and rule["direction"] in {"increase", "decrease"} and signed is not None:
            if "minimum_delta" in rule and signed < rule["minimum_delta"]:
                ok = False; reasons.append(f"改善量低于 {rule['minimum_delta']}")
            if "minimum_improvement_ratio" in rule:
                ratio = signed / abs(float(before)) if float(before) != 0 else 0.0
                if ratio < rule["minimum_improvement_ratio"]:
                    ok = False; reasons.append(f"改善比例低于 {rule['minimum_improvement_ratio']:.0%}")
        checks.append({"metric": name, "before": before, "after": after, "delta": delta,
                       "direction": rule["direction"], "ok": ok, "reasons": reasons})
        if not ok:
            blockers.append(name + "：" + "、".join(reasons))
    if envelope.get("status") != "pass" or envelope.get("blockers"):
        blockers.extend(str(item) for item in (envelope.get("blockers") or ["观察证据状态未通过"]))
    proven = not blockers and bool(checks) and all(item["ok"] for item in checks)
    return {"status": "proven" if proven else "regressed", "proven": proven,
            "contract": contract, "checks": checks, "blockers": list(dict.fromkeys(blockers)),
            "evidence_id": envelope.get("evidence_id"), "path": path,
            "sample_size": sample_size, "metrics": metrics, "baseline": baseline}


def quality_assessment(row: dict) -> dict:
    """把机器事实和人工判断分开计分，并解释分数、置信度与版本收益。"""
    health = _evidence_health(row)
    records = health.get("_records") or {}
    freshness_rows = health.get("_freshness") or {}
    human_dimensions = row.get("dimensions") if isinstance(row.get("dimensions"), dict) else {}
    machine_dimensions: dict[str, dict] = {}

    current_records = [records[name] for name in health["satisfied"] if name in records]
    assertions = [item for record in current_records for item in record.get("assertions") or []
                  if isinstance(item, dict)]
    if assertions:
        passed = sum(item.get("ok") is True for item in assertions)
        machine_dimensions["reliability"] = {
            "score": round(100 * passed / len(assertions)), "evidence_ids": [
                record.get("evidence_id") for record in current_records],
            "rationale": f"当前证据断言通过 {passed}/{len(assertions)}",
        }
    if health["required"] and (records or health["invalid"]):
        machine_dimensions["completeness"] = {
            "score": round(100 * health["coverage"]),
            "evidence_ids": [records[name].get("evidence_id") for name in health["satisfied"] if name in records],
            "rationale": f"必需验收覆盖 {len(health['satisfied'])}/{len(health['required'])}",
        }
    ux_records = [record for name, record in records.items()
                  if name == "quality-L3" and freshness_rows.get(name, {}).get("current")]
    ux_assertions = [item for record in ux_records for item in record.get("assertions") or []
                     if isinstance(item, dict)]
    if ux_assertions:
        passed = sum(item.get("ok") is True for item in ux_assertions)
        machine_dimensions["ux"] = {
            "score": round(100 * passed / len(ux_assertions)),
            "evidence_ids": [record.get("evidence_id") for record in ux_records],
            "rationale": f"真实浏览器与交付断言通过 {passed}/{len(ux_assertions)}",
        }
    speed_ratios = []
    for comparison in (row.get("machine_comparison") or {}).values():
        metrics = comparison.get("metric_delta") if isinstance(comparison, dict) else {}
        elapsed = metrics.get("elapsed_s") if isinstance(metrics, dict) else None
        if isinstance(elapsed, dict) and isinstance(elapsed.get("before"), (int, float)) \
                and isinstance(elapsed.get("after"), (int, float)) and elapsed["before"] > 0:
            speed_ratios.append(float(elapsed["after"]) / float(elapsed["before"]))
    if speed_ratios:
        ratio = sum(speed_ratios) / len(speed_ratios)
        machine_dimensions["speed"] = {
            "score": round(max(0, min(100, 100 * (2 - ratio)))),
            "evidence_ids": [record.get("evidence_id") for record in current_records],
            "rationale": f"相对基线耗时为 {ratio:.2f} 倍",
        }

    dimensions, machine_weight, human_weight = {}, 0.0, 0.0
    for name in _DIMENSIONS:
        human_score = int(human_dimensions[name]) * 20 if name in human_dimensions else None
        machine = machine_dimensions.get(name)
        if machine and human_score is not None:
            score, source = round(machine["score"] * .75 + human_score * .25), "机器 75% + 人工 25%"
            machine_weight += _DIMENSION_WEIGHTS[name] * .75
            human_weight += _DIMENSION_WEIGHTS[name] * .25
        elif machine:
            score, source = machine["score"], "机器证据"
            machine_weight += _DIMENSION_WEIGHTS[name]
        elif human_score is not None:
            score, source = human_score, "人工判断"
            human_weight += _DIMENSION_WEIGHTS[name]
        else:
            continue
        dimensions[name] = {
            "score": int(score), "source": source,
            "rationale": machine.get("rationale") if machine else "评审者填写的五级判断",
            "evidence_ids": list(machine.get("evidence_ids") or []) if machine else [],
        }
    available_weight = sum(_DIMENSION_WEIGHTS[name] for name in dimensions)
    dimension_score = (sum(dimensions[name]["score"] * _DIMENSION_WEIGHTS[name] for name in dimensions)
                       / available_weight) if available_weight else None
    human_score = row.get("quality_score") if isinstance(row.get("quality_score"), int) else None
    if human_score is not None and dimensions:
        score = round(sum((dimensions[name]["score"] if name in dimensions else human_score)
                          * _DIMENSION_WEIGHTS[name] for name in _DIMENSIONS))
        basis = "mixed" if machine_weight > 0 else "manual"
    elif human_score is not None:
        score, basis = human_score, "manual"
    elif dimension_score is not None:
        score = round(dimension_score)
        basis = "machine" if machine_weight > 0 and human_weight == 0 else "mixed" if machine_weight > 0 else "manual"
    else:
        score, basis = None, "unrated"
    confidence = 0.0
    if score is not None:
        confidence = .2
        confidence += .5 * float(health["coverage"] if health["required"] else 0)
        confidence += .2 * min(1.0, machine_weight / max(available_weight, .01))
        confidence += .05 if str(row.get("observation_result") or "").strip() else 0
        confidence += .05 if str(row.get("user_feedback") or "").strip() else 0
    confidence = round(min(1.0, confidence), 2)
    confidence_label = "高" if confidence >= .8 else "中" if confidence >= .55 else "低"
    history = list(row.get("version_history") or [])
    baseline = history[-1] if history else {}
    baseline_score = baseline.get("effective_quality_score", baseline.get("quality_score"))
    delta = round(score - baseline_score, 1) if isinstance(score, (int, float)) \
        and isinstance(baseline_score, (int, float)) else None
    gaps = []
    if health["missing"]:
        gaps.append("缺少机器证据：" + "、".join(health["missing"]))
    if health["stale"] or health["invalid"]:
        gaps.append("存在过期或无效证据")
    if row.get("source_finding_id") and str(row.get("root_cause_status") or "unresolved") \
            not in {"verified", "not_required"}:
        gaps.append("自动发现的问题尚未完成源码级根因核验")
    for name in _DIMENSIONS:
        if name not in dimensions:
            gaps.append(f"{name} 尚无可计分证据")
    value_evidence = _observation_value_proof(row)
    value_proof = value_evidence["status"] if value_evidence["status"] in {"proven", "regressed"} else \
        "supported" if health["current"] and str(row.get("success_metric") or "").strip() else "hypothesis"
    return {
        "score": score, "human_score": human_score,
        "machine_score": round(dimension_score) if dimension_score is not None and machine_weight > 0 else None,
        "label": _quality_label(score, int(row.get("quality_threshold") or 80)),
        "basis": basis, "confidence": confidence, "confidence_label": confidence_label,
        "dimensions": dimensions, "evidence": {key: value for key, value in health.items()
                                                if not key.startswith("_")},
        "version_delta": {"baseline_version": baseline.get("version") or row.get("current_version") or "",
                          "baseline_score": baseline_score, "current_score": score, "delta": delta},
        "value_proof": value_proof, "value_evidence": value_evidence, "gaps": gaps,
    }


def product_manager_decision(row: dict, gate: dict | None = None) -> dict:
    """给出唯一下一动作与理由；发布/回滚仍明确要求人类确认。"""
    gate = gate or product_gate(row)
    status = str(row.get("upgrade_status") or "not_planned")
    machine = str(row.get("machine_status") or "not_run")
    blockers = list(gate.get("blockers") or [])
    missing_brief = [item for item in blockers if item.startswith("未填写") or "版本计划" in item]
    if row.get("source_finding_id") and str(row.get("root_cause_status") or "unresolved") \
            not in {"verified", "not_required"}:
        action, label, autonomous = "investigate_root_cause", "让军团定位并独立核验根因", True
        reasons = ["自动发现的问题在改代码前必须落到可核验的源码 file:line"]
    elif row.get("source_finding_id") and (machine in {"issue", "interrupted"} or status == "blocked"):
        action, label, autonomous = "repair", "按已核验影响面做最小修复并持续复验", True
        reasons = [row.get("root_cause") or row.get("machine_summary") or "自动发现问题已具备可执行影响面"]
    elif status == "observing" and gate.get("quality_assessment", {}).get("value_proof") == "regressed":
        action, label, autonomous = "repair", "升级价值指标未达标，自动回到修复与复验", True
        reasons = list((gate.get("quality_assessment", {}).get("value_evidence") or {}).get("blockers") or
                       ["发布后指标低于价值合同"])
    elif missing_brief:
        action, label, autonomous = "complete_brief", "补齐升级目标", False
        reasons = missing_brief[:4]
    elif machine in {"issue", "interrupted"} or status == "blocked":
        action, label, autonomous = "repair", "让军团修复并持续复验", True
        reasons = [row.get("machine_summary") or "机器证据发现回归"] + blockers[:3]
    elif machine != "pass" or not (gate.get("evidence_health") or {}).get("current"):
        action, label, autonomous = "reverify_release", "运行发布复验（自动复用有效证据）", True
        reasons = ["当前版本的必需机器证据尚未闭合"] + blockers[:3]
    elif blockers:
        action, label, autonomous = "repair", "让军团收口剩余质量阻塞", True
        reasons = blockers[:4]
    elif status in {"planned", "in_progress"}:
        action, label, autonomous = "mark_ready", "机器证据已齐，推进到待发布", True
        reasons = ["质量门和证据门已通过"]
    elif status == "ready":
        action, label, autonomous = "release", "确认发布这个版本", False
        reasons = ["升级目标、质量分和当前提交证据已齐"]
    elif status == "released":
        action, label, autonomous = "observe", "进入发布后观察", False
        reasons = ["需要用真实指标和用户反馈证明升级价值"]
    elif status == "observing" and gate.get("quality_assessment", {}).get("value_proof") != "proven":
        action, label, autonomous = "record_outcome", "接入发布后机器指标证据", False
        reasons = list((gate.get("quality_assessment", {}).get("value_evidence") or {}).get("blockers") or
                       ["升级价值尚未形成绑定目标版本的机器证据"])
    elif status == "observing":
        action, label, autonomous = "mark_validated", "确认升级价值并完成闭环", True
        reasons = ["观察结果与用户反馈已形成价值证据"]
    else:
        action, label, autonomous = "none", "当前升级闭环已稳定", False
        reasons = ["没有新的机器阻塞或待决策事项"]
    return {"action": action, "label": label, "autonomous": autonomous,
            "human_confirmation_required": action in {"release", "observe", "record_outcome"},
            "reasons": [str(item) for item in reasons if str(item).strip()][:5]}


def product_gate(row: dict) -> dict:
    """把事实转成发布阻塞项；主观价值仍由评审者填写，机器证据只陈述客观状态。"""
    blockers = []
    verdict = str(row.get("verdict") or "pending")
    assessment = quality_assessment(row)
    score = assessment.get("score")
    threshold = int(row.get("quality_threshold") or 80)
    status = str(row.get("upgrade_status") or "not_planned")
    if verdict != "pass":
        blockers.append("验收尚未通过")
    if row.get("source_finding_id") and str(row.get("root_cause_status") or "unresolved") \
            not in {"verified", "not_required"}:
        blockers.append("源码级根因尚未独立核验")
    if score is None:
        blockers.append("未填写综合质量分")
    elif int(score) < threshold:
        blockers.append(f"综合质量分低于 {threshold}")
    for field, label in (("feature", "功能模块"), ("user_value", "用户价值"),
                         ("acceptance_criteria", "验收标准")):
        if not str(row.get(field) or "").strip():
            blockers.append(f"未填写{label}")
    if not str(row.get("evidence") or "").strip() and not _string_list(row.get("evidence_paths")):
        blockers.append("尚无验证证据")
    require_lifecycle = bool(row.get("require_lifecycle_gate"))
    if require_lifecycle and str(row.get("target") or "").startswith("feature:") and status == "not_planned":
        blockers.append("功能升级尚未进入版本计划")
    if status != "not_planned":
        for field, label in (("current_version", "当前版本"), ("target_version", "目标版本"),
                             ("upgrade_description", "升级说明"), ("owner", "负责人")):
            if not str(row.get(field) or "").strip():
                blockers.append(f"未填写{label}")
    required_levels = [item for item in _string_list(row.get("required_levels"), 5)
                       if item in {"L0", "L1", "L2", "L3", "L4"}]
    required = _string_list(row.get("required_evidence"))
    require_machine = bool(row.get("require_machine_gate"))
    if require_machine and status in {"ready", "released", "observing"} and not required_levels:
        blockers.append("未声明发布所需验收等级")
    evidence_health = assessment["evidence"]
    if require_machine and str(row.get("machine_status") or "not_run") == "pass" and not required:
        blockers.append("未绑定必需机器证据")
    if (require_machine or required or required_levels) and str(row.get("machine_status") or "not_run") != "pass":
        blockers.append("当前版本的必需机器证据尚未全部通过")
    if require_machine and str(row.get("machine_status") or "not_run") == "pass":
        if evidence_health.get("missing"):
            blockers.append("必需机器证据文件缺失")
        if evidence_health.get("stale") or evidence_health.get("invalid"):
            blockers.append("必需机器证据已过期、损坏或不属于当前版本")
        if required and not evidence_health.get("current"):
            blockers.append("机器证据覆盖不完整")
        if assessment.get("confidence", 0) < .55:
            blockers.append("质量评价置信度不足")
    if require_lifecycle and status in {"ready", "released", "observing", "validated"}:
        for field, label in (("success_metric", "升级成功指标"), ("release_scope", "灰度与发布范围"),
                             ("rollout_plan", "发布步骤"), ("rollback_version", "回滚版本"),
                             ("rollback_plan", "回滚方案"), ("rollback_conditions", "回滚条件"),
                             ("observation_plan", "发布后观察计划")):
            if not str(row.get(field) or "").strip():
                blockers.append(f"未填写{label}")
        if not str(row.get("target_commit") or "").strip():
            blockers.append("目标版本尚未绑定 commit")
    if status == "validated" and assessment.get("value_proof") != "proven":
        blockers.append("发布后价值尚未被观察结果与用户反馈证实")
    if status == "blocked":
        blockers.append("升级状态为阻塞")
    if status == "rolled_back":
        blockers.append("当前版本已回滚")
    blockers = list(dict.fromkeys(blockers))
    return {"release_ready": not blockers, "quality_label": _quality_label(score, threshold),
            "effective_quality_score": score, "quality_threshold": threshold,
            "quality_assessment": assessment, "evidence_health": evidence_health,
            "blockers": blockers, "blocking_count": len(blockers)}


def summary(rows: dict[str, dict] | None = None) -> dict:
    values = list((rows if rows is not None else load()).values())
    gates = [product_gate(row) for row in values]
    assessments = [gate["quality_assessment"] for gate in gates]
    scored = [int(item["score"]) for item in assessments if item.get("score") is not None]
    statuses = {name: 0 for name in sorted(_UPGRADE_STATUSES)}
    priorities = {name: 0 for name in sorted(_PRIORITIES)}
    machines = {name: 0 for name in sorted(_MACHINE_STATUSES)}
    now = time.time()
    for row in values:
        statuses[str(row.get("upgrade_status") or "not_planned")] += 1
        priorities[str(row.get("priority") or "P2")] += 1
        machines[str(row.get("machine_status") or "not_run")] += 1
    return {
        "reviewed": sum(str(row.get("verdict") or "pending") != "pending" for row in values),
        "scored": len(scored),
        "average_quality": round(sum(scored) / len(scored), 1) if scored else None,
        "release_ready": sum(gate["release_ready"] for gate in gates),
        "blocked": sum(not gate["release_ready"] for gate in gates),
        "overdue": sum(bool(row.get("due_at") and float(row["due_at"]) < now
                            and row.get("upgrade_status") not in {"released", "observing", "validated", "rolled_back"})
                       for row in values),
        "high_confidence": sum(float(item.get("confidence") or 0) >= .8 for item in assessments),
        "current_evidence": sum(bool(item.get("evidence", {}).get("current")) for item in assessments),
        "value_proven": sum(item.get("value_proof") == "proven" for item in assessments),
        "autonomous_next": sum(product_manager_decision(row, gate).get("autonomous")
                               for row, gate in zip(values, gates)),
        "upgrade_statuses": statuses, "priorities": priorities, "machine_statuses": machines,
    }


def query(*, offset: int = 0, limit: int = 50, q: str = "", priority: str = "",
          upgrade_status: str = "", release_ready: str = "", owner: str = "",
          machine_status: str = "") -> dict:
    """查询产品评审账本；只返回独立账本，不读取或修改原始交付。"""
    offset, limit = max(0, int(offset)), max(1, min(500, int(limit)))
    needle = str(q or "").strip().lower()
    want_ready = str(release_ready or "").lower()
    items = []
    for row in load().values():
        gate = product_gate(row)
        searchable = " ".join(str(row.get(name) or "") for name in (
            "target", "feature", "upgrade_description", "user_value", "owner", "note",
            "risk", "next_action")).lower()
        if needle and needle not in searchable:
            continue
        if priority and str(row.get("priority") or "P2") != priority:
            continue
        if upgrade_status and str(row.get("upgrade_status") or "not_planned") != upgrade_status:
            continue
        if owner and str(row.get("owner") or "") != owner:
            continue
        if machine_status and str(row.get("machine_status") or "not_run") != machine_status:
            continue
        if want_ready in {"true", "1", "ready"} and not gate["release_ready"]:
            continue
        if want_ready in {"false", "0", "blocked"} and gate["release_ready"]:
            continue
        item = dict(row)
        item["quality_assessment"] = gate["quality_assessment"]
        item["product_manager_decision"] = product_manager_decision(row, gate)
        item["product_gate"] = gate
        items.append(item)
    items.sort(key=lambda row: (-float(row.get("updated_at") or 0), str(row.get("target") or "")))
    return {"items": items[offset:offset + limit], "offset": offset, "limit": limit,
            "total": len(items), "has_more": offset + limit < len(items), "summary": summary()}


def _workflow_event(kind: str, actor: str, *, before: str = "", after: str = "",
                    note: str = "", run_id: str = "") -> dict:
    return {"id": f"wf-{int(time.time() * 1000)}-{threading.get_ident()}", "kind": kind,
            "actor": actor, "at": time.time(), "from": before, "to": after,
            "note": _text(note, 1000), "run_id": _text(run_id, 160)}


def save(target: str, verdict: str, note: str = "", actor: str = "local-reviewer",
         metadata: dict | None = None, _workflow_token=None, **fields) -> dict:
    target = str(target or "").strip()
    verdict = str(verdict or "pending").strip().lower()
    note = _text(note, 4000)
    actor = _text(actor, 120) or "local-reviewer"
    payload = {**(metadata if isinstance(metadata, dict) else {}), **fields}
    protected_root_fields = {"source_finding_id", "source_finding_kind", "skeleton_zones",
                             "root_cause", "root_cause_status", "root_cause_evidence"}
    supplied_protected = protected_root_fields & set(payload)
    if supplied_protected and _workflow_token is not _WORKFLOW_TOKEN:
        return {"ok": False, "error": "根因与骨架状态只能由验收工作流写入",
                "protected_fields": sorted(supplied_protected)}
    if not _TARGET_RE.fullmatch(target):
        return {"ok": False, "error": "invalid review target"}
    if verdict not in _VERDICTS:
        return {"ok": False, "error": "verdict must be pending/pass/issue"}
    with _LOCK, _file_locked():
        rows = _load_unlocked()
        previous = rows.get(target) or {}
        selected = lambda name, default="": payload[name] if name in payload else previous.get(name, default)
        priority = str(selected("priority", "P2") or "P2").upper()
        upgrade_status = str(selected("upgrade_status", "not_planned") or "not_planned").lower()
        machine_status = str(selected("machine_status", "not_run") or "not_run").lower()
        if priority not in _PRIORITIES:
            return {"ok": False, "error": "priority must be P0/P1/P2/P3"}
        if upgrade_status not in _UPGRADE_STATUSES:
            return {"ok": False, "error": "invalid upgrade_status"}
        if machine_status not in _MACHINE_STATUSES:
            return {"ok": False, "error": "invalid machine_status"}
        previous_status = str(previous.get("upgrade_status") or "not_planned")
        if previous and upgrade_status not in _TRANSITIONS.get(previous_status, {upgrade_status}):
            return {"ok": False, "error": f"invalid upgrade transition: {previous_status}->{upgrade_status}"}
        if previous_status != upgrade_status and upgrade_status in {"released", "observing", "rolled_back"} \
                and _workflow_token is not _WORKFLOW_TOKEN:
            return {"ok": False, "error": "release transitions must pass the unified workflow gate"}
        try:
            quality_score = _optional_score(selected("quality_score", None))
            quality_threshold = int(selected("quality_threshold", 80) or 80)
            root_cause_status = str(selected("root_cause_status", "unresolved") or "unresolved")
            if root_cause_status not in _ROOT_CAUSE_STATUSES:
                raise ValueError("invalid root_cause_status")
            if not 0 <= quality_threshold <= 100:
                raise ValueError("quality_threshold must be 0-100")
            dimensions = _dimensions(selected("dimensions", {}))
            current_version = _version(selected("current_version"))
            target_version = _version(selected("target_version"))
            current_commit = _commit_id(selected("current_commit"))
            target_commit = _commit_id(selected("target_commit"))
            due_at = _timestamp(selected("due_at", None))
            observation_window_hours = _bounded_int(
                selected("observation_window_hours", 24), default=24, minimum=1, maximum=8760,
                name="observation_window_hours")
            max_auto_cycles = _bounded_int(selected("max_auto_cycles", 2), default=2,
                                           minimum=1, maximum=5, name="max_auto_cycles")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        now = time.time()
        history = list(previous.get("history") or [])[-49:]
        if previous:
            history.append({key: previous.get(key) for key in (
                "verdict", "quality_score", "feature", "current_version", "target_version",
                "priority", "upgrade_status", "machine_status", "note", "actor", "updated_at")})
        workflow_history = list(previous.get("workflow_history") or [])[-99:]
        if not previous:
            workflow_history.append(_workflow_event("created", actor, after=upgrade_status, note=note))
        elif previous_status != upgrade_status:
            workflow_history.append(_workflow_event("state_changed", actor, before=previous_status,
                                                    after=upgrade_status, note=note,
                                                    run_id=str(selected("evidence_run_id"))))
        else:
            workflow_history.append(_workflow_event("review_updated", actor, before=previous_status,
                                                    after=upgrade_status, note=note,
                                                    run_id=str(selected("evidence_run_id"))))
        required_levels = [item for item in _string_list(selected("required_levels", []), 5)
                           if item in {"L0", "L1", "L2", "L3", "L4"}]
        row = {
            "schema_version": _SCHEMA_VERSION, "target": target, "verdict": verdict,
            "quality_score": quality_score, "quality_threshold": quality_threshold,
            "dimensions": dimensions, "feature": _text(selected("feature"), 160),
            "current_version": current_version, "target_version": target_version,
            "current_commit": current_commit, "target_commit": target_commit,
            "upgrade_description": _text(selected("upgrade_description"), 4000),
            "user_value": _text(selected("user_value"), 2000),
            "acceptance_criteria": _text(selected("acceptance_criteria"), 3000),
            "success_metric": _text(selected("success_metric"), 2000),
            "evidence": _text(selected("evidence"), 4000),
            "risk": _text(selected("risk"), 2000),
            "rollback_plan": _text(selected("rollback_plan"), 2000),
            "observation_plan": _text(selected("observation_plan"), 2000),
            "observation_window_hours": observation_window_hours,
            "observation_result": _text(selected("observation_result"), 3000),
            "release_scope": _text(selected("release_scope"), 1000),
            "rollout_plan": _text(selected("rollout_plan"), 2000),
            "rollback_version": _version(selected("rollback_version")),
            "rollback_conditions": _text(selected("rollback_conditions"), 2000),
            "user_feedback": _text(selected("user_feedback"), 3000),
            "value_outcome": _text(selected("value_outcome"), 3000),
            "value_metric_contract": _value_metric_contract(selected("value_metric_contract", {})),
            "observation_evidence_paths": _string_list(selected("observation_evidence_paths", []), 100),
            "next_action": _text(selected("next_action"), 2000),
            "priority": priority, "owner": _text(selected("owner"), 160),
            "due_at": due_at, "upgrade_status": upgrade_status,
            "required_levels": required_levels,
            "require_machine_gate": bool(selected("require_machine_gate", False)),
            "require_lifecycle_gate": bool(selected("require_lifecycle_gate", False)),
            "tracked_files": _string_list(selected("tracked_files", []), 500),
            "required_evidence": _string_list(selected("required_evidence", [])),
            "evidence_ids": _string_list(selected("evidence_ids", [])),
            "evidence_paths": _string_list(selected("evidence_paths", [])),
            "evidence_run_id": _text(selected("evidence_run_id"), 160),
            "machine_status": machine_status,
            "machine_summary": _text(selected("machine_summary"), 3000),
            "machine_comparison": selected("machine_comparison", {})
            if isinstance(selected("machine_comparison", {}), dict) else {},
            "source_finding_id": _text(selected("source_finding_id"), 160),
            "source_finding_kind": _text(selected("source_finding_kind"), 160),
            "skeleton_zones": _string_list(selected("skeleton_zones", []), 3),
            "root_cause": _text(selected("root_cause"), 4000),
            "root_cause_status": root_cause_status,
            "root_cause_evidence": selected("root_cause_evidence", [])
            if isinstance(selected("root_cause_evidence", []), list) else [],
            "repair_run_id": _text(selected("repair_run_id"), 160),
            "repair_team_id": _text(selected("repair_team_id"), 160),
            "machine_run": selected("machine_run", {}) if isinstance(selected("machine_run", {}), dict) else {},
            "auto_reverify": bool(selected("auto_reverify", True)),
            "continuous_reverify": bool(selected("continuous_reverify", True)),
            "max_auto_cycles": max_auto_cycles,
            "note": note, "actor": actor,
            "created_at": float(previous.get("created_at") or now), "updated_at": now,
            "history": history, "workflow_history": workflow_history,
            "version_history": list(previous.get("version_history") or [])[-29:],
        }
        gate = product_gate(row)
        row["quality_assessment"] = gate["quality_assessment"]
        row["product_gate"] = gate
        row["product_manager_decision"] = product_manager_decision(row, gate)
        if upgrade_status in {"released", "observing"} and target_version:
            version_history = list(row["version_history"])
            if not version_history or version_history[-1].get("version") != target_version:
                version_history.append({
                    "version": target_version, "from_version": current_version,
                    "commit": target_commit, "from_commit": current_commit,
                    "quality_score": quality_score,
                    "effective_quality_score": row["quality_assessment"].get("score"),
                    "quality_confidence": row["quality_assessment"].get("confidence"),
                    "evidence_ids": row["evidence_ids"], "evidence_run_id": row["evidence_run_id"],
                    "upgrade_description": row["upgrade_description"], "user_value": row["user_value"],
                    "success_metric": row["success_metric"], "release_scope": row["release_scope"],
                    "rollout_plan": row["rollout_plan"], "rollback_version": row["rollback_version"],
                    "released_at": now, "actor": actor,
                })
            row["version_history"] = version_history[-30:]
        rows[target] = row
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return {"ok": True, "review": row}


def batch_update(targets: list[str], *, actor: str = "local-reviewer", **fields) -> dict:
    targets = list(dict.fromkeys(str(item or "").strip() for item in targets if str(item or "").strip()))[:100]
    results = []
    for target in targets:
        previous = get(target)
        results.append(save(target, str(fields.get("verdict") or previous.get("verdict") or "pending"),
                            str(fields.get("note") or previous.get("note") or ""), actor,
                            metadata=fields))
    return {"ok": bool(results) and all(row.get("ok") for row in results),
            "updated": sum(row.get("ok") is True for row in results), "results": results}


def record_machine_result(target: str, execution: dict, *, actor: str = "quality-executor") -> dict:
    previous = get(target)
    evidence_rows = execution.get("evidence") if isinstance(execution.get("evidence"), list) else []
    evidence_ids = [str((item.get("evidence") or {}).get("evidence_id") or "") for item in evidence_rows]
    evidence_paths = [str(item.get("path") or "") for item in evidence_rows]
    comparisons = {str(item.get("level") or ""): item.get("comparison") or {} for item in evidence_rows}
    required_levels = list(previous.get("required_levels") or execution.get("levels") or [])
    full_scope = set(required_levels) <= set(execution.get("levels") or [])
    machine_status = "pass" if execution.get("ok") and full_scope else "partial" if execution.get("ok") else "issue"
    next_status = str(previous.get("upgrade_status") or "not_planned")
    if machine_status == "pass" and previous.get("verdict") == "pass" and next_status in {"in_progress", "blocked"}:
        next_status = "ready"
    elif machine_status == "issue" and next_status != "rolled_back":
        next_status = "blocked"
    return save(
        target, str(previous.get("verdict") or "pending"), str(previous.get("note") or ""), actor,
        metadata={"machine_status": machine_status,
                  "machine_summary": (
                      f"全部必需等级通过 · 执行 {len(execution.get('executed_levels') or [])} 层 · "
                      f"复用 {len(execution.get('reused_levels') or [])} 层"
                      if machine_status == "pass" else
                      f"快速层通过，仍待 {len(set(required_levels) - set(execution.get('levels') or []))} 个发布层"
                      if machine_status == "partial" else
                      "；".join(item for row in execution.get("failures") or []
                               for item in row.get("blockers") or [])),
                  "evidence_run_id": execution.get("run_id"), "evidence_ids": evidence_ids,
                  "evidence_paths": evidence_paths,
                  "required_evidence": [f"quality-{level}" for level in required_levels],
                  "required_levels": required_levels,
                  "require_machine_gate": True, "require_lifecycle_gate": True,
                  "machine_comparison": comparisons, "upgrade_status": next_status,
                  "target_commit": execution.get("commit") or previous.get("target_commit") or "",
                  "machine_run": {"run_id": execution.get("run_id"),
                                  "commit": execution.get("commit") or "",
                                  "levels": execution.get("levels") or [],
                                  "executed_levels": execution.get("executed_levels") or [],
                                  "reused_levels": execution.get("reused_levels") or [],
                                  "saved_seconds": execution.get("saved_seconds") or 0,
                                  "elapsed_s": execution.get("elapsed_s") or 0,
                                  "started_at": execution.get("started_at"),
                                  "finished_at": execution.get("finished_at")},
                  })


def record_repair(target: str, *, run_id: str, team_id: str = "", status: str = "in_progress",
                  summary: str = "", actor: str = "verification-center") -> dict:
    previous = get(target)
    return save(target, str(previous.get("verdict") or "pending"), str(previous.get("note") or ""), actor,
                metadata={"repair_run_id": run_id, "repair_team_id": team_id,
                          "upgrade_status": status,
                          "machine_status": "running" if status == "in_progress" else "issue",
                          "machine_summary": summary,
                          "next_action": summary or previous.get("next_action") or "等待军团修复完成后复验"})


def work_queue(machine_items: list[dict] | None = None) -> dict:
    """把评审阻塞和机器红项变成可解释待办；只整理事实，不替模型写修法。"""
    priority_weight = {"P0": 400, "P1": 300, "P2": 200, "P3": 100}
    rows, linked_evidence, tracked_targets = [], set(), set()
    now = time.time()
    for review in load().values():
        tracked_targets.add(str(review.get("target") or ""))
        gate = product_gate(review)
        linked_evidence.update(_string_list(review.get("evidence_ids")))
        status = str(review.get("upgrade_status") or "not_planned")
        if gate["release_ready"] and status in {"released", "observing", "validated"}:
            continue
        overdue = bool(review.get("due_at") and float(review["due_at"]) < now
                       and status not in {"released", "observing", "rolled_back"})
        score = priority_weight.get(str(review.get("priority") or "P2"), 200)
        score += 120 if review.get("verdict") == "issue" else 0
        score += 100 if status == "blocked" else 0
        score += 80 if overdue else 0
        score += 40 if review.get("machine_status") == "issue" else 0
        decision = product_manager_decision(review, gate)
        rows.append({"id": "review:" + review["target"], "kind": "review", "target": review["target"],
                     "title": review.get("feature") or review["target"], "priority": review.get("priority") or "P2",
                     "owner": review.get("owner") or "", "due_at": review.get("due_at"), "overdue": overdue,
                     "status": status, "machine_status": review.get("machine_status") or "not_run",
                     "score": score, "blockers": gate["blockers"],
                     "next_action": decision["label"], "decision": decision,
                     "evidence_run_id": review.get("evidence_run_id") or "", "review": review})
    seen_machine = set()
    for item in machine_items or []:
        if str(item.get("status") or "") not in {"issue", "missing"}:
            continue
        if str(item.get("id") or "") in linked_evidence:
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        subject = details.get("subject") if isinstance(details.get("subject"), dict) else {}
        target = str(subject.get("target") or f"evidence:{item.get('id')}")
        if target in tracked_targets:
            continue
        machine_key = (target, str(item.get("kind") or ""), str(subject.get("id") or ""))
        if machine_key in seen_machine:
            continue
        seen_machine.add(machine_key)
        status = str(item.get("status") or "issue")
        rows.append({"id": "machine:" + str(item.get("id") or ""), "kind": "machine",
                     "target": target, "title": item.get("title") or item.get("kind") or "机器证据",
                     "priority": "P1" if status == "issue" else "P2", "owner": "", "due_at": None,
                     "overdue": False, "status": status, "machine_status": status,
                     "score": 360 if status == "issue" else 250,
                     "blockers": list(details.get("blockers") or []) or [item.get("summary") or "机器证据需要处理"],
                     "next_action": "打开机器证据，确认影响范围并发起修复或复验",
                     "evidence_id": item.get("id"), "evidence_run_id": str(details.get("run_id") or "")})
    rows.sort(key=lambda row: (-int(row.get("score") or 0), float(row.get("due_at") or 1e20), row["title"]))
    return {"ok": True, "items": rows, "total": len(rows),
            "counts": {"overdue": sum(row["overdue"] for row in rows),
                       "unassigned": sum(not row.get("owner") for row in rows),
                       "machine_issues": sum(row["kind"] == "machine" for row in rows)}}


def release_notes() -> dict:
    groups = {name: [] for name in ("ready", "released", "in_progress", "blocked", "planned", "observing", "validated", "rolled_back")}
    for row in load().values():
        status = str(row.get("upgrade_status") or "not_planned")
        if status in groups:
            groups[status].append(row)
    for values in groups.values():
        values.sort(key=lambda row: (row.get("priority") or "P2", row.get("feature") or row.get("target")))
    lines = ["# 产品质量与版本升级", ""]
    labels = {"ready": "待发布", "released": "已发布", "observing": "发布后观察", "validated": "价值已证实",
              "in_progress": "进行中", "blocked": "阻塞", "planned": "已规划", "rolled_back": "已回滚"}
    for status in ("validated", "ready", "released", "observing", "in_progress", "blocked", "rolled_back", "planned"):
        if not groups[status]:
            continue
        lines.extend([f"## {labels[status]}", ""])
        for row in groups[status]:
            version = f"{row.get('current_version') or '?'} → {row.get('target_version') or '?'}"
            assessment = quality_assessment(row)
            score = assessment.get("score")
            delta = assessment.get("version_delta", {}).get("delta")
            proof = {"proven": "价值已证实", "supported": "机器证据支持", "hypothesis": "价值待观察"}.get(
                assessment.get("value_proof"), "价值待观察")
            lines.append(f"- **{row.get('feature') or row['target']}** · {row.get('priority') or 'P2'} · "
                         f"{version} · {score if score is not None else '未评分'}分"
                         f"（置信度{assessment.get('confidence_label')}"
                         f"{f' · 较基线 {delta:+g}' if isinstance(delta, (int, float)) else ''}）· {proof}"
                         f" — {row.get('upgrade_description') or row.get('next_action') or '待补说明'}")
        lines.append("")
    return {"ok": True, "groups": groups, "markdown": "\n".join(lines).rstrip() + "\n",
            "generated_at": time.time()}
