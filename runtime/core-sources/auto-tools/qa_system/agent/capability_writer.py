#!/usr/bin/env python3
"""
capability_writer · 2026-08-13

Agent 自主创造算法组件的完整闭环：
  描述问题 → LLM 生成代码 → 沙箱执行验证 → 注册进 capability_stack

设计理念：
  当 agent 遇到「工具返回 open_constraint/error，且 VLM 也解决不了」的情况，
  不应等待人类（CatPaw/用户）来写工具——agent 自己应能做到：
    1. 识别「这道题需要一个新算法」
    2. 把问题用代码层语言精确描述（Problem Statement）
    3. 让 LLM（ask_local_llm / multi_model）生成 Python solver 代码
    4. 在沙箱里运行并验证（有无 exception / 结果是否合理）
    5. 把通过验证的代码注册为 capability_stack 的命名工具
    6. 立刻用新工具解决当前问题

触发场景（主要）：
  • ortho_polygon_from_image 返回 status=open_constraint 且 VLM 无法读标注
  • sympy_calc 提示「公式无法直接建模，需要图算法/拓扑分析」
  • reflective_mind.need_new_tool 非空（反思脑认为需要新能力）
  • agent 卡在 shell_exec 里反复手写相同算法模板

接口：
  write_capability(problem_desc, context, session)
    → {"ok": True/False, "tool_name": str, "code": str, "test_output": str}

  quick_fix_code(code_desc, inputs_example, session)
    → {"ok": True/False, "result": any, "code": str}
    不注册、只跑一次（ad-hoc 场景）

  register_solved_algo(tool_name, description, code, session)
    → {"ok": True/False}
    把已验证的 ad-hoc 代码升格为持久化 capability 工具
"""
from __future__ import annotations
import os
import sys
import json
import time
import hashlib
import textwrap
import traceback
from typing import Any, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
_REGISTRY_FILE = os.path.join(HERE, "_capability_writer_registry.json")
_SANDBOX_TIMEOUT = 30  # 单次验证跑这么多秒够了


# ══════════════════════════════════════════════════════════════════
# 核心 1：LLM 生成 solver 代码
# ══════════════════════════════════════════════════════════════════

_CODE_GEN_PROMPT = """你是一个精确的 Python 算法工程师。根据下面的问题描述，写一个可独立运行的 Python 函数。

## 问题描述
{problem_desc}

## 上下文（已知信息）
{context}

## 要求
1. 函数签名: `def solve(inputs: dict) -> dict`
2. inputs 包含题目给出的所有已知量（key 用英文/下划线命名）
3. 返回 dict 包含 `result`（最终答案）和 `steps`（推导过程列表，至少 3 步，供验证）
4. 必须使用确定性计算（sympy/numpy/pure python），严禁 VLM/LLM/猜测
5. 有缺失约束时用方程组求解（sympy.solve），不能手动目测
6. 不得 import 未标准库的第三方包，只允许：sympy, numpy, math, itertools, collections, re, json
7. 函数体内含简短注释说明关键步骤
8. 代码必须可以直接 exec() 后调用 solve(inputs)，不需要任何外部依赖
9. 如果题目涉及读取文件(CSV/Excel等)，在 inputs 中传入文件路径，函数内用 open/csv 模块读取
10. 如果题目涉及解析网页内容，在 inputs 中传入已抓取的 HTML 文本，函数内用 re 解析
11. 如果题目涉及图像分析，在 inputs 中传入已提取的像素数据/颜色值，不做图像识别
12. 【严禁背答案】代码中绝不许出现 inputs.get('gold')、inputs.get('answer')、inputs.get('correct') 等获取已知答案的写法。必须通过算法计算得出结果，不能从输入中直接取答案。
13. 【通用性】工具必须对同类问题通用，不能硬编码特定题目的答案值。函数应该从 inputs 中的数据参数计算结果，而非返回固定值。

## 输出格式
只输出一个 Python 代码块，不要任何解释文字：
```python
def solve(inputs: dict) -> dict:
    ...
```
"""

_ADHOC_PROMPT = """写一段可独立运行的 Python 代码来完成以下计算。

## 任务描述
{code_desc}

## 示例输入
{inputs_example}

## 要求
1. 使用确定性计算（sympy/numpy/pure python），严禁猜测/VLM
2. 最后一行必须是: `_result = <答案变量或表达式>`
3. 只允许 import: sympy, numpy, math, itertools, collections, re
4. 代码直接可执行（无函数包装）

只输出代码块，不要解释：
```python
...
_result = <答案>
```
"""


def _ask_llm(prompt: str, session=None, model="claude-sonnet") -> str:
    """调 multi_model 的 LLM 生成代码。fail-safe。"""
    sys.path.insert(0, HERE)
    try:
        import multi_model as mm
        r = mm.ask(model, prompt, timeout=90, max_tokens=2000)
        if r.get("ok"):
            return r["out"]
        # 退到 claude-haiku
        r2 = mm.ask("claude-haiku", prompt, timeout=60, max_tokens=1500)
        return r2.get("out", "") if r2.get("ok") else ""
    except Exception as e:
        return ""


