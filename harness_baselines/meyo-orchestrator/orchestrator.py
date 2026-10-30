# -*- coding: utf-8 -*-
"""自主 captain 的【工具层】—— 不替模型做任何决定 (2026-08-25 重写)

用户铁律:
- 是否需要 agent、几个 agent、谁做什么、目标/验收、是否复核、何时停止 —— 全由模型自主。
- 判断来自模型自己读各家代码/历史 trace/多次执行结果持续磨合,不是人写 if-else。
- 本模块只给模型 3 类客观工具:list / read_history / run。它不包含 recommend、能力评分、
  cost_tier、strong_on、固定 reviewer、固定 captain 或任务拆解规则。

接 omnigent 后,这些函数注册成 supervisor 的工具。模型拿到目标后可自主:
  1) list_harnesses 看有哪些
  2) read_harness_history 读任意家的真实历史(或 read 文件/源码)
  3) run_harness 自己决定要不要跑/跑谁/跑几个/并行还是串行
  4) 根据返回继续行动或结束

这才是"大脑":经验在 trace/代码/模型上下文中积累,编排策略不是硬编码。
"""
from __future__ import annotations
import json, sys, os
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from meyo_harnesses import MEYO_HARNESSES

BASELINES = Path(__file__).resolve().parent.parent
TRACE_ROOT = BASELINES / "fair_bench_40" / "traces"


def list_harnesses() -> list[dict[str, Any]]:
    """客观列出军团成员 + 当前是否真可执行。

    2026-08-27 上线修:catalog 原写死13家·dist standalone池只有部分家→前端展示 auto
    但点了返回 unknown_harness(假家·很不爽)。现在以真实执行池为准标 available；家仍可见
    (让用户知道可扩展能力)但不可用的前端可置灰·绝不冒充能跑。"""
    try:
        runnable = set(_load_harnesses())
    except Exception:
        runnable = set()

    # auto 特判:它需完整仓库的 run_auto_thin.py(精简 dist 不打包 10k 行内核)。
    # runnable 集里有 auto 不代表真能跑——探测内核文件存在与否,给用户真实可用状态,
    # 否则前端显示可用、用户点了必失败(很不爽)。完整仓库文件在则正常可用。
    def _really_available(key: str) -> bool:
        if key != "auto":
            return key in runnable
        try:
            import os as _os
            _here = _os.path.dirname(_os.path.abspath(__file__))
            _thin = _os.path.join(_here, "..", "..", "seed-users-system",
                                  "qa_system", "agent", "run_auto_thin.py")
            return (key in runnable) and _os.path.exists(_thin)
        except Exception:
            return False

    rows = []
    known = set()
    try:
        service = Path(__file__).resolve().parents[2] / "release" / "legion-service"
        if str(service) not in sys.path:
            sys.path.insert(0, str(service))
        import runtime_manifest as _manifest
        import harness_registry as _hreg
        pool = set(_hreg.load_harness_pool())
        for key, item in _manifest.load_definitions().items():
            profile = item.get("profile") or {}
            runtime = item.get("runtime") or {}
            protocols = [str(row.get("type")) for row in runtime.get("transports") or [] if row.get("type")]
            rows.append({"key": key, "display_name": profile.get("name") or item.get("name") or key,
                         "cli_hint": item.get("adapter") if item.get("source") == "builtin" else
                                     ("/".join(protocols) or "external"),
                         "available": key in pool, "external": item.get("source") == "external",
                         "version": runtime.get("version"), "capabilities": runtime.get("capabilities") or [],
                         "manifest_valid": item.get("manifest_valid", False)})
            known.add(key)
    except Exception:
        rows = [{"key": h.key, "display_name": h.display_name, "cli_hint": h.cli_hint,
                 "available": _really_available(h.key)} for h in MEYO_HARNESSES.values()]
        known = {row["key"] for row in rows}
    return rows


