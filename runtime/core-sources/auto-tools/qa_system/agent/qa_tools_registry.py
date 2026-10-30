#!/usr/bin/env python3
"""
qa_tools_registry · 工具/子Agent 统一注册表 (v58)

用户 2026-08-06 需求:
  「记得时刻同步真qa agent的改动，最好直接就让他们注册好，qa和qa auto
   都是共用的」
  「现在auto 和 qa都是什么情况使用，最后架构优雅，是两种模式，工具等
   共享，核心组件复用」

问题背景:
  v57 的 qa_auto_agent.py 手写了一份 11 个工具的封装，和 qa_agent_main.py
  的 orchestrate() 是两份独立维护的代码——orchestrate() 里新增/改动一个
  Step (或者接入新 sub-agent，比如 qa_action_cone/qa_multi_nocode/
  qa_clarify)，auto agent 那边不会自动感知到，两边必然会漂移。

方案:
  单一真相来源 (single source of truth)。所有「可被调用的能力」
  (无论是 orchestrate() 里的固定 Step，还是独立的 sub-agent 模块) 都在
  这里用 @register_tool 注册一次，声明:
    - name/description: 给 LLM 看的 tool schema (auto agent 用)
    - category: 何时该用 (scope/search/deep_research/writing/qc/delivery/optional)
    - cost_tier: cheap(longcat-2.0 秒级) / medium(数十秒) / expensive(网络搜索·分钟级)
    - retryable: 是否允许重试 (配合 qa_retry_policy 做自适应重试次数)
    - executor(question, session) -> summary_str: 真正执行的函数，
      读写同一个 session dict (由调用方在 pipeline/auto 两边分别持有)

  qa_agent_main.orchestrate() 和 qa_auto_agent.run_auto_agent() 都从
  TOOLS 这个注册表里取工具，而不是各自 import 子模块、各自包一层。
  以后新增子 agent，只需要在这里 @register_tool 一次，两边自动同步。
"""
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class ToolSpec:
    name: str
    description: str
    category: str          # scope | search | deep_research | qc | writing | delivery | optional
    cost_tier: str          # cheap | medium | expensive
    executor: Callable      # (question: str, session: dict) -> str (摘要文本)
    retryable: bool = False
    max_retry_hint: int = 0  # 0=不建议重试；>0 是「资源充裕+不紧急」时的建议上限
    required_session_keys: list = field(default_factory=list)  # 调用前建议已具备的 session key


_REGISTRY: dict[str, ToolSpec] = {}


def register_tool(name: str, description: str, category: str, cost_tier: str,
                   retryable: bool = False, max_retry_hint: int = 0,
                   required_session_keys: list = None):
    """装饰器：把一个 (question, session) -> summary 的函数注册成工具。"""
    def _wrap(fn: Callable):
        _REGISTRY[name] = ToolSpec(
            name=name, description=description, category=category,
            cost_tier=cost_tier, executor=fn, retryable=retryable,
            max_retry_hint=max_retry_hint,
            required_session_keys=required_session_keys or [],
        )
        return fn
    return _wrap


def get_tool(name: str) -> Optional[ToolSpec]:
    return _REGISTRY.get(name)


def all_tools() -> list:
    """返回全部已注册工具，按注册顺序（Python 3.7+ dict 保序）。"""
    return list(_REGISTRY.values())


def tools_by_category(category: str) -> list:
    return [t for t in _REGISTRY.values() if t.category == category]


def _ensure_builtin_registered():
    """真正的注册逻辑放在 qa_tool_impls.py，这里只做懒加载触发，
    避免 import qa_tools_registry 时就把 qa_agent_main 一整套依赖拉进来
    (registry 本身应该是零依赖的薄基础设施)。"""
    if _REGISTRY:
        return
    # 两类实现模块共同构成注册表单一真相来源：
    # qa_tool_impls 是既有 pipeline/sub-agent 能力；capability_stack 是 P0 通用能力。
    # 二者都只在首次实际取工具时导入，保持 registry 基础设施本身零重依赖。
    import qa_tool_impls  # noqa: F401 · import 触发模块内的 @register_tool 装饰器执行
    import capability_stack  # noqa: F401 · P0 能力栈同样通过装饰器注册
    # capability_writer: agent 自主生成算法组件的闭环工具（动态注册·fail-safe）
    try:
        import capability_writer as _cw  # noqa: F401
        _cw._register_to_qa_tools()
    except Exception:
        pass


def get_all_tools() -> list:
    _ensure_builtin_registered()
    return all_tools()


def get_tool_lazy(name: str) -> Optional[ToolSpec]:
    _ensure_builtin_registered()
    return get_tool(name)


def register_extra_tool(name: str, description: str, category: str, cost_tier: str,
                        fn=None, retryable: bool = False, max_retry_hint: int = 0,
                        required_session_keys: list = None,
                        **kwargs) -> None:
    """供扩展模块（如 capability_writer）动态注册额外工具。
    fn 签名: (params: dict, session: dict) -> dict
    与 register_tool 的区别: fn 接收 params dict 而非 (question, session)，
    更灵活，适合参数不固定的能力工具。内部包一层 executor 适配。
    """
    if fn is None:
        return
    # 适配层：把 (question, session) -> summary 转成内部 executor 契约
    def _executor(question, session):
        try:
            # auto_agent 调用时把 params 放在 session["_tool_params"] 里
            params = session.pop("_tool_params", {}) if session else {}
            # 兜底：若没有 params，把 question 当 problem_desc
            if not params and question:
                params = {"problem_desc": question}
            r = fn(params, session)
            if isinstance(r, dict):
                # 取 msg 或 result 作为 summary 字符串
                return r.get("msg") or str(r.get("result") or r.get("error") or r)[:300]
            return str(r)[:300]
        except Exception as exc:
            return f"[{name}] error: {type(exc).__name__}: {exc}"

    _REGISTRY[name] = ToolSpec(
        name=name, description=description, category=category,
        cost_tier=cost_tier, executor=_executor, retryable=retryable,
        max_retry_hint=max_retry_hint,
        required_session_keys=required_session_keys or [],
    )
    # 同时在 spec 上保存原始 fn，供 qa_auto_agent 直接用 params dict 调用
    _REGISTRY[name]._raw_fn = fn  # type: ignore[attr-defined]