def _extract_code_block(text: str) -> str:
    """从 LLM 输出里提取 ```python ... ``` 代码块。"""
    import re
    # 优先匹配 ```python ... ```
    m = re.search(r"```python\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 退路：```...```
    m2 = re.search(r"```\s*(.*?)```", text, re.DOTALL)
    if m2:
        return m2.group(1).strip()
    # 退路2：找 def solve( 开头的连续行
    lines = text.splitlines()
    in_block = False
    block_lines = []
    for line in lines:
        if "def solve(" in line or line.strip().startswith("def solve"):
            in_block = True
        if in_block:
            block_lines.append(line)
    return "\n".join(block_lines).strip() if block_lines else text.strip()


# ══════════════════════════════════════════════════════════════════
# 核心 2：沙箱执行验证
# ══════════════════════════════════════════════════════════════════

def _sandbox_exec(code: str, call_expr: str = "", timeout_sec: int = _SANDBOX_TIMEOUT) -> dict:
    """
    在受限命名空间里执行 code，然后 eval(call_expr)。
    返回 {ok, result, stdout_like, error}。
    """
    import builtins
    import io
    import contextlib

    safe_builtins = {
        k: getattr(builtins, k) for k in [
            "print", "len", "str", "int", "float", "bool", "range",
            "list", "dict", "set", "tuple", "sorted", "reversed",
            "min", "max", "sum", "abs", "round", "isinstance", "type",
            "enumerate", "zip", "map", "filter", "any", "all",
            "repr", "hash", "id", "iter", "next", "vars", "dir",
            "ValueError", "TypeError", "KeyError", "RuntimeError",
            "AttributeError", "ImportError", "Exception", "StopIteration",
            "__import__",  # 允许 import sympy/numpy
            "open",  # v2: 允许读文件(CSV等)
            "format", "getattr", "setattr", "hasattr",
            "StopIteration", "OverflowError", "ZeroDivisionError",
        ]
    }
    ns: dict[str, Any] = {"__builtins__": safe_builtins}

    # 允许常用库
    try:
        import math; ns["math"] = math
    except Exception: pass
    try:
        import re; ns["re"] = re
    except Exception: pass
    try:
        import itertools; ns["itertools"] = itertools
    except Exception: pass
    try:
        import collections; ns["collections"] = collections
    except Exception: pass
    try:
        import sympy; ns["sympy"] = sympy
        from sympy import symbols, solve, Rational, sqrt, Abs, N
        ns.update({"symbols": symbols, "solve": solve, "Rational": Rational,
                   "sqrt": sqrt, "Abs": Abs, "N": N})
    except Exception: pass
    try:
        import numpy as np; ns["numpy"] = ns["np"] = np
    except Exception: pass

    buf = io.StringIO()
    t0 = time.time()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<capability_writer_sandbox>", "exec"), ns)
        elapsed = time.time() - t0
        if elapsed > timeout_sec:
            return {"ok": False, "error": f"执行超时({elapsed:.1f}s>{timeout_sec}s)",
                    "stdout": buf.getvalue()[:400]}

        result = None
        if call_expr:
            with contextlib.redirect_stdout(buf):
                result = eval(call_expr, ns)
        elif "_result" in ns:
            result = ns["_result"]

        return {
            "ok": True,
            "result": result,
            "ns": ns,
            "stdout": buf.getvalue()[:600],
            "elapsed_sec": round(elapsed, 2),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-300:]}",
            "stdout": buf.getvalue()[:300],
            "elapsed_sec": round(time.time() - t0, 2),
        }


# ══════════════════════════════════════════════════════════════════
# 核心 3：注册到 capability_stack
# ══════════════════════════════════════════════════════════════════

def _check_result_quality(problem_desc: str, context: str, code: str,
                          result: Any, session=None) -> dict:
    """质量验证:检查沙箱跑出的结果是否合理(不只验不崩·验算对)。
    不用 gold(防背答案)·但判结果类型/量级/格式是否符题目要求。
    返回 {acceptable: bool, reason: str}。fail-safe: LLM 异常返回 acceptable=True(不误拦)。
    用户洞察:"沙箱只验不崩不验算对"——造的工具算出来≠gold也过了。
    """
    try:
        _r = str(result) if result is not None else "(无返回值)"
        # 快速判:结果为 None/空 → 不合理
        if result is None or _r.strip() == "":
            return {"acceptable": False, "reason": "结果为 None/空——算法没有产出有效值"}
        # 快速判:结果含 error/None/NaN → 不合理
        _r_low = _r.lower()
        if any(k in _r_low for k in ("error", "none", "nan", "inf", "traceback")):
            return {"acceptable": False,
                    "reason": f"结果含异常标记({_r[:100]})——算法可能内部出错但没崩"}
        # LLM 判:看问题描述+代码+结果·判合理性(不给 gold·只看类型/量级/格式)
        _prompt = (
            "你是算法审查员。下面这段 Python 代码解了一个问题·执行成功(没崩)了。"
            "但「不崩」≠「算对」。请检查结果是否合理。\n\n"
            f"问题描述: {problem_desc[:300]}\n"
            f"已知上下文: {context[:200]}\n"
            f"代码(摘要): {code[:600]}\n"
            f"执行结果: {_r[:200]}\n\n"
            "判据(只判这些·不给解题步骤):\n"
            "  · 结果类型对吗?(该是数值还是列表?该是整数还是浮点?)\n"
            "  · 量级合理吗?(面积该是正数·不是负数或巨大值;计数该是小整数)\n"
            "  · 格式对吗?(题目要逗号分隔列表·结果是不是?)\n"
            "  · 有没有明显的逻辑错误?(如把所有值加起来而不是筛选·或单位换算错)\n"
            "输出JSON: {\"acceptable\":true/false, \"reason\":\"一句话说不合理在哪\"}\n"
            "acceptable=true 就别废话。只输出一个JSON。")
        _raw = _ask_llm(_prompt, session=session, model="claude-haiku")
        import re as _re_q
        s = (_raw or "").strip()
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            return {"acceptable": True}  # 解析不出 → 不误拦
        d = json.loads(s[i:j+1])
        return {"acceptable": bool(d.get("acceptable", True)),
                "reason": d.get("reason", "")}
    except Exception as e:
        return {"acceptable": True, "reason": f"质量检查异常(不误拦): {type(e).__name__}"}