def read_harness_history(harness: str, limit: int = 20) -> list[dict[str, Any]]:
    """读取某家的真实历史执行结果,供模型【自己】形成判断。

    原样给客观字段:qid/hit/status/searches/elapsed/final/trace_path。不替模型总结谁强谁弱。
    模型可反复调用、比较多家、读具体 trace,从累计执行里磨合出自己的策略。
    """
    rows = []
    for p in sorted(TRACE_ROOT.glob(f"*/{harness}/latest.json")):
        try:
            d = json.loads(p.read_text())
            ev, r = d.get("evaluation", {}), d.get("result", {})
            rows.append({
                "qid": d.get("qid"), "hit": ev.get("hit"),
                "status": r.get("status"), "search_count": ev.get("search_count"),
                "elapsed": r.get("elapsed"), "final": str(r.get("final", ""))[:300],
                "trace_path": str(p),
            })
        except Exception:
            continue
    return rows[-max(1, min(int(limit or 20), 100)):]


def harness_stats() -> list[dict[str, Any]]:
    """全部 harness 的【客观聚合统计】——给模型全局视角,但不做推荐/排序建议。

    给命中率/中位耗时/中位搜索数(和人看汇总表一样的原料),模型自己权衡性价比。
    这不是路由规则:不含"该选谁"、不含 cost_tier/strong_on 标签,只有可复现的统计事实。
    """
    from statistics import median
    agg: dict[str, dict] = {}
    for p in TRACE_ROOT.glob("*/*/latest.json"):
        try:
            d = json.loads(p.read_text())
            h = d.get("harness")
            if not h:
                continue
            ev, r = d.get("evaluation", {}), d.get("result", {})
            a = agg.setdefault(h, {"n": 0, "hit": 0, "els": [], "srch": []})
            a["n"] += 1
            a["hit"] += 1 if ev.get("hit") else 0
            if isinstance(r.get("elapsed"), (int, float)):
                a["els"].append(r["elapsed"])
            if isinstance(ev.get("search_count"), (int, float)):
                a["srch"].append(ev["search_count"])
        except Exception:
            continue
    out = []
    for h, a in agg.items():
        out.append({
            "harness": h, "n_samples": a["n"],
            "hit_rate": round(a["hit"] / a["n"], 3) if a["n"] else None,
            "median_elapsed_s": round(median(a["els"]), 1) if a["els"] else None,
            "median_searches": round(median(a["srch"]), 1) if a["srch"] else None,
        })
    # 只按 harness 名排序(稳定)·不按"谁更好"排·避免暗示推荐
    return sorted(out, key=lambda x: x["harness"])


def read_trace(trace_path: str) -> dict:
    """模型自主深读某次完整 trace(路径来自 read_harness_history)。"""
    try:
        p = Path(trace_path).resolve()
        # 只允许读 harness_baselines 下的 trace,防任意文件读取
        if BASELINES.resolve() not in p.parents or not p.is_file():
            return {"error": "trace_path_not_allowed"}
        return json.loads(p.read_text())
    except Exception as e:
        return {"error": f"{type(e).__name__}:{str(e)[:120]}"}


def _load_harnesses(harness: str | None = None):
    """加载执行入口；指定 key 时不先查询全目录健康。

    军团执行不再 import run_comparison benchmark入口。adapter 未装/缺配置时返回
    unavailable·不假跑；外部后端用 setdefault 补充、不覆盖原生9家。
    不传 key 保留原全目录查询；取得目标 callable 不代表该家已 ready。
    """
    _reg_dir = os.path.dirname(os.path.abspath(__file__))
    if _reg_dir not in sys.path:
        sys.path.insert(0, _reg_dir)
    try:
        import harness_registry as _hreg
        if harness is None:
            pool = _hreg.load_harness_pool()
        else:
            runner = _hreg.resolve_harness_adapter(harness)
            if runner is not None:
                return {harness: runner}
            pool = {}
    except Exception:
        pool = {}
    try:
        _mh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "meyo_harnesses")
        if _mh not in sys.path:
            sys.path.insert(0, _mh)
        from backends import BACKEND_HARNESSES
        for k, fn in BACKEND_HARNESSES.items():
            if harness is None or k == harness:
                pool.setdefault(k, fn)   # 不覆盖已有同名家
    except Exception:
        pass
    return pool


