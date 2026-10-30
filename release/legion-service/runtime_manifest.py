#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""军团运行时 manifest 单一读取入口。

内置和外部成员的来源暂时仍是两个兼容文件，但下游只消费这里归一后的定义。
这样迁移期间不要求一次重写 25 家，同时新增成员不会再漏掉画像、模型、探活或执行契约。
"""
from __future__ import annotations

import copy
import importlib
import json
import pathlib
import threading
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
BUILTIN_PATH = HERE / "harness_manifest.json"
EXTERNAL_PATH = HERE / "external_agents.json"

_SPEC_FIELDS = (
    "integration", "streaming", "resume", "checkpoint", "tools",
    "artifacts", "interrupt", "policy", "model", "cost_class",
    "usage", "compaction",
)
_COST_CLASSES = {"light", "standard", "heavy"}
_USAGE_MODES = {"native", "native_if_reported", "native_role_ledger", "unavailable"}
_COMPACTION_MODES = {"native_events", "native_internal", "framework_internal", "nested", "unknown", "none"}
_EXECUTION_DEPTHS = {"quick", "standard", "deep"}
_ENDPOINTS = {"chat", "vision", "image", "video", "embedding", "audio"}
_WIRE_APIS = {"native", "chat_completions", "responses", "anthropic_messages", "self_managed"}
_REASONING_TOOL_POLICIES = {"native", "omit_reasoning_effort", "responses", "unsupported"}
_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {"stamp": None, "definitions": None, "diagnostics": None}


def _read_json(path: pathlib.Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _stamp(paths: tuple[pathlib.Path, ...]) -> tuple:
    rows = []
    for path in paths:
        try:
            stat = path.stat()
            rows.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            rows.append((str(path), 0, 0))
    return tuple(rows)


def _default_profile(key: str, item: dict) -> dict:
    name = str(item.get("name") or key)
    capabilities = [str(value) for value in item.get("capabilities") or []]
    best_for = "、".join(capabilities[:4]) if capabilities else "通用外部 Agent 任务"
    return {
        "name": name,
        "icon": name[:1].upper() or "A",
        "intro": str(item.get("description") or f"{name} 通过统一运行时 manifest 接入军团。"),
        "strengths": capabilities[:4] or ["外部成员", "统一运行时契约"],
        "style": "按自身原生能力执行，军团提供统一上下文、健康、自愈与交付通道",
        "best_for": best_for,
        "speed": {"light": "快", "heavy": "慢"}.get(
            str((item.get("adapter_capabilities") or {}).get("cost_class") or "standard"), "中"),
    }


def _external_definition(item: dict) -> dict:
    declared = dict(item.get("adapter_capabilities") or {})
    transports = [row for row in item.get("transports") or [] if isinstance(row, dict)]
    protocols = [str(row.get("type")) for row in transports if row.get("type")]
    cost_class = str(declared.get("cost_class") or "standard")
    if cost_class not in _COST_CLASSES:
        cost_class = "standard"
    specs = {
        "integration": "+".join(protocols) or "external",
        "streaming": declared.get("streaming", "events" if "cli" in protocols else "complete"),
        "resume": declared.get("resume", "cold_replay"),
        "checkpoint": bool(declared.get("checkpoint", True)),
        "tools": bool(declared.get("tools", True)),
        "artifacts": bool(declared.get("artifacts", True)),
        "interrupt": bool(declared.get("interrupt", False)),
        "usage": declared.get("usage", "unavailable"),
        "compaction": declared.get("compaction", "none"),
        "policy": declared.get("policy", "context"),
        "model": declared.get("model", "runtime"),
        "cost_class": cost_class,
    }
    execution = dict(item.get("execution") or {})
    execution.setdefault("depths", ["standard", "deep"])
    return {
        "key": str(item.get("key") or ""),
        "name": str(item.get("name") or item.get("key") or ""),
        "source": "external",
        "adapter": "external_transport",
        "direction": str(item.get("direction") or "programming"),
        "scenes": list(item.get("scenes") or []),
        "model_picks": list(item.get("model_picks") or []),
        "model_compatibility": {
            "endpoint_types": ["chat"], "wire_api": "self_managed", "reasoning_tools": "native",
            **dict(item.get("model_compatibility") or {}),
        },
        "execution": execution,
        "specs": specs,
        "profile": dict(item.get("profile") or _default_profile(str(item.get("key") or ""), item)),
        "runtime": copy.deepcopy(item),
    }


def _builtin_definition(key: str, item: dict) -> dict:
    out = copy.deepcopy(item)
    profile = dict(item.get("profile") or {})
    profile.setdefault("name", key)
    profile.setdefault("intro", f"{key} 通过统一运行时 manifest 接入军团。")
    profile.setdefault("best_for", "通用 Agent 任务")
    profile.setdefault("strengths", ["统一运行时契约"])
    profile.setdefault("style", "按自身原生能力执行")
    out["profile"] = profile
    out.update({"key": key, "name": str(profile["name"]), "source": "builtin",
                "runtime": dict(item.get("runtime") or {})})
    # 平台面角色(用户2026-09-08钉死'auto剔除军团'):builtin家的runtime.execution_role如实透出——
    # capability/control-plane/ui-platform 的家永不进答题池(派单池纯净性铁律);外部catalog
    # 早有此过滤,内置manifest家现在同源接入。
    if str((out["runtime"] or {}).get("execution_role") or ""):
        out["execution_role"] = str(out["runtime"]["execution_role"])
    out.setdefault("scenes", [])
    compatibility = dict(out.get("model_compatibility") or {})
    compatibility.setdefault("endpoint_types", ["chat"])
    compatibility.setdefault("wire_api", "self_managed")
    compatibility.setdefault("reasoning_tools", "native")
    out["model_compatibility"] = compatibility
    execution = dict(out.get("execution") or {})
    # 执行档位由 manifest 明确声明；未声明只给 standard/deep，新增家不会凭名字冒充快家。
    execution.setdefault("depths", ["standard", "deep"])
    out["execution"] = execution
    return out


def _validate_definition(key: str, item: dict) -> list[str]:
    errors = []
    if not key:
        errors.append("缺 key")
    if item.get("source") not in {"builtin", "external"}:
        errors.append("source 必须是 builtin/external")
    if not item.get("adapter"):
        errors.append("缺 adapter/transport")
    specs = item.get("specs") or {}
    for field in _SPEC_FIELDS:
        if field not in specs or specs.get(field) in (None, ""):
            errors.append(f"specs 缺 {field}")
    if specs.get("cost_class") not in _COST_CLASSES:
        errors.append("cost_class 必须是 light/standard/heavy")
    if specs.get("usage") not in _USAGE_MODES:
        errors.append("usage 必须是 native/native_if_reported/native_role_ledger/unavailable")
    if specs.get("compaction") not in _COMPACTION_MODES:
        errors.append("compaction 声明无效")
    profile = item.get("profile") or {}
    for field in ("name", "intro", "best_for"):
        if not profile.get(field):
            errors.append(f"profile 缺 {field}")
    execution = item.get("execution") or {}
    depths = execution.get("depths") or []
    invalid_depths = [str(value) for value in depths if str(value) not in _EXECUTION_DEPTHS]
    if invalid_depths:
        errors.append(f"未知 execution depths:{','.join(invalid_depths)}")
    if "standard" not in depths:
        errors.append("execution.depths 必须包含 standard")
    compatibility = item.get("model_compatibility") or {}
    endpoints = compatibility.get("endpoint_types") or ["chat"]
    invalid = [str(value) for value in endpoints if str(value) not in _ENDPOINTS]
    if invalid:
        errors.append(f"未知 endpoint_types:{','.join(invalid)}")
    if compatibility.get("wire_api") not in _WIRE_APIS:
        errors.append("model_compatibility.wire_api 声明无效")
    if compatibility.get("reasoning_tools") not in _REASONING_TOOL_POLICIES:
        errors.append("model_compatibility.reasoning_tools 声明无效")
    if item.get("source") == "external":
        runtime = item.get("runtime") or {}
        transports = [row for row in runtime.get("transports") or [] if isinstance(row, dict)]
        executable = runtime.get("execution_role", "agent") not in {"control-plane", "ui-platform", "capability"}
        if executable and specs.get("resume") == "native" and not any(
                row.get("resume_command") or row.get("native_resume") for row in transports):
            errors.append("resume=native 但 transport 无 resume_command/native_resume 证据")
        if executable and specs.get("streaming") == "events" and not transports:
            errors.append("streaming=events 但没有可执行 transport")
        if specs.get("tools") and executable \
                and not (runtime.get("provided_tools") or runtime.get("capabilities")):
            errors.append("tools=true 但没有 provided_tools/capabilities 证据")
    return errors


def load_definitions(force: bool = False) -> dict[str, dict]:
    paths = (BUILTIN_PATH, EXTERNAL_PATH)
    current_stamp = _stamp(paths)
    with _LOCK:
        if not force and _CACHE["definitions"] is not None and _CACHE["stamp"] == current_stamp:
            return copy.deepcopy(_CACHE["definitions"])
        definitions: dict[str, dict] = {}
        diagnostics = []
        builtin = _read_json(BUILTIN_PATH).get("harnesses") or {}
        for key, item in builtin.items():
            if isinstance(item, dict):
                definitions[str(key)] = _builtin_definition(str(key), item)
        for item in _read_json(EXTERNAL_PATH).get("agents") or []:
            if not isinstance(item, dict) or not item.get("key"):
                continue
            key = str(item["key"])
            external = _external_definition(item)
            if key in definitions:
                diagnostics.append({"key": key, "severity": "error", "error": "内置与外部 key 重复"})
                continue
            definitions[key] = external
        for key, item in definitions.items():
            errors = _validate_definition(key, item)
            item["manifest_valid"] = not errors
            item["manifest_errors"] = errors
            diagnostics.extend({"key": key, "severity": "error", "error": error} for error in errors)
        _CACHE.update({"stamp": current_stamp, "definitions": definitions, "diagnostics": diagnostics})
        return copy.deepcopy(definitions)


def diagnostics(force: bool = False) -> dict:
    definitions = load_definitions(force=force)
    with _LOCK:
        rows = copy.deepcopy(_CACHE.get("diagnostics") or [])
    return {"ok": not rows, "count": len(definitions), "errors": rows,
            "builtin": sum(1 for row in definitions.values() if row.get("source") == "builtin"),
            "external": sum(1 for row in definitions.values() if row.get("source") == "external")}


def get(key: str) -> dict:
    return load_definitions().get(str(key), {})


def builtin_adapter_specs() -> dict[str, str]:
    return {key: str(item.get("adapter") or "") for key, item in load_definitions().items()
            if item.get("source") == "builtin" and item.get("adapter")
            and str((item.get("runtime") or {}).get("execution_role")
                    or item.get("execution_role") or "agent")
            not in {"capability", "control-plane", "ui-platform"}}


def resolve_builtin_adapters(module_name: str = "harness_registry") -> dict[str, Any]:
    module = importlib.import_module(module_name)
    out = {}
    for key, symbol in builtin_adapter_specs().items():
        adapter = getattr(module, symbol, None)
        if callable(adapter):
            out[key] = adapter
    return out


def autonomy_specs() -> dict[str, dict]:
    return {key: copy.deepcopy(item.get("specs") or {}) for key, item in load_definitions().items()}


def profiles() -> dict[str, dict]:
    return {key: copy.deepcopy(item.get("profile") or {}) for key, item in load_definitions().items()}


def direction(key: str, default: str = "programming") -> str:
    return str(get(key).get("direction") or default)


def model_picks(key: str) -> list:
    return copy.deepcopy(get(key).get("model_picks") or [])


def model_compatible(key: str, endpoint_type: str) -> bool:
    allowed = (get(key).get("model_compatibility") or {}).get("endpoint_types") or ["chat"]
    return endpoint_type in allowed


def model_protocol(key: str) -> dict:
    compatibility = copy.deepcopy(get(key).get("model_compatibility") or {})
    compatibility.setdefault("endpoint_types", ["chat"])
    compatibility.setdefault("wire_api", "self_managed")
    compatibility.setdefault("reasoning_tools", "native")
    return compatibility


def supports_depth(key: str, depth: str) -> bool:
    return str(depth) in ((get(key).get("execution") or {}).get("depths") or ["standard", "deep"])