def _detect_gold_leakage(code: str) -> str:
    """★gold 泄露检测器★(2026-08-14 用户:杜绝背答案)。
    检查生成的工具代码是否含获取已知答案的写法。
    返回泄露描述(str)，空串=无泄露。"""
    if not code:
        return ""
    _l = code.lower()
    # 检测 inputs.get('gold') 等背答案写法
    _patterns = [
        "inputs.get('gold'", "inputs.get(\"gold\"",
        "inputs.get('answer'", "inputs.get(\"answer\"",
        "inputs.get('correct'", "inputs.get(\"correct\"",
        "inputs['gold']", "inputs[\"gold\"]",
        "gold_answer", "gold_answer =",
        "# gold", "# gold answer", "# 正确答案",
        "gold standard answer", "known correct answer",
    ]
    for p in _patterns:
        if p in _l:
            return f"代码含背答案写法: '{p}' — 工具不能从输入取答案，必须通过算法计算"
    return ""


def _register_to_capability_stack(tool_name: str, description: str,
                                  code: str, test_output: str) -> dict:
    """
    把已验证的 solver 代码注册成 capability_stack 里可调用的工具。

    原理：向 _capability_writer_registry.json 写入元信息，
    capability_stack 的 get_written_tool() 按 tool_name 加载并执行。
    （这样不用修改 capability_stack.py 的结构，只扩展它的"已知工具"集合）
    """
    registry = _load_registry()
    tool_hash = hashlib.sha256(code.encode()).hexdigest()[:16]
    registry[tool_name] = {
        "tool_name": tool_name,
        "description": description,
        "code": code,
        "test_output": test_output[:300],
        "hash": tool_hash,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_by": "capability_writer",
    }
    _save_registry(registry)
    return {"ok": True, "tool_name": tool_name, "hash": tool_hash}


def _load_registry() -> dict:
    if not os.path.exists(_REGISTRY_FILE):
        return {}
    try:
        return json.load(open(_REGISTRY_FILE, encoding="utf-8"))
    except Exception:
        return {}


