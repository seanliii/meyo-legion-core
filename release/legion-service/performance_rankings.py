#!/usr/bin/env python3
"""按 Harness × 实际模型 × 场景 × 执行深度聚合可审计性能证据。"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Iterable


def aggregate(samples: Iterable[dict], *, min_samples: int = 3) -> dict:
    groups = defaultdict(list)
    rejected = []
    for sample in samples:
        harness = str(sample.get("harness") or "")
        effective = str(sample.get("effective_model") or "")
        scene = str(sample.get("scene") or "unknown")
        depth = str(sample.get("execution_depth") or sample.get("depth") or "standard")
        wall = sample.get("wall_s")
        if not harness or not effective or not isinstance(wall, (int, float)) or wall <= 0:
            rejected.append({"reason": "missing_effective_provenance_or_wall", "sample": sample})
            continue
        groups[(scene, harness, effective, depth)].append(sample)
    rows = []
    for (scene, harness, effective, depth), values in groups.items():
        walls = sorted(float(value["wall_s"]) for value in values)
        good = sum(bool(value.get("ok")) for value in values)
        quality = [float(value["quality"]) for value in values if value.get("quality") is not None]
        row = {"scene": scene, "harness": harness, "effective_model": effective,
               "execution_depth": depth,
               "n": len(values), "good": good, "success_rate": round(good / len(values), 4),
               "p50_s": round(statistics.median(walls), 3),
               "p95_s": round(walls[min(len(walls) - 1, __import__('math').ceil(len(walls) * .95) - 1)], 3),
               "quality_avg": round(sum(quality) / len(quality), 2) if quality else None,
               "eligible": len(values) >= min_samples}
        rows.append(row)
    rows.sort(key=lambda row: (row["scene"], not row["eligible"], -row["success_rate"],
                               -(row["quality_avg"] or 0), row["p95_s"]))
    return {"rows": rows, "rejected": rejected, "min_samples": min_samples}


def recommend(samples: Iterable[dict], scene: str, *, min_samples: int = 3) -> dict:
    report = aggregate(samples, min_samples=min_samples)
    candidates = [row for row in report["rows"] if row["scene"] == scene and row["eligible"]]
    winner = candidates[0] if candidates else None
    return {"scene": scene, "winner": winner, "candidates": candidates,
            "reason": "verified_evidence" if winner else "insufficient_samples",
            "rejected": report["rejected"]}
