#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团性能/质量账本：按 harness × scene × mode 聚合真实运行证据。"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path

import legion_paths as _paths

# ── 账本文件路径 ──────────────────────────────────────────────────
# 用函数而非 import 时固化的常量：测试(pytest 收集阶段 import 全部测试文件·各自设不同
# MEYO_LEGION_DIR/LEGION_HOME_DIR)与生产热改 env 时，固化常量会读到别的测试留下的账本
# (2026-09-07 全量15失败根因之一)。legion_paths 本身就是调用时读 env 的单一权威。
_HP_NAME = "harness_performance.json"
_COLLAB_NAME = "collaboration_performance.json"
_DEPTH_NAME = "execution_depth_performance.json"
_ROUTE_POLICY_NAME = "execution_route_policy.json"


def _hp_path() -> Path:
    return Path(_paths.under(_HP_NAME))


def _collab_path() -> Path:
    return Path(_paths.under(_COLLAB_NAME))


def _depth_path() -> Path:
    return Path(_paths.under(_DEPTH_NAME))


def _route_policy_path() -> Path:
    return Path(_paths.under(_ROUTE_POLICY_NAME))

_LOCK = threading.RLock()


def _wilson(good: int, bad: int) -> float:
    n = good + bad
    if not n:
        return 0.0
    p, z = good / n, 1.96
    return (p + z*z/(2*n) - z*math.sqrt((p*(1-p)+z*z/(4*n))/n)) / (1+z*z/n)