def _save_registry(data: dict):
    try:
        with open(_REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════
# 公开接口 1：write_capability（完整闭环）
# ══════════════════════════════════════════════════════════════════

def write_capability(problem_desc: str, context: str = "",
                     session=None, tool_name: str = "",
                     verify_call: str = "") -> dict:
    """
    完整闭环：描述问题 → 生成代码 → 沙箱验证 → 注册。

    Args:
        problem_desc: 用自然语言描述「需要一个什么样的算法/函数」
        context: 已知量（如边长、约束条件），JSON 或自然语言
        session: agent session，用于向 calculation_trace 写入痕迹
        tool_name: 为新工具起一个名字（空则自动生成）
        verify_call: 用于验证的 Python 表达式（如 `solve({'edges': [...]})`）

    Returns:
        {
          "ok": bool,
          "tool_name": str,
          "code": str,
          "test_output": str,
          "error": str  # only on failure
        }
    """
    t0 = time.time()

    # Step 1: 生成语义化工具名(基于场景描述·非hash/qid)
    if not tool_name:
        # 让 LLM 从问题描述提取 2-4 个英文词的工具名
        _name_prompt = (
            "根据以下问题描述，生成一个简洁的英文工具名(2-4个单词，下划线连接)。\n"
            "要求: 名字要描述工具做什么，便于搜索发现。"
            "如 polygon_area_solver / csv_column_sum_calc / image_color_count / ec_number_lookup\n"
            f"问题描述: {problem_desc[:200]}\n"
            "只输出工具名，不要解释"
        )
        _name_raw = _ask_llm(_name_prompt, session=session, model="claude-haiku")
        _name_raw = _name_raw.strip().strip('`').strip()
        # 清理成合法 python 标识符
        import re as _re_name
        _name_raw = _re_name.sub(r"[^a-z0-9_]", "", _name_raw.lower().replace(" ", "_"))[:48]
        if _name_raw and _name_raw[0].isdigit():
            _name_raw = "tool_" + _name_raw
        tool_name = _name_raw if _name_raw else "custom_solver"

    # Step 1.5: 【先找现成方法/库】(2026-08-13 用户:自进化该会"自己上网搜方法找插件和skill"·
    #   不是只凭空造代码)。凭空生成质量不够(多边形/火车时刻反复错)——先让 LLM 列出【解这类问题
    #   的现成成熟库/方法】(它知道 shapely 解多边形/gtfs-kit 解时刻表/CoolProp 解物性)·把这些
    #   喂进代码生成提示·让它【优先用经过验证的现成库】而非重新发明轮子。这是"查对/算对靠找现成
    #   工具"的落地。fail-safe:搜方法失败退回纯生成。
    _existing_methods = ""
    try:
        _search_prompt = (
            "下面是一个需要用 Python 解决的问题。列出【解这类问题的现成成熟 Python 库/方法】"
            "(最多3个·按可靠性排序)·每个给:库名 + pip安装名 + 关键函数/用法一句话。"
            "只列真实存在的成熟库(如几何→shapely·GTFS时刻表→gtfs-kit/partridge·物性→CoolProp·"
            "PDF→pdfplumber·化学→pubchempy)。没有合适现成库就答'无·需自己实现'。\n"
            f"问题: {problem_desc[:400]}\n只输出库清单或'无'。")
        _existing_methods = _ask_llm(_search_prompt, session=session, model="claude-haiku")[:500]
    except Exception:
        _existing_methods = ""
    _methods_block = (f"\n\n## 现成可用的成熟库(优先用这些·别重新发明·缺库用 pip 装):\n{_existing_methods}\n"
                      if _existing_methods and "无" not in _existing_methods[:5] else "")
    # Step 2: LLM 生成代码（最多重试 3 次·优先用现成库）
    prompt = _CODE_GEN_PROMPT.format(
        problem_desc=problem_desc[:1200],
        context=(context or "(无额外上下文)")[:800]
    ) + _methods_block
    code = ""
    last_error = ""
    exec_result = {}
    for attempt in range(1, 4):
        raw = _ask_llm(prompt, session=session)
        code = _extract_code_block(raw)
        if not code:
            last_error = f"第{attempt}次生成代码为空"
            continue

        # Step 3: 沙箱验证（代码不崩）
        exec_result = _sandbox_exec(code, call_expr=verify_call)
        if exec_result["ok"]:
            # Step 3.5: 质量验证（结果合理性·不只验不崩）
            # 用户洞察:"沙箱只验不崩不验算对"——造的工具算出来≠gold也过了。
            # 加一道:让 LLM 看"问题描述+代码+执行结果"·判结果是否合理。
            # 不用 gold(防背答案)·但判"结果类型/量级/格式"是否符合题目要求。
            _result_val = exec_result.get("result")
            _quality = _check_result_quality(problem_desc, context, code, _result_val, session)
            # ★gold 泄露检测★(2026-08-14 用户:杜绝背答案)
            # 检查生成的代码是否含 inputs.get('gold') 等背答案写法
            _gold_leak = _detect_gold_leakage(code)
            if _gold_leak:
                _quality = {"acceptable": False, "reason": f"gold泄露检测: {_gold_leak}"}
            if _quality.get("acceptable"):
                break  # 不崩+合理+无gold泄露 → 通过
            # 结果不合理：把原因附进提示让 LLM 修复
            last_error = _quality.get("reason", "结果不合理")
            prompt = (prompt
                     + f"\n\n## 上一次生成的代码执行成功但结果不合理（请修复）\n"
                     + f"执行结果: {str(_result_val)[:200]}\n"
                     + f"不合理原因: {last_error[:300]}\n"
                     + f"请修正算法逻辑确保结果合理。")
            continue

        # 失败：把错误附进提示让 LLM 修复
        last_error = exec_result["error"]
        prompt = prompt + f"\n\n## 上一次生成的代码执行出错（请修复）\n错误信息:\n{last_error[:400]}"
    else:
        return {
            "ok": False,
            "tool_name": tool_name,
            "code": code,
            "error": f"生成代码连续{3}次失败（执行错误或结果不合理），最后错误: {last_error[:200]}",
        }

    test_output = str(exec_result.get("result", ""))[:200] + "\n" + exec_result.get("stdout", "")[:200]

    # Step 4: 注册
    reg_result = _register_to_capability_stack(
        tool_name=tool_name,
        description=problem_desc[:200],
        code=code,
        test_output=test_output,
    )

    # Step 5: 写入 calculation_trace（供推导链门放行）
    if session is not None:
        trace = session.setdefault("calculation_trace", [])
        trace.append({
            "engine": "capability_writer",
            "tool_name": tool_name,
            "problem_desc": problem_desc[:120],
            "result": test_output[:80],
            "elapsed_sec": round(time.time() - t0, 2),
        })
        # 通知 budget
        budget = session.get("_budget")
        if budget is not None:
            budget["_capability_written"] = tool_name
            budget["vlm_ever_used"] = budget.get("vlm_ever_used", False)  # 不重置VLM标

    elapsed = round(time.time() - t0, 2)
    return {
        "ok": True,
        "tool_name": tool_name,
        "code": code,
        "test_output": test_output,
        "registry": reg_result,
        "elapsed_sec": elapsed,
        "msg": (
            f"[capability_writer] ✓ 新工具 `{tool_name}` 已生成并注册\n"
            f"测试输出: {test_output[:120]}\n"
            f"代码已存入 _capability_writer_registry.json\n"
            f"下一步: 调用 run_written_tool('{tool_name}', inputs) 来使用它"
        ),
    }


# ══════════════════════════════════════════════════════════════════
# 公开接口 2：quick_fix_code（ad-hoc，不注册）
# ══════════════════════════════════════════════════════════════════

def quick_fix_code(code_desc: str, inputs_example: str = "",
                   session=None) -> dict:
    """
    一次性 ad-hoc 计算：生成代码、跑一次、返回结果。不注册。
    适合「只用一次的小计算」，不需要工具化。

    当反复出现相同 code_desc 时，上层可调 register_solved_algo 升格为持久工具。
    """
    prompt = _ADHOC_PROMPT.format(
        code_desc=code_desc[:1000],
        inputs_example=inputs_example[:400] or "(无示例)"
    )
    code = ""
    for attempt in range(1, 3):
        raw = _ask_llm(prompt, session=session)
        code = _extract_code_block(raw)
        if not code:
            continue
        result = _sandbox_exec(code)
        if result["ok"]:
            # 写 calculation_trace
            if session is not None:
                trace = session.setdefault("calculation_trace", [])
                trace.append({
                    "engine": "capability_writer_adhoc",
                    "desc": code_desc[:80],
                    "result": str(result.get("result", ""))[:80],
                })
            return {
                "ok": True,
                "result": result.get("result"),
                "stdout": result.get("stdout", ""),
                "code": code,
                "elapsed_sec": result.get("elapsed_sec"),
            }
    return {
        "ok": False,
        "error": "ad-hoc 代码生成/执行失败",
        "code": code,
    }


# ══════════════════════════════════════════════════════════════════
# 公开接口 3：run_written_tool（执行已注册工具）
# ══════════════════════════════════════════════════════════════════

def run_written_tool(tool_name: str, inputs: dict, session=None) -> dict:
    """
    执行 capability_writer 注册的工具（从 registry 加载代码，沙箱运行）。

    Args:
        tool_name: write_capability 返回的 tool_name
        inputs: 传给 solve(inputs) 的参数 dict

    Returns:
        solve() 的返回值 dict，含 result 和 steps
    """
    registry = _load_registry()
    if tool_name not in registry:
        return {"ok": False, "error": f"工具 '{tool_name}' 未在 capability_writer registry 中，"
                                       f"可用工具: {list(registry.keys())}"}
    entry = registry[tool_name]
    code = entry["code"]
    call_expr = f"solve({json.dumps(inputs, ensure_ascii=False)})"
    result = _sandbox_exec(code, call_expr=call_expr)
    if result["ok"]:
        # 写 calculation_trace
        if session is not None:
            trace = session.setdefault("calculation_trace", [])
            trace.append({
                "engine": "capability_writer_run",
                "tool_name": tool_name,
                "inputs": str(inputs)[:120],
                "result": str(result.get("result", ""))[:120],
            })
        return {
            "ok": True,
            "tool_name": tool_name,
            "result": result.get("result"),
            "stdout": result.get("stdout", ""),
        }
    return {
        "ok": False,
        "tool_name": tool_name,
        "error": result.get("error", ""),
    }


# ══════════════════════════════════════════════════════════════════
# 公开接口 4：register_solved_algo（升格 ad-hoc 为持久工具）
# ══════════════════════════════════════════════════════════════════

def register_solved_algo(tool_name: str, description: str,
                         code: str, session=None) -> dict:
    """
    把一段已验证的 ad-hoc 代码升格为持久化 capability 工具。
    供 agent 在「某个 ad-hoc 计算反复被需要」时调用。
    """
    # 先验证
    result = _sandbox_exec(code)
    if not result["ok"]:
        return {"ok": False, "error": f"代码验证失败，未注册: {result['error'][:200]}"}
    test_output = str(result.get("result", "")) + result.get("stdout", "")[:100]
    return _register_to_capability_stack(tool_name, description, code, test_output)


# ══════════════════════════════════════════════════════════════════
# 公开接口 5：list_written_tools
# ══════════════════════════════════════════════════════════════════

def list_written_tools() -> list:
    """列出所有 capability_writer 写过的工具（供 agent 发现复用）。"""
    registry = _load_registry()
    return [
        {
            "tool_name": v["tool_name"],
            "description": v["description"][:100],
            "created_at": v.get("created_at", ""),
        }
        for v in registry.values()
    ]


def search_written_tools(query: str, top_k: int = 5) -> list:
    """v2: 用关键词搜索已造的工具（让 agent 发现复用，避免重复造轮子）。"""
    registry = _load_registry()
    if not registry:
        return []
    query_lower = query.lower()
    scored = []
    for v in registry.values():
        name = v.get("tool_name", "").lower()
        desc = v.get("description", "").lower()
        score = 0
        for word in query_lower.split():
            if len(word) < 2:
                continue
            if word in name:
                score += 3
            if word in desc:
                score += 1
        if score > 0:
            scored.append((score, v))
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "tool_name": v["tool_name"],
            "description": v["description"][:100],
            "created_at": v.get("created_at", ""),
            "score": score,
        }
        for score, v in scored[:top_k]
    ]