def judge_answer(question: str, candidate_answer: str, judge_harness: str = "cc",
                 timeout_s: int = 120) -> dict:
    """让裁判 harness 独立复核候选答案。模型自主决定用不用/用谁。

    裁判也必须有硬墙钟:历史 ~22s 不代表当前必然快;本次 live E2E 暴露 auto >210s 卡死。
    timeout 默认120s(可由模型收紧,上限300s),超时诚实返回 judge_timeout,不拖死 captain。
    """
    import threading, time as _t
    task = (f"另一个 agent 对下面问题给出了答案。请【独立复核】此答案是否正确:"
            f"逐条核对题目每个约束是否被满足;若错·给出你认为正确的答案。\n"
            f"格式:VERDICT: CORRECT 或 VERDICT: WRONG + 正确答案。\n\n"
            f"问题:{question}\n\n候选答案:{candidate_answer}")
    box = {"out": None}
    def _call():
        box["out"] = run_harness(judge_harness, task, qhash="judge", gold="")
    t0 = _t.time(); th = threading.Thread(target=_call, daemon=True); th.start()
    limit = max(1, min(int(timeout_s or 120), 300))
    th.join(limit)
    if th.is_alive():
        return {"judge": judge_harness, "verdict": "", "status": "judge_timeout",
                "elapsed": round(_t.time() - t0, 1),
                "note": f"裁判超过{limit}s·已熔断;模型可换裁判/放弃复核"}
    out = box["out"] or {}
    return {"judge": judge_harness, "verdict": str(out.get("final", ""))[:500],
            "status": out.get("status"), "elapsed": round(_t.time() - t0, 1)}


def probe_harness_latency(harness: str, timeout_s: int = 30) -> dict:
    """对某家做极小实时探针(2+2),让模型【自己】测当前速度/健康再决定派谁。

    不自动排序/推荐,只返回客观 elapsed/status/final。timeout默认30s·慢家会快速暴露,
    不再靠过时历史中位数误判(deepseek实测连2+2>120s·penguin3.3s/codex4.8s)。
    """
    import threading, time as _t
    box = {"out": None}
    def _call():
        box["out"] = run_harness(harness, "What is 2+2? Answer with just the number.",
                                 qhash="probe", gold="")
    t0 = _t.time(); th = threading.Thread(target=_call, daemon=True); th.start()
    th.join(timeout=max(1, min(int(timeout_s or 30), 60)))
    elapsed = round(_t.time() - t0, 1)
    if th.is_alive():
        return {"harness": harness, "status": "probe_timeout", "elapsed_s": elapsed,
                "note": "当前实时延迟高·模型自行决定是否避开/再试"}
    out = box["out"] or {}
    return {"harness": harness, "status": out.get("status"), "elapsed_s": elapsed,
            "final": str(out.get("final", ""))[:80]}


def run_harness(harness: str, task: str, qhash: str = "adhoc", gold: str = "", ctx=None) -> dict:
    """真调任意一家。模型自己决定 harness/目标/调用次数/顺序。

    本函数不自动加 reviewer、不自动重试、不强制角色 —— 这些决策由模型根据结果自行作出。
    返回原始 result + 客观 harness/qhash 字段,供模型继续判断。

    ctx(HarnessCtx·可选·2026-08-28):per-call 运行时上下文(model/appid/方向/session/失败历史)。
    渐进迁移:已 ctx 化的 adapter(签名含 ctx=)按 kwarg 透传;未改造的老家零改动仍走
    (qhash,task,gold)。这样 10 家可一家家迁,不用一次性全改(双路径对称·不断老调用)。
    """
    pool = _load_harnesses(harness)
    if harness not in pool:
        # catalog 已知(如 auto·需完整仓库/未装CLI)但当前执行池没有 → 诚实 unavailable·
        #   不冒充 unknown_harness(那会让用户以为家名打错)。完全没听过的才 unknown。
        _known = harness in MEYO_HARNESSES
        return {"status": "unavailable" if _known else "unknown_harness",
                "harness": harness, "available": list(pool),
                "note": ("该家在当前部署不可执行(需完整仓库或未装对应CLI)·换一个可用家"
                         if _known else "未知家名")}
    try:
        fn = pool[harness]
        _accepts_ctx = False
        if ctx is not None:
            try:
                import inspect as _inspect
                _accepts_ctx = "ctx" in _inspect.signature(fn).parameters
            except (TypeError, ValueError):
                _accepts_ctx = False  # 内建/C 函数拿不到签名 → 当老家处理
        if _accepts_ctx:
            result = fn(qhash, task, gold, ctx=ctx)   # 已迁移的家:走 ctx per-call model
        else:
            result = fn(qhash, task, gold)            # 老家:零改动
        if not isinstance(result, dict):
            result = {"final": str(result), "status": "ok"}
        return {"harness": harness, "qhash": qhash, **result}
    except Exception as e:
        return {"harness": harness, "qhash": qhash,
                "final": "", "status": f"err:{type(e).__name__}:{str(e)[:160]}"}


