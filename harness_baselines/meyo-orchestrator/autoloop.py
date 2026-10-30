# -*- coding: utf-8 -*-
"""极简自主 tool-loop 驱动器 (2026-08-25)

这不是编排器 —— 它【零编排逻辑】。只做一件事:把 4 个客观工具喂给模型,
模型自己决定调哪个/调几次/何时停,loop 忠实执行工具并回灌结果,直到模型不再调工具。

用户铁律的最终形态:是否用agent、几个、派谁、顺序、复核、停止 —— 全在模型的
tool_calls 里,本文件不含任何 if task_kind / recommend / 固定流程。

依赖:公司网关 function-calling(已验证支持)。可独立跑,不需装全 omnigent。
omnigent supervisor 接 captain.yaml 后是同一语义的生产版;本文件是可立即验证的等价最小实现。
"""
from __future__ import annotations
import json, sys, os, urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "meyo_harnesses"))
import orchestrator as O
import mcp_bridge as MB
import skills as SK
import templates as ST
import health as H
import cascade as CX

GW = "https://aigc.sankuai.com/v1/openai/native/chat/completions"
APPID = os.environ.get("MEYO_UNIFIED_APPID") or os.environ.get("AIGC_APPID", "")
MODEL = "gpt-5.6-sol"


def _extract_content(msg: dict) -> str:
    """reasoning 兼容(全系列 harness 铁律·2026-09-02):GLM 系等推理模型小 max_tokens
    时思考吃光预算,答案落 reasoning_content 而 content 为空。所有直调网关读答案处
    统一走本函数:content 空 → reasoning_content 兜底。与 CCR 插件/call_model.py 同款。"""
    c = msg.get("content") or ""
    if c:
        return c
    return msg.get("reasoning_content") or ""

# 当前解题上下文:qhash 由外层固定注入·模型【不能】传(防串题);gold【不给】模型(防作弊)。
_SOLVE_CTX = {"qhash": "adhoc", "gold": ""}


import threading, time as _time

# 子 agent 硬超时(秒):只兜【真卡死】——须 > 各家自身墙钟(UNIFIED_WALL_S=1200)。
# 实测 cc 历史中位600s·max1200s·60%题>360s:360s会误杀正常cc·故设1260(墙钟+60s余量)。
# codex 卡23分钟(>1260s)那种才是真异常·会被熔断。
SUBAGENT_HARD_TIMEOUT = 1260
_SUB_LOG_DIR = Path(__file__).resolve().parent / "subagent_logs"


