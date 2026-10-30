#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首脑适任度(captain aptitude):用竞技场控制变量实验的真数据判断【谁适合当首脑】.

背景(用户 2026-08-30 定):控制变量实验证明【首脑是决定群体效果的关键变量】——
同任务+同军团+仅换首脑,群体分极差可达53分(jcode89/deepseek82/auto36)。
所以总指挥选首脑不该硬编码"快慢顺序",而应按【测试数据里的首脑适任度】排序:
  ①按任务方向默认选历史最强首脑 ②过程不行再交接(handoff_captain 已有)。

数据源:dev/collective-arena-runs/*/blind_scores.json + captain_task.json(竞技场真跑盲评)。
适任度 = 该首脑当总集成者时的历史群体均分(越高越适合当首脑)。
无数据的家给中性默认分(不惩罚未测家·让它有机会被选并积累数据)。
"""
from __future__ import annotations
import json
import os
import statistics
from pathlib import Path

# 竞技场 run 目录(相对仓库);读盘失败则回退空表→用中性默认.
_RUNS = Path(__file__).resolve().parents[2] / "dev" / "collective-arena-runs"
_NEUTRAL = 60.0  # 无测试数据的家的中性适任度(不高不低·给机会积累数据)
# 已知弱首脑硬下限保护位:实验证实 auto 当首脑组织/收口能力弱(36分),但不写死排除,
# 仅让它在无更多数据时排在有数据的强首脑之后。纯数据驱动·不做关键词/身份歧视.


def _load_run(d: Path) -> dict | None:
    try:
        ct = json.loads((d / "captain_task.json").read_text(encoding="utf-8"))
        bs = json.loads((d / "blind_scores.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if bs.get("collective_avg") is None:
        return None
    return {"captain": ct.get("captain"), "task": ct.get("task", ""),
            "score": float(bs["collective_avg"])}


def aptitude_table() -> dict:
    """扫全部竞技场 run·聚合每个首脑的历史群体均分=适任度. {captain: {'score':x,'n':k}}"""
    if not _RUNS.exists():
        return {}
    by_cap: dict[str, list[float]] = {}
    for d in _RUNS.iterdir():
        if not d.is_dir():
            continue
        r = _load_run(d)
        if r and r["captain"]:
            by_cap.setdefault(r["captain"], []).append(r["score"])
    return {k: {"score": round(statistics.mean(v), 1), "n": len(v)} for k, v in by_cap.items()}


def rank_candidates(candidates: list[str]) -> list[str]:
    """按首脑适任度对候选排序(高分在前);无数据的家用中性默认分插在中间.

    这是"按测试数据判断谁适合当首脑"的落地:总指挥默认先让适任度高的家竞选/当首脑.
    环境变量 LEGION_APTITUDE_OFF=1 可关闭(退回原顺序·出问题一键回退).
    """
    if os.environ.get("LEGION_APTITUDE_OFF") == "1":
        return list(candidates)
    tbl = aptitude_table()

    def _key(cap: str) -> float:
        info = tbl.get(cap)
        return info["score"] if info else _NEUTRAL
    # 稳定排序:适任度降序;同分保持原顺序(原顺序已按快慢优化过)
    return sorted(candidates, key=lambda c: -_key(c))


def untested_captains(all_keys: list[str]) -> list[str]:
    """找出还没有首脑测评数据的家(新来的agent)——需自动测评后才知排序.

    用户2026-08-30目标2:以后新来的agent都测一下·判断能否进入+调整排序。
    有数据的家用历史适任度排;无数据的新家先测评(跑标准任务当首脑)再定位。
    """
    tbl = aptitude_table()
    return [k for k in all_keys if k not in tbl]


def record_aptitude(captain: str, score: float, task: str = "", direction: str = "unknown",
                    mode: str = "orchestrate") -> dict:
    """把真实盲评写入统一 harness×scene×mode 账本，下一次排序立即消费。"""
    import harness_performance
    return harness_performance.record(captain, direction or "unknown", mode,
                                      ok=float(score) > 0, quality=float(score),
                                      source="collective_arena")


def by_direction(candidates: list[str], direction: str = "") -> list[str]:
    """按场景真实质量/稳定/延迟排序；样本不足时保留全局适任度排序。"""
    base = rank_candidates(candidates)
    try:
        import harness_performance
        return harness_performance.rank(base, direction or "unknown", "orchestrate")
    except Exception:
        return base


if __name__ == "__main__":
    import sys
    t = aptitude_table()
    print("=== 首脑适任度表(竞技场真数据) ===")
    for cap, info in sorted(t.items(), key=lambda x: -x[1]["score"]):
        print(f"  {cap:12} 适任度 {info['score']:5}  (样本 {info['n']})")
    if len(sys.argv) > 1:
        cands = sys.argv[1].split(",")
        print("\n排序后候选:", rank_candidates(cands))
