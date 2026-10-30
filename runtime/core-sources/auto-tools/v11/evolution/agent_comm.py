#!/usr/bin/env python3
"""agent_comm · v13.88.9i · 2026-07-31
用户: 「派活给agent建立等待时间预估·定时或按步骤给你状态·你再主动汇报给我·
       完整预估任务时间+目标+结果+步骤进展·这样就能通讯和实时监督调整」

问题: 之前派子agent = 黑洞。派出去不知道啥时候回、卡在哪、怎么接管、干等很恶心。

方案: 双向通讯协议 + 进度落盘。
  主进程(我) 派活: register_task(id, goal, eta_sec, steps)
  子agent 每步回报: report(id, step_idx, pct, note, self_check)
  主进程 定时读: read(id) / is_stale(id) / summary(id)
  超时 → 主进程接管(拿已有步骤结果接着做·不干等)

落盘: .agent_comm/<task_id>.json (单文件·原子写·重启不丢)
"""
import os
import json
import tempfile

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".agent_comm")


def _path(task_id: str) -> str:
    os.makedirs(_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(task_id))[:80]
    return os.path.join(_DIR, f"{safe}.json")


def _atomic_write(path: str, data: dict):
    """原子写·防并发读到半截"""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _now() -> float:
    # 不用 Date.now 那类·用 os 时间(脚本环境允许 time)
    import time
    return time.time()


# ═══════════ 主进程 API (我用) ═══════════

def register_task(task_id: str, goal: str, eta_sec: int, steps: list) -> dict:
    """派活登记·定好【目标+预估时间+分步骤】。steps=['查xx','改yy','测zz']"""
    rec = {
        "task_id": task_id,
        "goal": goal,
        "eta_sec": int(eta_sec),
        "started_at": _now(),
        "steps": [{"name": s, "status": "pending", "note": "", "self_check": None}
                  for s in steps],
        "cur_step": 0,
        "pct": 0,
        "done": False,
        "last_report_at": _now(),
    }
    _atomic_write(_path(task_id), rec)
    return rec


