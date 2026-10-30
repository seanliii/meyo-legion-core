# -*- coding: utf-8 -*-
"""meyo harness 目录 —— 只报客观事实,不做任何判断 (2026-08-25 重写)

设计原则(用户铁律):**任何"派谁/难不难/要不要复核/几个agent"的判断都由模型自主**。
本模块【不】预设 cost_tier/strong_on/擅长啥/recommend 规则 —— 那是把模型当傻子、
给大脑戴镣铐。模型自己会:读各家历史 trace、读代码、跑几次磨合,自行积累对每家的
理解并决定编排。本模块只提供【客观、无评价】的事实:名字 + 怎么调。
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class MeyoHarness:
    key: str            # canonical 名(与 run_comparison.HARNESSES 一致)
    display_name: str
    cli_hint: str       # 客观:实际启动命令(便于模型/人理解怎么调),无褒贬


# harness = 能领任务、自己解题、返回答案的 agent 本体。只列客观事实:有这些家、各自怎么调。
# 不写"谁便宜/谁擅长/谁强" —— 模型自己去试/去读。
# 2026-08-28 定位纠正(用户"他们不是harness吗·挪出去"):cua/supervision/herdr/orca 不是 harness,
# 是【能力后端】(桌面操作/CV库/终端runtime/桌面并行编排CLI)——它们不领题解题,只暴露一种工具能力
# 供 harness/模型按需调用。故从 MEYO_HARNESSES 挪到 MEYO_CAPABILITIES,不再算军团成员/派单池。
MEYO_HARNESSES: dict[str, MeyoHarness] = {
    "auto":       MeyoHarness("auto", "Auto+Native(自研)", "qa_auto_agent 引擎"),
    "cc":         MeyoHarness("cc", "Claude Code", "claude -p --output-format json"),
    "codex":      MeyoHarness("codex", "Codex", "codex exec"),
    "deepseek":   MeyoHarness("deepseek", "DeepSeek Harness", "dsh headless"),
    "hermes":     MeyoHarness("hermes", "Hermes Agent", "hermes"),
    "deepagents": MeyoHarness("deepagents", "DeepAgents", "langgraph react"),
    "penguin":    MeyoHarness("penguin", "PenguinHarness", "penguin run -m"),
    "prime":      MeyoHarness("prime", "Prime Agent", "prime-agent -p --mode json"),
    "jcode":      MeyoHarness("jcode", "jcode", "jcode run --json"),
    "omnigent":   MeyoHarness("omnigent", "Omnigent(编排放大器)", "omnigent run --harness cursor/kimi/goose/qwen…一行调一堆家"),
}


# 能力后端(不是 harness·不进军团派单/接力池):暴露一种工具能力·由 harness/模型按需调用。
MEYO_CAPABILITIES: dict[str, MeyoHarness] = {
    "cua":         MeyoHarness("cua", "Cua Computer Use(能力)", "cua-mcp-server·桌面操作/截屏"),
    "supervision": MeyoHarness("supervision", "Supervision CV(能力)", "python 包·目标检测/追踪/几何"),
    "herdr":       MeyoHarness("herdr", "Herdr Terminal Runtime(能力)", "herdr api <method>·常驻终端"),
    "orca":        MeyoHarness("orca", "Orca 并行编排(能力)", "orca CLI·多agent worktree并行"),
}


def get_harness(key: str) -> MeyoHarness | None:
    return MEYO_HARNESSES.get(key)


def get_capability(key: str) -> MeyoHarness | None:
    return MEYO_CAPABILITIES.get(key)


def list_keys() -> list[str]:
    return list(MEYO_HARNESSES)


def list_capability_keys() -> list[str]:
    return list(MEYO_CAPABILITIES)