def _run_harness_tool(harness, task, mode="single", **k):
    """调子 agent · 全程可追踪。mode 由 captain 模型自主选择:
      single      = 普通单次执行
      self_evolve = 调 penguin/prime 原生 --goal 模式·agent 自己形成子目标并迭代

    是否启用自进化也不写死·完全由模型 tool_call 决定。qhash/gold仍外层固定。
    """
    _SUB_LOG_DIR.mkdir(exist_ok=True)
    t0 = _time.time()
    # 熔断护栏:某家连续失败/超慢已被短路时,如实告知模型(不假跑、不静默)。
    if H.circuit_open(harness):
        snap = H.health_snapshot().get("breakers", {}).get(harness, {})
        print(f"    ⚡ [熔断短路] {harness} circuit_open·冷却中·跳过", flush=True)
        return {"harness": harness, "final": "", "status": "circuit_open",
                "note": "该家连续失败/超慢已被熔断冷却·可换家或稍后再试",
                "breaker": snap}
    # 每次子 agent 调用一个独立日志文件(harness+起始秒),可事后完整复盘
    stamp = str(int(t0))
    logf = _SUB_LOG_DIR / f"{_SOLVE_CTX['qhash']}_{harness}_{stamp}.json"
    print(f"    ▶ [子agent开始] {harness} · mode={mode} · task={task[:55]!r} · log={logf.name}", flush=True)

    box = {"out": None, "done": False}

    def _call():
        try:
            if mode == "self_evolve":
                box["out"] = O.run_self_evolve(harness, task,
                                               qhash=_SOLVE_CTX["qhash"], gold=_SOLVE_CTX["gold"])
            else:
                box["out"] = O.run_harness(harness, task,
                                           qhash=_SOLVE_CTX["qhash"], gold=_SOLVE_CTX["gold"])
        except Exception as e:
            box["out"] = {"harness": harness, "final": "",
                          "status": f"err:{type(e).__name__}:{str(e)[:120]}"}
        finally:
            box["done"] = True

    th = threading.Thread(target=_call, daemon=True)
    th.start()
    # 心跳:每 30s 打一次"还在跑",这样绝不会再出现"黑盒23分钟无日志"
    while th.is_alive():
        # join 最长30s,但临近硬超时按剩余秒数醒来·小timeout测试也不会等满30s。
        remain = max(0.1, SUBAGENT_HARD_TIMEOUT - (_time.time() - t0))
        th.join(timeout=min(30, remain))
        el = round(_time.time() - t0)
        if box["done"]:
            break
        if el >= SUBAGENT_HARD_TIMEOUT:
            out = {"harness": harness, "final": "",
                   "status": f"HARD_TIMEOUT_{SUBAGENT_HARD_TIMEOUT}s",
                   "note": "子agent超硬超时·熔断(线程遗弃·结果作废)"}
            print(f"    ✖ [子agent熔断] {harness} 超 {SUBAGENT_HARD_TIMEOUT}s·放弃", flush=True)
            H.record_result(harness, ok=False, elapsed_s=el)   # 超时计失败·可能触发熔断
            _write_sub_log(logf, harness, task, out, el)
            return out
        print(f"    · [子agent心跳] {harness} 已跑 {el}s...", flush=True)

    out = box["out"] or {"harness": harness, "final": "", "status": "no_output"}
    el = round(_time.time() - t0, 1)
    # 记录结果到熔断器:status 以 ok/err 开头判成败(与各 runner 约定一致)。
    _st = str(out.get("status", ""))
    _ok = _st == "ok" or _st.startswith("ok")
    H.record_result(harness, ok=_ok, elapsed_s=el)
    print(f"    ◀ [子agent完成] {harness} · {el}s · status={out.get('status')} · "
          f"final={str(out.get('final',''))[:60]!r}", flush=True)
    _write_sub_log(logf, harness, task, out, el)
    return out


def _write_sub_log(logf, harness, task, out, elapsed):
    """子 agent 完整结果落盘(含 stdout/stderr/final/status·遵 CLAUDE.md 完整trace)。"""
    try:
        logf.write_text(json.dumps({
            "harness": harness, "qhash": _SOLVE_CTX["qhash"], "task": task,
            "elapsed": elapsed, "status": out.get("status"),
            "final": out.get("final", ""),
            "stdout": str(out.get("stdout", ""))[:20000],
            "stderr": str(out.get("stderr", ""))[:8000],
            "full_result": {kk: (str(vv)[:4000] if isinstance(vv, str) else vv)
                            for kk, vv in out.items()},
        }, ensure_ascii=False, indent=2))
    except Exception:
        pass


# 工具 = orchestrator 的客观函数(与 captain.yaml 同一套)
TOOLS_IMPL = {
    "list_harnesses": lambda **k: O.list_harnesses(),
    "harness_stats": lambda **k: O.harness_stats(),
    "read_harness_history": lambda harness, limit=20, **k: O.read_harness_history(harness, limit),
    "probe_harness_latency": lambda harness, timeout_s=30, **k: O.probe_harness_latency(harness, timeout_s),
    "judge_answer": lambda question, candidate_answer, judge_harness="cc", timeout_s=120, **k: O.judge_answer(question, candidate_answer, judge_harness, timeout_s),
    "read_trace": lambda trace_path, **k: O.read_trace(trace_path),
    "run_harness": _run_harness_tool,
    "list_mcp_tools": lambda **k: MB.list_mcp_tools(),
    "call_mcp_tool": lambda server, name, arguments=None, **k: MB.call_mcp_tool(server, name, arguments or {}),
    "list_skills": lambda source="pm-skills", **k: SK.list_skills(source),
    "read_skill": lambda source, path, **k: SK.read_skill(source, path),
    "list_templates": lambda **k: ST.list_templates(),
    "instantiate_template": lambda key, params=None, **k: ST.instantiate_template(key, params or {}),
    "health_snapshot": lambda **k: H.health_snapshot(RUN_DIR),
    "run_cascade": lambda task, **k: CX.run_cascade(
        task, qhash=_SOLVE_CTX["qhash"], gold=_SOLVE_CTX["gold"],
        run_fn=lambda h, t, q, g: _run_harness_tool(h, t)),
}

TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "list_harnesses", "description": "列出可调用的 harness(客观事实,无评分)",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "harness_stats",
        "description": "全部harness的客观聚合统计(命中率/中位耗时/中位搜索数)·给你全局性价比视角·自行权衡",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "read_harness_history",
        "description": "读某家累计历史执行结果,自行判断其水平/失败模式",
        "parameters": {"type": "object", "properties": {
            "harness": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["harness"]}}},
    {"type": "function", "function": {
        "name": "probe_harness_latency",
        "description": "对某家做极小实时探针(2+2)测【当前】速度/健康·避免凭过时历史选到现在卡死的家。返回elapsed_s/status。",
        "parameters": {"type": "object", "properties": {
            "harness": {"type": "string"}, "timeout_s": {"type": "integer"}},
            "required": ["harness"]}}},
    {"type": "function", "function": {
        "name": "judge_answer",
        "description": "让任意裁判harness独立复核候选答案(默认auto·实测约22s)。你自主决定要不要裁判/用谁。",
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"}, "candidate_answer": {"type": "string"},
            "judge_harness": {"type": "string"}, "timeout_s": {"type": "integer"}},
            "required": ["question", "candidate_answer"]}}},
    {"type": "function", "function": {
        "name": "read_trace", "description": "深读一次完整 trace(路径来自 read_harness_history)",
        "parameters": {"type": "object", "properties": {
            "trace_path": {"type": "string"}}, "required": ["trace_path"]}}},
    {"type": "function", "function": {
        "name": "run_harness",
        "description": "真调某家执行任务(你决定调谁/几次/顺序)。mode=self_evolve 时"
                       "penguin/prime走原生自进化goal模式(自己拆子目标迭代);默认single单次。",
        "parameters": {"type": "object", "properties": {
            "harness": {"type": "string"}, "task": {"type": "string"},
            "mode": {"type": "string", "enum": ["single", "self_evolve"]}},
            "required": ["harness", "task"],
            "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "list_mcp_tools",
        "description": "客观列出所有已注册MCP server的工具(server/name/描述/入参schema)。无评分或推荐。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "call_mcp_tool",
        "description": "真调某个MCP工具。你自主决定server/tool/参数与调用次数。",
        "parameters": {"type": "object", "properties": {
            "server": {"type": "string"}, "name": {"type": "string"},
            "arguments": {"type": "object"}},
            "required": ["server", "name"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "list_skills",
        "description": "客观列出某skill源(默认pm-skills)里可用的SKILL.md。你自主决定是否用/交给谁执行。",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "read_skill",
        "description": "读取某个skill的完整内容(方法论/步骤),供你决定如何应用。",
        "parameters": {"type": "object", "properties": {
            "source": {"type": "string"}, "path": {"type": "string"}},
            "required": ["source", "path"]}}},
    {"type": "function", "function": {
        "name": "list_templates",
        "description": "列出10个C端场景模板(目标/验收/可选工具)·不绑定harness。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "instantiate_template",
        "description": "用参数实例化一个场景模板,得到清晰目标+验收标准;缺参数会如实列出。",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}, "params": {"type": "object"}},
            "required": ["key"]}}},
    {"type": "function", "function": {
        "name": "health_snapshot",
        "description": "查看当前健康:哪些harness被熔断(circuit_open)、活跃子进程。避免调已知卡死的家。",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "run_cascade",
        "description": "置信级联执行:先跑最强主家CC,用其执行特征判断是否失败,疑似失败才升级到"
                       "DeepAgents。40题实测17→21优于任何单家。适合要极限正确率时用。",
        "parameters": {"type": "object", "properties": {"task": {"type": "string"}},
            "required": ["task"]}}},
]

