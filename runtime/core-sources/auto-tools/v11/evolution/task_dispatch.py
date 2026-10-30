#!/usr/bin/env python3
"""
task_dispatch · 真子代理隔离层 (对齐 deepagents SubAgentMiddleware 的 task() 语义)

用户抓正 (2026-08-06):
「其他他们的核心优势你也得学习」——深挖 deepagents/middleware/subagents.py
发现 CC/DeepAgents 真正的子代理设计核心不是"能不能调工具"，而是三条铁律:

  1. 隔离 (Isolation)：子代理有独立的 messages/state，跑多少轮工具调用都
     不占用主 agent 的上下文窗口。
         > "The calling agent only sees your final assistant message, not
         >  your intermediate work, tool results, or status tracking."
  2. 汇报契约 (Report Contract)：子代理必须自己把"完整答案"塞进最后一句
     话/structured_response，因为主 agent 看不到过程，只能看这一句。
  3. 并发派发 (Concurrent Dispatch)：多个独立子任务可以一条消息内并发派发
     (CC 的 task 工具支持一次消息发多个 tool_call)。

现状问题 (meta_graph.py):
    MetaGraph.run() 是同一个 Python 进程里，顺序地直接调用
    self.deduction.deconstruct(goal) → self.search.search(...) → ...
    每一步的**全量返回值**都被塞进同一个 result dict，一路带到底。这是
    "模块化函数管道"，不是"子代理"——没有隔离、没有强制的"只回传摘要"契约，
    大结果(比如 Search 抓回的证据全文)会一路撑大 MetaGraph 的内存态和最终
    log/trace。

本模块思路 (不引入 LangGraph，保持跟 280 模块生态零耦合，只加一层调用约定):

    from task_dispatch import dispatch_task, dispatch_concurrent

    # 单个隔离任务：跑在独立进程里(真正隔离，互不干扰内存态/全局变量)，
    # 只拿回一句"最终汇报"文本 + 结构化产出的 context_store 引用
    report = dispatch_task(
        agent_name="Search",
        fn_path="search_agent.SearchAgent.search",   # 模块.类.方法 或 模块.函数
        args=("RAG 评测怎么做",),
        kwargs={"mode": "balanced", "top_k": 10},
        task_id="search_001",
    )
    # report = {ok, summary, ref_id, full_result_available, elapsed_s}
    # summary 是可以直接喂给上游 LLM/agent 的一句话汇报
    # 全量结果通过 context_store.read_back(ref_id) 按需读回，不占上游上下文

    # 多个独立子任务并发派发 (对齐 CC "一条消息内并发多个 task 调用")
    reports = dispatch_concurrent([
        {"agent_name": "Search", "fn_path": "...", "args": (...,), "task_id": "s1"},
        {"agent_name": "Deduction", "fn_path": "...", "args": (...,), "task_id": "d1"},
    ])

设计取舍:
- 用 multiprocessing 而非 LangGraph/线程：保证真正的内存隔离(子任务哪怕
  死循环/内存泄漏也不影响主进程)，且和现有 280 模块的全局变量/单例惯用法
  完全兼容(它们大多假设"自己独占一个进程")。
- 子任务产出统一走 context_store：无论子任务返回值多大，主进程只拿到
  "一句话摘要 + ref_id"，符合 deepagents 的隔离契约。
- 仍然保留"内联模式"(dispatch_inline)：轻量任务(比如 deduction 的几十ms
  操作)不值得开进程，MetaGraph 可以按 cost_tier 自己选。
"""
import json
import multiprocessing as mp
import os
import sys
import time
import traceback
from typing import Any, Callable, Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
V11 = os.path.dirname(_DIR)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

TRACE_LOG = os.path.join(V11, ".task_dispatch_trace.jsonl")


def _resolve_callable(fn_path: str) -> Callable:
    """把 "module.Class.method" 或 "module.function" 解析成可调用对象。"""
    import importlib
    parts = fn_path.split(".")
    # 尝试从长到短匹配模块名 (module 可能本身含 . ，虽然本项目基本是扁平模块)
    for split_at in range(len(parts) - 1, 0, -1):
        mod_name = ".".join(parts[:split_at])
        attr_chain = parts[split_at:]
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        obj = mod
        try:
            for a in attr_chain[:-1]:
                obj = getattr(obj, a)
                if isinstance(obj, type):
                    obj = obj()  # 类 → 实例化 (门面类都是无参构造)
            return getattr(obj, attr_chain[-1])
        except AttributeError:
            continue
    raise ImportError(f"无法解析 fn_path='{fn_path}'，检查模块/类/方法名是否正确")


