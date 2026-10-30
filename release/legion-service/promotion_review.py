#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外聘家转正评估(用户 2026-09-02 定:固定化机制·不是自测自己申请·系统自动评估).

背景:外聘(external agent)与内置(core harness)的区别只在接入方式——专门 adapter(90-140行)
vs 通用 transport(JSON声明)。转正=值得写专门 adapter 挖出通用层够不着的独门能力。

判据(数据驱动·三层,复用现有账本·不新造数据源):
  ①战绩层(model_health):总成功 ≥ MIN_GOOD 且成功率 ≥ MIN_RATE —— 它是主力
  ②场景层(harness_performance):有多场景/多模式真跑样本 —— 不是只会一招
  ③能力层(catalog 声明 vs 通用 transport 实际用到的):provided_tools 里声明了但
    通用 CLI transport 只用到 shell/read/write 的 = 有独门能力可挖

输出:转正评估报告(谁够格·差什么·独门能力清单)·不自动写代码(context not control:
转正=写专门 adapter 是架构动作,报告给人/军团决策,不偷偷生成)。

用法:
  python3.12 promotion_review.py            # 全量评估
  python3.12 promotion_review.py ironclaw   # 单家深评
"""
from __future__ import annotations
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent

# 转正门槛(可调·写死在模块顶部显式可见)
MIN_GOOD = 30          # model_health 总成功数下限(主力证明)
MIN_SUCCESS_RATE = 0.70  # 成功率下限
MIN_MODE_SAMPLES = 8   # harness_performance 至少 8 个多模式样本(不是只会 single)

# 通用 transport 实际透传的基本盘(声明了超出这些的=有独门能力可挖)
_GENERIC_BASE = {"shell", "read", "write", "search", "mcp"}


def _model_health() -> dict:
    import legion_paths
    p = pathlib.Path(legion_paths.home_under("model_health.json"))
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _perf_ledger() -> dict:
    try:
        import harness_performance
        return harness_performance._load()
    except Exception:
        return {}


def _catalog() -> list[dict]:
    try:
        import external_agent_registry as ER
        return ER.load_catalog()
    except Exception:
        return []


def review(key: str = "") -> dict:
    """全量或单家转正评估。纯读账本·零副作用·不自动改任何东西。"""
    mh = _model_health()
    perf = _perf_ledger()
    catalog = {a["key"]: a for a in _catalog() if a.get("key")}
    core = {"auto", "cc", "codex", "deepagents", "deepseek",
            "hermes", "jcode", "omnigent", "penguin", "prime"}
    rows = []
    keys = [key] if key else sorted(set(mh) | set(perf))
    for k in keys:
        if k in core or k not in catalog:
            continue  # 只评外聘
        # ①战绩层
        models = mh.get(k) or {}
        good = sum(int(v.get("good", 0)) for v in models.values() if isinstance(v, dict))
        bad = sum(int(v.get("bad", 0)) for v in models.values() if isinstance(v, dict))
        rate = good / (good + bad) if (good + bad) else 0.0
        # ②场景层
        perf_rows = perf.get(k) or {}
        mode_samples = sum(int(m.get("good", 0)) + int(m.get("bad", 0))
                           for scene in perf_rows.values() for m in scene.values())
        scenes = sorted(scene for scene in perf_rows
                        if any(m.get("good", 0) + m.get("bad", 0) for m in perf_rows[scene].values()))
        # ③能力层:声明 provided_tools 超出通用基本盘的部分
        declared = set((catalog[k].get("provided_tools") or []))
        unique = sorted(declared - _GENERIC_BASE - {"web_fetch", "subagent", "acp", "session"})
        verdict = "eligible" if (good >= MIN_GOOD and rate >= MIN_SUCCESS_RATE
                                 and mode_samples >= MIN_MODE_SAMPLES) else \
                  ("watch" if good >= MIN_GOOD * 0.5 else "early")
        gaps = []
        if good < MIN_GOOD: gaps.append(f"战绩 {good}/{MIN_GOOD}")
        if rate < MIN_SUCCESS_RATE: gaps.append(f"成功率 {rate:.0%}/{MIN_SUCCESS_RATE:.0%}")
        if mode_samples < MIN_MODE_SAMPLES: gaps.append(f"多模式样本 {mode_samples}/{MIN_MODE_SAMPLES}")
        rows.append({
            "key": k, "good": good, "bad": bad, "success_rate": round(rate, 3),
            "mode_samples": mode_samples, "scenes": scenes,
            "unique_tools": unique, "verdict": verdict,
            "promotion_gaps": gaps,
        })
    rows.sort(key=lambda r: (-r["good"], -r["success_rate"]))
    return {"thresholds": {"min_good": MIN_GOOD, "min_rate": MIN_SUCCESS_RATE,
                           "min_mode_samples": MIN_MODE_SAMPLES},
            "eligible": [r for r in rows if r["verdict"] == "eligible"],
            "watch": [r for r in rows if r["verdict"] == "watch"],
            "all": rows}


def render(report: dict | None = None) -> str:
    r = report or review()
    lines = ["=== 外聘转正评估(数据驱动·自动) ===",
             f"门槛: 成功≥{r['thresholds']['min_good']} · 成功率≥{r['thresholds']['min_rate']:.0%} · 多模式样本≥{r['thresholds']['min_mode_samples']}",
             ""]
    if not r["eligible"]:
        lines.append("(暂无够格家)")
    for row in r["eligible"]:
        lines.append(f"✅ {row['key']:14s} 成功{row['good']}/失败{row['bad']} 成功率{row['success_rate']:.0%} "
                     f"多模式{row['mode_samples']}样本 场景{row['scenes']}")
        if row["unique_tools"]:
            lines.append(f"   独门能力可挖: {','.join(row['unique_tools'])}")
    lines.append("")
    lines.append("—— 观察区(差一点) ——")
    for row in r["watch"]:
        lines.append(f"👀 {row['key']:14s} 差: {'; '.join(row['promotion_gaps']) or '只差评估'}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    key = sys.argv[1] if len(sys.argv) > 1 else ""
    print(render(review(key) if key else None))