def _load() -> dict:
    try:
        value = json.loads(_hp_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_path(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush(); os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _save(value: dict) -> None:
    _save_path(_hp_path(), value)


def record(harness: str, scene: str, mode: str, *, ok: bool, wall_s: float = 0,
           quality: float | None = None, artifacts: int = 0, source: str = "runtime") -> dict:
    key, scene, mode = str(harness), str(scene or "unknown"), str(mode or "single")
    with _LOCK:
        data = _load()
        row = data.setdefault(key, {}).setdefault(scene, {}).setdefault(mode, {
            "good": 0, "bad": 0, "walls": [], "qualities": [], "artifacts": 0, "sources": {}})
        row["good" if ok else "bad"] += 1
        if wall_s > 0: row["walls"] = (row.get("walls") or [])[-99:] + [round(float(wall_s), 3)]
        if quality is not None: row["qualities"] = (row.get("qualities") or [])[-99:] + [float(quality)]
        row["artifacts"] = int(row.get("artifacts") or 0) + int(artifacts or 0)
        row.setdefault("sources", {})[source] = int(row.get("sources", {}).get(source, 0)) + 1
        row["updated_at"] = time.time()
        _save(data)
    return summarize_row(row)


def summarize_row(row: dict) -> dict:
    walls = sorted(float(x) for x in row.get("walls") or [])
    qualities = [float(x) for x in row.get("qualities") or []]
    n = int(row.get("good", 0)) + int(row.get("bad", 0))
    def pct(p):
        return walls[min(len(walls)-1, int((len(walls)-1)*p))] if walls else None
    return {"n": n, "good": int(row.get("good", 0)), "bad": int(row.get("bad", 0)),
            "success_lb": round(_wilson(int(row.get("good", 0)), int(row.get("bad", 0))), 4),
            "p50_s": pct(.5), "p95_s": pct(.95),
            "quality_avg": round(sum(qualities)/len(qualities), 2) if qualities else None,
            "quality_n": len(qualities), "artifacts": int(row.get("artifacts") or 0),
            "updated_at": row.get("updated_at")}


def table(scene: str = "", mode: str = "") -> dict:
    data, rows = _load(), []
    for harness, scenes in data.items():
        for sc, modes in scenes.items():
            if scene and sc != scene: continue
            for md, raw in modes.items():
                if mode and md != mode: continue
                rows.append({"harness": harness, "scene": sc, "mode": md, **summarize_row(raw)})
    rows.sort(key=lambda r: (-(r["quality_avg"] if r["quality_avg"] is not None else -1),
                             -r["success_lb"], r["p95_s"] if r["p95_s"] is not None else 1e9))
    return {"rows": rows, "count": len(rows), "path": str(_hp_path())}


def rank(candidates: list[str], scene: str, mode: str = "single") -> list[str]:
    data = _load()
    def score(key):
        row = (((data.get(key) or {}).get(scene) or {}).get(mode) or {})
        s = summarize_row(row)
        n = s["n"]
        # 小样本保持中性探索；有数据后质量优先、稳定其次、延迟第三。
        if n < 3: return (0.5, 0.0, 0.0)
        quality = (s["quality_avg"] or 0) / 100
        speed = 1 / (1 + (s["p95_s"] or 999))
        return (quality, s["success_lb"], speed)
    order = {key: i for i, key in enumerate(candidates)}
    return sorted(candidates, key=lambda key: (*(-x for x in score(key)), order[key]))


def strengths(harness: str, min_n: int = 3) -> list[dict]:
    rows = [r for r in table()["rows"] if r["harness"] == harness and r["n"] >= min_n]
    rows.sort(key=lambda r: (-(r["quality_avg"] or 0), -r["success_lb"], r["p95_s"] or 1e9))
    return rows[:5]


def record_depth(harness: str, scene: str, effective_model: str, execution_depth: str,
                 *, ok: bool, wall_s: float, quality: float | None = None,
                 source: str = "runtime") -> dict:
    sample = {"harness": str(harness), "scene": str(scene or "unknown"),
              "effective_model": str(effective_model or "unknown"),
              "execution_depth": str(execution_depth or "standard"),
              "ok": bool(ok), "wall_s": max(0.0, float(wall_s or 0)),
              "quality": quality, "source": str(source or "runtime"), "ts": time.time()}
    with _LOCK:
        try:
            data = json.loads(_depth_path().read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {"version": 1, "samples": []}
        except Exception:
            data = {"version": 1, "samples": []}
        data["samples"] = (list(data.get("samples") or []) + [sample])[-5000:]
        _save_path(_depth_path(), data)
    return sample


def depth_samples(harness: str = "", scene: str = "") -> list[dict]:
    try:
        data = json.loads(_depth_path().read_text(encoding="utf-8"))
    except Exception:
        return []
    return [sample for sample in (data.get("samples") or []) if isinstance(sample, dict)
            and (not harness or sample.get("harness") == harness)
            and (not scene or sample.get("scene") == scene)]


def recommend_depth(harness: str, scene: str, supported: list[str],
                    default: str = "standard", min_samples: int = 3) -> dict:
    import depth_policy
    return depth_policy.recommend_depth(depth_samples(harness, scene), harness, scene, supported,
                                        default=default, min_samples=min_samples)


def _production_direct_model() -> str:
    try:
        import model_registry
        return str((model_registry.scene_models("fast") or [""])[0])
    except Exception:
        return "gpt-5.6-sol"


def route_policy(scene: str = "fast") -> dict:
    try:
        data = json.loads(_route_policy_path().read_text(encoding="utf-8"))
    except Exception:
        data = {}
    row = (data.get("scenes") or {}).get(str(scene)) if isinstance(data, dict) else None
    if not isinstance(row, dict):
        return {"scene": str(scene), "route": "strongest_single", "verified": False,
                "reason": "no_verified_route_evidence"}
    result = dict(row)
    if result.get("route") == "direct_model":
        model = str(result.get("model") or "")
        expected = _production_direct_model()
        if not model or model != expected:
            return {"scene": str(scene), "route": "strongest_single", "verified": False,
                    "reason": "direct_model_policy_stale_or_non_primary",
                    "policy_model": model, "required_model": expected}
    return result


def record_route_policy(scene: str, decision: dict, *, evidence_path: str = "",
                        minimum_cases: int = 3) -> dict:
    """只把多案例、质量不退化的 A/B 结论写成可逆路由策略。"""
    row = dict(decision or {})
    cases = max(0, int(row.get("cases") or 0))
    route = str(row.get("route") or "")
    if row.get("verified") is not True or cases < max(1, int(minimum_cases)) \
            or route not in {"direct_model", "strongest_single", "dynamic_legion"}:
        return {"ok": False, "applied": False, "reason": "insufficient_verified_route_evidence",
                "required_cases": max(1, int(minimum_cases)), "observed_cases": cases}
    with _LOCK:
        try:
            data = json.loads(_route_policy_path().read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except Exception:
            data = {}
        if route == "direct_model" and str(row.get("model") or "") != _production_direct_model():
            return {"ok": False, "applied": False, "reason": "direct_model_requires_primary_model",
                    "required_model": _production_direct_model(), "observed_model": str(row.get("model") or ""),
                    "required_cases": max(1, int(minimum_cases)), "observed_cases": cases}
        stored = {"scene": str(scene), "route": route, "verified": True, "cases": cases,
                  "model": str(row.get("model") or ""),
                  "quality_basis": str(row.get("quality_basis") or ""),
                  "reason": str(row.get("reason") or ""), "evidence_path": str(evidence_path or ""),
                  "updated_at": time.time()}
        data.setdefault("scenes", {})[str(scene)] = stored
        data["version"] = 1
        _save_path(_route_policy_path(), data)
    return {"ok": True, "applied": True, "policy": stored, "path": str(_route_policy_path())}


def _load_collaboration() -> dict:
    try:
        value = json.loads(_collab_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"version": 1, "runs": []}
    except Exception:
        return {"version": 1, "runs": []}


def record_collaboration(scene: str, mode: str, team_size: int, *, completed: bool,
                         wall_s: float = 0, quality: float | None = None,
                         contract_score: float | None = None, attempts: int = 0,
                         failovers: int = 0, artifacts: int = 0,
                         pattern: str = "", verification: bool = False,
                         source: str = "runtime") -> dict:
    """记录整单协作收益；质量只接受独立评审分，契约分单列不冒充质量。"""
    row = {
        "scene": str(scene or "unknown"), "mode": str(mode or "single"),
        "team_size": max(1, int(team_size or 1)), "completed": bool(completed),
        "wall_s": max(0.0, float(wall_s or 0)),
        "quality": None if quality is None else max(0.0, min(100.0, float(quality))),
        "contract_score": (None if contract_score is None
                           else max(0.0, min(100.0, float(contract_score)))),
        "attempts": max(0, int(attempts or 0)), "failovers": max(0, int(failovers or 0)),
        "artifacts": max(0, int(artifacts or 0)), "pattern": str(pattern or mode or "single"),
        "verification": bool(verification), "source": str(source or "runtime"), "ts": time.time(),
    }
    with _LOCK:
        data = _load_collaboration()
        data["runs"] = (list(data.get("runs") or []) + [row])[-2000:]
        _save_path(_collab_path(), data)
    return row


def collaboration_summary(scene: str = "") -> dict:
    rows = [row for row in (_load_collaboration().get("runs") or [])
            if not scene or str(row.get("scene")) == str(scene)]
    groups = {}
    for row in rows:
        bucket = "single" if int(row.get("team_size") or 1) <= 1 else "multi"
        groups.setdefault(bucket, []).append(row)
    summary = {}
    for bucket in ("single", "multi"):
        values = groups.get(bucket, [])
        walls = sorted(float(row.get("wall_s") or 0) for row in values if float(row.get("wall_s") or 0) > 0)
        scores = [float(row["quality"] if row.get("quality") is not None else row["contract_score"])
                  for row in values if row.get("quality") is not None or row.get("contract_score") is not None]
        summary[bucket] = {
            "n": len(values), "score_n": len(scores),
            "success_rate": round(sum(bool(row.get("completed")) for row in values) / len(values), 4)
                            if values else None,
            "score_avg": round(sum(scores) / len(scores), 2) if scores else None,
            "p50_s": walls[(len(walls) - 1) // 2] if walls else None,
            "avg_team_size": round(sum(int(row.get("team_size") or 1) for row in values) / len(values), 2)
                             if values else None,
            "avg_attempts": round(sum(int(row.get("attempts") or 0) for row in values) / len(values), 2)
                            if values else None,
        }
    return {"scene": scene or "all", "single": summary["single"], "multi": summary["multi"],
            "runs": len(rows), "path": str(_collab_path())}


def verified_quality(scene: str, mode: str, team_size: int) -> dict:
    """返回同场景/模式/规模的独立质量证据，供运行时扩军和验证闭环消费。"""
    rows = [row for row in (_load_collaboration().get("runs") or [])
            if str(row.get("scene")) == str(scene)
            and str(row.get("mode")) == str(mode)
            and int(row.get("team_size") or 1) == max(1, int(team_size or 1))
            and row.get("quality") is not None]
    scores = [float(row["quality"]) for row in rows]
    return {"n": len(rows), "quality_avg": round(sum(scores) / len(scores), 2) if scores else None,
            "success_rate": (round(sum(bool(row.get("completed")) for row in rows) / len(rows), 4)
                             if rows else None)}


def recommend_initial_team_size(scene: str, requested: int, *, explicit: bool = False,
                                min_samples: int = 3) -> dict:
    """多人质量增益覆盖协调税才冷启动多人；显式用户编排永远优先。"""
    requested = max(1, int(requested or 1))
    if explicit or requested <= 1:
        return {"recommended": requested, "requested": requested,
                "reason": "explicit_user_choice" if explicit else "single_requested",
                "evidence": collaboration_summary(scene)}
    evidence = collaboration_summary(scene)
    single, multi = evidence["single"], evidence["multi"]
    if single["score_n"] < min_samples or multi["score_n"] < min_samples:
        return {"recommended": 1, "requested": requested,
                "reason": "insufficient_verified_multi_gain", "evidence": evidence}
    quality_gain = float(multi["score_avg"] or 0) - float(single["score_avg"] or 0)
    single_wall = max(0.001, float(single["p50_s"] or 0.001))
    latency_ratio = float(multi["p50_s"] or single_wall) / single_wall
    coordination_tax = max(0.0, latency_ratio - 1.0) * 5.0 + max(
        0.0, float(multi["avg_team_size"] or 1) - 1.0) * 2.0
    net_gain = round(quality_gain - coordination_tax, 3)
    recommended = requested if net_gain > 0 and (multi["success_rate"] or 0) >= (single["success_rate"] or 0) else 1
    return {"recommended": recommended, "requested": requested,
            "reason": "verified_multi_net_gain" if recommended > 1 else "coordination_tax_not_covered",
            "quality_gain": round(quality_gain, 3), "coordination_tax": round(coordination_tax, 3),
            "net_gain": net_gain, "evidence": evidence}
