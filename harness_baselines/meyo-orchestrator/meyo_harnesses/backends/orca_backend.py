# -*- coding: utf-8 -*-
"""Orca 桌面 Electron 外壳后端 adapter。

当前仓库只 clone 了源码,CLI 未构建/不在 PATH。该 adapter 先把真实可用性探测和统一
runner 契约接进编排层;不伪装已构建。Electron 全量 build/签名/权限授权留后续专项。
"""
from __future__ import annotations
import shutil, subprocess, time
from pathlib import Path

_ORCA_SRC = Path(__file__).resolve().parents[3] / "ecosystem" / "orca"
_ORCA_CLI = _ORCA_SRC / "out" / "cli" / "index.js"  # corepack pnpm run build:cli 产物


def _cmd() -> list[str] | None:
    """返回可执行 orca 命令(argv)。优先 PATH 的 orca·否则 node 跑构建产物 out/cli。"""
    b = shutil.which("orca")
    if b:
        return [b]
    if _ORCA_CLI.exists():
        node = shutil.which("node")
        if node:
            return [node, str(_ORCA_CLI)]
    return None


def probe() -> dict:
    c = _cmd()
    return {"backend": "orca", "available": bool(c), "bin": (" ".join(c) if c else None),
            "source_present": _ORCA_SRC.is_dir(),
            **({} if c else {"reason": "orca_cli_not_built",
               "hint": f"cd {_ORCA_SRC} && corepack pnpm install && corepack pnpm run build:cli"})}


def run(qhash: str, task: str, gold: str = "") -> dict:
    """对口交付:orca agent-context(打印232命令编排schema·供agent发现调用)。不在位诚实 unavailable。"""
    t0 = time.time()
    c = _cmd()
    if not c:
        return {"harness": "orca", "status": "unavailable", "final": "",
                "reason": "orca_cli_not_built", "elapsed": round(time.time() - t0, 1)}
    try:
        r = subprocess.run(c + ["agent-context"], capture_output=True,
                           text=True, timeout=30)
        return {"harness": "orca", "status": "ok" if r.returncode == 0 else f"rc{r.returncode}",
                "final": (r.stdout or r.stderr or "")[:400], "bin": " ".join(c),
                "note": "orca并行agent编排CLI·agent-context给出机器可读命令schema",
                "elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        return {"harness": "orca", "status": f"err:{type(e).__name__}",
                "final": str(e)[:200], "elapsed": round(time.time() - t0, 1)}
