# -*- coding: utf-8 -*-
"""置信级联执行器(生产版)—— 唯一被真实数据证明超过最强单家的架构。

策略:先跑 primary(cc);用本次执行的可观测特征(search_count/elapsed/final长度/timeout)
经预训练 logistic 失败检测器判断是否疑似失败;疑似失败才升级到 fallback(deepagents)。
40 题 5 折 OOF:17/40 → 21/40(+4,五 seed 稳定,0 回归)。

失败检测器系数来自 cascade_policy.json(在 fair_bench_40 上训练),运行时纯 python 打分,
不依赖 sklearn、不看 gold。fallback 家由 union 覆盖分析学得,非题目关键词硬编码。
"""
from __future__ import annotations
import json, math, re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_POLICY = _HERE / "cascade_policy.json"


def _load_policy() -> dict:
    return json.loads(_POLICY.read_text())


def _features(result: dict) -> list[float]:
    """从一次 run 结果里抽可观测特征(与训练一致)。"""
    final = str(result.get("final", "") or "")
    status = str(result.get("status", "") or "").lower()
    sc = result.get("search_count")
    if sc is None:
        sc = result.get("searches") or 0
    elapsed = result.get("elapsed") or 0
    return [float(sc), float(elapsed), float(len(final)),
            1.0 if "timeout" in status else 0.0]


def predict_failure(result: dict, policy: dict | None = None) -> tuple[bool, float]:
    """返回 (是否疑似失败, 概率)。纯 python logistic,无 sklearn。"""
    p = policy or _load_policy()
    x = _features(result)
    z = p["intercept"]
    for xi, mean, scale, coef in zip(x, p["scaler_mean"], p["scaler_scale"], p["coef"]):
        z += coef * ((xi - mean) / (scale or 1.0))
    prob = 1.0 / (1.0 + math.exp(-z))
    return prob >= p.get("threshold", 0.5), prob


def run_cascade(task: str, qhash: str = "adhoc", gold: str = "",
                run_fn=None, primary_result: dict | None = None) -> dict:
    """执行置信级联。run_fn(harness, task, qhash, gold)->result;默认用 orchestrator.run_harness。

    primary_result:可选,直接注入 primary 执行结果(用于 replay/回归验证真实失败样本的升级分支);
    生产不传,则真跑 primary。返回选中结果 + 级联决策路径(primary_prob / escalated / route)。
    """
    if run_fn is None:
        import orchestrator as O
        run_fn = lambda h, t, q, g: O.run_harness(h, t, qhash=q, gold=g)
    pol = _load_policy()
    primary = primary_result if primary_result is not None else run_fn(pol["primary"], task, qhash, gold)
    failed, prob = predict_failure(primary, pol)
    route = [{"harness": pol["primary"], "status": primary.get("status"),
              "fail_prob": round(prob, 3)}]
    if not failed:
        return {"final": primary.get("final", ""), "status": primary.get("status"),
                "chosen": pol["primary"], "escalated": False, "route": route,
                "primary_fail_prob": round(prob, 3)}
    fb = run_fn(pol["fallback"], task, qhash, gold)
    route.append({"harness": pol["fallback"], "status": fb.get("status")})
    return {"final": fb.get("final", ""), "status": fb.get("status"),
            "chosen": pol["fallback"], "escalated": True, "route": route,
            "primary_fail_prob": round(prob, 3)}
