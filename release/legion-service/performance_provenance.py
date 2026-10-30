#!/usr/bin/env python3
"""Harness 模型 provenance：区分父层请求、适配器回报与日志实际模型。"""
from __future__ import annotations

import re

_MODEL_PATTERNS = (
    re.compile(r"(?im)^\s*Model:\s*([^\s]+)"),
    re.compile(r'"model_used"\s*:\s*"([^"]+)"'),
    re.compile(r'"model"\s*:\s*"([^"]+)"'),
)


def infer_effective_model(result: dict | None, requested_model: str = "") -> dict:
    result = result if isinstance(result, dict) else {}
    explicit = str(result.get("effective_model") or result.get("model_used") or "").strip()
    raw = "\n".join(str(result.get(key) or "") for key in ("stdout", "stderr", "raw_output"))
    logged = ""
    for pattern in _MODEL_PATTERNS:
        match = pattern.search(raw)
        if match:
            logged = match.group(1).strip()
            break
    effective = explicit or logged
    source = "adapter" if explicit else "harness_log" if logged else "unknown"
    honored = bool(effective and requested_model and effective == requested_model)
    return {"requested_model": str(requested_model or ""), "effective_model": effective,
            "model_source": source, "requested_model_honored": honored,
            "model_mismatch": bool(effective and requested_model and effective != requested_model)}
