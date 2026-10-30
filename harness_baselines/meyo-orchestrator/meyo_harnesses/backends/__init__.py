# -*- coding: utf-8 -*-
"""外部后端 adapter 包 (2026-08-25)

每后端一个 runner,统一签名 run(qhash, task, gold)->dict,可被 orchestrator/autoloop
当作一个可调 harness。诚实原则:CLI/包不在位时不假跑,返回 status=unavailable + 真实原因。

- cua        : Computer Use(经 MCP·截屏等只读动作)
- supervision: 视觉/CV(经 pip 包或 MCP wrapper)
- herdr      : 终端常驻 runtime(经 CLI + socket API)
- orca       : 桌面 Electron(需构建·本轮 adapter+探测·全量构建留后续)
- omnigent   : 编排放大器(用户2026-08-27"auto薄·直接用omnigent")·一成员带来
               cursor/kimi/goose/qwen 等军团没有的家+多层嵌套(军团harness它·它harness一堆家)
"""
from __future__ import annotations
from . import cua_backend, supervision_backend, herdr_backend, orca_backend, omnigent_backend

# 统一 harness pool 片段:key -> runner(qhash,task,gold)->dict
BACKEND_HARNESSES = {
    "cua": cua_backend.run,
    "supervision": supervision_backend.run,
    "herdr": herdr_backend.run,
    "orca": orca_backend.run,
    "omnigent": omnigent_backend.run,
}

BACKEND_PROBES = {
    "cua": cua_backend.probe,
    "supervision": supervision_backend.probe,
    "herdr": herdr_backend.probe,
    "orca": orca_backend.probe,
    "omnigent": omnigent_backend.probe,
}

__all__ = ["BACKEND_HARNESSES", "BACKEND_PROBES"]
