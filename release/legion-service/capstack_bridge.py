#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""capability_stack 工具无状态桥(用户2026-08-30:auto工厂工具全部映射给军团直接复用).

核心洞察(用户提):auto 的 OCR/PDF/图像工具签名是 t_<name>(question, session),看似依赖
auto 整套 session pipeline·但实测【核心干活完全无状态】——session 只用于 _record_extracted_values
的 setdefault/get(给空 dict {} 就能跑)。所以军团任何家可【绕开 auto 恶心的完整 pipeline】,
用 stub session 直接调 capability_stack.t_<name> 拿结果·复用工具核心。

这让"所有工厂工具被全军团直接使用"落地:不必派 auto 家·不走它默认工厂装配流程。
"""
from __future__ import annotations
import os
import sys
import importlib
import json
from pathlib import Path


def source_root():
    """Same relative package layout in dev, cold builds and relocated releases."""
    root = Path(__file__).resolve().parents[2]
    packaged = root / "runtime/core-sources/auto-tools"
    if (packaged / "retained-tools.json").is_file():
        return str(packaged)
    return str(root / "seed-users-system")


_CAPSTACK_DIR = os.path.join(source_root(), "qa_system", "agent")
_LIBRARY_MANIFEST = Path(source_root()) / "retained-tools.json"
_BINDINGS = (json.loads(_LIBRARY_MANIFEST.read_text()).get("bridge_bindings", {})
             if _LIBRARY_MANIFEST.is_file() else None)
# A dedicated additive dependency namespace, never an external member's private venv.
_DEPENDENCIES = Path(__file__).resolve().parents[2] / "runtime/py-tools/retained-auto-tools"
if _DEPENDENCIES.is_dir():
    sys.path.insert(0, str(_DEPENDENCIES))

_MOD = None


# auto 的 t_<name>(question, session) 工具散在多个文件·全部统一桥接(用户2026-08-30:
# auto工具非常多·散在多处·彻底打散全给军团直调)。都是同一 t_ 签名+stub session 机制。
_T_MODULES = ["capability_stack", "qa_tool_impls"]
_MODS = None  # 缓存:{module_name: module_or_exception}


def _no_runtime_install(*args, **kwargs):
    raise RuntimeError("随包工具依赖未就绪；须由构建阶段准备，不在用户任务内安装")


def _load():
    global _MODS
    if _MODS is not None:
        return _MODS
    if _CAPSTACK_DIR not in sys.path:
        sys.path.insert(0, _CAPSTACK_DIR)
    _MODS = {}
    for name in _T_MODULES:
        if _BINDINGS is not None and name not in {b["module"] for b in _BINDINGS.values()}:
            continue  # Preserve other programs as reference; do not start legacy flows.
        try:
            _MODS[name] = __import__(name)
            if _BINDINGS is not None and name == "capability_stack":
                import legion_paths
                state = Path(legion_paths.legion_dir()) / "retained-auto-tools"
                state.mkdir(parents=True, exist_ok=True)
                _MODS[name]._SANDBOX_DATA_DIR = str(state / "datasets")
                _MODS[name]._CAP_TELEMETRY_LOG = str(state / "capability_telemetry.jsonl")
                for installer in ("_install_sympy", "_install_duckdb", "_install_ijson"):
                    if hasattr(_MODS[name], installer):
                        setattr(_MODS[name], installer, _no_runtime_install)
        except Exception as e:
            _MODS[name] = e
    return _MODS


def available() -> bool:
    return any(not isinstance(m, Exception) for m in _load().values())


def _find_fn(tool_name: str):
    """在所有 t_ 模块里找 t_<tool_name> callable(先到先得)."""
    for m in _load().values():
        if isinstance(m, Exception):
            continue
        fn = getattr(m, f"t_{tool_name}", None)
        if callable(fn) and _binding_ready(tool_name):
            return fn
    return None


def _binding_ready(name):
    if _BINDINGS is None:
        return True  # Existing dev layout; packaged tools explicitly declare their bridge.
    binding = _BINDINGS.get(name)
    if not binding:
        return False
    try:
        # Existence alone misses absent transitive packages or broken native libraries.
        for module in binding.get("requires", []):
            importlib.import_module(module)
        return True
    except Exception:
        return False


def list_bridged_tools() -> list[str]:
    """列出所有 t_ 模块里可无状态桥调的 t_ 工具(签名 (question, session))·去重."""
    names = set()
    for m in _load().values():
        if isinstance(m, Exception):
            continue
        for n in dir(m):
            if n.startswith("t_") and callable(getattr(m, n)) and _binding_ready(n[2:]):
                names.add(n[2:])
    return sorted(names)


def call(tool_name: str, path: str = "", question: str = "", timeout_s: int = 120) -> dict:
    """无状态桥调 auto 的 t_<tool_name>(跨 capability_stack+qa_tool_impls)·stub session·绕开 auto pipeline.

    path: 本地文件路径(OCR/PDF/图像类解析它);question: 覆盖式问题(优先于path).
    返回 {ok, result, tool, via:'capstack_bridge'}。核心工具无状态·session 用空 dict 承接记录。
    """
    fn = _find_fn(tool_name)
    if not callable(fn):
        return {"ok": False, "runnable": False,
                "error": f"工具源码仅供参考或无状态桥/随包依赖未就绪: t_{tool_name}",
                "hint": f"可桥调的: {list_bridged_tools()[:20]}"}
    # 构造 question(工具内 _parse_local_path 从 question 提路径)+ stub session(空dict即可)
    q = question or (path or "")
    stub_session: dict = {"extracted_values": [], "_budget": {}}
    try:
        result = fn(q, stub_session)
        return {"ok": True, "result": str(result), "tool": tool_name,
                "via": "capstack_bridge",
                "extracted_values": stub_session.get("extracted_values", [])}
    except Exception as e:
        return {"ok": False, "error": f"桥调 t_{tool_name} 失败: {type(e).__name__}: {e}",
                "tool": tool_name}


if __name__ == "__main__":
    print("capability_stack 桥可用:", available())
    print("可桥调工具:", list_bridged_tools())
    if len(sys.argv) > 2:
        print(call(sys.argv[1], sys.argv[2]))
