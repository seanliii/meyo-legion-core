#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Per-user captain selection preference: automatic election or a fixed runnable harness."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import legion_paths as _paths


def _path(user_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(user_id or "default"))[:80]
    return Path(_paths.under("users", safe, "captain.json"))


def load(user_id: str = "default") -> dict:
    try:
        raw = json.loads(_path(user_id).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    mode = raw.get("mode") if raw.get("mode") in {"auto", "fixed"} else "auto"
    return {"mode": mode, "preferred_captain": str(raw.get("preferred_captain") or ""),
            "candidates": [str(x) for x in (raw.get("candidates") or []) if str(x)]}


def save(user_id: str, config: dict, valid: set[str]) -> dict:
    mode = config.get("mode") if config.get("mode") in {"auto", "fixed"} else "auto"
    preferred = str(config.get("preferred_captain") or "")
    if preferred not in valid:
        preferred = ""
    candidates = []
    for value in config.get("candidates") or []:
        key = str(value)
        if key in valid and key not in candidates:
            candidates.append(key)
    if mode == "fixed" and not preferred:
        mode = "auto"
    clean = {"mode": mode, "preferred_captain": preferred, "candidates": candidates}
    path = _path(user_id); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return clean