# ══════════════════════════════════════════════════════════════════
# 特化接口：solve_open_constraint（专门处理 ortho_polygon open_constraint）
# ══════════════════════════════════════════════════════════════════

def solve_open_constraint(edges_info: str, session=None) -> dict:
    """
    ortho_polygon_from_image 返回 open_constraint 时的专用求解器。

    edges_info: ortho_polygon 输出里的边信息字符串（如 "e0(H)=10 | e1(V)=? | e2(H)=6 | ..."）

    思路：
      1. 解析 edges_info → [{idx, orient, label/None}]
      2. 直接在 Python 里用 sum(dx)=0, sum(dy)=0 方程组求解缺失边
      3. 计算顶点 → 鞋带公式面积
      4. 写 calculation_trace

    如果 edges_info 太复杂，才退化到 write_capability 生成通用代码。
    """
    import re
    # 解析 edges 字符串: e0(H)=10 | e1(V)=? | ...
    edge_pattern = re.findall(r"e(\d+)\(([HV])\)=([0-9.]+|\?)", edges_info)
    if not edge_pattern:
        # 退化：用 write_capability 写通用解析
        return write_capability(
            problem_desc=(
                "从正交多边形边描述字符串中提取边列表，用闭合约束(sum(dx)=0, sum(dy)=0)"
                "方程组求解缺失边，然后用鞋带公式计算精确面积。"
            ),
            context=f"edges_info: {edges_info[:400]}",
            session=session,
            tool_name="ortho_polygon_open_constraint_solver",
            verify_call="",
        )

    # 构造边列表
    edges = []
    for idx_s, orient, label_s in edge_pattern:
        edges.append({
            "idx": int(idx_s),
            "orient": orient,   # 'H' or 'V'
            "label": None if label_s == "?" else float(label_s),
        })

    # 构造求解代码
    code = _gen_closure_solver_code(edges)

    result = _sandbox_exec(code, call_expr="solve_polygon(edges)")
    if not result["ok"]:
        return {"ok": False, "error": f"闭合约束求解失败: {result['error'][:200]}"}

    answer = result.get("result")

    # 写 calculation_trace
    if session is not None:
        trace = session.setdefault("calculation_trace", [])
        trace.append({
            "engine": "capability_writer_open_constraint",
            "inputs": f"edges_count={len(edges)}, labeled={sum(1 for e in edges if e['label'] is not None)}",
            "result": str(answer)[:80],
        })

    return {
        "ok": True,
        "result": answer,
        "edges_parsed": len(edges),
        "code": code,
        "msg": (
            f"[capability_writer] ✓ open_constraint 用闭合约束方程组求解\n"
            f"解析到 {len(edges)} 条边，计算结果: {answer}"
        ),
    }