def read(task_id: str) -> dict:
    p = _path(task_id)
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def is_stale(task_id: str, grace_sec: int = 120) -> bool:
    """超时判据: 超过 eta + grace 且没 done · 或 last_report 停太久。
    True = 该接管了(别干等黑洞)。"""
    r = read(task_id)
    if not r or r.get("done"):
        return False
    elapsed = _now() - r.get("started_at", _now())
    since_report = _now() - r.get("last_report_at", _now())
    eta = r.get("eta_sec", 600)
    # 2026-07-31 修: 原来「静默超 eta/3」太敏感·agent 在一个长步骤里专心干活
    # (没中途 report)就被误判 stale。改成【超 eta 较多 且 长时间没任何回报】才算真卡。
    # 单纯静默不算(agent 可能在跑一个耗时步骤)·只有超时+静默双条件才判接管。
    over_eta = elapsed > eta + grace_sec
    long_silent = since_report > max(300, eta // 2)   # 静默阈值放宽到 eta/2 且至少 5min
    return over_eta and long_silent                   # 双条件(and)·不再单凭静默误判


def summary(task_id: str) -> str:
    """一行人话进度·供我汇报给用户"""
    r = read(task_id)
    if not r:
        return f"[{task_id}] 无记录"
    elapsed = int(_now() - r.get("started_at", _now()))
    eta = r.get("eta_sec", 0)
    cur = r.get("cur_step", 0)
    steps = r.get("steps", [])
    total = len(steps)
    cur_name = steps[cur]["name"] if 0 <= cur < total else "?"
    tag = "✅完成" if r.get("done") else ("⚠️超时" if is_stale(task_id) else "🔄进行")
    done_steps = sum(1 for s in steps if s.get("status") == "done")
    return (f"[{tag}] {r.get('goal','')[:40]} · "
            f"步骤{done_steps}/{total}(当前:{cur_name[:20]}) · "
            f"{r.get('pct',0)}% · 已用{elapsed}s/预估{eta}s")


# ═══════════ 子 agent API (子agent用) ═══════════

def report(task_id: str, step_idx: int = None, pct: int = None,
           note: str = "", self_check: str = None, done: bool = False):
    """子agent每完成一步/每阶段调一次·回报进度+自检。
    step_idx: 当前第几步(0起)·pct: 总进度%·note: 这步干了啥·
    self_check: 这步的自检结果(测试过没/有没有假设没验证)·done: 全完成"""
    r = read(task_id)
    if not r:
        # 没登记也允许裸报(容错)
        r = {"task_id": task_id, "steps": [], "started_at": _now()}
    if step_idx is not None and 0 <= step_idx < len(r.get("steps", [])):
        # 回报第 N 步 = 这步已完成(下一步是当前)· done=全任务完成
        r["steps"][step_idx]["status"] = "done"
        if note:
            r["steps"][step_idx]["note"] = note[:300]
        if self_check is not None:
            r["steps"][step_idx]["self_check"] = self_check[:300]
        r["cur_step"] = min(step_idx + 1, len(r["steps"]) - 1) if not done else step_idx
    if pct is not None:
        r["pct"] = int(pct)
    if done:
        r["done"] = True
        r["pct"] = 100
    r["last_report_at"] = _now()
    _atomic_write(_path(task_id), r)
    return r


# ═══════════ v13.88.9L-14 · 结论回流 (用户: 子agent完成结论要代码级回到总线·非靠自觉) ═══════════
# 根因: 子agent完成了·结论没任何机制回流→主session以为它死了(big3/7cap坑3次)。
#   register_task/report 只存进度不存结论正文。加 save_result/get_result 补结论存储。
#   约定: 子agent最后必 save_result(task_id, 完整结论)·主session get_result 取·
#   不靠子agent自觉写长报告·不靠主session手动tail/唤醒。

def save_result(task_id: str, result: str, ok: bool = True):
    """子agent完成时调这·把完整结论落盘到任务记录·并标 done。
    result: 完整结论正文(会存全量·不截断)·ok: 成功/失败。
    v13.90: TASK_DONE 通知里的 result 走 context_store 的 maybe_offload——
      短结论直接带全文(不用绕一趟落盘再读)，长结论(>4000字)落盘只带预览+
      ref_id，subscribe(TASK_DONE) 的主进程需要全文时自己 context_store.
      read_back(ref_id) 分页读，不再是硬截断 2000 字后半截丢失。"""
    r = read(task_id)
    if not r:
        r = {"task_id": task_id, "steps": [], "started_at": _now()}
    r["result"] = str(result)          # 全量结论·不截断 (任务级持久化，本就该留全量)
    r["result_ok"] = bool(ok)
    r["done"] = True
    r["pct"] = 100
    r["last_report_at"] = _now()
    r["result_at"] = _now()
    _atomic_write(_path(task_id), r)
    # 同步上总线广播一条 (可见)
    emit("RESULT", task_id, f"结论已回流 ({'ok' if ok else 'fail'})·{len(str(result))}字",
         {"task_id": task_id, "ok": ok})
    # v13.89: 同时发一条 TASK_DONE 通知 (对齐 CC 的 <task-notification>)
    # 让 subscribe(TASK_DONE) 的主进程立即收到，不再靠轮询发现
    try:
        from context_store import maybe_offload
        result_field = maybe_offload(f"task_result_{task_id}", str(result), tag="task_done_result")
    except Exception:
        result_field = str(result)[:2000]
    emit("TASK_DONE", task_id, f"任务完成: {task_id} ({'ok' if ok else 'fail'})",
         {"task_id": task_id, "ok": ok, "result": result_field})
    return r


def get_result(task_id: str) -> dict:
    """主session取子agent结论。返 {done, result, ok, at} · 没完成返 done=False。"""
    r = read(task_id)
    if not r:
        return {"done": False, "result": "", "found": False}
    return {
        "done": bool(r.get("done")),
        "result": r.get("result", ""),
        "ok": r.get("result_ok", None),
        "at": r.get("result_at"),
        "found": True,
    }


def pending_results() -> list:
    """主session一眼看哪些任务已回流结论、哪些还没·治「以为agent死了」黑洞。
    返 [{task_id, goal, done, has_result, silent_s}]。"""
    import glob
    out = []
    for p in glob.glob(os.path.join(_DIR, "*.json")):
        tid = os.path.basename(p)[:-5]
        r = read(tid)
        if not r:
            continue
        out.append({
            "task_id": tid,
            "goal": r.get("goal", "")[:40],
            "done": bool(r.get("done")),
            "has_result": bool(r.get("result")),
            "silent_s": int(_now() - r.get("last_report_at", _now())),
        })
    return sorted(out, key=lambda x: x["silent_s"])


# ═══════════ v13.88.9L · 全系统总线升级 (用户: 全系统agent通讯·全暴露) ═══════════
# 用户要求: 不只我派子agent·全系统所有agent/组件的活动都上总线·全部可见。
#   - 任何组件搜索 → log_search (治「谁又搜了·我让他搜了吗」·全可见可叫停)
#   - 一眼看全部 agent 状态 → summary_all
#   - 按系统 CPU 决定是否再派 agent → can_spawn_more

_BUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".agent_bus.jsonl")

