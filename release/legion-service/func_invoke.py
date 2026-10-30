#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""源码函数调用入口：静态候选不等于运行成功。

同步 Python 函数尝试本地加载；需环境的源码调用复用已声明的成员原生
执行工具与 run_one。实例、缺依赖、拒绝、派发尝试和运行结果分别返回。
"""
from __future__ import annotations
import hashlib
import importlib.util
import inspect
import json
import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_IMPORT_LOCK = threading.RLock()
_MISSING = object()

# 判"能否本进程直调"的启发式:模块顶层 import 了这些=有重依赖/需服务→不直调(派家)。
_HEAVY_IMPORTS = {
    "fastmcp", "httpx", "docker", "playwright", "selenium", "torch", "tensorflow",
    "google.genai", "google.cloud", "openai", "anthropic", "boto3", "grpc",
    "uvicorn", "fastapi", "flask", "aiohttp", "websockets", "pyaudio", "sounddevice",
    "cv2", "mss", "pyautogui",
}


def _family_to_harness(family: str) -> str:
    """把 func_index 的 family 归到可派单的家名(eco:xxx → xxx·主家原样)。"""
    return family.split(":", 1)[-1] if family.startswith("eco:") else family


def _module_heavy_deps(path: str) -> list[str]:
    """扫模块顶层 import·返回命中的重依赖(判定要不要派家·不真import避免副作用)。"""
    hit = []
    try:
        src = open(os.path.join(_REPO, path), encoding="utf-8", errors="ignore").read()
    except Exception:
        return hit
    for line in src.splitlines()[:80]:  # 只看文件头部 import 区
        s = line.strip()
        if not (s.startswith("import ") or s.startswith("from ")):
            continue
        for dep in _HEAVY_IMPORTS:
            root = dep.split(".")[0]
            if f"import {root}" in s or f"from {root}" in s or f"from {dep}" in s:
                hit.append(dep)
    return sorted(set(hit))


def can_direct_call(path: str, lang: str) -> tuple[bool, str]:
    """静态初筛，不导入、不验证调用；True 只表示未发现模块级环境要求。"""
    if lang != "python":
        return False, f"{lang} 非py·本进程不能直调"
    deps = _module_heavy_deps(path)
    if deps:
        return False, f"模块依赖 {deps}(需该家runtime·非纯本地)"
    return True, "Python 静态候选，尚未验证导入、参数和实际调用"


def _delegate_source(path, qual, harness, args, lang, reason, timeout_s, dispatch):
    result = {"ok": False, "callable": "requires_runtime", "harness": harness,
              "path": path, "qual": qual, "source_reason": reason, "reason": reason,
              "dispatch_attempted": False, "dispatched": False}
    if not dispatch:
        return result
    try:
        import agent_tool_provider as atp
        declaration = next((row for row in atp.inventory()["declarations"]
                            if row["provider"] == harness), {})
        if declaration.get("execution_ready") is False:
            return {**result, "reason": declaration["reason"]}
        # qual is a source symbol, never an invented native tool name.
        executor = next((tool for tool in declaration.get("tools", [])
                         if tool.get("capability") == "shell"), None)
        if executor is None:
            return {**result, "reason": f"{harness} 未声明可执行源码的原生工具",
                    "how": f"查看 {path} 的项目运行入口与依赖声明"}
        task = (f"请使用你已声明的原生 {executor['name']} 工具执行源码 {path} 里的 "
                f"{qual}（{lang}），参数：{json.dumps(args, ensure_ascii=False)}。"
                f"本地调用情况：{reason}。返回真实执行结果或无法执行的具体原因。")
        reply = atp.invoke(harness, executor["name"], args, task=task, attempt_timeout=timeout_s)
        attempted = reply.get("dispatch_attempted", False)
        started = reply.get("dispatched", None if attempted else False)
        return {**result, "ok": bool(reply.get("ok")),
                "callable": ("dispatched" if started is True else
                             "dispatch_attempted" if attempted else "requires_runtime"),
                "dispatch_attempted": attempted, "dispatched": started,
                "reason": (reply.get("error") or (reply.get("result") or {}).get("error")
                           or ("" if reply.get("ok") else
                               f"成员返回状态：{(reply.get('result') or {}).get('status', 'unknown')}")),
                "via": f"func_invoke:dispatch→{harness}",
                "result": reply.get("result"), "dispatch_raw": reply}
    except Exception as exc:
        return {**result, "reason": f"成员调用入口失败: {type(exc).__name__}: {exc}"}


def _load_module(abs_path):
    """Keep one registered module per path/source version, not per invocation.

    Old versions retain their identity while a new version executes. A failed
    load removes only its incomplete version. Read source afresh (not pyc);
    the lock protects loading only, never the subsequently invoked function.
    """
    base_name = f"_fi_{abs(hash(abs_path))}"
    with _IMPORT_LOCK:
        spec = importlib.util.spec_from_file_location(base_name, abs_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"不能加载源码: {abs_path}")
        source = spec.loader.get_source(base_name)
        if source is None:
            raise ImportError(f"没有 Python 源码: {abs_path}")
        name = f"{base_name}_{hashlib.sha256(source.encode('utf-8')).hexdigest()}"
        previous = sys.modules.get(name, _MISSING)
        if previous is not _MISSING:
            return previous
        spec = importlib.util.spec_from_file_location(name, abs_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"不能加载源码: {abs_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            exec(compile(source, abs_path, "exec"), module.__dict__)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module


def invoke(path: str, qual: str, family: str, args: dict | None = None,
           lang: str = "python", timeout_s: int = 60, dispatch: bool = True) -> dict:
    """尝试调用源码；dispatch=False 时绝不委派或自动安装依赖。"""
    args = args or {}
    harness = _family_to_harness(family)
    base = {"ok": False, "path": path, "qual": qual, "harness": harness,
            "dispatch_attempted": False, "dispatched": False}
    ok_direct, reason = can_direct_call(path, lang)
    if not ok_direct:
        return _delegate_source(path, qual, harness, args, lang, reason, timeout_s, dispatch)
    abs_path = os.path.abspath(os.path.join(_REPO, path))
    if not os.path.isfile(abs_path):
        return {**base, "error": f"源码文件不存在: {path}"}
    mod_dir = os.path.dirname(abs_path)
    try:
        for p in (mod_dir, os.path.dirname(mod_dir)):
            if p not in sys.path:
                sys.path.insert(0, p)
        mod = _load_module(abs_path)
    except Exception as e:
        reason = f"import失败: {type(e).__name__}: {e}"
        if isinstance(e, ModuleNotFoundError) and getattr(e, "name", None):
            return {**base, "callable": "requires_runtime", "reason": reason,
                    "source_reason": reason, "missing_dependency": e.name,
                    "how": "按项目依赖声明配置环境后再试；模块名不一定等于安装包名"}
        return _delegate_source(path, qual, harness, args, lang, reason, timeout_s, dispatch)
    target = mod
    for part in qual.split("."):
        descriptor = inspect.getattr_static(target, part, _MISSING)
        if (inspect.isclass(target) and
                (inspect.isfunction(descriptor) or inspect.ismethoddescriptor(descriptor)
                 or isinstance(descriptor, property)) and
                not isinstance(descriptor, (staticmethod, classmethod))):
            return {**base, "callable": "requires_instance", "requires_instance": True,
                    "reason": f"{qual} 需要已有实例；未构造实例、未调用方法"}
        target = getattr(target, part, None)
        if target is None:
            return {**base, "error": f"{path} 里没有 {qual}"}
    if not callable(target):
        return {**base, "error": f"{qual} 不可调用"}
    if (inspect.iscoroutinefunction(target) or inspect.isgeneratorfunction(target)
            or inspect.isasyncgenfunction(target)):
        return _delegate_source(path, qual, harness, args, lang,
                                "异步或生成器函数需要项目运行入口，创建对象不等于执行函数体",
                                timeout_s, dispatch)
    try:
        inspect.signature(target).bind(**args)
    except TypeError as e:
        return {**base, "error": f"参数不匹配: {e}",
                "hint": f"用 func_search 看 {qual} 的签名再补 args"}
    except ValueError:
        pass  # Some extension callables have no inspectable signature.
    try:
        result = target(**args) if args else target()
        if inspect.isawaitable(result) or inspect.isgenerator(result) or inspect.isasyncgen(result):
            if inspect.iscoroutine(result) or inspect.isgenerator(result):
                result.close()
            return {**base, "callable": "requires_runtime", "invocation_started": True,
                    "reason": "函数返回延迟执行对象，未消费结果；不自动重跑以免重复副作用"}
        return {**base, "ok": True, "result": str(result)[:4000], "via": "func_invoke:direct",
                "callable": "direct", "callability_verified": True}
    except Exception as e:
        return {**base, "error": f"调用 {qual} 失败: {type(e).__name__}: {e}"}


if __name__ == "__main__":
    import json
    os.environ.setdefault("MEYO_LEGION_DIR", os.path.expanduser("~/.meyo/legion"))
    # 自测:一个纯本地(dsh-vision不算·它是脚本)·找 func_index 里一个纯本地py函数直调
    print(json.dumps(invoke("release/legion-service/func_index.py", "build",
                            "meyo", {"force": False}), ensure_ascii=False)[:200])