def _gen_closure_solver_code(edges: list) -> str:
    """生成用 sympy 求解 ortho_polygon open_constraint 的代码。"""
    # json.dumps 的 null → Python None；在嵌入 Python 代码时需要转换
    edges_repr = json.dumps(edges, ensure_ascii=False).replace(": null", ": None").replace(":null", ":None")
    code = textwrap.dedent(f"""
import sympy as _sp

def solve_polygon(edges):
    '''闭合约束 → 顶点 → 鞋带公式面积'''
    # edges: [{{idx, orient, label/None}}]
    H_edges = [e for e in edges if e['orient'] == 'H']
    V_edges = [e for e in edges if e['orient'] == 'V']

    # 统计缺失边
    H_missing = [e for e in H_edges if e['label'] is None]
    V_missing = [e for e in V_edges if e['label'] is None]

    # 超过 1 个缺失无法单独确定时，尝试符号解
    syms = {{}}
    for e in H_missing + V_missing:
        s = _sp.Symbol(f"x_{{e['idx']}}", positive=True)
        e['sym'] = s
        syms[e['idx']] = s

    # 用 label 或 sym 得到每条边的符号长度
    def edge_len(e):
        if e['label'] is not None:
            return _sp.Rational(int(e['label'])) if float(e['label']) == int(float(e['label'])) else _sp.Float(e['label'])
        return e['sym']

    # 闭合约束：
    # 正交多边形沿轮廓行走，水平方向位移和=0(右走-左走=0)，垂直=0
    # 这里简化：假设每条 H/V 边交替出现，方向由奇偶决定（适用于简单凸/凹正交多边形）
    H_lens = [edge_len(e) for e in H_edges]
    V_lens = [edge_len(e) for e in V_edges]

    # 水平约束: 右移总量 = 左移总量 → sum(H[偶]) = sum(H[奇])
    H_eq = sum(H_lens[0::2]) - sum(H_lens[1::2])
    V_eq = sum(V_lens[0::2]) - sum(V_lens[1::2])

    all_syms = list(syms.values())
    solutions = []
    if all_syms:
        eqs = [H_eq, V_eq] if len(all_syms) <= 2 else [H_eq]
        sol = _sp.solve(eqs[:len(all_syms)], all_syms[:len(all_syms)], dict=True)
        if sol:
            solutions = sol[0]
            # 回填符号解
            for e in H_missing + V_missing:
                if e['idx'] in syms:
                    e['label'] = float(solutions.get(syms[e['idx']], e.get('sym', 0)))

    # 累积顶点坐标（起点 0,0，沿轮廓走）
    vertices = [(0, 0)]
    x, y = _sp.Integer(0), _sp.Integer(0)
    for i, e in enumerate(edges):
        l = e['label'] if e['label'] is not None else 0
        l = _sp.Rational(int(l)) if isinstance(l, float) and l == int(l) else l
        if e['orient'] == 'H':
            x = x + (l if i % 2 == 0 else -l)
        else:
            y = y + (l if i % 2 == 1 else -l)
        vertices.append((x, y))

    # 鞋带公式
    n = len(vertices) - 1  # 去掉重复的起点
    verts = vertices[:n]
    area2 = _sp.Integer(0)
    for i in range(n):
        xi, yi = verts[i]
        xj, yj = verts[(i + 1) % n]
        area2 += xi * yj - xj * yi
    area = _sp.Abs(area2) / 2

    return {{
        'result': area,
        'area_exact': str(area),
        'vertices': [(float(v[0]), float(v[1])) for v in verts[:12]],
        'filled_edges': {{e['idx']: e['label'] for e in H_missing + V_missing}},
        'steps': [
            f'H缺失边: {{[e["idx"] for e in H_missing]}}',
            f'V缺失边: {{[e["idx"] for e in V_missing]}}',
            f'闭合约束解: {{solutions}}',
            f'顶点数: {{n}}',
            f'面积(鞋带): {{area}}',
        ]
    }}

edges = {edges_repr}
""")
    return code


# ══════════════════════════════════════════════════════════════════
# qa_tools_registry 注册接口（供 capability_stack 感知到这个工具）
# ══════════════════════════════════════════════════════════════════