def run_self_evolve(harness: str, objective: str, qhash: str = "adhoc",
                    gold: str = "", token_budget: str = "300k") -> dict:
    """启用 penguin/prime 的【原生自进化 goal 模式】:agent 自己形成子目标并迭代到完成。

    这不是我们编排 —— 是调用 agent 自带的自进化 loop(penguin --goal / prime --goal)。
    仅 penguin/prime 支持;其他家返回 unsupported(由模型自行改用 single 模式)。
    objective 是高层目标(agent 自己拆子目标),qhash/gold 外层固定。
    """
    import shutil, subprocess, time as _t
    from pathlib import Path as _P
    t0 = _t.time()
    env = dict(os.environ); env["MEYO_FAIR_QID"] = qhash
    search_cli = str(BASELINES / "comparison" / "search_fabric_cli.py")
    prompt = (f"用统一搜索(python3.12 '{search_cli}' \"QUERY\")完成目标。"
              f"你自主拆解子目标并迭代到每条约束被证据独立支持。\n\n目标:{objective}")
    try:
        if harness == "penguin":
            binp = shutil.which("penguin") or str(_P.home() / ".penguin/bin/penguin")
            # 原生 goal 模式:loop until goal completes(自进化核心)
            cmd = [binp, "run", "-m", prompt, "--provider", "custom",
                   "--model-id", "gpt-5.6-sol", "--approve", "allow-all",
                   "--goal", token_budget]
        elif harness == "prime":
            binp = shutil.which("prime-agent") or str(_P.home() / ".npm-global/bin/prime-agent")
            budget_n = "300000"
            cmd = [binp, "-p", prompt, "--mode", "json", "--provider", "meyo-aigc",
                   "--model", "gpt-5.6-sol", "--no-session",
                   "--goal", objective[:200], "--goal-token-budget", budget_n]
        else:
            return {"harness": harness, "status": "self_evolve_unsupported",
                    "final": "", "note": "仅 penguin/prime 支持自进化 goal 模式·改用 single"}
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1200, env=env)
        return {"harness": harness, "qhash": qhash, "mode": "self_evolve",
                "final": (r.stdout or "").strip()[-2000:],
                "status": "ok" if r.returncode == 0 else f"rc{r.returncode}",
                "elapsed": round(_t.time() - t0, 1),
                "stdout": (r.stdout or "")[-8000:], "stderr": (r.stderr or "")[-2000:]}
    except subprocess.TimeoutExpired:
        return {"harness": harness, "status": "timeout", "final": "",
                "elapsed": round(_t.time() - t0, 1), "mode": "self_evolve"}
    except Exception as e:
        return {"harness": harness, "status": f"err:{type(e).__name__}:{str(e)[:120]}",
                "final": "", "mode": "self_evolve"}


def autonomous_captain_contract(goal: str) -> dict:
    """给 omnigent supervisor 的最小契约。只提供目标 + 工具,不提供编排建议。

    没有任何固定流程/角色/数量/停止规则 —— 模型基于目标和工具返回自主决定下一步。
    """
    return {
        "goal": goal,
        "tools": {
            "list_harnesses": "列出可用 harness(客观事实)",
            "read_harness_history": "读某家累计历史结果,自行判断",
            "read_trace": "深读一次真实 trace/代码证据",
            "run_harness": "真调任意一家(可调用任意次数/顺序)",
        },
        "instruction": (
            "完成目标。你自主决定是否需要调用 agent、调用几个、选择谁、是否并行、"
            "每个调用的目标与验收标准、是否需要复核以及何时停止。"
            "如需了解某家,自行读其累计历史/trace/代码或先小试;不要依赖预设能力标签。"
        ),
    }


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        out = list_harnesses()
    elif cmd == "history":
        out = read_harness_history(sys.argv[2] if len(sys.argv) > 2 else "auto")
    elif cmd == "contract":
        out = autonomous_captain_contract(" ".join(sys.argv[2:]))
    elif cmd == "run":
        out = run_harness(sys.argv[2], sys.argv[3])
    else:
        out = {"error": "usage: list|history <harness>|contract <goal>|run <harness> <task>"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