def _summarize_result(result: Any, max_len: int = 300) -> str:
    """把任意返回值压成一句话摘要，供上游 agent/LLM 直接消费。"""
    if result is None:
        return "(无返回值)"
    if isinstance(result, dict):
        keys = list(result.keys())[:6]
        pieces = []
        for k in keys:
            v = result[k]
            if isinstance(v, (list, dict)):
                pieces.append(f"{k}={len(v)}项")
            else:
                pieces.append(f"{k}={str(v)[:40]}")
        return "、".join(pieces)[:max_len]
    if isinstance(result, list):
        return f"共 {len(result)} 项" + (f"，首项: {str(result[0])[:100]}" if result else "")
    return str(result)[:max_len]


def _worker_entry(fn_path, args, kwargs, out_path):
    """子进程入口：跑目标函数，把 {ok, result/error} 写到 out_path (JSON)。
    必须是模块级函数(不能是闭包)，multiprocessing spawn/fork 都能 pickle。"""
    if _DIR not in sys.path:
        sys.path.insert(0, _DIR)
    out = {"ok": False, "result": None, "error": ""}
    t0 = time.time()
    try:
        fn = _resolve_callable(fn_path)
        result = fn(*args, **(kwargs or {}))
        out["ok"] = True
        out["result"] = result
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}"
    out["elapsed_s"] = round(time.time() - t0, 2)
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, default=str)
    except Exception:
        # 兜底：写不进去就写个最小错误标记，主进程按超时/异常处理
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"ok": False, "error": "结果无法JSON序列化", "result": None}, f)
        except Exception:
            pass


import random as _random
import threading as _threading

_jitter_counter = 0
_jitter_lock = _threading.Lock()