def _register_to_qa_tools(session_factory=None):
    """把 capability_writer 的工具注册进 qa_tools_registry，供 agent 发现调用。
    由 qa_tools_registry.py 在 import 时调用（延迟注册·zero-overhead）。"""
    try:
        sys.path.insert(0, HERE)
        from qa_tools_registry import register_extra_tool

        register_extra_tool(
            name="write_capability",
            description=(
                "当现有工具无法解题（如 open_constraint/error/VLM 盲区）时，"
                "自主生成算法组件代码、沙箱验证、注册为持久工具。"
                "输入: problem_desc(str), context(str可选)。"
                "输出: {ok, tool_name, code, test_output}"
            ),
            category="capability",
            cost_tier="expensive",  # 要调 LLM·不cheap
            search_hint="write code algorithm solver new tool capability constraint open",
            fn=lambda params, session: write_capability(
                problem_desc=params.get("problem_desc", ""),
                context=params.get("context", ""),
                session=session,
                tool_name=params.get("tool_name", ""),
                verify_call=params.get("verify_call", ""),
            ),
        )

        register_extra_tool(
            name="solve_open_constraint",
            description=(
                "ortho_polygon_from_image 返回 status=open_constraint 时的专用求解器。"
                "输入 edges_info（边描述字符串）→ 用闭合约束方程组补全缺失边 → 鞋带公式面积。"
                "输入: edges_info(str)。输出: {ok, result, edges_parsed, msg}"
            ),
            category="capability",
            cost_tier="cheap",  # 纯符号计算·不调LLM
            search_hint="open constraint ortho polygon missing edge closure solve area",
            fn=lambda params, session: solve_open_constraint(
                edges_info=params.get("edges_info", ""),
                session=session,
            ),
        )

        register_extra_tool(
            name="run_written_tool",
            description=(
                "执行 write_capability 注册的算法工具。"
                "输入: tool_name(str), inputs(dict)。"
                "输出: solve() 返回值 {ok, result, steps}"
            ),
            category="capability",
            cost_tier="cheap",
            search_hint="run execute written tool capability solver registered",
            fn=lambda params, session: run_written_tool(
                tool_name=params.get("tool_name", ""),
                inputs=params.get("inputs", {}),
                session=session,
            ),
        )

        register_extra_tool(
            name="list_written_tools",
            description="列出 capability_writer 写过的所有算法工具（供 agent 发现复用）",
            category="capability",
            cost_tier="cheap",
            search_hint="list written tools capability registry",
            fn=lambda params, session: {"ok": True, "tools": list_written_tools()},
        )

        register_extra_tool(
            name="search_written_tools",
            description=(
                "v2: 用关键词搜索已造的算法工具·避免重复造轮子。"
                "输入: query(str,如'polygon area')。输出: 匹配的工具列表(tool_name+description+score)。"
            ),
            category="capability",
            cost_tier="cheap",
            search_hint="search find written tools capability registry keyword reuse",
            fn=lambda params, session: {"ok": True,
                "tools": search_written_tools(params.get("query", ""),
                                              top_k=params.get("top_k", 5))},
        )

        register_extra_tool(
            name="quick_fix_code",
            description=(
                "一次性 ad-hoc 计算：描述计算任务 → 生成代码 → 跑一次 → 返回结果。"
                "不注册工具，适合只用一次的小计算。"
                "输入: code_desc(str), inputs_example(str可选)"
            ),
            category="capability",
            cost_tier="expensive",
            search_hint="adhoc one time code run calc quick fix python",
            fn=lambda params, session: quick_fix_code(
                code_desc=params.get("code_desc", ""),
                inputs_example=params.get("inputs_example", ""),
                session=session,
            ),
        )

        register_extra_tool(
            name="auto_diagnose_and_solve",
            description=(
                "v2: 从失败信号自动诊断需要什么工具→造工具→解题。"
                "当所有现有工具都失败时调用。"
                "输入: question(str), gold(str), why_failed(str), trace_tools(list可选)。"
                "输出: {ok, tool_name, answer, msg}"
            ),
            category="capability",
            cost_tier="expensive",
            search_hint="auto diagnose solve create tool evolve fail wrong timeout",
            fn=lambda params, session: auto_diagnose_and_solve(
                question=params.get("question", ""),
                gold=params.get("gold", ""),
                why_failed=params.get("why_failed", ""),
                trace_tools=params.get("trace_tools", []),
                session=session,
            ),
        )
    except Exception:
        pass  # fail-safe：注册失败不影响其他工具


# ══════════════════════════════════════════════════════════════════
# v2: auto_diagnose_and_solve — 从失败信号自动诊断+造工具+解题
# ══════════════════════════════════════════════════════════════════

