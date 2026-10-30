#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""家×模型兼容矩阵:让模型选择器【首轮就选对】,不靠先撞 400/残片再学。

单一事实源,两类数据合成:
  1. 声明式静态表(真跑钉死的不兼容·改代码才变):
     - reasoning 模型 × function-tools+chat-completions 家 → 400/剥残片
  2. 账本驱动(model_health.json·数据长出来):
     - 某家×某模型连续连败(阈值 N 且 zero good)→ 该家排除该模型
       (不是全局 broken——同模型对别家可能完全正常·per-harness 判定)

消费点:legion_core.select_usable_model(候选过滤)+ run_one(_base_exclude 合成)。
守边界:这是"把已知不兼容如实告诉选择器"(context·首轮就选对),不是替模型判断答案。
所有排除都是 per-harness 的,池耗尽时仍可被穷尽选到(hard 排除只留给静态表——
那是结构性不兼容,重试也不变)。
"""
from __future__ import annotations

import os

# ── 声明式静态表(结构性不兼容·真跑钉死) ─────────────────────────────
# gpt-5.6 系是 reasoning 模型。带 function tools 走网关 chat completions 时:
# cline 400 "Function tools with reasoning_effort not supported"、goose/openclaw
# reasoning 输出被剥成残片。DeepSeek 已在 serializer 中保留强模型和工具、仅省略冲突参数。
# 的家首轮就该避开。新家撞到同类 400 时在此登记,一家修复全家防。
REASONING_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
FUNCTION_TOOLS_HARNESSES = {"cline", "goose", "openclaw"}

# 家 → {该家结构性跑不了的模型集合}。静态表=hard 排除(重试也不变的结构病)。
# max_tokens 类(deepseek×GLM)已在桥层根修(70c1b69a·按模型上限收敛),不进此表。
STATIC_INCOMPATIBLE: dict[str, set[str]] = {
    k: set(REASONING_MODELS) for k in FUNCTION_TOOLS_HARNESSES
}

# ── 账本驱动(数据长出来的不兼容) ────────────────────────────────────
# 调用时解析(非 import 时固化)：pytest 收集阶段多个测试文件各自设不同 env，
# 固化路径会读到别的测试留下的账本(2026-09-07 全量失败根因)。env 缺省仍指 ~/.legion。
_HEALTH_ENV = "LEGION_MODEL_HEALTH"
_HEALTH_DEFAULT = os.path.expanduser("~/.legion/model_health.json")
# 连败阈值:连续 N 次失败且零成功 → 该家排除该模型(账本有救活记录即恢复·非永久禁)。
_LEDGER_BAD_STREAK = int(os.environ.get("LEGION_COMPAT_BAD_STREAK", "5"))


def _health_path() -> str:
    return os.environ.get(_HEALTH_ENV, _HEALTH_DEFAULT)


def _load_health() -> dict:
    try:
        import json
        with open(_health_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ledger_incompatible(harness_key: str) -> set[str]:
    """该家在账本上连败零胜的模型(软排除:池耗尽仍可选·给翻身机会按 health_rank 兜底排序)。"""
    if not harness_key:
        return set()
    rec = _load_health().get(harness_key) or {}
    out = set()
    for model, v in rec.items():
        if not isinstance(v, dict):
            continue
        good, bad = int(v.get("good") or 0), int(v.get("bad") or 0)
        if good == 0 and bad >= _LEDGER_BAD_STREAK:
            out.add(model)
    return out


def incompatible_models(harness_key: str) -> set[str]:
    """该家当下不该选的模型 = 静态表(hard) ∪ 账本连败(soft)。"""
    return set(STATIC_INCOMPATIBLE.get(harness_key, set())) | ledger_incompatible(harness_key)


def is_static_incompatible(harness_key: str, model: str) -> bool:
    """结构性不兼容(重试也不变)——供硬过滤(不进候选)。"""
    return model in STATIC_INCOMPATIBLE.get(harness_key, set())