# v13.89 · 融合 Claude Code 的 <task-notification> 模式
# CC 的 task-notification 机制: 子任务完成时自动发一条结构化通知到主 session,
# 主 session 无需轮询就能知道子任务完成了。我们用 emit + subscribe 做到同等效果。
#
# 新增能力:
#   - subscribe(callback): 注册消息回调，emit 时自动触发 (事件驱动，不再纯轮询)
#   - notify_task_complete(task_id, result, ok): 子任务完成的结构化通知
#   - wait_for(task_id, timeout): 阻塞等待某任务完成 (对齐 CC 的 task-notification 同步语义)
#   - pending_notifications(): 拉取未消费的完成通知

_SUBSCRIBERS = []   # [(kind_filter, callback_fn)]
_UNCONSUMED = []     # 未消费的 task-notification 消息

# v13.90 · 上下文工程地基：大结果自动落盘阈值 (字符数)。
# 对齐 context_store.AUTO_OFFLOAD_THRESHOLD_CHARS，独立冗余一份默认值，
# 避免 context_store 不可用时 emit 直接失败(fail-safe 优先于强一致)。
_EXTRA_OFFLOAD_THRESHOLD = 4000


def _offload_large_extra_fields(extra: dict) -> dict:
    """把 extra 字典里过大的字符串字段自动落盘替换成摘要指针。
    fail-safe: context_store 不可用/落盘异常 → 原样截断返回，不阻断 emit。"""
    try:
        from context_store import maybe_offload
    except Exception:
        # 退化处理: 没有 context_store 就简单截断，好过不设限
        return {k: (v[:2000] + "...(截断)" if isinstance(v, str) and len(v) > 2000 else v)
                for k, v in extra.items()}
    out = {}
    for k, v in extra.items():
        if isinstance(v, str) and len(v) > _EXTRA_OFFLOAD_THRESHOLD:
            try:
                out[k] = maybe_offload(f"bus_extra_{k}", v, tag="agent_comm_extra",
                                       threshold=_EXTRA_OFFLOAD_THRESHOLD)
            except Exception:
                out[k] = v[:2000] + "...(截断)"
        else:
            out[k] = v
    return out


# 消息类型 (全 visibility=all·但分类便于过滤/UI 分区)
#   PROGRESS 任务进度 · SEARCH 搜索活动 · ALERT 报警 · INTERNAL 运维 · USER_ASK 需用户回
#   TASK_DONE 子任务完成通知 (对齐 CC 的 <task-notification>)