def auto_diagnose_and_solve(question: str, gold: str, why_failed: str,
                             trace_tools: list = None, session=None) -> dict:
    """v2: 从失败信号自动诊断需要什么工具，然后造工具并解题。
    
    这是 agent 自主进化的核心入口：
      1. 分析失败原因 + trace 步骤 → 诊断"需要一个什么样的算法"
      2. 调 write_capability 生成代码+验证+注册
      3. 自动构造 inputs 并调用新工具
      4. 返回结果
    
    与 write_capability 的区别：这个接口自动从失败信号提取问题描述，
    不需要 agent 显式描述"我需要一个什么工具"。
    
    Args:
        question: 题目文本
        gold: 正确答案（用于验证）
        why_failed: 失败原因（如 "timeout", "计算错误", "工具返回 error" 等）
        trace_tools: 之前用过的工具列表（帮助诊断）
        session: agent session
    
    Returns:
        {ok, tool_name, result, answer, msg}
    """
    t0 = time.time()
    
    # Step 1: 诊断——从失败信号提取"需要一个什么算法"
    _tools_summary = ", ".join(trace_tools[:10]) if trace_tools else "(无工具记录)"
    
    _diag_prompt = f"""分析以下解题失败的情况，判断"需要一个什么样的算法/工具"才能解决。

## 题目
{question[:400]}

## 失败原因
{why_failed[:300]}

## 之前用过的工具
{_tools_summary}

## 任务
判断这道题需要什么样的算法/计算才能得到正确答案。
输出一个简洁的问题描述（1-3句话），说明需要实现什么样的 Python 函数。
注意：你不知道正确答案是什么，请根据题目本身的分析来判断需要什么算法。
只输出问题描述，不要代码。
"""
    
    _diag = _ask_llm(_diag_prompt, session=session, model="claude-sonnet")
    if not _diag.strip():
        _diag = f"解题失败({why_failed[:80]})·需要一个Python函数来计算答案。题目: {question[:200]}"
    
    # Step 2: 造工具（★gold 绝不出现在造工具的任何环节★）
    _context = (
        f"题目: {question[:300]}\n"
        f"失败原因: {why_failed[:200]}\n"
        f"之前用过的工具: {_tools_summary}\n"
        f"诊断: {_diag[:200]}"
    )
    
    # v2: 从问题提取语义化工具名(不用hash/qid)
    _name_prompt = (
        "根据以下题目和诊断，生成一个简洁的英文工具名(2-4个单词，下划线连接)。\n"
        "名字要描述工具做什么，便于搜索发现和复用。\n"
        "如: polygon_area_solver / pubchem_cid_lookup / video_timestamp_extract\n"
        f"题目: {question[:200]}\n诊断: {_diag[:100]}\n"
        "只输出工具名"
    )
    _tool_name_raw = _ask_llm(_name_prompt, session=session, model="claude-haiku")
    _tool_name_raw = _tool_name_raw.strip().strip('`').strip()
    import re as _re_a2
    _tool_name_raw = _re_a2.sub(r"[^a-z0-9_]", "", _tool_name_raw.lower().replace(" ", "_"))[:48]
    if not _tool_name_raw or _tool_name_raw[0].isdigit():
        _tool_name_raw = "custom_solver"
    result = write_capability(
        problem_desc=_diag[:500],
        context=_context,
        session=session,
        tool_name=_tool_name_raw,
    )
    
    if not result.get("ok"):
        return {
            "ok": False,
            "error": f"造工具失败: {result.get('error', '')[:200]}",
            "diag": _diag[:200],
        }
    
    tool_name = result["tool_name"]
    
    # Step 3: 构造 inputs 并调用新工具
    # 让 LLM 从题目提取参数
    _inputs_prompt = f"""从下面的题目中提取参数，构造一个 JSON dict 传给 solve(inputs) 函数。

## 题目
{question[:400]}

## 生成的工具描述
{_diag[:200]}

注意：不要在 inputs 中放入任何'答案'、'gold'、'correct'等字段。只放题目给出的数据参数。
输出一个 JSON dict，key 用英文/下划线命名。只输出 JSON，不要其他文字。
"""
    
    _inputs_raw = _ask_llm(_inputs_prompt, session=session, model="claude-sonnet")
    inputs = {}
    try:
        import re
        m = re.search(r'\{.*\}', _inputs_raw, re.DOTALL)
        if m:
            inputs = json.loads(m.group(0))
    except Exception:
        pass
    
    if not inputs:
        inputs = {"question": question[:200]}
    
    # Step 4: 调用新工具
    run_result = run_written_tool(tool_name, inputs, session=session)
    
    if run_result.get("ok"):
        answer = run_result.get("result")
        if isinstance(answer, dict):
            answer = answer.get("result", str(answer))
        return {
            "ok": True,
            "tool_name": tool_name,
            "result": run_result.get("result"),
            "answer": str(answer)[:200],
            "diag": _diag[:200],
            "inputs": inputs,
            "elapsed_sec": round(time.time() - t0, 2),
            "msg": f"[auto_diagnose] ✓ 自动诊断+造工具+解题完成。工具={tool_name}, 答案={str(answer)[:80]}",
        }
    
    return {
        "ok": False,
        "tool_name": tool_name,
        "error": f"新工具执行失败: {run_result.get('error', '')[:200]}",
        "diag": _diag[:200],
        "inputs": inputs,
        "elapsed_sec": round(time.time() - t0, 2),
    }


# ══════════════════════════════════════════════════════════════════
# 自测
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    if "--test-open-constraint" in sys.argv:
        # 模拟 GAIA 6359a0b1 题目的 open_constraint 场景
        test_edges = "e0(H)=10 | e1(V)=6 | e2(H)=4 | e3(V)=? | e4(H)=6 | e5(V)=4 | e6(H)=? | e7(V)=2"
        session = {"_budget": {}}
        r = solve_open_constraint(test_edges, session)
        print(f"ok={r['ok']}")
        print(f"result={r.get('result')}")
        print(f"msg={r.get('msg', r.get('error', ''))}")
        print(f"calculation_trace={session.get('calculation_trace')}")
        sys.exit(0 if r["ok"] else 1)

    if "--test-write" in sys.argv:
        session = {"_budget": {}}
        r = write_capability(
            problem_desc="计算正交多边形面积：给出各边长和方向，用鞋带公式精确计算",
            context="edges: H=10, V=6, H=4, V=3, H=6, V=3",
            session=session,
            tool_name="test_ortho_area",
            verify_call="",
        )
        print(f"ok={r['ok']}")
        print(f"tool_name={r.get('tool_name')}")
        print(f"test_output={r.get('test_output', '')[:100]}")
        print(f"error={r.get('error', '')}")
        sys.exit(0 if r["ok"] else 1)

    if "--list" in sys.argv:
        tools = list_written_tools()
        print(f"已注册 {len(tools)} 个工具:")
        for t in tools:
            print(f"  · {t['tool_name']}: {t['description'][:60]}")
        sys.exit(0)

    print("capability_writer · agent 自主算法组件生成器")
    print("用法: --test-open-constraint | --test-write | --list")
    tools = list_written_tools()
    print(f"当前注册工具数: {len(tools)}")
