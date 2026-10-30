#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具查询的模型改写器：只补跨语言检索 context，不维护意图词表。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import legion_paths as _paths

_CACHE_PATH = _paths.under("cache", "tool_query_rewrites.json")
_MEM_CACHE: dict[str, list[str]] | None = None


def _load_cache() -> dict[str, list[str]]:
    global _MEM_CACHE
    if _MEM_CACHE is not None:
        return _MEM_CACHE
    try:
        data = json.load(open(_CACHE_PATH, encoding="utf-8"))
        _MEM_CACHE = data if isinstance(data, dict) else {}
    except Exception:
        _MEM_CACHE = {}
    return _MEM_CACHE


def _save_cache(data: dict[str, list[str]]) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _gateway_env() -> dict:
    """给 call_model 子进程一份干净环境:内网 AIGC 网关不走公网代理。

    血泪根因([[codex-rc1-real-cause-dead-proxy.md]]):用户机上常有 socks5 死代理
    (ALL_PROXY=socks5h://127.0.0.1:15235),子进程继承后连不上内网网关 aigc.sankuai.com,
    rewrite 全失败→退化纯 bm25(中文检索召回变差)。把网关 host 加进 NO_PROXY 并清掉
    socks 代理,让子进程直连内网,不依赖那个可能死掉的公网代理。稳定性底线。
    """
    env = dict(os.environ)
    gateway_hosts = "aigc.sankuai.com,127.0.0.1,localhost"
    existing = env.get("NO_PROXY", "") or env.get("no_proxy", "")
    merged = (existing + "," + gateway_hosts).strip(",") if existing else gateway_hosts
    env["NO_PROXY"] = merged
    env["no_proxy"] = merged
    # socks5 代理对内网网关无意义;死代理会拖垮请求,清掉。
    for key in ("ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    return env


def rewrite(query: str, timeout_s: float = 2.0) -> dict:
    """让强模型把需求改写成工具目录检索短语；失败时原查询仍可继续用。"""
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "queries": [], "source": "empty", "error": "empty query"}
    cached = _load_cache().get(query)
    if cached:
        return {"ok": True, "queries": cached, "source": "cache"}

    script = os.path.expanduser("~/.claude/skills/aigc-model-router/scripts/call_model.py")
    if not os.path.isfile(script):
        return {"ok": False, "queries": [], "source": "unavailable", "error": "model router unavailable"}
    prompt = (
        "Rewrite the following request into 3 to 5 short English capability phrases for searching a "
        "software tool catalog. Preserve the user's goal; do not choose a tool or invent capabilities. "
        "Output one phrase per line, English only, no numbering or explanation. Request: " + query
    )
    if os.environ.get("LEGION_TOOL_QUERY_REWRITE", "1") == "0":
        return {"ok": False, "queries": [], "source": "disabled"}
    # 偶发网关抖动会让冷调整体失败→退纯 bm25(中文召回变差)。给一次换 domain 的重试消化
    # 抖动(稳定性底线·不是加规则);两次都失败才降级,legion_tool 仍能用原 query 走 bm25。
    last_err = "no output"
    deadline = time.monotonic() + max(0.2, float(timeout_s))
    for domain in ("programming", "general"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            proc = subprocess.run(
                [sys.executable, script, "chat", prompt, "--domain", domain],
                capture_output=True, text=True, timeout=remaining,
                env=_gateway_env(),
            )
        except Exception as exc:
            last_err = f"{type(exc).__name__}:{exc}"
            continue
        lines = []
        for raw in (proc.stdout or "").splitlines():
            line = re.sub(r"^[\s#>*-]*(?:\d+[.)]\s*)?", "", raw).strip().strip("`\"'")
            if (3 <= len(line) <= 100 and re.search(r"[A-Za-z]", line)
                    and "使用模型" not in line and "appid" not in line.lower()):
                lines.append(line)
        unique = list(dict.fromkeys(lines))[:5]
        if unique:
            cache = _load_cache()
            cache[query] = unique
            _save_cache(cache)
            return {"ok": True, "queries": unique, "source": "model", "elapsed_ms": None}
        last_err = (proc.stderr or proc.stdout or f"rc={proc.returncode}")[-300:]
    return {"ok": False, "queries": [], "source": "model", "error": last_err}