def _jittered_backoff(attempt: int, *, base_delay: float = 2.0, max_delay: float = 8.0,
                       jitter_ratio: float = 0.5) -> float:
    """抖动指数退避 · 对齐 hermes agent/retry_utils.jittered_backoff。

    G-align(2026-08-07) · 之前是固定 min(2**attempt, 8),多个 delegate 子任务
    并发失败(尤其多用户 web 入口下)会在同一秒一起重试,撞同一个过载窗口
    (thundering-herd)。改成带去相关计数器的抖动退避:同样的指数上限,但每次
    重试的实际 sleep = delay + 随机抖动,seed 用 time_ns ^ counter 去相关,
    多进程/多线程并发也不会撞同一时刻。base/max 保持原来的 2s/8s 量级不变。
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter
    exponent = max(0, attempt - 1)
    delay = min(base_delay * (2 ** exponent), max_delay)
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = _random.Random(seed)
    return delay + rng.uniform(0, jitter_ratio * delay)


def dispatch_task(agent_name: str, fn_path: str, args: tuple = (),
                   kwargs: Optional[dict] = None, task_id: str = "",
                   timeout_s: float = 120, isolate: bool = True,
                   retries: int = 0) -> dict:
    """派发一个隔离子任务，对齐 deepagents task() 的语义。

    Args:
        agent_name: 门面 agent 名 (仅用于 trace/日志，比如 "Search")
        fn_path: 目标可调用对象路径，"module.Class.method" 或 "module.function"
        args/kwargs: 透传给目标函数的参数
        task_id: 任务标识 (用于 trace 关联和 context_store ref 前缀)
        timeout_s: 子进程超时，超时视为失败但不阻塞主进程
        isolate: True=开独立进程真隔离(默认，对齐CC语义) / False=当前进程内联跑
                 (给足够轻量、不需要隔离的调用用，比如几十ms的纯函数)
        retries: v59 · 借鉴 hermes-web-ui GatewayManager 的 Self-Healing 模式
                 (指数退避重连) ——mars-data/docs/hermes-webui-comparison.md
                 里已验证过的模式，这里适配成"任务失败按抖动指数退避重试"：
                 第 i 次重试前 sleep _jittered_backoff(i)(带去相关抖动,防
                 多子任务/多用户并发同秒重试撞同一过载窗口)。0=不重试(默认，保持
                 旧行为不变)。超时(拿不到子进程结果)也算失败，会重试；
                 但重试次数用尽后仍然老实返回失败，不无限重试掩盖真实故障。

    Returns:
        {ok, summary, ref_id, elapsed_s, agent_name, task_id, attempts}
        - summary: 一句话汇报，直接可喂给上游 LLM/agent (对齐 CC "只看最后一句")
        - ref_id: 完整结果的 context_store 引用，上游需要细节时
          context_store.read_back(ref_id) 按需读取，不会撑爆上游上下文
        - attempts: 实际尝试次数(1 + 触发的重试次数)
    """
    task_id = task_id or f"{agent_name}_{int(time.time() * 1000)}"

    last_report = None
    for attempt in range(max(0, retries) + 1):
        if attempt > 0:
            time.sleep(_jittered_backoff(attempt))
        last_report = _dispatch_task_once(agent_name, fn_path, args, kwargs,
                                            f"{task_id}_try{attempt}" if attempt else task_id,
                                            timeout_s, isolate)
        last_report["attempts"] = attempt + 1
        if last_report["ok"]:
            return last_report
    return last_report


def _dispatch_task_once(agent_name: str, fn_path: str, args: tuple,
                          kwargs: Optional[dict], task_id: str,
                          timeout_s: float, isolate: bool) -> dict:
    """单次派发(不含重试)，被 dispatch_task 的重试循环调用。"""
    t0 = time.time()

    if not isolate:
        # 内联模式：不开进程，直接调用 (轻量任务用，仍然做摘要+落盘处理，
        # 保证跟隔离模式返回的数据形状一致，调用方不用区分)
        try:
            fn = _resolve_callable(fn_path)
            result = fn(*args, **(kwargs or {}))
            ok, err = True, ""
        except Exception as e:
            result, ok = None, False
            err = f"{type(e).__name__}: {e}"
        elapsed = round(time.time() - t0, 2)
        return _finalize_report(agent_name, task_id, ok, result, err, elapsed)

    # 隔离模式：独立子进程跑，主进程只等结果文件
    out_path = os.path.join(V11, ".task_dispatch_tmp", f"{task_id}.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    ctx = mp.get_context("spawn" if sys.platform == "darwin" else "fork")
    proc = ctx.Process(target=_worker_entry, args=(fn_path, args, kwargs, out_path))
    proc.start()
    proc.join(timeout=timeout_s)

    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)
        elapsed = round(time.time() - t0, 2)
        return _finalize_report(agent_name, task_id, False, None,
                                 f"任务超时({timeout_s}s)已终止", elapsed)

    elapsed = round(time.time() - t0, 2)
    if not os.path.exists(out_path):
        return _finalize_report(agent_name, task_id, False, None,
                                 "子进程未产出结果(可能崩溃)", elapsed)
    try:
        data = json.load(open(out_path, encoding="utf-8"))
    except Exception as e:
        return _finalize_report(agent_name, task_id, False, None,
                                 f"结果文件解析失败: {e}", elapsed)
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass

    return _finalize_report(agent_name, task_id, data.get("ok", False),
                             data.get("result"), data.get("error", ""), elapsed)


def _finalize_report(agent_name: str, task_id: str, ok: bool, result: Any,
                      error: str, elapsed: float) -> dict:
    """统一收口：产出摘要 + context_store 落盘引用 + trace 记录。"""
    ref_id = ""
    summary = ""
    if ok:
        summary = _summarize_result(result)
        try:
            from context_store import offload
            full_text = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
            ref = offload(f"task_{agent_name}", full_text, tag=f"dispatch:{task_id}")
            ref_id = ref.get("ref_id", "")
        except Exception:
            pass
    else:
        summary = f"任务失败: {error[:200]}"

    report = {
        "ok": ok, "summary": summary, "ref_id": ref_id,
        "elapsed_s": elapsed, "agent_name": agent_name, "task_id": task_id,
        "result_inline": result if (ok and _is_small(result)) else None,
        # result_inline: 小结果(对齐 deepagents 阈值以下)直接内联返回，省一趟
        # read_back 往返；大结果为 None，逼调用方走 ref_id 读回，符合隔离契约
    }

    try:
        from agent_comm import emit
        emit("TASK_DISPATCH", agent_name,
             f"{'✅' if ok else '❌'} {task_id} · {summary[:80]}",
             {"task_id": task_id, "ok": ok, "elapsed_s": elapsed, "ref_id": ref_id})
    except Exception:
        pass

    try:
        with open(TRACE_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **{k: v for k, v in report.items() if k != "result_inline"}},
                                ensure_ascii=False) + "\n")
    except Exception:
        pass

    return report


def _is_small(result: Any, threshold: int = 2000) -> bool:
    try:
        s = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
        return len(s) <= threshold
    except Exception:
        return False


def dispatch_concurrent(tasks: list) -> list:
    """并发派发多个独立子任务 (对齐 CC "一条消息内并发多个 task 调用")。

    Args:
        tasks: [{agent_name, fn_path, args, kwargs, task_id, timeout_s}, ...]

    Returns:
        [report, ...] 顺序对应 tasks 顺序 (即便并发执行，结果按输入顺序返回)
    """
    procs = []
    out_paths = []
    task_ids = []
    t0 = time.time()
    os.makedirs(os.path.join(V11, ".task_dispatch_tmp"), exist_ok=True)
    ctx = mp.get_context("spawn" if sys.platform == "darwin" else "fork")

    for t in tasks:
        task_id = t.get("task_id") or f"{t['agent_name']}_{int(time.time() * 1000)}_{len(procs)}"
        out_path = os.path.join(V11, ".task_dispatch_tmp", f"{task_id}.json")
        proc = ctx.Process(target=_worker_entry,
                            args=(t["fn_path"], t.get("args", ()), t.get("kwargs"), out_path))
        proc.start()
        procs.append((proc, t.get("timeout_s", 120)))
        out_paths.append(out_path)
        task_ids.append(task_id)

    reports = []
    for (proc, timeout_s), out_path, task_id, t in zip(procs, out_paths, task_ids, tasks):
        remaining = max(0.1, timeout_s - (time.time() - t0))
        proc.join(timeout=remaining)
        agent_name = t["agent_name"]
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
            reports.append(_finalize_report(agent_name, task_id, False, None,
                                             f"任务超时({timeout_s}s)已终止",
                                             round(time.time() - t0, 2)))
            continue
        if not os.path.exists(out_path):
            reports.append(_finalize_report(agent_name, task_id, False, None,
                                             "子进程未产出结果(可能崩溃)",
                                             round(time.time() - t0, 2)))
            continue
        try:
            data = json.load(open(out_path, encoding="utf-8"))
        except Exception as e:
            data = {"ok": False, "error": f"结果文件解析失败: {e}"}
        finally:
            try:
                os.remove(out_path)
            except Exception:
                pass
        reports.append(_finalize_report(agent_name, task_id, data.get("ok", False),
                                         data.get("result"), data.get("error", ""),
                                         data.get("elapsed_s", round(time.time() - t0, 2))))
    return reports


# ══════════════════════════════════════════════════════════════════
# 注册为 evolution_tool_registry 工具
# ══════════════════════════════════════════════════════════════════

def _register_self():
    try:
        from evolution_tool_registry import register_evolution_tool

        @register_evolution_tool(
            name="task_dispatch_status",
            description="查看最近的隔离子任务派发记录(哪些任务跑了/耗时/成功率)",
            category="meta",
            cost_tier="cheap",
            search_hint="task dispatch subagent isolation trace status",
            module="task_dispatch",
            is_deferred=True,
        )
        def _status_tool(params):
            n = params.get("n", 10)
            if not os.path.exists(TRACE_LOG):
                return {"ok": True, "result": []}
            lines = open(TRACE_LOG, encoding="utf-8").read().strip().splitlines()
            recent = [json.loads(l) for l in lines[-n:] if l.strip()]
            return {"ok": True, "result": recent}
    except Exception:
        pass


# 模块级触发注册：之前 _register_self() 只在 `python task_dispatch.py status`
# 这个 CLI 分支里被调用，普通 `import task_dispatch`（真实运行时的加载方式）
# 根本不会执行到它，导致 task_dispatch_status 工具实际从未进入统一注册表——
# 属于"看起来接好了、实测查不到"的假修复。现在改成模块加载即注册，和
# context_store/plan_state/rubric_gate/skill_library/memory_layers 保持同一
# 修复模式。
_register_self()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["smoke", "status"])
    args = p.parse_args()

    if args.cmd == "status":
        if os.path.exists(TRACE_LOG):
            lines = open(TRACE_LOG, encoding="utf-8").read().strip().splitlines()
            for l in lines[-10:]:
                r = json.loads(l)
                print(f"  [{'OK' if r['ok'] else 'FAIL'}] {r['agent_name']}/{r['task_id']} "
                      f"· {r['elapsed_s']}s · {r['summary'][:60]}")
        else:
            print("暂无派发记录")

    elif args.cmd == "smoke":
        print("=== 单任务隔离派发 smoke test ===")
        # 用一个必然存在的轻量函数做冒烟：os.path.exists 太简单不够代表性，
        # 用 agent_facades.DeductionAgent.deconstruct 做真实调用
        r = dispatch_task(
            agent_name="Deduction",
            fn_path="agent_facades.DeductionAgent.deconstruct",
            args=("测试系统健康度",),
            task_id="smoke_deduction",
            timeout_s=60,
        )
        print(f"  ok={r['ok']} summary={r['summary'][:100]}")
        print(f"  ref_id={r['ref_id']} elapsed={r['elapsed_s']}s")
        assert r["ok"], f"隔离派发应成功: {r}"

        print("\n=== 并发派发 smoke test ===")
        reports = dispatch_concurrent([
            {"agent_name": "Deduction", "fn_path": "agent_facades.DeductionAgent.deconstruct",
             "args": ("并发测试A",), "task_id": "smoke_concurrent_a"},
            {"agent_name": "Deduction", "fn_path": "agent_facades.DeductionAgent.deconstruct",
             "args": ("并发测试B",), "task_id": "smoke_concurrent_b"},
        ])
        for r in reports:
            print(f"  [{r['task_id']}] ok={r['ok']} · {r['elapsed_s']}s · {r['summary'][:60]}")
        assert all(r["ok"] for r in reports), "并发派发应全部成功"
        print("\n✅ task_dispatch 自测通过")
