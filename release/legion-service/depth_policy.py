#!/usr/bin/env python3
"""基于场景实测证据选择执行深度；小样本保持 manifest 默认。"""
from __future__ import annotations

from typing import Iterable


def recommend_depth(samples: Iterable[dict], harness: str, scene: str,
                    supported: list[str], *, default: str = "standard",
                    min_samples: int = 3) -> dict:
    import performance_rankings
    supported = [str(value) for value in supported if str(value)] or ["standard"]
    report = performance_rankings.aggregate(samples, min_samples=min_samples)
    rows = [row for row in report["rows"]
            if row["scene"] == scene and row["harness"] == harness
            and row["execution_depth"] in supported and row["eligible"]]
    if not rows:
        chosen = default if default in supported else supported[0]
        return {"depth": chosen, "reason": "insufficient_samples", "evidence": []}
    rows.sort(key=lambda row: (-row["success_rate"], -(row["quality_avg"] or 0), row["p95_s"]))
    return {"depth": rows[0]["execution_depth"], "reason": "verified_scene_depth",
            "evidence": rows}