SYSTEM = (
    "你是多-agent 编排队长,可调用 9 家外部 agent。所有编排决策由你自主:"
    "是否需要 agent、几个、派谁、串/并行、每个子目标与验收、是否让另一家独立复核、"
    "何时停止。不要凭预设标签选人——先用 read_harness_history/read_trace 读真实战绩或"
    "小规模 run 试探再判断。仅当答案每条约束都被证据独立支持才交卷;否则如实说不确定"
    "并列已核实/未核实的约束。\n"
    "你还可用 list_mcp_tools 看有哪些外部 MCP 工具(如桌面控制/视觉),call_mcp_tool 调用;"
    "用 list_templates/instantiate_template 把模糊诉求变成清晰目标+验收。是否用、用哪个"
    "以及如何编排仍由你自主决定。")


# opus4.8 长会话实测会间歇 404(单发OK·多轮偶发)。重试+退避·仍失败降级备用模型。
FALLBACK_MODEL = "gpt-5.6-sol"


def _post_once(messages, model):
    if not APPID or APPID.startswith("appid-"):
        raise ValueError("Configure a resolved MEYO_UNIFIED_APPID or AIGC_APPID")
    body = {"model": model, "messages": messages, "tools": TOOLS_SCHEMA, "max_tokens": 1500}
    req = urllib.request.Request(GW, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {APPID}"})
    return json.loads(urllib.request.urlopen(req, timeout=300).read())


def _post(messages):
    """治反复出现的 opus4.8 长会话 404:主模型退避重试·仍挂降级 gpt-5.6-sol。

    错误分类与退避序列收敛到 health 模块(可测):可重试码退避,不可重试码(如400)直接抛。
    """
    import time as _t
    last = None
    delays = H.backoff_schedule(3)          # [2,4,6]·与历史行为一致
    for attempt in range(3):
        try:
            return _post_once(messages, MODEL)
        except Exception as e:
            last = e
            code = getattr(e, "code", None)
            if not H.is_retryable(code):     # 400 等参数错→直接抛,不空转重试
                raise
            _t.sleep(delays[attempt])
    # 主模型三连挂 → 降级备用(单发稳)
    try:
        return _post_once(messages, FALLBACK_MODEL)
    except Exception:
        raise last


TRACE_DIR = Path(__file__).resolve().parent / "autoloop_traces"
RUN_DIR = Path(__file__).resolve().parent / "runs"   # 活跃跑的 PID 注册(治僵尸误读)


def _register_run(qhash):
    """写本次跑的 PID 文件·并清理已死的旧 PID(收敛到 health.register_pid·可测)。"""
    return H.register_pid(RUN_DIR, qhash)


def _finish_run(pf):
    try:
        if pf:
            Path(pf).unlink(missing_ok=True)
    except Exception:
        pass


def _save_trace(goal, trace, final, status, n_steps):
    """遵 CLAUDE.md 硬要求:每次跑落完整 tool_log + final + status。fail-safe。"""
    try:
        TRACE_DIR.mkdir(exist_ok=True)
        import hashlib
        h = hashlib.md5(goal.encode()).hexdigest()[:12]
        # 无 Date.now·用 step 计数 + goal hash 命名(可复现)
        p = TRACE_DIR / f"autoloop_{h}.json"
        p.write_text(json.dumps({
            "goal": goal, "status": status, "n_steps": n_steps,
            "final": final, "tool_log": trace,
        }, ensure_ascii=False, indent=2))
        return str(p)
    except Exception:
        return ""


def autonomous_solve(goal: str, max_steps: int = 12, verbose: bool = True,
                     qhash: str = "adhoc", gold: str = "", cancel_event=None) -> dict:
    """模型全自主解题。返回 {final, steps[], tool_calls[]}。loop 零编排逻辑。

    qhash/gold 由外层固定注入 _SOLVE_CTX,模型调 run_harness 时用它,不能自己传
    (防串题 + 防 gold 泄漏作弊)。
    """
    _SOLVE_CTX["qhash"] = qhash or "adhoc"
    _SOLVE_CTX["gold"] = gold or ""
    _pf = _register_run(_SOLVE_CTX["qhash"])   # 注册本次跑PID·清死僵尸
    try:
        return _solve_loop(goal, max_steps, verbose, cancel_event)
    finally:
        _finish_run(_pf)


def _solve_loop(goal, max_steps, verbose, cancel_event=None):
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": goal}]
    trace = []
    for step in range(max_steps):
        if cancel_event is not None and cancel_event.is_set():
            tp = _save_trace(goal, trace, "", "interrupted", step)
            return {"final": "", "status": "interrupted", "interrupt_reason": "cancelled",
                    "steps": trace, "n_steps": step, "trace_path": tp}
        try:
            resp = _post(messages)
        except Exception as e:
            _save_trace(goal, trace, "", f"gateway_err:{type(e).__name__}", step)
            return {"final": "", "error": f"gateway:{type(e).__name__}:{str(e)[:120]}",
                    "steps": trace}
        msg = resp["choices"][0]["message"]
        fin = resp["choices"][0].get("finish_reason")
        messages.append(msg)
        tcs = msg.get("tool_calls") or []
        if not tcs:  # 模型自主决定停(不再调工具)→ 交卷
            _fin = _extract_content(msg)
            tp = _save_trace(goal, trace, _fin, "done", step)
            return {"final": _fin, "steps": trace, "n_steps": step, "trace_path": tp}
        # 执行模型自己选的每个工具调用
        for tc in tcs:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except Exception:
                args = {}
            if verbose:
                print(f"  [step{step}] 模型自主调用 {name}({json.dumps(args,ensure_ascii=False)[:80]})", flush=True)
            try:
                out = TOOLS_IMPL[name](**args)
            except Exception as e:
                out = {"error": f"{type(e).__name__}:{str(e)[:120]}"}
            trace.append({"step": step, "tool": name, "args": args,
                          "result_preview": json.dumps(out, ensure_ascii=False)[:300]})
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": json.dumps(out, ensure_ascii=False)[:8000]})
    # 工具预算耗尽后再给模型一次【只综合、不再调工具】机会。旧行为直接返回
    # “[达步数上限未收敛]”，会把已经花成本取得的成员结果全部浪费掉。
    try:
        synthesis_messages = list(messages)
        synthesis_messages.append({"role": "system", "content": (
            "工具调用预算已经耗尽。禁止继续调用任何工具；请仅依据已有工具结果，"
            "立即给用户一个完整、诚实的最终答案，并明确仍未验证的部分。")})
        # GLM 系推理模型思考写 reasoning_content 且吃预算:小 max_tokens 思考吃光→content 空。
        # (同 call_model.py/CCR 插件 2026-09-02 根修)其余模型维持 1800 不变。
        _mt = 8192 if "glm" in MODEL.lower() else 1800
        if not APPID or APPID.startswith("appid-"):
            raise ValueError("Configure a resolved MEYO_UNIFIED_APPID or AIGC_APPID")
        body = {"model": MODEL, "messages": synthesis_messages, "max_tokens": _mt}
        req = urllib.request.Request(GW, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {APPID}"})
        resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
        final = _extract_content(resp["choices"][0]["message"]).strip()
    except Exception:
        final = ""
    status = "done_after_budget" if final else "max_steps"
    rendered = final or "[达步数上限未收敛]"
    tp = _save_trace(goal, trace, rendered, status, max_steps)
    return {"final": rendered, "status": status, "steps": trace,
            "n_steps": max_steps, "trace_path": tp}


if __name__ == "__main__":
    goal = sys.argv[1] if len(sys.argv) > 1 else "列出可用 harness 并说明你会怎么用它们"
    qh = sys.argv[2] if len(sys.argv) > 2 else "adhoc"   # 固定当前题qhash(模型不能改)
    gd = sys.argv[3] if len(sys.argv) > 3 else ""        # gold仅外层评分用·不进模型
    r = autonomous_solve(goal, qhash=qh, gold=gd)
    print("\n=== 模型自主最终答复 ===")
    print(r.get("final", "")[:1500])
    print(f"\n(自主调用工具 {len(r.get('steps',[]))} 次·{r.get('n_steps','?')} 轮)")