def emit(kind: str, who: str, msg: str, extra: dict = None):
    """全系统任何 agent/组件往总线发一条·全部可见 (append-only·原子)
    v13.89: emit 后自动触发已注册的 subscribers (事件驱动，不再纯轮询)。
    v13.90: 用户抓正「学 CC/deepagents 核心优势不只是 tool」——接入
      context_store 的大结果卸载。extra 里任何字段的字符串值超过阈值，
      自动落盘只留预览+读回指针，避免大产出(比如 deep_research 全文)
      直接怼进总线把 .agent_bus.jsonl 越撑越大、下游读总线的 agent
      上下文也跟着被撑爆。对齐 deepagents 的 _offload_tool_message_content。
    """
    rec = {"ts": _now(), "kind": kind, "who": who, "msg": str(msg)[:400]}
    if extra:
        extra = _offload_large_extra_fields(extra)
        rec["extra"] = extra
    try:
        os.makedirs(os.path.dirname(_BUS), exist_ok=True)
        with open(_BUS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    # v13.89: 事件驱动·触发 subscribers
    import traceback as _tb
    for kind_filter, cb in list(_SUBSCRIBERS):
        try:
            if kind_filter is None or kind_filter == kind:
                cb(rec)
        except Exception:
            _tb.format_exc()  # 吞掉 subscriber 异常，不影响 emit
    # TASK_DONE 类消息存入未消费列表
    if kind == "TASK_DONE":
        _UNCONSUMED.append(rec)
        # 防 backlog 无限增长
        if len(_UNCONSUMED) > 100:
            _UNCONSUMED.pop(0)
    return rec


def subscribe(callback, kind_filter: str = None):
    """注册一个消息回调函数。emit 时自动触发。
    kind_filter: 只监听某种 kind，None=全监听。
    对齐 CC 的事件驱动通知机制，不再靠定时轮询 tail_bus。
    """
    _SUBSCRIBERS.append((kind_filter, callback))
    return lambda: _SUBSCRIBERS.remove((kind_filter, callback))  # 返回取消订阅函数


def notify_task_complete(task_id: str, result: str, ok: bool = True,
                         who: str = ""):
    """子任务完成时调此函数·发一条结构化 TASK_DONE 通知。
    对齐 CC 的 <task-notification> 机制。
    主进程可 subscribe(TASK_DONE) 或 pending_notifications() 消费。
    """
    return emit("TASK_DONE", who or task_id,
                f"任务完成: {task_id} ({'ok' if ok else 'fail'})",
                {"task_id": task_id, "ok": ok, "result": str(result)[:2000],
                 "task_id_ref": task_id})


def pending_notifications() -> list:
    """拉取所有未消费的 TASK_DONE 通知。拉完后清空。
    对齐 CC 的 task-notification 消费语义。"""
    out = list(_UNCONSUMED)
    _UNCONSUMED.clear()
    return out


def wait_for(task_id: str, timeout: float = 60) -> dict:
    """阻塞等待某个任务完成通知。对齐 CC 的同步等待语义。
    返回 {done, result, ok, elapsed_s}。超时返回 done=False。
    """
    import time as _t
    started = _t.time()
    while _t.time() - started < timeout:
        for i, rec in enumerate(_UNCONSUMED):
            extra = rec.get("extra") or {}
            if extra.get("task_id") == task_id:
                _UNCONSUMED.pop(i)
                return {"done": True, "result": extra.get("result", ""),
                        "ok": extra.get("ok", True),
                        "elapsed_s": round(_t.time() - started, 2)}
        # 也查总线历史
        for r in tail_bus(10, kind="TASK_DONE"):
            extra = r.get("extra") or {}
            if extra.get("task_id") == task_id:
                return {"done": True, "result": extra.get("result", ""),
                        "ok": extra.get("ok", True),
                        "elapsed_s": round(_t.time() - started, 2)}
        _t.sleep(0.5)
    return {"done": False, "result": "", "ok": False,
            "elapsed_s": round(timeout, 2)}


def log_search(who: str, query: str, source: str = "", trigger: str = "auto"):
    """任何组件搜索前调这·上总线 (治「谁又搜了」·who=谁·trigger=user/auto/cron)"""
    return emit("SEARCH", who, f"搜索: {query[:80]}",
                {"query": query[:200], "source": source, "trigger": trigger})


def tail_bus(n: int = 20, kind: str = None) -> list:
    """读总线最近 n 条 (可按 kind 过滤)·供主 agent 汇报用户"""
    if not os.path.exists(_BUS):
        return []
    try:
        lines = open(_BUS, encoding="utf-8").read().strip().splitlines()
    except Exception:
        return []
    out = []
    for line in reversed(lines):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if kind and r.get("kind") != kind:
            continue
        out.append(r)
        if len(out) >= n:
            break
    return list(reversed(out))


def summary_all() -> str:
    """一眼看全部在跑的 agent 任务状态 (主 agent 定时读·汇报用户·不用逐个查)"""
    import glob
    tasks = []
    for p in glob.glob(os.path.join(_DIR, "*.json")):
        tid = os.path.basename(p)[:-5]
        r = read(tid)
        if not r:
            continue
        tasks.append((tid, r))
    if not tasks:
        return "无在跑任务"
    lines = []
    for tid, r in sorted(tasks, key=lambda x: x[1].get("started_at", 0)):
        silent = int(_now() - r.get("last_report_at", _now()))
        tag = ("✅" if r.get("done") else ("⚠️卡" if is_stale(tid) else "🔄"))
        lines.append(f"{tag} {tid[:18]:20} {r.get('pct',0):3}% 静默{silent}s · "
                     f"{r.get('goal','')[:32]}")
    return "\n".join(lines)


def can_spawn_more(max_load: float = 8.0, max_agents: int = 4) -> tuple:
    """按系统 CPU 负载 + 在跑 agent 数·判能否再派子 agent (防雪崩·配合_spawn_gate)。
    返 (bool, reason)。用户: 「是否引入其他子agent(根据系统cpu)」"""
    import glob
    try:
        load1 = os.getloadavg()[0]
    except Exception:
        load1 = 0.0
    active = 0
    for p in glob.glob(os.path.join(_DIR, "*.json")):
        r = read(os.path.basename(p)[:-5])
        if r and not r.get("done"):
            active += 1
    if load1 > max_load:
        return False, f"load {load1:.1f} > {max_load}·别再派(防雪崩)"
    if active >= max_agents:
        return False, f"在跑 {active} agent ≥ {max_agents}·先等完"
    return True, f"load {load1:.1f}·在跑 {active}·可再派"


# ═══════════ v13.88.9L-20 · 启动即上总线 (用户: 「我看不到qa进程·你没用总线?」) ═══════════
# 根因: 后台任务靠我记得手动 register_task 才上总线·我又忘了(裸 nohup & 启动 qa_agent)
#   → 总线看不到=黑洞。跟结论回流/接线门同病: 靠自觉 = 迟早漏。做成机制:
#   launch_tracked 一个函数搞定 register + 后台启动 + 存pid + 进程结束自动 save_result。
#   任何后台任务经它启动就自动上总线可见·不靠自觉。

def launch_tracked(task_id: str, goal: str, argv: list,
                   steps: list = None, eta_sec: int = 600,
                   out_file: str = None, cwd: str = None) -> dict:
    """启动一个后台任务并自动全程上总线。
    task_id: 任务ID · goal: 目标 · argv: subprocess命令列表(如 ["python3","x.py",...])
    steps: 分步(可选) · out_file: 任务stdout落哪(也作save_result来源) · cwd: 工作目录
    返回 {task_id, pid, out_file}。进程结束时后台线程自动 save_result(读out_file尾部)。
    """
    import subprocess as _sp
    import threading as _th
    register_task(task_id, goal, eta_sec, steps or ["启动", "执行", "完成"])
    _out = out_file or os.path.join(tempfile.gettempdir(), f"{task_id}.out")
    try:
        _fh = open(_out, "w")
        proc = _sp.Popen(argv, stdout=_fh, stderr=_sp.STDOUT, cwd=cwd)
    except Exception as e:
        save_result(task_id, f"启动失败: {type(e).__name__}: {e}", ok=False)
        return {"task_id": task_id, "pid": None, "error": str(e)[:120]}
    emit("LAUNCH", "launch_tracked", f"后台启动: {goal[:60]} (pid={proc.pid})",
         {"task_id": task_id, "pid": proc.pid, "out_file": _out,
          "argv": " ".join(str(a) for a in argv)[:200]})

    def _watch():
        # 等进程结束 → 读输出尾部 → 自动 save_result (结论回流·不靠任务自己报)
        rc = proc.wait()
        try:
            _fh.close()
        except Exception:
            pass
        tail = ""
        try:
            with open(_out, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-4000:]   # 尾部4000字作结论摘要
        except Exception:
            pass
        save_result(task_id,
                    f"[退出码 {rc}]\n{tail}",
                    ok=(rc == 0))

    t = _th.Thread(target=_watch, daemon=True)
    t.start()
    return {"task_id": task_id, "pid": proc.pid, "out_file": _out}


if __name__ == "__main__":
    # demo/自测
    import sys
    if "--consume-search" in sys.argv:
        # v13.90 · L2-7 · SEARCH 事件消费者 (根因: 3902 条 SEARCH 事件无人消费)
        # 输出搜索热度到 .search_heatmap.jsonl·供 search_quality_monitor / code_evolver 用
        heatmap_path = os.path.join(os.path.dirname(_BUS), ".search_heatmap.jsonl")
        from collections import Counter
        from datetime import datetime as _dt
        events = tail_bus(5000, kind="SEARCH")
        who_counter = Counter(e.get("who", "?") for e in events)
        query_counter = Counter((e.get("extra") or {}).get("query", e.get("msg", ""))[:80] for e in events)
        rec = {
            "ts": _dt.now().isoformat(),
            "n_events": len(events),
            "top_who": who_counter.most_common(10),
            "top_query": query_counter.most_common(15),
        }
        with open(heatmap_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"heatmap: {len(events)} SEARCH events · top who: {who_counter.most_common(3)}")
        sys.exit(0)
    if "--bus-test" in sys.argv:
        log_search("deep_research", "AI记忆机制 Letta", "friday_bing", "cron")
        log_search("content_supervisor", "SaaS续费访谈", "zhihu", "auto")
        emit("PROGRESS", "main", "spawn咽喉收口完成")
        print("=== 总线最近事件 ===")
        for r in tail_bus(5):
            print(f"  [{r['kind']}] {r['who']}: {r['msg']}")
        print("=== 搜索活动(谁又搜了) ===")
        for r in tail_bus(5, kind="SEARCH"):
            print(f"  {r['who']} ({r['extra']['trigger']}): {r['extra']['query'][:40]}")
        print("=== 全部agent状态 ===")
        print(summary_all())
        print("=== 能否再派agent ===")
        print(can_spawn_more())
        sys.exit(0)
    if "--test" in sys.argv:
        register_task("t1", "测试通讯", 60, ["查", "改", "测"])
        print("派活:", summary("t1"))
        report("t1", 0, 33, "查完了", "grep确认过")
        print("回报1:", summary("t1"))
        report("t1", 1, 66, "改完了", "py_compile过")
        print("回报2:", summary("t1"))
        report("t1", 2, 100, "测完了", "pytest 5绿", done=True)
        print("完成:", summary("t1"))
        print("stale?", is_stale("t1"))
        os.unlink(_path("t1"))
        print("自测通过·清理完")
